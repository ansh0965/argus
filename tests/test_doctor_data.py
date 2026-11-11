"""Doctor: a source that silently stopped producing data is called out."""
import os
import time
from pathlib import Path

from argus import db, doctor


def _isolate(monkeypatch, tmp_path, idle=10.0, repo_commit=0.0):
    from argus.sensors import browser
    from argus.sensors import git as gitsensor
    from argus.sensors import window
    monkeypatch.setattr(window, "idle_seconds", lambda: idle)
    monkeypatch.setattr(gitsensor, "all_states", lambda max_age=0: [{"last_commit_ts": repo_commit}])
    monkeypatch.setattr(browser, "profiles", lambda: [])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def _names(checks):
    return {c["name"]: c["status"] for c in checks}


def test_all_quiet_is_ok(monkeypatch, tmp_path):
    now = time.time()
    db.add_activity("VS Code", "code.exe", "x", "code", "work", now - 120, now - 30, False, False)
    _isolate(monkeypatch, tmp_path)
    assert _names(doctor.check_data(now)) == {"data": "ok"}


def test_active_but_nothing_recorded(monkeypatch, tmp_path):
    now = time.time()
    db.add_activity("VS Code", "code.exe", "x", "code", "work", now - 3600, now - 3000, False, False)
    _isolate(monkeypatch, tmp_path, idle=5)
    assert _names(doctor.check_data(now))["data: windows"] == "fail"


def test_commits_and_transcripts_not_recorded(monkeypatch, tmp_path):
    now = time.time()
    db.add_activity("VS Code", "code.exe", "x", "code", "work", now - 120, now - 30, False, False)
    _isolate(monkeypatch, tmp_path, idle=999, repo_commit=now - 600)
    d = tmp_path / ".claude" / "projects" / "p"
    d.mkdir(parents=True)
    f = d / "s.jsonl"
    f.write_text("{}")
    os.utime(f, (now - 60, now - 60))
    got = _names(doctor.check_data(now))
    assert got["data: git"] == "warn" and got["data: claude"] == "warn"
