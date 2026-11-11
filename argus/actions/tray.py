"""System tray presence: a visible reminder that ARGUS is on, and a kill switch.

The icon colour is the status at a glance:
  cyan   - running normally
  amber  - a sensor is down or the brain is unreachable
  grey   - paused
"""
from __future__ import annotations

import threading
import time
import webbrowser
from datetime import datetime
from typing import Callable

from .. import config, db
# Pause lives in argus.pause so sensors can obey it without importing the tray;
# the names are re-exported for the callers that already use tray.pause().
from ..pause import is_paused, pause, paused_until, resume  # noqa: F401
from ..util import human_dur, log, plain

lg = log("actions.tray")

_icon = None
_lock = threading.Lock()

COLORS = {"ok": (34, 211, 238), "warn": (251, 191, 36), "paused": (120, 120, 130)}


def _image(state: str = "ok"):
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = COLORS.get(state, COLORS["ok"])
    d.ellipse([2, 2, size - 3, size - 3], outline=c + (255,), width=4)
    d.ellipse([20, 20, size - 21, size - 21], fill=c + (255,))
    if state == "paused":
        d.rectangle([26, 24, 30, 40], fill=(20, 20, 24, 255))
        d.rectangle([34, 24, 38, 40], fill=(20, 20, 24, 255))
    return img


def balloon(title: str, body: str) -> bool:
    with _lock:
        ic = _icon
    if ic is None:
        return False
    try:
        ic.notify(body[:200], title[:64])
        return True
    except Exception:
        return False


def tooltip(paused_to: float = 0.0, status: dict | None = None,
            productive: float | None = None, now: float | None = None) -> str:
    """The hover text: what the icon colour means, in words. It used to say
    "ARGUS" whatever the state, so amber or grey had to be decoded from the
    README. Windows cuts tooltips at 127 characters."""
    now = now or time.time()
    if paused_to > now:
        return f"ARGUS - paused until {datetime.fromtimestamp(paused_to):%H:%M}"
    bits = []
    if status:
        dead = sorted(k for k, v in (status.get("sensors") or {}).items()
                      if not v.get("alive") and v.get("reason") != "disabled")
        if dead:
            bits.append(f"{', '.join(dead)} sensor down" if len(dead) < 3
                        else f"{len(dead)} sensors down")
        if not (status.get("net") or {}).get("online", True):
            bits.append("offline")
    if not bits:
        bits.append("watching")
    if productive is not None:
        bits.append(f"{human_dur(productive)} productive today")
    return ("ARGUS - " + " · ".join(bits))[:127]


def _open_dashboard(*_a) -> None:
    webbrowser.open(f"http://127.0.0.1:{config.get('daemon.web_port', 7842)}")


def run(stop, status_fn: Callable[[], dict] | None = None) -> None:
    global _icon
    try:
        import pystray
    except Exception as e:
        lg.warning("pystray unavailable: %s", e)
        stop.wait()
        return

    def _quit(icon, _item) -> None:
        stop.set()
        icon.stop()

    def _brief(*_a) -> None:
        from ..brain import digest
        threading.Thread(
            target=lambda: _show(digest.morning_brief()), daemon=True).start()

    def _wrap(*_a) -> None:
        from ..brain import digest
        threading.Thread(
            target=lambda: _show(digest.evening_wrap()), daemon=True).start()

    def _show(d: dict) -> None:
        from . import notify
        first = next((l for l in d["body"].splitlines() if l.strip()), "")
        notify.send(d["title"], plain(first)[:180], force=True, record=False,
                    url=notify.dashboard("ask"))
        _open_dashboard()

    def _status_text(_item) -> str:
        if is_paused():
            return "Status: paused"
        if not status_fn:
            return "Status: running"
        try:
            s = status_fn()
            dead = [k for k, v in s["sensors"].items()
                    if not v["alive"] and v.get("reason") != "disabled"]
            alive = sum(1 for v in s["sensors"].values() if v["alive"])
            return (f"Status: {alive} sensors up"
                    + (f", {len(dead)} down" if dead else "")
                    + ("" if s.get("net", {}).get("online", True) else ", offline")
                    + f" | {s['db_mb']} MB")
        except Exception:
            return "Status: unknown"

    def _health_text(_item) -> str:
        # Read from the summary the health_check job keeps; menus must open
        # instantly, and a live check can touch the network.
        h = db.get_state("health_summary", {}) or {}
        fails = h.get("fails") or []
        if fails:
            return f"Health: {len(fails)} problem(s) - open"
        return "Health: all good" if h else "Health check"

    def _open_health(*_a) -> None:
        webbrowser.open(f"http://127.0.0.1:{config.get('daemon.web_port', 7842)}/#system")

    menu = pystray.Menu(
        pystray.MenuItem(_status_text, None, enabled=False),
        pystray.MenuItem(_health_text, _open_health),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open dashboard", _open_dashboard, default=True),
        pystray.MenuItem("Morning brief now", _brief),
        pystray.MenuItem("Evening wrap now", _wrap),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Pause 15 minutes", lambda *_: (pause(15), _refresh())),
        pystray.MenuItem("Pause 1 hour", lambda *_: (pause(60), _refresh())),
        pystray.MenuItem("Pause 8 hours", lambda *_: (pause(480), _refresh())),
        pystray.MenuItem("Resume", lambda *_: (resume(), _refresh())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit ARGUS", _quit),
    )

    icon = pystray.Icon("argus", _image("ok"), "ARGUS", menu)
    with _lock:
        _icon = icon

    def _refresh() -> None:
        try:
            icon.icon = _image("paused" if is_paused() else "ok")
        except Exception:
            pass

    def _productive() -> float | None:
        try:
            from ..brain import insights
            s, e, _ = insights.day_range()
            return insights.totals(s, e)["productive_seconds"]
        except Exception:
            return None

    def _watch() -> None:
        last = last_tip = None
        while not stop.is_set():
            state = "paused" if is_paused() else "ok"
            s = None
            if status_fn and state == "ok":
                try:
                    s = status_fn()
                    # amber: a sensor is down, or the machine is offline (the
                    # README always promised the second; nothing checked it)
                    sensor_down = any(not v["alive"] and v.get("reason") != "disabled"
                                      for v in s["sensors"].values())
                    offline = not s.get("net", {}).get("online", True)
                    if sensor_down or offline:
                        state = "warn"
                except Exception:
                    pass
            if state != last:
                try:
                    icon.icon = _image(state)
                except Exception:
                    pass
                last = state
            tip = tooltip(paused_until(), s, None if state == "paused" else _productive())
            if tip != last_tip:
                try:
                    icon.title = tip
                except Exception:
                    pass
                last_tip = tip
            stop.wait(30)
        try:
            icon.stop()
        except Exception:
            pass

    threading.Thread(target=_watch, name="tray.watch", daemon=True).start()
    lg.info("tray started")
    try:
        icon.run()
    except Exception:
        lg.exception("tray crashed")
    lg.info("tray stopped")
