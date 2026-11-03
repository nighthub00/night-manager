"""
Crash-safe file writes for account and settings data.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _replace_with_retry(source: str, destination: str, attempts: int = 10) -> None:
    # Antivirus and sync tools briefly hold freshly written files open on Windows.
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)


def _overwrite_keeping_backup(source: str, destination: str) -> None:
    backup = destination + ".bak"
    has_backup = os.path.exists(destination)
    if has_backup:
        shutil.copy2(destination, backup)
    try:
        shutil.copyfile(source, destination)
    except BaseException:
        if has_backup:
            try:
                shutil.copyfile(backup, destination)
            except OSError:
                pass
        raise
    if has_backup:
        _remove_quietly(backup)


def write_text_atomic(path: str, text: str) -> None:
    """Replace path with text so a crash or failed write never leaves it half written."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=folder,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _remove_quietly(temp_path)
        raise

    try:
        _replace_with_retry(temp_path, path)
    except OSError as exc:
        # Something holds the target open without delete sharing. Overwrite it in
        # place, keeping a copy so a failed write can't destroy what was there.
        print(f"[WARNING] Atomic replace of {os.path.basename(path)} failed ({exc}); writing in place.")
        try:
            _overwrite_keeping_backup(temp_path, path)
        finally:
            _remove_quietly(temp_path)


def write_json_atomic(path: str, data, indent: int | None = 2) -> None:
    # Serialize first, so an unserializable value never touches the file.
    write_text_atomic(path, json.dumps(data, indent=indent, ensure_ascii=False))
