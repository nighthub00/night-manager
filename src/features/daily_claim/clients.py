"""
RobloxClientManager: launches one account's client through the existing launcher, finds that
client in the client tracker, waits for it to reach the game and closes it again.

Only clients this manager launched are ever closed. A client is identified by (pid, process
start time), so a reused PID can never be mistaken for ours.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

import psutil

from features.daily_claim.adapters import AdapterError

ROBLOX_PROCESS_NAMES = {"robloxplayerbeta.exe", "windows10universal.exe"}


@dataclass
class ClientHandle:
    username: str
    pid: int
    create_time: float
    simulated: bool = False

    @property
    def key(self) -> tuple[int, float]:
        return self.pid, self.create_time

    @property
    def ref(self) -> str:
        return f"PID {self.pid}"


class LaunchError(AdapterError):
    def __init__(self, message: str, retryable: bool = True, review: bool = False):
        super().__init__(message, retryable)
        self.review = review


class ClientManager(Protocol):
    def active_count(self) -> int: ...
    def launch(self, username: str, place_id: str, cancel: threading.Event, timeout: float) -> ClientHandle: ...
    def wait_in_game(self, handle: ClientHandle, place_id: str, cancel: threading.Event, timeout: float) -> str: ...
    def close(self, handle: ClientHandle) -> bool: ...
    def is_alive(self, handle: ClientHandle) -> bool: ...


class SimulatedClientManager:
    """Fake clients for tests and dry runs; records the peak number open at once."""

    def __init__(self, launch_seconds: float = 0.01):
        self.launch_seconds = launch_seconds
        self._lock = threading.Lock()
        self._next_pid = 50000
        self._open: dict[tuple[int, float], ClientHandle] = {}
        self.peak = 0
        self.launches = 0
        self.closed: list[tuple[int, float]] = []
        self.foreign: set[tuple[int, float]] = set()   # never ours: must never be closed

    def active_count(self) -> int:
        with self._lock:
            return len(self._open)

    def launch(self, username, place_id, cancel, timeout) -> ClientHandle:
        if cancel.wait(self.launch_seconds):
            raise LaunchError("cancelled", retryable=False)
        with self._lock:
            self._next_pid += 1
            handle = ClientHandle(username, self._next_pid, time.time(), simulated=True)
            self._open[handle.key] = handle
            self.launches += 1
            self.peak = max(self.peak, len(self._open))
        return handle

    def wait_in_game(self, handle, place_id, cancel, timeout) -> str:
        return "simulated client in game"

    def close(self, handle) -> bool:
        with self._lock:
            if handle.key in self.foreign:
                raise AssertionError("tried to close a client this session does not own")
            self._open.pop(handle.key, None)
            self.closed.append(handle.key)
        return True

    def is_alive(self, handle) -> bool:
        with self._lock:
            return handle.key in self._open


class RobloxClientManager:
    APPEAR_TIMEOUT = 60.0
    POLL = 0.5
    # Roblox's "Failed to download or apply critical settings" box has a Retry button, and a
    # retry a couple of seconds later usually gets through (SslConnectFail on the first fetch
    # after an idle spell). Pressing it beats closing the client and queueing a new launch.
    DIALOG_RETRIES = 6
    DIALOG_RETRY_GAP = 2.5

    def __init__(self, manager):
        self._manager = manager
        self._lock = threading.Lock()
        self._owned: dict[tuple[int, float], ClientHandle] = {}

    def _tracker(self):
        from features.client_tracker import get_tracker
        return get_tracker(self._manager)

    def active_count(self) -> int:
        with self._lock:
            return len(self._owned)

    def owned_keys(self) -> set[tuple[int, float]]:
        with self._lock:
            return set(self._owned)

    def launch(self, username: str, place_id: str, cancel: threading.Event, timeout: float) -> ClientHandle:
        from classes.operation_result import ensure_result
        from features.account_actions import load_ui_settings

        tracker = self._tracker()
        owned = self.owned_keys()
        running = [c for c in tracker.clients() if c.username == username and c.key not in owned]
        if running:
            # A second client would sign the first one out; leave the user's client alone.
            raise LaunchError(
                f"{username} already has a Roblox client running outside this session (PID "
                f"{running[0].pid}); it was left alone.", retryable=False, review=True,
            )

        settings = load_ui_settings()
        before = {c.key for c in tracker.clients()}
        started = time.time()
        result = ensure_result(self._manager.launch_roblox(
            username, place_id,
            launcher_preference=settings.get("roblox_launcher", "default"),
            custom_launcher_path=settings.get("custom_roblox_launcher_path", ""),
        ))
        if not result:
            code = getattr(result, "code", "")
            retryable = bool(getattr(result, "retryable", False)) or code not in (
                "ACCOUNT_NOT_FOUND", "INVALID_COOKIE", "COOKIE_INVALID",
            )
            raise LaunchError(f"Launch failed: {result.message or code}", retryable=retryable)

        deadline = started + min(timeout, self.APPEAR_TIMEOUT)
        retried: dict[int, list[float]] = {}     # pid -> times Retry was pressed
        while time.time() < deadline:
            fresh = []
            for client in tracker.clients():
                if client.create_time < started - 5 or client.key in before:
                    continue
                if client.username != username:
                    if not client.username:
                        fresh.append(client)
                    continue
                with self._lock:
                    if client.key in self._owned:
                        continue
                    handle = ClientHandle(username, client.pid, client.create_time)
                    self._owned[handle.key] = handle
                    return handle
            for client in fresh:
                message = roblox_error_dialog(client.pid)
                if not message:
                    continue
                presses = retried.setdefault(client.pid, [])
                if presses and time.time() - presses[-1] < self.DIALOG_RETRY_GAP:
                    continue
                if len(presses) < self.DIALOG_RETRIES and press_dialog_retry(client.pid):
                    presses.append(time.time())
                    # Each press may take a while to come back; give the join the time.
                    deadline = max(deadline, time.time() + 30)
                    continue
                tracker.close(client.key)
                tries = f" (pressed Retry {len(presses)} times)" if presses else ""
                raise LaunchError(f"Roblox showed an error before joining: {message}{tries}")
            if cancel.wait(self.POLL):
                self._close_unnamed(tracker, before, started)
                raise LaunchError("cancelled", retryable=False)
        closed = self._close_unnamed(tracker, before, started)
        raise LaunchError("Roblox started but no client for this account showed up in time"
                          + (f" (closed {closed} stuck client(s) it started)" if closed else ""))

    @staticmethod
    def _close_unnamed(tracker, before: set, started: float) -> int:
        """Clients that appeared with this launch but never signed in: ours, and stuck."""
        closed = 0
        for client in tracker.clients():
            if (client.key not in before and started - 5 <= client.create_time <= started + 90
                    and not client.username and not client.user_id):
                if tracker.close(client.key):
                    closed += 1
        return closed

    def wait_in_game(self, handle: ClientHandle, place_id: str, cancel: threading.Event, timeout: float) -> str:
        tracker = self._tracker()
        deadline = time.time() + timeout
        while time.time() < deadline:
            client = tracker.get(handle.key)
            if client is None:
                if not self.is_alive(handle):
                    raise LaunchError("client exited before reaching the game")
            else:
                facts = client.facts
                if facts.auth_failed:
                    raise LaunchError("Roblox refused this account's sign-in", retryable=False)
                if facts.challenged:
                    raise LaunchError("Roblox asked for a human check", retryable=False, review=True)
                if facts.disconnected and not facts.teleporting:
                    raise LaunchError(f"disconnected: {facts.reason or 'no reason given'}")
                if facts.in_game and str(facts.place_id) == str(place_id):
                    return f"client log: joined place {facts.place_id} job {facts.job_id[:8]}"
                if facts.in_game and facts.place_id and str(facts.place_id) != str(place_id):
                    raise LaunchError(f"client joined place {facts.place_id}, not {place_id}")
            if cancel.wait(self.POLL):
                raise LaunchError("cancelled", retryable=False)
        raise LaunchError("game did not finish loading in time")

    def is_alive(self, handle: ClientHandle) -> bool:
        return process_matches(handle.pid, handle.create_time)

    def close(self, handle: ClientHandle) -> bool:
        with self._lock:
            if handle.key not in self._owned:
                return False
        ok = self._tracker().close(handle.key)
        if ok:
            with self._lock:
                self._owned.pop(handle.key, None)
        return ok


def roblox_error_dialog(pid: int) -> str:
    """Text of a Roblox message box (class #32770, e.g. "Failed to download or apply critical
    settings") owned by `pid`, or ""."""
    import win32gui
    import win32process

    found: list[str] = []

    def _child(hwnd, texts):
        if win32gui.GetClassName(hwnd) == "Static":
            text = win32gui.GetWindowText(hwnd).strip()
            if text:
                texts.append(text)
        return True

    def _top(hwnd, _):
        try:
            if (win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "#32770"
                    and win32process.GetWindowThreadProcessId(hwnd)[1] == pid):
                texts: list[str] = []
                win32gui.EnumChildWindows(hwnd, _child, texts)
                found.append(" ".join(texts) or win32gui.GetWindowText(hwnd) or "error dialog")
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_top, None)
    except Exception:
        return ""
    return found[0] if found else ""


