"""Get your data out: sessions as CSV, or everything for a date range as JSON.

Everything ARGUS records lives in one SQLite file, which is portable but not
convenient. This writes the same rows in formats a spreadsheet, a script or
another tool can read. Private titles stay "<private>" - exporting does not
un-redact anything.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta

from . import db

_ACTIVITY_COLS = ("day", "app", "exe", "title", "category", "realm", "started",
                  "ended", "seconds", "afk", "private")


def _bounds(start: str, end: str) -> tuple[float, float]:
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    return s.timestamp(), e.timestamp()


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else ""


def activity_rows(start: str, end: str, include_afk: bool = False) -> list[dict]:
    s, e = _bounds(start, end)
    sql = ("SELECT " + ",".join(_ACTIVITY_COLS) + " FROM activity "
           "WHERE started >= ? AND started < ?" + ("" if include_afk else " AND afk=0")
           + " ORDER BY started")
    out = []
    for r in db.q(sql, (s, e)):
        d = dict(r)
        d["started"], d["ended"] = _iso(d["started"]), _iso(d["ended"])
        d["seconds"] = round(d["seconds"] or 0)
        out.append(d)
    return out


def activity_csv(start: str, end: str, include_afk: bool = False) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(_ACTIVITY_COLS), lineterminator="\n")
    w.writeheader()
    w.writerows(activity_rows(start, end, include_afk))
    return buf.getvalue()


def everything_json(start: str, end: str) -> str:
    """Sessions, events and briefings in the range, as one JSON document."""
    s, e = _bounds(start, end)
    events = []
    for r in db.q("SELECT ts, source, kind, subject, detail, value, meta FROM events "
                  "WHERE ts >= ? AND ts < ? ORDER BY ts", (s, e)):
        d = dict(r)
        d["ts"] = _iso(d["ts"])
        try:
            d["meta"] = json.loads(d["meta"]) if d["meta"] else None
        except ValueError:
            pass
        events.append(d)
    digests = [{"ts": _iso(r["ts"]), "kind": r["kind"], "title": r["title"], "body": r["body"]}
               for r in db.q("SELECT ts, kind, title, body FROM digests "
                             "WHERE ts >= ? AND ts < ? ORDER BY ts", (s, e))]
    return json.dumps({
        "exported": datetime.now().isoformat(timespec="seconds"),
        "from": start, "to": end,
        "activity": activity_rows(start, end, include_afk=True),
        "events": events,
        "digests": digests,
    }, ensure_ascii=False, indent=1, default=str)
