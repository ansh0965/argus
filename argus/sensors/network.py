"""Network + attack-surface sensor.

Two jobs:
  - remember which remote hosts your machine talks to (aggregate, not payloads)
  - notice new listening ports and new autostart entries, which is how most
    unwanted software announces itself
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import psutil

from .. import config, db, pause
from ..util import log, run

lg = log("sensor.network")


def is_local(ip: str) -> bool:
    """Private, loopback or link-local - traffic that never leaves the building.

    The old prefix list had "172.2", which matched public 172.2.x.x and
    172.200-255.x.x while missing nothing it meant to catch.
    """
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_private or a.is_loopback or a.is_link_local


# Reverse DNS. This used to call socket.setdefaulttimeout(1.5), which is
# process-wide: every socket created afterwards without an explicit timeout -
# notably httplib2 underneath the Google API client - inherited 1.5 seconds,
# which lines up with the steady trickle of "read operation timed out" from
# Gmail and Calendar syncs. It did not even bound the lookup it was meant for,
# because gethostbyaddr() ignores socket timeouts. A small pool with a real
# deadline does both jobs properly.
_RDNS_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="rdns")
_rdns_cache: dict[str, tuple[str, float]] = {}
_rdns_lock = threading.Lock()
_RDNS_OK_TTL = 24 * 3600
_RDNS_FAIL_TTL = 1800
_RDNS_MAX = 4096


def _lookup(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


def resolve_many(ips, deadline: float = 3.0) -> dict[str, str]:
    """ip -> hostname (or the ip itself), never blocking longer than `deadline`."""
    now = time.time()
    out: dict[str, str] = {}
    todo = []
    with _rdns_lock:
        for ip in set(ips):
            hit = _rdns_cache.get(ip)
            if hit and hit[1] > now:
                out[ip] = hit[0]
            else:
                todo.append(ip)
    if todo:
        futs = {_RDNS_POOL.submit(_lookup, ip): ip for ip in todo}
        done, _ = wait(futs, timeout=deadline)
        with _rdns_lock:
            if len(_rdns_cache) > _RDNS_MAX:
                _rdns_cache.clear()
            for f, ip in futs.items():
                name = f.result() if f in done else ""
                ttl = _RDNS_OK_TTL if name else _RDNS_FAIL_TTL
                if f in done:
                    _rdns_cache[ip] = (name or ip, now + ttl)
                out[ip] = name or ip
    return out


def connections() -> list[dict]:
    out: list[dict] = []
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception:
        return out
    for c in conns:
        if c.status != psutil.CONN_ESTABLISHED or not c.raddr:
            continue
        try:
            name = psutil.Process(c.pid).name() if c.pid else "?"
        except Exception:
            name = "?"
        out.append({"pid": c.pid, "proc": name, "ip": c.raddr.ip,
                    "port": c.raddr.port,
                    "local": is_local(c.raddr.ip)})
    return out


def listening_ports() -> list[dict]:
    out: list[dict] = []
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception:
        return out
    for c in conns:
        if c.status != psutil.CONN_LISTEN or not c.laddr:
            continue
        try:
            name = psutil.Process(c.pid).name() if c.pid else "?"
        except Exception:
            name = "?"
        out.append({"port": c.laddr.port, "addr": c.laddr.ip, "proc": name, "pid": c.pid})
    # de-duplicate on (port, proc)
    uniq = {(d["port"], d["proc"]): d for d in out}
    return sorted(uniq.values(), key=lambda d: d["port"])


def autostart_entries() -> list[dict]:
    """Registry Run keys + Startup folders. Used for drift detection."""
    entries: list[dict] = []
    ps = (
        "$o=@();"
        "foreach($k in 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run',"
        "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run'){"
        "if(Test-Path $k){$p=Get-ItemProperty $k;"
        "$p.PSObject.Properties|Where-Object{$_.Name -notlike 'PS*'}|"
        "ForEach-Object{$o+=[pscustomobject]@{n=$_.Name;v=[string]$_.Value;k=$k}}}};"
        "$o|ConvertTo-Json -Compress"
    )
    rc, out, _ = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=45)
    if rc == 0 and out:
        import json
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            for d in data:
                entries.append({"name": d.get("n", ""), "cmd": (d.get("v") or "")[:300],
                                "where": d.get("k", "")})
        except Exception:
            pass

    from pathlib import Path
    for folder in (
        Path(r"~\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup").expanduser(),
        Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup"),
    ):
        if folder.is_dir():
            for f in folder.iterdir():
                if f.name.lower() != "desktop.ini":
                    entries.append({"name": f.name, "cmd": str(f)[:300], "where": "StartupFolder"})
    return sorted(entries, key=lambda d: (d["where"], d["name"]))


# Which app talked to which host is recorded when the pair is first seen and
# then at most once an hour while it stays connected. Logging every open
# connection on every two-minute poll was 6,600 rows a day here - 2,500 of
# them one Chrome connection to Google - and said nothing new after the first.
RELOG_AFTER = 3600.0
_logged: dict[tuple[str, str], float] = {}


def collect_once(now: float | None = None) -> None:
    now = now or time.time()
    conns = connections()
    remote = [c for c in conns if not c["local"]]

    agg: dict[tuple[str, str], int] = {}
    for c in remote:
        agg[(c["proc"], c["ip"])] = agg.get((c["proc"], c["ip"]), 0) + 1

    top = sorted(agg.items(), key=lambda kv: -kv[1])[:40]
    fresh = [(k, n) for k, n in top
             if k not in _logged or now - _logged[k] >= RELOG_AFTER]
    names = resolve_many([ip for (_p, ip), _n in fresh])
    rows = [("net", "conn", proc, names.get(ip, ip), count, {"ip": ip}, None)
            for (proc, ip), count in fresh]
    db.log_events(rows)
    for k, _n in fresh:
        _logged[k] = now
    if len(_logged) > 5000:
        for k in [k for k, ts in _logged.items() if now - ts >= RELOG_AFTER]:
            del _logged[k]

    db.set_state("net_snapshot", {
        "ts": time.time(),
        "established": len(conns),
        "remote": len(remote),
        "listening": listening_ports()[:40],
    })


def run_loop(stop) -> None:
    while not stop.is_set():
        poll = float(config.get("sensors.network.poll_seconds", 120))
        try:
            if not pause.is_paused():
                collect_once()
        except Exception:
            lg.exception("network collect failed")
        stop.wait(poll)
    lg.info("network sensor stopped")
