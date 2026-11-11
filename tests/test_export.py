import csv
import io
import json
from datetime import datetime

from argus import cli, db, export


def _seed():
    t = datetime(2026, 9, 21, 10, 0).timestamp()
    db.add_activity("VS Code", "code.exe", "main.py", "code", "work", t, t + 600, False, False)
    db.add_activity("KeePassXC", "keepassxc.exe", "<private>", "other", "unknown",
                    t + 600, t + 700, False, True)
    db.add_activity("Away", "<idle>", "Idle", "idle", "unknown", t + 700, t + 900, True, False)
    db.log_event("git", "commit", "atlas", "fix", ts=t + 50, meta={"sha": "abc"})
    db.add_digest("morning", "Morning brief", "ship it")


def test_csv_has_sessions_without_away_by_default():
    _seed()
    rows = list(csv.DictReader(io.StringIO(export.activity_csv("2026-09-21", "2026-09-21"))))
    assert [r["app"] for r in rows] == ["VS Code", "KeePassXC"]
    assert rows[1]["title"] == "<private>", "exports never un-redact"
    assert rows[0]["started"] == "2026-09-21T10:00:00" and rows[0]["seconds"] == "600"
    with_afk = export.activity_csv("2026-09-21", "2026-09-21", include_afk=True)
    assert "Away" in with_afk


def test_json_has_everything_in_range():
    _seed()
    doc = json.loads(export.everything_json("2026-09-21", "2026-09-21"))
    assert len(doc["activity"]) == 3
    assert doc["events"][0]["meta"] == {"sha": "abc"}
    assert doc["from"] == doc["to"] == "2026-09-21"
    assert json.loads(export.everything_json("2026-09-22", "2026-09-22"))["activity"] == []


def test_cli_export_to_stdout(capsys):
    _seed()
    assert cli.main(["export", "--from", "2026-09-21", "--to", "2026-09-21", "--out", "-"]) == 0
    assert capsys.readouterr().out.startswith("day,app,exe,title")
