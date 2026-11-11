"""Getting ARGUS's attention onto your screen.

Backends are tried in order and the first that works is remembered:
  1. win11toast  - real Windows 11 action-centre toast, supports buttons
  2. PowerShell WinRT  - same toast, no Python dependency
  3. tray balloon  - falls back to the system tray icon
  4. log only     - never crashes a caller

Quiet hours and severity gating happen here, once, so no caller has to think
about whether 3am is a good time to speak.
"""
from __future__ import annotations

import html
import threading
import time

from .. import config, db
from ..util import is_quiet_hours, log

lg = log("actions.notify")

SEVERITY_RANK = {"info": 0, "warn": 1, "urgent": 2}
_backend_lock = threading.Lock()
_chosen: str | None = None
_last_sent: dict[str, float] = {}

APP_ID = "ARGUS"


def dashboard(tab: str = "") -> str:
    """The dashboard, optionally on one tab - where a toast should lead."""
    url = f"http://127.0.0.1:{config.get('daemon.web_port', 7842)}/"
    return f"{url}#{tab}" if tab else url


def safe_url(url: str | None) -> str:
    """Only web links and our own dashboard may be launched from a toast.

    Some links come from data other people control - a meeting's video link
    is whatever the invite says. A toast launches its URL through the shell,
    so a crafted "file:" or "ms-settings:" link would be one click from
    running; anything that is not https (or the local dashboard) falls back
    to the dashboard.
    """
    u = (url or "").strip()
    if u.startswith("https://") or u.startswith(dashboard()):
        return u
    return dashboard()


def _win11toast(title: str, body: str, severity: str, actions: list[str],
                url: str) -> bool:
    from win11toast import notify as _n
    kw: dict = {
        "title": title,
        "body": body[:600],
        "app_id": APP_ID,
        # Without on_click the library's template launches the bare URL
        # "http:" when the toast is clicked - i.e. clicking did nothing useful.
        "on_click": url,
        "audio": {"silent": "true"} if severity == "info" else None,
        "duration": "long" if severity == "urgent" else "short",
    }
    if actions:
        kw["buttons"] = actions[:3]
    _n(**{k: v for k, v in kw.items() if v is not None})
    return True


def _ps_toast(title: str, body: str, severity: str, actions: list[str], url: str) -> bool:
    from ..util import run
    xml = (
        f"<toast activationType='protocol' launch='{html.escape(url)}'>"
        "<visual><binding template='ToastGeneric'>"
        f"<text>{html.escape(title)}</text>"
        f"<text>{html.escape(body[:400])}</text>"
        "</binding></visual>"
        + ("<audio silent='true'/>" if severity == "info" else "")
        + "</toast>"
    )
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument,"
        " ContentType=WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml(@'\n{xml}\n'@);"
        "$t = New-Object Windows.UI.Notifications.ToastNotification $x;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'Microsoft.WindowsPowerShell_8wekyb3d8bbwe!Microsoft.PowerShell').Show($t);"
    )
    rc, _, err = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=25)
    if rc != 0:
        raise RuntimeError(err[:200])
    return True


def _tray_balloon(title: str, body: str, severity: str, actions: list[str],
                  url: str) -> bool:
    from . import tray
    return tray.balloon(title, body)


BACKENDS = [("win11toast", _win11toast), ("ps_toast", _ps_toast), ("tray", _tray_balloon)]


def _deliver(title: str, body: str, severity: str, actions: list[str],
             url: str = "") -> str:
    global _chosen
    with _backend_lock:
        order = ([b for b in BACKENDS if b[0] == _chosen] +
                 [b for b in BACKENDS if b[0] != _chosen]) if _chosen else BACKENDS
    for name, fn in order:
        try:
            if fn(title, body, severity, actions, url or dashboard()):
                with _backend_lock:
                    _chosen = name
                return name
        except Exception as e:
            lg.debug("toast backend %s failed: %s", name, str(e)[:160])
    return "none"


