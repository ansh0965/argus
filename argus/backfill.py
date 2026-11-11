"""Reconstruct history from evidence that already exists on this machine.

ARGUS only starts seeing your screen the day you install it, which makes the
first week of briefings useless - it has no baseline to compare against. But the
machine already holds months of proof of what you were doing:

  browser history   -> when you were reading and what about
  Claude Code logs  -> when you were in a coding session, and in which project
  git commits       -> what actually shipped

These are reconstructed into `activity` rows marked `exe='<inferred:...>'`, so
they are always distinguishable from real observation. Backfill is idempotent
and never overwrites a day the live sensor already recorded.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from . import classify, db, privacy
from .sensors import browser
from .util import log

lg = log("backfill")

INFERRED_BROWSER = "<inferred:browser>"
INFERRED_CLAUDE = "<inferred:claude>"
INFERRED = (INFERRED_BROWSER, INFERRED_CLAUDE)

# A visit or log line implies presence for at most this long before we assume
# the person walked away.
MAX_IMPLIED_SECONDS = 8 * 60
MIN_IMPLIED_SECONDS = 25
SESSION_GAP = 15 * 60

_TS = re.compile(rb'"timestamp":"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')


def real_days() -> set[str]:
    """Days that already have genuine sensor observations - never touch these."""
    rows = db.q("SELECT DISTINCT day FROM activity WHERE exe NOT IN (?,?)", INFERRED)
    return {r["day"] for r in rows}


def clear_inferred(day: str | None = None) -> int:
    with db.tx() as c:
        if day:
            return c.execute("DELETE FROM activity WHERE exe IN (?,?) AND day=?",
                             (*INFERRED, day)).rowcount
        return c.execute("DELETE FROM activity WHERE exe IN (?,?)", INFERRED).rowcount


def _emit(rows: list[tuple], skip_days: set[str]) -> int:
    """rows: (app, exe, title, category, realm, started, ended)."""
    n = 0
    for app, exe, title, cat, realm, start, end in rows:
        day = db.today(start)
        if day in skip_days:
            continue
        db.add_activity(app, exe, title, cat, realm, start, end, afk=False, private=False)
        n += 1
    return n


# --------------------------------------------------------------------------- browser

def from_browser(days: int = 90) -> int:
    """Turn browsing history into browsing sessions."""
    since = time.time() - days * 86400
    skip = real_days()
    total = 0

    for label, path in browser.profiles():
        visits = browser._read_since(path, since, limit=200000)
        if not visits:
            continue
        visits.sort(key=lambda v: v["ts"])

        rows: list[tuple] = []
        for i, v in enumerate(visits):
            host = privacy.host_of(v["url"])
            if not host:
                continue
            nxt = visits[i + 1]["ts"] if i + 1 < len(visits) else v["ts"] + MIN_IMPLIED_SECONDS
            span = max(MIN_IMPLIED_SECONDS, min(MAX_IMPLIED_SECONDS, nxt - v["ts"]))

            if privacy.is_sensitive_domain(host):
                title = "<private>"
            else:
                title = privacy.scrub(v["title"] or host)[:300]
            cat = classify.category_from_title(f"{host} {title}", "browse")
            realm = classify.realm_of(f"{host} {title}")
            app = label.split("/")[0]
            rows.append((app, INFERRED_BROWSER, title, cat, realm, v["ts"], v["ts"] + span))

        total += _emit(rows, skip)
    lg.info("browser backfill wrote %d sessions", total)
    return total


# --------------------------------------------------------------------------- claude code

def claude_projects_dir() -> Path:
    return Path("~/.claude/projects").expanduser()


def _project_label(folder: str) -> tuple[str, str]:
    """'C--Users-Jane-Doe-Desktop-atlas' -> ('atlas', realm).

    Claude Code names a transcript folder after the working directory with
    every separator turned into a dash, so the pieces of the home folder
    (drive, "Users", the account name) are dropped to leave the project.
    """
    parts = [p for p in folder.split("-") if p]
    skip = {"c", "users", "desktop", "windows", "system32",
            "argus", "data"}        # argus's own LLM scratch dir is not a project
    skip.update(p.lower() for p in re.split(r"[^A-Za-z0-9]+", str(Path.home())) if p)
    tail = [p for p in parts if p.lower() not in skip]
    name = "-".join(tail) or "home"
    return name[:40], classify.realm_of(folder.replace("-", " "))


def from_claude_sessions(days: int = 120) -> int:
    """Each Claude Code transcript is a record of real, focused work."""
    root = claude_projects_dir()
    if not root.is_dir():
        return 0
    since = time.time() - days * 86400
    skip = real_days()
    total = 0

    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        name, realm = _project_label(proj.name)
        stamps: list[float] = []
        for f in proj.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < since:
                    continue
                raw = f.read_bytes()
            except Exception:
                continue
            for m in _TS.finditer(raw):
                try:
                    # Transcript stamps are UTC; convert to local epoch.
                    dt = datetime.strptime(m.group(1).decode(), "%Y-%m-%dT%H:%M:%S")
                    ts = dt.replace(tzinfo=__import__("datetime").timezone.utc).timestamp()
                except Exception:
                    continue
                if ts >= since:
                    stamps.append(ts)

        if not stamps:
            continue
        stamps.sort()

        rows: list[tuple] = []
        start = prev = stamps[0]
        for ts in stamps[1:]:
            if ts - prev > SESSION_GAP:
                if prev - start >= 60:
                    rows.append(("Claude Code", INFERRED_CLAUDE, name, "code",
                                 realm, start, prev))
                start = ts
            prev = ts
        if prev - start >= 60:
            rows.append(("Claude Code", INFERRED_CLAUDE, name, "code", realm, start, prev))

        total += _emit(rows, skip)
    lg.info("claude backfill wrote %d sessions", total)
    return total


# --------------------------------------------------------------------------- git

def from_git(days: int = 120) -> int:
    """Log historical commits as events so briefings can cite them."""
    from .sensors import git
    since = time.time() - days * 86400
    rows = []
    seen = {(r["subject"], r["detail"]) for r in db.q(
        "SELECT subject, detail FROM events WHERE source='git' AND kind='commit'")}

    for path in git.discover():
        name = Path(path).name
        for c in git.commits_since(path, since):
            key = (name, c["subject"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(("git", "commit", name, c["subject"], None,
                         {"sha": c["sha"], "author": c["author"], "path": path,
                          "backfilled": True}, c["ts"]))
    db.log_events(rows)
    lg.info("git backfill wrote %d commits", len(rows))
    return len(rows)


# --------------------------------------------------------------------------- entry

def run_all(days: int = 90, reset: bool = True) -> dict[str, int]:
    if reset:
        cleared = clear_inferred()
    else:
        cleared = 0
    out = {
        "cleared": cleared,
        "browser": from_browser(days),
        "claude": from_claude_sessions(days),
        "git": from_git(days),
    }
    covered = db.q1("SELECT COUNT(DISTINCT day) n FROM activity")
    out["days_covered"] = covered["n"] if covered else 0
    db.set_state("backfill_ran", time.time())
    return out
