"""Foreground-window sensor: the backbone of activity tracking.

Polls the active window a few times a second, coalesces identical (exe, title)
runs into a single `activity` row, and folds idle time into an AFK session so
"8 hours at the desk" never means "8 hours of work".
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta

import psutil
import win32gui
import win32process

from .. import classify, config, db, pause, privacy
from ..util import log

lg = log("sensor.window")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input. No key content is read."""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return max(0.0, (kernel32.GetTickCount() - info.dwTime) / 1000.0)


def session_locked() -> bool:
    """A locked workstation has no foreground window we can own."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower() in ("lockapp.exe", "logonui.exe")
    except Exception:
        return False


def foreground() -> tuple[str, str, int]:
    """Return (exe_name, window_title, pid). Empty exe means nothing focused."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "", "", 0
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            exe = psutil.Process(pid).name()
        except Exception:
            exe = ""
        return exe, title, pid
    except Exception:
        return "", "", 0


class _Session:
    __slots__ = ("app", "exe", "title", "category", "realm", "started",
                 "last", "afk", "private", "row_id", "saved")

    def __init__(self, app: str, exe: str, title: str, category: str, realm: str,
                 afk: bool, private: bool, now: float) -> None:
        self.app, self.exe, self.title = app, exe, title
        self.category, self.realm = category, realm
        self.afk, self.private = afk, private
        self.started = self.last = now
        self.row_id: int | None = None   # set once the session is first written
        self.saved = 0.0                 # when it was last written

    def key(self) -> tuple:
        return (self.exe, self.title, self.afk)

    def public(self) -> dict:
        return {"app": self.app, "exe": self.exe, "title": self.title,
                "category": self.category, "realm": self.realm, "afk": int(self.afk),
                "private": int(self.private), "started": self.started,
                "ended": self.last, "seconds": self.last - self.started,
                "ongoing_seconds": self.last - self.started, "live": True}


# A session used to reach the database only when it ended. So a three-hour
# stretch in one editor window was missing from today's totals until you
# switched away, the dashboard's "right now" showed the window *before* it,
# and killing the daemon lost the whole thing. Now an open session is written
# once it passes min_session_seconds and extended every CHECKPOINT seconds.
CHECKPOINT = 60.0

_current: dict = {}
_current_lock = threading.Lock()


def current() -> dict:
    """The session in progress right now, when this process runs the sensor."""
    with _current_lock:
        return dict(_current)


def _publish(s: "_Session | None") -> None:
    with _current_lock:
        _current.clear()
        if s is not None:
            _current.update(s.public())
            _current["seen"] = time.time()


def _save(s: _Session | None, min_seconds: float) -> None:
    """Write or extend a session. Safe to call as often as you like."""
    if s is None:
        return
    if s.row_id is None:
        if s.last - s.started < min_seconds:
            return
        s.row_id = db.add_activity(s.app, s.exe, s.title, s.category, s.realm,
                                   s.started, s.last, s.afk, s.private)
    else:
        db.extend_activity(s.row_id, s.last)
    s.saved = s.last


def _flush(s: _Session | None, min_seconds: float) -> None:
    """Close a session for good."""
    _save(s, min_seconds)


def _next_midnight(ts: float) -> float:
    d = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return (d + timedelta(days=1)).timestamp()


def run(stop) -> None:
    """Blocking loop. `stop` is a threading.Event."""
    cur: _Session | None = None
    last_state = ""
    was_paused = False

    while not stop.is_set():
        cfg = config.get("sensors.window", {})
        poll = float(cfg.get("poll_seconds", 2))
        min_sec = float(cfg.get("min_session_seconds", 3))
        afk_after = float(config.get("sensors.idle.afk_seconds", 180))
        now = time.time()

        # Pause must actually stop the watching, not just recolour the tray
        # icon. Flush whatever is open so no half-session is lost, then record
        # nothing at all until the user resumes.
        if pause.is_paused():
            if not was_paused:
                if cur is not None:
                    cur.last = now
                    _flush(cur, min_sec)
                    cur = None
                _publish(None)
                db.log_event("presence", "paused", "Away", "monitoring paused")
                lg.info("window sensor paused - recording nothing")
                was_paused = True
            stop.wait(min(poll * 5, 15))
            continue
        if was_paused:
            lg.info("window sensor resumed")
            db.log_event("presence", "resumed", "", "monitoring resumed")
            was_paused = False
            last_state = ""

        try:
            idle = idle_seconds()
            locked = session_locked()
            afk = locked or idle >= afk_after

            if afk:
                exe, title = ("<locked>", "Locked") if locked else ("<idle>", "Idle")
                app, cat, realm = ("Away", "idle", "unknown")
                private = False
            else:
                exe, title, _pid = foreground()
                if not exe:
                    stop.wait(poll)
                    continue
                app, cat, realm = classify.classify(exe, title)
                title, private = privacy.clean_title(exe, app, title)

            state = "afk" if afk else "active"
            if state != last_state:
                db.log_event("presence", state, app,
                             f"idle {int(idle)}s" if afk else title, value=idle)
                last_state = state

            if cur is not None and now >= _next_midnight(cur.started):
                # Split at midnight so each day owns exactly its own seconds;
                # `day` is stamped from `started`, and a session running from
                # 23:00 to 02:00 used to land entirely on the first day.
                cut = _next_midnight(cur.started)
                cur.last = cut
                _flush(cur, 0)
                cur = _Session(cur.app, cur.exe, cur.title, cur.category, cur.realm,
                               cur.afk, cur.private, cut)

            if cur is None or cur.key() != (exe, title, afk):
                # Backdate the boundary so no wall-clock time is lost.
                if cur is not None:
                    cur.last = now
                    _flush(cur, min_sec)
                cur = _Session(app, exe, title, cat, realm, afk, private, now)
            else:
                cur.last = now
                if now - cur.saved >= CHECKPOINT:
                    _save(cur, min_sec)
            _publish(cur)

        except Exception:
            lg.exception("window poll failed")

        stop.wait(poll)

    if cur is not None:
        cur.last = time.time()
        _flush(cur, 0)
    _publish(None)
    lg.info("window sensor stopped")
