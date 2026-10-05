"""
Shared filesystem paths for source runs and PyInstaller builds.
"""

from __future__ import annotations

import os
import sys


def get_project_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.basename(source_root).casefold() == "src":
        return os.path.dirname(source_root)
    return source_root


def get_app_dir() -> str:
    return get_project_root()


def get_bundle_root() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", get_project_root())
    return get_project_root()


def get_resource_path(*parts: str) -> str:
    return os.path.join(get_bundle_root(), *parts)


def get_data_dir() -> str:
    return os.path.join(get_project_root(), "AccountManagerData")


def get_clean_child_env() -> dict[str, str]:
    """Environment for a process that may start NightManager.exe again.

    A onefile build's _PYI_* variables leak into every child. A NightManager.exe
    started further down that chain reads them, takes itself for this process's
    unpacked child, and stops with "Security validation failure: failed to obtain
    executable path for parent process". PYINSTALLER_RESET_ENVIRONMENT makes it
    start as a fresh instance instead.
    """
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("_PYI_") and key.upper() != "_MEIPASS2"
    }
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env
