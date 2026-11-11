"""The autonomous behaviours. Each returns a short summary string for the run log.

Design rules every automation here follows:
  - read a lot, write little, and never act destructively without being asked
  - be quiet by default: cooldowns and quiet hours are respected via notify.send
  - return a one-line summary so `argus runs` reads like a diary of decisions
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from .. import classify, config, db
from ..actions import notify
from ..brain import insights, llm
from ..sensors import git as gitsensor
from ..sensors import network, process
from ..util import human_dur, log, plain

lg = log("auto")


def _on(name: str) -> bool:
    return bool(config.get(f"automations.{name}.enabled", True))


def _cooldown_min(name: str, default: float) -> float:
    return float(config.get(f"automations.{name}.cooldown_minutes", default)) * 60


# --------------------------------------------------------------------------- focus


def _in_meeting() -> str:
    """The meeting you are in right now, from the local calendar mirror."""
    try:
        from ..gsuite import gcal
        m = gcal.in_meeting_now()
        return (m or {}).get("summary") or ""
    except Exception:
        return ""


def focus_guardian() -> str:
    """Notice a distraction spiral or a context-switch storm while it is happening."""
    from ..brain import board as _board
    if _board.focus_state():
        return "focusing"
    if not _on("focus_guardian"):
        return "disabled"
    # Flipping between Meet, the doc being discussed and the ticket is what a
    # meeting looks like; "You're bouncing" in the middle of one is noise.
    meeting = _in_meeting()
    if meeting:
        return f"in a meeting ({meeting[:40]}), holding nudges"
    now = time.time()
    window = 30 * 60
    s, e = now - window, now

    dist = insights.distractions(s, e)
    lost = sum(d["seconds"] for d in dist)
    sw = insights.context_switches(s, e)
    avg_sw = sum(b["switches"] for b in sw) / len(sw) if sw else 0
    storm_at = float(config.get("focus.switch_storm_per_10min", 25))
    cooldown = float(config.get("focus.nudge_cooldown_minutes", 45)) * 60

    from ..util import is_work_hours
    working = is_work_hours()

    if lost > 15 * 60 and working:
        what = ", ".join(f"{d['what']} {d['human']}" for d in dist[:3])
        notify.send(
            "Half an hour, mostly gone",
            f"{human_dur(lost)} on {what} in the last 30 minutes. "
            f"You were on {_last_productive_thing()} before that.",
            severity="warn", rule="focus_guardian", cooldown_seconds=cooldown,
            url=notify.dashboard("today"))
        return f"nudged: {human_dur(lost)} distracted"

    if avg_sw >= storm_at:
        notify.send(
            "You're bouncing",
            f"{avg_sw:.0f} app switches per 10 minutes. Pick one thing and close the rest.",
            severity="info", rule="focus_switch_storm", cooldown_seconds=cooldown,
            url=notify.dashboard("today"))
        return f"nudged: {avg_sw:.0f} switches/10min"

    return f"clear (lost {human_dur(lost)}, {avg_sw:.0f} switches/10min)"


def _last_productive_thing() -> str:
    r = db.q1("SELECT app, title FROM activity WHERE afk=0 AND private=0 AND category IN "
              "(" + ",".join("?" * len(classify.PRODUCTIVE)) + ") "
              "ORDER BY started DESC LIMIT 1", tuple(sorted(classify.PRODUCTIVE)))
    if not r:
        return "something else"
    return f"{r['app']}" + (f" ({r['title'][:50]})" if r["title"] else "")


BREAK_GAP = 240.0          # four minutes away counts as a break


def stretches(start: float, end: float) -> list[float]:
    """Lengths of unbroken runs at the machine between two times: a run ends
    at an idle stretch, or a gap in the record, of at least BREAK_GAP."""
    out: list[float] = []
    run_start = last_end = None
    for r in db.q("SELECT started, ended, afk FROM activity WHERE ended > ? AND started < ? "
                  "ORDER BY started", (start, end)):
        brk = (r["afk"] and r["ended"] - r["started"] >= BREAK_GAP) or \
              (last_end is not None and r["started"] - last_end >= BREAK_GAP)
        if brk and run_start is not None:
            out.append(last_end - run_start)
            run_start = None
        if not r["afk"]:
            if run_start is None:
                run_start = r["started"]
            last_end = r["ended"]
        elif not brk:
            last_end = r["ended"]
    if run_start is not None and last_end is not None:
        out.append(last_end - run_start)
    return out


def typical_stretch(days: int = 14) -> float | None:
    """Median unbroken stretch (20+ minutes) over the last `days`, cached
    for a day."""
    cached = db.get_state("typical_stretch") or {}
    if cached.get("ts", 0) > time.time() - 86400:
        return cached.get("value")
    now = time.time()
    runs = sorted(s for s in stretches(now - days * 86400, now) if s >= 20 * 60)
    value = runs[len(runs) // 2] if len(runs) >= 5 else None
    db.set_state("typical_stretch", {"ts": now, "value": value})
    return value


def break_reminder() -> str:
    """Nudge after a long unbroken stretch at the machine."""
    from ..brain import board as _board
    if _board.focus_state():
        return "focusing"
    if not _on("break_reminder"):
        return "disabled"
    meeting = _in_meeting()
    if meeting:
        return f"in a meeting ({meeting[:40]}), holding nudges"
    after = float(config.get("automations.break_reminder.after_minutes", 90)) * 60
    if config.get("automations.break_reminder.adaptive", True):
        # Your own rhythm, not a fixed number: ~1.25x your typical unbroken
        # stretch over the last two weeks, kept between 60 and 150 minutes.
        typical = typical_stretch()
        if typical:
            after = min(150 * 60, max(60 * 60, typical * 1.25))
    brk = 240.0                               # four minutes away counts as a break
    now = time.time()
    rows = db.q("SELECT started, ended, afk FROM activity "
                "WHERE ended > ? ORDER BY started DESC", (now - 6 * 3600,))
    if not rows or now - rows[0]["ended"] > brk + 90:
        return "no current streak"            # nothing recorded lately: you're away
    streak_start = None
    newer_start = None
    for r in rows:  # newest first: walk back until a real break
        if r["afk"] and (r["ended"] - r["started"]) >= brk:
            break
        # A gap with no rows at all is also time away - a pause, sleep, or the
        # lock screen before the sensor noticed. It used to be walked straight
        # over, so coming back from a paused lunch triggered "Stand up".
        if newer_start is not None and newer_start - r["ended"] >= brk:
            break
        streak_start = newer_start = r["started"]
    if not streak_start:
        return "no streak"
    dur = time.time() - streak_start
    if dur >= after:
        notify.send("Stand up",
                    f"{human_dur(dur)} without a real break. Two minutes away from the screen.",
                    severity="info", rule="break_reminder",
                    cooldown_seconds=max(after, 45 * 60))
        return f"nudged after {human_dur(dur)}"
    return f"streak {human_dur(dur)}"


# --------------------------------------------------------------------------- machine


def system_watchdog() -> str:
    """Disk, sustained CPU, battery and runaway processes."""
    if not _on("system_watchdog"):
        return "disabled"
    cfg = config.get("automations.system_watchdog", {})
    findings: list[str] = []

    m = db.q1("SELECT * FROM metrics ORDER BY ts DESC LIMIT 1")
    if not m:
        return "no samples yet"

    free_gb = float(cfg.get("disk_free_gb", 12))
    if m["disk_free_gb"] is not None and m["disk_free_gb"] < free_gb:
        notify.send("Disk is filling up",
                    f"Only {m['disk_free_gb']:.1f} GB free on C:. "
                    "Downloads and node_modules are the usual suspects.",
                    severity="warn", rule="disk_low", cooldown_seconds=6 * 3600,
                    url=notify.dashboard("system"))
        findings.append(f"disk {m['disk_free_gb']:.1f}GB")

    mins = float(cfg.get("cpu_sustained_minutes", 10))
    thresh = float(cfg.get("cpu_sustained_pct", 92))
    rows = db.q("SELECT cpu FROM metrics WHERE ts > ?", (time.time() - mins * 60,))
    if len(rows) >= 4 and all((r["cpu"] or 0) >= thresh for r in rows):
        top = process.top_consumers(3)
        who = ", ".join(f"{t['name']} {t['cpu']:.0f}%" for t in top)
        notify.send("CPU pinned",
                    f"Above {thresh:.0f}% for {mins:.0f} minutes. Top: {who}",
                    severity="warn", rule="cpu_sustained", cooldown_seconds=2 * 3600,
                    url=notify.dashboard("system"))
        findings.append("cpu sustained")

    low = float(cfg.get("battery_low_pct", 20))
    if m["battery"] is not None and not m["plugged"] and m["battery"] <= low:
        notify.send("Battery low", f"{m['battery']:.0f}% and unplugged.",
                    severity="urgent" if m["battery"] <= 10 else "warn",
                    rule="battery_low", cooldown_seconds=1800,
                    url=notify.dashboard("system"))
        findings.append(f"battery {m['battery']:.0f}%")

    if m["mem"] is not None and m["mem"] >= 94:
        top = process.top_consumers(3)
        who = ", ".join(f"{t['name']} {t['mem_mb']:.0f}MB" for t in top)
        notify.send("Memory nearly full", f"RAM at {m['mem']:.0f}%. Top: {who}",
                    severity="warn", rule="mem_high", cooldown_seconds=3 * 3600,
                    url=notify.dashboard("system"))
        findings.append(f"mem {m['mem']:.0f}%")

    return ", ".join(findings) if findings else "all clear"


def downloads_janitor() -> str:
    """Report on Downloads bloat and obviously stale installers. Never deletes."""
    if not _on("downloads_janitor"):
        return "disabled"
    d = Path("~/Downloads").expanduser()
    if not d.is_dir():
        return "no Downloads folder"
    total = 0
    old_big: list[tuple[str, float, float]] = []
    cutoff = time.time() - 30 * 86400
    for f in d.iterdir():
        try:
            if not f.is_file():
                continue
            st = f.stat()
            total += st.st_size
            if st.st_mtime < cutoff and st.st_size > 100e6:
                old_big.append((f.name, st.st_size / 1e9, st.st_mtime))
        except Exception:
            continue
    gb = total / 1e9
    warn_gb = float(config.get("automations.downloads_janitor.warn_gb", 15))
    if gb >= warn_gb:
        old_big.sort(key=lambda t: -t[1])
        sample = ", ".join(f"{n[:34]} ({g:.1f}GB)" for n, g, _ in old_big[:3])
        notify.send("Downloads folder is heavy",
                    f"{gb:.1f} GB total. Untouched for a month: {sample or 'nothing obvious'}",
                    severity="info", rule="downloads_janitor", cooldown_seconds=7 * 86400)
        return f"{gb:.1f}GB, {len(old_big)} stale large files"
    return f"{gb:.1f}GB, fine"


def security_watch() -> str:
    """Drift detection on autostart entries and listening ports."""
    if not _on("security_watch"):
        return "disabled"
    findings = []

    entries = network.autostart_entries()
    cur = {f"{e['where']}|{e['name']}": e["cmd"] for e in entries}
    prev = db.get_state("autostart_baseline", None)
    if prev is None:
        db.set_state("autostart_baseline", cur)
        return f"baseline set ({len(cur)} autostart entries)"
    new = {k: v for k, v in cur.items() if k not in prev}
    if new:
        listing = "; ".join(f"{k.split('|')[-1]} -> {v[:70]}" for k, v in list(new.items())[:4])
        notify.send("New startup program", listing, severity="warn",
                    rule="autostart_new", cooldown_seconds=3600,
                    url=notify.dashboard("system"))
        findings.append(f"{len(new)} new autostart")
    db.set_state("autostart_baseline", cur)

    ports = {f"{p['port']}/{p['proc']}" for p in network.listening_ports()}
    prev_ports = set(db.get_state("ports_baseline", []) or [])
    if not prev_ports:
        db.set_state("ports_baseline", sorted(ports))
        return (", ".join(findings) or "") + f" baseline set ({len(ports)} ports)"
    new_ports = ports - prev_ports
    # Dev servers churn ports constantly; only flag privileged/unusual ones.
    interesting = {p for p in new_ports
                   if int(p.split("/")[0]) < 1024 or int(p.split("/")[0]) in
                   (3389, 5900, 4444, 5555, 8888, 9001, 1080)}
    if interesting:
        notify.send("New listening port", ", ".join(sorted(interesting)),
                    severity="warn", rule="port_new", cooldown_seconds=3600,
                    url=notify.dashboard("system"))
        findings.append(f"ports {sorted(interesting)}")
    db.set_state("ports_baseline", sorted(ports))

    return ", ".join(findings) if findings else "no drift"


# --------------------------------------------------------------------------- work


def repo_janitor() -> str:
    """Nag about work that exists only on disk."""
    if not _on("repo_janitor"):
        return "disabled"
    stale_h = float(config.get("automations.repo_janitor.stale_hours", 24))
    try:
        states = gitsensor.all_states()
    except Exception as e:
        return f"git scan failed: {e}"

    at_risk = []
    for s in states:
        if s["dirty"] == 0:
            continue
        age_h = (time.time() - s["last_commit_ts"]) / 3600 if s["last_commit_ts"] else 999
        if age_h >= stale_h:
            at_risk.append((s, age_h))
    unpushed = [s for s in states if s["ahead"] > 0]

    if at_risk:
        at_risk.sort(key=lambda t: -t[0]["dirty"])
        lines = [f"{s['name']}: {s['dirty']} files, {h:.0f}h since last commit"
                 for s, h in at_risk[:4]]
        extra = f" | {len(unpushed)} repo(s) with unpushed commits" if unpushed else ""
        notify.send("Uncommitted work piling up", "; ".join(lines) + extra,
                    severity="info", rule="repo_janitor", cooldown_seconds=20 * 3600,
                    url=notify.dashboard("work"))
    return (f"{len(at_risk)} stale-dirty, {len(unpushed)} unpushed, "
            f"{len(states)} repos scanned")


def last_workday(now: datetime | None = None) -> str:
    """The most recent work day before today, by identity.work_hours.days.

    A standup reports on the last day you worked: on a Monday that is Friday,
    not the Sunday "yesterday" the writer used to pick.
    """
    now = now or datetime.now()
    days = config.get("identity.work_hours.days", [0, 1, 2, 3, 4]) or [0, 1, 2, 3, 4]
    d = now - timedelta(days=1)
    for _ in range(7):
        if d.weekday() in days:
            break
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def standup_writer(now: datetime | None = None) -> str:
    """Draft the standup from the last work day's evidence. Stored, never sent."""
    if not _on("standup_writer"):
        return "disabled"
    now = now or datetime.now()
    days = config.get("identity.work_hours.days", [0, 1, 2, 3, 4]) or [0, 1, 2, 3, 4]
    if now.weekday() not in days:
        return "not a work day"          # there is no standup on a Saturday
    yday = last_workday(now)
    facts = insights.facts_for_llm(yday)
    if "active 0s" in facts and "Commits:" not in facts:
        return "nothing to report"
    from ..brain.digest import _threads_context
    work_threads = _threads_context(limit=5, realm="work")
    prompt = f"""Write a standup update from this evidence of the last work day
({datetime.strptime(yday, '%Y-%m-%d'):%A %d %B}).

Format exactly:
Yesterday:
- (2-4 bullets, each under 90 characters, concrete: repo, feature, what changed)
Today:
- (1-2 bullets, the obvious continuation)
Blockers:
- (only if the evidence suggests one, else write "None")

Only office work. Exclude anything that looks like coursework, a personal
portfolio, or a hobby repo. If nothing office-related happened, say so.

EVIDENCE:
{facts}

{work_threads}"""
    body = llm.ask_safe(prompt, "You write terse engineering standups.", fallback="")
    if not body:
        return "brain unavailable"
    # Named like the other briefings - by the day it is for, "Standup - Thu
    # 24 Sep" - rather than an ISO date of the day it reports on.
    db.add_digest("standup", f"Standup - {now:%a %d %b}", body, {"for_day": yday})
    # It was stored silently, to be found only by someone who went looking.
    first = next((plain(line) for line in body.splitlines()
                  if line.strip() and not line.rstrip().endswith(":")), "")
    notify.send("Standup drafted", first[:160] or f"on {datetime.strptime(yday, '%Y-%m-%d'):%A}", severity="info",
                rule="standup_writer", url=notify.dashboard("ask"))
    return body.splitlines()[0][:120] if body else "empty"