def press_dialog_retry(pid: int) -> bool:
    """Presses Retry on a Roblox message box owned by `pid` (WM_COMMAND IDRETRY, no focus or
    mouse needed). False when there is no such box or it has no Retry button."""
    import win32con
    import win32gui
    import win32process

    IDRETRY = 4
    boxes: list[int] = []

    def _top(hwnd, _):
        try:
            if (win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "#32770"
                    and win32process.GetWindowThreadProcessId(hwnd)[1] == pid):
                boxes.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_top, None)
        for box in boxes:
            try:
                button = win32gui.GetDlgItem(box, IDRETRY)
            except Exception:
                button = 0
            if button:
                win32gui.PostMessage(box, win32con.WM_COMMAND, IDRETRY, button)
                return True
    except Exception:
        return False
    return False


def process_matches(pid: int, create_time: float) -> bool:
    """True when `pid` is still the Roblox process that started at `create_time`."""
    try:
        process = psutil.Process(int(pid))
        if abs(process.create_time() - float(create_time)) > 0.01:
            return False
        return process.name().casefold() in ROBLOX_PROCESS_NAMES
    except (psutil.Error, OSError, ValueError):
        return False


def close_orphans(store, close_fn=None) -> int:
    """Clients a previous run launched and never closed (crash, power loss). Each is closed only
    if the very same process is still running; the rest are just marked gone."""
    closed = 0
    for row in store.open_clients():
        pid, create_time = int(row["ProcessId"]), float(row["ProcessCreateTime"])
        if process_matches(pid, create_time):
            if close_fn is None:
                from features.client_tracker import get_tracker
                close_fn = get_tracker().close
            if close_fn((pid, create_time)):
                closed += 1
                store.finish_client(row["ClientInstanceId"], "orphan-closed")
                continue
        store.finish_client(row["ClientInstanceId"], "gone")
    return closed
