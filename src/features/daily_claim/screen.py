"""
On-screen claim engine: every managed client is shrunk to one fixed tile (320 x 240, the same as
the Clients page's Small Window and the Window Grid tile; Roblox's client area is then 318 x 208),
so the templates cut from real captures (anime_dice_screen.json) match at the same scale. Pictures come from PrintWindow (works while the window is covered,
not while minimized); clicks are real mouse input, sent one window at a time.

A template is a small grayscale patch around a point. Matching compares that
patch with the window by mean absolute difference: near the recorded spot first, then over the
whole window (a calendar tile can move from day to day).
"""

from __future__ import annotations

import base64
import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

DEFAULT_SIZE = (320, 240)
PATCH = (36, 20)
MATCH_LIMIT = 22.0          # mean |difference| (0-255) a match may have
_PW_RENDERFULLCONTENT = 0x2

@dataclass
class Frame:
    width: int
    height: int
    gray: bytes      # one byte a pixel, row-major

    def at(self, x: int, y: int) -> int:
        return self.gray[y * self.width + x]

    def crop(self, cx: int, cy: int, w: int, h: int) -> "Template | None":
        x0, y0 = cx - w // 2, cy - h // 2
        if x0 < 0 or y0 < 0 or x0 + w > self.width or y0 + h > self.height:
            return None
        rows = [self.gray[(y0 + r) * self.width + x0:(y0 + r) * self.width + x0 + w] for r in range(h)]
        return Template(cx, cy, w, h, b"".join(rows))


@dataclass
class Template:
    x: int          # centre (the click point), window client coordinates
    y: int
    w: int
    h: int
    pixels: bytes

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h,
                "pixels": base64.b64encode(self.pixels).decode("ascii")}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Template | None":
        if not isinstance(data, dict):
            return None
        try:
            t = cls(int(data["x"]), int(data["y"]), int(data["w"]), int(data["h"]),
                    base64.b64decode(data["pixels"]))
        except (KeyError, TypeError, ValueError):
            return None
        return t if len(t.pixels) == t.w * t.h else None


