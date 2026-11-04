"""
features/roblox_logs.py
Following Roblox clients through their own log files
(%LOCALAPPDATA%\\Roblox\\logs\\*_Player_*_last.log), never through the client process.

- Which log belongs to which client: Windows says which processes have a file open
  (NtQueryInformationFile, FileProcessIdsUsingFile). That needs a handle on the log file only,
  none on Roblox, so nothing reads the client's memory or copies its handles.
- What happened in it: `parse_line` turns the client's own lines into `LogFacts` (joined a
  server, teleporting, disconnected and why, left on purpose). Line formats follow RoALT's
  research (MIT, github.com/inspectsoftware/RoALT, instances/logwatch.rs).

Logs can hold the launch ticket (`gameinfo:`): lines are parsed and dropped, and the few
kept for crash reports go through `safe_line` first.
"""

from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field

_JOINING = "! Joining game '"
_TELEPORT = "UgcExperienceController: doTeleport"
_DISCONNECTS = (
    "Disconnection Notification",
    "Lost connection with reason",
    "Time to disconnect replication data",
    "Sending disconnect with reason",
    "Disconnected from server for reason",
    "Connection closed by server",
)
_AUTH_FAILED = ("encountered during authentication", "Error Code: 403")
# A line holding any of these is never shown, logged or sent anywhere.
SENSITIVE = ("gameinfo", "ticket", "placelauncher", "roblosecurity", "cookie", "accesscode", "linkcode")
_GUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_REASON_END = re.compile(r"[^A-Za-z0-9 .,:;()_\-]")
_HEAD_BYTES = 256 * 1024

# Reason codes from the player's side: 285 is Leave, 273 the same account starting elsewhere
# (rejoining would kick that session).
_CLIENT_INITIATED_TEXT = ("DisconnectClientInitiated", "Same account launched", "DuplicateLogin")
_CLIENT_INITIATED_CODES = {"285", "273"}


def logs_dir() -> str:
    return os.path.join(os.getenv("LOCALAPPDATA", ""), "Roblox", "logs")


def is_player_log(name: str) -> bool:
    return "_Player_" in name and "_CrashHandler_" not in name and name.lower().endswith(".log")


def player_logs(since: float | None = None) -> list[tuple[str, float]]:
    """Player logs (path, last written) written since `since` (epoch seconds), newest first."""
    directory = logs_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found: list[tuple[str, float]] = []
    for name in names:
        if not is_player_log(name):
            continue
        path = os.path.join(directory, name)
        try:
            modified = os.path.getmtime(path)
        except OSError:
            continue
        if since is None or modified >= since:
            found.append((path, modified))
    found.sort(key=lambda item: item[1], reverse=True)
    return found


# Which processes have a file open, asked of the file

_FILE_READ_ATTRIBUTES = 0x80
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_FILE_PROCESS_IDS_USING_FILE = 47
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQueryInformationFile.restype = ctypes.c_ulong
_ntdll.NtQueryInformationFile.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p, wintypes.ULONG, ctypes.c_int,
]


def pids_using_file(path: str) -> set[int]:
    """PIDs of the processes holding `path` open (this process left out). Empty when unknown."""
    handle = _kernel32.CreateFileW(
        path, _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL, None, _OPEN_EXISTING, 0, None,
    )
    if not handle or handle == _INVALID_HANDLE:
        return set()
    try:
        size = 4096
        for _ in range(6):
            buffer = ctypes.create_string_buffer(size)
            status = _ntdll.NtQueryInformationFile(
                handle, ctypes.byref(_IoStatusBlock()), buffer, size, _FILE_PROCESS_IDS_USING_FILE,
            )
            if status == _STATUS_INFO_LENGTH_MISMATCH:
                size *= 4
                continue
            if status != 0:
                return set()
            count = ctypes.c_ulong.from_buffer(buffer, 0).value
            pointer_size = ctypes.sizeof(ctypes.c_size_t)
            ids = (ctypes.c_size_t * count).from_buffer(buffer, pointer_size)
            own = os.getpid()
            return {int(pid) for pid in ids if pid and pid != own}
        return set()
    finally:
        _kernel32.CloseHandle(handle)


