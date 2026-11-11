"""Optional visual memory. OFF by default - flip sensors.screenshot.enabled to use it.

Rules baked in, not configurable away:
  - nothing is captured while a private app or private window title is focused
  - nothing leaves this machine; frames live under data/screenshots
  - frames are downscaled, JPEG-compressed and auto-deleted after retain_days
"""
from __future__ import annotations

import time
from datetime import datetime

from .. import classify, config, db, pause, privacy
from ..paths import SHOTS
from ..util import log
from .window import foreground, idle_seconds, session_locked

lg = log("sensor.screenshot")


def capture_once() -> str | None:
    if pause.is_paused():
        return None
    exe, title, _ = foreground()
    if not exe or session_locked():
        return None
    app, _cat, _realm = classify.classify(exe, title)
    safe_title, private = privacy.clean_title(exe, app, title)
    if private:
        db.log_event("screenshot", "skipped", app, "private window")
        return None
    if idle_seconds() > float(config.get("sensors.idle.afk_seconds", 180)):
        return None

    try:
        import mss
        from PIL import Image
    except Exception as e:
        lg.warning("screenshot deps missing: %s", e)
        return None

    max_w = int(config.get("sensors.screenshot.max_width", 1280))
    now = datetime.now()
    day_dir = SHOTS / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / f"{now.strftime('%H%M%S')}_{app.replace(' ', '')[:20]}.jpg"

    try:
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[0])
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        if img.width > max_w:
            img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
        img.save(out, "JPEG", quality=68, optimize=True)
    except Exception:
        lg.exception("capture failed")
        return None

    db.log_event("screenshot", "captured", app, str(out.name),
                 value=out.stat().st_size, meta={"title": safe_title, "path": str(out)})
    return str(out)


def purge(retain_days: int | None = None) -> int:
    days = retain_days if retain_days is not None else int(
        config.get("sensors.screenshot.retain_days", 3))
    cutoff = time.time() - days * 86400
    removed = 0
    for d in SHOTS.iterdir() if SHOTS.is_dir() else []:
        if not d.is_dir():
            continue
        try:
            # days <= 0 means "all of them": comparing mtimes against "now"
            # could spare a folder written a moment ago, whose filesystem
            # timestamp can land a hair after time.time().
            if days <= 0 or d.stat().st_mtime < cutoff:
                for f in d.iterdir():
                    f.unlink(missing_ok=True)
                    removed += 1
                d.rmdir()
        except Exception:
            continue
    return removed


def run(stop) -> None:
    while not stop.is_set():
        cfg = config.get("sensors.screenshot", {})
        if not cfg.get("enabled"):
            stop.wait(60)
            continue
        try:
            capture_once()
            purge()
        except Exception:
            lg.exception("screenshot loop error")
        stop.wait(float(cfg.get("poll_seconds", 300)))
    lg.info("screenshot sensor stopped")