def _score(frame: Frame, t: Template, cx: int, cy: int, step: int, limit: float) -> float:
    """Mean |difference| of the template centred at (cx, cy); stops early once over `limit`."""
    x0, y0 = cx - t.w // 2, cy - t.h // 2
    if x0 < 0 or y0 < 0 or x0 + t.w > frame.width or y0 + t.h > frame.height:
        return 255.0
    gray, fw, px, tw = frame.gray, frame.width, t.pixels, t.w
    total = 0
    count = 0
    budget = limit * ((t.w + step - 1) // step) * ((t.h + step - 1) // step)
    for r in range(0, t.h, step):
        row = (y0 + r) * fw + x0
        trow = r * tw
        for c in range(0, t.w, step):
            total += abs(gray[row + c] - px[trow + c])
        count += (tw + step - 1) // step
        if total > budget:
            return 255.0
    return total / max(1, count)


def find(frame: Frame, t: Template, radius: int = 10, full: bool = True,
         limit: float = MATCH_LIMIT) -> tuple[float, int, int] | None:
    """(score, x, y) of the best match within `limit`, or None."""
    best = (255.0, t.x, t.y)
    for dy in range(-radius, radius + 1, 2):
        for dx in range(-radius, radius + 1, 2):
            s = _score(frame, t, t.x + dx, t.y + dy, 2, limit * 1.5)
            if s < best[0]:
                best = (s, t.x + dx, t.y + dy)
    if best[0] > limit and full:
        coarse = (255.0, 0, 0)
        for cy in range(t.h // 2, frame.height - t.h // 2, 4):
            for cx in range(t.w // 2, frame.width - t.w // 2, 4):
                s = _score(frame, t, cx, cy, 3, limit * 1.5)
                if s < coarse[0]:
                    coarse = (s, cx, cy)
        if coarse[0] < 255.0:
            _, bx, by = coarse
            for dy in range(-3, 4):
                for dx in range(-3, 4):
                    s = _score(frame, t, bx + dx, by + dy, 1, limit * 1.5)
                    if s < best[0]:
                        best = (s, bx + dx, by + dy)
    if best[0] <= limit:
        # Final score at full resolution.
        s = _score(frame, t, best[1], best[2], 1, 255.0)
        if s <= limit:
            return s, best[1], best[2]
    return None


class ScreenProfile:
    """Templates and tile positions for one game at one tile size, cut from real captures."""

    def __init__(self, data: dict):
        self.window = tuple(data.get("window") or DEFAULT_SIZE)
        self.client = tuple(data.get("client") or (0, 0))
        self.observed = str(data.get("observed", ""))
        self.templates: dict[str, Template] = {}
        for key, value in (data.get("templates") or {}).items():
            t = Template.from_dict(value)
            if t is not None:
                self.templates[key] = t
        self.tiles = [tuple(xy) for xy in data.get("tiles") or []]
        self.check_size = tuple(data.get("check_size") or (16, 14))

    def t(self, key: str) -> Template:
        return self.templates[key]

    def seen(self, frame: Frame, key: str, radius: int = 6, full: bool = True):
        t = self.templates.get(key)
        return find(frame, t, radius=radius, full=full) if t is not None else None

    def claimed_days(self, frame: Frame) -> list[bool]:
        """Which calendar tiles show the green check."""
        check = self.templates["check"]
        out = []
        for x, y in self.tiles:
            probe = Template(x, y, check.w, check.h, check.pixels)
            out.append(find(frame, probe, radius=2, full=False) is not None)
        return out

    @classmethod
    def load(cls, name: str = "anime_dice_screen.json") -> "ScreenProfile":
        import json
        import os
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), encoding="utf-8") as f:
            return cls(json.load(f))


# Windows side

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_CLICK_LOCK = threading.Lock()


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def client_size(hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    _user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def capture(hwnd: int) -> Frame | None:
    """The window's client area as grayscale, or None (minimized, closed, black frame)."""
    width, height = client_size(hwnd)
    if width <= 0 or height <= 0 or _user32.IsIconic(wintypes.HWND(hwnd)):
        return None
    window_dc = _user32.GetWindowDC(wintypes.HWND(hwnd))
    mem_dc = _gdi32.CreateCompatibleDC(window_dc)
    # PrintWindow draws the whole window (frame included); the client area sits at an offset.
    wrect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
    _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(wrect))
    _user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin))
    full_w, full_h = wrect.right - wrect.left, wrect.bottom - wrect.top
    off_x, off_y = origin.x - wrect.left, origin.y - wrect.top
    bitmap = _gdi32.CreateCompatibleBitmap(window_dc, full_w, full_h)
    old = _gdi32.SelectObject(mem_dc, bitmap)
    try:
        if not _user32.PrintWindow(wintypes.HWND(hwnd), mem_dc, _PW_RENDERFULLCONTENT):
            return None
        info = _BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(info)
        info.biWidth, info.biHeight = full_w, -full_h
        info.biPlanes, info.biBitCount = 1, 32
        buffer = ctypes.create_string_buffer(full_w * full_h * 4)
        if not _gdi32.GetDIBits(mem_dc, bitmap, 0, full_h, buffer, ctypes.byref(info), 0):
            return None
        raw = buffer.raw
    finally:
        _gdi32.SelectObject(mem_dc, old)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(wintypes.HWND(hwnd), window_dc)
    rows = []
    for y in range(off_y, off_y + height):
        start = (y * full_w + off_x) * 4
        rows.append(raw[start + 1:start + width * 4:4])     # green channel ~ luminance
    gray = b"".join(rows)
    if len(gray) != width * height or max(gray[::97] or b"\0") < 8:
        return None    # a black frame: nothing rendered yet
    return Frame(width, height, gray)


def capture_rgb(hwnd: int) -> tuple[int, int, bytes] | None:
    """BGRA picture of the client area (previews and diagnostics)."""
    width, height = client_size(hwnd)
    if width <= 0 or height <= 0 or _user32.IsIconic(wintypes.HWND(hwnd)):
        return None
    window_dc = _user32.GetWindowDC(wintypes.HWND(hwnd))
    mem_dc = _gdi32.CreateCompatibleDC(window_dc)
    wrect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
    _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(wrect))
    _user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin))
    full_w, full_h = wrect.right - wrect.left, wrect.bottom - wrect.top
    off_x, off_y = origin.x - wrect.left, origin.y - wrect.top
    bitmap = _gdi32.CreateCompatibleBitmap(window_dc, full_w, full_h)
    old = _gdi32.SelectObject(mem_dc, bitmap)
    try:
        if not _user32.PrintWindow(wintypes.HWND(hwnd), mem_dc, _PW_RENDERFULLCONTENT):
            return None
        info = _BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(info)
        info.biWidth, info.biHeight = full_w, -full_h
        info.biPlanes, info.biBitCount = 1, 32
        buffer = ctypes.create_string_buffer(full_w * full_h * 4)
        if not _gdi32.GetDIBits(mem_dc, bitmap, 0, full_h, buffer, ctypes.byref(info), 0):
            return None
        raw = buffer.raw
    finally:
        _gdi32.SelectObject(mem_dc, old)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(wintypes.HWND(hwnd), window_dc)
    rows = [raw[((y * full_w) + off_x) * 4:((y * full_w) + off_x + width) * 4]
            for y in range(off_y, off_y + height)]
    return width, height, b"".join(rows)


