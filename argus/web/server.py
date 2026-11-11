"""Local dashboard. Binds to 127.0.0.1 only - nothing is exposed to the network."""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import classify, config, db
from ..actions import notify, tray, voice
from ..brain import digest, insights, llm
from ..gsuite import auth as gauth
from ..gsuite import gcal, gdrive, gmail, gtasks
from ..paths import JOURNAL, SHOTS, asset
from ..sensors import browser as browsersensor
from ..sensors import files as filesensor
from ..sensors import git as gitsensor
from ..sensors import process as procsensor
from ..util import human_ago, human_dur, log

lg = log("web")
STATIC = asset("web", "static")

app = FastAPI(title="ARGUS", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- guard
#
# Binding to 127.0.0.1 keeps other machines out, but not other *websites*. Any
# page open in your browser can aim requests at localhost:
#
#   - DNS rebinding: evil.example re-resolves to 127.0.0.1, after which the
#     browser treats this API as same-origin with the attacker and hands them
#     every response - activity, mail subjects, browsing history.
#   - CSRF: a plain cross-site <form> POST needs no CORS permission to *send*,
#     so it could pause monitoring, burn the LLM budget or complete tasks.
#
# So the Host header must name this machine, and anything that changes state
# must come from the dashboard itself (or from a non-browser client like the
# CLI, which sends no Origin at all).

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _port() -> int:
    return int(config.get("daemon.web_port", 7842))


def _host_ok(host: str) -> bool:
    name, _, port = (host or "").lower().rpartition(":")
    if not name:                      # no port given
        name, port = port, ""
    return name in _LOCAL_HOSTS and port in ("", str(_port()))


def _origin_ok(origin: str) -> bool:
    return origin in {f"http://{h}:{_port()}" for h in _LOCAL_HOSTS}


def request_allowed(method: str, headers: dict) -> tuple[bool, str]:
    """Pure policy, kept separate from the middleware so it can be tested."""
    h = {k.lower(): v for k, v in headers.items()}
    if not _host_ok(h.get("host", "")):
        return False, "host not allowed"
    if method.upper() in _UNSAFE:
        origin = h.get("origin")
        if origin is not None:
            if not _origin_ok(origin):
                return False, "cross-origin request refused"
        elif h.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return False, "cross-site request refused"
    return True, ""


@app.middleware("http")
async def _local_only(request, call_next):
    ok, why = request_allowed(request.method, dict(request.headers))
    if not ok:
        lg.warning("refused %s %s: %s (host=%r origin=%r)", request.method,
                   request.url.path, why, request.headers.get("host"),
                   request.headers.get("origin"))
        return JSONResponse({"error": why}, status_code=403)
    resp = await call_next(request)
    # The dashboard has a "Delete everything" button; it must not be framable.
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    else:
        # The page itself was sent with no caching headers, so a browser could
        # keep showing the previous build's dashboard after an upgrade (seen
        # live: the new pause picker was served but not displayed). Revalidate.
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


@app.exception_handler(Exception)
async def _log_unhandled(request, exc: Exception):
    # uvicorn runs with log_config=None (its own formatter crashes in the
    # windowed build), so a route that raised used to return a bare 500 and
    # leave no trace anywhere. Record it where `argus logs` will show it.
    lg.error("%s %s failed: %s", request.method, request.url.path, exc, exc_info=exc)
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


# --------------------------------------------------------------------------- helpers

def _day_arg(day: str | None) -> str:
    return day or insights.current_day()


def _err(e: Exception) -> JSONResponse:
    return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


# --------------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    f = STATIC / "index.html"
    if f.is_file():
        return f.read_text(encoding="utf-8")
    return "<h1>ARGUS</h1><p>Dashboard assets missing.</p>"


@app.get("/api/status")
def api_status() -> dict:
    from .. import daemon
    try:
        s = daemon.status()
    except Exception:
        s = {"running": False}
    s["paused"] = tray.is_paused()
    s["paused_until"] = float(db.get_state("paused_until", 0) or 0)
    s["brain"] = {"providers": llm.available(), "calls_today": llm.calls_today(),
                  "budget": config.get("brain.max_daily_calls", 200)}
    s["identity"] = config.get("identity", {})
    s["now"] = time.time()
    return s


@app.get("/api/today")
def api_today(day: str | None = None) -> dict:
    d = _day_arg(day)
    from ..brain import timesheet
    with insights.memo():            # the timesheet row reads the same sessions
        try:
            summary = insights.day_summary(d)
        except Exception as e:
            return {"error": str(e), "day": d}
        row = timesheet.day_row(d)
    summary["streaks"] = insights.streaks()
    summary["workday"] = {"first": row["first"], "last": row["last"],
                          "work_seconds": row["work_seconds"],
                          "active_seconds": row["active_seconds"]}
    summary["peak_hours"] = insights.peak_hours()
    return summary


@app.get("/api/hourly")
def api_hourly(days: int = 14) -> list[dict]:
    return insights.hourly_profile(days)


@app.get("/api/trend")
def api_trend(days: int = 21) -> list[dict]:
    since = time.time() - days * 86400
    wd, off = insights.workday_sql()
    rows = db.q(
        f"SELECT {wd} day, "
        " SUM(seconds) total, "
        " SUM(CASE WHEN category IN (" + ",".join("?" * len(classify.PRODUCTIVE)) + ")"
        "     THEN seconds ELSE 0 END) productive, "
        " SUM(CASE WHEN category IN (" + ",".join("?" * len(classify.DISTRACTING)) + ")"
        "     THEN seconds ELSE 0 END) distracting "
        "FROM activity WHERE started > ? AND afk=0 GROUP BY 1 ORDER BY 1",
        (off, *sorted(classify.PRODUCTIVE), *sorted(classify.DISTRACTING), since))
    return [{"day": r["day"], "total": r["total"] or 0,
             "productive": r["productive"] or 0,
             "distracting": r["distracting"] or 0} for r in rows]


@app.get("/api/events")
def api_events(limit: int = 80, source: str | None = None) -> list[dict]:
    sql = "SELECT * FROM events"
    args: list[Any] = []
    if source:
        sql += " WHERE source=?"
        args.append(source)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(min(limit, 500))
    rows = db.rows_to_dicts(db.q(sql, args))
    for r in rows:
        r["ago"] = human_ago(r["ts"])
    return rows


@app.get("/api/now")
def api_now() -> dict:
    """What is happening this second - drives the live header."""
    from ..sensors import window
    # The sensor runs in this process when the daemon serves the dashboard, so
    # read the session in progress rather than the newest *finished* row -
    # which is the window you were in before this one.
    cur = window.current()
    if not cur or time.time() - cur.get("seen", 0) > 15:
        r = db.q1("SELECT * FROM activity ORDER BY started DESC LIMIT 1")
        cur = dict(r) if r else {}
        if cur:
            cur["ongoing_seconds"] = time.time() - cur["started"]
            cur["live"] = False
    idle_seconds = window.idle_seconds
    m = db.q1("SELECT * FROM metrics ORDER BY ts DESC LIMIT 1")
    s, e, _ = insights.day_range()
    return {
        "current": cur,
        "idle_seconds": idle_seconds(),
        "metrics": dict(m) if m else {},
        "today": insights.totals(s, e),
        "meeting_now": gcal.in_meeting_now() if gauth.is_connected() else None,
        "next_event": gcal.next_event() if gauth.is_connected() else None,
        "calendar_synced": digest.last_synced("calendar") if gauth.is_connected() else None,
    }


@app.get("/api/alerts")
def api_alerts(limit: int = 40, unacked: bool = False, all: int = 0) -> list[dict]:
    rows = notify.recent(limit, unacked, collapse=not all)
    for r in rows:
        r["ago"] = human_ago(r["ts"])
    return rows


@app.post("/api/alerts/ack")
def api_ack(payload: dict = Body(default={})) -> dict:
    return {"acked": notify.ack(payload.get("id"))}


@app.get("/api/digests")
def api_digests(limit: int = 20, kind: str | None = None) -> list[dict]:
    sql = "SELECT id, ts, day, kind, title FROM digests"
    args: list[Any] = []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    rows = db.rows_to_dicts(db.q(sql, args))
    for r in rows:
        r["ago"] = human_ago(r["ts"])
    return rows


@app.get("/api/digest/{digest_id}")
def api_digest(digest_id: int) -> dict:
    r = db.q1("SELECT * FROM digests WHERE id=?", (digest_id,))
    if not r:
        raise HTTPException(404, "not found")
    return dict(r)


@app.post("/api/digest/run/{kind}")
def api_run_digest(kind: str) -> dict:
    fns = {"morning": digest.morning_brief, "evening": digest.evening_wrap,
           "weekly": digest.weekly_retro}
    fn = fns.get(kind)
    if not fn:
        raise HTTPException(400, "kind must be morning, evening or weekly")
    try:
        return fn()
    except Exception as e:
        return {"title": "failed", "body": f"{type(e).__name__}: {e}"}


@app.post("/api/ask")
def api_ask(payload: dict = Body(...)) -> dict:
    q = (payload.get("q") or "").strip()
    if not q:
        raise HTTPException(400, "empty question")
    t0 = time.time()
    from ..brain import board
    if board.is_what_now(q):            # "what should I do now?" - straight from the board
        return {"q": q, "answer": board.what_now(), "seconds": round(time.time() - t0, 1)}
    if board.is_schedule(q):            # "schedule a call with Sam tomorrow 4pm"
        try:
            answer = board.schedule(q)["message"]
        except ValueError as e:
            answer = f"I couldn't schedule that: {e}"
        return {"q": q, "answer": answer, "seconds": round(time.time() - t0, 1)}
    answer = digest.answer(q)
    db.log_event("argus", "asked", "user", q[:300], value=time.time() - t0)
    if payload.get("speak"):
        voice.say(answer)
    return {"q": q, "answer": answer, "seconds": round(time.time() - t0, 1)}


@app.post("/api/speak")
def api_speak(payload: dict = Body(...)) -> dict:
    return {"ok": voice.say(payload.get("text", ""))}


# --------------------------------------------------------------------------- work data

@app.get("/api/threads")
def api_threads(limit: int = 12, rebuild: bool = False) -> dict:
    """Where you left off. Deterministic, so this is cheap and repeatable."""
    from ..brain import threads as th
    if rebuild:
        th.rebuild()
    rows = th.ranked(limit=limit)
    for r in rows:
        r["ago"] = human_ago(r["last_ts"])
        r["human_seconds"] = human_dur(r["seconds_total"])
    return {"threads": rows, "count": len(rows)}


@app.post("/api/threads/rebuild")
def api_threads_rebuild() -> dict:
    from ..brain import threads as th
    return th.rebuild()


@app.get("/api/threads/{key}")
def api_thread(key: str) -> dict:
    """One thread with every piece of evidence behind it - the 'why' panel."""
    from ..brain import threads as th
    ctx = th.context_for(key)
    if not ctx:
        raise HTTPException(404, "no such thread")
    ctx["ago"] = human_ago(ctx["last_ts"])
    ctx["human_seconds"] = human_dur(ctx["seconds_total"])
    for t in ctx["touches"]:
        t["ago"] = human_ago(t["ts"])
    return ctx


@app.post("/api/threads/{key}/feedback")
def api_thread_feedback(key: str, payload: dict = Body(...)) -> dict:
    """resumed | dismissed | wrong. Captured now because it is unrecoverable later."""
    from ..brain import threads as th
    signal = (payload.get("signal") or "").strip()
    if signal not in ("resumed", "dismissed", "wrong"):
        raise HTTPException(400, "signal must be resumed, dismissed or wrong")
    th.record_feedback(key, signal, payload.get("detail", ""), payload.get("meta"))
    return {"ok": True, "key": key, "signal": signal}


@app.get("/api/repos")
def api_repos(fresh: bool = False) -> list[dict]:
    try:
        states = gitsensor.all_states(max_age=0 if fresh else 300)
    except Exception as e:
        return [{"error": str(e)}]
    for s in states:
        s["last_commit_ago"] = human_ago(s["last_commit_ts"]) if s["last_commit_ts"] else "never"
    return sorted(states, key=lambda s: -(s["last_commit_ts"] or 0))


@app.get("/api/timesheet")
def api_timesheet(week: int = 0, realm: str = "work") -> dict:
    """Monday..Sunday of this week (0), last week (-1), ..."""
    from ..brain import timesheet as ts
    if realm not in ("work", "personal", "all"):
        raise HTTPException(400, "realm must be work, personal or all")
    start, end = ts.week_bounds(week)
    table = ts.rows(start, end, realm)
    for r in table:
        r["active_human"] = human_dur(r["active_seconds"])
        r["work_human"] = human_dur(r["work_seconds"])
    return {"start": start, "end": end, "realm": realm, "days": table,
            "summary": ts.summary(table)}


@app.get("/api/timesheet.csv", response_class=PlainTextResponse)
def api_timesheet_csv(week: int = 0, realm: str = "work"):
    from ..brain import timesheet as ts
    if realm not in ("work", "personal", "all"):
        raise HTTPException(400, "realm must be work, personal or all")
    start, end = ts.week_bounds(week)
    return PlainTextResponse(
        ts.to_csv(ts.rows(start, end, realm)), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="timesheet-{start}.csv"'})


@app.get("/api/files")
def api_files(hours: float = 24, limit: int = 40) -> list[dict]:
    rows = filesensor.recent_files(hours, limit)
    for r in rows:
        r["ago"] = human_ago(r["ts"])
    return rows


@app.get("/api/domains")
def api_domains(hours: float = 24) -> list[dict]:
    rows = browsersensor.top_domains(hours, 20)
    for r in rows:
        r["ago"] = human_ago(r["last"])
    return rows


@app.get("/api/processes")
def api_processes() -> list[dict]:
    return procsensor.top_consumers(12)


@app.get("/api/devservers")
def api_devservers() -> list[dict]:
    from .. import devtools
    return devtools.dev_servers()


@app.post("/api/devservers/{pid}/stop")
def api_devserver_stop(pid: int) -> dict:
    from .. import devtools
    try:
        return {"ok": True, "message": devtools.stop_server(pid)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/network")
def api_network() -> dict:
    snap = db.get_state("net_snapshot", {}) or {}
    snap["autostart_count"] = len(db.get_state("autostart_baseline", {}) or {})
    return snap


# --------------------------------------------------------------------------- google

@app.get("/api/google")
def api_google() -> dict:
    """Calendar and mail are merged across accounts, each row tagged with its
    label, because what you want to see is one timeline - not three."""
    accts = gauth.accounts()
    out: dict[str, Any] = {
        "configured": gauth.is_configured(),
        "connected": bool(accts),
        "accounts": accts,
        "help": None if accts else gauth.help_text(),
        "all_features": list(gauth.ALL_FEATURES),
    }
    if not accts:
        return out

    out["synced"] = {k: digest.last_synced(k) for k in ("calendar", "mail")}
    out["calendar"] = [e for e in gcal.upcoming(hours=48)
                       if e.get("self_status") != "declined"]
    for e in out["calendar"]:
        start = datetime.fromtimestamp(e["start_ts"])
        e["when"] = start.strftime("%a") + " all day" if e.get("all_day") \
            else start.strftime("%a %H:%M")
        e["in_minutes"] = round((e["start_ts"] - time.time()) / 60)
    out["mail"] = gmail.important_unread(14)
    for m in out["mail"]:
        m["ago"] = human_ago(m["ts"])
    out["mail_counts"] = gmail.unread_counts()
    out["free_slots"] = gcal.free_slots()

    drive: list[dict] = []
    for a in accts:
        if not a["valid"] or "drive" not in a["features"]:
            continue
        try:
            for f2 in gdrive.recent(a["label"], days=7, limit=8):
                f2["account"] = a["label"]
                f2["ago"] = human_ago(f2["modified"])
                drive.append(f2)
        except Exception:
            continue
    out["drive"] = sorted(drive, key=lambda d: -d["modified"])[:14]
    return out


_google_logins: dict[str, threading.Thread] = {}


@app.post("/api/google/login/{label}")
def api_google_login(label: str) -> dict:
    """Start the consent flow for one account from the dashboard.

    Runs in the daemon: Google's flow opens the browser and waits on a local
    port for the redirect, so this returns at once and the page polls.
    """
    try:
        label = gauth.validate_label(label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if label not in gauth.labels():
        raise HTTPException(404, "no such account - add new ones with `argus google login`")
    t = _google_logins.get(label)
    if t and t.is_alive():
        return {"started": False, "running": True}

    def _run() -> None:
        try:
            gauth.creds(label, interactive=True)
            db.set_state(f"google_email:{label}", "")
            db.set_state(f"google_login_error:{label}", "")
            from .. import daemon
            daemon.google_sync()
        except Exception as e:
            lg.warning("dashboard login for %s failed: %s", label, str(e)[:200])
            db.set_state(f"google_login_error:{label}", str(e)[:300])

    t = threading.Thread(target=_run, name=f"google.login.{label}", daemon=True)
    _google_logins[label] = t
    t.start()
    return {"started": True, "email": gauth.email_of(label)}


@app.post("/api/google/sync")
def api_google_sync() -> dict:
    from .. import daemon
    return {"result": daemon.google_sync()}


@app.post("/api/mail/draft/{message_id}")
def api_draft(message_id: str) -> dict:
    return {"result": gmail.draft_reply(message_id)}


@app.post("/api/mail/read/{message_id}")
def api_mark_read(message_id: str) -> dict:
    return {"ok": gmail.mark_read(message_id)}


# --------------------------------------------------------------------------- tasks

@app.get("/api/tasks")
def api_tasks() -> dict:
    return {"open": gtasks.open_tasks(), "overdue": gtasks.overdue()}


@app.post("/api/tasks")
def api_add_task(payload: dict = Body(...)) -> dict:
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    due = payload.get("due")
    tid = gtasks.add_local(title, float(due) if due else None,
                           payload.get("notes", ""), "user")
    return {"id": tid}


@app.post("/api/tasks/{task_id}/done")
def api_done(task_id: int) -> dict:
    return {"ok": gtasks.complete_local(task_id)}


# --------------------------------------------------------------------------- board

def _card_or_404(fn, *a, **k) -> dict:
    from ..brain import board
    try:
        return fn(*a, **k)
    except KeyError:
        raise HTTPException(404, "no such card")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/settings")
def api_settings() -> dict:
    return {k: config.get(k) for k in config.SETTABLE}


@app.post("/api/settings")
def api_settings_save(payload: dict = Body(...)) -> dict:
    try:
        return config.save_settings(payload)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e).strip("'"))


@app.get("/api/home")
def api_home() -> dict:
    from ..brain import board
    return board.home()


@app.get("/api/board")
def api_board() -> dict:
    from ..brain import board
    return board.board()


@app.post("/api/board/capture")
def api_board_capture(payload: dict = Body(...)) -> dict:
    from ..brain import board
    return _card_or_404(board.capture, payload.get("text") or "")


@app.post("/api/board/{card_id}/move")
def api_board_move(card_id: int, payload: dict = Body(...)) -> dict:
    from ..brain import board
    return _card_or_404(board.move, card_id, payload.get("status") or "",
                        payload.get("before"))


@app.post("/api/board/{card_id}/park")
def api_board_park(card_id: int, payload: dict = Body(default={})) -> dict:
    from ..brain import board
    return _card_or_404(board.park, card_id, payload.get("note") or "")


@app.get("/api/board/{card_id}/resume")
def api_board_resume(card_id: int) -> dict:
    from ..brain import board
    return _card_or_404(board.resume, card_id)


@app.post("/api/board/{card_id}/open")
def api_board_open(card_id: int, payload: dict = Body(default={})) -> dict:
    from ..brain import board
    return {"ok": True, "message": _card_or_404(board.open_project, card_id,
                                                 payload.get("how") or "code")}


@app.post("/api/board/{card_id}/claude")
def api_board_claude(card_id: int) -> dict:
    from ..brain import board
    return {"ok": True, "message": _card_or_404(board.resume_claude, card_id)}


@app.post("/api/board/{card_id}/focus")
def api_board_focus(card_id: int, payload: dict = Body(default={})) -> dict:
    from ..brain import board
    return _card_or_404(board.start_focus, card_id, int(payload.get("minutes") or 50))


@app.get("/api/focus")
def api_focus() -> dict:
    from ..brain import board
    return {"focus": board.focus_state()}


@app.post("/api/focus/stop")
def api_focus_stop() -> dict:
    from ..brain import board
    return {"stopped": board.stop_focus()}


@app.post("/api/board/{card_id}/snooze")
def api_board_snooze(card_id: int, payload: dict = Body(default={})) -> dict:
    from ..brain import board
    return _card_or_404(board.snooze, card_id, int(payload.get("minutes") or 15))


@app.post("/api/board/{card_id}/remind")
def api_board_remind(card_id: int, payload: dict = Body(default={})) -> dict:
    """Set a reminder from text ("5pm", "tomorrow 10am") or clear it (empty)."""
    from ..brain import board
    text = (payload.get("when") or "").strip()
    if not text:
        return _card_or_404(board.set_reminder, card_id, None)
    _, at, _ = board.parse_when(text)
    if not at:
        raise HTTPException(400, f"could not read a time in {text!r}")
    return _card_or_404(board.set_reminder, card_id, at)


@app.post("/api/board/{card_id}/restore")
def api_board_restore(card_id: int) -> dict:
    from ..brain import board
    return _card_or_404(board.restore, card_id)


@app.post("/api/board/{card_id}/dismiss")
def api_board_dismiss(card_id: int) -> dict:
    from ..brain import board
    return {"ok": board.dismiss(card_id)}


@app.post("/api/board/{card_id}")
def api_board_edit(card_id: int, payload: dict = Body(...)) -> dict:
    from ..brain import board
    return _card_or_404(board.edit, card_id, **payload)


@app.get("/api/shutdown")
def api_shutdown() -> dict:
    from ..brain import board
    return board.shutdown()


@app.post("/api/shutdown")
def api_shutdown_commit(payload: dict = Body(...)) -> dict:
    from ..brain import board
    try:
        return board.close_day(payload.get("items") or [])
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/plan/block")
def api_plan_block(payload: dict = Body(...)) -> dict:
    from ..brain import board
    return board.time_block([int(i) for i in payload.get("ids") or []],
                            int(payload.get("minutes") or 60))


@app.get("/api/week")
def api_week(offset: int = 0) -> dict:
    from ..brain import board
    return board.week(int(offset))


@app.post("/api/review")
def api_review(payload: dict = Body(...)) -> dict:
    from ..brain import board
    try:
        return board.review(payload.get("decisions") or [])
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/plan")
def api_plan() -> dict:
    from ..brain import board
    return board.plan()


@app.post("/api/plan")
def api_plan_commit(payload: dict = Body(...)) -> dict:
    from ..brain import board
    return board.commit_plan([int(i) for i in payload.get("ids") or []])


# --------------------------------------------------------------------------- control

@app.post("/api/pause")
def api_pause(payload: dict = Body(default={})) -> dict:
    return {"result": tray.pause(int(payload.get("minutes", 60)))}


@app.post("/api/resume")
def api_resume() -> dict:
    return {"result": tray.resume()}


@app.post("/api/teach")
def api_teach(payload: dict = Body(...)) -> dict:
    kind = payload.get("kind", "preference")
    if kind == "classify":
        match = (payload.get("match") or "").strip()
        if not match:
            raise HTTPException(400, "match required")
        n = classify.teach(match, payload.get("category", ""), payload.get("realm", ""))
        return {"ok": True, "relabelled": n,
                "what": f"'{match}' -> {payload.get('category')}/{payload.get('realm')}"}
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    db.remember(kind, text, payload.get("key"), float(payload.get("weight", 1.0)))
    return {"ok": True, "what": text}


@app.get("/api/memory")
def api_memory() -> list[dict]:
    return db.rows_to_dicts(db.recall(limit=100))


@app.delete("/api/memory/{mem_id}")
def api_forget(mem_id: int) -> dict:
    with db.tx() as c:
        return {"deleted": c.execute("DELETE FROM memory WHERE id=?", (mem_id,)).rowcount}


@app.get("/api/runs")
def api_runs(limit: int = 60, all: bool = False) -> list[dict]:
    """The decision diary. No-op runs are hidden unless ?all=1."""
    from ..daemon import is_quiet_run
    limit = max(1, min(limit, 500))
    out: list[dict] = []
    before = time.time() + 1
    while len(out) < limit:
        rows = db.rows_to_dicts(db.q("SELECT * FROM runs WHERE ts < ? ORDER BY ts DESC "
                                     "LIMIT ?", (before, limit * 4)))
        if not rows:
            break
        for r in rows:
            if all or not is_quiet_run(r["name"], r["ok"], r["summary"]):
                r["ago"] = human_ago(r["ts"])
                out.append(r)
        before = rows[-1]["ts"]
        if len(rows) < limit * 4:
            break
    return out[:limit]


@app.get("/api/doctor")
def api_doctor() -> dict:
    from .. import doctor
    # daemon: we ARE the daemon, and asking our own API from inside a request
    # handler would only measure ourselves. autostart: slow PowerShell.
    checks = doctor.run_checks(skip=("daemon", "autostart"))
    return {"checks": checks, "worst": doctor.worst(checks)}


@app.post("/api/shutdown")
def api_shutdown() -> dict:
    """Let `argus stop` end the daemon cleanly instead of taskkill /F, so the
    sensors flush their open sessions. Same-origin/CLI only (see the guard)."""
    from .. import daemon
    lg.info("shutdown requested over the local API")
    threading.Timer(0.3, daemon.STOP.set).start()
    return {"ok": True}


@app.post("/api/job/{name}")
def api_run_job(name: str) -> dict:
    from .. import daemon
    job = daemon.SCHED.by_name(name)
    if not job:
        raise HTTPException(404, f"no job '{name}'")
    ok, summary = daemon.SCHED.run_job(job, forced=True)
    return {"ok": ok, "summary": summary}


# --------------------------------------------------------------------------- privacy

@app.get("/api/privacy")
def api_privacy() -> dict:
    """Full disclosure of what is stored, and how much of it."""
    def count(table: str) -> int:
        r = db.q1(f"SELECT COUNT(*) n FROM {table}")
        return r["n"] if r else 0

    oldest = db.q1("SELECT MIN(ts) t FROM events")
    shots = 0
    shot_bytes = 0
    if SHOTS.is_dir():
        for d in SHOTS.rglob("*.jpg"):
            shots += 1
            try:
                shot_bytes += d.stat().st_size
            except Exception:
                pass
    from ..gsuite import guard
    return {
        "keylogging": False,
        "google_guard": guard.status(),
        "google_scopes": [s.rsplit("/", 1)[-1] for s in gauth.SCOPES],
        "clipboard_capture": bool(config.get("sensors.clipboard.enabled", False)),
        "screenshots_enabled": bool(config.get("sensors.screenshot.enabled", False)),
        "screenshot_count": shots,
        "screenshot_mb": round(shot_bytes / 1e6, 1),
        "db_mb": db.db_size_mb(),
        # This list is hardcoded, so a new table that is not declared here makes
        # the privacy page silently under-report what is stored.
        "rows": {t: count(t) for t in
                 ("events", "activity", "metrics", "alerts", "digests", "gmail", "gcal",
                  "tasks", "memory", "runs", "threads", "thread_touches", "feedback")},
        "journal_files": len(list(JOURNAL.glob("*.md"))),
        "claude_transcript_mining": bool(config.get("threads.transcripts", True)),
        "oldest_event": oldest["t"] if oldest and oldest["t"] else None,
        "retention": config.get("privacy.retain_days", {}),
        "private_apps": config.get("privacy.private_apps", []),
        "sensitive_domains": config.get("privacy.sensitive_domains", []),
        "data_dir": str(db.DB_PATH.parent),
        "network_exposure": "127.0.0.1 only; no telemetry; LLM calls send activity "
                            "summaries only when you or a digest asks",
    }


# State keys that hold copies of observed activity (as opposed to watermarks,
# which must survive a purge or the next poll would re-ingest the same history).
_ACTIVITY_STATE = ("git_states", "net_snapshot", "health_summary", "drive_seen")


@app.post("/api/privacy/purge")
def api_purge(payload: dict = Body(default={})) -> dict:
    """Delete data. `scope` one of: day, screenshots, browser, threads, all.

    Two things used to make this lie. "screenshots" was missing from the
    branch inside the transaction, so it hit the error branch and the
    dashboard's Delete screenshots button returned 400 every time. And "all"
    cleared the tables but left data/journal/*.md - every evening wrap with
    its raw activity facts - and the screenshots on disk.

    Deleted rows also linger in SQLite's free pages until the file is rebuilt,
    so every purge ends with a VACUUM.
    """
    from ..sensors import screenshot
    scope = payload.get("scope", "")
    day = payload.get("day")
    if scope == "day" and not (isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)):
        raise HTTPException(400, "day must be YYYY-MM-DD")
    if scope not in ("day", "screenshots", "browser", "threads", "all"):
        raise HTTPException(400, "scope must be day, screenshots, browser, threads or all")

    n = 0
    files = 0
    if scope != "screenshots":
        with db.tx() as c:
            if scope == "day":
                n = (c.execute("DELETE FROM activity WHERE day=?", (day,)).rowcount
                     + c.execute("DELETE FROM events WHERE day=?", (day,)).rowcount
                     + c.execute("DELETE FROM digests WHERE day=?", (day,)).rowcount)
            elif scope == "browser":
                n = c.execute("DELETE FROM events WHERE source='browser'").rowcount
            elif scope == "threads":
                n = (c.execute("DELETE FROM thread_touches").rowcount
                     + c.execute("DELETE FROM threads").rowcount)
            elif scope == "all":
                # "all" must actually mean all, or the delete-everything button
                # silently leaves the derived tables behind. What you taught it
                # (memory) and your task list are yours, not observations.
                for t in ("events", "activity", "metrics", "alerts", "digests",
                          "gmail", "gcal", "runs", "threads", "thread_touches",
                          "feedback"):
                    n += c.execute(f"DELETE FROM {t}").rowcount
                c.execute("DELETE FROM state WHERE key IN ("
                          + ",".join("?" * len(_ACTIVITY_STATE)) + ")", _ACTIVITY_STATE)

    if scope in ("screenshots", "all"):
        files += screenshot.purge(retain_days=0)
    if scope == "day":
        day_shots = SHOTS / day
        if day_shots.is_dir():
            for f in day_shots.iterdir():
                f.unlink(missing_ok=True)
                files += 1
            day_shots.rmdir()
    if scope in ("day", "all"):
        for f in ([JOURNAL / f"{day}.md"] if scope == "day" else list(JOURNAL.glob("*.md"))):
            if f.is_file():
                f.unlink()
                files += 1
    if scope == "all":
        # A backup is a full copy of everything just deleted; keeping it would
        # make "Delete everything" a lie.
        from ..paths import BACKUPS
        for f in BACKUPS.glob("argus-*.db*") if BACKUPS.is_dir() else []:
            f.unlink(missing_ok=True)
            files += 1

    if scope != "screenshots":
        try:
            db.vacuum()
            db.checkpoint()
        except Exception as e:                   # busy: the rows are gone either way
            lg.warning("vacuum after purge deferred: %s", e)
    db.log_event("argus", "purge", scope, str(day or ""), value=n + files)
    return {"deleted": n + files, "rows": n, "files": files, "scope": scope}


@app.get("/api/journal")
def api_journal() -> list[dict]:
    out = []
    for f in sorted(JOURNAL.glob("*.md"), reverse=True)[:60]:
        out.append({"day": f.stem, "path": str(f), "size": f.stat().st_size})
    return out


@app.get("/api/journal/{day}", response_class=PlainTextResponse)
def api_journal_day(day: str) -> str:
    # `day` becomes part of a path; on Windows a backslash is a separator the
    # router does not stop, so "..\..\README" would have walked out of JOURNAL.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(404, "no journal for that day")
    f = JOURNAL / f"{day}.md"
    if not f.is_file():
        raise HTTPException(404, "no journal for that day")
    return f.read_text(encoding="utf-8")


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=2), limit: int = 60) -> dict:
    like = f"%{q}%"
    return {
        "activity": db.rows_to_dicts(db.q(
            "SELECT app,title,category,started,seconds FROM activity "
            "WHERE (title LIKE ? OR app LIKE ?) AND private=0 "
            "ORDER BY started DESC LIMIT ?", (like, like, limit))),
        "events": db.rows_to_dicts(db.q(
            "SELECT source,kind,subject,detail,ts FROM events "
            "WHERE subject LIKE ? OR detail LIKE ? ORDER BY ts DESC LIMIT ?",
            (like, like, limit))),
    }


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# --------------------------------------------------------------------------- runner

def serve(stop: threading.Event | None = None) -> None:
    import uvicorn
    port = int(config.get("daemon.web_port", 7842))
    # log_config=None is required, not cosmetic. Uvicorn's default formatter calls
    # sys.stdout.isatty(), and in the windowed frozen build sys.stdout is None - so
    # configuring its logging raises and takes the whole web thread down. We have
    # our own file logging anyway.
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                         access_log=False, log_config=None)
    server = uvicorn.Server(cfg)

    if stop is not None:
        def _watch() -> None:
            stop.wait()
            server.should_exit = True
        threading.Thread(target=_watch, name="web.stop", daemon=True).start()

    lg.info("dashboard on http://127.0.0.1:%d", port)
    try:
        server.run()
    except Exception:
        lg.exception("web server crashed")
