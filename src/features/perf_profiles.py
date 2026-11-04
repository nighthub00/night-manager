"""
features/perf_profiles.py
Performance settings as a share code (or file), to pass a working setup to another PC (after
RoALT's performance.rs, MIT). Only performance settings travel: never accounts, cookies,
webhooks, hotkeys or anything per account. An import is checked against the same ranges as
Settings, and the user sees what changes before it is applied.
"""

from __future__ import annotations

import base64
import json
import zlib

import features.settings_store as settings_store

PREFIX = "NMPERF1:"
# key -> (type, low, high, default)
FIELDS = {
    "launch_fps_cap": (int, 0, 360, 0),
    "launch_lowest_graphics": (bool, None, None, False),
    "limits_background_enabled": (bool, None, None, False),
    "limits_background_cpu": (bool, None, None, True),
    "limits_background_cpu_percent": (int, 0, 100, 0),
    "limits_background_efficiency": (bool, None, None, True),
    "limits_background_trim_minutes": (int, 0, 240, 0),
    "free_resize_enabled": (bool, None, None, False),
    "recovery_memory_limit_mb": (int, 0, 65536, 0),
    "window_grid_max_width": (int, 0, 7680, 0),
    "window_grid_max_height": (int, 0, 4320, 0),
}
LABELS = {
    "launch_fps_cap": "FPS cap for launched clients",
    "launch_lowest_graphics": "Lowest graphics for launched clients",
    "limits_background_enabled": "Limit background clients",
    "limits_background_cpu": "Cap background CPU",
    "limits_background_cpu_percent": "Background CPU cap (%)",
    "limits_background_efficiency": "Efficiency mode in the background",
    "limits_background_trim_minutes": "Trim background memory every (min)",
    "free_resize_enabled": "Free resizing",
    "recovery_memory_limit_mb": "Restart over memory (MB)",
    "window_grid_max_width": "Window Grid max tile width",
    "window_grid_max_height": "Window Grid max tile height",
}


def current() -> dict:
    settings = settings_store.load()
    return {key: settings.get(key, spec[3]) for key, spec in FIELDS.items()}


def to_code(profile: dict | None = None) -> str:
    raw = json.dumps(profile or current(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")


def from_code(code: str) -> dict:
    """The checked settings in a share code or exported file. Raises ValueError."""
    text = (code or "").strip()
    if text.startswith("{"):
        data = json.loads(text)
    else:
        if not text.startswith(PREFIX):
            raise ValueError("That isn't a NIGHT MANAGER performance code.")
        body = text[len(PREFIX):]
        try:
            raw = zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)), bufsize=4096)
        except Exception as exc:
            raise ValueError("That performance code is damaged.") from exc
        if len(raw) > 4096:
            raise ValueError("That performance code is too long.")
        data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("That performance code holds no settings.")
    checked = {}
    for key, value in data.items():
        spec = FIELDS.get(key)
        if spec is None:
            continue  # Anything that isn't a performance setting never comes in.
        kind, low, high, _ = spec
        if kind is bool:
            if not isinstance(value, bool):
                raise ValueError(f"'{LABELS[key]}' should be on or off.")
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"'{LABELS[key]}' should be a number from {low} to {high}.")
        checked[key] = value
    return checked


def changes(profile: dict) -> list[tuple[str, object, object]]:
    """(label, now, after) for every setting the profile would change."""
    now = current()
    return [(LABELS[key], now[key], value) for key, value in profile.items() if now.get(key) != value]


def apply(profile: dict) -> None:
    for key, value in profile.items():
        if key in FIELDS:
            settings_store.save(key, value)
