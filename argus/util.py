"""Shared helpers: logging, time windows, single-instance lock, safe threads."""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import config
from .paths import LOGS

_configured = False


def log(name: str = "argus") -> logging.Logger:
    global _configured
    if not _configured:
        root = logging.getLogger("argus")
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)-18s %(message)s",
                                "%Y-%m-%d %H:%M:%S")
        fh = logging.handlers.RotatingFileHandler(
            LOGS / "argus.log", maxBytes=4_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        if os.environ.get("ARGUS_CONSOLE") == "1":
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            root.addHandler(sh)
        _configured = True
    return logging.getLogger(f"argus.{name}" if name != "argus" else "argus")


# --------------------------------------------------------------------------- time

def parse_hhmm(s: str | int) -> tuple[int, int]:
    """'HH:MM' -> (h, m), clamped to a real time of day.

    YAML 1.1 reads an unquoted 20:00 as the base-60 integer 1200, so an
    innocent `at: 20:00` in config.yaml arrives here as an int; it is decoded
    back rather than becoming hour 1200 and crashing datetime.replace().
    """
    try:
        if isinstance(s, int) and not isinstance(s, bool):
            h, m = divmod(s, 60)
        else:
            h, _, m = str(s).strip().partition(":")
            h, m = int(h), int(m or 0)
        return min(max(h, 0), 23), min(max(m, 0), 59)
    except Exception:
        return 0, 0


def at_today(hhmm: str, base: datetime | None = None) -> datetime:
    h, m = parse_hhmm(hhmm)
    b = base or datetime.now()
    return b.replace(hour=h, minute=m, second=0, microsecond=0)


def in_window(hhmm_start: str, hhmm_end: str, now: datetime | None = None) -> bool:
    """True if now is inside [start, end), wrapping over midnight when needed."""
    now = now or datetime.now()
    s, e = at_today(hhmm_start, now), at_today(hhmm_end, now)
    if s <= e:
        return s <= now < e
    return now >= s or now < e


def is_quiet_hours(now: datetime | None = None) -> bool:
    qh = config.get("notify.quiet_hours", {}) or {}
    if not qh.get("start") or not qh.get("end"):
        return False
    return in_window(qh["start"], qh["end"], now)


def is_work_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    wh = config.get("identity.work_hours", {}) or {}
    if now.weekday() not in (wh.get("days") or [0, 1, 2, 3, 4]):
        return False
    return in_window(wh.get("start", "10:30"), wh.get("end", "18:30"), now)


def human_dur(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{sec}s"


def human_ago(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def day_str(offset_days: int = 0) -> str:
    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- text

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Not _ or __: briefings name files and branches like update_pages.py.
_MD_EMPH = re.compile(r"(\*\*|\*|`)(?=\S)(.+?)(?<=\S)\1")
_MD_LEAD = re.compile(r"^\s*(?:#{1,6}\s+|[-*+>]\s+|\d+[.)]\s+)+")


def plain(line: str) -> str:
    """One line of a briefing's markdown as text a toast can show (or a voice
    can read): "**The one thing** - push `feat/x`" -> "The one thing - push
    feat/x". Stripping only leading "#* " left "The one thing** - ..."."""
    s = _MD_LEAD.sub("", line)
    s = _MD_LINK.sub(r"\1", s)
    for _ in range(2):                  # **_nested_** emphasis
        s = _MD_EMPH.sub(r"\2", s)
    return s.replace("**", "").strip()


# --------------------------------------------------------------------------- process

def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def run(cmd: list[str] | str, timeout: int = 60, cwd: str | Path | None = None,
        shell: bool = False, input: str | None = None) -> tuple[int, str, str]:
    """Run a command with no console window. Returns (rc, stdout, stderr).

    `input` is fed to stdin; without it stdin is closed, so a child that
    unexpectedly waits for input fails fast instead of hanging until timeout.
    """
    flags = 0
    si = None
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, shell=shell,
            creationflags=flags, startupinfo=si,
            encoding="utf-8", errors="replace",
            input=input, stdin=None if input is not None else subprocess.DEVNULL,
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


class SingleInstance:
    """Windows named mutex so only one daemon runs at a time."""

    def __init__(self, name: str = "Global\\ArgusDaemonMutex") -> None:
        self.name = name
        self.handle = None
        self.acquired = False

    def acquire(self) -> bool:
        try:
            import win32api
            import win32event
            import winerror
            self.handle = win32event.CreateMutex(None, True, self.name)
            self.acquired = win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS
            return self.acquired
        except Exception:
            self.acquired = True
            return True

    def release(self) -> None:
        try:
            if self.handle:
                import win32api
                win32api.CloseHandle(self.handle)
        except Exception:
            pass


def spawn(fn: Callable[[], None], name: str, logger: logging.Logger | None = None) -> threading.Thread:
    """Daemon thread that logs and survives a crash inside fn."""
    lg = logger or log(name)

    def _wrap() -> None:
        try:
            fn()
        except Exception:
            lg.exception("thread %s died", name)

    t = threading.Thread(target=_wrap, name=name, daemon=True)
    t.start()
    return t


class Every:
    """Cheap interval gate for use inside a fast poll loop."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.last = 0.0

    def due(self, now: float | None = None) -> bool:
        now = now or time.time()
        if now - self.last >= self.seconds:
            self.last = now
            return True
        return False
