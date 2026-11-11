"""The dashboard API must be reachable only from this machine's own pages."""
import pytest
from fastapi.testclient import TestClient

from argus.web import server

BASE = "http://127.0.0.1:7842"


@pytest.fixture
def client():
    return TestClient(server.app, base_url=BASE)


def test_policy_accepts_the_dashboard_and_the_cli():
    ok, _ = server.request_allowed("GET", {"Host": "127.0.0.1:7842"})
    assert ok
    ok, _ = server.request_allowed("POST", {"Host": "localhost:7842",
                                            "Origin": "http://localhost:7842"})
    assert ok
    # httpx / curl from the CLI send no Origin and no Sec-Fetch-Site
    ok, _ = server.request_allowed("POST", {"Host": "127.0.0.1:7842"})
    assert ok


@pytest.mark.parametrize("host", ["evil.example:7842", "evil.example", "127.0.0.1:9999",
                                  "10.0.0.5:7842", ""])
def test_policy_rejects_foreign_hosts(host):
    ok, why = server.request_allowed("GET", {"Host": host})
    assert not ok and "host" in why


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:5173",
                                    "null", "http://localhost"])
def test_policy_rejects_cross_origin_writes(origin):
    ok, _ = server.request_allowed("POST", {"Host": "127.0.0.1:7842", "Origin": origin})
    assert not ok


def test_policy_rejects_cross_site_fetch_metadata_without_origin():
    ok, _ = server.request_allowed("POST", {"Host": "127.0.0.1:7842",
                                            "Sec-Fetch-Site": "cross-site"})
    assert not ok


def test_dns_rebinding_cannot_read_data(client):
    r = client.get("/api/privacy", headers={"Host": "attacker.example:7842"})
    assert r.status_code == 403


def test_cross_site_post_cannot_pause(client):
    from argus.actions import tray
    r = client.post("/api/pause", json={"minutes": 60},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not tray.is_paused()


def test_same_origin_post_still_works(client):
    from argus.actions import tray
    r = client.post("/api/pause", json={"minutes": 5}, headers={"Origin": BASE})
    assert r.status_code == 200
    assert tray.is_paused()
    tray.resume()


def test_dashboard_is_not_framable(client):
    r = client.get("/api/runs")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_runs_api_hides_noop_runs(client):
    from argus import db
    db.add_run("heartbeat", True, 1, "ok, 8/8 sensors alive")
    db.add_run("meeting_sentinel", True, 1, "nothing due")
    db.add_run("meeting_sentinel", True, 1, "Standup@10m")
    db.add_run("heartbeat", False, 1, "", "boom")
    names = [(r["name"], r["summary"]) for r in client.get("/api/runs").json()]
    assert ("meeting_sentinel", "Standup@10m") in names
    assert ("heartbeat", "") in names, "failures always show"
    assert len(names) == 2
    assert len(client.get("/api/runs?all=true").json()) == 4


def test_dashboard_login_rejects_unknown_labels(client):
    assert client.post("/api/google/login/Bad Label!").status_code in (400, 404)
    assert client.post("/api/google/login/nosuch").status_code == 404


def test_journal_day_must_be_a_date(client):
    assert client.get("/api/journal/..%5C..%5CREADME").status_code == 404
    assert client.get("/api/journal/2026-09-99").status_code == 404


def test_a_failing_route_is_logged(client, monkeypatch, caplog):
    from argus.web import server as srv
    monkeypatch.setattr(srv.procsensor, "top_consumers",
                        lambda n: (_ for _ in ()).throw(RuntimeError("boom")))
    c = TestClient(srv.app, base_url=BASE, raise_server_exceptions=False)
    import logging
    with caplog.at_level(logging.ERROR, logger="argus.web"):
        r = c.get("/api/processes")
    assert r.status_code == 500 and "boom" in r.json()["error"]
    assert any("/api/processes failed" in rec.getMessage() for rec in caplog.records)


def test_the_page_is_revalidated_so_upgrades_show_up(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
