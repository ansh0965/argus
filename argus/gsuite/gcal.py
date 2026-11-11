"""Calendar: mirror upcoming events locally, warn before each one, open the link.

The mirror means the dashboard and the morning brief work with no network, and
gives us a place to record which warnings already fired.
"""
from __future__ import annotations

import json
import time
import webbrowser
from datetime import datetime, timedelta, timezone

from .. import config, db
from ..actions import notify
from ..util import log, parse_hhmm
from . import auth

lg = log("google.calendar")


def _svc(label: str, interactive: bool = False):
    return auth.service("calendar", "v3", label, interactive)


def _parse(dt: dict) -> tuple[float, bool]:
    """Return (epoch, all_day)."""
    if "dateTime" in dt:
        s = dt["dateTime"].replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp(), False
    if "date" in dt:
        return datetime.strptime(dt["date"], "%Y-%m-%d").timestamp(), True
    return 0.0, False


def _list_events(svc, cal_id: str, tmin: str, tmax: str) -> list[dict]:
    """Every event in the window, following pagination to the end.

    Completeness matters: sync() deletes whatever the listing did not return,
    so a truncated listing would silently delete real meetings.
    """
    items: list[dict] = []
    token = None
    for _ in range(20):
        res = svc.events().list(
            calendarId=cal_id, timeMin=tmin, timeMax=tmax, singleEvents=True,
            orderBy="startTime", maxResults=250, pageToken=token).execute()
        items += res.get("items", [])
        token = res.get("nextPageToken")
        if not token:
            return items
    raise RuntimeError(f"calendar {cal_id}: too many pages")


def _self_status(ev: dict) -> str:
    for a in ev.get("attendees", []) or []:
        if a.get("self"):
            return a.get("responseStatus", "") or ""
    return ""


def sync(label: str, days_ahead: int = 14, days_back: int = 1) -> int:
    svc = _svc(label)
    if not svc:
        return 0
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(days=days_back), now + timedelta(days=days_ahead)
    tmin, tmax = lo.isoformat(), hi.isoformat()

    try:
        cals = svc.calendarList().list().execute().get("items", [])
    except Exception as e:
        lg.warning("calendarList failed: %s", str(e)[:200])
        return 0

    count = removed = 0
    for cal in cals:
        if cal.get("selected") is False:
            continue
        cal_id = cal["id"]
        try:
            events = _list_events(svc, cal_id, tmin, tmax)
        except Exception as e:
            lg.warning("events.list failed for %s: %s", cal_id, str(e)[:160])
            continue                     # keep the mirror as it was, never guess

        seen: list[str] = []
        with db.tx() as c:
            for ev in events:
                start, all_day = _parse(ev.get("start", {}))
                end, _ = _parse(ev.get("end", {}))
                if not start:
                    continue
                attendees = [a.get("email", "") for a in ev.get("attendees", [])][:20]
                hangout = ev.get("hangoutLink") or ""
                if not hangout:
                    for ep in (ev.get("conferenceData", {}) or {}).get("entryPoints", []):
                        if ep.get("entryPointType") == "video":
                            hangout = ep.get("uri", "")
                            break
                c.execute(
                    "INSERT INTO gcal(id,cal_id,summary,start_ts,end_ts,location,hangout,"
                    "attendees,status,updated,notified,account,all_day,self_status)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,'',?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET summary=excluded.summary,"
                    "start_ts=excluded.start_ts,end_ts=excluded.end_ts,"
                    "location=excluded.location,hangout=excluded.hangout,"
                    "attendees=excluded.attendees,status=excluded.status,"
                    "updated=excluded.updated,all_day=excluded.all_day,"
                    "self_status=excluded.self_status",
                    (ev["id"], cal_id, (ev.get("summary") or "(no title)")[:300],
                     start, end, (ev.get("location") or "")[:200], hangout,
                     json.dumps(attendees), ev.get("status", "confirmed"), time.time(),
                     label, int(all_day), _self_status(ev)))
                seen.append(ev["id"])
                count += 1

            # A meeting deleted upstream simply stops being listed. Without this
            # it stayed in the mirror forever, and the sentinel kept warning
            # you ten minutes before a meeting that no longer existed.
            gone = c.execute(
                "SELECT id FROM gcal WHERE account=? AND cal_id=? AND start_ts >= ? "
                "AND start_ts < ?", (label, cal_id, lo.timestamp(), hi.timestamp())
            ).fetchall()
            keep = set(seen)
            stale = [r["id"] for r in gone if r["id"] not in keep]
            for i in range(0, len(stale), 400):
                chunk = stale[i:i + 400]
                c.execute("DELETE FROM gcal WHERE id IN (" + ",".join("?" * len(chunk))
                          + ")", chunk)
            removed += len(stale)

    with db.tx() as c:
        c.execute("DELETE FROM gcal WHERE start_ts < ?", (time.time() - 30 * 86400,))
    if count or removed:
        db.log_event("gcal", "sync", label,
                     f"{count} events" + (f", {removed} removed" if removed else ""),
                     value=count)
    return count


# Events that occupy your time: not all-day markers, not cancelled, not declined.
_REAL = "status!='cancelled' AND COALESCE(all_day,0)=0 AND COALESCE(self_status,'')!='declined'"


def upcoming(hours: int = 24, limit: int = 20) -> list[dict]:
    now = time.time()
    return db.rows_to_dicts(db.q(
        "SELECT * FROM gcal WHERE end_ts > ? AND start_ts < ? AND status!='cancelled' "
        "ORDER BY start_ts LIMIT ?", (now, now + hours * 3600, limit)))