# --------------------------------------------------------------------------- housekeeping


def reminders(now: float | None = None) -> str:
    """Fire the board's reminders whose time has come ("call Sam at 5pm").

    After a sleep or a shutdown several can come due at once; those that are
    already late arrive as one "while you were away" toast instead of a burst.
    """
    from ..brain import board
    now = now or time.time()
    f = board.focus_state(now)
    if f and f["left"] <= 0:
        done = board.stop_focus(now)
        notify.send(f"Focus done: {done['title'][:60]}",
                    f"{done['spent']} minutes in. Mark it done, take a break, or go another round.",
                    severity="warn", rule="focus_end", cooldown_seconds=0,
                    url=notify.dashboard(f"card-{done['card']}"))
    due = board.due_reminders(now)
    if not due:
        return "focus ended" if f and f["left"] <= 0 else "nothing due"
    late = [c for c in due if now - (c.get("remind_at") or now) > 10 * 60]
    fresh = [c for c in due if c not in late]
    for c in fresh:
        notify.send(f"Reminder: {c['title']}", "Due now. Open it to start, snooze or finish it.",
                    severity="warn", rule=f"remind:{c['id']}", cooldown_seconds=0,
                    url=notify.dashboard(f"card-{c['id']}"))
    if len(late) == 1:
        c = late[0]
        notify.send(f"Reminder: {c['title']}",
                    f"Was due {human_dur(now - c['remind_at'])} ago, while you were away.",
                    severity="warn", rule=f"remind:{c['id']}", cooldown_seconds=0,
                    url=notify.dashboard(f"card-{c['id']}"))
    elif late:
        notify.send(f"While you were away: {len(late)} reminders",
                    "; ".join(c["title"] for c in late[:4]) + (" ..." if len(late) > 4 else ""),
                    severity="warn", rule="remind:missed", cooldown_seconds=0,
                    url=notify.dashboard("board"))
    for c in due:
        board.mark_reminded(c["id"])
    return "reminded: " + "; ".join(c["title"] for c in due)


