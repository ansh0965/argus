"""Timesheet rows from the activity record: when you started, when you stopped,
how much of it was work, and on what.

Deterministic, like insights - the numbers come straight from recorded
sessions, the project names from the thread engine, the commit counts from
git. Nothing is estimated by a model, so the same day always yields the same
row and a timesheet can be checked against it.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta

from .. import classify, config, db
from ..util import human_dur
from . import insights


def _hours(seconds: float) -> float:
    return round(seconds / 3600, 2)


def work_day_bounds(day: str) -> tuple[float, float]:
    """A timesheet day runs from identity.day_starts_at (default 04:00) to the
    same time next morning. With midnight as the boundary, anyone who works
    past twelve gets days that "start" at 00:00 and "end" at 00:00, and the
    late hours land on the wrong date."""
    s, e = insights.day_bounds(day)
    return s, min(e, insights.now())


def day_row(day: str, realm: str = "work") -> dict:
    """One day. `realm` picks which work counts (work / personal / all)."""
    s, e = work_day_bounds(day)
    act = insights.sessions(s, e) if e > s else []
    row: dict = {
        "day": day,
        "weekday": datetime.strptime(day, "%Y-%m-%d").strftime("%a"),
        "first": None, "last": None,
        "active_seconds": 0.0, "work_seconds": 0.0, "productive_seconds": 0.0,
        "projects": [], "commits": [], "inferred": False,
    }
    if not act:
        return row

    row["first"] = min(r["started"] for r in act)
    row["last"] = max(r["ended"] for r in act)
    row["active_seconds"] = sum(r["seconds"] for r in act)
    row["work_seconds"] = sum(r["seconds"] for r in act
                              if realm == "all" or r["realm"] == realm)
    row["productive_seconds"] = sum(r["seconds"] for r in act
                                    if r["category"] in classify.PRODUCTIVE)
    # Reconstructed history (backfill) is an estimate; say so in the row.
    row["inferred"] = all((r["exe"] or "").startswith("<inferred:") for r in act)

    # Projects, from the threads touched that day, heaviest first.
    for r in db.q(
            "SELECT t.title, t.key, t.realm, SUM(tt.weight) w FROM thread_touches tt "
            "JOIN threads t ON t.id = tt.thread_id WHERE tt.ts >= ? AND tt.ts < ? "
            "GROUP BY t.id ORDER BY w DESC LIMIT 12", (s, e)):
        if realm == "all" or r["realm"] == realm:
            if r["title"] not in row["projects"]:
                row["projects"].append(r["title"])

    for r in db.q("SELECT subject, detail, meta FROM events WHERE source='git' "
                  "AND kind='commit' AND ts >= ? AND ts < ? ORDER BY ts", (s, e)):
        try:
            path = json.loads(r["meta"] or "{}").get("path", "")
        except ValueError:
            path = ""
        if realm == "all" or classify.realm_of(f"{r['subject']} {path}") == realm:
            row["commits"].append(f"{r['subject']}: {r['detail']}"[:160])
    return row


def rows(start: str, end: str, realm: str = "work") -> list[dict]:
    """Every day from start to end inclusive (YYYY-MM-DD)."""
    d = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    out = []
    while d <= stop:
        out.append(day_row(d.strftime("%Y-%m-%d"), realm))
        d += timedelta(days=1)
    return out


def week_bounds(offset_weeks: int = 0, today: datetime | None = None) -> tuple[str, str]:
    """Monday..Sunday of this week (0), last week (-1), ..."""
    today = today or datetime.strptime(insights.current_day(), "%Y-%m-%d")
    monday = (today - timedelta(days=today.weekday())) + timedelta(weeks=offset_weeks)
    return monday.strftime("%Y-%m-%d"), (monday + timedelta(days=6)).strftime("%Y-%m-%d")


def _clock(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""


def to_csv(table: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "weekday", "first_active", "last_active", "active_hours",
                "work_hours", "productive_hours", "projects", "commits", "estimated"])
    for r in table:
        w.writerow([r["day"], r["weekday"], _clock(r["first"]), _clock(r["last"]),
                    _hours(r["active_seconds"]), _hours(r["work_seconds"]),
                    _hours(r["productive_seconds"]), "; ".join(r["projects"][:5]),
                    len(r["commits"]), "yes" if r["inferred"] else ""])
    return buf.getvalue()


def summary(table: list[dict]) -> dict:
    worked = [r for r in table if r["active_seconds"] > 0]
    return {
        "days": len(worked),
        "active": human_dur(sum(r["active_seconds"] for r in table)),
        "work": human_dur(sum(r["work_seconds"] for r in table)),
        "work_hours": _hours(sum(r["work_seconds"] for r in table)),
        "commits": sum(len(r["commits"]) for r in table),
    }
