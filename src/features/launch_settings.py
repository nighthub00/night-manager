"""
features/launch_settings.py
An FPS cap and the lowest graphics for the clients NIGHT MANAGER starts, without changing the
user's own Roblox settings (after RoALT's launcher/client_settings.rs, MIT).

Every client shares %LOCALAPPDATA%\\Roblox\\GlobalBasicSettings_13.xml and reads it once, a few
seconds after it starts. A launch writes its values (FramerateCap, GraphicsQualityLevel,
SavedQualityLevel), waits until its client's log shows it has read them, and puts the user's
own back; launches hold a lock meanwhile, so one never reads another's. A client writes all
its settings back when it closes: `settle` (on every client that ends) puts the user's values
back over any value a launch wrote. Accounts marked Full quality are left alone.
"""

from __future__ import annotations

import threading
import time

import features.settings_store as settings_store
from features import roblox_logs, roblox_settings

KEYS = ("FramerateCap", "GraphicsQualityLevel", "SavedQualityLevel")
# Log lines a client writes only after it has read its settings.
_READ_MARKS = ("SceneManager: resizing main targets", "[FLog::Network] Replicator created")
_LOG_TIMEOUT = 60.0
_MARK_TIMEOUT = 20.0
_AFTER_READ = 1.0

_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
# The user's own values, and the values launches wrote, so a closing client's write-back can be
# told from a change the user made. Kept across restarts.
_KEPT = "launch_settings_state"
_kept = settings_store.get(_KEPT, {}) or {}
_users: dict[str, str] = dict(_kept.get("users") or {})
_ours: dict[str, str] = dict(_kept.get("ours") or {})


def _keep() -> None:
    settings_store.save(_KEPT, {"users": dict(_users), "ours": dict(_ours)})


def values_for(username: str) -> dict[str, str]:
    """The settings a launch of this account should start with ({} = leave Roblox's alone)."""
    settings = settings_store.load()
    if username in (settings.get("full_quality_accounts") or []):
        return {}
    values: dict[str, str] = {}
    fps = int((settings.get("launch_fps_by_account") or {}).get(username) or 0)
    fps = fps or int(settings.get("launch_fps_cap", 0) or 0)
    if fps:
        values["FramerateCap"] = str(max(5, min(360, fps)))
    if settings.get("launch_lowest_graphics", False):
        values["GraphicsQualityLevel"] = "1"
        values["SavedQualityLevel"] = "1"
    return values


def _current() -> dict[str, str] | None:
    loaded = roblox_settings.load_settings()
    if not loaded:
        return None
    return {
        record["key"]: str(record["value"])
        for record in loaded.data.get("settings", [])
        if record.get("key") in KEYS
    }


def _write(values: dict[str, str]) -> bool:
    if not values:
        return True
    result = roblox_settings.apply_settings(values, framerate_locked=None)
    if not result:
        print(f"[WARNING] Launch settings: couldn't write Roblox's settings ({result.code})")
    return bool(result)


class LaunchSettings:
    """One launch's hold on the settings file; `release` puts the user's values back."""

    def __init__(self, restore: dict[str, str]):
        self.restore = restore
        self.released = False

    def release_after_read(self, launch_data, started: float) -> None:
        tracker_id = str((launch_data or {}).get("browser_tracker_id") or "") if isinstance(launch_data, dict) else ""
        threading.Thread(
            target=self._wait_then_release, args=(tracker_id, started),
            daemon=True, name="launch-settings-restore",
        ).start()

    def _wait_then_release(self, tracker_id: str, started: float) -> None:
        try:
            wait_until_read(tracker_id, started)
        finally:
            self.release()

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            _write(self.restore)
        finally:
            _LOCK.release()


def begin(username: str) -> LaunchSettings | None:
    """Writes this account's launch values; None when there is nothing to change (or the
    file can't be written, in which case the launch goes ahead with Roblox's own settings)."""
    wanted = values_for(username)
    if not wanted:
        return None
    _LOCK.acquire()
    try:
        current = _current()
        if current is None:
            _LOCK.release()
            return None
        wanted = {key: value for key, value in wanted.items() if key in current}
        restore = {key: current[key] for key in wanted if current[key] != wanted[key]}
        with _STATE_LOCK:
            for key in wanted:
                # What's there now is the user's, unless it is a value a launch left.
                if current[key] != _ours.get(key):
                    _users[key] = current[key]
                _ours[key] = wanted[key]
            restore = {key: _users.get(key, value) for key, value in restore.items()}
            _keep()
        if not restore or not _write(wanted):
            _LOCK.release()
            return None
        return LaunchSettings(restore)
    except Exception:
        _LOCK.release()
        raise


def wait_until_read(browser_tracker_id: str, started: float) -> None:
    """Until the launched client's log shows it read its settings (or a timeout)."""
    deadline = time.monotonic() + _LOG_TIMEOUT
    path = ""
    while time.monotonic() < deadline:
        if not path and browser_tracker_id:
            path = roblox_logs.find_log_by_tracker(browser_tracker_id, started)
            if path:
                deadline = min(deadline, time.monotonic() + _MARK_TIMEOUT)
        if path:
            try:
                with open(path, "rb") as handle:
                    text = handle.read().decode("utf-8", "replace")
                if any(mark in text for mark in _READ_MARKS):
                    time.sleep(_AFTER_READ)
                    return
            except OSError:
                pass
        elif not browser_tracker_id:
            time.sleep(_MARK_TIMEOUT)
            return
        time.sleep(0.5)


def settle() -> None:
    """A client closed and wrote all its settings back: put the user's own over ours."""
    if not _LOCK.acquire(blocking=False):
        return  # A launch holds the file; it puts the user's values back itself.
    try:
        current = _current()
        if current is None:
            return
        with _STATE_LOCK:
            back = {
                key: _users[key]
                for key in _users
                if key in current and current[key] == _ours.get(key) and current[key] != _users[key]
            }
        if back:
            _write(back)
            print("[INFO] Put your own Roblox graphics settings back after a client closed")
    finally:
        _LOCK.release()
