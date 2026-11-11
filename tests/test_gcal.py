"""Calendar mirror: deletions propagate, all-day and declined events stay quiet."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from argus import db
from argus.gsuite import gcal


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class _Req:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeCal:
    """Just enough of the Calendar API: one calendar, paged events."""

    def __init__(self, events, page=2):
        self.events_data = events
        self.page = page

    def calendarList(self):
        return self

    def list(self, **kw):
        if "calendarId" not in kw:
            return _Req({"items": [{"id": "primary"}]})
        start = int(kw.get("pageToken") or 0)
        chunk = self.events_data[start:start + self.page]
        res = {"items": chunk}
        if start + self.page < len(self.events_data):
            res["nextPageToken"] = str(start + self.page)
        return _Req(res)

    def events(self):
        return self


def _ev(i, start, minutes=30, **extra):
    e = {"id": f"e{i}", "summary": f"Meeting {i}", "status": "confirmed",
         "start": {"dateTime": _iso(start)}, "end": {"dateTime": _iso(start + minutes * 60)}}
    e.update(extra)
    return e


@pytest.fixture
def cal(monkeypatch):
    holder = {}
    monkeypatch.setattr(gcal, "_svc", lambda label, interactive=False: holder["svc"])
    return holder


def test_sync_follows_pagination(cal):
    now = time.time()
    cal["svc"] = FakeCal([_ev(i, now + 3600 * (i + 1)) for i in range(5)], page=2)
    assert gcal.sync("work") == 5
    assert db.q1("SELECT COUNT(*) n FROM gcal")["n"] == 5


def test_deleted_upstream_is_deleted_here(cal):
    now = time.time()
    cal["svc"] = FakeCal([_ev(1, now + 3600), _ev(2, now + 7200)])
    gcal.sync("work")
    cal["svc"] = FakeCal([_ev(1, now + 3600)])          # e2 was cancelled/deleted
    gcal.sync("work")
    assert [r["id"] for r in db.q("SELECT id FROM gcal")] == ["e1"]


def test_other_accounts_are_not_touched_by_a_sync(cal):
    now = time.time()
    cal["svc"] = FakeCal([_ev(1, now + 3600)])
    gcal.sync("personal")
    cal["svc"] = FakeCal([_ev(2, now + 3600)])
    gcal.sync("work")
    assert {r["id"] for r in db.q("SELECT id FROM gcal")} == {"e1", "e2"}


def test_failed_listing_keeps_the_mirror(cal):
    now = time.time()
    cal["svc"] = FakeCal([_ev(1, now + 3600)])
    gcal.sync("work")

    class Broken(FakeCal):
        def list(self, **kw):
            if "calendarId" in kw:
                raise TimeoutError("read timed out")
            return super().list(**kw)

    cal["svc"] = Broken([])
    gcal.sync("work")
    assert db.q1("SELECT COUNT(*) n FROM gcal")["n"] == 1


def _put(id_, start, **cols):
    base = {"id": id_, "cal_id": "primary", "summary": id_, "start_ts": start,
            "end_ts": start + 1800, "location": "", "hangout": "", "attendees": "[]",
            "status": "confirmed", "updated": time.time(), "notified": "",
            "account": "work", "all_day": 0, "self_status": ""}
    base.update(cols)
    with db.tx() as c:
        c.execute(f"INSERT INTO gcal({','.join(base)}) VALUES({','.join('?' * len(base))})",
                  tuple(base.values()))


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(gcal.notify, "send", lambda title, *a, **k: out.append(title))
    return out


def test_sentinel_ignores_all_day_events(sent):
    _put("Holiday", time.time() + 10 * 60, all_day=1, end_ts=time.time() + 86400)
    gcal.sentinel()
    assert sent == []


def test_sentinel_ignores_declined_meetings(sent):
    _put("Declined sync", time.time() + 10 * 60, self_status="declined")
    gcal.sentinel()
    assert sent == []


def test_sentinel_warns_once_per_lead(sent):
    _put("Standup", time.time() + 10 * 60)
    gcal.sentinel()
    gcal.sentinel()
    assert sent == ["Standup in 10 min"]


def test_all_day_is_not_a_meeting_now():
    _put("Conference", time.time() - 3600, all_day=1, end_ts=time.time() + 86400)
    assert gcal.in_meeting_now() is None
    assert gcal.next_event() is None
    _put("1:1", time.time() + 600)
    assert gcal.next_event()["id"] == "1:1"


def test_all_day_parse():
    ts, all_day = gcal._parse({"date": "2026-10-02"})
    assert all_day and datetime.fromtimestamp(ts).hour == 0


def test_free_slots_use_configured_work_hours(write_config):
    write_config({"identity": {"work_hours": {"start": "09:00", "end": "10:00",
                                              "days": [0, 1, 2, 3, 4, 5, 6]}}})
    slots = gcal.free_slots(day_offset=1)
    assert [s["label"] for s in slots] == ["09:00-10:00"]


def test_sentinel_admits_a_stale_calendar(monkeypatch):
    bodies = []
    monkeypatch.setattr(gcal.notify, "send", lambda title, body="", **k: bodies.append(body))
    _put("Update timesheet", time.time() + 10 * 60, updated=time.time() - 7 * 86400)
    gcal.sentinel()
    assert "calendar not synced since" in bodies[0]
