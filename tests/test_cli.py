"""Every read-only CLI command runs on a populated database without crashing.

`argus alerts` raised KeyError on every call for as long as it existed; a
command nobody's test exercised could stay broken indefinitely.
"""
import time

import pytest

from argus import cli, db


@pytest.fixture
def populated(monkeypatch):
    now = time.time()
    db.add_activity("VS Code", "code.exe", "main.py - argus", "code", "work",
                    now - 3600, now - 60, False, False)
    db.add_alert("disk_low", "Disk is filling up", "8 GB free", "warn")
    db.add_run("heartbeat", True, 1, "ok")
    db.log_event("git", "commit", "argus", "fix things", ts=now - 1000)
    db.add_digest("morning", "Morning brief", "**The one thing** ship it")
    # nothing here may reach the network or a real repo
    from argus.sensors import git
    monkeypatch.setattr(git, "all_states", lambda max_age=300: [])
    from argus.gsuite import auth
    monkeypatch.setattr(auth, "labels", lambda: [])
    monkeypatch.setattr(auth, "is_configured", lambda: False)
    from argus import daemon
    monkeypatch.setattr(daemon, "daemon_alive", lambda: False)
    from argus.brain import threads          # never read the real ~/.claude
    monkeypatch.setattr(threads, "collect_claude", lambda since: [])


@pytest.mark.parametrize("argv", [
    ["status"], ["today"], ["yesterday"], ["day", "2026-09-21"],
    ["alerts"], ["alerts", "-v", "--unread"], ["runs"], ["runs", "--all"],
    ["repos"], ["memory"], ["mail"], ["cal"], ["task", "list"],
    ["google", "list"], ["timesheet"], ["timesheet", "--days", "3"],
    ["timesheet", "--csv", "-"], ["resume"], ["config"],
])
def test_read_only_commands_run(populated, argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr().out
    assert rc in (0, None), out
    assert "Traceback" not in out
