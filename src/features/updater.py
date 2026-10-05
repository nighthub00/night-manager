"""
features/updater.py
Core logic of update checker.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

import psutil
import requests

from utils.app_paths import get_clean_child_env, get_data_dir
from utils.version import APP_NAME

# Updates come only from NIGHT MANAGER's own releases. Pointing this at the
# upstream Evanovar RAM repository would replace this build with theirs.
UPDATE_REPOSITORY = "nighthub00/night-manager"
GITHUB_API = f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{UPDATE_REPOSITORY}/releases/latest"
RELEASE_ASSET_PATTERN = re.compile(
    r"^NightManager-v\d+\.\d+\.\d+(?:\.\d+)?\.exe$",
    re.IGNORECASE,
)
PROCESS_WAIT_SECONDS = 120
REPLACE_WAIT_SECONDS = 30

def _clean(version: str) -> str:
    """Strip alpha/beta suffixes so we compare only numeric parts."""
    return re.sub(r"(alpha|beta).*$", "", version, flags=re.IGNORECASE).strip(" .")


def _parts(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in _clean(version).split("."))
    except ValueError:
        return (0,)


def is_newer(current: str, latest: str) -> bool:
    return _parts(latest.lstrip("v")) > _parts(current.lstrip("v"))

def check_latest_version() -> str | None:
    try:
        response = requests.get(GITHUB_API, timeout=8)
        if response.status_code == 200:
            tag = response.json().get("tag_name", "").lstrip("v")
            return tag or None
        print(f"[INFO] GitHub API status {response.status_code}")
        return None
    except Exception as exc:
        print(f"[ERROR] check_latest_version error: {exc}")
        return None


def get_exe_download_url() -> tuple[str, str] | None:
    try:
        response = requests.get(GITHUB_API, timeout=8)
        response.raise_for_status()
        release = response.json()
        assets = [
            asset
            for asset in release.get("assets", [])
            if str(asset.get("name", "")).lower().endswith(".exe")
            and asset.get("browser_download_url")
        ]
        release_tag = str(release.get("tag_name", "")).strip()
        if release_tag and not release_tag.lower().startswith("v"):
            release_tag = f"v{release_tag}"
        expected_name = f"NightManager-{release_tag}.exe" if release_tag else ""
        preferred = next(
            (
                asset
                for asset in assets
                if expected_name
                and asset["name"].lower() == expected_name.lower()
            ),
            None,
        )
        selected = preferred or next(
            (
                asset
                for asset in assets
                if RELEASE_ASSET_PATTERN.fullmatch(asset["name"])
            ),
            None,
        )
        if not selected:
            return None
        return selected["browser_download_url"], selected["name"]
    except Exception as exc:
        print(f"[ERROR] get_exe_download_url error: {exc}")
        return None


def read_product_name(path: str) -> str:
    """Return the ProductName stored in an executable's version resource."""
    try:
        import win32api

        translations = win32api.GetFileVersionInfo(path, r"\VarFileInfo\Translation")
        for language, codepage in translations or ():
            name = win32api.GetFileVersionInfo(
                path,
                rf"\StringFileInfo\{language:04x}{codepage:04x}\ProductName",
            )
            if name:
                return str(name).strip()
    except Exception:
        pass
    return ""


def get_update_target() -> str | None:
    if not getattr(sys, "frozen", False):
        return None
    target = os.path.abspath(sys.executable)
    if not os.path.isfile(target):
        return None
    return target


def _build_update_log_path() -> str:
    log_dir = os.path.join(get_data_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(log_dir, f"update-{stamp}.log")


def _build_installer_script() -> str:
    return f'''param(
    [Parameter(Mandatory=$true)][int]$TargetProcessId,
    [int]$LauncherProcessId = 0,
    [Parameter(Mandatory=$true)][string]$SourcePath,
    [Parameter(Mandatory=$true)][string]$DestinationPath,
    [Parameter(Mandatory=$true)][string]$LogPath,
    [Parameter(Mandatory=$true)][string]$UpdateDirectory,
    [switch]$Relaunch
)

$ErrorActionPreference = "Stop"

function Write-UpdateFailure([string]$Message) {{
    try {{
        $parent = Split-Path -Parent $LogPath
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        $Message | Set-Content -LiteralPath $LogPath -Encoding UTF8
    }} catch {{
    }}
}}

try {{
    $exitDeadline = [DateTime]::UtcNow.AddSeconds({PROCESS_WAIT_SECONDS})
    while ((Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue) -or
           ($LauncherProcessId -and (Get-Process -Id $LauncherProcessId -ErrorAction SilentlyContinue))) {{
        if ([DateTime]::UtcNow -ge $exitDeadline) {{
            throw "The running application did not exit within {PROCESS_WAIT_SECONDS} seconds."
        }}
        Start-Sleep -Milliseconds 500
    }}

    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {{
        throw "The downloaded update file is missing."
    }}

    $replaceDeadline = [DateTime]::UtcNow.AddSeconds({REPLACE_WAIT_SECONDS})
    $installed = $false
    while (-not $installed) {{
        try {{
            Copy-Item -LiteralPath $SourcePath -Destination $DestinationPath -Force
            $sourceLength = (Get-Item -LiteralPath $SourcePath).Length
            $destinationLength = (Get-Item -LiteralPath $DestinationPath).Length
            if ($sourceLength -ne $destinationLength) {{
                throw "The installed executable size does not match the download."
            }}
            $installed = $true
        }} catch {{
            if ([DateTime]::UtcNow -ge $replaceDeadline) {{
                throw
            }}
            Start-Sleep -Milliseconds 500
        }}
    }}

    Remove-Item -LiteralPath $SourcePath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $UpdateDirectory -Force -ErrorAction SilentlyContinue
    if ($Relaunch) {{
        Start-Process -FilePath $DestinationPath -WorkingDirectory (Split-Path -Parent $DestinationPath)
    }}
    exit 0
}} catch {{
    $detail = "NIGHT MANAGER automatic update failed.`r`n"
    $detail += "Timestamp: $([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))`r`n"
    $detail += "Destination: $DestinationPath`r`n"
    $detail += "Error: $($_.Exception.Message)"
    Write-UpdateFailure $detail
    # Bring the current version back so a failed update never leaves the user with nothing open
    if ($Relaunch -and (Test-Path -LiteralPath $DestinationPath -PathType Leaf)) {{
        Start-Process -FilePath $DestinationPath -WorkingDirectory (Split-Path -Parent $DestinationPath)
    }}
    exit 1
}}
'''


def _onefile_launcher_pid() -> int:
    # A onefile build runs as two processes; the outer bootloader keeps the exe
    # open until it has cleaned up after this one, so the installer waits for both.
    try:
        parent = psutil.Process(os.getpid()).parent()
        if parent is not None and os.path.normcase(parent.exe()) == os.path.normcase(sys.executable):
            return parent.pid
    except Exception:
        pass
    return 0


def _launch_installer(
    source_path: str,
    destination_path: str,
    update_directory: str,
    relaunch: bool = False,
) -> None:
    script_path = os.path.join(update_directory, "install_update.ps1")
    log_path = _build_update_log_path()
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write(_build_installer_script())

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script_path,
            "-TargetProcessId",
            str(os.getpid()),
            "-LauncherProcessId",
            str(_onefile_launcher_pid()),
            "-SourcePath",
            source_path,
            "-DestinationPath",
            destination_path,
            "-LogPath",
            log_path,
            "-UpdateDirectory",
            update_directory,
            *(["-Relaunch"] if relaunch else []),
        ],
        shell=False,
        creationflags=creation_flags,
        env=get_clean_child_env(), # the script relaunches NightManager.exe
    )


