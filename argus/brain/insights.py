"""Analytics over the activity store: the numbers behind every brief and nudge.

Nothing here calls an LLM. These are deterministic aggregates so the dashboard
stays instant and the digests have hard facts to reason about.
"""
from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

from .. import classify, config, db
from ..util import human_dur

# --------------------------------------------------------------------------- core pulls


# One day summary asked for the same day's sessions thirteen times (totals,
# focus, categories, apps, realms, blocks, switches, timeline, ...), which was
# most of /api/today's time. Inside `with memo():` a window is read once.
# Callers treat the returned rows as read-only.
_memo: ContextVar[dict | None] = ContextVar("insights_memo", default=None)


@contextmanager
def memo():
    if _memo.get() is not None:         # already inside one: share it
        yield
        return
    token = _memo.set({})
    try:
        yield
    finally:
        _memo.reset(token)


def now() -> float:
    """time.time(), pinned for the length of a memo so that every "today up to
    now" window in one summary is the same window (and the same cache key)."""
    cache = _memo.get()
    if cache is None:
        return time.time()
    return cache.setdefault("now", time.time())


def sessions(start: float, end: float, include_afk: bool = False) -> list[dict]:
    cache = _memo.get()
    key = (start, end, include_afk)
    if cache is not None and key in cache:
        return cache[key]
    sql = ("SELECT app, exe, title, category, realm, started, ended, seconds, afk, private "
           "FROM activity WHERE ended > ? AND started < ?")
    if not include_afk:
        sql += " AND afk=0"
    sql += " ORDER BY started"
    rows = db.rows_to_dicts(db.q(sql, (start, end)))
    # Clip partial sessions to the window so day totals never over-count.
    for r in rows:
        r["started"] = max(r["started"], start)
        r["ended"] = min(r["ended"], end)
        r["seconds"] = max(0.0, r["ended"] - r["started"])
    out = [r for r in rows if r["seconds"] > 0]
    if cache is not None:
        cache[key] = out
    return out


# A day runs from identity.day_starts_at (default 04:00) to the same time the
# next morning, everywhere: the Today tab, the briefings and the timesheet.
# With midnight as the boundary the Today tab counted 00:00-01:00 of a late
# night as "at the desk" today while the timesheet (already on 04:00) put it
# on yesterday, so the same card said "day 09:05-10:44" and "at desk 2h10m".


def day_start() -> tuple[int, int]:
    from ..util import parse_hhmm
    return parse_hhmm(config.get("identity.day_starts_at", "04:00"))


def current_day(now: datetime | None = None) -> str:
    """Which day it is: before day_starts_at the night still belongs to yesterday."""
    now = now or datetime.now()
    if (now.hour, now.minute) < day_start():
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def day_bounds(day: str) -> tuple[float, float]:
    h, m = day_start()
    start = datetime.strptime(day, "%Y-%m-%d").replace(hour=h, minute=m)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def workday_sql() -> tuple[str, int]:
    """SQL for the day a session belongs to, by day_starts_at, and its one
    parameter: `date(started - offset, 'unixepoch', 'localtime')`. The stored
    `day` column is the calendar date, which is not the day ARGUS reports."""
    h, m = day_start()
    return "date(started - ?, 'unixepoch', 'localtime')", h * 3600 + m * 60


def day_range(day: str | None = None) -> tuple[float, float, str]:
    day = day or current_day()
    s, e = day_bounds(day)
    return s, min(e, now()), day


# --------------------------------------------------------------------------- aggregates


# An idle stretch up to this long is a pause at the desk; longer is time away.
SHORT_BREAK = 30 * 60