def logs_by_pid(since: float | None = None) -> dict[int, str]:
    """Each running client's own Player log, from the log side (newest log wins per PID)."""
    owners: dict[int, str] = {}
    for path, _ in player_logs(since):
        for pid in pids_using_file(path):
            owners.setdefault(pid, path)
    return owners


# What a log says

@dataclass
class LogFacts:
    browser_tracker_id: str = ""
    job_id: str = ""
    place_id: str = ""
    user_id: str = ""
    in_game: bool = False
    teleporting: bool = False
    disconnected: bool = False
    # The disconnect was the player's own (Leave, or the account signed in elsewhere).
    left: bool = False
    reason: str = ""
    # Roblox asked for a human check (challengedByGcs).
    challenged: bool = False
    # Roblox refused the client's sign-in before it reached a game.
    auth_failed: bool = False
    # Closed because another client took over the singleton.
    replaced: bool = False
    joins: int = 0
    _signalled: bool = field(default=False, repr=False)


def _first_tag(line: str) -> str | None:
    start = line.find("[")
    if start < 0:
        return None
    end = line.find("]", start)
    return line[start + 1:end] if end > start else None


def _number_after(line: str, marker: str) -> str:
    index = line.find(marker)
    if index < 0:
        return ""
    rest = line[index + len(marker):]
    digits = re.match(r"\d+", rest)
    return digits.group(0) if digits else ""


def _tracker_id(line: str) -> str:
    return _number_after(line, "websiteBTId is ") or _number_after(line, "BTID is overriden to ")


def _client_initiated(line: str) -> bool:
    # Only the message counts, not the timestamp/thread prefix.
    index = line.find(" [")
    message = line[index:] if index >= 0 else line
    if any(marker in message for marker in _CLIENT_INITIATED_TEXT):
        return True
    return any(number in _CLIENT_INITIATED_CODES for number in re.split(r"\D+", message))


def _reason_of(line: str, marker: str) -> str:
    rest = line[line.find(marker):]
    end = _REASON_END.search(rest)
    return rest[:end.start() if end else len(rest)].rstrip()[:160]


def parse_line(line: str, facts: LogFacts) -> None:
    # A game's scripts write print/warn/error lines: nothing in them is an event. The real
    # join line is an Output line too, so it only counts while no game is running.
    tag = _first_tag(line)
    joining = line.find(_JOINING) if tag in (None, "FLog::Output") else -1
    if not facts.in_game and any(marker in line for marker in _AUTH_FAILED):
        facts.auth_failed = True
        return
    if tag in ("FLog::Output", "FLog::Warning", "FLog::Error") and joining < 0:
        return
    if joining >= 0 and facts.in_game and not facts.teleporting and not facts.disconnected:
        return
    tracker = _tracker_id(line)
    if tracker:
        facts.browser_tracker_id = tracker
    elif joining >= 0:
        job, _, rest = line[joining + len(_JOINING):].partition("'")
        if _GUID.match(job):
            facts.job_id = job
            facts.place_id = _number_after(rest, " place ")
            facts.in_game = False
            facts.teleporting = False
            facts.disconnected = False
            facts.left = False
            facts.auth_failed = False
            facts.reason = ""
            facts.joins += 1
    elif "[FLog::Network] Replicator created" in line or "[FLog::Network] serverId:" in line:
        facts.in_game = True
    elif facts.in_game and _TELEPORT in line:
        facts.teleporting = True
    elif any(marker in line for marker in _DISCONNECTS):
        marker = next(marker for marker in _DISCONNECTS if marker in line)
        facts.disconnected = True
        facts.left = facts.left or _client_initiated(line)
        # The first line that names a reason; "Time to disconnect ..." names none.
        if "reason" not in facts.reason.lower():
            facts.reason = _reason_of(line, marker) or facts.reason
    elif "challengedByGcs" in line:
        facts.challenged = True
    elif "userid:" in line and "game_join_loadtime" in line:
        user_id = _number_after(line, "userid:")
        if user_id:
            facts.user_id = user_id
    elif "EventWaitResultUpdate" in line:
        facts._signalled = True
    elif "Received close main window request" in line and facts._signalled:
        facts.replaced = True