def size_window(hwnd: int, slot: int, width: int, height: int) -> None:
    """Shrinks a client to the tile size and puts slot N in its own cell (left to right, top
    to bottom over the primary work area), so tiles never cover each other."""
    from features import window_grid

    area = wintypes.RECT()
    _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)    # SPI_GETWORKAREA
    columns = max(1, (area.right - area.left) // width)
    rows = max(1, (area.bottom - area.top) // height)
    cell = slot % (columns * rows)
    x = area.left + (cell % columns) * width
    y = area.top + (cell // columns) * height
    window_grid._place_window(hwnd, x, y, width, height)


def _move_input(dx: int, dy: int, absolute: bool):
    from features import account_actions as aa

    flags = 0x0001 | ((0x8000 | 0x4000) if absolute else 0)     # MOVE (| ABSOLUTE | VIRTUALDESK)
    return aa._AfkInput(input_type=aa._AFK_INPUT_MOUSE, mouse=aa._AfkMouseInput(
        dx=dx, dy=dy, mouse_data=0, flags=flags, time=0, extra_info=0))


def _move_to(sx: int, sy: int) -> None:
    """Moves the cursor with real mouse input: Roblox ignores SetCursorPos for hover/press."""
    from features import account_actions as aa

    left, top = _user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77)
    width, height = _user32.GetSystemMetrics(78), _user32.GetSystemMetrics(79)
    nx = int(round((sx - left) * 65535 / max(1, width - 1)))
    ny = int(round((sy - top) * 65535 / max(1, height - 1)))
    aa._send_afk_input(_move_input(nx, ny, True))


def click(hwnd: int, x: int, y: int) -> bool:
    """A real left click at client point (x, y). One window at a time; the cursor and the
    window that had focus are put back afterwards."""
    from features import account_actions

    with _CLICK_LOCK:
        previous = _user32.GetForegroundWindow()
        cursor = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(cursor))
        point = wintypes.POINT(int(x), int(y))
        _user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(point))
        try:
            if not account_actions._focus_afk_window(hwnd):
                return False
            # Roblox drops a press that lands while it is still taking focus.
            time.sleep(0.3)
            _move_to(point.x, point.y)
            time.sleep(0.1)
            # A one-pixel nudge so Roblox registers the hover before the press.
            account_actions._send_afk_input(_move_input(1, 0, False))
            time.sleep(0.05)
            account_actions._send_afk_input(_move_input(-1, 0, False))
            time.sleep(0.08)
            down = account_actions._send_afk_input(account_actions._afk_mouse_input(0x0002))
            time.sleep(0.05)
            up = account_actions._send_afk_input(account_actions._afk_mouse_input(0x0004))
            time.sleep(0.05)
            return bool(down and up)
        finally:
            _user32.SetCursorPos(cursor.x, cursor.y)
            if previous and previous != hwnd:
                account_actions._focus_afk_window(previous)


class WindowsScreen:
    """The real backend; tests pass a fake with the same four methods."""

    def __init__(self, tracker_getter):
        self._tracker = tracker_getter

    def hwnd_for(self, handle) -> int:
        client = self._tracker().get(handle.key)
        return int(client.hwnd) if client and client.hwnd else 0

    def size(self, hwnd: int, slot: int, width: int, height: int) -> None:
        size_window(hwnd, slot, width, height)

    def capture(self, hwnd: int) -> Frame | None:
        return capture(hwnd)

    def click(self, hwnd: int, x: int, y: int) -> bool:
        return click(hwnd, x, y)
