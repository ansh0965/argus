from datetime import datetime

from argus import db
from argus.brain import timesheet as ts


def _s(day, h1, h2, realm="work", cat="code", exe="code.exe"):
    d = datetime.strptime(day, "%Y-%m-%d")
    a = d.replace(hour=int(h1), minute=int(h1 % 1 * 60)).timestamp()
    b = d.replace(hour=int(h2), minute=int(h2 % 1 * 60)).timestamp()
    db.add_activity("VS Code", exe, "x", cat, realm, a, b, False, False)


def test_day_row_counts_work_and_bounds():
    _s("2026-09-21", 10.5, 12.5)
    _s("2026-09-21", 13, 14, realm="personal")
    _s("2026-09-21", 15, 18.5)
    r = ts.day_row("2026-09-21")
    assert datetime.fromtimestamp(r["first"]).strftime("%H:%M") == "10:30"
    assert datetime.fromtimestamp(r["last"]).strftime("%H:%M") == "18:30"
    assert round(r["active_seconds"] / 3600, 2) == 6.5
    assert round(r["work_seconds"] / 3600, 2) == 5.5
    assert r["inferred"] is False


def test_empty_day_and_estimated_day():
    assert ts.day_row("2026-09-20")["active_seconds"] == 0
    _s("2026-09-19", 10, 11, exe="<inferred:browser>")
    assert ts.day_row("2026-09-19")["inferred"] is True


def test_week_bounds_are_monday_to_sunday():
    wed = datetime(2026, 9, 23, 15, 0)
    assert ts.week_bounds(0, wed) == ("2026-09-21", "2026-09-27")
    assert ts.week_bounds(-1, wed) == ("2026-09-14", "2026-09-20")


def test_csv_has_one_row_per_day():
    _s("2026-09-21", 10, 12)
    text = ts.to_csv(ts.rows("2026-09-21", "2026-09-23"))
    lines = text.strip().splitlines()
    assert lines[0].startswith("date,weekday,first_active")
    assert len(lines) == 4
    assert lines[1].startswith("2026-09-21,Mon,10:00,12:00,2.0,2.0")


def test_api_and_csv_download():
    from fastapi.testclient import TestClient

    from argus.web import server
    c = TestClient(server.app, base_url="http://127.0.0.1:7842")
    j = c.get("/api/timesheet?week=0").json()
    assert len(j["days"]) == 7 and "summary" in j
    r = c.get("/api/timesheet.csv?week=-1")
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]


def test_work_after_midnight_belongs_to_the_day_before():
    _s("2026-09-21", 22, 23.99)
    _s("2026-09-22", 0, 2)            # 00:00-02:00 on the 22nd
    _s("2026-09-22", 11, 12)
    mon = ts.day_row("2026-09-21")
    tue = ts.day_row("2026-09-22")
    assert datetime.fromtimestamp(mon["last"]).strftime("%H:%M") == "02:00"
    assert round(mon["active_seconds"] / 3600, 1) == 4.0
    assert datetime.fromtimestamp(tue["first"]).strftime("%H:%M") == "11:00"


def test_day_start_is_configurable(write_config):
    write_config({"identity": {"day_starts_at": "00:00"}})
    _s("2026-09-22", 0, 2)
    assert datetime.fromtimestamp(ts.day_row("2026-09-22")["first"]).strftime("%H:%M") == "00:00"
