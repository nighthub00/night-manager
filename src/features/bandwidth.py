"""
features/bandwidth.py
One upload cap for every Roblox client together: a Windows QoS policy on RobloxPlayerBeta.exe
(after RoALT's instances/qos.rs, MIT). Setting it needs admin rights, so a fixed built-in
PowerShell script (only the validated rate goes in) runs through the UAC prompt, and only when
the user clicks Apply. The policy lives in the ActiveStore, so Windows drops it on restart and
nothing about the system stays changed.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

import psutil

from classes.operation_result import OperationResult
import features.settings_store as settings_store

POLICY = "NightManager-Roblox"
MIN_KBPS, MAX_KBPS = 64, 1_000_000
_SEE_MASK_NOCLOSEPROCESS = 0x40
_SW_HIDE = 0


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


def _run_elevated(script: str) -> OperationResult:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = f"-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    info.nShow = _SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return OperationResult.failure(
            "BANDWIDTH_NOT_ALLOWED", "Bandwidth Limit Not Set",
            "Windows didn't allow the change (the admin prompt was declined).",
        )
    kernel32 = ctypes.windll.kernel32
    kernel32.WaitForSingleObject(info.hProcess, 60_000)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(info.hProcess)
    if code.value != 0:
        return OperationResult.failure(
            "BANDWIDTH_FAILED", "Bandwidth Limit Not Set",
            "Windows refused the QoS policy.", detail=f"PowerShell exit code {code.value}",
        )
    return OperationResult.success()


def apply(kbps: int) -> OperationResult:
    kbps = int(kbps)
    if not MIN_KBPS <= kbps <= MAX_KBPS:
        return OperationResult.failure(
            "BANDWIDTH_INVALID", "Invalid Limit",
            f"Pick between {MIN_KBPS} and {MAX_KBPS} kbit/s.",
        )
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Remove-NetQosPolicy -Name '{POLICY}' -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue; "
        f"New-NetQosPolicy -Name '{POLICY}' -AppPathNameMatchCondition 'RobloxPlayerBeta.exe' "
        f"-ThrottleRateActionBitsPerSecond {kbps * 1000} -PolicyStore ActiveStore | Out-Null"
    )
    result = _run_elevated(script)
    if result:
        settings_store.save("bandwidth_applied", {"kbps": kbps, "boot": psutil.boot_time()})
    return result


def clear() -> OperationResult:
    script = f"Remove-NetQosPolicy -Name '{POLICY}' -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue; exit 0"
    result = _run_elevated(script)
    if result:
        settings_store.save("bandwidth_applied", {})
    return result


def active_kbps() -> int:
    """The cap applied since Windows last started (reading it back would need admin rights)."""
    applied = settings_store.get("bandwidth_applied", {}) or {}
    if applied.get("kbps") and abs(float(applied.get("boot", 0)) - psutil.boot_time()) < 5:
        return int(applied["kbps"])
    return 0
