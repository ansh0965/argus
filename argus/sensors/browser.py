"""Browser history sensor for Chromium-family browsers (Chrome, Edge, Brave, Opera).

The live History DB is locked while the browser runs, so we copy it to temp and
read the copy. A per-profile watermark keeps the read incremental.

Sensitive domains are recorded host-only, and every URL is stripped of its query
string before storage - see argus.privacy.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from .. import classify, config, db, pause, privacy
from ..util import log

lg = log("sensor.browser")

# WebKit epoch: microseconds since 1601-01-01.
WEBKIT_OFFSET = 11644473600


def _to_unix(webkit_us: int) -> float:
    return (webkit_us / 1_000_000) - WEBKIT_OFFSET if webkit_us else 0.0


def _to_webkit(unix_ts: float) -> int:
    return int((unix_ts + WEBKIT_OFFSET) * 1_000_000)


BROWSER_ROOTS: list[tuple[str, str]] = [
    ("Chrome", r"~\AppData\Local\Google\Chrome\User Data"),
    ("Edge", r"~\AppData\Local\Microsoft\Edge\User Data"),
    ("Brave", r"~\AppData\Local\BraveSoftware\Brave-Browser\User Data"),
    ("Opera", r"~\AppData\Roaming\Opera Software\Opera Stable"),
    ("Vivaldi", r"~\AppData\Local\Vivaldi\User Data"),
]


def profiles() -> list[tuple[str, Path]]:
    """Discover every (browser_profile_label, History path) on this machine."""
    found: list[tuple[str, Path]] = []
    for name, root in BROWSER_ROOTS:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        direct = base / "History"
        if direct.is_file():
            found.append((name, direct))
        for sub in base.iterdir():
            if not sub.is_dir():
                continue
            if sub.name == "Default" or sub.name.startswith("Profile "):
                h = sub / "History"
                if h.is_file():
                    label = name if sub.name == "Default" else f"{name}/{sub.name}"
                    found.append((label, h))
    return found


def _read_since(history: Path, since_unix: float, limit: int = 3000) -> list[dict]:
    tmp = Path(tempfile.gettempdir()) / f"argus_hist_{abs(hash(str(history))) % 10**8}.db"
    try:
        shutil.copy2(history, tmp)
    except Exception as e:
        lg.debug("copy failed for %s: %s", history, e)
        return []

    rows: list[dict] = []
    try:
        c = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        cur = c.execute(
            "SELECT v.visit_time, v.transition, u.url, u.title "
            "FROM visits v JOIN urls u ON u.id = v.url "
            "WHERE v.visit_time > ? ORDER BY v.visit_time ASC LIMIT ?",
            (_to_webkit(since_unix), limit),
        )
        for r in cur:
            rows.append({
                "ts": _to_unix(r["visit_time"]),
                "url": r["url"] or "",
                "title": r["title"] or "",
                "transition": r["transition"] & 0xFF,
            })
        c.close()
    except Exception as e:
        lg.debug("read failed for %s: %s", history, e)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    return rows


# Chromium core transition types worth naming in the UI.
TRANSITIONS = {0: "link", 1: "typed", 2: "bookmark", 3: "auto_subframe",
               4: "manual_subframe", 5: "generated", 6: "start_page",
               7: "form_submit", 8: "reload", 9: "keyword", 10: "keyword_generated"}


def _signature(history: Path) -> list:
    """(mtime, size) of the History file and its journal - changes when the
    browser writes a visit."""
    history = Path(history)
    sig = []
    for f in (history, history.with_name(history.name + "-journal"),
              history.with_name(history.name + "-wal")):
        try:
            st = f.stat()
            sig += [round(st.st_mtime, 3), st.st_size]
        except OSError:
            sig += [0, 0]
    return sig


def collect_once() -> int:
    total = 0
    paused = pause.intervals()
    for label, path in profiles():
        # Reading means copying the whole History database first (it is locked
        # while the browser runs) - ~48 MB across five profiles here, every five
        # minutes, ~14 GB a day of temp writes, mostly for profiles untouched
        # for days. Skip a profile whose files have not changed since last time.
        sig_key = f"browser_sig:{label}"
        sig = _signature(path)
        if db.get_state(sig_key) == sig:
            continue

        wm_key = f"browser_watermark:{label}"
        since = float(db.get_state(wm_key, 0) or 0)
        if since == 0:
            since = time.time() - 7 * 86400  # first run: seed with a week of context

        visits = _read_since(path, since)
        db.set_state(sig_key, sig)
        if not visits:
            continue

        rows = []
        newest = since
        for v in visits:
            newest = max(newest, v["ts"])
            if pause.was_paused_at(v["ts"], paused):
                continue                 # you asked not to be watched then
            host = privacy.host_of(v["url"])
            if not host:
                continue
            if privacy.is_sensitive_domain(host):
                url, title = f"https://{host}/<private>", "<private>"
            else:
                url = privacy.trim_url(v["url"])
                title = privacy.scrub(v["title"])[:300]
            cat = classify.category_from_title(f"{host} {title}", "browse")
            rows.append(("browser", "visit", host, title, None,
                         {"url": url, "browser": label, "category": cat,
                          "transition": TRANSITIONS.get(v["transition"], "other")},
                         v["ts"]))

        db.log_events(rows)
        db.set_state(wm_key, newest)
        total += len(rows)
    return total


def top_domains(hours: float = 24, limit: int = 15) -> list[dict]:
    since = time.time() - hours * 3600
    return db.rows_to_dicts(db.q(
        "SELECT subject AS host, COUNT(*) AS visits, MAX(ts) AS last "
        "FROM events WHERE source='browser' AND ts > ? "
        "GROUP BY subject ORDER BY visits DESC LIMIT ?", (since, limit)))


def run(stop) -> None:
    while not stop.is_set():
        poll = float(config.get("sensors.browser.poll_seconds", 300))
        try:
            n = collect_once()
            if n:
                lg.info("collected %d visits", n)
        except Exception:
            lg.exception("browser collect failed")
        stop.wait(poll)
    lg.info("browser sensor stopped")