def safe_line(line: str, limit: int = 300) -> str:
    """A log line fit for a crash report: none that could carry a ticket, cookie or link."""
    lower = line.lower()
    if any(word in lower for word in SENSITIVE) or "http" in lower:
        return ""
    return line.strip()[:limit]


class LogTail:
    """Reads a client's log as it grows and keeps what it says in `facts`."""

    def __init__(self, path: str):
        self.path = path
        self.offset = 0
        self.facts = LogFacts()
        self.disconnected_since: float | None = None
        self.teleporting_since: float | None = None
        self.last_growth = time.monotonic()

    def read_new(self) -> LogFacts:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size < self.offset:
                    # A new file under the same name: start over.
                    self.offset = 0
                    self.facts = LogFacts()
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return self.facts
        end = data.rfind(b"\n")
        if end >= 0:
            # A partial last line waits for the next read.
            for raw in data[:end].split(b"\n"):
                parse_line(raw.decode("utf-8", "replace"), self.facts)
            self.offset += end + 1
            self.last_growth = time.monotonic()
        now = time.monotonic()
        if self.facts.disconnected and not self.facts.teleporting:
            if self.disconnected_since is None:
                self.disconnected_since = now
        else:
            self.disconnected_since = None
        if self.facts.teleporting:
            if self.teleporting_since is None:
                self.teleporting_since = now
        else:
            self.teleporting_since = None
        return self.facts

    def disconnected_for(self) -> float:
        return 0.0 if self.disconnected_since is None else time.monotonic() - self.disconnected_since

    def teleporting_for(self) -> float:
        return 0.0 if self.teleporting_since is None else time.monotonic() - self.teleporting_since

    def tail(self, count: int = 25) -> list[str]:
        """The client's last lines, cleaned for a crash report."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 64 * 1024))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        cleaned = [safe_line(line) for line in lines[-count * 3:]]
        return [line for line in cleaned if line][-count:]


def tracker_of(path: str) -> str:
    """The browser tracker id a log names in its first lines ('' when none yet)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""
    for line in head.splitlines():
        tracker = _tracker_id(line)
        if tracker:
            return tracker
    return ""


def find_log_by_tracker(browser_tracker_id: str, since: float) -> str:
    """The Player log a launch with this tracker id wrote, '' until it exists."""
    for path, _ in player_logs(since - 5.0):
        if tracker_of(path) == str(browser_tracker_id):
            return path
    return ""


# Housekeeping

_CLEAN_AGE_SECONDS = 24 * 60 * 60
_CLEAN_BYTES = 50 * 1024 * 1024


def clean_old_logs(keep: set[str] | None = None) -> tuple[int, int]:
    """Deletes Roblox logs written over a day ago or over 50 MB, never a live client's.
    Returns (files, bytes) removed."""
    keep_norm = {os.path.normcase(os.path.abspath(path)) for path in (keep or set())}
    directory = logs_dir()
    removed = 0
    freed = 0
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return 0, 0
    for name in names:
        if not name.lower().endswith(".log"):
            continue
        path = os.path.join(directory, name)
        if os.path.normcase(os.path.abspath(path)) in keep_norm:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if now - stat.st_mtime < _CLEAN_AGE_SECONDS and stat.st_size <= _CLEAN_BYTES:
            continue
        # A log some process still has open belongs to a running client.
        if pids_using_file(path):
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        removed += 1
        freed += stat.st_size
    return removed, freed
