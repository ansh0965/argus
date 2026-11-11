"""Developer hygiene: things a repo or a dev box quietly accumulates.

  - merged branches nobody deleted       -> one board card per repo, with the
                                            command to copy (never deleted here)
  - a secret staged for commit           -> urgent alert, before it is committed
  - a huge file staged                   -> warning
  - .env tracked, or not gitignored      -> warning
  - dev servers left running             -> listed on the System page, stoppable

Reads only, except `stop_server`, which ends one process you chose.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import config, db
from .util import log, run

lg = log("devtools")

STALE_BRANCH_DAYS = 30
BIG_FILE_MB = 10
SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("OpenAI/Anthropic key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("Slack token", re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
]
DEV_PROCS = {"node.exe", "python.exe", "pythonw.exe", "java.exe", "deno.exe", "bun.exe",
             "php.exe", "ruby.exe", "dotnet.exe", "go.exe", "uvicorn.exe", "next-server"}


def _git(repo: str, *args: str, timeout: int = 20) -> tuple[int, str]:
    rc, out, _ = run(["git", "-C", repo, *args], timeout=timeout)
    return rc, out


# --------------------------------------------------------------------------- branches

def merged_branches(repo: str, now: float | None = None) -> list[str]:
    """Local branches merged into the default branch whose last commit is
    older than STALE_BRANCH_DAYS (never the current or the default branch)."""
    now = now or time.time()
    rc, head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    default = head.split("/", 1)[1] if rc == 0 and "/" in head else ""
    if not default:
        for cand in ("main", "master", "develop"):
            if _git(repo, "rev-parse", "--verify", "--quiet", cand)[0] == 0:
                default = cand
                break
    if not default:
        return []
    rc, cur = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    rc, out = _git(repo, "for-each-ref", "--merged", default,
                   "--format=%(refname:short) %(committerdate:unix)", "refs/heads")
    if rc != 0:
        return []
    stale = []
    for line in out.splitlines():
        name, _, ts = line.rpartition(" ")
        if name in (default, cur.strip(), "main", "master", "develop") or not ts.isdigit():
            continue
        if now - int(ts) > STALE_BRANCH_DAYS * 86400:
            stale.append(name)
    return stale


def branch_cards() -> str:
    from .brain import board
    from .sensors import git as gitsensor
    made = 0
    for repo in gitsensor.discover():
        stale = merged_branches(repo)
        origin = f"branches:{Path(repo).name.lower()}"
        existing = db.q1("SELECT * FROM tasks WHERE origin=?", (origin,))
        if len(stale) >= 2 and not existing:
            board.add(f"{Path(repo).name}: clean up {len(stale)} merged branches",
                      notes=", ".join(stale[:12]), origin=origin, source="devtools",
                      meta={"kind": "branches", "repo": repo,
                            "command": f'git -C "{repo}" branch -d ' + " ".join(stale)})
            made += 1
        elif existing and not stale and not existing["done"]:
            board.move(existing["id"], "done")
    return f"{made} branch clean-up card(s)" if made else "nothing new"


# --------------------------------------------------------------------------- hygiene

def staged_problems(repo: str) -> list[tuple[str, str]]:
    """(severity, message) for what is staged right now in `repo`."""
    out: list[tuple[str, str]] = []
    rc, names = _git(repo, "diff", "--cached", "--name-only")
    if rc == 0 and names.strip():
        for n in names.splitlines():
            p = Path(repo) / n
            try:
                if p.is_file() and p.stat().st_size > BIG_FILE_MB * 1_000_000:
                    out.append(("warn", f"{n} ({p.stat().st_size / 1e6:.0f} MB) is staged"))
            except OSError:
                pass
        rc, diff = _git(repo, "diff", "--cached", "-U0", "--no-color", timeout=30)
        added = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        for label, rx in SECRET_PATTERNS:
            if rx.search(added):
                out.append(("urgent", f"a {label} is staged for commit"))
    env = Path(repo) / ".env"
    if env.exists():
        if _git(repo, "ls-files", "--error-unmatch", ".env")[0] == 0:
            out.append(("warn", ".env is tracked by git"))
        elif _git(repo, "check-ignore", "-q", ".env")[0] != 0:
            out.append(("warn", ".env is not in .gitignore"))
    return out


def hygiene_scan() -> str:
    from .actions import notify
    from .sensors import git as gitsensor
    found = []
    for repo in gitsensor.discover():
        for sev, msg in staged_problems(repo):
            name = Path(repo).name
            notify.send(f"{name}: {msg}",
                        "Unstage it before committing (git restore --staged <file>)." if sev == "urgent"
                        else "Worth fixing before it reaches the remote.",
                        severity=sev, rule=f"hygiene:{name}:{msg[:40]}",
                        cooldown_seconds=6 * 3600, url=notify.dashboard("system"))
            found.append(f"{name}: {msg}")
    return "; ".join(found) if found else "clean"


# --------------------------------------------------------------------------- dev servers

def dev_servers() -> list[dict]:
    """Local servers a development tool is listening on."""
    import psutil
    out, seen = [], set()
    try:
        conns = psutil.net_connections(kind="tcp")
    except Exception:
        return out
    for c in conns:
        if c.status != psutil.CONN_LISTEN or not c.laddr or not c.pid:
            continue
        port = c.laddr.port
        if port < 1024 or port == int(config.get("daemon.web_port", 7842)) or (c.pid, port) in seen:
            continue
        try:
            p = psutil.Process(c.pid)
            name = (p.name() or "").lower()
            if name not in DEV_PROCS:
                continue
            cmd = " ".join(p.cmdline()[1:4])[:120]
            cwd = p.cwd()
            started = p.create_time()
        except Exception:
            continue
        seen.add((c.pid, port))
        out.append({"pid": c.pid, "port": port, "name": name, "cmd": cmd,
                    "project": Path(cwd).name if cwd else "", "since": started})
    return sorted(out, key=lambda d: d["port"])


def stop_server(pid: int) -> str:
    """End one dev server - only one that is still listening as a dev tool."""
    import psutil
    if not any(s["pid"] == pid for s in dev_servers()):
        raise ValueError("that process is not a running dev server")
    if pid == os.getpid():
        raise ValueError("refusing to stop ARGUS itself")
    p = psutil.Process(pid)
    p.terminate()
    try:
        p.wait(timeout=5)
    except psutil.TimeoutExpired:
        p.kill()
    return f"stopped {p.name()} (pid {pid})"
