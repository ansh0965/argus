"""Notification gating: cooldown, pause, quiet hours."""
import pytest

from argus import db
from argus.actions import notify


@pytest.fixture
def delivered(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_deliver",
                        lambda t, b, s, a, url="": sent.append(t) or "test")
    monkeypatch.setattr(notify, "is_quiet_hours", lambda *a: False)
    notify._last_sent.clear()
    return sent


def _alerts(rule):
    return db.q1("SELECT COUNT(*) n FROM alerts WHERE rule=?", (rule,))["n"]


def test_cooldown_suppresses_recording_not_just_the_toast(delivered):
    for _ in range(5):
        notify.send("Google sign-in needed", rule="tok", cooldown_seconds=3600)
    assert delivered == ["Google sign-in needed"]
    assert _alerts("tok") == 1


def test_cooldown_survives_a_restart(delivered):
    notify.send("x", rule="r", cooldown_seconds=3600)
    notify._last_sent.clear()                 # what a daemon restart does
    notify.send("x", rule="r", cooldown_seconds=3600)
    assert _alerts("r") == 1


def test_suppressed_alert_still_starts_the_cooldown(delivered, monkeypatch):
    # quiet hours swallow the toast; the repeat must not re-record every tick
    monkeypatch.setattr(notify, "is_quiet_hours", lambda *a: True)
    notify.send("x", rule="q", cooldown_seconds=3600)
    notify.send("x", rule="q", cooldown_seconds=3600)
    assert delivered == []
    assert _alerts("q") == 1


def test_force_bypasses_cooldown(delivered):
    notify.send("a", rule="f", cooldown_seconds=3600)
    notify.send("b", rule="f", cooldown_seconds=3600, force=True)
    assert delivered == ["a", "b"]


def test_expired_cooldown_fires_again(delivered):
    notify.send("a", rule="e", cooldown_seconds=60)
    with db.tx() as c:
        c.execute("UPDATE alerts SET ts = ts - 120 WHERE rule='e'")
    notify._last_sent.clear()
    notify.send("b", rule="e", cooldown_seconds=60)
    assert delivered == ["a", "b"]


def test_pause_blocks_all_but_urgent(delivered):
    from argus.actions import tray
    tray.pause(10)
    try:
        assert not notify.send("info", rule="p1")
        assert not notify.send("warn", severity="warn", rule="p2")
        assert notify.send("battery", severity="urgent", rule="p3")
    finally:
        tray.resume()
    assert delivered == ["battery"]


def test_notified_flag_set_only_on_delivery(delivered):
    notify.send("ok", rule="n")
    assert db.q1("SELECT notified FROM alerts WHERE rule='n'")["notified"] == 1


def test_clicking_a_toast_opens_something_safe(monkeypatch):
    urls = []
    monkeypatch.setattr(notify, "_deliver", lambda t, b, s, a, url="": urls.append(url) or "x")
    monkeypatch.setattr(notify, "is_quiet_hours", lambda *a: False)
    notify.send("meeting", url="https://meet.google.com/abc-defg-hij", rule="u1")
    notify.send("brief", url=notify.dashboard("ask"), rule="u2")
    notify.send("hostile invite", url="file:///C:/Windows/System32/calc.exe", rule="u3")
    notify.send("ms", url="ms-settings:privacy", rule="u4")
    notify.send("none", rule="u5")
    d = notify.dashboard()
    assert urls == ["https://meet.google.com/abc-defg-hij", d + "#ask", d, d, d]


def test_repeated_alerts_list_as_one_row_with_a_count():
    for i in range(30):
        db.add_alert("token_expired:work", "Google sign-in needed: work", "log in", "warn")
    db.add_alert("cpu", "CPU pinned", "chrome 90%", "warn")
    db.add_alert("token_expired:work", "Google sign-in needed: work", "log in", "warn")
    rows = notify.recent(10, collapse=True)
    assert [(r["title"], r["count"]) for r in rows] == [
        ("Google sign-in needed: work", 31), ("CPU pinned", 1)]
    assert rows[0]["first_ts"] <= rows[0]["ts"]
    assert len(notify.recent(10)) == 10, "the raw listing is unchanged"


def test_a_collapsed_row_is_unread_while_any_repeat_is():
    a = db.add_alert("r", "same", "", "info")
    db.add_alert("r", "same", "", "info")
    notify.ack(a)
    assert notify.recent(5, collapse=True)[0]["acked"] == 0
    notify.ack()
    assert notify.recent(5, collapse=True)[0]["acked"] == 1


def test_the_dashboard_lists_alerts_collapsed():
    from fastapi.testclient import TestClient

    from argus.web import server
    for _ in range(5):
        db.add_alert("dup", "same thing", "", "warn")
    c = TestClient(server.app, base_url="http://127.0.0.1:7842")
    rows = c.get("/api/alerts").json()
    assert len(rows) == 1 and rows[0]["count"] == 5
    assert len(c.get("/api/alerts?all=1").json()) == 5


@pytest.mark.parametrize("md, text", [
    ("**The one thing** — Push the atlas `feat/enterprise-v2` branch",
     "The one thing — Push the atlas feat/enterprise-v2 branch"),
    ("## Morning brief", "Morning brief"),
    ("- *fix* [the PR](https://x/1) today", "fix the PR today"),
    ("1. update_pages.py and __init__ stay intact", "update_pages.py and __init__ stay intact"),
    ("2 * 3 * 4 is arithmetic", "2 * 3 * 4 is arithmetic"),
])
def test_briefing_markdown_becomes_plain_toast_text(md, text):
    from argus.util import plain
    assert plain(md) == text


def test_the_morning_toast_has_no_markdown(monkeypatch, delivered):
    from argus import daemon
    monkeypatch.setattr(daemon.config, "get", lambda k, d=None: d)
    brief = {"title": "Morning brief - Thu 24 Sep",
             "body": "**The one thing** — push `feat/x`.\n\nmore"}
    bodies = []
    monkeypatch.setattr(notify, "send", lambda title, body="", **k: bodies.append(body))
    daemon._digest_and_notify(lambda: brief, "morning_brief", False)
    assert bodies == ["The one thing — push feat/x."]
