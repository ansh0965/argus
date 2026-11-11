"""Pause must mean "do not watch me" to every sensor, live and retrospective."""
import time

import pytest

from argus import db, pause


def test_pause_resume_roundtrip():
    assert not pause.is_paused()
    pause.pause(30)
    assert pause.is_paused()
    assert pause.paused_until() > time.time() + 29 * 60
    pause.resume()
    assert not pause.is_paused()


def test_intervals_record_when_you_were_paused():
    pause.pause(60)
    time.sleep(0.01)
    pause.resume()
    [(s, e)] = pause.intervals()
    assert e - s < 5, "resume must close the interval early"
    assert pause.was_paused_at((s + e) / 2)
    assert not pause.was_paused_at(e + 1)


def test_extending_a_pause_does_not_open_a_second_interval():
    pause.pause(10)
    pause.pause(60)
    ivs = pause.intervals()
    assert len(ivs) == 1 and ivs[0][1] - ivs[0][0] > 59 * 60
    pause.resume()


def test_tray_names_still_work():
    from argus.actions import tray
    tray.pause(5)
    assert tray.is_paused()
    tray.resume()
    assert not tray.is_paused()


def test_files_sensor_records_nothing_while_paused(tmp_path, write_config):
    from argus.sensors import files
    write_config({"watch_dir_ignores": []})       # tmp_path lives under AppData
    f = tmp_path / "note.md"
    f.write_text("x")
    h = files._Handler()
    pause.pause(5)
    h._emit("modified", str(f))
    assert db.q1("SELECT COUNT(*) n FROM events WHERE source='file'")["n"] == 0
    pause.resume()
    h._emit("modified", str(f))
    assert db.q1("SELECT COUNT(*) n FROM events WHERE source='file'")["n"] == 1


def test_screenshot_never_captures_while_paused(monkeypatch):
    from argus.sensors import screenshot
    monkeypatch.setattr(screenshot, "foreground",
                        lambda: pytest.fail("must not even look at the screen"))
    pause.pause(5)
    assert screenshot.capture_once() is None
    pause.resume()


def test_browser_history_from_a_pause_is_not_ingested_later(monkeypatch):
    from argus.sensors import browser
    pause.pause(60)
    s, _ = pause.intervals()[0]
    time.sleep(0.01)
    pause.resume()
    visits = [
        {"ts": s + 0.001, "url": "https://secret.example/a", "title": "paused", "transition": 0},
        {"ts": time.time() + 1, "url": "https://github.com/x/y", "title": "after", "transition": 1},
    ]
    monkeypatch.setattr(browser, "profiles", lambda: [("Chrome", "History")])
    monkeypatch.setattr(browser, "_read_since", lambda *a, **k: visits)
    assert browser.collect_once() == 1
    rows = db.q("SELECT subject FROM events WHERE source='browser'")
    assert [r["subject"] for r in rows] == ["github.com"]
    # the watermark still moves past the paused visit, so it is never re-read
    assert db.get_state("browser_watermark:Chrome") >= visits[1]["ts"]


def test_unchanged_browser_profiles_are_not_copied_again(monkeypatch, tmp_path):
    from argus.sensors import browser
    hist = tmp_path / "History"
    hist.write_bytes(b"x" * 10)
    reads = []
    monkeypatch.setattr(browser, "profiles", lambda: [("Chrome", hist)])
    monkeypatch.setattr(browser, "_read_since",
                        lambda path, since, limit=3000: reads.append(path) or [])
    browser.collect_once()
    browser.collect_once()
    assert len(reads) == 1, "an untouched History file must not be copied again"
    import os
    later = time.time() + 5
    hist.write_bytes(b"y" * 12)
    os.utime(hist, (later, later))
    browser.collect_once()
    assert len(reads) == 2


def test_the_tray_tooltip_says_what_the_colour_means():
    from argus.actions import tray
    now = 1_790_000_000.0
    assert tray.tooltip(now + 900, None, None, now=now).startswith("ARGUS - paused until ")
    ok = {"sensors": {"window": {"alive": True}, "screenshot": {"alive": False,
                                                                 "reason": "disabled"}},
          "net": {"online": True}}
    assert tray.tooltip(0, ok, 4980, now=now) == "ARGUS - watching · 1h 23m productive today"
    down = {"sensors": {"browser": {"alive": False}}, "net": {"online": False}}
    assert tray.tooltip(0, down, None, now=now) == "ARGUS - browser sensor down · offline"
    assert len(tray.tooltip(0, {"sensors": {f"s{i}": {"alive": False} for i in range(40)}},
                            1, now=now)) <= 127
