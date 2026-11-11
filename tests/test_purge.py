"""Deleting data must delete it - rows, files, and the free pages they left."""
import time

import pytest
from fastapi.testclient import TestClient

from argus import db, paths
from argus.web import server


@pytest.fixture
def client():
    return TestClient(server.app, base_url="http://127.0.0.1:7842")


def _seed(day="2026-09-20"):
    ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + 3600
    db.add_activity("Chrome", "chrome.exe", "secret page", "browse", "unknown",
                    ts, ts + 60, False, False)
    db.log_event("browser", "visit", "example.com", "secret page", ts=ts)
    db.add_digest("evening", "wrap", "body")
    (paths.JOURNAL / f"{day}.md").write_text("# wrap\nraw facts", encoding="utf-8")
    shots = paths.SHOTS / day
    shots.mkdir(parents=True, exist_ok=True)
    (shots / "120000_Chrome.jpg").write_bytes(b"jpg")
    db.remember("preference", "no toasts before 10")
    db.set_state("browser_watermark:Chrome", ts)
    db.set_state("git_states", {"ts": ts, "states": []})


def test_delete_screenshots_actually_works(client):
    _seed()
    r = client.post("/api/privacy/purge", json={"scope": "screenshots"})
    assert r.status_code == 200, r.text          # was a 400 every time
    assert not any(paths.SHOTS.rglob("*.jpg"))


def test_delete_everything_takes_the_journal_and_screenshots_too(client):
    _seed()
    r = client.post("/api/privacy/purge", json={"scope": "all"}).json()
    assert r["files"] >= 2
    assert db.q1("SELECT COUNT(*) n FROM activity")["n"] == 0
    assert db.q1("SELECT COUNT(*) n FROM events WHERE source != 'argus'")["n"] == 0
    assert not list(paths.JOURNAL.glob("*.md"))
    assert not any(paths.SHOTS.rglob("*.jpg"))
    assert db.get_state("git_states") is None
    # kept: what you taught it, and watermarks (or history is re-ingested)
    assert db.recall("preference")
    assert db.get_state("browser_watermark:Chrome")


def test_delete_a_day(client):
    _seed("2026-09-20")
    _seed("2026-09-21")
    r = client.post("/api/privacy/purge", json={"scope": "day", "day": "2026-09-20"}).json()
    assert r["rows"] >= 2 and r["files"] >= 2
    assert db.q1("SELECT COUNT(*) n FROM activity WHERE day='2026-09-20'")["n"] == 0
    assert db.q1("SELECT COUNT(*) n FROM activity WHERE day='2026-09-21'")["n"] == 1
    assert (paths.JOURNAL / "2026-09-21.md").exists()
    assert not (paths.JOURNAL / "2026-09-20.md").exists()


def test_bad_scope_or_day_is_refused(client):
    assert client.post("/api/privacy/purge", json={"scope": "nope"}).status_code == 400
    assert client.post("/api/privacy/purge",
                       json={"scope": "day", "day": "../../x"}).status_code == 400


def test_privacy_page_counts_every_table(client):
    import sqlite3
    tables = {r[0] for r in db.q("SELECT name FROM sqlite_master WHERE type='table' "
                                 "AND name NOT LIKE 'sqlite_%'")}
    shown = set(client.get("/api/privacy").json()["rows"])
    # state is key-value bookkeeping, not observations
    assert tables - shown <= {"state"}, tables - shown
