"""SQLite event store. Append-only events + derived tables. WAL, thread-safe."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterable

from .paths import DB_PATH

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL NOT NULL,
  day     TEXT NOT NULL,
  source  TEXT NOT NULL,
  kind    TEXT NOT NULL,
  subject TEXT,
  detail  TEXT,
  value   REAL,
  meta    TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_day_src ON events(day, source);
CREATE INDEX IF NOT EXISTS ix_events_subject ON events(subject);

-- Coalesced foreground-window sessions: the backbone of activity analytics.
CREATE TABLE IF NOT EXISTS activity (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  day       TEXT NOT NULL,
  app       TEXT NOT NULL,
  exe       TEXT,
  title     TEXT,
  category  TEXT,
  realm     TEXT,
  started   REAL NOT NULL,
  ended     REAL NOT NULL,
  seconds   REAL NOT NULL,
  afk       INTEGER DEFAULT 0,
  private   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_activity_day     ON activity(day);
CREATE INDEX IF NOT EXISTS ix_activity_started ON activity(started);
CREATE INDEX IF NOT EXISTS ix_activity_app     ON activity(app);

CREATE TABLE IF NOT EXISTS metrics (
  ts        REAL NOT NULL,
  cpu       REAL, mem REAL, disk_free_gb REAL,
  battery   REAL, plugged INTEGER,
  net_up    REAL, net_down REAL,
  procs     INTEGER, temp REAL
);
CREATE INDEX IF NOT EXISTS ix_metrics_ts ON metrics(ts);

CREATE TABLE IF NOT EXISTS alerts (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  day      TEXT NOT NULL,
  rule     TEXT NOT NULL,
  severity TEXT NOT NULL,
  title    TEXT NOT NULL,
  body     TEXT,
  meta     TEXT,
  acked    INTEGER DEFAULT 0,
  notified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS digests (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL NOT NULL,
  day     TEXT NOT NULL,
  kind    TEXT NOT NULL,
  title   TEXT,
  body    TEXT,
  meta    TEXT
);
CREATE INDEX IF NOT EXISTS ix_digests_day ON digests(day, kind);

-- Things ARGUS learns and should remember across restarts.
CREATE TABLE IF NOT EXISTS memory (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  category TEXT NOT NULL,
  key      TEXT,
  text     TEXT NOT NULL,
  weight   REAL DEFAULT 1.0,
  expires  REAL
);
CREATE INDEX IF NOT EXISTS ix_memory_cat ON memory(category);

CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  val TEXT,
  ts  REAL
);

-- Google-side mirrors so the dashboard works offline and we can diff for alerts.
CREATE TABLE IF NOT EXISTS gcal (
  id        TEXT PRIMARY KEY,
  cal_id    TEXT,
  summary   TEXT,
  start_ts  REAL, end_ts REAL,
  location  TEXT, hangout TEXT,
  attendees TEXT, status TEXT,
  updated   REAL,
  notified  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_gcal_start ON gcal(start_ts);

CREATE TABLE IF NOT EXISTS gmail (
  id        TEXT PRIMARY KEY,
  thread_id TEXT,
  ts        REAL,
  sender    TEXT, sender_email TEXT,
  subject   TEXT, snippet TEXT,
  labels    TEXT,
  unread    INTEGER,
  triage    TEXT,
  reason    TEXT,
  seen      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_gmail_ts ON gmail(ts);

CREATE TABLE IF NOT EXISTS tasks (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  created  REAL NOT NULL,
  due      REAL,
  title    TEXT NOT NULL,
  notes    TEXT,
  source   TEXT,
  ext_id   TEXT,
  done     INTEGER DEFAULT 0,
  done_ts  REAL
);

-- Autonomous run log: every automation execution, for auditability.
CREATE TABLE IF NOT EXISTS runs (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  name     TEXT NOT NULL,
  ok       INTEGER,
  ms       INTEGER,
  summary  TEXT,
  error    TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_ts ON runs(ts);

-- Work threads: what you were doing, assembled from evidence. DERIVED STATE ONLY.
-- rebuild() is DELETE + re-derive, so losing these two tables loses nothing.
CREATE TABLE IF NOT EXISTS threads (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  key           TEXT NOT NULL,
  seq           INTEGER NOT NULL DEFAULT 1,
  uid           TEXT NOT NULL UNIQUE,
  title         TEXT,
  title_user    TEXT,
  realm         TEXT,
  first_ts      REAL NOT NULL,
  last_ts       REAL NOT NULL,
  seconds_total REAL DEFAULT 0,
  touch_count   INTEGER DEFAULT 0,
  source_count  INTEGER DEFAULT 0,
  status        TEXT,
  status_reason TEXT,
  confidence    REAL DEFAULT 0,
  confidence_parts TEXT,
  score         REAL DEFAULT 0,
  open_loop     INTEGER DEFAULT 0,
  meta          TEXT,
  updated       REAL
);
CREATE INDEX IF NOT EXISTS ix_threads_key    ON threads(key);
CREATE INDEX IF NOT EXISTS ix_threads_score  ON threads(score);
CREATE INDEX IF NOT EXISTS ix_threads_last   ON threads(last_ts);
CREATE INDEX IF NOT EXISTS ix_threads_status ON threads(status);

-- Provenance. ref_table/ref_id make every claim traceable to a real row, which
-- is what turns "the engine produced garbage" from a mystery into a query.
CREATE TABLE IF NOT EXISTS thread_touches (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL,
  ts        REAL NOT NULL,
  source    TEXT NOT NULL,
  ref_table TEXT NOT NULL DEFAULT '',
  ref_id    INTEGER NOT NULL DEFAULT 0,
  evidence  TEXT,
  seconds   REAL DEFAULT 0,
  weight    REAL DEFAULT 1.0,
  resolver  TEXT
);
CREATE INDEX IF NOT EXISTS ix_touches_thread ON thread_touches(thread_id, ts);

-- The one table here holding unrecoverable data: it survives every rollback.
-- Accuracy can be computed later; the signal cannot be reconstructed later.
CREATE TABLE IF NOT EXISTS feedback (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     REAL NOT NULL,
  kind   TEXT NOT NULL,
  target TEXT NOT NULL,
  signal TEXT NOT NULL,
  detail TEXT,
  meta   TEXT
);
CREATE INDEX IF NOT EXISTS ix_feedback_target ON feedback(kind, target);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=15000")
        _local.conn = c
    return c


def _migrate(c: sqlite3.Connection) -> None:
    """Additive migrations. Safe to run on every start.

    Only ever adds columns or indexes - an existing database is never rewritten,
    so a downgrade keeps working against the same file.
    """
    wanted = {
        "gmail": [("account", "TEXT DEFAULT ''")],
        "gcal": [("account", "TEXT DEFAULT ''"),
                 # all-day events start at local midnight; without this flag the
                 # sentinel announced "Holiday in 10 min" at 23:50 the night before
                 ("all_day", "INTEGER DEFAULT 0"),
                 # your own RSVP: a meeting you declined is not a meeting
                 ("self_status", "TEXT DEFAULT ''")],
        "tasks": [("account", "TEXT DEFAULT ''"),
                  # 1 once Google knows this task is done. A local completion
                  # waits at 0 until pushed, so a pull cannot undo it.
                  ("pushed_done", "INTEGER DEFAULT 0"),
                  # the board (brain/board.py): column, where an automatic card
                  # came from, ordering, and the note left when parking it
                  ("status", "TEXT DEFAULT 'inbox'"),
                  ("origin", "TEXT DEFAULT ''"),
                  ("realm", "TEXT DEFAULT ''"),
                  ("meta", "TEXT"),
                  ("updated", "REAL"),
                  ("sort", "REAL"),
                  ("pinned", "INTEGER DEFAULT 0"),
                  ("dismissed", "INTEGER DEFAULT 0"),
                  ("park_note", "TEXT"),
                  ("parked_ts", "REAL"),
                  ("started", "REAL"),
                  # a reminder at a clock time ("call Sam at 5pm")
                  ("remind_at", "REAL"),
                  ("reminded", "INTEGER DEFAULT 0")],
    }
    for table, cols in wanted.items():
        try:
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not have:
            continue
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    c.execute("CREATE INDEX IF NOT EXISTS ix_gmail_account ON gmail(account)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_gcal_account ON gcal(account)")
    # notify's cooldown asks "when did this rule last fire" on every send.
    c.execute("CREATE INDEX IF NOT EXISTS ix_alerts_rule ON alerts(rule, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_tasks_origin ON tasks(origin)")


def init() -> None:
    c = conn()
    with _write_lock:
        c.executescript(SCHEMA)
        _migrate(c)
        c.commit()


@contextmanager
def tx():
    c = conn()
    with _write_lock:
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def today(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- writes

def log_event(source: str, kind: str, subject: str = "", detail: str = "",
              value: float | None = None, meta: dict | None = None,
              ts: float | None = None) -> None:
    ts = ts or time.time()
    with tx() as c:
        c.execute(
            "INSERT INTO events(ts,day,source,kind,subject,detail,value,meta) VALUES(?,?,?,?,?,?,?,?)",
            (ts, today(ts), source, kind, (subject or "")[:400], (detail or "")[:2000], value,
             json.dumps(meta, default=str) if meta else None),
        )


def log_events(rows: Iterable[tuple]) -> None:
    """Bulk insert of (source, kind, subject, detail, value, meta, ts) tuples."""
    payload = []
    for source, kind, subject, detail, value, meta, ts in rows:
        ts = ts or time.time()
        payload.append((ts, today(ts), source, kind, (subject or "")[:400],
                        (detail or "")[:2000], value,
                        json.dumps(meta, default=str) if meta else None))
    if not payload:
        return
    with tx() as c:
        c.executemany(
            "INSERT INTO events(ts,day,source,kind,subject,detail,value,meta) VALUES(?,?,?,?,?,?,?,?)",
            payload,
        )


def add_activity(app: str, exe: str, title: str, category: str, realm: str,
                 started: float, ended: float, afk: bool, private: bool) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO activity(day,app,exe,title,category,realm,started,ended,seconds,afk,private)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (today(started), app, exe, (title or "")[:500], category, realm, started, ended,
             max(0.0, ended - started), int(afk), int(private)),
        )
        return int(cur.lastrowid)


def extend_activity(row_id: int, ended: float) -> None:
    """Move the end of a session that is still in progress."""
    with tx() as c:
        c.execute("UPDATE activity SET ended=?, seconds=MAX(0, ? - started) WHERE id=?",
                  (ended, ended, row_id))


def add_metric(**kw: Any) -> None:
    cols = ["ts", "cpu", "mem", "disk_free_gb", "battery", "plugged",
            "net_up", "net_down", "procs", "temp"]
    vals = [kw.get(k) for k in cols]
    vals[0] = kw.get("ts") or time.time()
    with tx() as c:
        c.execute(f"INSERT INTO metrics({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)


def add_alert(rule: str, title: str, body: str = "", severity: str = "info",
              meta: dict | None = None) -> int:
    ts = time.time()
    with tx() as c:
        cur = c.execute(
            "INSERT INTO alerts(ts,day,rule,severity,title,body,meta) VALUES(?,?,?,?,?,?,?)",
            (ts, today(ts), rule, severity, title, body,
             json.dumps(meta, default=str) if meta else None),
        )
        return int(cur.lastrowid)


def add_digest(kind: str, title: str, body: str, meta: dict | None = None) -> int:
    ts = time.time()
    with tx() as c:
        cur = c.execute(
            "INSERT INTO digests(ts,day,kind,title,body,meta) VALUES(?,?,?,?,?,?)",
            (ts, today(ts), kind, title, body, json.dumps(meta, default=str) if meta else None),
        )
        return int(cur.lastrowid)


def add_run(name: str, ok: bool, ms: int, summary: str = "", error: str = "") -> None:
    with tx() as c:
        c.execute("INSERT INTO runs(ts,name,ok,ms,summary,error) VALUES(?,?,?,?,?,?)",
                  (time.time(), name, int(ok), ms, (summary or "")[:4000], (error or "")[:2000]))


def remember(category: str, text: str, key: str | None = None,
             weight: float = 1.0, ttl_days: float | None = None) -> None:
    exp = time.time() + ttl_days * 86400 if ttl_days else None
    with tx() as c:
        if key:
            c.execute("DELETE FROM memory WHERE category=? AND key=?", (category, key))
        c.execute("INSERT INTO memory(ts,category,key,text,weight,expires) VALUES(?,?,?,?,?,?)",
                  (time.time(), category, key, text, weight, exp))


def recall(category: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    sql = "SELECT * FROM memory WHERE (expires IS NULL OR expires > ?)"
    args: list[Any] = [time.time()]
    if category:
        sql += " AND category=?"
        args.append(category)
    sql += " ORDER BY weight DESC, ts DESC LIMIT ?"
    args.append(limit)
    return list(conn().execute(sql, args))


# --------------------------------------------------------------------------- kv state

def set_state(key: str, val: Any) -> None:
    with tx() as c:
        c.execute("INSERT INTO state(key,val,ts) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET val=excluded.val, ts=excluded.ts",
                  (key, json.dumps(val, default=str), time.time()))


def get_state(key: str, default: Any = None) -> Any:
    r = conn().execute("SELECT val FROM state WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["val"])
    except Exception:
        return default


# --------------------------------------------------------------------------- reads

def q(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(conn().execute(sql, tuple(args)))


def q1(sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn().execute(sql, tuple(args)).fetchone()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- maintenance

def prune(events_days: int, metrics_days: int, runs_days: int = 30,
          alerts_days: int = 90, mail_days: int = 60,
          events_by_source: dict[str, int] | None = None) -> dict[str, int]:
    now = time.time()
    out: dict[str, int] = {}
    with tx() as c:
        for source, days in (events_by_source or {}).items():
            try:
                days = int(days)
            except (TypeError, ValueError):
                continue
            out[f"events:{source}"] = c.execute(
                "DELETE FROM events WHERE source=? AND ts < ?",
                (source, now - days * 86400)).rowcount
        # The run log gained ~2,400 rows a day and was never pruned at all.
        out["runs"] = c.execute("DELETE FROM runs WHERE ts < ?",
                                (now - runs_days * 86400,)).rowcount
        out["alerts"] = c.execute("DELETE FROM alerts WHERE ts < ?",
                                  (now - alerts_days * 86400,)).rowcount
        # Subjects and snippets of old mail are someone else's words; keep them
        # only as long as they are useful for triage and briefings.
        out["gmail"] = c.execute("DELETE FROM gmail WHERE ts < ? AND unread=0",
                                 (now - mail_days * 86400,)).rowcount
        cutoff = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        out["state"] = c.execute("DELETE FROM state WHERE key LIKE 'llm_calls:%' "
                                 "AND substr(key, 11) < ?", (cutoff,)).rowcount
        out["events"] = c.execute("DELETE FROM events WHERE ts < ?",
                                  (now - events_days * 86400,)).rowcount
        out["metrics"] = c.execute("DELETE FROM metrics WHERE ts < ?",
                                   (now - metrics_days * 86400,)).rowcount
        out["activity"] = c.execute("DELETE FROM activity WHERE started < ?",
                                    (now - events_days * 86400,)).rowcount
        # Provenance must not outlive what it points at, or `resume --why`
        # starts citing rows that have been pruned away.
        out["thread_touches"] = c.execute("DELETE FROM thread_touches WHERE ts < ?",
                                          (now - events_days * 86400,)).rowcount
    return out


def backup(dest) -> int:
    """Write a consistent copy of the database to `dest`; returns its size.

    Uses SQLite's online backup API, so the copy is a single point in time
    even while the sensors keep writing - copying the file (plus its WAL)
    by hand could capture a torn state.
    """
    from pathlib import Path
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    target = sqlite3.connect(str(tmp))
    try:
        conn().backup(target)
    finally:
        target.close()
    tmp.replace(dest)
    return dest.stat().st_size


def vacuum() -> None:
    conn().execute("VACUUM")


def checkpoint() -> None:
    """Fold the WAL back into the main file and truncate it (it grows to tens
    of MB between automatic checkpoints on a busy day)."""
    try:
        conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn().execute("PRAGMA optimize")
    except sqlite3.Error:
        pass


def db_size_mb() -> float:
    try:
        return round(DB_PATH.stat().st_size / 1e6, 2)
    except Exception:
        return 0.0


def day_bounds(day: str) -> tuple[float, float]:
    d = datetime.strptime(day, "%Y-%m-%d")
    return d.timestamp(), (d + timedelta(days=1)).timestamp()