GOOGLE_TESTING_DAYS = 7


def google_expiry_check(now: float | None = None) -> str:
    """Warn the day before Google signs ARGUS out.

    An OAuth app left in Testing mode gets refresh tokens that die 7 days
    after the login - which is how all four accounts went dark on Sep 18
    without anyone noticing for a week. On day 6 this says so, once. A token
    still working after day 8 means the app was published, and the warnings
    stop for good.
    """
    from ..gsuite import auth as gauth
    if db.get_state("google_published"):
        return "published"
    now = now or time.time()
    soon, survived = [], []
    for label in gauth.labels():
        ts = float(db.get_state(f"google_login_ts:{label}", 0) or 0)
        if not ts or gauth.token_state(label).get("state") == "expired":
            continue
        age = (now - ts) / 86400
        if GOOGLE_TESTING_DAYS - 1.2 <= age < GOOGLE_TESTING_DAYS:
            soon.append(label)
        elif age >= GOOGLE_TESTING_DAYS + 1:
            survived.append(label)
    if survived:
        db.set_state("google_published", now)
        return f"published (token for {survived[0]} outlived the 7-day test limit)"
    if not soon:
        return "nothing due"
    notify.send("Google will sign ARGUS out tomorrow",
                f"{', '.join(soon)}: logins expire after 7 days while the Google app is in "
                f"Testing mode. Publish it (Google page) or log in again tomorrow.",
                severity="warn", rule="google_expiry_soon", cooldown_seconds=20 * 3600,
                url=notify.dashboard("google"))
    return f"warned: {', '.join(soon)}"