def totals(start: float, end: float) -> dict:
    act = sessions(start, end)
    everything = sessions(start, end, include_afk=True)
    active = sum(s["seconds"] for s in act)
    # "At desk" used to add every AFK row, including a whole night of an idle,
    # unlocked machine - 22h "at the desk" on a 7h day, which a morning brief
    # then explained as "check your power settings". Only short pauses count.
    breaks = [s["seconds"] for s in everything if s["afk"]]
    at_desk = active + sum(b for b in breaks if b <= SHORT_BREAK)
    away = sum(b for b in breaks if b > SHORT_BREAK)
    productive = sum(s["seconds"] for s in act if s["category"] in classify.PRODUCTIVE)
    distracting = sum(s["seconds"] for s in act if s["category"] in classify.DISTRACTING)
    work = sum(s["seconds"] for s in act if s["realm"] == "work")
    personal = sum(s["seconds"] for s in act if s["realm"] == "personal")
    return {
        "active_seconds": active,
        "at_desk_seconds": at_desk,
        "idle_seconds": max(0.0, at_desk - active),
        "away_seconds": away,
        "productive_seconds": productive,
        "distracting_seconds": distracting,
        "work_seconds": work,
        "personal_seconds": personal,
        "session_count": len(act),
        "productive_pct": round(100 * productive / active, 1) if active else 0.0,
    }


def by_category(start: float, end: float) -> list[dict]:
    agg: dict[str, float] = defaultdict(float)
    for s in sessions(start, end):
        agg[s["category"] or "other"] += s["seconds"]
    total = sum(agg.values()) or 1
    out = [{"category": k, "seconds": v, "pct": round(100 * v / total, 1),
            "human": human_dur(v)} for k, v in agg.items()]
    return sorted(out, key=lambda d: -d["seconds"])


def by_app(start: float, end: float, limit: int = 15) -> list[dict]:
    agg: dict[str, dict] = {}
    for s in sessions(start, end):
        a = agg.setdefault(s["app"], {"app": s["app"], "seconds": 0.0, "sessions": 0,
                                      "cats": defaultdict(float), "titles": {}})
        a["seconds"] += s["seconds"]
        a["sessions"] += 1
        a["cats"][s["category"] or "other"] += s["seconds"]
        if not s["private"] and s["title"]:
            a["titles"][s["title"]] = a["titles"].get(s["title"], 0) + s["seconds"]
    out = []
    for a in agg.values():
        top = sorted(a["titles"].items(), key=lambda kv: -kv[1])[:3]
        # The category holding most of this app's time. It used to be whichever
        # session came first, so a browser was "browse" or "media" by accident.
        cats = sorted(a["cats"].items(), key=lambda kv: -kv[1])
        out.append({"app": a["app"], "seconds": a["seconds"], "sessions": a["sessions"],
                    "category": cats[0][0] if cats else "other",
                    "categories": {k: round(v) for k, v in cats[:4]},
                    "human": human_dur(a["seconds"]),
                    "top_titles": [t for t, _ in top]})
    return sorted(out, key=lambda d: -d["seconds"])[:limit]


def by_realm(start: float, end: float) -> dict:
    agg: dict[str, float] = defaultdict(float)
    for s in sessions(start, end):
        agg[s["realm"] or "unknown"] += s["seconds"]
    return {k: {"seconds": v, "human": human_dur(v)} for k, v in agg.items()}


# --------------------------------------------------------------------------- focus


def deep_work_blocks(start: float, end: float, min_minutes: int | None = None,
                     gap_tolerance: float = 120) -> list[dict]:
    """Contiguous runs of productive work, allowing brief detours.

    A quick glance at Slack does not end a flow state, so gaps shorter than
    `gap_tolerance` inside a productive run are absorbed into the block.
    """
    min_minutes = min_minutes or int(config.get("focus.deep_work_min_minutes", 25))
    blocks: list[dict] = []
    cur: dict | None = None

    for s in sessions(start, end):
        prod = s["category"] in classify.PRODUCTIVE
        if prod:
            if cur and s["started"] - cur["ended"] <= gap_tolerance:
                cur["ended"] = s["ended"]
                cur["apps"][s["app"]] = cur["apps"].get(s["app"], 0) + s["seconds"]
            else:
                if cur:
                    blocks.append(cur)
                cur = {"started": s["started"], "ended": s["ended"],
                       "apps": {s["app"]: s["seconds"]}}
        elif cur and s["started"] - cur["ended"] > gap_tolerance:
            blocks.append(cur)
            cur = None
    if cur:
        blocks.append(cur)

    out = []
    for b in blocks:
        dur = b["ended"] - b["started"]
        if dur >= min_minutes * 60:
            out.append({
                "started": b["started"], "ended": b["ended"], "seconds": dur,
                "human": human_dur(dur),
                "from": datetime.fromtimestamp(b["started"]).strftime("%H:%M"),
                "to": datetime.fromtimestamp(b["ended"]).strftime("%H:%M"),
                "apps": sorted(b["apps"], key=b["apps"].get, reverse=True)[:3],
            })
    return out


