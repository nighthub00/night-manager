"""
On-screen claim engine: every managed client is shrunk to one fixed tile (320 x 240, the same as
the Clients page's Small Window and the Window Grid tile; Roblox's client area is then 318 x 208),
so the templates cut from real captures (anime_dice_screen.json) match at the same scale. Pictures come from PrintWindow (works while the window is covered,
not while minimized); clicks are real mouse input, sent one window at a time.

A template is a small grayscale patch around a point. Matching compares that patch with the
window by mean absolute difference: near the recorded spot first, then over the whole window (a
calendar tile can move from day to day). Both sides are softened with a 3 x 3 box blur first, so
a half-pixel shift in how Roblox draws text (which broke every text template after the
2026-11-12 client update) no longer reads as a different picture. A key can hold several
variants (one per look the game has had); the best one counts.
"""

from __future__ import annotations

import base64
import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

DEFAULT_SIZE = (320, 240)
PATCH = (36, 20)
CHECK_LIMIT = 24.0
MATCH_LIMIT = 13.0          # mean |difference| (0-255, on the softened picture) a match may have
_PW_RENDERFULLCONTENT = 0x2

def soften(gray: bytes, width: int, height: int) -> bytes:
    """3 x 3 box blur (edges repeat), so sub-pixel text shifts stop mattering."""
    if width < 2 or height < 2 or len(gray) != width * height:
        return bytes(gray)
    rows = []
    for y in range(height):
        row = gray[y * width:(y + 1) * width]
        left = row[:1] + row[:-1]
        right = row[1:] + row[-1:]
        rows.append([a + b + c for a, b, c in zip(left, row, right)])
    out = bytearray()
    for y in range(height):
        up, mid, down = rows[max(0, y - 1)], rows[y], rows[min(height - 1, y + 1)]
        out += bytes((a + b + c) // 9 for a, b, c in zip(up, mid, down))
    return bytes(out)


@dataclass
class Frame:
    width: int
    height: int
    gray: bytes      # one byte a pixel, row-major
    _soft: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def soft(self) -> bytes:
        if self._soft is None:
            self._soft = soften(self.gray, self.width, self.height)
        return self._soft

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
    _soft: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def soft(self) -> bytes:
        if self._soft is None:
            self._soft = soften(self.pixels, self.w, self.h)
        return self._soft

    def at_point(self, x: int, y: int) -> "Template":
        """The same picture expected around another point (a calendar tile, a moved button)."""
        return Template(x, y, self.w, self.h, self.pixels, self._soft)

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
    gray, fw, px, tw = frame.soft, frame.width, t.soft, t.w
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
    """(score, x, y) of the best match within `limit`, or None. Every offset within `radius` of
    the template's own spot is tried (sampling every other pixel), then the best one is
    refined at full resolution; `full` falls back to a coarse scan of the whole window."""
    loose = limit * 1.5

    def refine(bx: int, by: int, r: int) -> tuple[float, int, int]:
        b = (255.0, bx, by)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                s = _score(frame, t, bx + dx, by + dy, 1, loose)
                if s < b[0]:
                    b = (s, bx + dx, by + dy)
        return b

    best = (255.0, t.x, t.y)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            s = _score(frame, t, t.x + dx, t.y + dy, 2, loose)
            if s < best[0]:
                best = (s, t.x + dx, t.y + dy)
    if best[0] < 255.0:
        best = refine(best[1], best[2], 1)
    if best[0] > limit and full:
        coarse = (255.0, 0, 0)
        for cy in range(t.h // 2, frame.height - t.h // 2, 4):
            for cx in range(t.w // 2, frame.width - t.w // 2, 4):
                s = _score(frame, t, cx, cy, 3, loose)
                if s < coarse[0]:
                    coarse = (s, cx, cy)
        if coarse[0] < 255.0:
            found = refine(coarse[1], coarse[2], 3)
            if found[0] < best[0]:
                best = found
    return best if best[0] <= limit else None


class ScreenProfile:
    """Templates and tile positions for one game at one tile size, cut from real captures."""

    def __init__(self, data: dict):
        self.window = tuple(data.get("window") or DEFAULT_SIZE)
        self.client = tuple(data.get("client") or (0, 0))
        self.observed = str(data.get("observed", ""))
        self.variants: dict[str, list[Template]] = {}
        for key, value in (data.get("templates") or {}).items():
            items = value if isinstance(value, list) else [value]
            ts = [t for t in (Template.from_dict(v) for v in items) if t is not None]
            if ts:
                self.variants[key] = ts
        # First variant of each key (positions, sizes), for callers that want just one.
        self.templates: dict[str, Template] = {k: v[0] for k, v in self.variants.items()}
        self.tiles = [tuple(xy) for xy in data.get("tiles") or []]
        self.check_size = tuple(data.get("check_size") or (16, 14))
        # The size a client is grown to for a moment to reach the game's top-bar Daily button,
        # which Roblox tucks into its overflow menu at tile size.
        self.open_size = tuple(data.get("open_size") or (900, 600))

    def t(self, key: str) -> Template:
        return self.templates[key]

    def seen(self, frame: Frame, key: str, radius: int = 6, full: bool = True,
             limit: float = MATCH_LIMIT):
        """(score, x, y) of the best-matching variant of `key`, or None. Every variant is tried
        near its own spot before any of them is searched for over the whole window."""
        best = None
        for t in self.variants.get(key, ()):
            hit = find(frame, t, radius=radius, full=False, limit=limit)
            if hit and (best is None or hit[0] < best[0]):
                best = hit
        if best is None and full:
            for t in self.variants.get(key, ()):
                hit = find(frame, t, radius=radius, full=True, limit=limit)
                if hit and (best is None or hit[0] < best[0]):
                    best = hit
        return best

    def which(self, frame: Frame, keys: tuple[str, ...], **kw) -> str | None:
        """The key among `keys` that matches best. Labels that share words ("Reward Ready!" and
        "Next Reward In") can both pass the limit; the closer one wins."""
        scored = [(hit[0], key) for key in keys if (hit := self.seen(frame, key, **kw))]
        return min(scored)[1] if scored else None

    def claimed_days(self, frame: Frame) -> list[bool]:
        """Which calendar tiles show the green check."""
        # A check that has just been earned is still fading in (scores ~14); an empty tile
        # scores 37+, so the check gets a looser limit than text labels.
        return [any(find(frame, check.at_point(x, y), radius=2, full=False, limit=CHECK_LIMIT)
                    is not None for check in self.variants.get("check", ()))
                for x, y in self.tiles]

    @classmethod
    def load(cls, name: str = "anime_dice_screen.json") -> "ScreenProfile":
        import json
        import os
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), encoding="utf-8") as f:
            return cls(json.load(f))


# Windows side

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_CLICK_LOCK = threading.RLock()


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


def exclusive():
    """Holds every other client's clicks, for a step that grows one window over the others."""
    return _CLICK_LOCK


class WindowsScreen:
    """The real backend; tests pass a fake with the same methods."""

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

    def exclusive(self):
        return exclusive()