DRIFT_AFTER = 45 * 60


def drift_check(now: float | None = None) -> str:
    """A card in Doing whose project you left a while ago, while busy elsewhere:
    ask once whether to park it, so the "where was I" note gets written while
    you still remember."""
    from ..brain import board
    now = now or time.time()
    if not config.get("board.drift_nudges", True):
        return "disabled"
    if _in_meeting():
        return "in a meeting"
    if board.focus_state(now):
        return "focusing"
    recent = db.q1("SELECT COUNT(*) n FROM activity WHERE afk=0 AND ended > ?", (now - 600,))
    if not recent or not recent["n"]:
        return "away"
    nudged = []
    for c in board.board(now)["columns"]["doing"]:
        key = board._thread_key_for(c)
        if not key:
            continue
        r = db.q1("SELECT MAX(last_ts) t FROM threads WHERE key=?", (key,))
        last = max((r["t"] if r else 0) or 0, c.get("started") or 0)
        if now - last < DRIFT_AFTER:
            continue
        notify.send(f"Still on {c['title'][:60]}?",
                    f"No sign of it for {human_dur(now - last)}. Park it with a note "
                    f"about where you were, or pick it back up.",
                    severity="info", rule=f"drift:{c['id']}", cooldown_seconds=4 * 3600,
                    url=notify.dashboard(f"card-{c['id']}"))
        nudged.append(c["title"])
    return ("nudged: " + "; ".join(nudged)) if nudged else "on track"


