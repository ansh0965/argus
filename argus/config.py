"""Config loading with deep-merged defaults. Edit config.yaml; defaults fill the gaps."""
from __future__ import annotations
import copy
import logging
import os
import threading
import time
from typing import Any

import yaml

from .paths import CONFIG_PATH, DATA

DEFAULTS: dict[str, Any] = {
    "identity": {
        # The Windows account's first name, so a fresh install greets its owner.
        "user_name": (os.environ.get("USERNAME") or "there").split()[0].split(".")[0].title(),
        "assistant_name": "ARGUS",
        "timezone": "",                 # informational; times follow the system clock
        "work_hours": {"start": "10:30", "end": "18:30", "days": [0, 1, 2, 3, 4]},
        # Where one day ends and the next begins, for timesheets. Work at 01:00
        # belongs to the day before, not to a day that "starts" at midnight.
        "day_starts_at": "04:00",
    },
    "daemon": {
        "web_port": 7842,
        "tray": True,
        "heartbeat_seconds": 60,
    },
    "sensors": {
        "window":     {"enabled": True,  "poll_seconds": 2,  "min_session_seconds": 3},
        "idle":       {"enabled": True,  "afk_seconds": 180},
        "system":     {"enabled": True,  "poll_seconds": 30},
        "process":    {"enabled": True,  "poll_seconds": 10},
        "files":      {"enabled": True,  "debounce_seconds": 20},
        "browser":    {"enabled": True,  "poll_seconds": 300},
        "git":        {"enabled": True,  "poll_seconds": 600},
        "network":    {"enabled": True,  "poll_seconds": 120},
        "screenshot": {"enabled": False, "poll_seconds": 300, "retain_days": 3, "max_width": 1280},
        "clipboard":  {"enabled": False, "poll_seconds": 5},
    },
    "watch_dirs": ["~/Desktop", "~/Documents", "~/Downloads"],
    "watch_dir_ignores": [
        "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
        ".next", ".cache", "AppData", "$RECYCLE.BIN", ".pytest_cache", "site-packages",
    ],
    "git_repos": {
        "scan_roots": ["~/Desktop", "~"],
        "scan_depth": 2,
    },
    # What makes something "work" or "personal". Nothing is built in - every
    # marker is yours. Matched case-insensitively against window titles, project
    # names and paths; personal wins when both match.
    "realms": {
        "work_markers": [],            # e.g. ["acme", "atlas", "jira.acme.example"]
        "personal_markers": [],        # e.g. ["portfolio", "side-project"]
        # Terminal titles that mean "a remote session" (ssh is always one).
        "remote_markers": [],          # e.g. ["build-box", "deploy@"]
        # `gh` accounts whose pull requests are office work.
        "work_github_logins": [],
    },
    # The thread engine: which folders contain projects, and which folder names
    # are never a project. Empty `roots` = threads.DEFAULT_ROOTS.
    "threads": {"roots": [], "non_projects": []},
    "privacy": {
        # Folders / projects that are never recorded at all: no file events,
        # no commits, no project card, window titles stored as "<private>".
        "private_projects": ["_secure"],
        # Apps whose window titles are never stored (only "<private>" + duration).
        "private_apps": [
            "keepass", "keepassxc", "bitwarden", "1password", "lastpass",
            "dashlane", "authy", "protonpass", "windowsterminal-vault",
        ],
        # Any window title containing these (case-insensitive) is redacted.
        "private_title_words": [
            "password", "passphrase", "otp", "one-time", "seed phrase",
            "private browsing", "incognito", "inprivate", "net banking",
            "netbanking", "internet banking", "credit card", "cvv", "aadhaar",
        ],
        # Browser domains recorded as domain-only, never full URL/title.
        "sensitive_domains": [
            "bank", "sbi", "hdfc", "icici", "axisbank", "paytm", "phonepe",
            "razorpay", "stripe.com/dashboard", "accounts.google.com",
            "mail.google.com/mail/u/0/#settings", "health", "medical",
        ],
        # Never store keystrokes. Hard-coded off; here for transparency.
        "keylogging": False,
        "retain_days": {"events": 400, "metrics": 90, "screenshots": 3,
                        "runs": 30, "alerts": 90, "mail": 60,
                        # Shorter lives for the chatty, low-value sources: network
                        # snapshots were ~4,500 rows a day, kept for 400 days.
                        "events_by_source": {"net": 30, "process": 90}},
    },
    # Nightly snapshot of data/argus.db into data/backups (newest `keep` kept).
    "backup": {"enabled": True, "keep": 5},
    "brain": {
        # order tried: claude CLI (uses your Claude Code login, no API key), gemini, anthropic
        "providers": ["claude_cli", "gemini", "anthropic"],
        "claude_cli_model": "sonnet",
        "gemini_model": "gemini-2.5-flash",
        "anthropic_model": "claude-sonnet-5",
        "timeout_seconds": 180,
        "max_daily_calls": 200,
        # One line of the briefing voice: the English you want and how times read,
        # e.g. "British English, 12h clock." Empty = plain English, 24h clock.
        "style": "",
    },
    "notify": {
        "enabled": True,
        "voice": False,
        "voice_name": "Zira",
        "quiet_hours": {"start": "01:30", "end": "08:00"},
        "min_severity": "info",
    },
    # the Board tab: when the "Plan your day" reminder fires (work days only)
    # (hotkey: quick capture from any app; "" turns it off)
    "board": {"enabled": True, "plan_at": "09:15", "shutdown_at": "18:45",
              "hotkey": "ctrl+alt+space", "drift_nudges": True},
    "devtools": {"hygiene": True},
    "github": {"enabled": True},
    "digests": {
        "morning_brief": {"enabled": True, "at": "08:30"},
        "evening_wrap": {"enabled": True, "at": "23:30"},
        "weekly_retro": {"enabled": True, "day": 6, "at": "20:00"},
        "journal": {"enabled": True},
    },
    "focus": {
        "distraction_apps": ["youtube", "netflix", "instagram", "twitter", "x.com",
                              "reddit", "primevideo", "hotstar", "steam", "discord"],
        "deep_work_min_minutes": 25,
        "switch_storm_per_10min": 25,
        "nudge_cooldown_minutes": 45,
    },
    "google": {
        "enabled": True,
        "poll_seconds": 300,
        "calendar_alert_minutes": [10, 2],
        "mail_vip": [],
        "mail_ignore_senders": ["noreply@", "no-reply@", "newsletter@", "notifications@"],
        # What a newly connected account is allowed to touch until you say otherwise.
        "default_features": ["calendar", "mail", "tasks", "drive"],
        # Account that receives tasks created with `argus task add`.
        # Empty = the first connected account.
        "tasks_account": "",
        # ARGUS may read, and may create events/drafts/tasks. It may never
        # delete anything or send mail on any account. Flip to True only if
        # you deliberately want to hand it destructive power.
        "allow_destructive": False,
        # Per-account overrides, keyed by the label you gave at login. Useful when
        # a work account should contribute meetings but not have its mail read:
        #   accounts:
        #     work:     {features: [calendar]}
        #     personal: {features: [calendar, mail, tasks, drive]}
        #     school:   {features: []}
        "accounts": {},
    },
    "automations": {
        "focus_guardian":    {"enabled": True},
        "meeting_sentinel":  {"enabled": True},
        "system_watchdog":   {"enabled": True, "disk_free_gb": 12, "cpu_sustained_pct": 92,
                               "cpu_sustained_minutes": 10, "battery_low_pct": 20},
        "repo_janitor":      {"enabled": True, "stale_hours": 24},
        "inbox_triage":      {"enabled": True},
        "standup_writer":    {"enabled": True},
        "security_watch":    {"enabled": True},
        "break_reminder":    {"enabled": True, "after_minutes": 90},
        "downloads_janitor": {"enabled": True, "warn_gb": 15},
    },
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_mtime: float = 0.0
_checked: float = 0.0
_error: str = ""
CHECK_EVERY = 2.0
_log = logging.getLogger("argus.config")   # not util.log: util imports this module


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# What the dashboard's Settings page changes lives in data/settings.json,
# layered over config.yaml - so a click never rewrites (or reformats, or
# loses the comments of) the file you edit by hand. Only these keys can be
# set that way, each with the type it must have.
SETTINGS_PATH = DATA / "settings.json"
SETTABLE: dict[str, str] = {
    "identity.user_name": "str",
    "identity.work_hours.start": "time", "identity.work_hours.end": "time",
    "identity.work_hours.days": "days",
    "notify.quiet_hours.start": "time", "notify.quiet_hours.end": "time",
    "notify.voice": "bool",
    "board.plan_at": "time", "board.shutdown_at": "time",
    "board.drift_nudges": "bool", "board.hotkey": "str",
    "automations.break_reminder.enabled": "bool",
    "automations.focus_guardian.enabled": "bool",
    "automations.standup_writer.enabled": "bool",
    "automations.repo_janitor.enabled": "bool",
    "automations.downloads_janitor.enabled": "bool",
    "digests.morning_brief.enabled": "bool", "digests.evening_wrap.enabled": "bool",
    "digests.weekly_retro.enabled": "bool",
    "github.enabled": "bool", "devtools.hygiene": "bool",
}


def _settings() -> dict[str, Any]:
    try:
        import json
        d = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _coerce(kind: str, value: Any) -> Any:
    import re
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("expected true or false")
    if kind == "time":
        if isinstance(value, str) and re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", value.strip()):
            h, m = value.strip().split(":")
            return f"{int(h):02d}:{m}"
        raise ValueError("expected a time like 09:15")
    if kind == "days":
        if isinstance(value, list) and value and all(isinstance(d, int) and 0 <= d <= 6 for d in value):
            return sorted(set(value))
        raise ValueError("expected weekdays 0 (Mon) - 6 (Sun)")
    if isinstance(value, str) and 0 < len(value.strip()) <= 60:
        return value.strip()
    raise ValueError("expected a short text")


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Apply {dotted.key: value} from the Settings page, validated."""
    import json
    current = _settings()
    for key, value in updates.items():
        if key not in SETTABLE:
            raise KeyError(f"{key} cannot be changed from the dashboard")
        node = current
        *path, leaf = key.split(".")
        for part in path:
            node = node.setdefault(part, {})
        node[leaf] = _coerce(SETTABLE[key], value)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(current, indent=1), encoding="utf-8")
    load(force=True)
    return {k: get(k) for k in SETTABLE}


def load(force: bool = False) -> dict[str, Any]:
    """Return merged config, reloading automatically when config.yaml changes.

    A config.yaml that fails to parse used to be replaced by an empty dict -
    i.e. every customisation (quiet hours, work hours, privacy lists) silently
    reverted to defaults on the next save with a stray tab in it. Now the last
    good config stays in force and the error is kept for `argus doctor`.
    """
    global _cache, _mtime, _error, _checked
    # Looking at config.yaml's mtime is a stat() syscall, and get() is called
    # everywhere - once per file event in a threads rebuild (35k stats, 4.3s of
    # a 10s rebuild), several times per window poll. Edits only need to land
    # "within a minute", so look at most every CHECK_EVERY seconds.
    if _cache is not None and not force and time.monotonic() - _checked < CHECK_EVERY:
        return _cache
    with _lock:
        _checked = time.monotonic()
        try:
            m = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            m = 0.0
        try:                         # the Settings page's file counts as a change too
            m += SETTINGS_PATH.stat().st_mtime / 1e3
        except (FileNotFoundError, OSError):
            pass
        if _cache is None or force or m != _mtime:
            user: dict[str, Any] = {}
            err = ""
            if CONFIG_PATH.exists():
                try:
                    user = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
                    if not isinstance(user, dict):
                        raise ValueError("the top level must be a mapping of settings")
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
            _mtime = m
            if err:
                _error = err
                _log.error("config.yaml not applied, keeping the previous settings: %s", err)
                if _cache is not None:
                    return _cache
                user = {}
            else:
                _error = ""
            _cache = _deep_merge(_deep_merge(DEFAULTS, user), _settings())
            _cache["privacy"]["keylogging"] = False  # not negotiable
        return _cache


def last_error() -> str:
    """Why the current config.yaml is not in force, or '' when it is."""
    load()
    return _error


def _hhmm_ok(v: Any) -> bool:
    if isinstance(v, int) and not isinstance(v, bool):
        return 0 <= v < 24 * 60          # YAML's base-60 reading of an unquoted HH:MM
    try:
        h, _, m = str(v).partition(":")
        return 0 <= int(h) <= 23 and 0 <= int(m or 0) <= 59 and ":" in str(v)
    except (TypeError, ValueError):
        return False


def problems(cfg: dict[str, Any] | None = None) -> list[str]:
    """Human-readable problems with the merged config. Empty means healthy."""
    cfg = cfg or load()
    out: list[str] = []
    if _error:
        out.append(f"config.yaml is not being applied: {_error}")

    for top in cfg:
        if top not in DEFAULTS and top not in ("threads",):
            out.append(f"unknown top-level key '{top}' (typo?)")

    port = cfg.get("daemon", {}).get("web_port")
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        out.append(f"daemon.web_port must be a number 1024-65535, got {port!r}")

    for name, sensor in (cfg.get("sensors") or {}).items():
        if not isinstance(sensor, dict):
            out.append(f"sensors.{name} must be a mapping")
            continue
        poll = sensor.get("poll_seconds")
        if poll is not None and not (isinstance(poll, (int, float)) and poll > 0):
            out.append(f"sensors.{name}.poll_seconds must be a positive number, got {poll!r}")

    times = {
        "notify.quiet_hours.start": cfg.get("notify", {}).get("quiet_hours", {}).get("start"),
        "notify.quiet_hours.end": cfg.get("notify", {}).get("quiet_hours", {}).get("end"),
        "identity.work_hours.start": cfg.get("identity", {}).get("work_hours", {}).get("start"),
        "identity.work_hours.end": cfg.get("identity", {}).get("work_hours", {}).get("end"),
        "identity.day_starts_at": cfg.get("identity", {}).get("day_starts_at"),
    }
    for k, d in (cfg.get("digests") or {}).items():
        if isinstance(d, dict) and "at" in d:
            times[f"digests.{k}.at"] = d["at"]
    for k, v in times.items():
        if v not in (None, "") and not _hhmm_ok(v):
            out.append(f"{k} must be HH:MM (quoted in YAML), got {v!r}")

    days = cfg.get("identity", {}).get("work_hours", {}).get("days", [])
    if not isinstance(days, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        out.append(f"identity.work_hours.days must be a list of 0 (Mon) .. 6 (Sun), got {days!r}")
    wd = cfg.get("digests", {}).get("weekly_retro", {}).get("day", 6)
    if not isinstance(wd, int) or not 0 <= wd <= 6:
        out.append(f"digests.weekly_retro.day must be 0 (Mon) .. 6 (Sun), got {wd!r}")

    sev = cfg.get("notify", {}).get("min_severity", "info")
    if sev not in ("info", "warn", "urgent"):
        out.append(f"notify.min_severity must be info, warn or urgent, got {sev!r}")

    provs = cfg.get("brain", {}).get("providers", [])
    unknown = [p for p in provs or [] if p not in ("claude_cli", "gemini", "anthropic")]
    if unknown:
        out.append(f"brain.providers has unknown entries {unknown}")

    for k in ("watch_dirs", "watch_dir_ignores"):
        if not isinstance(cfg.get(k), list):
            out.append(f"{k} must be a list")
    return out


def get(path: str, default: Any = None) -> Any:
    """Dotted lookup: get('sensors.window.poll_seconds')."""
    node: Any = load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def write_default_config() -> None:
    if CONFIG_PATH.exists():
        return
    CONFIG_PATH.write_text(
        "# ARGUS config. Anything omitted falls back to argus/config.py DEFAULTS.\n"
        "# Edit and save - the daemon picks changes up within a minute.\n\n"
        + yaml.safe_dump(DEFAULTS, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