_NOT_FROZEN_MESSAGE = (
    "Automatic updates are only available in the compiled application. "
    "Use Manual Download when running from source."
)


def _download_release(on_progress: Callable[[int], None]) -> dict:
    """Download and verify the latest release exe. Returns the staged update or raises."""
    result = get_exe_download_url()
    if not result:
        raise RuntimeError(f"No {APP_NAME} executable was found in the latest release.")

    url, filename = result
    print(f"[INFO] Downloading {filename} from {url}")
    on_progress(2)

    update_directory = tempfile.mkdtemp(prefix="night_manager_update_")
    source_path = os.path.join(update_directory, "update.exe")
    try:
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        downloaded = 0

        with open(source_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    on_progress(int(2 + (downloaded / total) * 95))

        if not os.path.isfile(source_path) or os.path.getsize(source_path) == 0:
            raise RuntimeError("The downloaded update file is empty.")
        if total > 0 and os.path.getsize(source_path) != total:
            raise RuntimeError("The download was cut off before it finished.")

        product_name = read_product_name(source_path)
        if product_name != APP_NAME:
            raise RuntimeError(
                f"The downloaded file is not a {APP_NAME} build "
                f"(product name: {product_name or 'missing'}). The update was refused."
            )
    except Exception:
        shutil.rmtree(update_directory, ignore_errors=True)
        raise
    return {"source": source_path, "directory": update_directory, "filename": filename}


def install_staged_update(staged: dict, relaunch: bool) -> bool:
    """Hand a downloaded update to the installer; it swaps the exe once this process exits."""
    target = get_update_target()
    if not target or not staged or not os.path.isfile(staged.get("source", "")):
        return False
    try:
        _launch_installer(staged["source"], target, staged["directory"], relaunch=relaunch)
    except Exception as exc:
        print(f"[ERROR] Could not start the update installer: {exc}")
        return False
    print(f"[SUCCESS] Installing {staged.get('filename', 'update')} over {target}")
    return True


def discard_staged_update(staged: dict | None) -> None:
    if staged and staged.get("directory"):
        shutil.rmtree(staged["directory"], ignore_errors=True)


def stage_update(on_done: Callable[[bool, str, object], None]) -> None:
    """Download the latest release quietly in the background. on_done(ok, error, staged)."""
    def _run():
        if not get_update_target():
            on_done(False, _NOT_FROZEN_MESSAGE, None)
            return
        try:
            staged = _download_release(lambda _pct: None)
        except Exception as exc:
            print(f"[ERROR] Background update download failed: {type(exc).__name__}: {exc}")
            on_done(False, str(exc), None)
            return
        on_done(True, "", staged)

    threading.Thread(target=_run, daemon=True, name="UpdaterStage").start()


def download_update(
    on_progress: Callable[[int], None],
    on_done: Callable[[bool, str], None],
    relaunch: bool = True,
) -> None:
    def _run():
        try:
            if not get_update_target():
                on_done(False, _NOT_FROZEN_MESSAGE)
                return
            on_progress(0)
            staged = _download_release(on_progress)
            if not install_staged_update(staged, relaunch=relaunch):
                discard_staged_update(staged)
                on_done(False, "The update installer could not be started.")
                return
            on_progress(100)
            on_done(True, "")
        except Exception as exc:
            print(f"[ERROR] download_update error: {type(exc).__name__}: {exc}")
            on_done(False, str(exc))

    threading.Thread(target=_run, daemon=True, name="UpdaterDownload").start()
