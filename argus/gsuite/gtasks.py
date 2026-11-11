"""Google Tasks, mirrored into the local tasks table and pushed back on change.

Local-first: `argus task add` works with no network and syncs when Google is
reachable, so the task list is never blocked on connectivity.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .. import db
from ..util import log
from . import auth

lg = log("google.tasks")


def _svc(label: str, interactive: bool = False):
    return auth.service("tasks", "v1", label, interactive)


def push_account() -> str:
    """Which account receives tasks you create locally.

    Defaults to the first connected label so a single-account setup needs no
    configuration; override with google.tasks_account in config.yaml.
    """
    from .. import config
    want = config.get("google.tasks_account", "") or ""
    labels = auth.labels()
    if want and want in labels:
        return want
    return labels[0] if labels else ""


def _default_list(svc) -> str | None:
    try:
        items = svc.tasklists().list(maxResults=10).execute().get("items", [])
        return items[0]["id"] if items else None
    except Exception:
        return None


def add_local(title: str, due: float | None = None, notes: str = "",
              source: str = "argus") -> int:
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO tasks(created,due,title,notes,source) VALUES(?,?,?,?,?)",
            (time.time(), due, title[:400], notes[:2000], source))
        return int(cur.lastrowid)


def complete_local(task_id: int) -> bool:
    """Done here now; Google hears about it on the next sync (it may be offline)."""
    with db.tx() as c:
        return c.execute("UPDATE tasks SET done=1, done_ts=?, pushed_done=0 "
                         "WHERE id=? AND done=0", (time.time(), task_id)).rowcount > 0


def open_tasks(limit: int = 50) -> list[dict]:
    return db.rows_to_dicts(db.q(
        "SELECT * FROM tasks WHERE done=0 ORDER BY "
        "CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, created LIMIT ?", (limit,)))


def overdue() -> list[dict]:
    return db.rows_to_dicts(db.q(
        "SELECT * FROM tasks WHERE done=0 AND due IS NOT NULL AND due < ? ORDER BY due",
        (time.time(),)))


def sync(label: str) -> str:
    """Two-way: pull Google's tasks in, push locally created ones up."""
    svc = _svc(label)
    if not svc:
        return "not connected"
    lst = _default_list(svc)
    if not lst:
        return "no task list"

    pulled = pushed = completed = 0

    # Completions first. `argus task done` on a Google task used to be undone
    # within five minutes: the pull below overwrote done=1 with Google's
    # "needsAction", because nothing ever told Google.
    for r in db.q("SELECT id, ext_id FROM tasks WHERE account=? AND ext_id IS NOT NULL "
                  "AND done=1 AND COALESCE(pushed_done,0)=0", (label,)):
        try:
            svc.tasks().patch(tasklist=lst, task=r["ext_id"],
                              body={"status": "completed"}).execute()
            with db.tx() as c:
                c.execute("UPDATE tasks SET pushed_done=1 WHERE id=?", (r["id"],))
            completed += 1
        except Exception as e:
            lg.warning("could not mark task %s done upstream: %s", r["id"], str(e)[:120])

    try:
        items = svc.tasks().list(tasklist=lst, showCompleted=True,
                                 showHidden=False, maxResults=100).execute().get("items", [])
    except Exception as e:
        return f"pull failed: {str(e)[:150]}"

    for t in items:
        due = None
        if t.get("due"):
            try:
                due = datetime.fromisoformat(t["due"].replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        done = 1 if t.get("status") == "completed" else 0
        existing = db.q1("SELECT id, done, pushed_done FROM tasks WHERE ext_id=?", (t["id"],))
        with db.tx() as c:
            if existing:
                if existing["done"] and not existing["pushed_done"] and not done:
                    done = 1             # our completion has not reached Google yet
                c.execute("UPDATE tasks SET title=?, due=?, done=?, notes=?, "
                          "pushed_done=?, done_ts=CASE WHEN ?=1 THEN COALESCE(done_ts,?) "
                          "ELSE NULL END WHERE ext_id=?",
                          (t.get("title", "")[:400], due, done,
                           (t.get("notes") or "")[:2000],
                           1 if t.get("status") == "completed" else 0,
                           done, time.time(), t["id"]))
            else:
                c.execute("INSERT INTO tasks(created,due,title,notes,source,ext_id,done,"
                          "done_ts,account,pushed_done) VALUES(?,?,?,?,'gtasks',?,?,?,?,?)",
                          (time.time(), due, t.get("title", "")[:400],
                           (t.get("notes") or "")[:2000], t["id"], done,
                           time.time() if done else None, label, done))
                pulled += 1

    # Push locally created tasks to exactly one account. Without this the task
    # you added lands in whichever account happened to sync first (alphabetical),
    # so `argus task add` would scatter your list across four Google accounts.
    if label != push_account():
        return f"pulled {pulled}, completed {completed}, pushed 0 (not the push account)"

    for r in db.q("SELECT * FROM tasks WHERE ext_id IS NULL AND done=0 LIMIT 40"):
        body = {"title": r["title"]}
        if r["notes"]:
            body["notes"] = r["notes"]
        if r["due"]:
            body["due"] = datetime.fromtimestamp(r["due"], timezone.utc).isoformat()
        try:
            created = svc.tasks().insert(tasklist=lst, body=body).execute()
            with db.tx() as c:
                c.execute("UPDATE tasks SET ext_id=?, account=? WHERE id=?",
                          (created["id"], label, r["id"]))
            pushed += 1
        except Exception:
            continue

    return f"pulled {pulled}, completed {completed}, pushed {pushed}"
