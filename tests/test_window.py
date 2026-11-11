"""The window sensor's bookkeeping, driven through a fake clock and screen."""
import threading
from datetime import datetime

import pytest

from argus import db
from argus.sensors import window


class Clock:
    def __init__(self, t):
        self.t = t


@pytest.fixture
def screen(monkeypatch):
    """Scripted foreground windows; each poll advances the clock by 2s."""
    state = {"script": [], "clock": Clock(datetime(2026, 9, 23, 10, 0).timestamp())}

    def fake_time():
        return state["clock"].t

    monkeypatch.setattr(window.time, "time", fake_time)
    monkeypatch.setattr(window, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(window, "session_locked", lambda: False)

    def fg():
        return state["script"][0] if state["script"] else ("", "", 0)

    monkeypatch.setattr(window, "foreground", fg)
    return state


def _drive(screen, steps):
    """steps: list of (exe, title, seconds). Runs the real loop to completion."""
    stop = threading.Event()
    plan = []
    for exe, title, secs in steps:
        plan += [(exe, title)] * max(1, int(secs // 2))

    class FakeStop:
        def is_set(self):
            return stop.is_set()

        def wait(self, s):
            screen["clock"].t += 2
            if plan:
                plan.pop(0)
            if not plan:
                stop.set()
            screen["script"] = [(*plan[0], 1)] if plan else []

    screen["script"] = [(*plan[0], 1)]
    window.run(FakeStop())


def _rows():
    return db.rows_to_dicts(db.q("SELECT app, title, started, ended, seconds, day "
                                 "FROM activity ORDER BY started"))


def test_long_session_is_checkpointed_while_open(screen, monkeypatch):
    seen = []
    orig = db.extend_activity
    monkeypatch.setattr(db, "extend_activity", lambda rid, end: (seen.append(end), orig(rid, end)))
    _drive(screen, [("Code.exe", "main.py - argus", 400)])
    assert len(seen) >= 5, "an open session must be extended every CHECKPOINT"
    [row] = _rows()
    assert 390 <= row["seconds"] <= 402


def test_switching_windows_closes_the_previous_session(screen):
    _drive(screen, [("Code.exe", "a.py", 20), ("chrome.exe", "docs", 20)])
    rows = _rows()
    assert [r["title"] for r in rows] == ["a.py", "docs"]
    assert rows[0]["ended"] == rows[1]["started"]


def test_current_is_published_for_the_dashboard(screen):
    seen = {}

    orig_publish = window._publish

    def spy(s):
        orig_publish(s)
        if s is not None:
            seen.update(window.current())
    window._publish = spy
    try:
        _drive(screen, [("Code.exe", "live.py", 10)])
    finally:
        window._publish = orig_publish
    assert seen["title"] == "live.py" and seen["live"] is True
    assert window.current() == {}, "cleared when the sensor stops"


def test_session_is_split_at_midnight(screen):
    screen["clock"].t = datetime(2026, 9, 23, 23, 58).timestamp()
    _drive(screen, [("Code.exe", "late.py", 240)])
    rows = _rows()
    assert [r["day"] for r in rows] == ["2026-09-23", "2026-09-24"]
    assert rows[0]["ended"] == datetime(2026, 9, 24).timestamp() == rows[1]["started"]
