"""The ARGUS supervisor: starts every sensor, the scheduler, the web UI and the tray.

Run it with:  argus start          (background, no console)
              argus run            (foreground, logs to console - for debugging)
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime

from . import config, db, net
from .actions import notify
from .automations import builtin as auto
from .brain import digest
from .gsuite import auth as gauth
from .gsuite import gcal, gdrive, gmail, gtasks
from .paths import harden_secrets
from .scheduler import Scheduler
from .sensors import browser, files, git, network, process, screenshot, system, window
from .util import SingleInstance, log, plain, spawn

lg = log("daemon")

STOP = threading.Event()
SENSOR_HEALTH: dict[str, dict] = {}
STARTED_AT = time.time()
SCHED = Scheduler()


# --------------------------------------------------------------------------- sensors

SENSORS = [
    ("window", "sensors.window.enabled", lambda: window.run(STOP)),
    ("system", "sensors.system.enabled", lambda: system.run(STOP)),
    ("process", "sensors.process.enabled", lambda: process.run(STOP)),
    ("files", "sensors.files.enabled", lambda: files.run(STOP)),
    ("browser", "sensors.browser.enabled", lambda: browser.run(STOP)),
    ("git", "sensors.git.enabled", lambda: git.run_loop(STOP)),
    ("network", "sensors.network.enabled", lambda: network.run_loop(STOP)),
    ("screenshot", "sensors.screenshot.enabled", lambda: screenshot.run(STOP)),
]


def start_sensors() -> None:
    for name, flag, fn in SENSORS:
        if not config.get(flag, False) and name != "screenshot":
            SENSOR_HEALTH[name] = {"alive": False, "reason": "disabled"}
            lg.info("sensor %s disabled", name)
            continue
        t = spawn(fn, f"sensor.{name}")
        SENSOR_HEALTH[name] = {"alive": True, "thread": t, "started": time.time()}
        lg.info("sensor %s started", name)


def _refresh_health() -> None:
    for name, h in SENSOR_HEALTH.items():
        t = h.get("thread")
        if t is not None:
            h["alive"] = t.is_alive()


# A sensor whose thread dies used to stay dead until someone restarted ARGUS:
# the heartbeat raised an alert and that was all. Every sensor loop is safe to
# start again, so the supervisor does, a bounded number of times per hour - a
# sensor that keeps crashing is left down (and alerted on) rather than spun.
MAX_RESTARTS_PER_HOUR = 3


# The threads the daemon cannot do without. The heartbeat that revives sensors
# runs *on* the scheduler, so these are watched from the main loop instead.
CORE: dict[str, dict] = {}


def start_core(name: str, fn) -> None:
    CORE[name] = {"fn": fn, "thread": spawn(fn, name), "restarts": []}


def revive_core(now: float | None = None) -> list[str]:
    """Restart a dead web server, scheduler or connectivity watcher."""
    now = now or time.time()
    revived = []
    for name, h in CORE.items():
        if h["thread"].is_alive() or STOP.is_set():
            continue
        recent = [ts for ts in h["restarts"] if now - ts < 3600]
        if len(recent) >= MAX_RESTARTS_PER_HOUR:
            h["restarts"] = recent
            continue
        lg.warning("core thread %s died - restarting it", name)
        if name == "web":
            time.sleep(1)                 # let the dead server release its port
        h.update({"thread": spawn(h["fn"], name), "restarts": recent + [now]})
        revived.append(name)
    return revived


def revive_sensors(now: float | None = None) -> list[str]:
    """Restart dead, enabled sensor threads. Returns the names restarted."""
    now = now or time.time()
    revived = []
    fns = {name: fn for name, _flag, fn in SENSORS}
    for name, h in SENSOR_HEALTH.items():
        t = h.get("thread")
        if t is None or t.is_alive() or name not in fns or STOP.is_set():
            continue
        recent = [ts for ts in h.get("restarts", []) if now - ts < 3600]
        if len(recent) >= MAX_RESTARTS_PER_HOUR:
            h["restarts"] = recent
            h["alive"] = False
            h["reason"] = f"crashed {len(recent)} times in the last hour; left down"
            continue
        lg.warning("sensor %s died - restarting it (%d restart(s) this hour)",
                   name, len(recent) + 1)
        h.update({"thread": spawn(fns[name], f"sensor.{name}"), "alive": True,
                  "restarts": recent + [now], "reason": None})
        revived.append(name)
    return revived


# --------------------------------------------------------------------------- google jobs


def google_connected() -> bool:
    return gauth.is_connected()


def google_sync() -> str:
    """Sync every connected account, honouring its per-account feature list.

    One account failing (an expired token, a Workspace admin revoking access)
    must not stop the others, so each surface is attempted independently and
    errors are reported rather than raised.
    """
    accts = gauth.accounts()
    if not accts:
        return "no accounts connected"

    out = []
    dead = [a for a in accts if a["state"] == "expired"]
    if dead:
        # A dead token is silent otherwise: syncs just stop returning data and
        # you notice days later that the calendar went stale. Say it out loud -
        # once for all of them, since they usually die together.
        names = ", ".join(a["label"] for a in dead)
        fix = ("argus google relogin" if len(dead) > 1
               else f"argus google login --label {dead[0]['label']} --force")
        notify.send(
            f"Google sign-in needed: {names}",
            f"{len(dead)} account(s) stopped authorising ARGUS. Run: {fix}. "
            f"If this recurs weekly, the OAuth app is in Testing mode - see "
            f"`argus doctor`.",
            severity="warn", rule="google_token_expired",
            cooldown_seconds=24 * 3600, url=notify.dashboard("google"))
    for a in accts:
        label = a["label"]
        if a["state"] == "expired":
            out.append(f"{label}: SIGN-IN NEEDED (argus google login --label {label} --force)")
            continue
        if not a["valid"]:
            # DNS, TLS, a timeout: nothing wrong with the account, try next round.
            out.append(f"{label}: unreachable ({a['reason'][:60]})")
            continue
        parts = []
        if gauth.enabled(label, "calendar"):
            try:
                parts.append(f"cal {gcal.sync(label)}")
                db.set_state("sync_ok:calendar", time.time())
            except Exception as e:
                parts.append(f"cal ERR {str(e)[:60]}")
        if gauth.enabled(label, "mail"):
            try:
                n = gmail.sync(label)
                parts.append(f"mail {n}")
                db.set_state("sync_ok:mail", time.time())
                if n:
                    gmail.triage()
            except Exception as e:
                parts.append(f"mail ERR {str(e)[:60]}")
        if gauth.enabled(label, "tasks"):
            try:
                parts.append(f"tasks {gtasks.sync(label)}")
            except Exception as e:
                parts.append(f"tasks ERR {str(e)[:60]}")
        out.append(f"{label}[{', '.join(parts) or 'nothing enabled'}]")
    return "; ".join(out)


def drive_sync() -> str:
    accts = [a for a in gauth.accounts() if a["valid"] and gauth.enabled(a["label"], "drive")]
    if not accts:
        return "no accounts with drive enabled"
    out = []
    for a in accts:
        try:
            out.append(f"{a['label']} {gdrive.sync(a['label'])}")
        except Exception as e:
            out.append(f"{a['label']} ERR {str(e)[:60]}")
    return "; ".join(out)


def meeting_sentinel() -> str:
    return gcal.sentinel() if google_connected() else "not connected"


def _gmail_link(m: dict) -> str:
    """Open that thread, in the right account, in Gmail on the web."""
    from urllib.parse import quote
    who = gauth.email_of(m.get("account") or "") if m.get("account") else ""
    base = "https://mail.google.com/mail/u/" + (f"?authuser={quote(who)}" if who else "0/")
    return f"{base}#all/{quote(m['thread_id'])}" if m.get("thread_id") else base


def inbox_alert() -> str:
    """Speak up only for genuinely important unread mail, at most a few times a day."""
    if not google_connected() or not config.get("automations.inbox_triage.enabled", True):
        return "off"
    important = gmail.important_unread(limit=5)
    fresh = [m for m in important if not m["seen"]]
    if not fresh:
        return "nothing new"
    top = fresh[0]
    others = f" (+{len(fresh) - 1} more)" if len(fresh) > 1 else ""
    # With several accounts connected, "who is this for" is the first thing
    # you need from the toast.
    which = f"[{top['account']}] " if top.get("account") else ""
    shown = notify.send(f"{which}Mail: {top['sender'][:36]}",
                        f"{top['subject'][:110]}{others}",
                        severity="warn" if top["triage"] == "vip" else "info",
                        rule="inbox_important", cooldown_seconds=90 * 60,
                        url=_gmail_link(top))
    if not shown:
        # Held back by the cooldown, quiet hours or a pause. These used to be
        # marked seen regardless, so a mail arriving within 90 minutes of the
        # previous alert was never announced at all. Keep them for next time.
        return f"{len(fresh)} waiting (not shown yet)"
    with db.tx() as c:
        c.executemany("UPDATE gmail SET seen=1 WHERE id=?", [(m["id"],) for m in fresh])
    return f"{len(fresh)} flagged"


# --------------------------------------------------------------------------- digests


def _digest_and_notify(fn, rule: str, speak: bool) -> str:
    d = fn()
    first = next((l for l in d["body"].splitlines() if l.strip()), d["title"])
    notify.send(d["title"], plain(first)[:180], severity="info",
                rule=rule, force=True, speak=speak, url=notify.dashboard("ask"))
    return d["title"]


def morning() -> str:
    return _digest_and_notify(digest.morning_brief, "morning_brief",
                              config.get("notify.voice", False))


def evening() -> str:
    return _digest_and_notify(digest.evening_wrap, "evening_wrap", False)


def weekly() -> str:
    return _digest_and_notify(digest.weekly_retro, "weekly_retro", False)


# --------------------------------------------------------------------------- job wiring


def _quiet(*prefixes: str):
    return lambda summary: summary.startswith(prefixes)


# Summaries that mean "nothing happened", per job. Such runs are not logged, and
# the dashboard hides any recorded before this existed.
QUIET = {
    "heartbeat": _quiet("ok"),
    "meeting_sentinel": _quiet("nothing due", "not connected", "disabled"),
    "focus_guardian": _quiet("clear", "disabled", "in a meeting", "focusing"),
    "break_reminder": _quiet("streak", "no streak", "no current streak", "disabled",
                             "in a meeting", "focusing"),
    "system_watchdog": _quiet("all clear", "no samples", "disabled"),
    "inbox_alert": lambda s: s.startswith(("nothing new", "off")) or "waiting" in s,
    # Every account expired: nothing was attempted, and the daily alert already
    # says so - repeating it in the diary every five minutes adds nothing.
    "google_sync": lambda s: bool(s) and all("SIGN-IN NEEDED" in p for p in s.split("; ")),
    "drive_sync": _quiet("no accounts with drive enabled"),
    "standup_writer": _quiet("not a work day", "disabled"),
    "health_check": _quiet("ok"),
    "reminders": _quiet("nothing due"),
    "drift_check": _quiet("on track", "away", "in a meeting", "focusing", "disabled"),
    "github_sync": _quiet("nothing new", "disabled", "gh not logged in"),
    "google_expiry_check": _quiet("nothing due", "published"),
    "repo_hygiene": _quiet("clean"),
    "branch_cleanup": _quiet("nothing new"),
}


def is_quiet_run(name: str, ok: bool, summary: str) -> bool:
    q = QUIET.get(name)
    return bool(ok and q and q(summary or ""))


def _repo_hygiene() -> str:
    from . import devtools
    if not config.get("devtools.hygiene", True):
        return "clean"
    return devtools.hygiene_scan()


def _branch_cleanup() -> str:
    from . import devtools
    return devtools.branch_cards()


def _github_sync() -> str:
    from . import github
    return github.sync()


def build_schedule() -> Scheduler:
    # Built on a scratch scheduler and swapped in whole, so the web thread
    # reading SCHED.jobs never sees a half-built list.
    s = Scheduler()
    d = config.get("digests", {})

    # fast loops - their no-op runs stay out of the run log
    s.every(60, "heartbeat", auto.heartbeat, run_on_start=True, quiet=QUIET["heartbeat"])
    s.every(90, "meeting_sentinel", meeting_sentinel,
            enabled=lambda: config.get("automations.meeting_sentinel.enabled", True),
            quiet=QUIET["meeting_sentinel"])
    s.every(5 * 60, "focus_guardian", auto.focus_guardian,
            enabled=lambda: config.get("automations.focus_guardian.enabled", True),
            quiet=QUIET["focus_guardian"])
    s.every(10 * 60, "break_reminder", auto.break_reminder,
            enabled=lambda: config.get("automations.break_reminder.enabled", True),
            quiet=QUIET["break_reminder"])
    s.every(5 * 60, "system_watchdog", auto.system_watchdog,
            enabled=lambda: config.get("automations.system_watchdog.enabled", True),
            quiet=QUIET["system_watchdog"])
    # "Where you left off" is the dashboard's first tab; it was only ever
    # re-derived when someone ran `argus resume` or pressed re-derive.
    s.every(30 * 60, "threads_rebuild", auto.threads_rebuild, run_on_start=True,
            enabled=lambda: config.get("threads.enabled", True))
    s.every(15 * 60, "health_check", auto.health_check, run_on_start=True,
            quiet=QUIET["health_check"])

    # google - all of these are pointless without a link, so they defer instead
    # of burning their slot while offline
    s.every(int(config.get("google.poll_seconds", 300)), "google_sync", google_sync,
            enabled=lambda: config.get("google.enabled", True) and google_connected(),
            requires_net=True, quiet=QUIET["google_sync"])
    s.every(20 * 60, "inbox_alert", inbox_alert, enabled=google_connected,
            requires_net=True, quiet=QUIET["inbox_alert"])
    s.every(60 * 60, "drive_sync", drive_sync, enabled=google_connected,
            requires_net=True, quiet=QUIET["drive_sync"])

    # slower housekeeping
    s.every(4 * 3600, "repo_janitor", auto.repo_janitor,
            enabled=lambda: config.get("automations.repo_janitor.enabled", True))
    s.every(6 * 3600, "security_watch", auto.security_watch,
            enabled=lambda: config.get("automations.security_watch.enabled", True))
    s.daily("03:30", "maintenance", auto.maintenance)
    s.daily("03:45", "backup", auto.backup, catch_up_hours=20,
            enabled=lambda: config.get("backup.enabled", True))
    s.every(60, "reminders", auto.reminders, run_on_start=True, quiet=QUIET["reminders"],
            enabled=lambda: config.get("board.enabled", True))
    s.every(15 * 60, "github_sync", _github_sync, run_on_start=True, requires_net=True,
            quiet=QUIET["github_sync"], enabled=lambda: config.get("github.enabled", True))
    s.every(3600, "google_expiry_check", auto.google_expiry_check, quiet=QUIET["google_expiry_check"])
    s.every(10 * 60, "repo_hygiene", _repo_hygiene, quiet=QUIET["repo_hygiene"])
    s.daily("10:05", "branch_cleanup", _branch_cleanup, catch_up_hours=12,
            quiet=QUIET["branch_cleanup"])
    s.every(5 * 60, "drift_check", auto.drift_check, quiet=QUIET["drift_check"],
            enabled=lambda: config.get("board.enabled", True))
    s.daily(config.get("board.shutdown_at", "18:45"), "shutdown_reminder", auto.shutdown_reminder,
            catch_up_hours=3, enabled=lambda: config.get("board.enabled", True))
    s.weekly(int(config.get("board.review_day", 4)), config.get("board.review_at", "17:30"),
             "review_reminder", auto.review_reminder, enabled=lambda: config.get("board.enabled", True))
    s.daily(config.get("board.plan_at", "09:15"), "plan_reminder", auto.plan_reminder,
            catch_up_hours=4, enabled=lambda: config.get("board.enabled", True))
    s.daily("09:15", "downloads_janitor", auto.downloads_janitor,
            enabled=lambda: config.get("automations.downloads_janitor.enabled", True))

    # briefings - the brain lives over the network, so these defer too rather
    # than emitting a "brain offline" digest you would have to regenerate
    if d.get("morning_brief", {}).get("enabled", True):
        s.daily(d["morning_brief"].get("at", "08:30"), "morning_brief", morning,
                catch_up_hours=5, requires_net=True)
    if d.get("evening_wrap", {}).get("enabled", True):
        s.daily(d["evening_wrap"].get("at", "23:30"), "evening_wrap", evening,
                catch_up_hours=8, requires_net=True)
    if d.get("weekly_retro", {}).get("enabled", True):
        s.weekly(int(d["weekly_retro"].get("day", 6)),
                 d["weekly_retro"].get("at", "20:00"), "weekly_retro", weekly,
                 catch_up_hours=14, requires_net=True)
    s.daily("09:45", "standup_writer", auto.standup_writer,
            enabled=lambda: config.get("automations.standup_writer.enabled", True),
            requires_net=True, quiet=QUIET["standup_writer"])
    SCHED.jobs = s.jobs
    return SCHED


_sched_sig: str | None = None


def _schedule_signature() -> str:
    """The config values baked into job definitions rather than read live."""
    return json.dumps([config.get("digests", {}), config.get("google.poll_seconds", 300),
                       config.get("board", {})],
                      sort_keys=True, default=str)


def reload_schedule_if_changed() -> bool:
    """config.yaml promises edits apply within a minute. Toggles always did,
    because they are read live; digest times and the Google poll interval
    were fixed at startup until this ran between scheduler ticks."""
    global _sched_sig
    sig = _schedule_signature()
    if sig == _sched_sig:
        return False
    if _sched_sig is not None:
        lg.info("config changed - rebuilding the schedule")
    build_schedule()
    _sched_sig = sig
    return True


def register_reconnect_hooks() -> None:
    """What should happen the moment the internet comes back.

    Ordered cheapest-first so the most useful data lands soonest: anything the
    scheduler already owed you, then Google, then the local collectors whose
    upstreams may also have been unreachable.
    """
    net.on_reconnect("due_jobs", SCHED.run_due_net_jobs)
    net.on_reconnect("google", lambda: google_sync() if google_connected() else "not connected")
    net.on_reconnect("git", git.collect_once)
    net.on_reconnect("browser", browser.collect_once)
    net.on_reconnect("notify", _reconnect_notice)


def _reconnect_notice() -> str:
    """Tell the user what they missed, but only after a meaningful outage."""
    st = net.state()
    # for_seconds was read here, but by the time this callback runs the state
    # has already flipped to online, so it measured seconds since reconnecting
    # and this notice could never fire.
    down_for = st.get("last_outage_seconds", 0)
    if down_for < 15 * 60:
        return "brief blip, staying quiet"
    bits = []
    if google_connected():
        counts = gmail.unread_counts()
        n = counts.get("needs_reply", 0) + counts.get("vip", 0)
        if n:
            bits.append(f"{n} mail needing a reply")
        nxt = gcal.next_event()
        if nxt:
            mins = int((nxt["start_ts"] - time.time()) / 60)
            if 0 < mins < 180:
                bits.append(f"{nxt['summary'][:40]} in {mins}m")
    if not bits:
        return "back online, nothing urgent"
    notify.send("Back online", "; ".join(bits), severity="info",
                rule="reconnect", cooldown_seconds=600, url=notify.dashboard("google"))
    return "; ".join(bits)


# --------------------------------------------------------------------------- status


def status() -> dict:
    _refresh_health()
    hb = float(db.get_state("daemon_heartbeat", 0) or 0)
    return {
        "running": True,
        "started_at": STARTED_AT,
        "uptime_seconds": time.time() - STARTED_AT,
        "heartbeat": hb,
        "heartbeat_age": time.time() - hb if hb else None,
        "sensors": {k: {"alive": v.get("alive"), "reason": v.get("reason")}
                    for k, v in SENSOR_HEALTH.items()},
        "core": {k: {"alive": v["thread"].is_alive(), "restarts": len(v["restarts"])}
                 for k, v in CORE.items()},
        "jobs": SCHED.describe(),
        "db_mb": db.db_size_mb(),
        "net": net.state(),
        "google": {"configured": gauth.is_configured(), "connected": gauth.is_connected()},
        "counts": {
            "events": (db.q1("SELECT COUNT(*) n FROM events") or {"n": 0})["n"],
            "activity": (db.q1("SELECT COUNT(*) n FROM activity") or {"n": 0})["n"],
            "alerts_unacked": (db.q1("SELECT COUNT(*) n FROM alerts WHERE acked=0")
                               or {"n": 0})["n"],
        },
    }


def daemon_alive() -> bool:
    """Liveness as seen from another process.

    The heartbeat alone is not enough: a daemon killed with taskkill (or a
    foreground run stopped with ctrl+c mid-tick) leaves a fresh-looking
    heartbeat behind, and the CLI would then refuse to start a replacement.
    So the port is the real test, with the heartbeat only as a fallback for a
    daemon deliberately run with the web UI off.
    """
    import socket
    port = int(config.get("daemon.web_port", 7842))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        pass
    hb = float(db.get_state("daemon_heartbeat", 0) or 0)
    if not hb or (time.time() - hb) >= 180:
        return False
    # Port shut but heartbeat fresh: only believe it if a daemon process exists.
    return _daemon_pids() != []


def stop_running(wait: float = 12.0) -> tuple[bool, int]:
    """Stop the running daemon. Returns (exited_cleanly, processes_force_killed).

    Asks over the local API first, so sensors flush their open sessions and
    the daemon clears its own heartbeat; whatever is still running after
    `wait` seconds is killed.
    """
    import httpx

    from .util import run
    graceful = False
    port = int(config.get("daemon.web_port", 7842))
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/api/shutdown", timeout=5)
        if r.status_code == 200:
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(0.5)
                if not daemon_alive():
                    time.sleep(2.5)          # let the process finish exiting
                    graceful = True
                    break
    except Exception:
        pass
    pids = _daemon_pids()
    for pid in pids:
        run(["taskkill", "/PID", str(pid), "/F"], timeout=20)
    # A killed daemon cannot clear its own heartbeat, and a stale one would make
    # the next start believe it is still up.
    db.set_state("daemon_heartbeat", 0)
    return graceful, len(pids)


def start_background(wait: float = 30.0) -> bool:
    """Launch the daemon detached (the frozen app when built) and wait for it."""
    import subprocess

    from .paths import ROOT, daemon_command
    subprocess.Popen(
        daemon_command() + ["--quiet"], cwd=str(ROOT),
        creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                       | subprocess.CREATE_NEW_PROCESS_GROUP),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1)
        if daemon_alive():
            return True
    return False


def _daemon_pids() -> list[int]:
    """PIDs of running ARGUS daemons.

    Two shapes have to be matched. Run from source the process is a python.exe
    running the module `argus` exactly; run as the frozen app it is Argus.exe
    with no subcommand. Matching only the first is what made `argus stop`
    silently do nothing once the exe existed.

    The source pattern used to be `*-m argus*`, which also matched every CLI
    invocation (`-m argus.cli ...`). A venv's python.exe is a launcher with its
    own PID, separate from the interpreter's, so excluding os.getpid() did not
    protect it: `argus stop` killed its own launcher (exit 1, output lost) and
    any `argus` command running in another terminal, and `daemon_alive()`
    reported a CLI as a live daemon.
    """
    import json as _json

    from .util import run
    rc, out, _ = run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { "
         "  $_.Name -like 'python*' -or $_.Name -eq 'Argus.exe' } | "
         "Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Compress"],
        timeout=45)
    if rc != 0 or not out.strip():
        return []
    try:
        procs = _json.loads(out)
    except ValueError:
        return []
    if isinstance(procs, dict):
        procs = [procs]
    mine = {os.getpid(), os.getppid()}
    return [int(p["ProcessId"]) for p in procs
            if is_daemon_cmdline(p.get("Name") or "", p.get("CommandLine") or "")
            and int(p["ProcessId"]) not in mine]


def is_daemon_cmdline(name: str, cmdline: str) -> bool:
    """True for the daemon's own command line, never for a CLI invocation."""
    import re
    if name.lower() == "argus.exe":
        # The app takes launch flags (--quiet, --force); any other argument is
        # a CLI subcommand borrowing the exe.
        args = re.sub(r'^\s*("[^"]*"|\S+)', "", cmdline).split()
        return all(a.startswith("--") for a in args)
    return re.search(r"(?:^|\s)-m\s+argus(?=\s|$)", cmdline) is not None