def shutdown_reminder(now: datetime | None = None) -> str:
    """End of the work day: set down what is open, and get the timesheet text."""
    from ..brain import board
    now = now or datetime.now()
    days = config.get("identity.work_hours.days", [0, 1, 2, 3, 4]) or [0, 1, 2, 3, 4]
    if now.weekday() not in days:
        return "not a work day"
    if db.get_state("shutdown_day") == board.insights.current_day():
        return "already wrapped up"
    s = board.shutdown()
    hours = s["draft"]["hours"]
    body = (f"{len(s['open'])} card(s) still open, {len(s['done'])} done. "
            f"Your timesheet entry ({hours:.1f}h) is drafted - two minutes to wrap up.")
    notify.send("Wrap up the day", body, severity="info", rule="shutdown_reminder",
                cooldown_seconds=6 * 3600, url=notify.dashboard("shutdown"))
    return f"nudged: {body}"


def review_reminder(now: datetime | None = None) -> str:
    """Friday evening: ten minutes to look back, and to clear what went stale."""
    from ..brain import board
    w = board.week()
    stale = len(w["stale"])
    shipped = sum(len(v) for v in w["shipped"].values())
    body = (f"{shipped} things shipped this week" + (f", {stale} card(s) gone stale" if stale else "")
            + ". Your weekly report is ready to copy.")
    notify.send("Weekly review", body, severity="info", rule="review_reminder",
                cooldown_seconds=24 * 3600, url=notify.dashboard("review"))
    return f"nudged: {body}"


