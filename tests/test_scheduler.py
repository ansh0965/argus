from datetime import datetime

from argus import db
from argus.scheduler import Job, Scheduler

# 2026-09-20 is a Sunday
SUN_2100 = datetime(2026, 9, 20, 21, 0)
MON_0700 = datetime(2026, 9, 21, 7, 0)
MON_1300 = datetime(2026, 9, 21, 13, 0)


def _job(**kw):
    return Job(name=kw.pop("name", "j"), fn=lambda: "ran", **kw)


def test_daily_fires_once_and_catches_up():
    j = _job(at="08:30", catch_up_hours=5)
    assert not j.due(datetime(2026, 9, 21, 8, 0))
    assert j.due(datetime(2026, 9, 21, 9, 0))
    j.mark(datetime(2026, 9, 21, 9, 0).timestamp())
    assert not j.due(datetime(2026, 9, 21, 10, 0))
    assert not j.due(datetime(2026, 9, 21, 14, 0)), "beyond catch-up window"


def test_weekly_catches_up_the_morning_after():
    j = _job(at="20:00", weekday=6, catch_up_hours=14)
    assert j.due(SUN_2100)
    assert j.due(MON_0700)
    assert not j.due(MON_1300)


def test_interval_job():
    j = _job(every_seconds=60, run_on_start=True)
    assert j.due(datetime.now())
    j.mark()
    assert not j.due(datetime.now())


def test_failed_job_is_marked_so_it_does_not_hot_loop():
    def boom():
        raise RuntimeError("x")
    s = Scheduler()
    j = s.daily("00:00", "bad", boom, catch_up_hours=24)
    ok, _ = s.run_job(j)
    assert not ok and j.last_run() > 0
    assert db.q1("SELECT ok FROM runs WHERE name='bad'")["ok"] == 0


def test_next_due_for_weekly_is_the_next_occurrence():
    s = Scheduler()
    j = s.weekly(6, "20:00", "retro", lambda: None)
    wed = datetime(2026, 9, 23, 12, 0)
    assert datetime.fromtimestamp(s.next_due(j, wed)) == datetime(2026, 9, 27, 20, 0)
    assert datetime.fromtimestamp(s.next_due(j, SUN_2100)) == datetime(2026, 9, 27, 20, 0)


def test_next_due_for_daily_rolls_over():
    s = Scheduler()
    j = s.daily("08:30", "brief", lambda: None)
    assert datetime.fromtimestamp(s.next_due(j, MON_1300)) == datetime(2026, 9, 22, 8, 30)


def test_before_tick_can_swap_jobs_safely():
    import threading
    s = Scheduler()
    ran = []
    s.every(1, "a", lambda: ran.append("a"), run_on_start=True)
    stop = threading.Event()

    def swap():
        s.jobs = [Job(name="b", fn=lambda: (ran.append("b"), stop.set()),
                      every_seconds=1, run_on_start=True)]
    s.loop(stop, interval=0.01, before_tick=swap)
    assert ran == ["b"]


def test_config_edit_rebuilds_the_schedule(write_config, monkeypatch):
    from argus import daemon
    monkeypatch.setattr(daemon, "_sched_sig", None)
    daemon.reload_schedule_if_changed()
    assert daemon.SCHED.by_name("morning_brief").at == "08:30"
    assert not daemon.reload_schedule_if_changed()
    write_config({"digests": {"morning_brief": {"enabled": True, "at": "07:15"}}})
    assert daemon.reload_schedule_if_changed()
    assert daemon.SCHED.by_name("morning_brief").at == "07:15"


def test_quiet_runs_are_not_logged_but_still_marked():
    s = Scheduler()
    j = s.every(60, "sentinel", lambda: "nothing due",
                quiet=lambda r: r.startswith("nothing"))
    s.run_job(j)
    assert db.q1("SELECT COUNT(*) n FROM runs WHERE name='sentinel'")["n"] == 0
    assert j.last_run() > 0
    j.fn = lambda: "Standup@10m"
    s.run_job(j)
    assert db.q1("SELECT COUNT(*) n FROM runs WHERE name='sentinel'")["n"] == 1


def test_forced_quiet_run_is_logged():
    s = Scheduler()
    j = s.every(60, "hb", lambda: "ok", quiet=lambda r: True)
    s.run_job(j, forced=True)
    assert db.q1("SELECT COUNT(*) n FROM runs WHERE name='hb'")["n"] == 1


def test_quiet_runs_stay_out_of_the_log_file_too(caplog):
    import logging
    s = Scheduler()
    j = s.every(60, "sentinel", lambda: "nothing due", quiet=lambda r: r.startswith("nothing"))
    with caplog.at_level(logging.INFO, logger="argus"):
        s.run_job(j)
        assert not [r for r in caplog.records if "sentinel" in r.getMessage()]
        j.fn = lambda: "Standup in 10m"
        s.run_job(j)
        assert [r for r in caplog.records if "Standup in 10m" in r.getMessage()]
