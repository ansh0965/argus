"""Git sensor: discovers repos once, then tracks commits, branch moves and drift.

Feeds three things: the activity timeline, the standup writer, and the repo
janitor automation that nags about work sitting uncommitted.
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import config, db, pause, privacy
from ..util import expand, log, run

lg = log("sensor.git")

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "AppData",
             "anaconda3", "eclipse", "dist", "build", ".next", "site-packages",
             "$RECYCLE.BIN", "VirtualBox VMs", "OneDrive"}


def discover(force: bool = False) -> list[str]:
    """Find git repos under the configured roots. Cached in state for 12 hours."""
    cached = db.get_state("git_repos", None)
    age = time.time() - float(db.get_state("git_repos_ts", 0) or 0)
    if cached and not force and age < 12 * 3600:
        return [r for r in cached if not privacy.private_project_in(r)]

    depth = int(config.get("git_repos.scan_depth", 2))
    repos: set[str] = set()

    def walk(base: Path, level: int) -> None:
        if level > depth:
            return
        try:
            entries = list(base.iterdir())
        except Exception:
            return
        if any(e.name == ".git" for e in entries):
            if not privacy.private_project_in(str(base)):    # private projects: never read
                repos.add(str(base))
            return
        for e in entries:
            if e.is_dir() and e.name not in SKIP_DIRS and not e.name.startswith("."):
                walk(e, level + 1)

    for root in config.get("git_repos.scan_roots", []):
        p = expand(root)
        if p.is_dir():
            walk(p, 0)

    out = sorted(repos)
    db.set_state("git_repos", out)
    db.set_state("git_repos_ts", time.time())
    lg.info("discovered %d repos", len(out))
    return out


def repo_state(path: str) -> dict | None:
    rc, branch, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path, timeout=20)
    if rc != 0:
        return None
    rc, head, _ = run(["git", "rev-parse", "HEAD"], cwd=path, timeout=20)
    _, status, _ = run(["git", "status", "--porcelain"], cwd=path, timeout=30)
    dirty = [l for l in status.splitlines() if l.strip()]
    _, last, _ = run(["git", "log", "-1", "--format=%ct|%s|%an"], cwd=path, timeout=20)
    ct, _, rest = last.partition("|")
    subject, _, author = rest.partition("|")
    _, ahead, _ = run(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=path, timeout=20)
    _, behind, _ = run(["git", "rev-list", "--count", "HEAD..@{u}"], cwd=path, timeout=20)
    return {
        "path": path,
        "name": Path(path).name,
        "branch": branch,
        "head": head[:12],
        "dirty": len(dirty),
        "dirty_files": [l[3:] for l in dirty[:12]],
        "last_commit_ts": float(ct) if ct.isdigit() else 0.0,
        "last_commit": subject[:200],
        "last_author": author[:80],
        "ahead": int(ahead) if ahead.isdigit() else 0,
        "behind": int(behind) if behind.isdigit() else 0,
    }


def commits_since(path: str, since_ts: float, author: str = "") -> list[dict]:
    args = ["git", "log", f"--since=@{int(since_ts)}", "--format=%ct%x1f%h%x1f%an%x1f%s", "--no-merges"]
    if author:
        args += [f"--author={author}"]
    rc, out, _ = run(args, cwd=path, timeout=40)
    if rc != 0 or not out:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            rows.append({"ts": float(parts[0]), "sha": parts[1],
                         "author": parts[2], "subject": parts[3][:200]})
    return rows


def all_states(max_age: float = 300) -> list[dict]:
    """State of every repo, reusing a scan younger than `max_age` seconds.

    A full scan is ~6 git commands per repo - about 7.5s for 24 repos - and it
    ran on every `argus ask`, every briefing and every load of the dashboard's
    Work tab, while the sensor loop computed exactly the same thing every ten
    minutes and threw it away. Pass max_age=0 to force a fresh scan.
    """
    if max_age > 0:
        cached = db.get_state("git_states", None) or {}
        if cached.get("states") is not None and time.time() - cached.get("ts", 0) < max_age:
            return cached["states"]
    out = []
    for r in discover():
        s = repo_state(r)
        if s:
            out.append(s)
    _remember_states(out)
    return out


def _remember_states(states: list[dict]) -> None:
    db.set_state("git_states", {"ts": time.time(), "states": states})


def collect_once() -> int:
    """Emit events for commits we have not seen and for branch changes."""
    seen: dict = db.get_state("git_seen", {}) or {}
    since_default = time.time() - 3 * 86400
    n = 0
    rows = []
    states = []
    paused = pause.intervals()

    for path in discover():
        st = repo_state(path)
        if not st:
            continue
        states.append(st)
        key = st["name"]
        prev = seen.get(path, {})

        last_seen_ts = float(prev.get("last_commit_ts", since_default))
        for c in commits_since(path, last_seen_ts):
            if c["ts"] <= last_seen_ts or pause.was_paused_at(c["ts"], paused):
                continue
            rows.append(("git", "commit", key, c["subject"], None,
                         {"sha": c["sha"], "author": c["author"], "path": path,
                          "branch": st["branch"]}, c["ts"]))
            n += 1

        if prev.get("branch") and prev["branch"] != st["branch"]:
            rows.append(("git", "branch", key, f"{prev['branch']} -> {st['branch']}",
                         None, {"path": path}, None))

        seen[path] = {"last_commit_ts": st["last_commit_ts"], "branch": st["branch"],
                      "dirty": st["dirty"], "checked": time.time()}

    db.log_events(rows)
    db.set_state("git_seen", seen)
    _remember_states(states)             # the scan everyone else reuses
    return n


def run_loop(stop) -> None:
    while not stop.is_set():
        poll = float(config.get("sensors.git.poll_seconds", 600))
        try:
            n = collect_once()
            if n:
                lg.info("recorded %d commits", n)
        except Exception:
            lg.exception("git collect failed")
        stop.wait(poll)
    lg.info("git sensor stopped")
