"""Pause: the one switch every sensor must obey.

Pausing means "do not watch me for a while", so it has two halves:

  - live sensors (window, files, process, network, screenshot) record nothing
    while paused;
  - collectors that read history after the fact (browser history, git log,
    Claude Code transcripts) skip anything timestamped inside a pause, or the
    paused hour would simply be ingested the moment you resume.

The second half needs to know *when* you were paused, not just whether you are
now, so every pause is kept as an interval in the state table.

State lives in the database rather than in memory because pause is set from
three places - the tray, the dashboard and `argus pause` in another process -
and every one of them has to be heard by the daemon.
"""
from __future__ import annotations

import json
import time

from . import db

_UNTIL = "paused_until"
_LOG = "pause_log"
_KEEP = 200


def is_paused(now: float | None = None) -> bool:
    until = float(db.get_state(_UNTIL, 0) or 0)
    return (now or time.time()) < until


def paused_until() -> float:
    return float(db.get_state(_UNTIL, 0) or 0)


def intervals() -> list[tuple[float, float]]:
    """Every recorded pause as (start, end). The current one ends in the future."""
    out = []
    for iv in db.get_state(_LOG, []) or []:
        try:
            out.append((float(iv[0]), float(iv[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def was_paused_at(ts: float, ivs: list[tuple[float, float]] | None = None) -> bool:
    for s, e in (intervals() if ivs is None else ivs):
        if s <= ts < e:
            return True
    return False


def pause(minutes: float = 60) -> str:
    now = time.time()
    until = now + max(0.0, float(minutes)) * 60
    log = [list(iv) for iv in intervals()]
    if log and log[-1][1] > now:
        log[-1][1] = until               # extending a pause already in progress
    else:
        log.append([now, until])
    with db.tx() as c:
        for k, v in ((_UNTIL, until), (_LOG, log[-_KEEP:])):
            c.execute("INSERT INTO state(key,val,ts) VALUES(?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET val=excluded.val, ts=excluded.ts",
                      (k, json.dumps(v), now))
    db.log_event("argus", "paused", "user", f"{minutes:g} minutes")
    return f"paused for {minutes:g} minutes"


def resume() -> str:
    now = time.time()
    log = [list(iv) for iv in intervals()]
    if log and log[-1][1] > now:
        log[-1][1] = now                 # the pause ended early
    with db.tx() as c:
        for k, v in ((_UNTIL, 0), (_LOG, log[-_KEEP:])):
            c.execute("INSERT INTO state(key,val,ts) VALUES(?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET val=excluded.val, ts=excluded.ts",
                      (k, json.dumps(v), now))
    db.log_event("argus", "resumed", "user", "")
    return "resumed"
