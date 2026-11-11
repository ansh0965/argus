"""Connectivity awareness: know when the internet comes back, and catch up.

A laptop spends real time offline - metro, patchy wifi, hotel captive portals.
Without this module the failure mode is silent and annoying: the 09:00 Google
sync fires while you are on the train, fails, gets marked done, and you get no
mail or calendar data until the next slot hours later.

Three things happen here:

  1. A cheap poll (TCP connect to public DNS, no HTTP, no DNS lookup) tracks
     online/offline, with hysteresis so one dropped packet is not an outage.
  2. A rising edge (offline -> online) fires callbacks. The daemon uses this to
     run every network job immediately instead of waiting for its next slot.
  3. Jobs marked `requires_net` are *skipped without being marked done* while
     offline, so they stay due and fire the moment the link is back.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable

from . import db
from .util import log

lg = log("net")

# TCP 53 against well-known resolvers: no DNS resolution needed (so a poisoned
# or captive-portal resolver cannot fake a positive), no HTTP, ~1 packet.
PROBES = [("1.1.1.1", 53), ("8.8.8.8", 53), ("9.9.9.9", 53)]
PROBE_TIMEOUT = 2.5

# A captive portal answers TCP but not real traffic. This second-stage check
# only runs on a rising edge, so it costs nothing in the steady state.
CAPTIVE_CHECK = ("connectivitycheck.gstatic.com", 80)

_state_lock = threading.Lock()
_online: bool | None = None
_since: float = 0.0
_last_outage: float = 0.0
_fail_streak = 0
_ok_streak = 0
_callbacks: list[tuple[str, Callable[[], object]]] = []

# Consecutive probes required before we believe a change. At a 20s poll that is
# ~40s to declare an outage and ~20s to declare recovery - recovery is
# deliberately twitchier because acting early is cheap and acting late is not.
DOWN_AFTER = 2
UP_AFTER = 1


def _tcp_ok(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def probe() -> bool:
    """True if any probe target answers. Cheap and quiet."""
    for host, port in PROBES:
        if _tcp_ok(host, port):
            return True
    return False


def behind_captive_portal() -> bool:
    """TCP works but real HTTP is intercepted - the hotel-wifi case."""
    try:
        import httpx
        r = httpx.get("http://connectivitycheck.gstatic.com/generate_204",
                      timeout=5, follow_redirects=False)
        return r.status_code != 204
    except Exception:
        return False


def is_online() -> bool:
    """Last known state. Never blocks."""
    with _state_lock:
        if _online is None:
            return True  # assume up until the first probe says otherwise
        return _online


def state() -> dict:
    with _state_lock:
        return {
            "online": True if _online is None else _online,
            "known": _online is not None,
            "since": _since,
            "for_seconds": time.time() - _since if _since else 0,
            "last_outage_seconds": _last_outage,
        }


def on_reconnect(name: str, fn: Callable[[], object]) -> None:
    """Register work to run when the link comes back. Order is registration order."""
    _callbacks.append((name, fn))


def _fire_reconnect() -> None:
    lg.info("link restored - running %d catch-up task(s)", len(_callbacks))
    db.log_event("net", "online", "connectivity", f"{len(_callbacks)} catch-up tasks")
    for name, fn in list(_callbacks):
        t0 = time.time()
        try:
            result = fn()
            ms = int((time.time() - t0) * 1000)
            db.add_run(f"reconnect:{name}", True, ms,
                       "" if result is None else str(result)[:1000])
            lg.info("catch-up %s: %s", name, str(result)[:160])
        except Exception as e:
            db.add_run(f"reconnect:{name}", False, int((time.time() - t0) * 1000),
                       "", str(e)[:800])
            lg.exception("catch-up %s failed", name)


def _set(online: bool) -> bool:
    """Apply a confirmed state. Returns True if this was a rising edge."""
    global _online, _since, _last_outage
    with _state_lock:
        was = _online
        if was == online:
            return False
        now = time.time()
        if online and was is False and _since:
            # Remember how long the outage lasted *before* the clock resets:
            # reconnect callbacks run after this, and "for_seconds" then
            # measures time since coming back - always ~0.
            _last_outage = now - _since
        _online = online
        _since = now
    if was is None:
        lg.info("connectivity: %s", "online" if online else "offline")
        db.set_state("net_online", online)
        return False  # first observation is not an edge
    lg.info("connectivity changed: %s -> %s", was, online)
    db.set_state("net_online", online)
    if not online:
        db.log_event("net", "offline", "connectivity", "link lost")
    return online


def poll_once() -> bool:
    """One probe with hysteresis. Returns the current believed state."""
    global _fail_streak, _ok_streak
    up = probe()
    if up:
        _ok_streak += 1
        _fail_streak = 0
    else:
        _fail_streak += 1
        _ok_streak = 0

    if up and _ok_streak >= UP_AFTER:
        if _set(True):
            if behind_captive_portal():
                lg.warning("captive portal detected - deferring catch-up")
                db.log_event("net", "captive", "connectivity", "portal intercepting")
                with _state_lock:
                    globals()["_online"] = False   # do not claim we are really up
                return False
            _fire_reconnect()
    elif not up and _fail_streak >= DOWN_AFTER:
        _set(False)
    return is_online()


def run_loop(stop, interval: float = 20.0) -> None:
    lg.info("connectivity watcher started")
    while not stop.is_set():
        try:
            poll_once()
        except Exception:
            lg.exception("connectivity poll failed")
        stop.wait(interval)
    lg.info("connectivity watcher stopped")
