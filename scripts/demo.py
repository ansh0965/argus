"""Run the ARGUS dashboard on made-up sample data.

    python scripts/demo.py [port]

For trying the dashboard, working on its UI, and taking screenshots without
showing anyone your real day. It points ARGUS_HOME at a fresh temp folder, so
your own data/ and config.yaml are never read or written, and it starts only
the web server - no sensors, no scheduler, nothing watching the machine.
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7843
HOME = Path(tempfile.mkdtemp(prefix="argus-demo-"))
os.environ["ARGUS_HOME"] = str(HOME)                 # must precede `import argus`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

(HOME / "config.yaml").write_text(
    "identity: {user_name: Alex}\n"
    f"daemon: {{web_port: {PORT}}}\n"
    "realms: {work_markers: [acme, atlas, billing], personal_markers: [portfolio]}\n"
    "google: {enabled: false}\n"
    "github: {enabled: false}\n",
    encoding="utf-8")

from argus import db, paths  # noqa: E402

assert paths.ROOT == HOME.resolve(), "refusing to seed a real ARGUS home"

from argus.brain import board  # noqa: E402

# (app, exe, window title, category, realm)
WORK = [
    ("VS Code", "code.exe", "export.py - atlas - Visual Studio Code", "code", "work"),
    ("VS Code", "code.exe", "test_export.py - atlas - Visual Studio Code", "code", "work"),
    ("Windows Terminal", "windowsterminal.exe", "pytest - atlas", "terminal", "work"),
    ("Chrome", "chrome.exe", "Pull requests · acme/atlas - Google Chrome", "code", "work"),
    ("Chrome", "chrome.exe", "Tenant export spec - Google Docs - Google Chrome", "docs", "work"),
    ("Slack", "slack.exe", "acme-general - Slack", "comms", "work"),
    ("Chrome", "chrome.exe", "Meet - weekly sync - Google Chrome", "meeting", "work"),
    ("VS Code", "code.exe", "invoice.py - billing-api - Visual Studio Code", "code", "work"),
]
OTHER = [
    ("VS Code", "code.exe", "index.astro - portfolio - Visual Studio Code", "code", "personal"),
    ("Chrome", "chrome.exe", "Watch later - YouTube - Google Chrome", "media", "unknown"),
    ("Chrome", "chrome.exe", "Home / X - Google Chrome", "social", "unknown"),
]


def seed() -> None:
    rnd = random.Random(7)
    db.init()
    now = datetime.now()

    # Three weeks of sessions, so the trend and rhythm charts have something to show.
    for back in range(21, -1, -1):
        day = (now - timedelta(days=back)).replace(hour=9, minute=40, second=0, microsecond=0)
        if day.weekday() >= 5 and back:
            continue
        t = day.timestamp()
        end = min(day.replace(hour=18, minute=20).timestamp(), time.time() - 120)
        while t < end:
            app, exe, title, cat, realm = rnd.choice(WORK if rnd.random() < 0.82 else OTHER)
            dur = rnd.randint(6, 48) * 60
            stop = min(t + dur, end)
            if stop - t > 60:
                db.add_activity(app, exe, title, cat, realm, t, stop, False, False)
            t = stop + rnd.randint(0, 6) * 60

    t = time.time()
    db.log_event("git", "commit", "atlas", "feat(export): stream tenant export as CSV",
                 ts=t - 5400, meta={"sha": "a1b2c3d"})
    db.log_event("git", "commit", "atlas", "test(export): cover empty tenants",
                 ts=t - 3000, meta={"sha": "d4e5f6a"})
    db.log_event("file", "modified", "export.py", "atlas/backend/export.py", ts=t - 900,
                 meta={"dir": "atlas/backend", "ext": ".py"})

    # The board.
    doing = board.add("atlas: tenant export", status="doing", realm="work",
                      notes="CSV first, then the scheduled job")
    board.add("Review: Rate limits for the public API", status="today", realm="work",
              origin="gh-review:acme/atlas#212", source="github",
              notes="acme/atlas#212 by sam",
              meta={"kind": "review", "url": "https://github.com/acme/atlas/pull/212"})
    board.add("write the release notes", status="today", realm="work")
    board.add("call Sam about the launch date", status="today", realm="work",
              due=t + 3 * 3600)
    for title in ("fix the flaky login test", "reply to Priya about the invoice"):
        c = board.add(title, status="today", realm="work")
        board.move(c["id"], "done")
    board.add("billing-api: finish feat/invoices", origin="loop:billing-api", source="auto",
              realm="work", notes="2 files not committed",
              meta={"kind": "loop", "thread": "billing-api",
                    "next": "Commit 2 changed files on feat/invoices (invoice.py)"})
    board.add("Pick back up: portfolio?", origin="drop:portfolio", source="auto",
              realm="personal", notes="quiet for 6 days",
              meta={"kind": "dropped", "thread": "portfolio",
                    "intent": "make the project cards load faster"})
    board.add("book the dentist", realm="personal")
    board.add("renew the domain", realm="personal", due=t + 2 * 86400)
    parked = board.add("migrate CI to the new runners", realm="work")
    board.park(parked["id"], "waiting for the infra team to open access")
    board.commit_plan([doing["id"]])

    db.add_digest("morning", "Morning brief", "\n".join([
        "**The one thing** - finish the tenant export on atlas; the CSV path is "
        "written, the scheduled job is not.",
        "",
        "## Yesterday",
        "- 5h 40m of focused work, 4h 10m of it on atlas",
        "- Shipped: flaky login test fixed, 3 commits pushed",
        "",
        "## Today",
        "- 11:00 weekly sync (30 min)",
        "- Sam is waiting on your review of #212",
        "",
        "## Loose ends",
        "- billing-api has 2 uncommitted files on `feat/invoices`",
    ]))
    db.add_alert("repo_janitor", "billing-api has uncommitted work",
                 "2 files changed on feat/invoices, untouched for 26h", "info")
    db.add_run("morning_brief", True, 8400, "brief written")
    db.add_run("repo_janitor", True, 120, "1 repo with stale changes")


def main() -> None:
    seed()

    # Keep the demo to its own made-up world: no scanning this machine's
    # repositories, Google accounts or Claude Code transcripts.
    from argus.brain import threads
    from argus.gsuite import auth
    from argus.sensors import git
    git.all_states = lambda max_age=300: []
    auth.labels = lambda: []
    auth.is_configured = lambda: False
    threads.collect_claude = lambda since: []

    from argus.web import server
    print(f"  ARGUS demo on http://127.0.0.1:{PORT}   (sample data in {HOME})")
    print("  Ctrl+C to stop.")
    server.serve()


if __name__ == "__main__":
    main()
