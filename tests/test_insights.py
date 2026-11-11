"""The deterministic analytics every brief and dashboard number rests on."""
from datetime import datetime

import pytest

from argus import db
from argus.brain import insights

DAY = "2026-09-21"


def _at(h, m=0):
    return datetime.strptime(DAY, "%Y-%m-%d").replace(hour=h, minute=m).timestamp()


def _s(h1, m1, h2, m2, app="VS Code", cat="code", realm="work", afk=False):
    db.add_activity(app, app.lower() + ".exe", f"{app} window", cat, realm,
                    _at(h1, m1), _at(h2, m2), afk, False)


@pytest.fixture
def window():
    return _at(0), _at(23, 59)


def test_sessions_are_clipped_to_the_window():
    _s(9, 30, 10, 30)
    rows = insights.sessions(_at(10), _at(11))
    assert len(rows) == 1 and rows[0]["seconds"] == 1800


def test_totals_split_productive_distracting_and_idle(window):
    _s(10, 0, 11, 0)                                   # code 60m
    _s(11, 0, 11, 30, app="Chrome", cat="media")       # 30m distraction
    _s(11, 30, 11, 45, app="Away", cat="idle", afk=True)
    t = insights.totals(*window)
    assert t["active_seconds"] == 90 * 60
    assert t["productive_seconds"] == 60 * 60
    assert t["distracting_seconds"] == 30 * 60
    assert t["at_desk_seconds"] == 105 * 60
    assert t["productive_pct"] == pytest.approx(66.7, abs=0.1)


def test_deep_work_absorbs_short_detours(window):
    _s(10, 0, 10, 20)
    _s(10, 20, 10, 21, app="Slack", cat="comms")       # a glance, under 2 min
    _s(10, 21, 10, 45)
    blocks = insights.deep_work_blocks(*window, min_minutes=25)
    assert len(blocks) == 1 and blocks[0]["from"] == "10:00" and blocks[0]["to"] == "10:45"


def test_a_long_detour_breaks_a_block(window):
    _s(10, 0, 10, 30)
    _s(10, 30, 10, 40, app="Chrome", cat="media")
    _s(10, 40, 11, 10)
    blocks = insights.deep_work_blocks(*window, min_minutes=25)
    assert [(b["from"], b["to"]) for b in blocks] == [("10:00", "10:30"), ("10:40", "11:10")]


def test_focus_score_is_explainable(window):
    _s(10, 0, 12, 0)
    f = insights.focus_score(*window)
    assert 0 <= f["score"] <= 100
    assert set(f["components"]) == {"productive_ratio", "deep_work_ratio",
                                    "switch_rate_per_10min", "churn_penalty"}
    assert f["components"]["productive_ratio"] == 1.0


def test_too_little_activity_has_no_score(window):
    _s(10, 0, 10, 2)
    assert insights.focus_score(*window)["score"] is None


def test_context_switches_count_app_changes(window):
    for i in range(6):
        app = "VS Code" if i % 2 == 0 else "Chrome"
        _s(10, i, 10, i + 1, app=app, cat="code" if i % 2 == 0 else "browse")
    sw = insights.context_switches(*window)
    assert sum(b["switches"] for b in sw) == 5


def test_timeline_slots_carry_the_dominant_category(window):
    _s(10, 0, 10, 12)
    _s(10, 12, 10, 15, app="Chrome", cat="media")
    tl = insights.timeline(_at(10), _at(10, 30))
    first = next(s for s in tl if s["label"] == "10:00")
    assert first["category"] == "code"
    assert first["mix"]["media"] == 180


def test_distractions_use_configured_names(window, write_config):
    write_config({"focus": {"distraction_apps": ["stremio"]}})
    _s(20, 0, 21, 0, app="Stremio", cat="media")
    d = insights.distractions(*window)
    assert d[0]["what"] == "stremio" and d[0]["seconds"] == 3600


def test_facts_for_llm_is_compact_and_complete():
    _s(10, 0, 11, 0)
    db.log_event("git", "commit", "atlas", "add OCR triage", ts=_at(10, 30))
    text = insights.facts_for_llm(DAY)
    assert text.startswith(f"DATE: {DAY}")
    assert "Commits: atlas: add OCR triage" in text
    assert "VS Code" in text


def test_an_apps_category_is_where_its_time_went(window):
    _s(9, 0, 9, 5, app="Chrome", cat="media")
    _s(9, 5, 11, 0, app="Chrome", cat="code")
    [a] = insights.by_app(*window)
    assert a["category"] == "code"
    assert set(a["categories"]) == {"code", "media"}


def test_a_night_away_is_not_time_at_the_desk(window):
    _s(9, 0, 10, 0)                                           # an hour of work
    _s(10, 0, 10, 10, app="Away", cat="idle", afk=True)       # a coffee: at desk
    _s(10, 10, 18, 0, app="Away", cat="idle", afk=True)       # machine left idle
    t = insights.totals(*window)
    assert t["at_desk_seconds"] == 70 * 60
    assert t["away_seconds"] == (7 * 60 + 50) * 60
    assert "away" in insights.facts_for_llm(DAY)


