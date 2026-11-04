"""
Global hotkey and window tiling support for Roblox windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import math

import win32api
import win32con
import win32gui

from classes.operation_result import OperationResult
import features.presence as presence_mod


DEFAULT_HOTKEY = "Ctrl+Shift+A"
HOTKEY_ID = 0x52414D47
# Stop All (panic) shares the registration code under its own id.
PANIC_HOTKEY_ID = 0x52414D48
WM_HOTKEY = 0x0312

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000

_SPECIAL_KEYS = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "PAUSE": 0x13,
    "CAPSLOCK": 0x14,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "SPACE": 0x20,
    "PGUP": 0x21,
    "PAGEUP": 0x21,
    "PGDOWN": 0x22,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "INS": 0x2D,
    "INSERT": 0x2D,
    "DEL": 0x2E,
    "DELETE": 0x2E,
}

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.RegisterHotKey.argtypes = [
    wintypes.HWND,
    ctypes.c_int,
    wintypes.UINT,
    wintypes.UINT,
]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]

_dwmapi = ctypes.WinDLL("dwmapi")
_dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
_dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]

_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
# Skips WM_WINDOWPOSCHANGING, which is where Roblox clamps the window to its
# minimum tracking size, so grid cells can be smaller than a manual drag allows.
_SWP_NOSENDCHANGING = 0x0400


def _parse_hotkey(sequence: str) -> tuple[int, int] | None:
    if not sequence or "," in sequence:
        return None

    parts = [part.strip() for part in sequence.split("+") if part.strip()]
    if not parts:
        return None

    modifiers = 0
    key_name = ""
    for part in parts:
        normalized = part.upper().replace(" ", "")
        if normalized in {"CTRL", "CONTROL"}:
            modifiers |= _MOD_CONTROL
        elif normalized == "SHIFT":
            modifiers |= _MOD_SHIFT
        elif normalized == "ALT":
            modifiers |= _MOD_ALT
        elif normalized in {"META", "WIN", "WINDOWS"}:
            modifiers |= _MOD_WIN
        elif key_name:
            return None
        else:
            key_name = normalized

    if not key_name:
        return None
    if len(key_name) == 1 and key_name.isalnum():
        virtual_key = ord(key_name)
    elif key_name.startswith("F") and key_name[1:].isdigit():
        function_number = int(key_name[1:])
        if not 1 <= function_number <= 24:
            return None
        virtual_key = 0x70 + function_number - 1
    else:
        virtual_key = _SPECIAL_KEYS.get(key_name, 0)

    if not virtual_key:
        return None
    return modifiers | _MOD_NOREPEAT, virtual_key


def register_hotkey(window_handle: int, sequence: str, hotkey_id: int = HOTKEY_ID) -> OperationResult:
    parsed = _parse_hotkey(sequence)
    if not parsed:
        return OperationResult.failure(
            "WINDOW_GRID_KEYBIND_INVALID",
            "Invalid Window Grid Keybind",
            "Choose one keyboard shortcut using a letter, number, function key, or navigation key. "
            f"The keybind was reset to {DEFAULT_HOTKEY}.",
            detail=f"Keybind: {sequence or '(empty)'}",
        )

    modifiers, virtual_key = parsed
    if _user32.RegisterHotKey(
        wintypes.HWND(window_handle),
        hotkey_id,
        modifiers,
        virtual_key,
    ):
        return OperationResult.success(
            f"Window Grid keybind registered: {sequence}"
        )

    error_code = ctypes.get_last_error()
    if error_code == 1409:
        return OperationResult.failure(
            "WINDOW_GRID_KEYBIND_IN_USE",
            "Window Grid Keybind Already In Use",
            "Another application is already using this keybind. Choose a different one.",
            detail=f"Keybind: {sequence}",
        )
    return OperationResult.failure(
        "WINDOW_GRID_KEYBIND_FAILED",
        "Window Grid Keybind Could Not Be Enabled",
        "Windows could not register the selected keybind.",
        detail=f"Keybind: {sequence}\nWindows error: {error_code}",
    )


def unregister_hotkey(window_handle: int, hotkey_id: int = HOTKEY_ID) -> None:
    if window_handle:
        _user32.UnregisterHotKey(wintypes.HWND(window_handle), hotkey_id)


def is_hotkey_message(message, hotkey_id: int = HOTKEY_ID) -> bool:
    try:
        native_message = wintypes.MSG.from_address(int(message))
        return (
            native_message.message == WM_HOTKEY
            and int(native_message.wParam) == hotkey_id
        )
    except Exception:
        return False


def _get_roblox_pids() -> set[int]:
    return set(presence_mod.get_roblox_processes())


def _get_roblox_windows() -> list[int]:
    roblox_pids = _get_roblox_pids()
    largest_by_pid: dict[int, tuple[int, int]] = {}
    windows_by_pid = presence_mod.get_windows_by_pid(roblox_pids)
    for pid, hwnds in windows_by_pid.items():
        for hwnd in hwnds:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    continue
                if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                    continue
                if not win32gui.GetWindowText(hwnd).strip():
                    continue
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                area = max(0, right - left) * max(0, bottom - top)
                if area > largest_by_pid.get(pid, (0, 0))[0]:
                    largest_by_pid[pid] = (area, hwnd)
            except Exception:
                pass
    return [largest_by_pid[pid][1] for pid in sorted(largest_by_pid)]


def _get_cursor_monitor_work_area() -> tuple[int, int, int, int]:
    cursor = win32api.GetCursorPos()
    monitor = win32api.MonitorFromPoint(
        cursor,
        win32con.MONITOR_DEFAULTTONEAREST,
    )
    return tuple(win32api.GetMonitorInfo(monitor)["Work"])


def _frame_insets(hwnd: int) -> tuple[int, int, int, int]:
    """Invisible resize borders between GetWindowRect and the visible frame."""
    window = win32gui.GetWindowRect(hwnd)
    frame = wintypes.RECT()
    if _dwmapi.DwmGetWindowAttribute(
        hwnd,
        _DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(frame),
        ctypes.sizeof(frame),
    ):
        return 0, 0, 0, 0
    return (
        max(0, frame.left - window[0]),
        max(0, frame.top - window[1]),
        max(0, window[2] - frame.right),
        max(0, window[3] - frame.bottom),
    )


def _place_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    if win32gui.IsIconic(hwnd) or _user32.IsZoomed(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    inset_left, inset_top, inset_right, inset_bottom = _frame_insets(hwnd)
    if not _user32.SetWindowPos(
        hwnd,
        None,
        x - inset_left,
        y - inset_top,
        width + inset_left + inset_right,
        height + inset_top + inset_bottom,
        _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_NOSENDCHANGING,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _grid_dimensions(
    count: int,
    available_width: int,
    max_cell_width: int,
) -> tuple[int, int]:
    if max_cell_width > 0:
        columns = max(1, min(count, available_width // max_cell_width))
    else:
        columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    return columns, rows


def tile_roblox_windows(
    max_width: int = 0,
    max_height: int = 0,
) -> OperationResult:
    """Tile every Roblox window; a max size of 0 lets that side fill its cell."""
    windows = _get_roblox_windows()
    if not windows:
        return OperationResult.failure(
            "ROBLOX_WINDOWS_NOT_FOUND",
            "No Roblox Windows Found",
            "Open at least one visible Roblox window before using Window Grid.",
        )

    try:
        left, top, right, bottom = _get_cursor_monitor_work_area()
        available_width = right - left
        available_height = bottom - top
        gap = 4
        max_cell_width = max_width + gap * 2 if max_width > 0 else 0
        max_cell_height = max_height + gap * 2 if max_height > 0 else 0
        columns, rows = _grid_dimensions(
            len(windows), available_width, max_cell_width
        )
        # Capped tiles pack from the top-left corner and only shrink further
        # when there are too many windows to fit at the requested size.
        grid_width = available_width
        if max_cell_width:
            grid_width = min(available_width, columns * max_cell_width)
        grid_height = available_height
        if max_cell_height:
            grid_height = min(available_height, rows * max_cell_height)
        moved = 0

        for index, hwnd in enumerate(windows):
            row = index // columns
            column = index % columns
            cell_left = left + column * grid_width // columns
            cell_right = left + (column + 1) * grid_width // columns
            cell_top = top + row * grid_height // rows
            cell_bottom = top + (row + 1) * grid_height // rows

            x = cell_left + gap
            y = cell_top + gap
            width = max(1, cell_right - cell_left - gap * 2)
            height = max(1, cell_bottom - cell_top - gap * 2)

            try:
                _place_window(hwnd, x, y, width, height)
                moved += 1
            except Exception as exc:
                print(f"[Window Grid] Failed to move window {hwnd}: {exc}")

        if not moved:
            return OperationResult.failure(
                "ROBLOX_WINDOWS_MOVE_FAILED",
                "Roblox Windows Could Not Be Arranged",
                "Windows prevented the Roblox windows from being moved.",
            )

        print(
            f"[Window Grid] Arranged {moved} Roblox window(s) "
            f"into a {columns}x{rows} grid."
        )
        return OperationResult.success(
            f"Arranged {moved} Roblox window(s).",
            data={"count": moved, "columns": columns, "rows": rows},
        )
    except Exception as exc:
        return OperationResult.failure(
            "WINDOW_GRID_FAILED",
            "Window Grid Failed",
            "The Roblox windows could not be arranged.",
            detail=f"{type(exc).__name__}: {exc}",
        )
