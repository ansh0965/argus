"""Encryption tied to your Windows account (DPAPI).

Data protected here can only be read back by the same Windows user on the
same machine - no key file to lose or leak. Used for the nightly database
backups, which are full copies of your activity history sitting in a
folder. (The live database relies on Windows Device encryption /
BitLocker; `argus doctor` says so.)
"""
from __future__ import annotations

from pathlib import Path

MAGIC = b"ARGUSDPAPI1\n"


def available() -> bool:
    try:
        import win32crypt  # noqa: F401
        return True
    except ImportError:
        return False


def protect(data: bytes) -> bytes:
    import win32crypt
    return MAGIC + win32crypt.CryptProtectData(data, "ARGUS backup", None, None, None, 0)


def unprotect(blob: bytes) -> bytes:
    import win32crypt
    if not blob.startswith(MAGIC):
        raise ValueError("not an ARGUS-encrypted file")
    return win32crypt.CryptUnprotectData(blob[len(MAGIC):], None, None, None, 0)[1]


def encrypt_file(src: Path, dest: Path) -> int:
    blob = protect(Path(src).read_bytes())
    Path(dest).write_bytes(blob)
    return len(blob)


def decrypt_file(src: Path, dest: Path) -> int:
    data = unprotect(Path(src).read_bytes())
    Path(dest).write_bytes(data)
    return len(data)
