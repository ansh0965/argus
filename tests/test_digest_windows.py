from datetime import datetime

from argus.brain import digest


def test_wrap_after_midnight_is_about_the_day_that_ended():
    assert digest.wrap_day(datetime(2026, 9, 24, 0, 40)) == "2026-09-23"
    assert digest.wrap_day(datetime(2026, 9, 24, 7, 10)) == "2026-09-23"
    assert digest.wrap_day(datetime(2026, 9, 23, 23, 30)) == "2026-09-23"
    assert digest.wrap_day(datetime(2026, 9, 23, 15, 0)) == "2026-09-23"


def test_retro_on_sunday_covers_monday_to_now():
    now = datetime(2026, 9, 20, 20, 0)                  # Sunday
    s, e = digest.retro_window(now)
    assert s == datetime(2026, 9, 14) and e == now


def test_retro_caught_up_on_monday_covers_the_finished_week():
    s, e = digest.retro_window(datetime(2026, 9, 21, 7, 5))     # Monday
    assert s == datetime(2026, 9, 14) and e == datetime(2026, 9, 21)


def test_retro_on_demand_midweek_is_week_so_far():
    now = datetime(2026, 9, 23, 16, 0)                   # Wednesday
    s, e = digest.retro_window(now)
    assert s == datetime(2026, 9, 21) and e == now


def test_retro_respects_a_configured_day(write_config):
    write_config({"digests": {"weekly_retro": {"day": 4, "at": "18:00"}}})   # Friday
    now = datetime(2026, 9, 25, 18, 0)
    s, e = digest.retro_window(now)
    assert s == datetime(2026, 9, 19) and e == now


def test_evening_wrap_uses_the_wrap_day(monkeypatch):
    monkeypatch.setattr(digest.llm, "ask_safe", lambda p, s="", fallback="": "body")
    monkeypatch.setattr(digest, "_repo_context", lambda: "")
    monkeypatch.setattr(digest, "write_journal", lambda day, body, facts: day)
    d = digest.evening_wrap(save=False, day="2026-09-23")
    assert d["title"] == "Evening wrap - Wed 23 Sep"


def test_stale_calendar_says_so():
    import time as _t

    from argus import db
    now = _t.time()
    with db.tx() as c:
        c.execute("INSERT INTO gcal(id,cal_id,summary,start_ts,end_ts,location,hangout,"
                  "attendees,status,updated,notified,account,all_day,self_status) VALUES"
                  "('e1','p','Update timesheet',?,?,'','','[]','confirmed',?,'','work',0,'')",
                  (now + 3600, now + 5400, now - 7 * 86400))
    text = digest._calendar_context()
    assert "Update timesheet" in text and "last synced 7 days ago" in text
    db.set_state("sync_ok:calendar", now)
    assert "WARNING" not in digest._calendar_context()