def plan_reminder(now: datetime | None = None) -> str:
    """The morning ritual's knock on the door: pick what today is about."""
    from ..brain import board
    now = now or datetime.now()
    days = config.get("identity.work_hours.days", [0, 1, 2, 3, 4]) or [0, 1, 2, 3, 4]
    if now.weekday() not in days:
        return "not a work day"
    b = board.board()
    if b["planned"]:
        return "already planned"
    cols = b["columns"]
    hanging = sum(1 for c in cols["inbox"] if c["auto"])
    late = sum(1 for c in sum(cols.values(), []) if c["overdue"])
    carried = len(cols["today"]) + len(cols["doing"])
    bits = [f"{carried} still on your plate" if carried else "",
            f"{late} overdue" if late else "",
            f"{hanging} things you left hanging" if hanging else ""]
    body = ", ".join(b_ for b_ in bits if b_) or "Two minutes to pick what today is about."
    notify.send("Plan your day", body, severity="info", rule="plan_reminder",
                cooldown_seconds=6 * 3600, url=notify.dashboard("plan"))
    return f"nudged: {body}"


def maintenance() -> str:
    """Prune old rows, purge screenshots, keep the DB small."""
    r = config.get("privacy.retain_days", {}) or {}
    pruned = db.prune(int(r.get("events", 400)), int(r.get("metrics", 90)),
                      runs_days=int(r.get("runs", 30)),
                      alerts_days=int(r.get("alerts", 90)),
                      mail_days=int(r.get("mail", 60)),
                      events_by_source=r.get("events_by_source") or {})
    from ..sensors import files, screenshot
    pruned["generated_files"] = files.prune_generated()
    shots = screenshot.purge()
    before = db.db_size_mb()
    if sum(pruned.values()) > 5000:
        db.vacuum()
    db.checkpoint()
    size = db.db_size_mb()
    return ("pruned " + " ".join(f"{k}={v}" for k, v in pruned.items() if v)
            + f" shots={shots}, db={before}->{size}MB")