def _last_fired(rule: str) -> float:
    """When this rule last produced an alert, surviving restarts.

    The in-memory stamp alone reset on every daemon restart, and it was only
    set on *delivery* - so a rule suppressed by quiet hours or a pause (or one
    whose toast backend failed) re-recorded itself on every tick. That is how
    four dead Google tokens wrote ~3,900 identical alert rows at one per five
    minutes each, while the 12-hour cooldown was nominally in force.
    """
    last = _last_sent.get(rule, 0.0)
    try:
        r = db.q1("SELECT MAX(ts) t FROM alerts WHERE rule=?", (rule,))
        if r and r["t"]:
            last = max(last, float(r["t"]))
    except Exception:
        pass
    return last


def send(title: str, body: str = "", severity: str = "info", rule: str = "manual",
         actions: list[str] | None = None, force: bool = False,
         cooldown_seconds: float = 0, speak: bool | None = None,
         record: bool = True, url: str | None = None) -> bool:
    """Notify the user. Returns True if it actually reached the screen.

    A cooldown covers the whole alert, not just the toast: inside it, a repeat
    is neither shown nor written down, because the first one already was.
    """
    cfg = config.get("notify", {})
    min_rank = SEVERITY_RANK.get(cfg.get("min_severity", "info"), 0)
    rank = SEVERITY_RANK.get(severity, 0)

    if cooldown_seconds and not force:
        if time.time() - _last_fired(rule) < cooldown_seconds:
            lg.debug("suppressed %s (cooldown)", rule)
            return False

    alert_id = db.add_alert(rule, title, body, severity) if record else 0
    _last_sent[rule] = time.time()

    if not force:
        if not cfg.get("enabled", True):
            return False
        if rank < min_rank:
            return False
        # Pause used to be cosmetic: it recoloured the tray icon and nothing
        # else consulted it, so ARGUS kept notifying while claiming to be
        # paused. In a tool that watches everything you do, that is a trust
        # defect. Only `urgent` still gets through.
        from .. import pause
        if pause.is_paused() and rank < SEVERITY_RANK["urgent"]:
            lg.debug("suppressed %s (paused)", rule)
            return False
        if is_quiet_hours() and rank < SEVERITY_RANK["urgent"]:
            lg.debug("suppressed %s (quiet hours)", rule)
            return False

    backend = _deliver(title, body, severity, actions or [], url=safe_url(url))
    ok = backend != "none"
    if ok:
        if alert_id:
            with db.tx() as c:
                c.execute("UPDATE alerts SET notified=1 WHERE id=?", (alert_id,))

    want_voice = cfg.get("voice", False) if speak is None else speak
    if ok and want_voice and not is_quiet_hours():
        from .voice import say
        say(f"{title}. {body}" if body else title)

    try:                                   # the phone, for what matters away from the desk
        from . import telegram
        telegram.forward(title, body, severity, rule)
    except Exception:
        lg.debug("telegram forward failed", exc_info=True)

    lg.info("notify[%s/%s] %s -> %s", severity, rule, title[:60], backend)
    return ok


def recent(limit: int = 30, unacked_only: bool = False,
           collapse: bool = False) -> list[dict]:
    """Newest alerts first. With `collapse`, repeats of the same alert (same
    rule, title and body) are one row carrying `count` and `first_ts`, so a
    burst - like the ~3,900 identical sign-in alerts from before cooldowns
    were persisted - reads as one line instead of filling the list."""
    sql = "SELECT * FROM alerts"
    if unacked_only:
        sql += " WHERE acked=0"
    sql += " ORDER BY ts DESC LIMIT ?"
    if not collapse:
        return db.rows_to_dicts(db.q(sql, (limit,)))
    groups: dict[tuple, dict] = {}
    for r in db.rows_to_dicts(db.q(sql, (min(limit * 50, 5000),))):
        key = (r["rule"], r["title"], r["body"])
        g = groups.get(key)
        if g is None:
            if len(groups) >= limit:
                continue
            groups[key] = {**r, "count": 1, "first_ts": r["ts"]}
        else:
            g["count"] += 1
            g["first_ts"] = r["ts"]
            g["acked"] = g["acked"] and r["acked"]
    return list(groups.values())


def ack(alert_id: int | None = None) -> int:
    with db.tx() as c:
        if alert_id:
            return c.execute("UPDATE alerts SET acked=1 WHERE id=?", (alert_id,)).rowcount
        return c.execute("UPDATE alerts SET acked=1 WHERE acked=0").rowcount
