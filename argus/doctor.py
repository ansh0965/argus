"""`argus doctor`: every way ARGUS can be quietly broken, checked in one place.

Each check returns a status and, when it is not ok, the exact command or edit
that fixes it. Nothing here changes anything - it only looks.

Status meanings:
  ok    - working as intended
  warn  - working, but degraded or about to bite
  fail  - a feature you are counting on is not working
  info  - worth knowing, not a problem
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, paths

Check = dict


def _check(name: str, status: str, detail: str, fix: str = "") -> Check:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


# --------------------------------------------------------------------------- checks

def check_daemon() -> list[Check]:
    from .daemon import daemon_alive
    out = []
    alive = daemon_alive()
    hb = float(db.get_state("daemon_heartbeat", 0) or 0)
    if not alive:
        out.append(_check("daemon", "fail", "not running", "argus start"))
        return out
    age = time.time() - hb if hb else None
    if age is not None and age > 300:
        out.append(_check("daemon", "warn",
                          f"running, but the last heartbeat was {int(age // 60)} min ago "
                          "- the scheduler may be stuck", "argus restart"))
    else:
        out.append(_check("daemon", "ok", "running"))

    try:
        import httpx
        port = config.get("daemon.web_port", 7842)
        st = httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=8).json()
    except Exception as e:
        out.append(_check("dashboard", "warn", f"daemon up but its API did not answer: {e}",
                          "argus restart"))
        return out
    sensors = st.get("sensors") or {}
    dead = sorted(k for k, v in sensors.items()
                  if not v.get("alive") and v.get("reason") != "disabled")
    off = sorted(k for k, v in sensors.items() if v.get("reason") == "disabled")
    if dead:
        out.append(_check("sensors", "fail", f"stopped: {', '.join(dead)}",
                          "argus logs --lines 120   (the traceback), then argus restart"))
    else:
        live = len(sensors) - len(off)
        out.append(_check("sensors", "ok", f"{live} running"
                          + (f", disabled: {', '.join(off)}" if off else "")))
    core_dead = sorted(k for k, v in (st.get("core") or {}).items() if not v.get("alive"))
    if core_dead:
        out.append(_check("core threads", "fail", f"not running: {', '.join(core_dead)}",
                          "argus logs --lines 120, then argus restart"))
    if st.get("paused"):
        until = datetime.fromtimestamp(float(st.get("paused_until") or 0))
        out.append(_check("paused", "info", f"monitoring paused until {until:%H:%M}",
                          "argus unpause"))
    return out


def check_build() -> list[Check]:
    """A frozen Argus.exe does not see source edits until it is rebuilt."""
    exe = paths.APP_EXE
    if not exe.exists():
        return []
    src = paths.ROOT / "argus"
    if not src.is_dir():
        return []
    newest = max((p.stat().st_mtime for p in src.rglob("*") if p.is_file()
                  and p.suffix in (".py", ".html")), default=0)
    lag = newest - exe.stat().st_mtime
    if lag > 60:
        return [_check("build", "warn",
                       f"Argus.exe is {timedelta(seconds=int(lag))} older than the source, so "
                       "the app you launch lacks the latest changes",
                       "argus build   (builds, then swaps it in with a few seconds of downtime)")]
    return [_check("build", "ok", "Argus.exe matches the source")]


def check_config() -> list[Check]:
    errs = config.problems()
    if not errs:
        return [_check("config", "ok", "config.yaml valid")]
    status = "fail" if config.last_error() else "warn"
    return [_check("config", status, e, "argus config --edit") for e in errs]


def check_database() -> list[Check]:
    out = []
    try:
        r = db.q1("PRAGMA quick_check")
        verdict = r[0] if r else "?"
    except Exception as e:
        verdict = str(e)
    if verdict != "ok":
        from .paths import BACKUPS
        newest = sorted(BACKUPS.glob("argus-*.db"))[-1:] if BACKUPS.is_dir() else []
        fix = ("argus stop, copy data/argus.db somewhere safe, then replace it with "
               f"data/backups/{newest[0].name} and delete argus.db-wal / -shm; argus start"
               if newest else "stop ARGUS and copy data/argus.db somewhere safe before "
                              "anything else")
        out.append(_check("database", "fail", f"integrity check: {verdict}", fix))
    size = db.db_size_mb()
    wal = paths.DB_PATH.with_name(paths.DB_PATH.name + "-wal")
    wal_mb = round(wal.stat().st_size / 1e6, 1) if wal.exists() else 0
    detail = f"{size} MB" + (f" (+{wal_mb} MB WAL)" if wal_mb else "")
    if not out:
        out.append(_check("database", "ok" if wal_mb < 128 else "warn", detail,
                          "" if wal_mb < 128 else "argus restart (checkpoints the WAL)"))
    return out


def check_backup() -> list[Check]:
    if not config.get("backup.enabled", True):
        return [_check("backup", "warn", "nightly backups are turned off",
                       "set backup.enabled: true in config.yaml")]
    from .paths import BACKUPS
    files = sorted(BACKUPS.glob("argus-*.db*"), key=lambda f: f.stat().st_mtime) if BACKUPS.is_dir() else []
    if not files:
        return [_check("backup", "warn", "no backup of data/argus.db yet",
                       "argus backup")]
    newest = files[-1]
    age_h = (time.time() - newest.stat().st_mtime) / 3600
    if age_h > 48:
        return [_check("backup", "warn", f"newest backup is {age_h / 24:.0f} days old "
                       f"({newest.name})", "argus backup")]
    return [_check("backup", "ok", f"{len(files)} kept, newest {newest.name}")]


def check_disk() -> list[Check]:
    try:
        import psutil
        free = psutil.disk_usage(str(paths.ROOT.anchor or "C:\\")).free / 1e9
    except Exception:
        return []
    want = float(config.get("automations.system_watchdog.disk_free_gb", 12))
    if free < want:
        return [_check("disk", "warn", f"{free:.1f} GB free (warning threshold {want:g} GB)",
                       "clear Downloads / node_modules, or lower disk_free_gb")]
    return [_check("disk", "ok", f"{free:.0f} GB free")]


def check_brain() -> list[Check]:
    from .brain import llm
    provs = llm.available()
    used = llm.calls_today()
    budget = int(config.get("brain.max_daily_calls", 200))
    out = []
    if not provs:
        out.append(_check("brain", "fail",
                          "no LLM provider available - briefings fall back to raw facts",
                          "install Claude Code (claude) or set GEMINI_API_KEY"))
    else:
        out.append(_check("brain", "ok", f"{', '.join(provs)} ({used}/{budget} calls today)"))
    if used >= budget * 0.8:
        out.append(_check("brain budget", "warn", f"{used} of {budget} daily calls used",
                          "raise brain.max_daily_calls if this is expected"))
    return out


def check_google() -> list[Check]:
    from .gsuite import auth
    if not auth.labels():
        if auth.is_configured():
            return [_check("google", "info", "OAuth client present, no account connected",
                           "argus google login --label personal")]
        return [_check("google", "info", "not set up (optional)", "argus google login")]
    out = []
    hint = ""
    for a in auth.accounts():
        who = f"{a['label']} ({a['email'] or '?'})"
        if a["valid"]:
            out.append(_check(f"google {a['label']}", "ok",
                              f"{who}: {', '.join(a['features']) or 'no features'}"))
        elif a["state"] == "expired":
            out.append(_check(f"google {a['label']}", "fail",
                              f"{who}: token expired or revoked - nothing syncs",
                              f"argus google login --label {a['label']} --force"))
            hint = hint or a.get("hint", "")
        else:
            out.append(_check(f"google {a['label']}", "warn",
                              f"{who}: unreachable right now ({a['reason'][:80]})"))
    expired = [c for c in out if c["status"] == "fail"]
    if len(expired) > 1:
        out.append(_check("google relogin", "info",
                          f"{len(expired)} accounts expired - fix them all in one go",
                          "argus google relogin"))
    if hint:
        out.append(_check("google tokens", "info", hint))
    return out


def check_autostart() -> list[Check]:
    try:
        from .install import is_installed
        ok = is_installed()
    except Exception:
        return []
    if ok:
        return [_check("autostart", "ok", "starts at login (Task Scheduler)")]
    return [_check("autostart", "warn", "will not start at login", "argus install")]


_LOGLINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (ERROR|WARNING)\s+(\S+)\s+(.*)$")


def check_log(hours: float = 24) -> list[Check]:
    f = paths.LOGS / "argus.log"
    if not f.is_file():
        return []
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    errors, last = 0, ""
    try:
        with open(f, "rb") as fh:
            fh.seek(max(0, f.stat().st_size - 2_000_000))
            for raw in fh:
                m = _LOGLINE.match(raw.decode("utf-8", "replace").rstrip())
                if m and m.group(1) >= cutoff and m.group(2) == "ERROR":
                    errors += 1
                    last = f"{m.group(3)}: {m.group(4)[:120]}"
    except OSError:
        return []
    if errors:
        return [_check("log", "warn", f"{errors} error(s) in the last {hours:g}h - latest {last}",
                       "argus logs --lines 120")]
    return [_check("log", "ok", f"no errors in the last {hours:g}h")]


def check_privacy() -> list[Check]:
    out = []
    if config.get("sensors.screenshot.enabled", False):
        days = config.get("sensors.screenshot.retain_days", 3)
        out.append(_check("screenshots", "info", f"ON - kept {days} day(s) under data/screenshots"))
    if config.get("sensors.clipboard.enabled", False):
        out.append(_check("clipboard", "info", "clipboard capture is ON"))
    return out


def _newest(q: str, args: tuple = ()) -> float:
    r = db.q1(q, args)
    return float((r[0] if r else 0) or 0)


def check_data(now: float | None = None) -> list[Check]:
    """Is each source still producing data, judged against what the machine
    itself shows? A sensor that stops quietly - a browser update moving the
    History file, Claude Code changing its transcript format - otherwise
    leaves the board and the briefings wrong without a single error."""
    now = now or time.time()
    out: list[Check] = []

    # windows: if you are using the machine, sessions must be landing
    try:
        from .sensors.window import idle_seconds
        idle = idle_seconds()
    except Exception:
        idle = None
    last_act = _newest("SELECT MAX(ended) FROM activity")
    if idle is not None and idle < 120 and now - last_act > 15 * 60:
        out.append(_check("data: windows", "fail",
                          f"you are active but no window activity recorded for "
                          f"{(now - last_act) / 60:.0f} min", "argus restart"))

    # git: a repo with a commit newer than anything recorded
    try:
        from .sensors import git as gitsensor
        repo_new = max((s.get("last_commit_ts") or 0) for s in gitsensor.all_states(max_age=3600))
    except (ValueError, Exception):
        repo_new = 0
    git_new = _newest("SELECT MAX(ts) FROM events WHERE source='git' AND kind='commit'")
    if repo_new and repo_new - git_new > 6 * 3600 and now - repo_new < 7 * 86400:
        out.append(_check("data: git", "warn",
                          f"a repository has commits from {datetime.fromtimestamp(repo_new):%d %b %H:%M} "
                          f"that were not recorded", "argus restart"))

    # Claude Code: transcripts newer than anything the thread engine read
    base = Path.home() / ".claude" / "projects"
    try:
        tr_new = max((p.stat().st_mtime for p in base.glob("*/*.jsonl")), default=0)
    except OSError:
        tr_new = 0
    claude_new = _newest("SELECT MAX(ts) FROM thread_touches WHERE source='claude'")
    if tr_new and now - tr_new < 2 * 86400 and tr_new - claude_new > 12 * 3600:
        out.append(_check("data: claude", "warn",
                          "Claude Code sessions are newer than anything ARGUS parsed - "
                          "the transcript format may have changed", "argus doctor -v; report it"))

    # browser: History files touched after the last recorded visit
    try:
        from .sensors import browser
        hist_new = max((p.stat().st_mtime for _, p in browser.profiles()), default=0)
    except Exception:
        hist_new = 0
    web_new = _newest("SELECT MAX(ts) FROM events WHERE source='browser'")
    if hist_new and now - hist_new < 86400 and hist_new - web_new > 3 * 3600:
        out.append(_check("data: browser", "warn",
                          f"browser history changed {datetime.fromtimestamp(hist_new):%H:%M} but the "
                          f"last visit recorded is from {datetime.fromtimestamp(web_new):%d %b %H:%M}",
                          "argus restart"))
    if not out:
        out.append(_check("data", "ok", "every source is producing data"))
    return out


CHECKS = [check_daemon, check_build, check_config, check_database, check_backup,
          check_disk,
          check_brain, check_google, check_autostart, check_log, check_privacy, check_data]


def run_checks(skip: tuple[str, ...] = ()) -> list[Check]:
    out: list[Check] = []
    for fn in CHECKS:
        if fn.__name__.removeprefix("check_") in skip:
            continue
        try:
            out += fn()
        except Exception as e:
            out.append(_check(fn.__name__.removeprefix("check_"), "warn",
                              f"check itself failed: {type(e).__name__}: {e}"))
    return out


def worst(checks: list[Check]) -> str:
    order = {"fail": 3, "warn": 2, "info": 1, "ok": 0}
    return max((c["status"] for c in checks), key=lambda s: order.get(s, 0), default="ok")


def log_path() -> Path:
    return paths.LOGS / "argus.log"