def backup(keep: int | None = None) -> str:
    """Nightly snapshot of the database into data/backups, keeping the newest few.

    data/argus.db is the only copy of weeks of history (.gitignore calls it
    irreplaceable) and there was no backup of any kind.
    """
    from .. import vault
    from ..paths import BACKUPS
    keep = int(keep if keep is not None else config.get("backup.keep", 5))
    dest = BACKUPS / f"argus-{datetime.now():%Y-%m-%d}.db"
    size = db.backup(dest)
    # Encrypted to this Windows account, so a copied backup folder is useless
    # to anyone else; `argus backup --decrypt` turns one back into a database.
    if config.get("backup.encrypt", True) and vault.available():
        enc = dest.with_name(dest.name + ".enc")
        vault.encrypt_file(dest, enc)
        dest.unlink(missing_ok=True)
        dest = enc
    every = sorted(BACKUPS.glob("argus-*.db*"), key=lambda f: f.name.split(".")[0])
    old = every[:-keep] if keep > 0 else []
    for f in old:
        f.unlink(missing_ok=True)
    kept = len(list(BACKUPS.glob("argus-*.db*")))
    return f"backed up {size / 1e6:.1f} MB to {dest.name}; kept {kept}"


def first_run_catch_up() -> str:
    """On a fresh install, rebuild the last 90 days from what is already on
    the machine - browser history, git logs, Claude Code transcripts - so the
    board and "where you left off" have something to say on day one instead
    of after a week of watching. Runs once; `argus backfill` repeats it."""
    if db.get_state("backfill_ran"):
        return "already caught up"
    from .. import backfill
    from ..brain import board, threads
    res = backfill.run_all(days=90, reset=False)
    threads.rebuild()
    board.sync(force=True)
    return (f"caught up: {res['days_covered']} days from browser {res['browser']}, "
            f"Claude {res['claude']}, git {res['git']}")


def classifier_upgrade() -> str:
    t0 = time.time()
    out = classify.upgrade_stored()
    if "re-labelled" in out:
        db.add_run("classifier_upgrade", True, int((time.time() - t0) * 1000), out)
    return out


def health_check() -> str:
    """Run the doctor's checks in the background and keep a summary for the
    tray and the status line, which must answer instantly."""
    from .. import doctor
    checks = doctor.run_checks(skip=("daemon", "autostart"))
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    db.set_state("health_summary", {
        "ts": time.time(), "worst": doctor.worst(checks),
        "fails": [f"{c['name']}: {c['detail'][:80]}" for c in fails],
        "warns": [f"{c['name']}: {c['detail'][:80]}" for c in warns],
    })
    if not fails and not warns:
        return "ok, all checks pass"
    return (f"{len(fails)} failing, {len(warns)} warning: "
            + "; ".join(c["name"] for c in fails + warns)[:300])


def threads_rebuild() -> str:
    """Re-derive work threads, then label unknown-realm sessions from them."""
    from ..brain import threads
    r = threads.rebuild()
    inf = threads.infer_realms()
    return (f"{r['threads']} threads in {r['ms']}ms; realm inferred for "
            f"{inf['labelled']} of {inf['checked']} unlabelled sessions")


def heartbeat() -> str:
    """Prove the daemon is alive and surface a crashed sensor."""
    db.set_state("daemon_heartbeat", time.time())
    from ..daemon import SENSOR_HEALTH, _refresh_health, revive_sensors
    revived = revive_sensors()
    _refresh_health()
    watched = {k: v for k, v in SENSOR_HEALTH.items() if v.get("reason") != "disabled"}
    dead = sorted(k for k, v in watched.items() if not v.get("alive"))
    if revived and not dead:
        return f"revived {', '.join(revived)}; {len(watched)}/{len(watched)} sensors alive"
    if dead:
        notify.send("An ARGUS sensor stopped", ", ".join(dead) +
                    " - see `argus logs` for the traceback; `argus restart` revives it",
                    severity="warn", rule="sensor_dead", cooldown_seconds=6 * 3600,
                    url=notify.dashboard("system"))
        return f"DEGRADED: {', '.join(dead)} down, {len(watched) - len(dead)} alive"
    return f"ok, {len(watched)}/{len(watched)} sensors alive"
