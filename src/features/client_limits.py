"""
features/client_limits.py
Per-client limits, set through Windows rather than inside the client (after RoALT's
instances/limits.rs, MIT): a job object with a hard CPU cap, efficiency mode (EcoQoS with
below-normal priority and very low memory priority), CPU affinity and working-set trims.

Each change opens a short-lived handle with only the right it needs, checked against the
client's start time; nothing reads or writes the client's memory. Only the job handle is kept,
so its cap can change later. A process can't leave a job, so a cap is lifted by turning the
job's cap off, and every limit is taken back when NIGHT MANAGER exits (`release_all`).

The background planner (`plan_tick`, a client-tracker hook) limits clients you aren't playing:
never the one in front, one still loading or not responding, or an account marked as main.
A limit you set on one client by hand wins over the planner for that limit.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

import psutil

import features.settings_store as settings_store
from features import client_tracker

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)

_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
_PROCESS_POWER_THROTTLING = 4
_PROCESS_MEMORY_PRIORITY = 0
_POWER_THROTTLING_EXECUTION_SPEED = 0x1
_MEMORY_PRIORITY_VERY_LOW = 1
_MEMORY_PRIORITY_NORMAL = 5
_NORMAL_PRIORITY_CLASS = 0x20
_BELOW_NORMAL_PRIORITY_CLASS = 0x4000

_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
_kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]


class _CpuRateControl(ctypes.Structure):
    _fields_ = [("control_flags", wintypes.DWORD), ("cpu_rate", wintypes.DWORD)]


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("version", wintypes.ULONG), ("control_mask", wintypes.ULONG), ("state_mask", wintypes.ULONG)]


class _MemoryPriority(ctypes.Structure):
    _fields_ = [("memory_priority", wintypes.ULONG)]


def _creation_time(handle) -> float | None:
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
        return None
    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return ticks / 10_000_000 - 11_644_473_600


class _Handle:
    """A short-lived process handle with only `access`, refused if the PID was reused."""

    def __init__(self, key: tuple[int, float], access: int):
        self.key = key
        self.access = access | _PROCESS_QUERY_LIMITED_INFORMATION
        self.handle = None

    def __enter__(self):
        handle = _kernel32.OpenProcess(self.access, False, int(self.key[0]))
        if not handle:
            return None
        created = _creation_time(handle)
        if created is None or abs(created - self.key[1]) > 0.05:
            _kernel32.CloseHandle(handle)
            return None
        self.handle = handle
        return handle

    def __exit__(self, *exc):
        if self.handle:
            _kernel32.CloseHandle(self.handle)
        return False


class ClientLimits:
    """What is set on one client now, so a tick only calls Windows for changes."""

    def __init__(self):
        self.job = None
        self.cpu_cap = 0          # percent of the whole CPU, 0 = none
        self.efficiency = False
        self.affinity = 0         # bitmask, 0 = all cores
        self.last_trim = time.monotonic()


_LOCK = threading.RLock()
_applied: dict[tuple[int, float], ClientLimits] = {}
# Set by hand on the Clients page: {key: {"cpu_cap": int, "efficiency": bool, "affinity": int}}
_manual: dict[tuple[int, float], dict] = {}
_played_pid = 0


def _state(key) -> ClientLimits:
    with _LOCK:
        state = _applied.get(key)
        if state is None:
            state = _applied[key] = ClientLimits()
        return state


def set_cpu_cap(key: tuple[int, float], percent: int) -> bool:
    """Hard cap on the client's CPU use, in percent of the whole CPU (0 lifts it)."""
    state = _state(key)
    percent = max(0, min(100, int(percent)))
    if state.cpu_cap == percent:
        return True
    if state.job is None:
        if percent == 0:
            return True
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        with _Handle(key, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE) as handle:
            if not handle or not _kernel32.AssignProcessToJobObject(job, handle):
                _kernel32.CloseHandle(job)
                return False
        state.job = job
    info = _CpuRateControl()
    if percent:
        info.control_flags = _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        info.cpu_rate = percent * 100  # in 1/100 of a percent
    if not _kernel32.SetInformationJobObject(
        state.job, _JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION, ctypes.byref(info), ctypes.sizeof(info),
    ):
        return False
    state.cpu_cap = percent
    return True


def set_efficiency(key: tuple[int, float], enabled: bool) -> bool:
    """Efficiency mode: EcoQoS, below-normal priority and very low memory priority."""
    state = _state(key)
    if state.efficiency == bool(enabled):
        return True
    with _Handle(key, _PROCESS_SET_INFORMATION) as handle:
        if not handle:
            return False
        throttle = _PowerThrottlingState(1, _POWER_THROTTLING_EXECUTION_SPEED,
                                         _POWER_THROTTLING_EXECUTION_SPEED if enabled else 0)
        ok = bool(_kernel32.SetProcessInformation(
            handle, _PROCESS_POWER_THROTTLING, ctypes.byref(throttle), ctypes.sizeof(throttle),
        ))
        _kernel32.SetPriorityClass(handle, _BELOW_NORMAL_PRIORITY_CLASS if enabled else _NORMAL_PRIORITY_CLASS)
        priority = _MemoryPriority(_MEMORY_PRIORITY_VERY_LOW if enabled else _MEMORY_PRIORITY_NORMAL)
        _kernel32.SetProcessInformation(
            handle, _PROCESS_MEMORY_PRIORITY, ctypes.byref(priority), ctypes.sizeof(priority),
        )
    if ok:
        state.efficiency = bool(enabled)
    return ok


