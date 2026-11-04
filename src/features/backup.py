"""
features/backup.py
Encrypted account backups (.nmbackup): accounts with their cookies, notes and IDs, sealed with a
password (scrypt, then AES-256-GCM), to move them to another PC or keep them safe. Unlike the
hardware encryption of the saved accounts, a backup opens anywhere with its password.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import time

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt

from classes.operation_result import OperationResult
from utils.atomic_file import write_json_atomic

FORMAT = "nightmanager-backup"
EXTENSION = ".nmbackup"
_N, _R, _P = 2 ** 15, 8, 1
_MIN_PASSWORD = 8
# Bound to the file's header, so it can't be swapped onto another backup's data.
_AAD = b"NIGHT MANAGER backup v1"


def _key(password: str, salt: bytes) -> bytes:
    return scrypt(password.encode("utf-8"), salt, 32, N=_N, r=_R, p=_P)


def export(path: str, accounts: dict, password: str) -> OperationResult:
    if len(password) < _MIN_PASSWORD:
        return OperationResult.failure(
            "BACKUP_PASSWORD_SHORT", "Password Too Short",
            f"Use at least {_MIN_PASSWORD} characters: the backup holds your sign-ins.",
        )
    salt = os.urandom(16)
    cipher = AES.new(_key(password, salt), AES.MODE_GCM)
    cipher.update(_AAD)
    plain = json.dumps({"accounts": copy.deepcopy(accounts), "exported_at": time.time()}).encode("utf-8")
    data, tag = cipher.encrypt_and_digest(plain)
    encode = lambda raw: base64.b64encode(raw).decode("ascii")
    try:
        write_json_atomic(path, {
            "format": FORMAT, "v": 1, "kdf": "scrypt", "n": _N, "r": _R, "p": _P,
            "salt": encode(salt), "nonce": encode(cipher.nonce), "tag": encode(tag), "data": encode(data),
        })
    except OSError as exc:
        return OperationResult.failure(
            "BACKUP_WRITE_FAILED", "Backup Not Saved", "The backup file couldn't be written.", detail=str(exc),
        )
    return OperationResult.success(f"Backed up {len(accounts)} account(s).")


def read(path: str, password: str) -> OperationResult:
    """The accounts in a backup (data: {username: account})."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            sealed = json.load(handle)
        if sealed.get("format") != FORMAT or sealed.get("v") != 1:
            raise ValueError("not a NIGHT MANAGER backup")
        decode = lambda key: base64.b64decode(sealed[key])
        n, r, p = int(sealed["n"]), int(sealed["r"]), int(sealed["p"])
        if not (2 ** 14 <= n <= 2 ** 20 and 1 <= r <= 16 and 1 <= p <= 4):
            raise ValueError("unusual key settings")
        key = scrypt(password.encode("utf-8"), decode("salt"), 32, N=n, r=r, p=p)
        cipher = AES.new(key, AES.MODE_GCM, nonce=decode("nonce"))
        cipher.update(_AAD)
        plain = cipher.decrypt_and_verify(decode("data"), decode("tag"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        wrong = "MAC check failed" in str(exc)
        return OperationResult.failure(
            "BACKUP_WRONG_PASSWORD" if wrong else "BACKUP_UNREADABLE",
            "Wrong Password" if wrong else "Backup Unreadable",
            "That password doesn't open this backup." if wrong else "That file isn't a readable NIGHT MANAGER backup.",
            detail="" if wrong else f"{type(exc).__name__}: {exc}",
        )
    accounts = json.loads(plain.decode("utf-8")).get("accounts") or {}
    accounts = {str(name): data for name, data in accounts.items() if isinstance(data, dict) and data.get("cookie")}
    return OperationResult.success(data=accounts)


def merge(manager, accounts: dict, replace: bool = False) -> tuple[int, int]:
    """Adds the backup's accounts; existing ones are kept unless `replace`. (added, skipped)"""
    added = skipped = 0
    with manager._accounts_lock:
        for username, data in accounts.items():
            if username in manager.accounts and not replace:
                skipped += 1
                continue
            manager.accounts[username] = copy.deepcopy(data)
            added += 1
        if added:
            manager.save_accounts()
    return added, skipped