def context_switches(start: float, end: float, bucket_minutes: int = 10) -> list[dict]:
    """App-to-app switches per bucket. High values mean a fragmented head."""
    buckets: dict[int, int] = defaultdict(int)
    prev_app = None
    for s in sessions(start, end):
        if prev_app is not None and s["app"] != prev_app:
            buckets[int(s["started"] // (bucket_minutes * 60))] += 1
        prev_app = s["app"]
    return [{"bucket_ts": k * bucket_minutes * 60, "switches": v}
            for k, v in sorted(buckets.items())]


def focus_score(start: float, end: float) -> dict:
    """0-100. Rewards productive time and long blocks, penalises churn.

    The three components are reported separately so the number is explainable
    rather than a black box you learn to ignore.
    """
    t = totals(start, end)
    active = t["active_seconds"]
    if active < 300:
        return {"score": None, "reason": "not enough activity yet",
                "components": {}, **t}

    prod_ratio = t["productive_seconds"] / active
    blocks = deep_work_blocks(start, end)
    deep_seconds = sum(b["seconds"] for b in blocks)
    deep_ratio = min(1.0, deep_seconds / max(active, 1))

    sw = context_switches(start, end)
    avg_switch = sum(b["switches"] for b in sw) / len(sw) if sw else 0
    churn_penalty = min(1.0, avg_switch / max(1, config.get("focus.switch_storm_per_10min", 25)))

    score = 100 * (0.5 * prod_ratio + 0.35 * deep_ratio + 0.15 * (1 - churn_penalty))
    return {
        "score": round(score),
        "components": {
            "productive_ratio": round(prod_ratio, 3),
            "deep_work_ratio": round(deep_ratio, 3),
            "switch_rate_per_10min": round(avg_switch, 1),
            "churn_penalty": round(churn_penalty, 3),
        },
        "deep_blocks": len(blocks),
        "deep_seconds": deep_seconds,
        "longest_block": max((b["seconds"] for b in blocks), default=0),
        **t,
    }


def distractions(start: float, end: float, limit: int = 8) -> list[dict]:
    names = [n.lower() for n in config.get("focus.distraction_apps", [])]
    agg: dict[str, float] = defaultdict(float)
    for s in sessions(start, end):
        blob = f"{s['app']} {s['title']}".lower()
        if s["category"] in classify.DISTRACTING or any(n in blob for n in names):
            label = s["app"]
            for n in names:
                if n in blob:
                    label = n
                    break
            agg[label] += s["seconds"]
    out = [{"what": k, "seconds": v, "human": human_dur(v)} for k, v in agg.items()]
    return sorted(out, key=lambda d: -d["seconds"])[:limit]


# --------------------------------------------------------------------------- timeline


def timeline(start: float, end: float, slot_minutes: int = 15) -> list[dict]:
    """One row per slot with the dominant category - drives the dashboard strip."""
    slot = slot_minutes * 60
    slots: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in sessions(start, end, include_afk=True):
        cur = s["started"]
        while cur < s["ended"]:
            idx = int(cur // slot)
            nxt = min(s["ended"], (idx + 1) * slot)
            cat = "idle" if s["afk"] else (s["category"] or "other")
            slots[idx][cat] += nxt - cur
            cur = nxt
    out = []
    for idx in range(int(start // slot), int(end // slot) + 1):
        cats = slots.get(idx)
        ts = idx * slot
        if not cats:
            out.append({"ts": ts, "label": datetime.fromtimestamp(ts).strftime("%H:%M"),
                        "category": "off", "seconds": 0})
            continue
        top = max(cats, key=cats.get)
        out.append({"ts": ts, "label": datetime.fromtimestamp(ts).strftime("%H:%M"),
                    "category": top, "seconds": round(cats[top]),
                    "mix": {k: round(v) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])[:3]}})
    return out


def hourly_profile(days: int = 14) -> list[dict]:
    """Average productive minutes per hour-of-day - reveals your real rhythm."""
    since = time.time() - days * 86400
    # Grouped in SQLite: the Python loop formatted a datetime per session,
    # thousands of them, on every Today refresh.
    prod = sorted(classify.PRODUCTIVE)
    per_hour: dict[int, float] = {
        int(r["h"]): r["s"] or 0.0 for r in db.q(
            "SELECT CAST(strftime('%H', started, 'unixepoch', 'localtime') AS INTEGER) h, "
            "SUM(seconds) s FROM activity WHERE started > ? AND afk=0 AND category IN ("
            + ",".join("?" * len(prod)) + ") GROUP BY h", (since, *prod))}
    n = max(1, db.q1(
        "SELECT COUNT(DISTINCT date(started, 'unixepoch', 'localtime')) n FROM activity "
        "WHERE started > ? AND afk=0", (since,))["n"] or 0)
    return [{"hour": h, "avg_minutes": round(per_hour.get(h, 0) / n / 60, 1)}
            for h in range(24)]


def peak_hours(days: int = 14, top: int = 3) -> list[int]:
    prof = hourly_profile(days)
    return [p["hour"] for p in sorted(prof, key=lambda p: -p["avg_minutes"])[:top]]


# --------------------------------------------------------------------------- comparisons


def compare_to_baseline(day: str | None = None, baseline_days: int = 14) -> dict:
    s, e, day = day_range(day)
    todays = totals(s, e)
    hours_elapsed = max(0.5, (e - s) / 3600)

    base_start = s - baseline_days * 86400
    # Grouped by the same day as the window; by calendar date the 14 days
    # from 04:00 were 15 groups, two of them partial, pulling the average down.
    wd, off = workday_sql()
    rows = db.q(
        f"SELECT {wd} wd, SUM(seconds) sec FROM activity "
        "WHERE started >= ? AND started < ? AND afk=0 AND category IN "
        "(" + ",".join("?" * len(classify.PRODUCTIVE)) + ") GROUP BY wd",
        (off, base_start, s, *sorted(classify.PRODUCTIVE)))
    per_day = [r["sec"] for r in rows if r["sec"]]
    avg = sum(per_day) / len(per_day) if per_day else 0.0

    # Compare like with like: scale the baseline to the same elapsed fraction.
    scaled = avg * min(1.0, hours_elapsed / 24)
    delta = todays["productive_seconds"] - scaled
    return {
        "day": day,
        "today_productive": todays["productive_seconds"],
        "baseline_avg_full_day": avg,
        "baseline_scaled": scaled,
        "delta_seconds": delta,
        "delta_human": ("+" if delta >= 0 else "-") + human_dur(abs(delta)),
        "days_in_baseline": len(per_day),
    }


def streaks(metric_days: int = 30) -> dict:
    """Consecutive days with meaningful productive time, current and best."""
    since = time.time() - metric_days * 86400
    wd, off = workday_sql()
    rows = db.q(
        f"SELECT {wd} wd, SUM(seconds) s FROM activity WHERE started > ? AND afk=0 "
        "AND category IN (" + ",".join("?" * len(classify.PRODUCTIVE)) + ") "
        "GROUP BY wd ORDER BY wd", (off, since, *sorted(classify.PRODUCTIVE)))
    good = {r["wd"] for r in rows if (r["s"] or 0) >= 3600}
    cur = best = 0
    d = datetime.strptime(current_day(), "%Y-%m-%d")
    # Today only breaks the streak once it is over: at 9am, with no hour of
    # work logged *yet*, the dashboard used to show a 0-day streak.
    if d.strftime("%Y-%m-%d") not in good:
        d -= timedelta(days=1)
    while True:
        key = d.strftime("%Y-%m-%d")
        if key in good:
            cur += 1
            d -= timedelta(days=1)
        else:
            break
        if cur > 60:
            break
    run = 0
    prev = None
    for r in sorted(good):
        dt = datetime.strptime(r, "%Y-%m-%d")
        run = run + 1 if prev and (dt - prev).days == 1 else 1
        best = max(best, run)
        prev = dt
    return {"current": cur, "best": best, "active_days": len(good)}


# --------------------------------------------------------------------------- one-shot summary


def day_summary(day: str | None = None) -> dict:
    with memo():
        s, e, day = day_range(day)          # inside, so "now" is the pinned one
        return {
            "day": day,
            "window": {"start": s, "end": e},
            "focus": focus_score(s, e),
            "categories": by_category(s, e),
            "apps": by_app(s, e),
            "realms": by_realm(s, e),
            "deep_blocks": deep_work_blocks(s, e),
            "distractions": distractions(s, e),
            "timeline": timeline(s, e),
            "compare": compare_to_baseline(day),
        }


def facts_for_llm(day: str | None = None) -> str:
    with memo():
        return _facts_for_llm(day)


def _facts_for_llm(day: str | None = None) -> str:
    """Compact plain-text digest of a day, cheap enough to paste into a prompt."""
    s, e, day = day_range(day)
    f = focus_score(s, e)
    lines = [f"DATE: {day}"]
    away = f.get("away_seconds", 0)
    lines.append(f"At desk {human_dur(f['at_desk_seconds'])}"
                 + (f" (plus {human_dur(away)} away, idle stretches over 30m)" if away else "")
                 + f", active {human_dur(f['active_seconds'])}, "
                 f"productive {human_dur(f['productive_seconds'])} "
                 f"({f.get('productive_pct', 0)}%)")
    if f.get("score") is not None:
        lines.append(f"Focus score {f['score']}/100, {f['deep_blocks']} deep-work blocks, "
                     f"longest {human_dur(f['longest_block'])}")
    cats = ", ".join(f"{c['category']} {c['human']}" for c in by_category(s, e)[:7])
    lines.append(f"Categories: {cats or 'none'}")
    apps = "; ".join(f"{a['app']} {a['human']}" +
                     (f" [{a['top_titles'][0][:60]}]" if a["top_titles"] else "")
                     for a in by_app(s, e, 8))
    lines.append(f"Apps: {apps or 'none'}")
    r = by_realm(s, e)
    lines.append("Realms: " + ", ".join(f"{k} {v['human']}" for k, v in r.items()))
    d = distractions(s, e)
    if d:
        lines.append("Distractions: " + ", ".join(f"{x['what']} {x['human']}" for x in d))
    blocks = deep_work_blocks(s, e)
    if blocks:
        lines.append("Deep blocks: " + "; ".join(
            f"{b['from']}-{b['to']} ({b['human']}, {'/'.join(b['apps'])})" for b in blocks))

    commits = db.q("SELECT subject, detail FROM events WHERE source='git' AND kind='commit' "
                   "AND ts BETWEEN ? AND ? ORDER BY ts", (s, e))
    if commits:
        lines.append("Commits: " + "; ".join(f"{c['subject']}: {c['detail'][:80]}"
                                             for c in commits[:15]))
    # Build output recorded before the file sensor filtered it would otherwise
    # hand the model "__next._tree.txt, index.txt, ..." as the day's work.
    from ..sensors.files import generated
    names: list[str] = []
    for f in db.q("SELECT subject, detail FROM events WHERE source='file' "
                  "AND kind IN ('modified','created') AND ts BETWEEN ? AND ? "
                  "ORDER BY ts DESC LIMIT 3000", (s, e)):
        if f["subject"] and f["subject"] not in names and not generated(f["detail"] or ""):
            names.append(f["subject"])
            if len(names) >= 25:
                break
    if names:
        lines.append("Files touched: " + ", ".join(names))
    doms = db.q("SELECT subject, COUNT(*) n FROM events WHERE source='browser' "
                "AND ts BETWEEN ? AND ? GROUP BY subject ORDER BY n DESC LIMIT 12", (s, e))
    if doms:
        lines.append("Top sites: " + ", ".join(f"{d['subject']}({d['n']})" for d in doms))
    return "\n".join(lines)
