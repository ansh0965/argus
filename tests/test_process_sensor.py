"""Process sensor: app launches and exits, not every helper process."""
from argus.sensors.process import AppTracker

ME = 4000


def _kinds(rows):
    return [(r[1], r[3]) for r in rows]


def test_a_second_chrome_process_is_not_a_second_launch():
    t = AppTracker({}, me=ME)
    snap = {10: ("chrome.exe", 100.0, 1)}
    assert t.step(snap, now=105) == []                          # waits one poll
    assert _kinds(t.step(snap, now=115)) == [("start", "chrome.exe")]
    more = {**snap, 11: ("chrome.exe", 116.0, 10), 12: ("chrome.exe", 117.0, 10)}
    t.step(more, now=125)
    assert t.step(more, now=135) == [], "tabs and helpers of a running app"


def test_the_app_exits_when_its_last_process_does():
    t = AppTracker({10: ("chrome.exe", 1000.0, 1), 11: ("chrome.exe", 1005.0, 10)}, me=ME)
    assert t.step({10: ("chrome.exe", 1000.0, 1)}, now=1500) == []
    rows = t.step({}, now=1600)
    assert _kinds(rows) == [("exit", "chrome.exe")]
    assert rows[0][4] == 600, "lifetime runs from the first process of the session"


def test_what_runs_at_startup_is_not_a_launch():
    t = AppTracker({10: ("code.exe", 0.0, 1)}, me=ME)
    assert t.step({10: ("code.exe", 0.0, 1)}, now=50) == []


def test_our_own_children_are_not_events():
    t = AppTracker({}, me=ME)
    snap = {20: ("git.exe", 100.0, ME), 21: ("claude.exe", 100.0, ME)}
    assert t.step(snap, now=101) == []
    assert t.step(snap, now=111) == []
    assert t.step({}, now=500) == []


def test_always_notable_tools_do_not_wait_a_poll():
    t = AppTracker({}, me=ME)
    assert _kinds(t.step({30: ("git.exe", 100.0, 1)}, now=101)) == [("start", "git.exe")]


def test_a_blip_shorter_than_a_poll_is_nothing():
    t = AppTracker({}, me=ME)
    t.step({40: ("setup.exe", 100.0, 1)}, now=101)
    assert t.step({}, now=111) == []


def test_resuming_after_a_pause_replays_nothing():
    t = AppTracker({}, me=ME)
    t.reseed({50: ("steam.exe", 100.0, 1)})            # started while paused
    assert t.step({50: ("steam.exe", 100.0, 1)}, now=200) == []
