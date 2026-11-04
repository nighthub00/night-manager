"""
features/client_tracker.py
Every running Roblox client, followed once every 2 seconds: which account it is, what its log
says (joining, in a game, teleporting, disconnected and why), whether its window has stopped
responding, and what it uses. Auto-rejoin, the Clients page, client limits, recent servers,
crash history and Discord alerts all read from here.

Nothing here reads or writes a client's memory: the account and state come from the client's
own log (tied to it from the file side, `roblox_logs`), the window state from ordinary window
calls, and the usage from short-lived PROCESS_QUERY_LIMITED_INFORMATION queries.
"""

from __future__ import annotations

import ctypes
import threading
import time
from dataclasses import dataclass
from typing import Callable

import psutil
import win32con
import win32gui
import win32process

from features import client_history, roblox_logs
import features.presence as presence_mod
import features.settings_store as settings_store

TICK_SECONDS = 2.0
# Ticks (about 2 s each) a window must not respond in a row to count as hung: one minute.
HANG_TICKS = 30
# Memory samples in a row over the limit before a restart, so one spike isn't enough.
MEMORY_TICKS = 3
_user32 = ctypes.windll.user32
_user32.IsHungAppWindow.argtypes = [ctypes.c_void_p]
_user32.IsHungAppWindow.restype = ctypes.c_bool


@dataclass
class TrackedClient:
    pid: int
    create_time: float
    process: psutil.Process | None = None
    tail: roblox_logs.LogTail | None = None
    username: str = ""
    user_id: str = ""
    hwnd: int = 0
    hung_ticks: int = 0
    memory_high_ticks: int = 0
    private_mb: float = 0.0
    rss_mb: float = 0.0
    cpu_percent: float = 0.0
    closing: bool = False
    seen_joins: int = 0
    followed: bool = False

    @property
    def key(self) -> tuple[int, float]:
        return self.pid, self.create_time

    @property
    def facts(self) -> roblox_logs.LogFacts:
        return self.tail.facts if self.tail else roblox_logs.LogFacts()

    @property
    def hung(self) -> bool:
        return self.hung_ticks >= HANG_TICKS

    @property
    def uptime(self) -> float:
        return max(0.0, time.time() - self.create_time)

    @property
    def state(self) -> str:
        facts = self.facts
        if self.tail is None:
            return "starting"
        if facts.teleporting:
            return "teleporting"
        if facts.disconnected:
            return "disconnected"
        if facts.in_game:
            return "in game"
        return "joining" if facts.joins else "starting"

    def snapshot(self) -> dict:
        facts = self.facts
        return {
            "pid": self.pid,
            "create_time": self.create_time,
            "username": self.username,
            "user_id": self.user_id,
            "hwnd": self.hwnd,
            "state": self.state,
            "hung": self.hung,
            "place_id": facts.place_id,
            "job_id": facts.job_id,
            "reason": facts.reason,
            "uptime": self.uptime,
            "cpu_percent": self.cpu_percent,
            "rss_mb": self.rss_mb,
            "private_mb": self.private_mb,
            "log_path": self.tail.path if self.tail else "",
        }


def main_window(pid: int, windows: list[int] | None = None) -> int:
    """A client's main window: its biggest top-level window without an owner. Hidden
    windows count too, so a client hidden by the Headless Manager is still found."""
    if windows is None:
        windows = presence_mod.get_windows_by_pid({pid}).get(pid, [])
    best: tuple[int, int, int] | None = None
    for hwnd in windows:
        try:
            if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                continue
            if not (win32gui.GetWindowText(hwnd) or "").strip():
                continue
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            area = max(0, right - left) * max(0, bottom - top)
            visible = int(bool(win32gui.IsWindowVisible(hwnd)))
            candidate = (visible, area, hwnd)
            if area and (best is None or candidate > best):
                best = candidate
        except Exception:
            continue
    return best[2] if best else 0


def foreground_pid() -> int:
    try:
        hwnd = win32gui.GetForegroundWindow()
        return win32process.GetWindowThreadProcessId(hwnd)[1] if hwnd else 0
    except Exception:
        return 0


