"""
features/client_history.py
What clients did, kept on this PC: the servers each account reached (to rejoin one) and how
clients that ended badly ended (crash history). No cookies, tickets or link codes go in.
"""

from __future__ import annotations

import json
import os
import threading
import time

from utils.app_paths import get_data_dir
from utils.atomic_file import write_json_atomic

_LOCK = threading.RLock()
_SERVERS_FILE = os.path.join(get_data_dir(), "recent_servers.json")
_CRASHES_FILE = os.path.join(get_data_dir(), "crash_history.json")
_SERVERS_PER_ACCOUNT = 10
_MAX_CRASHES = 200
_cache: dict[str, object] = {}


def _load(path: str, default):
    with _LOCK:
        if path in _cache:
            return _cache[path]
        data = default
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, type(default)):
                data = loaded
        except (OSError, ValueError):
            pass
        _cache[path] = data
        return data


def _save(path: str, data) -> None:
    with _LOCK:
        _cache[path] = data
        try:
            write_json_atomic(path, data)
        except Exception as exc:
            print(f"[WARNING] Could not save {os.path.basename(path)}: {exc}")


def add_recent_server(username: str, place_id: str, job_id: str, game_name: str = "") -> None:
    if not username or not place_id or not job_id:
        return
    with _LOCK:
        data = _load(_SERVERS_FILE, {})
        rows = [row for row in data.get(username, []) if row.get("job_id") != job_id]
        rows.insert(0, {
            "place_id": str(place_id),
            "job_id": str(job_id),
            "name": game_name,
            "at": time.time(),
        })
        data[username] = rows[:_SERVERS_PER_ACCOUNT]
        _save(_SERVERS_FILE, data)


def recent_servers(username: str) -> list[dict]:
    with _LOCK:
        return [dict(row) for row in _load(_SERVERS_FILE, {}).get(username, [])]


def add_crash(username: str, kind: str, reason: str = "", place_id: str = "",
              uptime_seconds: float = 0.0) -> None:
    with _LOCK:
        rows = list(_load(_CRASHES_FILE, []))
        rows.insert(0, {
            "account": username or "",
            "kind": kind,
            "reason": reason[:160],
            "place_id": str(place_id or ""),
            "uptime": int(uptime_seconds),
            "at": time.time(),
        })
        _save(_CRASHES_FILE, rows[:_MAX_CRASHES])


def crashes() -> list[dict]:
    with _LOCK:
        return [dict(row) for row in _load(_CRASHES_FILE, [])]


def clear_crashes() -> None:
    _save(_CRASHES_FILE, [])
