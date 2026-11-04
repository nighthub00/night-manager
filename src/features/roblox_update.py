"""
features/roblox_update.py
Is the installed Roblox current, and updating it with Roblox's own installer (after RoALT's
launcher/update.rs + authenticode.rs, MIT).

- Check: clientsettingscdn's current WindowsPlayer version against the Versions folder.
- Install: refused while any client runs (an update ends them all). Downloads
  RobloxPlayerInstaller.exe for that version from setup.rbxcdn.com into a fresh temp folder,
  runs it only when Windows says its Authenticode signature is valid and signed by
  Roblox Corporation, waits for it (at most 10 minutes) and deletes the folder.
Bootstrappers (Bloxstrap and the like) keep Roblox up to date themselves and are left alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

from classes.operation_result import OperationResult
import features.presence as presence_mod

# The CDN host first; some networks only reach the origin.
_VERSION_URLS = (
    "https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer",
    "https://clientsettings.roblox.com/v2/client-version/WindowsPlayer",
)
_INSTALLER_URL = "https://setup.rbxcdn.com/{version}-RobloxPlayerInstaller.exe"
_MAX_INSTALLER_BYTES = 50 * 1024 * 1024
_SIGNER = "Roblox Corporation"
# Launchers that bring their own Roblox and keep it up to date themselves.
_BOOTSTRAPPERS = ("bloxstrap", "fishstrap", "froststrap", "voidstrap", "custom")


def versions_dir() -> Path:
    return Path(os.getenv("LOCALAPPDATA", "")) / "Roblox" / "Versions"


def installed_versions() -> list[str]:
    root = versions_dir()
    try:
        return sorted(
            folder.name for folder in root.iterdir()
            if folder.name.startswith("version-") and (folder / "RobloxPlayerBeta.exe").is_file()
        )
    except OSError:
        return []


def latest_version() -> dict:
    last_error: Exception | None = None
    for url in _VERSION_URLS:
        try:
            response = requests.get(url, timeout=8)
            response.raise_for_status()
            data = response.json()
            if data.get("clientVersionUpload"):
                return data
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    raise requests.RequestException(f"no version host answered ({last_error})")


def check(launcher: str = "default") -> OperationResult:
    """{"latest", "upload", "installed", "current", "managed"} for the Settings page.
    `managed`: a bootstrapper or custom launcher brings its own Roblox."""
    try:
        data = latest_version()
    except requests.RequestException as exc:
        return OperationResult.failure(
            "ROBLOX_VERSION_CHECK_FAILED", "Roblox Version Unknown",
            "Roblox's current version couldn't be checked.", detail=str(exc),
            retryable=True,
        )
    upload = str(data.get("clientVersionUpload") or "")
    installed = installed_versions()
    return OperationResult.success(data={
        "latest": str(data.get("version") or ""),
        "upload": upload,
        "installed": installed,
        "current": upload in installed,
        "managed": launcher in _BOOTSTRAPPERS,
    })


def signature_ok(path: str) -> tuple[bool, str]:
    """Windows' own Authenticode check (chain to a trusted root, file unmodified), and the
    signer must be Roblox Corporation."""
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:NM_CHECK_FILE; "
        "$c = $s.SignerCertificate; "
        "if ($c) { $n = $c.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) } else { $n = '' }; "
        "Write-Output ($s.Status.ToString() + '|' + $n)"
    )
    env = dict(os.environ, NM_CHECK_FILE=path)
    try:
        output = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"The signature couldn't be checked: {exc}"
    status, _, signer = output.partition("|")
    if status != "Valid":
        return False, f"Windows says the installer's signature is {status or 'missing'}."
    if signer != _SIGNER:
        return False, f"The installer is signed by '{signer}', not {_SIGNER}."
    return True, ""


def install(upload: str, on_progress=lambda text: None) -> OperationResult:
    if not upload.startswith("version-") or not upload[8:].isalnum():
        return OperationResult.failure("ROBLOX_VERSION_INVALID", "Invalid Version", "Roblox gave an unexpected version.")
    if presence_mod.get_roblox_processes(force=True):
        return OperationResult.failure(
            "ROBLOX_RUNNING", "Close Roblox First",
            "Updating Roblox closes every client. Close them, then update.",
        )
    folder = tempfile.mkdtemp(prefix="night_manager_roblox_")
    installer = os.path.join(folder, "RobloxPlayerInstaller.exe")
    try:
        on_progress("Downloading Roblox's installer...")
        with requests.get(_INSTALLER_URL.format(version=upload), stream=True, timeout=30) as response:
            response.raise_for_status()
            size = 0
            with open(installer, "wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    size += len(chunk)
                    if size > _MAX_INSTALLER_BYTES:
                        raise ValueError("the installer is larger than expected")
                    handle.write(chunk)
        on_progress("Checking the installer's signature...")
        ok, why = signature_ok(installer)
        if not ok:
            return OperationResult.failure("ROBLOX_INSTALLER_UNTRUSTED", "Installer Not Trusted", why)
        on_progress("Installing Roblox...")
        process = subprocess.Popen([installer], cwd=folder)
        try:
            code = process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            return OperationResult.failure(
                "ROBLOX_INSTALL_TIMEOUT", "Roblox Install Still Running",
                "Roblox's installer hasn't finished after 10 minutes.",
            )
        if upload not in installed_versions():
            return OperationResult.failure(
                "ROBLOX_INSTALL_FAILED", "Roblox Didn't Update",
                "Roblox's installer finished, but the new version isn't installed.",
                detail=f"Installer exit code {code}",
            )
        return OperationResult.success(f"Roblox updated to {upload}.")
    except (requests.RequestException, OSError, ValueError) as exc:
        return OperationResult.failure(
            "ROBLOX_INSTALL_DOWNLOAD_FAILED", "Roblox Update Failed",
            "Roblox's installer couldn't be downloaded.", detail=f"{type(exc).__name__}: {exc}",
            retryable=True,
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)