def classify_end(client: TrackedClient) -> str:
    """How a client that is gone ended: closed (by us), left, disconnected, hung or crashed."""
    facts = client.facts
    if client.closing:
        return "closed"
    if client.hung:
        return "hung"
    if facts.disconnected and facts.left and not facts.teleporting:
        return "left"
    if facts.disconnected:
        return "disconnected"
    if client.tail is None or not facts.joins:
        return "closed"
    return "crashed"


class ClientTracker:
    def __init__(self, manager=None):
        self._manager = manager
        self._lock = threading.RLock()
        self._clients: dict[tuple[int, float], TrackedClient] = {}
        self._launches: dict[str, tuple[str, float]] = {}  # tracker id -> (username, at)
        self._listeners: list[Callable[[str, dict], None]] = []
        self._tick_hooks: list[Callable[[list[TrackedClient]], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cpu_count = max(1, psutil.cpu_count() or 1)
        self.memory_limit_mb = 0

    # lifecycle

    def set_manager(self, manager) -> None:
        self._manager = manager

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ClientTracker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def add_listener(self, callback: Callable[[str, dict], None]) -> None:
        """`callback(event, data)`: "clients" (list of snapshots, every tick), "joined" and
        "ended" (one client's snapshot, "ended" with its "kind")."""
        self._listeners.append(callback)

    def add_tick_hook(self, callback: Callable[[list[TrackedClient]], None]) -> None:
        """Runs on the tracker thread after every tick with the live clients (limits)."""
        self._tick_hooks.append(callback)

    # queries

    def clients(self) -> list[TrackedClient]:
        with self._lock:
            return list(self._clients.values())

    def get(self, key: tuple[int, float]) -> TrackedClient | None:
        with self._lock:
            return self._clients.get(key)

    def for_user(self, user_id: str) -> list[TrackedClient]:
        with self._lock:
            return [c for c in self._clients.values() if c.user_id == str(user_id)]

    def snapshots(self) -> list[dict]:
        with self._lock:
            return [c.snapshot() for c in sorted(self._clients.values(), key=lambda c: c.create_time)]

    # launches and closing

    def note_launch(self, username: str, launch_data) -> None:
        """A launch we started: its tracker id names the account before its log does."""
        if not isinstance(launch_data, dict):
            return
        tracker_id = str(launch_data.get("browser_tracker_id") or "")
        if tracker_id:
            with self._lock:
                self._launches[tracker_id] = (username, time.time())
                cutoff = time.time() - 3600
                self._launches = {k: v for k, v in self._launches.items() if v[1] >= cutoff}

    def mark_closing(self, key: tuple[int, float]) -> None:
        with self._lock:
            client = self._clients.get(key)
            if client:
                client.closing = True

    def close(self, key: tuple[int, float], timeout: float = 5.0) -> bool:
        """Ends one client (checked against its start time, so never a reused PID)."""
        self.mark_closing(key)
        pid, create_time = key
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - create_time) > 0.01:
                return True
            for child in process.children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            process.kill()
            process.wait(timeout=timeout)
            return True
        except psutil.NoSuchProcess:
            return True
        except (psutil.Error, OSError) as exc:
            print(f"[WARNING] Could not close Roblox PID {pid}: {exc}")
            return False

    def close_all(self) -> int:
        closed = 0
        for client in self.clients():
            if self.close(client.key):
                closed += 1
        return closed

    # the loop

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                print(f"[ERROR] Client tracker: {type(exc).__name__}: {exc}")
            self._stop.wait(max(0.2, TICK_SECONDS - (time.monotonic() - started)))

    def _uid_map(self) -> dict[str, str]:
        if self._manager is None:
            return {}
        try:
            return presence_mod._build_uid_map(self._manager)
        except Exception:
            return {}

    def tick(self) -> None:
        self.memory_limit_mb = int(settings_store.get("recovery_memory_limit_mb", 0) or 0)
        processes = presence_mod.get_roblox_processes(force=True)
        live = {(pid, data[0]): data[1] for pid, data in processes.items()}
        uid_map = self._uid_map()

        with self._lock:
            ended = [c for key, c in self._clients.items() if key not in live]
            for client in ended:
                self._clients.pop(client.key, None)
            for key, process in live.items():
                if key not in self._clients:
                    self._clients[key] = TrackedClient(pid=key[0], create_time=key[1], process=process)
            current = list(self._clients.values())

        for client in ended:
            self._on_ended(client)

        windows = presence_mod.get_windows_by_pid({c.pid for c in current})
        claimed = {c.tail.path for c in current if c.tail}
        for client in current:
            self._follow(client, windows.get(client.pid, []), uid_map, claimed)

        for hook in list(self._tick_hooks):
            try:
                hook(current)
            except Exception as exc:
                print(f"[ERROR] Client tracker hook: {type(exc).__name__}: {exc}")
        self._emit("clients", [c.snapshot() for c in sorted(current, key=lambda c: c.create_time)])

    def _follow(self, client: TrackedClient, windows: list[int], uid_map: dict[str, str],
                claimed: set[str]) -> None:
        if client.tail is None:
            path = presence_mod.open_log_for_pid(client.pid, client.create_time)
            if path and path not in claimed:
                client.tail = roblox_logs.LogTail(path)
                claimed.add(path)
        if client.tail is not None:
            facts = client.tail.read_new()
            if facts.user_id:
                client.user_id = facts.user_id
            launched = self._launches.get(facts.browser_tracker_id) if facts.browser_tracker_id else None
            name = uid_map.get(client.user_id, "") if client.user_id else ""
            if not name and launched:
                name = launched[0]
            if name:
                client.username = name
            if facts.joins != client.seen_joins and facts.in_game and facts.job_id:
                client.seen_joins = facts.joins
                if client.username:
                    client_history.add_recent_server(client.username, facts.place_id, facts.job_id)
                # A client already in a game when we first saw it didn't just join.
                if client.followed:
                    self._emit("joined", client.snapshot())
            client.followed = True

        client.hwnd = main_window(client.pid, windows)
        try:
            hung = bool(client.hwnd) and _user32.IsHungAppWindow(client.hwnd)
        except Exception:
            hung = False
        client.hung_ticks = client.hung_ticks + 1 if hung else 0

        process = client.process
        if process is not None:
            try:
                memory = process.memory_info()
                client.rss_mb = memory.rss / 1048576
                client.private_mb = getattr(memory, "private", memory.rss) / 1048576
                client.cpu_percent = min(100.0, process.cpu_percent(interval=None) / self._cpu_count)
            except (psutil.Error, OSError):
                pass
        limit = int(self.memory_limit_mb or 0)
        if limit and client.private_mb >= limit:
            client.memory_high_ticks += 1
        else:
            client.memory_high_ticks = 0

    def memory_high(self, client: TrackedClient) -> bool:
        return bool(self.memory_limit_mb) and client.memory_high_ticks >= MEMORY_TICKS

    def _on_ended(self, client: TrackedClient) -> None:
        kind = classify_end(client)
        snapshot = client.snapshot()
        snapshot["kind"] = kind
        if kind in ("crashed", "hung", "disconnected"):
            facts = client.facts
            name = client.username or f"PID {client.pid}"
            client_history.add_crash(
                client.username, kind, facts.reason, facts.place_id, client.uptime,
            )
            # The autopsy: what the client's log said last, cleaned of anything secret.
            print(
                f"[WARNING] Roblox client for {name} {kind} after "
                f"{int(client.uptime // 60)} min"
                + (f" ({facts.reason})" if facts.reason else "")
            )
            if client.tail is not None and kind != "disconnected":
                lines = client.tail.tail(12)
                if lines:
                    print("[INFO] Its last log lines:\n  " + "\n  ".join(lines))
        self._emit("ended", snapshot)

    def _emit(self, event: str, data) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, data)
            except Exception:
                pass


_TRACKER: ClientTracker | None = None
_TRACKER_LOCK = threading.Lock()


def shutdown() -> None:
    with _TRACKER_LOCK:
        if _TRACKER is not None:
            _TRACKER.stop()


def get_tracker(manager=None) -> ClientTracker:
    """The one tracker, started on first use."""
    global _TRACKER
    with _TRACKER_LOCK:
        if _TRACKER is None:
            _TRACKER = ClientTracker(manager)
        elif manager is not None and _TRACKER._manager is None:
            _TRACKER.set_manager(manager)
        _TRACKER.start()
        return _TRACKER
