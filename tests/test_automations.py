"""Automation behaviours that depend on reading the activity record correctly."""
import time
from datetime import datetime, timedelta

import pytest

from argus import db
from argus.automations import builtin


@pytest.fixture
def nudges(monkeypatch):
    out = []
    monkeypatch.setattr(builtin.notify, "send", lambda title, *a, **k: out.append(title))
    return out


def _session(start_ago_min, minutes, afk=False, app="VS Code"):
    now = time.time()
    s = now - start_ago_min * 60
    db.add_activity(app, "code.exe" if not afk else "<idle>", "x", "code" if not afk else "idle",
                    "unknown", s, s + minutes * 60, afk, False)


def test_break_reminder_fires_after_a_long_unbroken_stretch(nudges):
    _session(100, 100)
    assert builtin.break_reminder().startswith("nudged")
    assert nudges == ["Stand up"]


def test_an_afk_session_is_a_break(nudges):
    _session(100, 50)
    _session(50, 10, afk=True)
    _session(40, 40)
    assert builtin.break_reminder().startswith("streak")
    assert nudges == []


def test_an_unrecorded_gap_is_a_break(nudges):
    # a pause (or the machine asleep) leaves no rows at all; that was counted
    # as continuous work, so coming back from a paused lunch said "Stand up"
    _session(160, 60)          # 160 -> 100 minutes ago
    _session(40, 40)           # nothing recorded 100 -> 40 minutes ago
    assert builtin.break_reminder().startswith("streak")
    assert nudges == []


def test_idle_since_the_last_session_is_a_break(nudges):
    _session(130, 100)         # ended 30 minutes ago, nothing since
    assert builtin.break_reminder() in ("no streak", "no current streak")
    assert nudges == []


def test_top_consumers_measures_real_cpu():
    import os
    import threading

    from argus.sensors import process
    rows = process.top_consumers(3, interval=0.05)
    assert rows and all("cpu" in r for r in rows)
    # Burn CPU in this process while sampling, so there is always something to
    # measure - an idle machine can otherwise round every process to 0.0%.
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            sum(range(1000))
    t = threading.Thread(target=spin, daemon=True)
    t.start()
    process._sample = (0.0, [])       # not the idle sample from a moment ago
    try:
        busy = process.top_consumers(500, interval=0.4)
    finally:
        stop.set()
        t.join()
    me = next((r for r in busy if r["pid"] == os.getpid()), None)
    assert me is not None and me["cpu"] > 0, "a real sample, not psutil's first-call 0.0"


def test_one_process_sample_serves_callers_for_a_few_seconds(monkeypatch):
    from argus.sensors import process
    calls = []
    monkeypatch.setattr(process, "_measure", lambda interval: calls.append(1) or
                        [{"pid": i, "name": f"p{i}", "cpu": 0.0, "mem_mb": 1.0} for i in range(20)])
    monkeypatch.setattr(process, "_sample", (0.0, []))
    assert len(process.top_consumers(12)) == 12
    assert len(process.top_consumers(3)) == 3
    assert calls == [1], "the System tab and the watchdog share one ~1s sample"
    monkeypatch.setattr(process, "_sample", (0.0, process._sample[1]))   # expired
    process.top_consumers(3)
    assert calls == [1, 1]


def test_streak_survives_until_today_is_over():
    from argus.brain import insights
    today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    for back in (1, 2, 3):
        d = today - timedelta(days=back)
        db.add_activity("VS Code", "code.exe", "work", "code", "work",
                        d.timestamp(), d.timestamp() + 2 * 3600, False, False)
    st = insights.streaks()
    assert st["current"] == 3, "no work *yet* today must not reset the streak to 0"
    assert st["best"] >= 3


def _meeting_now():
    now = time.time()
    with db.tx() as c:
        c.execute("INSERT INTO gcal(id,cal_id,summary,start_ts,end_ts,location,hangout,"
                  "attendees,status,updated,notified,account,all_day,self_status) "
                  "VALUES('m1','p','Sprint review',?,?,'','','[]','confirmed',?,'','work',0,'')",
                  (now - 600, now + 1200, now))


def test_no_nudges_during_a_meeting(nudges):
    _meeting_now()
    _session(100, 100)
    assert builtin.break_reminder().startswith("in a meeting")
    assert builtin.focus_guardian().startswith("in a meeting")
    assert nudges == []


def test_mail_held_back_by_cooldown_is_announced_later(monkeypatch):
    from argus import daemon
    from argus.actions import notify
    monkeypatch.setattr(daemon, "google_connected", lambda: True)
    now = time.time()
    with db.tx() as c:
        c.execute("INSERT INTO gmail(id,ts,sender,sender_email,subject,unread,triage,seen,account)"
                  " VALUES('g1',?,'Sam','v@x.io','Review please',1,'needs_reply',0,'work')",
                  (now,))
    monkeypatch.setattr(notify, "send", lambda *a, **k: False)     # e.g. cooldown
    assert "waiting" in daemon.inbox_alert()
    assert db.q1("SELECT seen FROM gmail WHERE id='g1'")["seen"] == 0
    monkeypatch.setattr(notify, "send", lambda *a, **k: True)
    assert daemon.inbox_alert() == "1 flagged"
    assert db.q1("SELECT seen FROM gmail WHERE id='g1'")["seen"] == 1


def test_git_states_are_reused_within_max_age(monkeypatch):
    from argus.sensors import git
    calls = []
    monkeypatch.setattr(git, "discover", lambda force=False: ["C:/r1"])
    monkeypatch.setattr(git, "repo_state", lambda p: calls.append(p) or {"path": p, "name": "r1"})
    db.set_state("git_states", {})
    git.all_states()
    git.all_states()
    assert calls == ["C:/r1"], "second call must reuse the fresh scan"
    git.all_states(max_age=0)
    assert len(calls) == 2


def test_standup_covers_the_last_work_day(monkeypatch):
    mon = datetime(2026, 9, 21, 9, 45)
    assert builtin.last_workday(mon) == "2026-09-18"            # Friday
    assert builtin.last_workday(datetime(2026, 9, 23, 9, 45)) == "2026-09-22"


def test_no_standup_at_the_weekend(monkeypatch):
    monkeypatch.setattr(builtin.llm, "ask_safe",
                        lambda *a, **k: pytest.fail("no standup on a Saturday"))
    assert builtin.standup_writer(datetime(2026, 9, 26, 9, 45)) == "not a work day"


def test_standup_is_announced(monkeypatch, nudges):
    d = datetime(2026, 9, 18, 11, 0).timestamp()
    db.add_activity("VS Code", "code.exe", "atlas", "code", "work", d, d + 3600, False, False)
    monkeypatch.setattr(builtin.llm, "ask_safe",
                        lambda *a, **k: "Yesterday:\n- atlas: OCR triage\nToday:\n- more")
    builtin.standup_writer(datetime(2026, 9, 21, 9, 45))
    assert nudges == ["Standup drafted"]
    row = db.q1("SELECT title, meta FROM digests WHERE kind='standup'")
    assert row["title"] == "Standup - Mon 21 Sep", "named like the other briefings"
    assert '"for_day": "2026-09-18"' in row["meta"], "and still says which day it covers"