# --------------------------------------------------------------------------- entry


def _shutdown(*_a) -> None:
    lg.info("shutdown requested")
    STOP.set()


def main(with_web: bool = True, with_tray: bool | None = None) -> int:
    lock = SingleInstance()
    if not lock.acquire():
        print("ARGUS is already running.")
        return 1

    config.write_default_config()
    db.init()
    harden_secrets()
    db.log_event("argus", "start", "daemon", datetime.now().isoformat())
    lg.info("=" * 60)
    lg.info("ARGUS starting")

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except Exception:
        pass

    start_sensors()
    # Categories are derived from (exe, title); when the rules improve, stored
    # sessions are re-derived once so history and today agree.
    spawn(lambda: lg.info(auto.classifier_upgrade()), "classify.upgrade")
    spawn(lambda: lg.info(auto.first_run_catch_up()), "first_run")
    reload_schedule_if_changed()
    register_reconnect_hooks()
    net.poll_once()                      # establish the initial state before jobs run
    start_core("net", lambda: net.run_loop(STOP))
    start_core("scheduler", lambda: SCHED.loop(STOP, before_tick=reload_schedule_if_changed))

    if with_web:
        from .web import server as web
        start_core("web", lambda: web.serve(STOP))

    want_tray = config.get("daemon.tray", True) if with_tray is None else with_tray
    if want_tray:
        from .actions import tray
        spawn(lambda: tray.run(STOP, status), "tray")
    from .actions import telegram
    spawn(lambda: telegram.run(STOP), "telegram")
    if config.get("board.enabled", True) and config.get("board.hotkey", "ctrl+alt+space"):
        from .actions import capture
        spawn(lambda: capture.run(STOP, config.get("board.hotkey", "ctrl+alt+space")), "capture")

    port = config.get("daemon.web_port", 7842)
    lg.info("ARGUS ready - dashboard at http://127.0.0.1:%s", port)
    notify.send("ARGUS is watching",
                f"Dashboard: http://127.0.0.1:{port}",
                severity="info", rule="daemon_start", record=False)

    try:
        last_check = time.time()
        while not STOP.is_set():
            STOP.wait(5)
            if time.time() - last_check >= 30:
                last_check = time.time()
                revive_core()
    except KeyboardInterrupt:
        _shutdown()

    lg.info("stopping sensors...")
    time.sleep(2)
    db.log_event("argus", "stop", "daemon", f"uptime {int(time.time() - STARTED_AT)}s")
    db.set_state("daemon_heartbeat", 0)
    lock.release()
    lg.info("ARGUS stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