def next_event() -> dict | None:
    """The next thing that actually needs you there - not a holiday banner."""
    now = time.time()
    r = db.q1(f"SELECT * FROM gcal WHERE start_ts > ? AND start_ts < ? AND {_REAL} "
              "ORDER BY start_ts LIMIT 1", (now, now + 48 * 3600))
    return dict(r) if r else None


def in_meeting_now() -> dict | None:
    now = time.time()
    r = db.q1(f"SELECT * FROM gcal WHERE start_ts <= ? AND end_ts > ? AND {_REAL} "
              "ORDER BY start_ts DESC LIMIT 1", (now, now))
    return dict(r) if r else None


def _stale_note() -> str:
    """A calendar copy over a day old may hold meetings that were moved or
    cancelled since; say so rather than warn with false confidence."""
    from ..brain.digest import last_synced
    ts = last_synced("calendar")
    if ts and time.time() - ts > 86400:
        return f"  (calendar not synced since {datetime.fromtimestamp(ts):%a %d %b})"
    return ""


def sentinel() -> str:
    """Warn ahead of each meeting at the configured lead times."""
    if not config.get("automations.meeting_sentinel.enabled", True):
        return "disabled"
    leads = sorted(config.get("google.calendar_alert_minutes", [10, 2]), reverse=True)
    now = time.time()
    fired = []

    for ev in upcoming(hours=3, limit=10):
        if ev.get("all_day") or ev.get("self_status") == "declined":
            continue
        mins_away = (ev["start_ts"] - now) / 60
        if mins_away < -1:
            continue
        already = set(filter(None, (ev.get("notified") or "").split(",")))
        for lead in leads:
            tag = str(lead)
            if tag in already:
                continue
            # Fire once inside the window, allowing for the poll interval.
            if lead - 1.2 <= mins_away <= lead + 0.6:
                where = ""
                if ev["hangout"]:
                    where = "  (video call - link ready)"
                elif ev["location"]:
                    where = f"  @ {ev['location']}"
                prep = ""
                if lead >= 5:                  # the early warning has time to read
                    try:
                        from ..brain import board
                        att = json.loads(ev.get("attendees") or "[]")
                        lines = board.meeting_prep(ev["summary"], att)
                        prep = ("\n" + "\n".join(lines)) if lines else ""
                    except Exception:
                        prep = ""
                notify.send(
                    f"{ev['summary']} in {lead} min",
                    f"{datetime.fromtimestamp(ev['start_ts']):%H:%M}{where}{_stale_note()}{prep}",
                    severity="urgent" if lead <= 2 else "warn",
                    rule=f"meeting_{ev['id']}_{lead}", force=True, speak=True,
                    # click to join; notify.safe_url() refuses non-https links,
                    # since the link is whatever the invite's sender put there
                    url=ev["hangout"] or notify.dashboard("google"))
                already.add(tag)
                fired.append(f"{ev['summary'][:30]}@{lead}m")
                if lead <= 2 and ev["hangout"] and config.get(
                        "automations.meeting_sentinel.auto_open_link", False):
                    try:
                        webbrowser.open(ev["hangout"])
                    except Exception:
                        pass
        if already:
            with db.tx() as c:
                c.execute("UPDATE gcal SET notified=? WHERE id=?",
                          (",".join(sorted(already)), ev["id"]))
    return ", ".join(fired) if fired else "nothing due"


def create_event(summary: str, start: datetime, minutes: int = 30,
                 description: str = "", label: str = "") -> str:
    svc = _svc(label or (auth.labels() or [""])[0])
    if not svc:
        return "not connected"
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.astimezone().isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=minutes)).astimezone().isoformat()},
    }
    try:
        ev = svc.events().insert(calendarId="primary", body=body).execute()
        db.log_event("gcal", "created", summary, ev.get("htmlLink", ""))
        return ev.get("htmlLink", "created")
    except Exception as e:
        return f"failed: {str(e)[:200]}"


def free_slots(day_offset: int = 0, work_start: str | None = None,
               work_end: str | None = None, min_minutes: int = 30) -> list[dict]:
    """Gaps in the working day - useful for 'when can I actually think today'."""
    wh = config.get("identity.work_hours", {}) or {}
    work_start = work_start or wh.get("start", "10:30")
    work_end = work_end or wh.get("end", "18:30")
    base = (datetime.now() + timedelta(days=day_offset)).replace(second=0, microsecond=0)
    sh, sm = parse_hhmm(work_start)
    eh, em = parse_hhmm(work_end)
    start = base.replace(hour=sh, minute=sm).timestamp()
    end = base.replace(hour=eh, minute=em).timestamp()

    busy = db.q(f"SELECT start_ts, end_ts FROM gcal WHERE end_ts > ? AND start_ts < ? "
                f"AND {_REAL} ORDER BY start_ts", (start, end))
    slots, cursor = [], max(start, time.time() if day_offset == 0 else start)
    for b in busy:
        if b["start_ts"] - cursor >= min_minutes * 60:
            slots.append({"start": cursor, "end": b["start_ts"],
                          "minutes": round((b["start_ts"] - cursor) / 60)})
        cursor = max(cursor, b["end_ts"])
    if end - cursor >= min_minutes * 60:
        slots.append({"start": cursor, "end": end, "minutes": round((end - cursor) / 60)})
    for s in slots:
        s["label"] = (f"{datetime.fromtimestamp(s['start']):%H:%M}-"
                      f"{datetime.fromtimestamp(s['end']):%H:%M}")
    return slots
