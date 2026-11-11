"""System vitals: CPU, memory, disk, battery, network throughput, process count."""
from __future__ import annotations

import time

import psutil

from .. import config, db
from ..util import log

lg = log("sensor.system")


def _disk_free_gb() -> float:
    best = 0.0
    try:
        best = psutil.disk_usage("C:\\").free / 1e9
    except Exception:
        pass
    return round(best, 2)


def _temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures()  # rarely populated on Windows
        for readings in temps.values():
            for r in readings:
                if r.current:
                    return float(r.current)
    except Exception:
        pass
    return None


def snapshot(prev: tuple[float, float, float] | None = None) -> dict:
    """One vitals sample. `prev` is (ts, bytes_sent, bytes_recv) for rate maths."""
    now = time.time()
    io = psutil.net_io_counters()
    up = down = 0.0
    if prev:
        dt = max(0.001, now - prev[0])
        up = max(0.0, (io.bytes_sent - prev[1]) / dt)
        down = max(0.0, (io.bytes_recv - prev[2]) / dt)

    batt = psutil.sensors_battery()
    return {
        "ts": now,
        "cpu": psutil.cpu_percent(interval=None),
        "mem": psutil.virtual_memory().percent,
        "disk_free_gb": _disk_free_gb(),
        "battery": float(batt.percent) if batt else None,
        "plugged": int(bool(batt.power_plugged)) if batt else None,
        "net_up": round(up, 1),
        "net_down": round(down, 1),
        "procs": len(psutil.pids()),
        "temp": _temp(),
        "_counters": (now, io.bytes_sent, io.bytes_recv),
    }


def run(stop) -> None:
    psutil.cpu_percent(interval=None)  # prime the counter
    prev = None
    last_plugged: int | None = None

    while not stop.is_set():
        poll = float(config.get("sensors.system.poll_seconds", 30))
        try:
            s = snapshot(prev)
            prev = s.pop("_counters")
            db.add_metric(**s)

            if s["plugged"] is not None and s["plugged"] != last_plugged:
                if last_plugged is not None:
                    db.log_event("power", "plugged" if s["plugged"] else "unplugged",
                                 "battery", f"{s['battery']:.0f}%", value=s["battery"])
                last_plugged = s["plugged"]
        except Exception:
            lg.exception("system sample failed")
        stop.wait(poll)
    lg.info("system sensor stopped")