def set_affinity(key: tuple[int, float], mask: int) -> bool:
    """Cores the client may run on (bitmask; 0 = all of them)."""
    state = _state(key)
    if state.affinity == mask:
        return True
    full = (1 << (psutil.cpu_count() or 1)) - 1
    with _Handle(key, _PROCESS_SET_INFORMATION) as handle:
        if not handle or not _kernel32.SetProcessAffinityMask(handle, (mask & full) or full):
            return False
    state.affinity = mask
    return True


def trim(key: tuple[int, float]) -> bool:
    """Pages the client's working set out (it pages back in as used; commit doesn't change)."""
    with _Handle(key, _PROCESS_SET_QUOTA) as handle:
        ok = bool(handle) and bool(_psapi.EmptyWorkingSet(handle))
    _state(key).last_trim = time.monotonic()
    return ok


def set_manual(key: tuple[int, float], **limits) -> None:
    """A limit chosen on the Clients page; None removes the override for that limit."""
    with _LOCK:
        manual = _manual.setdefault(key, {})
        for name, value in limits.items():
            if value is None:
                manual.pop(name, None)
            else:
                manual[name] = value
    if "cpu_cap" in limits and limits["cpu_cap"] is not None:
        set_cpu_cap(key, limits["cpu_cap"])
    if "efficiency" in limits and limits["efficiency"] is not None:
        set_efficiency(key, limits["efficiency"])
    if "affinity" in limits and limits["affinity"] is not None:
        set_affinity(key, limits["affinity"])


def manual_of(key) -> dict:
    with _LOCK:
        return dict(_manual.get(key, {}))


def applied_of(key) -> dict:
    with _LOCK:
        state = _applied.get(key)
        if state is None:
            return {"cpu_cap": 0, "efficiency": False, "affinity": 0}
        return {"cpu_cap": state.cpu_cap, "efficiency": state.efficiency, "affinity": state.affinity}


def _forget(live: set) -> None:
    with _LOCK:
        for key in [k for k in _applied if k not in live]:
            job = _applied.pop(key).job
            if job:
                _kernel32.CloseHandle(job)
        for key in [k for k in _manual if k not in live]:
            _manual.pop(key, None)


def fair_cpu_cap(background_count: int) -> int:
    """Each background client's share when they are capped automatically."""
    if background_count <= 0:
        return 0
    return max(2, min(25, round(80 / background_count)))


def plan_tick(clients: list) -> None:
    """Client-tracker hook: apply the background limits from settings."""
    live = {client.key for client in clients}
    _forget(live)
    settings = settings_store.load()
    enabled = bool(settings.get("limits_background_enabled", False))
    # Main accounts (Full quality) keep running unlimited, as they keep their own graphics.
    exempt_accounts = set(settings.get("full_quality_accounts", []) or [])
    # The client being played is the one last in front, so it stays free while you
    # glance at another window (this app included).
    global _played_pid
    front = client_tracker.foreground_pid()
    if any(client.pid == front for client in clients):
        _played_pid = front
    background = [
        client for client in clients
        if enabled
        and client.pid != _played_pid
        and client.username not in exempt_accounts
        and client.state == "in game"
        and not client.hung
    ]
    background_keys = {client.key for client in background}
    cap_setting = int(settings.get("limits_background_cpu_percent", 0) or 0)
    cap = cap_setting or fair_cpu_cap(len(background))
    use_cap = bool(settings.get("limits_background_cpu", True))
    use_efficiency = bool(settings.get("limits_background_efficiency", True))
    trim_minutes = int(settings.get("limits_background_trim_minutes", 0) or 0)

    for client in clients:
        key = client.key
        manual = manual_of(key)
        limited = key in background_keys
        try:
            if "cpu_cap" not in manual:
                set_cpu_cap(key, cap if (limited and use_cap) else 0)
            if "efficiency" not in manual:
                set_efficiency(key, limited and use_efficiency)
            if limited and trim_minutes:
                if time.monotonic() - _state(key).last_trim >= trim_minutes * 60:
                    trim(key)
        except Exception as exc:
            print(f"[WARNING] Limits for Roblox PID {client.pid}: {type(exc).__name__}: {exc}")


def release_all() -> None:
    """Takes every limit back (on exit): a job outlives us while its client runs."""
    with _LOCK:
        keys = list(_applied)
        _manual.clear()
    for key in keys:
        try:
            set_cpu_cap(key, 0)
            set_efficiency(key, False)
            set_affinity(key, 0)
        except Exception:
            pass
    _forget(set())
