"""Process lifecycle sensor: notable app launches and exits, plus resource hogs.

Noise control matters here - a Windows box churns dozens of short-lived helper
processes a minute. A launch is an app going from no processes to some (one
that survives a poll, unless it is always notable), an exit is its last
process ending; helpers of a running app and ARGUS's own child processes are
not events. Names on the system-noise list are never reported.
"""
from __future__ import annotations

import os
import threading
import time

import psutil

from .. import classify, config, db, pause, privacy
from ..util import log

lg = log("sensor.process")

NOISE = {
    "conhost.exe", "svchost.exe", "backgroundtaskhost.exe", "runtimebroker.exe",
    "dllhost.exe", "wmiprvse.exe", "audiodg.exe", "taskhostw.exe", "sihost.exe",
    "csrss.exe", "smss.exe", "wininit.exe", "services.exe", "fontdrvhost.exe",
    "ctfmon.exe", "searchprotocolhost.exe", "searchfilterhost.exe", "consent.exe",
    "backgroundtransferhost.exe", "applicationframehost.exe", "shellexperiencehost.exe",
    "systemsettingsbroker.exe", "useroobebroker.exe", "textinputhost.exe",
    "startmenuexperiencehost.exe", "widgets.exe", "widgetservice.exe",
    "crashpad_handler.exe", "msedgewebview2.exe", "gameinputsvc.exe",
    "compattelrunner.exe", "mousocoreworker.exe", "trustedinstaller.exe",
    "tiworker.exe", "usoclient.exe", "sppsvc.exe", "spoolsv.exe",
}

# Processes worth an explicit event even though they are short-lived.
ALWAYS = {"python.exe", "node.exe", "docker.exe", "ssh.exe", "git.exe", "claude.exe"}


def _snapshot() -> dict[int, tuple[str, float, int]]:
    """pid -> (name, create_time, parent pid)."""
    out: dict[int, tuple[str, float, int]] = {}
    for p in psutil.process_iter(["pid", "name", "create_time", "ppid"]):
        try:
            info = p.info
            n = (info["name"] or "").lower()
            if n in NOISE:
                continue
            out[info["pid"]] = (info["name"] or "", info["create_time"] or 0.0,
                                info["ppid"] or 0)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


class AppTracker:
    """Turns process snapshots into app launches and exits.

    Every Chrome tab, VS Code helper and Claude Code shell is its own process,
    and each one that outlived a poll used to be a "start" event: 2,300 Chrome
    starts in three days, plus a git start for every repo ARGUS itself
    scanned. Now an app starts when its first process appears and exits when
    its last one ends, and children of this process are ignored.
    """

    def __init__(self, snapshot: dict, me: int | None = None) -> None:
        self.me = os.getpid() if me is None else me
        self.prev = snapshot
        self.pending: dict[int, tuple[str, float, int]] = {}
        self.live: dict[str, dict] = {}
        self.reseed(snapshot)

    def reseed(self, snapshot: dict) -> None:
        """Whatever runs now is the baseline: none of it is a launch."""
        self.prev, self.pending, self.live = snapshot, {}, {}
        for pid, (name, ctime, _ppid) in snapshot.items():
            e = self.live.setdefault(name.lower(), {"pids": set(), "since": ctime or time.time()})
            e["pids"].add(pid)
            e["since"] = min(e["since"], ctime or e["since"])

    def step(self, cur: dict, now: float | None = None) -> list[tuple]:
        now = now or time.time()
        started = {k: v for k, v in cur.items() if k not in self.prev}
        ended = {k: v for k, v in self.prev.items() if k not in cur}
        rows: list[tuple] = []

        # Launches that survived a full interval, or are always notable.
        confirmed = [(pid, v) for pid, v in self.pending.items() if pid in cur]
        self.pending = {}
        for pid, v in started.items():
            if v[2] == self.me:
                continue                      # our own git / claude / helpers
            if v[0].lower() in ALWAYS:
                confirmed.append((pid, v))
            else:
                self.pending[pid] = v

        for pid, (name, ctime, _ppid) in confirmed:
            e = self.live.get(name.lower())
            if e and e["pids"]:
                e["pids"].add(pid)            # another window / helper of a running app
                continue
            self.live[name.lower()] = {"pids": {pid}, "since": ctime or now}
            app, cat = classify.app_of(name)
            rows.append(("process", "start", app, privacy.scrub(name),
                         None, {"pid": pid, "category": cat}, ctime or now))

        for pid, (name, _ctime, _ppid) in ended.items():
            e = self.live.get(name.lower())
            if not e or pid not in e["pids"]:
                continue
            e["pids"].discard(pid)
            if e["pids"]:
                continue
            del self.live[name.lower()]
            lifetime = max(0.0, now - e["since"])
            if lifetime >= 60:                # only report meaningful sessions ending
                app, _ = classify.app_of(name)
                rows.append(("process", "exit", app, privacy.scrub(name),
                             lifetime, {"pid": pid}, None))
        self.prev = cur
        return rows


