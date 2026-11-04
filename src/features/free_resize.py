"""
features/free_resize.py
Resizing a Roblox client by its edges past the minimum size Roblox holds it to, and moving a
small client by its title bar without it snapping back (after RoALT's instances/resize.rs, MIT).

The client clamps its window in WM_GETMINMAXINFO, and Windows' own sizing and move loops keep
to that whatever the window's style, so those loops must not start. A low-level mouse hook in
this process takes a left press on a client's edge (or on a small client's title bar), keeps it
from reaching Windows, and sizes the window itself with SetWindowPos + SWP_NOSENDCHANGING until
the button comes up. Nothing goes into the client, and no other window is touched.

A Python hook runs on every mouse event on the PC, so it is off unless switched on, and does
almost nothing for a press that isn't on a Roblox client.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi")

_WH_MOUSE_LL = 14
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_QUIT = 0x0012
_LLMHF_INJECTED = 0x1
_GA_ROOT = 2
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_NOSENDCHANGING = 0x0400
_SWP_ASYNCWINDOWPOS = 0x4000
_SWP_NOSIZE = 0x0001
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_SM_CYCAPTION = 4

# A press this close to the visible frame (or in the invisible border outside it) is an edge.
_EDGE = 6
# Smallest a client is made, visible frame.
MIN_WIDTH, MIN_HEIGHT = 160, 120
# Roblox's own minimum (visible frame): a title-bar drag below it would snap the window up.
_ROBLOX_MIN = (800, 600)
# Leave the caption buttons (minimize, maximize, close) to Windows.
_CAPTION_BUTTONS = 150


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouse_data", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra", ctypes.c_size_t),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
_user32.IsZoomed.argtypes = [wintypes.HWND]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


def _rect(hwnd) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _frame(hwnd, outer) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)):
        return outer
    return rect.left, rect.top, rect.right, rect.bottom


def hit(point: tuple[int, int], outer, frame, caption: int) -> str:
    """What a press at `point` takes hold of: "" (nothing), "move", or the edges it sizes
    ("l", "t", "r", "b" combined, e.g. "rb"). Pure, for tests."""
    x, y = point
    left, top, right, bottom = outer
    if not (left <= x < right and top <= y < bottom):
        return ""
    f_left, f_top, f_right, f_bottom = frame
    edges = ""
    if x < f_left + _EDGE:
        edges += "l"
    elif x >= f_right - _EDGE:
        edges += "r"
    if y < f_top + _EDGE:
        edges += "t"
    elif y >= f_bottom - _EDGE:
        edges += "b"
    if edges:
        return edges
    small = (f_right - f_left) < _ROBLOX_MIN[0] or (f_bottom - f_top) < _ROBLOX_MIN[1]
    if small and y < f_top + caption and x < f_right - _CAPTION_BUTTONS:
        return "move"
    return ""


def dragged(start, grab: str, dx: int, dy: int, inset) -> tuple[int, int, int, int]:
    """The outer rect after dragging `grab` by (dx, dy), kept at least MIN_* visible. Pure."""
    left, top, right, bottom = start
    if grab == "move":
        return left + dx, top + dy, right + dx, bottom + dy
    min_w = MIN_WIDTH + inset[0] + inset[2]
    min_h = MIN_HEIGHT + inset[1] + inset[3]
    if "l" in grab:
        left = min(left + dx, right - min_w)
    if "r" in grab:
        right = max(right + dx, left + min_w)
    if "t" in grab:
        top = min(top + dy, bottom - min_h)
    if "b" in grab:
        bottom = max(bottom + dy, top + min_h)
    return left, top, right, bottom


class FreeResize:
    def __init__(self):
        self._pids: frozenset[int] = frozenset()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook = None
        self._proc = _HOOKPROC(self._on_mouse)
        self._drag: dict | None = None
        self._caption = _user32.GetSystemMetrics(_SM_CYCAPTION) + 8
        # Presses another program injected (anti-AFK's, for one) aren't the user's.
        self.ignore_flags = _LLMHF_INJECTED

    def set_pids(self, pids) -> None:
        self._pids = frozenset(int(pid) for pid in pids)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(ready,), daemon=True, name="FreeResizeHook")
        self._thread.start()
        ready.wait(2.0)

    def stop(self) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._drag = None

    def _loop(self, ready: threading.Event) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()
        self._hook = _user32.SetWindowsHookExW(_WH_MOUSE_LL, self._proc, None, 0)
        ready.set()
        if not self._hook:
            print(f"[WARNING] Free resizing: the mouse hook couldn't start ({ctypes.get_last_error()})")
            return
        message = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        _user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._thread_id = 0

    def _client_at(self, point: wintypes.POINT) -> int:
        hwnd = _user32.WindowFromPoint(point)
        if not hwnd:
            return 0
        root = _user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        return root if pid.value in self._pids else 0

    def _on_mouse(self, code, wparam, lparam):
        try:
            if code >= 0 and self._pids:
                info = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                if not info.flags & self.ignore_flags:
                    if self._drag is not None:
                        if wparam == _WM_MOUSEMOVE:
                            self._move_to(info.pt.x, info.pt.y)
                        elif wparam == _WM_LBUTTONUP:
                            self._move_to(info.pt.x, info.pt.y)
                            self._drag = None
                            return 1  # its press never reached Windows either
                    elif wparam == _WM_LBUTTONDOWN and self._begin(info.pt):
                        return 1
        except Exception:
            self._drag = None
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    def _begin(self, point: wintypes.POINT) -> bool:
        hwnd = self._client_at(point)
        if not hwnd or _user32.IsZoomed(hwnd) or _user32.IsIconic(hwnd):
            return False
        outer = _rect(hwnd)
        if outer is None:
            return False
        frame = _frame(hwnd, outer)
        grab = hit((point.x, point.y), outer, frame, self._caption)
        if not grab:
            return False
        inset = (frame[0] - outer[0], frame[1] - outer[1], outer[2] - frame[2], outer[3] - frame[3])
        self._drag = {"hwnd": hwnd, "grab": grab, "start": outer, "x": point.x, "y": point.y, "inset": inset}
        return True

    def _move_to(self, x: int, y: int) -> None:
        drag = self._drag
        if drag is None:
            return
        left, top, right, bottom = dragged(drag["start"], drag["grab"], x - drag["x"], y - drag["y"], drag["inset"])
        flags = _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_NOSENDCHANGING | _SWP_ASYNCWINDOWPOS
        if drag["grab"] == "move":
            flags |= _SWP_NOSIZE
        _user32.SetWindowPos(drag["hwnd"], None, left, top, right - left, bottom - top, flags)


_INSTANCE: FreeResize | None = None


def get() -> FreeResize:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = FreeResize()
    return _INSTANCE
