"""
features/auto_rejoin.py
Core logic for auto-rejoin purposes.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import time
import random
import psutil
import requests
from typing import Callable, Optional
from classes.roblox_api import RobloxAPI
from features import client_tracker, discord_alerts
import features.presence as presence_mod
import features.settings_store as settings_store
from utils.app_paths import get_data_dir
from utils.atomic_file import write_json_atomic

_CONFIG_FILE = os.path.join(get_data_dir(), "auto_rejoin.json")
_CONFIG_LOCK = threading.RLock()
_CONFIG_CACHE: dict | None = None
_INTERNET_LOCK = threading.Lock()
_INTERNET_CACHE = (0.0, False)
_PID_UID_LOCK = threading.Lock()
_PID_UID_CACHE: dict[tuple[int, float], tuple[str, str]] = {} # identity -> (user_id, log_path)
_LAUNCH_LOCK = threading.Lock()

RobloxProcessIdentity = tuple[int, float]

def load_configs() -> dict:
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is None:
            data = {}
            if os.path.exists(_CONFIG_FILE):
                try:
                    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
                except (OSError, ValueError, TypeError):
                    pass
            _CONFIG_CACHE = data
        # Deep copies: a caller editing a nested config in place must not also edit
        # the cache, or save_configs() would see no change and skip the write.
        return copy.deepcopy(_CONFIG_CACHE)


def save_configs(configs: dict) -> None:
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        if _CONFIG_CACHE == configs:
            return
        try:
            write_json_atomic(_CONFIG_FILE, configs)
            _CONFIG_CACHE = copy.deepcopy(configs)
        except Exception as e:
            print(f"[ERROR] Auto-Rejoin settings could not be saved: {e}")

def _has_internet(timeout: int = 3) -> bool:
    global _INTERNET_CACHE
    now = time.monotonic()
    with _INTERNET_LOCK:
        checked_at, cached = _INTERNET_CACHE
        if now - checked_at < 10.0:
            return cached
    for url in ("https://www.google.com/generate_204",
                "https://www.cloudflare.com/cdn-cgi/trace"):
        try:
            if requests.get(url, timeout=timeout).status_code < 500:
                with _INTERNET_LOCK:
                    _INTERNET_CACHE = (time.monotonic(), True)
                return True
        except Exception:
            pass
    with _INTERNET_LOCK:
        _INTERNET_CACHE = (time.monotonic(), False)
    return False


def _get_roblox_processes(force: bool = False) -> dict[int, tuple[float, psutil.Process]]:
    return presence_mod.get_roblox_processes(force=force)


def _get_roblox_pids(force: bool = False) -> set[int]:
    return set(_get_roblox_processes(force=force))


def _identity_matches(
    identity: RobloxProcessIdentity,
    processes: dict[int, tuple[float, psutil.Process]] | None = None,
) -> bool:
    if processes is None:
        processes = _get_roblox_processes(force=True)
    pid, create_time = identity
    current = processes.get(pid)
    return bool(current and abs(current[0] - create_time) <= 0.01)


def _pid_alive(
    pid: int,
    create_time: float | None = None,
) -> bool:
    processes = _get_roblox_processes(force=True)
    if create_time is None:
        return pid in processes
    return _identity_matches((pid, create_time), processes)


def _terminate_process(
    identity: RobloxProcessIdentity,
    account: str,
    stop_event: threading.Event | None = None,
) -> tuple[bool, str]:
    pid, create_time = identity
    last_detail = ""

    for attempt in range(1, 4):
        processes = _get_roblox_processes(force=True)
        current = processes.get(pid)
        if current is None or abs(current[0] - create_time) > 0.01:
            return True, "Process already exited."

        try:
            result = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
            )
            output = (result.stdout or result.stderr or "").strip()
            last_detail = output[-500:]
            if result.returncode != 0:
                print(
                    f"[Auto-Rejoin] [{account}] taskkill failed for PID {pid} "
                    f"(attempt {attempt}/3, exit {result.returncode})"
                )
                try:
                    process = psutil.Process(pid)
                    if abs(process.create_time() - create_time) <= 0.01:
                        process.kill()
                except (OSError, psutil.Error):
                    pass
        except Exception as exc:
            last_detail = str(exc)
            print(
                f"[Auto-Rejoin] [{account}] Failed to terminate PID {pid} "
                f"(attempt {attempt}/3): {exc}"
            )

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _identity_matches(identity, _get_roblox_processes(force=True)):
                print(f"[Auto-Rejoin] [{account}] Closed Roblox PID {pid}.")
                return True, "Process closed."
            if stop_event is not None and stop_event.wait(0.25):
                return False, "Auto-Rejoin stopped while closing the process."
            time.sleep(0.25)

    print(
        f"[Auto-Rejoin] [{account}] Could not confirm Roblox PID {pid} closed."
    )
    return False, last_detail or "The process is still running."


def scan_pid_uid_map(
    wanted_user_ids: set[str],
) -> dict[str, set[RobloxProcessIdentity]]:
    current_processes = _get_roblox_processes(force=True)
    current_identities = {
        (pid, process_data[0])
        for pid, process_data in current_processes.items()
    }
    with _PID_UID_LOCK:
        stale = set(_PID_UID_CACHE) - current_identities
        for identity in stale:
            _PID_UID_CACHE.pop(identity, None)

        # Keep logs of already-resolved processes claimed so a newer client
        # can't be matched to another account's log.
        used_logs: set[str] = {path for _, path in _PID_UID_CACHE.values()}
        for identity in sorted(current_identities, key=lambda item: item[1]):
            if identity in _PID_UID_CACHE:
                continue
            entry = presence_mod._get_log_entry_from_pid(identity[0], used_logs)
            if entry:
                _PID_UID_CACHE[identity] = (str(entry.user_id), entry.path)

        result: dict[str, set[RobloxProcessIdentity]] = {}
        for identity, (uid, _) in _PID_UID_CACHE.items():
            if uid in wanted_user_ids:
                result.setdefault(uid, set()).add(identity)

    return result


def _get_configured_user_ids(manager) -> set[str]:
    wanted = set()
    for username in load_configs().keys():
        acc = manager.accounts.get(username)
        if isinstance(acc, dict):
            uid = str(acc.get("user_id", "") or "")
            if uid and uid != "0":
                wanted.add(uid)
    return wanted


def find_running_processes_for_user(
    manager,
    user_id: str,
) -> set[RobloxProcessIdentity]:
    normalized_user_id = str(user_id)
    wanted = _get_configured_user_ids(manager) | {normalized_user_id}
    return scan_pid_uid_map(wanted).get(normalized_user_id, set())


def find_running_pid_for_user(manager, user_id: str) -> int | None:
    identities = find_running_processes_for_user(manager, user_id)
    if not identities:
        return None
    return max(identities, key=lambda item: item[1])[0]

# How often a monitored client is looked at (its log, window and memory, via the tracker).
_TICK_SECONDS = 2.0
_DISCONNECT_GRACE_SECONDS = 30.0
_TELEPORT_TIMEOUT_SECONDS = 120.0
_PRESENCE_WITH_LOG_SECONDS = 60
_TROUBLE_LABELS = {
    "disconnected": "Disconnect detected",
    "crashed": "Client crashed or was closed",
    "hung": "Client not responding",
    "memory": "Client over the memory limit",
    "left": "Left the game",
}
# Troubles a relaunch can't fix; rejoining would make them worse.
_STOPS = {
    "left": "you left the game",
    "elsewhere": "account signed in elsewhere",
    "challenge": "Roblox wants a human check, open the account in a browser",
}


def _signed_in_elsewhere(reason: str) -> bool:
    text = reason or ""
    return "273" in text or "Same account" in text or "DuplicateLogin" in text


# Crash breaker: at most N relaunches of one account per window, with growing waits.
_BREAKER_LOCK = threading.Lock()
_RELAUNCHES: dict[str, list[float]] = {}


def _breaker_recent(account: str, window_seconds: float) -> list[float]:
    now = time.time()
    with _BREAKER_LOCK:
        recent = [at for at in _RELAUNCHES.get(account, []) if now - at < window_seconds]
        _RELAUNCHES[account] = recent
        return recent


def _breaker_count(account: str) -> int:
    with _BREAKER_LOCK:
        return len(_RELAUNCHES.get(account, []))


def _breaker_backoff(account: str, settings: dict) -> float | None:
    """Seconds to wait before the next relaunch, or None when the breaker has tripped."""
    limit = int(settings.get("recovery_breaker_max", 3) or 0)
    if limit <= 0:
        return 0.0
    window = max(1, int(settings.get("recovery_breaker_window_min", 10) or 10)) * 60
    recent = _breaker_recent(account, window)
    if len(recent) >= limit:
        return None
    return 0.0 if not recent else float(min(300, 10 * 2 ** (len(recent) - 1)))


def _breaker_note(account: str) -> None:
    with _BREAKER_LOCK:
        _RELAUNCHES.setdefault(account, []).append(time.time())


def reset_breaker(account: str) -> None:
    with _BREAKER_LOCK:
        _RELAUNCHES.pop(account, None)


_presence_lock = threading.Lock()
_presence_next_time = 0.0

def _wait_presence_slot(stop_event: threading.Event, gap: float = 0.4) -> bool:
    global _presence_next_time
    while True:
        now = time.time()
        with _presence_lock:
            wait = _presence_next_time - now
            if wait <= 0:
                _presence_next_time = now + gap
                return True
        sleep = min(0.25, max(0.05, wait))
        if stop_event.wait(sleep):
            return False

class AutoRejoinWorker:
    def __init__(
        self,
        account:   str,
        config:    dict,
        manager,
        on_status: Callable[[str, str], None],  # (account, status_string)
    ):
        self.account = account
        self.config = config
        self.manager = manager
        self.on_status = on_status

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._process_identity: RobloxProcessIdentity | None = None
        self._owned_processes: set[RobloxProcessIdentity] = set()
        self._next_presence = 0.0
        self._presence_fails = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"AutoRejoin-{self.account}",
        )
        self._thread.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _emit(self, status: str) -> None:
        try:
            self.on_status(self.account, status)
        except Exception:
            pass

    def _wait(self, seconds: float) -> bool:
        base = max(3, int(seconds))
        jitter = random.uniform(0.2, min(1.5, base * 0.2))
        return self._stop.wait(base + jitter)

    def _current_cookie(self, fallback: str) -> str:
        # Re-read so a cookie re-imported while monitoring is picked up.
        data = self.manager.accounts.get(self.account)
        return (data.get("cookie") if isinstance(data, dict) else "") or fallback

    def _can_launch(self) -> bool:
        if not self.config.get("check_internet", True):
            return True
        return _has_internet()

    def _launch_and_track(
        self,
        user_id: str,
        place_id: str,
        private_server: str,
        job_id: str,
    ) -> bool:
        with _LAUNCH_LOCK:
            processes_before = _get_roblox_processes(force=True)
            identities_before = {
                (pid, process_data[0])
                for pid, process_data in processes_before.items()
            }

            settings = settings_store.load()
            ok = self.manager.launch_roblox(
                self.account, place_id, private_server,
                launcher_preference=settings.get("roblox_launcher", "default"),
                job_id=job_id,
                custom_launcher_path=settings.get("custom_roblox_launcher_path", ""),
            )
            if not ok:
                return False

            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if self._stop.wait(0.5):
                    return False

                processes_after = _get_roblox_processes(force=True)
                candidates: set[RobloxProcessIdentity] = set()
                for pid, process_data in processes_after.items():
                    identity = (pid, process_data[0])
                    if identity in identities_before:
                        continue
                    resolved_user_id = presence_mod._get_user_id_from_pid(
                        pid,
                        set(),
                    )
                    if str(resolved_user_id or "") == str(user_id):
                        candidates.add(identity)

                if candidates:
                    self._owned_processes = candidates
                    self._process_identity = max(
                        candidates,
                        key=lambda item: item[1],
                    )
                    print(
                        f"[Auto-Rejoin] [{self.account}] Tracked PID "
                        f"{self._process_identity[0]} for user {user_id}."
                    )
                    if len(candidates) > 1:
                        print(
                            f"[Auto-Rejoin] [{self.account}] Detected "
                            f"{len(candidates)} matching Roblox processes."
                        )
                    return True

            processes_after = _get_roblox_processes(force=True)
            identities_after = {
                (pid, process_data[0])
                for pid, process_data in processes_after.items()
            }
            matching_identities: set[RobloxProcessIdentity] = set()
            for identity in identities_after - identities_before:
                resolved_user_id = presence_mod._get_user_id_from_pid(
                    identity[0],
                    set(),
                )
                if str(resolved_user_id or "") == str(user_id):
                    matching_identities.add(identity)

            for identity in matching_identities:
                closed, detail = _terminate_process(
                    identity,
                    self.account,
                    self._stop,
                )
                if not closed:
                    print(
                        f"[Auto-Rejoin] [{self.account}] Could not clean "
                        f"unresolved PID {identity[0]}: {detail}"
                    )

            print(
                f"[Auto-Rejoin] [{self.account}] Could not resolve the "
                "new Roblox process to the account."
            )
            return False

    def _is_in_game(
        self,
        user_id,
        cookie: str,
        place_id: str,
        check_place_id: bool = True,
    ) -> tuple[str, str]:
        if not self._process_identity or not _identity_matches(self._process_identity):
            return "disconnected", ""
        if not _wait_presence_slot(self._stop):
            return "unavailable", ""
        try:
            presence = RobloxAPI.get_player_presence(user_id, cookie)
            if not presence:
                return "unavailable", ""
            in_game = presence.get("in_game", False)
            cur_pid = presence.get("place_id")
            game_id = presence.get("game_id", "")
            status = presence.get("status")
            online = bool(status) if status is not None else bool(in_game)
            if not online:
                return "disconnected", game_id
            if not check_place_id:
                return "in_game", game_id
            if in_game:
                try:
                    if int(cur_pid) == int(place_id):
                        return "in_game", game_id
                except (TypeError, ValueError):
                    pass
            return "disconnected", game_id
        except Exception as e:
            print(f"[Auto-Rejoin] [{self.account}] Presence error: {e}")
            return "unavailable", ""

    def _check_client(
        self,
        tracker,
        user_id,
        cookie: str,
        place_id: str,
        check_presence: bool,
        check_place_id: bool,
        check_interval: int,
    ) -> tuple[str | None, str]:
        """What, if anything, is wrong with the account's client: None, or one of
        _TROUBLE_LABELS / _STOPS, plus the server to go back to ('' for a fresh one)."""
        identity = self._process_identity
        client = tracker.get(identity) if identity else None
        facts = client.facts if client else None

        if not identity or not _identity_matches(identity):
            # Gone. Its log says whether the player left or Roblox dropped it.
            if facts and facts.disconnected and facts.left and not facts.teleporting:
                return "left", ""
            if facts and facts.disconnected:
                return "disconnected", self._log_server(facts, place_id)
            return "crashed", self._log_server(facts, place_id)

        if client is not None and client.tail is not None:
            tail = client.tail
            if facts.challenged:
                return "challenge", ""
            # A teleport can disconnect and join again within seconds, logged or not:
            # a disconnect only counts once no new join followed for a while.
            if (
                facts.disconnected
                and not facts.teleporting
                and tail.disconnected_for() >= _DISCONNECT_GRACE_SECONDS
            ):
                if facts.left and _signed_in_elsewhere(facts.reason):
                    return "elsewhere", ""
                return "disconnected", self._log_server(facts, place_id)
            if facts.teleporting and tail.teleporting_for() >= _TELEPORT_TIMEOUT_SECONDS:
                return "disconnected", ""

        if client is not None:
            if client.hung and settings_store.get("recovery_restart_on_hang", False):
                return "hung", ""
            if tracker.memory_high(client):
                return "memory", self._log_server(facts, place_id)

        # The presence API is the fallback: always when the client's log isn't found, and for
        # the place check at most once a minute (Roblox limits cookie APIs per account).
        if check_presence and time.monotonic() >= self._next_presence:
            has_log = bool(client and client.tail)
            if not has_log or check_place_id:
                self._next_presence = time.monotonic() + (
                    max(_PRESENCE_WITH_LOG_SECONDS, check_interval) if has_log else check_interval
                )
                state, game_id = self._is_in_game(
                    user_id,
                    self._current_cookie(cookie),
                    place_id,
                    check_place_id,
                )
                if state == "in_game":
                    self._presence_fails = 0
                elif state == "disconnected":
                    self._presence_fails += 1
                    if self._presence_fails >= 2:
                        self._presence_fails = 0
                        return "disconnected", game_id or ""
        return None, ""

    @staticmethod
    def _log_server(facts, place_id: str) -> str:
        # Back into the same server only when it was the configured place, not a sub-place
        # a teleport went to.
        if facts and facts.job_id and str(facts.place_id) == str(place_id):
            return facts.job_id
        return ""

    def _run(self) -> None:
        cfg = self.config
        place_id = str(cfg.get("place_id", ""))
        private_server = cfg.get("private_server", "")
        job_id = cfg.get("job_id", "")
        check_interval = int(cfg.get("check_interval", 10))
        max_retries = int(cfg.get("max_retries", 5))
        check_presence = bool(cfg.get("check_presence", True))
        check_place_id = bool(cfg.get("check_place_id", True))

        if not place_id:
            self._emit("ERROR: no place_id")
            return
        if self.account not in self.manager.accounts:
            self._emit("ERROR: account not found")
            return

        acc_data = self.manager.accounts[self.account]
        cookie = acc_data.get("cookie", "")
        user_id = acc_data.get("user_id")
        if not user_id:
            user_id = RobloxAPI.get_user_id_from_username(self.account)
            if user_id:
                acc_data["user_id"] = user_id
                try:
                    self.manager.save_accounts()
                except Exception:
                    pass

        if not user_id:
            self._emit("ERROR: cannot resolve user ID")
            return

        stagger = random.uniform(4.0, 8.0)
        if self._stop.wait(stagger):
            return

        retry_count = 0

        if not self._process_identity or not _identity_matches(self._process_identity):
            while not self._stop.is_set():
                existing_processes = find_running_processes_for_user(
                    self.manager,
                    str(user_id),
                )
                if existing_processes:
                    self._owned_processes = existing_processes
                    self._process_identity = max(
                        existing_processes,
                        key=lambda item: item[1],
                    )
                    print(
                        f"[Auto-Rejoin] [{self.account}] Adopted existing PID "
                        f"{self._process_identity[0]} for user {user_id}"
                    )
                    self._emit(f"ACTIVE - Place {place_id} (existing client)")
                    break

                self._emit(f"Launching... (Place {place_id})")
                while not self._stop.is_set() and not self._can_launch():
                    if self._wait(check_interval):
                        return
                if self._stop.is_set():
                    return

                ok = self._launch_and_track(
                    str(user_id),
                    place_id,
                    private_server,
                    job_id,
                )
                if ok:
                    self._emit(f"ACTIVE - Place {place_id}")
                    break

                retry_count += 1
                self._emit(f"Launch failed ({retry_count}/{max_retries})")
                if retry_count >= max_retries:
                    self._emit("STOPPED: max retries")
                    return
                if self._wait(check_interval):
                    return

        self._emit(f"ACTIVE - Place {place_id}")
        tracker = client_tracker.get_tracker(self.manager)
        self._next_presence = time.monotonic() + check_interval

        while not self._stop.is_set():
            try:
                trouble, game_id = self._check_client(
                    tracker,
                    user_id,
                    cookie,
                    place_id,
                    check_presence,
                    check_place_id,
                    check_interval,
                )
                if trouble is None:
                    retry_count = 0
                    if self._stop.wait(_TICK_SECONDS):
                        break
                    continue

                settings = settings_store.load()
                if trouble == "left" and settings.get("recovery_rejoin_after_leave", False):
                    trouble = "disconnected"
                if trouble in _STOPS:
                    message = _STOPS[trouble]
                    print(f"[Auto-Rejoin] [{self.account}] Stopped: {message}")
                    self._emit(f"STOPPED: {message}")
                    break

                backoff = _breaker_backoff(self.account, settings)
                if backoff is None:
                    message = (
                        f"crash breaker, {_breaker_count(self.account)} relaunches in "
                        f"{int(settings.get('recovery_breaker_window_min', 10))} min"
                    )
                    print(f"[Auto-Rejoin] [{self.account}] Stopped: {message}")
                    self._emit(f"STOPPED: {message}")
                    discord_alerts.notify("breaker", self.account, f"Stopped after a {message}.")
                    break

                retry_count += 1
                print(
                    f"[Auto-Rejoin] [{self.account}] {_TROUBLE_LABELS[trouble]}, "
                    f"attempt {retry_count}/{max_retries}"
                )
                self._emit(f"Rejoining... ({retry_count}/{max_retries})")

                identities_to_close = set(self._owned_processes)
                if self._process_identity:
                    identities_to_close.add(self._process_identity)
                identities_to_close.update(
                    find_running_processes_for_user(
                        self.manager,
                        str(user_id),
                    )
                )

                cleanup_ok = True
                for identity in sorted(
                    identities_to_close,
                    key=lambda item: item[1],
                ):
                    tracker.mark_closing(identity)
                    closed, detail = _terminate_process(
                        identity,
                        self.account,
                        self._stop,
                    )
                    if not closed:
                        cleanup_ok = False
                        print(
                            f"[Auto-Rejoin] [{self.account}] Cleanup "
                            f"failed for PID {identity[0]}: {detail}"
                        )

                if not cleanup_ok:
                    self._emit("Rejoin delayed: closing old client")
                    if self._wait(check_interval):
                        break
                    continue

                self._process_identity = None
                self._owned_processes.clear()

                if backoff > 0:
                    self._emit(f"Rejoining in {int(backoff)}s (relaunched recently)")
                    if self._stop.wait(backoff):
                        break

                while not self._stop.is_set() and not self._can_launch():
                    if self._wait(check_interval):
                        break

                if self._stop.is_set():
                    break

                rejoin_jid = job_id if job_id else game_id
                existing_processes = find_running_processes_for_user(
                    self.manager,
                    str(user_id),
                )
                if existing_processes:
                    self._owned_processes = existing_processes
                    self._process_identity = max(
                        existing_processes,
                        key=lambda item: item[1],
                    )
                    ok = True
                    print(
                        f"[Auto-Rejoin] [{self.account}] Adopted existing "
                        f"PID {self._process_identity[0]} instead of relaunching"
                    )
                else:
                    _breaker_note(self.account)
                    ok = self._launch_and_track(
                        str(user_id),
                        place_id,
                        private_server,
                        rejoin_jid,
                    )

                if ok:
                    print(f"[Auto-Rejoin] [{self.account}] Rejoin successful")
                    discord_alerts.notify(
                        "restarted", self.account,
                        f"{_TROUBLE_LABELS[trouble]}; launched again into place {place_id}.",
                    )
                    retry_count = 0
                    self._presence_fails = 0
                    self._emit(f"ACTIVE - Place {place_id}")
                    if self._stop.wait(10):
                        break
                else:
                    self._emit(f"Launch failed ({retry_count}/{max_retries})")
                    if retry_count >= max_retries:
                        self._emit("STOPPED: max retries reached")
                        print(f"[Auto-Rejoin] [{self.account}] Max retries reached, stopping.")
                        break
                    if self._wait(check_interval):
                        break

            except Exception as e:
                print(f"[Auto-Rejoin] [{self.account}] Unhandled error: {e}")
                self._emit(f"ERROR: {e}")
                if self._wait(check_interval):
                    break

        self._emit("INACTIVE")
        print(f"[Auto-Rejoin] [{self.account}] Worker stopped.")
