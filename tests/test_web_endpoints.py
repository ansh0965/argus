"""Every GET endpoint answers 200 on a populated database.

The dashboard calls all of these; a 500 here is a blank card in the UI.
"""
import time

import pytest
from fastapi.testclient import TestClient

from argus import db
from argus.web import server


@pytest.fixture
def client(monkeypatch):
    now = time.time()
    db.add_activity("VS Code", "code.exe", "main.py - atlas", "code", "work",
                    now - 3600, now - 60, False, False)
    db.add_activity("Chrome", "chrome.exe", "YouTube - Google Chrome", "media", "unknown",
                    now - 7200, now - 3700, False, False)
    db.log_event("browser", "visit", "github.com", "atlas", ts=now - 100,
                 meta={"url": "https://github.com/x/atlas"})
    db.log_event("file", "modified", "main.py", "C:/atlas/main.py", ts=now - 50,
                 meta={"dir": "C:/atlas", "ext": ".py"})
    db.add_alert("disk_low", "Disk is filling up", "", "warn")
    db.add_run("maintenance", True, 5, "pruned")
    db.add_digest("morning", "Morning brief", "**The one thing** ship")
    from argus.sensors import git
    monkeypatch.setattr(git, "all_states", lambda max_age=300: [])
    from argus.gsuite import auth
    monkeypatch.setattr(auth, "labels", lambda: [])
    monkeypatch.setattr(auth, "is_configured", lambda: False)
    from argus.brain import threads
    monkeypatch.setattr(threads, "collect_claude", lambda since: [])
    return TestClient(server.app, base_url="http://127.0.0.1:7842")


@pytest.mark.parametrize("path", [
    "/", "/api/status", "/api/today", "/api/today?day=2026-09-21", "/api/hourly",
    "/api/trend", "/api/events", "/api/events?source=browser", "/api/now",
    "/api/alerts", "/api/digests", "/api/threads", "/api/repos", "/api/files",
    "/api/domains", "/api/processes", "/api/network", "/api/google", "/api/tasks",
    "/api/memory", "/api/runs", "/api/privacy", "/api/journal", "/api/search?q=atlas",
    "/api/doctor", "/api/timesheet", "/api/timesheet?week=-1", "/api/timesheet.csv",
])
def test_get_endpoints_answer(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.text[:300])


def test_digest_detail_and_journal(client):
    did = db.q1("SELECT id FROM digests")["id"]
    assert client.get(f"/api/digest/{did}").json()["title"] == "Morning brief"
    assert client.get("/api/digest/999999").status_code == 404
