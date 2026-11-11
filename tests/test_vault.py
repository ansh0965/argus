"""Backups are encrypted to the Windows account and can be restored."""
import sqlite3
import sys

import pytest

from argus import db, vault

pytestmark = pytest.mark.skipif(sys.platform != "win32" or not vault.available(),
                                reason="DPAPI is Windows-only")


def test_round_trip(tmp_path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"secret activity")
    vault.encrypt_file(src, tmp_path / "x.enc")
    assert b"secret activity" not in (tmp_path / "x.enc").read_bytes()
    vault.decrypt_file(tmp_path / "x.enc", tmp_path / "y.bin")
    assert (tmp_path / "y.bin").read_bytes() == b"secret activity"
    with pytest.raises(ValueError):
        vault.unprotect(b"plain")


def test_nightly_backup_is_encrypted_and_restorable(tmp_path):
    from argus.automations import builtin
    from argus.paths import BACKUPS
    db.add_activity("VS Code", "code.exe", "marker-title", "code", "work", 1, 2, False, False)
    out = builtin.backup(keep=2)
    assert ".db.enc" in out
    enc = sorted(BACKUPS.glob("argus-*.db.enc"))[-1]
    assert b"marker-title" not in enc.read_bytes()
    restored = tmp_path / "r.db"
    vault.decrypt_file(enc, restored)
    con = sqlite3.connect(restored)
    assert con.execute("SELECT COUNT(*) FROM activity WHERE title='marker-title'").fetchone()[0] == 1
    con.close()