# ---------------------------------------------------------------- the day boundary

def test_before_four_am_it_is_still_yesterday():
    from datetime import datetime

    from argus.brain import insights
    assert insights.current_day(datetime(2026, 9, 25, 1, 30)) == "2026-09-24"
    assert insights.current_day(datetime(2026, 9, 25, 4, 0)) == "2026-09-25"
    assert insights.current_day(datetime(2026, 9, 25, 23, 59)) == "2026-09-25"


def test_the_today_numbers_use_the_same_day_as_the_timesheet():
    """Late-night work was "at the desk" today on the Today tab but belonged to
    yesterday on the timesheet; both now cut the day at 04:00."""
    from datetime import datetime

    from argus import db
    from argus.brain import insights, timesheet

    def at(day, h1, h2):
        d = datetime.strptime(day, "%Y-%m-%d")
        db.add_activity("VS Code", "code.exe", "x", "code", "work",
                        d.replace(hour=h1).timestamp(), d.replace(hour=h2).timestamp(),
                        False, False)
    at("2026-09-22", 0, 2)            # the tail of the 21st's evening
    at("2026-09-22", 10, 12)
    s, e, _ = insights.day_range("2026-09-22")
    assert datetime.fromtimestamp(s).strftime("%d %H:%M") == "22 04:00"
    assert round(insights.totals(s, e)["active_seconds"] / 3600, 1) == 2.0
    assert (round(timesheet.day_row("2026-09-22")["active_seconds"])
            == round(insights.totals(s, e)["active_seconds"]))
    s, e, _ = insights.day_range("2026-09-21")
    assert round(insights.totals(s, e)["active_seconds"] / 3600, 1) == 2.0


def test_midnight_boundary_still_available(write_config):
    from datetime import datetime

    from argus.brain import insights
    write_config({"identity": {"day_starts_at": "00:00"}})
    assert insights.current_day(datetime(2026, 9, 25, 1, 30)) == "2026-09-25"
    s, _, _ = insights.day_range("2026-09-25")
    assert datetime.fromtimestamp(s).strftime("%H:%M") == "00:00"


def test_a_day_summary_reads_each_window_once(monkeypatch):
    from argus import db
    from argus.brain import insights
    calls = []
    real_q = db.q

    def counting_q(sql, args=()):
        if "FROM activity WHERE ended >" in sql:
            calls.append(sql)
        return real_q(sql, args)
    monkeypatch.setattr(db, "q", counting_q)
    insights.day_summary()
    assert len(calls) <= 2, "one read per window (active, with-idle), not thirteen"
    calls.clear()
    insights.totals(*insights.day_range()[:2])
    insights.totals(*insights.day_range()[:2])
    assert len(calls) == 4, "outside a summary nothing is cached"


def test_the_today_endpoint_shares_the_read_with_the_timesheet(monkeypatch):
    from fastapi.testclient import TestClient

    from argus import db
    from argus.web import server
    calls = []
    real_q = db.q
    monkeypatch.setattr(db, "q", lambda sql, args=(): (
        calls.append(sql) if "FROM activity WHERE ended >" in sql else None) or real_q(sql, args))
    c = TestClient(server.app, base_url="http://127.0.0.1:7842")
    assert c.get("/api/today").status_code == 200
    assert len(calls) <= 2


def test_the_baseline_is_fourteen_whole_days():
    """Grouped by calendar date, fourteen days from 04:00 were fifteen groups,
    two of them partial ("vs your 15-day average")."""
    from datetime import datetime, timedelta

    from argus import db
    from argus.brain import insights
    today = datetime.strptime(insights.current_day(), "%Y-%m-%d")
    for back in range(1, 20):
        d = today - timedelta(days=back)
        # a morning, and that day's late night (02:00 on the next date)
        for at in (d.replace(hour=11), (d + timedelta(days=1)).replace(hour=2)):
            a = at.timestamp()
            db.add_activity("VS Code", "code.exe", "x", "code", "work", a, a + 1800,
                            False, False)
    cmp_ = insights.compare_to_baseline(today.strftime("%Y-%m-%d"))
    assert cmp_["days_in_baseline"] == 14
    assert round(cmp_["baseline_avg_full_day"]) == 3600, "every day whole: 2 x 30 min"


def test_a_late_night_keeps_the_streak_of_the_day_it_belongs_to():
    from datetime import datetime, timedelta

    from argus import db
    from argus.brain import insights
    today = datetime.strptime(insights.current_day(), "%Y-%m-%d")
    for back in (1, 2):
        # 90 minutes of work, all of it after midnight
        a = (today - timedelta(days=back) + timedelta(days=1)).replace(hour=1).timestamp()
        db.add_activity("VS Code", "code.exe", "x", "code", "work", a, a + 5400, False, False)
    assert insights.streaks()["current"] == 2
