"""
Sign the release exe for NIGHT MANAGER's updater (see features/updater.py).

Writes dist/NightManager-vX.Y.Z.exe.sig: an ECDSA P-256 / SHA-256 signature (DER) over
"NightManager-vX.Y.Z.exe\n" followed by the exe's bytes. The private key comes from the
NM_RELEASE_SIGNING_KEY environment variable (a GitHub Actions secret, PEM text); without it
the release stops, because builds from 1.1.6 on refuse an unsigned update.
"""

from __future__ import annotations

import glob
import os
import sys

from Crypto.Hash import SHA256
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from features.updater import RELEASE_PUBLIC_KEY  # noqa: E402


def main() -> int:
    pem = os.environ.get("NM_RELEASE_SIGNING_KEY", "").strip()
    if not pem:
        print("NM_RELEASE_SIGNING_KEY is not set: refusing to publish an unsigned release.")
        return 1
    key = ECC.import_key(pem)
    if key.public_key().export_key(format="PEM").strip() != RELEASE_PUBLIC_KEY.strip():
        print("The signing key doesn't match the public key built into the app.")
        return 1
    assets = glob.glob(os.path.join("dist", "NightManager-v*.exe"))
    if len(assets) != 1:
        print(f"Expected one release exe in dist, found {len(assets)}.")
        return 1
    path = assets[0]
    name = os.path.basename(path)
    digest = SHA256.new(f"{name}\n".encode("utf-8"))
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    signature = DSS.new(key, "fips-186-3", encoding="der").sign(digest)
    with open(path + ".sig", "wb") as handle:
        handle.write(signature)
    print(f"Signed {name} ({len(signature)} byte signature)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