# One sample serves every caller for a few seconds: each takes about a second
# of CPU (see below), and the System tab, its refresh button and the watchdog
# can all ask at once.
_SAMPLE_TTL = 5.0
_sample_lock = threading.Lock()
_sample: tuple[float, list[dict]] = (0.0, [])


def top_consumers(n: int = 5, interval: float = 0.5) -> list[dict]:
    """Current heaviest processes by CPU then memory. Used by the watchdog + UI.

    psutil's per-process cpu_percent is measured *since the previous call*, and
    a fresh Process object has no previous call - so the old single pass
    reported 0.0 for everything, and the "CPU pinned" alert named three random
    processes at 0%. Two passes `interval` apart give a real reading. CPU is a
    share of the whole machine (0-100), not of one core.
    """
    global _sample
    with _sample_lock:
        taken, rows = _sample
        if time.monotonic() - taken > _SAMPLE_TTL:
            rows = _measure(interval)
            _sample = (time.monotonic(), rows)
    return rows[:n]


def _measure(interval: float) -> list[dict]:
    # Processes owned by SYSTEM cannot be opened, and for those psutil falls
    # back to enumerating every process on the machine for each value it
    # reads - once for the name/memory pass, once per cpu_percent. Reading CPU
    # and memory together under oneshot() shares one enumeration.
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        if p.info["pid"] == 0:                      # System Idle Process
            continue
        try:
            p.cpu_percent(None)
            procs.append(p)
        except psutil.Error:          # gone, denied, zombie - skip it
            continue
    time.sleep(max(0.05, interval))
    ncpu = psutil.cpu_count() or 1
    rows: list[dict] = []
    for p in procs:
        try:
            with p.oneshot():
                cpu = p.cpu_percent(None) / ncpu
                mi = p.memory_info()
            rows.append({
                "pid": p.info["pid"],
                "name": p.info["name"] or "?",
                "cpu": round(cpu, 1),
                "mem_mb": round((mi.rss if mi else 0) / 1e6, 1),
            })
        except psutil.Error:
            # A process can exit between the two passes in more ways than
            # NoSuchProcess; any psutil error just means "not measurable".
            continue
    rows.sort(key=lambda r: (r["cpu"], r["mem_mb"]), reverse=True)
    return rows


def run(stop) -> None:
    tracker = AppTracker(_snapshot())
    while not stop.is_set():
        poll = float(config.get("sensors.process.poll_seconds", 10))
        try:
            cur = _snapshot()
            if pause.is_paused():
                # Keep the baseline current so resuming does not replay every
                # launch that happened while paused.
                tracker.reseed(cur)
            else:
                db.log_events(tracker.step(cur))
        except Exception:
            lg.exception("process poll failed")
        stop.wait(poll)
    lg.info("process sensor stopped")
