"""The board: cards, columns, automatic cards from threads, the morning plan."""
import json
import time
from datetime import datetime, timedelta

import pytest

from argus import db
from argus.brain import board

WED_10 = datetime(2026, 9, 30, 10, 0)          # a Wednesday


@pytest.fixture(autouse=True)
def fresh_sync():
    board._last_sync = 0.0


@pytest.mark.parametrize("text, title, day, hour", [
    ("call Sam tomorrow", "call Sam", 1, 18),
    ("ship the deck today", "ship the deck", 0, 18),
    ("reply to Priya tonight", "reply to Priya", 0, 21),
    ("send invoice by friday", "send invoice", 2, 18),
    ("review PR on monday", "review PR", 5, 18),
    ("renew domain in 3 days", "renew domain", 3, 18),
    ("standup notes wednesday", "standup notes", 7, 18),    # the next Wednesday, not today
])
def test_capture_understands_plain_due_dates(text, title, day, hour):
    got_title, due = board.parse_due(text, WED_10)
    d = datetime.fromtimestamp(due)
    assert got_title == title
    assert (d.date() - WED_10.date()).days == day and d.hour == hour


def test_no_date_words_means_no_due_date():
    assert board.parse_due("fix the login bug", WED_10) == ("fix the login bug", None)


def test_capture_due_today_lands_in_today():
    assert board.capture("pay rent today")["status"] == "today"
    assert board.capture("read the RFC")["status"] == "inbox"


def test_cards_move_between_columns_and_done_is_done():
    c = board.add("write tests")
    assert board.move(c["id"], "doing")["started"]
    d = board.move(c["id"], "done")
    assert d["status"] == "done" and d["done"] == 1 and d["done_ts"]
    back = board.move(c["id"], "today")
    assert back["status"] == "today" and back["done"] == 0
    with pytest.raises(ValueError):
        board.move(c["id"], "someday")


def test_drag_above_another_card_orders_the_column():
    a, b, c = (board.add(t, status="today") for t in ("a", "b", "c"))
    board.move(c["id"], "today", before=a["id"])
    order = [x["title"] for x in board.board()["columns"]["today"]]
    assert order.index("c") < order.index("a")


def test_park_keeps_a_note_and_dismiss_hides():
    c = board.add("migrate db")
    p = board.park(c["id"], "stopped at the users table")
    assert p["status"] == "parked" and p["park_note"] == "stopped at the users table"
    board.dismiss(c["id"])
    assert all(x["id"] != c["id"] for col in board.board()["columns"].values() for x in col)


def test_edit_and_a_google_completion_both_count_as_done():
    c = board.add("old title")
    assert board.edit(c["id"], title="new title", pinned=1)["title"] == "new title"
    with db.tx() as cx:                      # what Google Tasks sync does
        cx.execute("UPDATE tasks SET done=1, done_ts=? WHERE id=?", (time.time(), c["id"]))
    assert board.get(c["id"])["status"] == "done"
    with pytest.raises(ValueError):
        board.edit(c["id"], title="  ")


def _thread(key, status="incomplete", open_loop=1, dirty=2, ahead=0, idle_days=3.0,
            confidence=0.9, intent=None):
    now = time.time()
    with db.tx() as c:
        c.execute("DELETE FROM threads WHERE key=?", (key,))
        cur = c.execute(
            "INSERT INTO threads(key, seq, uid, title, realm, first_ts, last_ts, status, "
            "confidence, score, open_loop, meta, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, 1, f"{key}:1", key, "work", now - 30 * 86400, now - idle_days * 86400,
             status, confidence, 1.0, open_loop,
             json.dumps({"repo": f"C:/src/{key}", "branch": "feat/x", "dirty": dirty,
                         "ahead": ahead}), now))
        if intent:
            c.execute("INSERT INTO thread_touches(thread_id, ts, source, evidence) "
                      "VALUES(?,?,?,?)", (cur.lastrowid, now - idle_days * 86400, "claude",
                                          intent))


def _auto(origin):
    r = db.q1("SELECT * FROM tasks WHERE origin=?", (origin,))
    return board.get(r["id"]) if r else None


def test_uncommitted_work_becomes_a_card_that_closes_itself():
    _thread("atlas", intent="fix the login bug in /documents")
    board.sync(force=True)
    card = _auto("loop:atlas")
    assert card and card["status"] == "inbox" and card["auto"]
    assert "2 uncommitted" in card["notes"]
    assert card["meta"]["intent"] == "fix the login bug in /documents"
    board.sync(force=True)
    assert db.q1("SELECT COUNT(*) n FROM tasks WHERE origin='loop:atlas'")["n"] == 1

    _thread("atlas", status="completed", open_loop=0, dirty=0)      # committed + pushed
    board.sync(force=True)
    assert _auto("loop:atlas")["status"] == "done"


def test_a_dismissed_automatic_card_is_not_recreated():
    _thread("formkit")
    board.sync(force=True)
    board.dismiss(_auto("loop:formkit")["id"])
    board.sync(force=True)
    assert db.q1("SELECT COUNT(*) n FROM tasks WHERE origin='loop:formkit'")["n"] == 1
    assert _auto("loop:formkit")["dismissed"] == 1


def test_work_that_went_quiet_is_a_dropped_card():
    _thread("pips", status="paused", open_loop=0, dirty=0, idle_days=5, intent="add webhooks")
    board.sync(force=True)
    card = _auto("drop:pips")
    assert card and card["title"].startswith("Pick back up") and card["meta"]["intent"]
    _thread("noise", status="paused", open_loop=0, dirty=0, idle_days=5, confidence=0.3)
    _thread("ancient", status="paused", open_loop=0, dirty=0, idle_days=60)
    board.sync(force=True)
    assert _auto("drop:noise") is None and _auto("drop:ancient") is None


def test_being_back_on_a_project_you_chose_moves_its_card_to_doing():
    _thread("indiamart", idle_days=3)
    _thread("formkit", idle_days=3)
    board.sync(force=True)
    board.move(_auto("loop:indiamart")["id"], "today")              # you picked this one
    _thread("indiamart", status="active", idle_days=0.001)
    _thread("formkit", status="active", idle_days=0.001)
    board.sync(force=True)
    assert _auto("loop:indiamart")["status"] == "doing"
    assert _auto("loop:formkit")["status"] == "inbox", "touching a project is not choosing it"


def test_a_dropped_project_that_comes_back_is_cleared_not_done():
    _thread("pips", status="paused", open_loop=0, dirty=0, idle_days=5, intent="add webhooks")
    board.sync(force=True)
    _thread("pips", status="active", open_loop=0, dirty=0, idle_days=0.5)
    board.sync(force=True)
    card = _auto("drop:pips")
    assert card["dismissed"] == 1 and not card["done"], "you did not finish anything"


def test_board_groups_columns_and_counts_progress():
    board.add("a", status="today")
    d = board.add("b", status="today")
    board.move(d["id"], "done")
    b = board.board()
    assert [c["title"] for c in b["columns"]["today"]] == ["a"]
    assert b["progress"] == {"done": 1, "total": 2}
    assert b["planned"] is False


def test_overdue_cards_lead_today():
    board.add("later", status="today", due=time.time() + 86400)
    board.add("late", status="today", due=time.time() - 3600)
    cols = board.board()["columns"]["today"]
    assert cols[0]["title"] == "late" and cols[0]["overdue"]


def test_the_morning_plan_offers_and_commits():
    due = board.add("pay bill", due=time.time() + 3600)
    other = board.add("someday idea")
    carried = board.add("unfinished", status="doing")
    p = board.plan()
    assert [c["title"] for c in p["due"]] == ["pay bill"]
    assert any(c["id"] == carried["id"] for c in p["carried"])
    assert any(c["id"] == other["id"] for c in p["rest"])
    assert "yesterday" in p and p["planned"] is False
    b = board.commit_plan([due["id"], other["id"]])
    assert b["planned"] is True
    assert [c["title"] for c in b["columns"]["today"]][:2] == ["pay bill", "someday idea"]
    assert board.get(carried["id"])["status"] == "doing", "a card in progress stays there"


def test_summary_line_for_the_briefings():
    assert board.summary_line() == ""
    board.add("write the deck", status="today")
    assert "write the deck" in board.summary_line()


def test_the_board_api():
    from fastapi.testclient import TestClient

    from argus.web import server
    c = TestClient(server.app, base_url="http://127.0.0.1:7842",
                   headers={"Origin": "http://127.0.0.1:7842"})
    card = c.post("/api/board/capture", json={"text": "call Sam tomorrow"}).json()
    assert card["title"] == "call Sam" and card["due"]
    assert c.post(f"/api/board/{card['id']}/move", json={"status": "doing"}).json()["status"] == "doing"
    assert c.post(f"/api/board/{card['id']}/move", json={"status": "nope"}).status_code == 400
    assert c.post(f"/api/board/{card['id']}/park", json={"note": "waiting"}).json()["park_note"] == "waiting"
    assert c.post(f"/api/board/{card['id']}", json={"title": "call Sam re login bug"}).json()["title"] == "call Sam re login bug"
    assert c.post("/api/board/99999/move", json={"status": "done"}).status_code == 404
    b = c.get("/api/board").json()
    assert set(b["columns"]) == {"inbox", "today", "doing", "parked", "done"}
    assert "carried" in c.get("/api/plan").json()
    assert c.post("/api/plan", json={"ids": [card["id"]]}).json()["planned"] is True
    assert c.post(f"/api/board/{card['id']}/dismiss").json()["ok"] is True


def test_the_plan_reminder_knocks_once_on_work_days(monkeypatch):
    from argus.automations import builtin
    sent = []
    monkeypatch.setattr(builtin.notify, "send", lambda title, body="", **k: sent.append((title, body, k.get("url"))))
    assert builtin.plan_reminder(datetime(2026, 10, 3, 9, 15)) == "not a work day"   # Saturday
    board.add("ship it", status="today")
    assert builtin.plan_reminder(datetime(2026, 9, 30, 9, 15)).startswith("nudged")
    assert sent[0][0] == "Plan your day" and "1 still on your plate" in sent[0][1]
    assert sent[0][2].endswith("#plan")
    board.commit_plan([])
    assert builtin.plan_reminder(datetime(2026, 9, 30, 9, 15)) == "already planned"


def test_the_capture_hotkey_spec():
    from argus.actions import capture
    assert capture.parse_hotkey("ctrl+alt+space") == (0x0002 | 0x0001, 0x20)
    assert capture.parse_hotkey("Win+Shift+N") == (0x0008 | 0x0004, ord("N"))
    for bad in ("", "space", "ctrl+hyper+k", "ctrl+banana"):
        with pytest.raises(ValueError):
            capture.parse_hotkey(bad)


# ---------------------------------------------------------------- reminders

@pytest.mark.parametrize("text, title, day, hm, timed", [
    ("call Sam at 5pm", "call Sam", 0, (17, 0), True),
    ("standup notes 9:30am tomorrow", "standup notes", 1, (9, 30), True),
    ("tomorrow 10am review PR", "review PR", 1, (10, 0), True),
    ("deploy at 17:30", "deploy", 0, (17, 30), True),
    ("ping Priya at 5", "ping Priya", 0, (17, 0), True),
    ("lunch with team at noon friday", "lunch with team", 2, (12, 0), True),
    ("pay rent friday", "pay rent", 2, (18, 0), False),
    ("check build at 9am", "check build", 1, (9, 0), True),          # 9am already passed
])
def test_when_understands_times(text, title, day, hm, timed):
    got, due, has_time = board.parse_when(text, WED_10)
    d = datetime.fromtimestamp(due)
    assert (got, (d.date() - WED_10.date()).days, (d.hour, d.minute), has_time) == \
        (title, day, hm, timed)


def test_relative_spans():
    for text, minutes in (("stretch in 30 min", 30), ("check deploy in 2 hours", 120),
                          ("call back in an hour", 60)):
        _, due, timed = board.parse_when(text, WED_10)
        assert timed and due == (WED_10.timestamp() + minutes * 60)


def test_numbers_that_are_not_times_stay_in_the_title():
    assert board.parse_when("merge PR 38", WED_10) == ("merge PR 38", None, False)
    assert board.parse_when("fix 2 bugs in atlas", WED_10)[1] is None


def test_a_timed_capture_reminds_once_and_can_be_snoozed(monkeypatch):
    from argus.automations import builtin
    sent = []
    monkeypatch.setattr(builtin.notify, "send", lambda title, body="", **k: sent.append(title))
    c = board.capture("call Sam in 30 min")
    assert c["remind_at"] and not c["reminded"]
    assert builtin.reminders(now=time.time()) == "nothing due"
    later = time.time() + 31 * 60
    assert builtin.reminders(now=later).startswith("reminded: call Sam")
    assert sent == ["Reminder: call Sam"]
    assert builtin.reminders(now=later + 60) == "nothing due", "once"
    board.snooze(c["id"], 10)
    assert builtin.reminders(now=time.time() + 11 * 60).startswith("reminded")
    board.move(c["id"], "done")
    board.snooze(c["id"], 1)
    assert builtin.reminders(now=time.time() + 120) == "nothing due", "done cards stay quiet"


def test_remind_api():
    from fastapi.testclient import TestClient

    from argus.web import server
    cl = TestClient(server.app, base_url="http://127.0.0.1:7842",
                    headers={"Origin": "http://127.0.0.1:7842"})
    c = board.add("ship it")
    assert cl.post(f"/api/board/{c['id']}/remind", json={"when": "tomorrow 10am"}).json()["remind_at"]
    assert cl.post(f"/api/board/{c['id']}/remind", json={"when": "whenever"}).status_code == 400
    assert cl.post(f"/api/board/{c['id']}/remind", json={"when": ""}).json()["remind_at"] is None
    assert cl.post(f"/api/board/{c['id']}/snooze", json={"minutes": 5}).json()["remind_at"]


def test_a_folder_of_files_is_not_a_dropped_project():
    # no repo, nothing asked of Claude: Desktop\PERSONAL collecting files
    _thread("personal", status="paused", open_loop=0, dirty=0, idle_days=5)
    with db.tx() as c:
        c.execute("UPDATE threads SET meta=? WHERE key='personal'", (json.dumps({"repo": None}),))
    board.sync(force=True)
    assert _auto("drop:personal") is None


def test_noise_cards_from_the_old_rule_are_cleared():
    _thread("research", status="paused", open_loop=0, dirty=0, idle_days=5)
    with db.tx() as c:
        c.execute("UPDATE threads SET meta=? WHERE key='research'", (json.dumps({"repo": None}),))
    board.add("Pick back up: research?", origin="drop:research", source="auto")
    board.sync(force=True)
    assert _auto("drop:research")["dismissed"] == 1


def test_resume_shows_where_you_were(monkeypatch):
    _thread("atlas", intent="fix the login bug in /documents")
    with db.tx() as c:
        tid = db.q1("SELECT id FROM threads WHERE key='atlas'")["id"]
        for i, (src, ev) in enumerate([("file", "documents.py"), ("git", "fix: login bug"),
                                       ("window", "VS Code: documents.py - atlas"),
                                       ("claude", "session: Security pass")]):
            c.execute("INSERT INTO thread_touches(thread_id, ts, source, evidence) VALUES(?,?,?,?)",
                      (tid, time.time() - i, src, ev))
    monkeypatch.setattr(board, "_changed_files", lambda repo, limit=12: ["api/documents.py"])
    board.sync(force=True)
    card = _auto("loop:atlas")
    p = board.resume(card["id"])["project"]
    assert p["branch"] == "feat/x" and p["dirty"] == 2
    assert p["asks"] == ["fix the login bug in /documents"] and p["session"] == "Security pass"
    assert p["files"] == ["documents.py"] and p["commits"] == ["fix: login bug"]
    assert p["windows"] == ["documents.py - atlas"]


def test_a_typed_card_finds_its_project_by_name():
    _thread("formkit")
    c = board.add("formkit: add rate limits")
    assert board.resume(c["id"])["project"]["key"] == "formkit"
    assert board.resume(board.add("buy milk")["id"])["project"] is None


# ---------------------------------------------------------------- scheduling

@pytest.fixture
def calendar(monkeypatch):
    from argus.gsuite import auth as gauth
    from argus.gsuite import gcal
    made = []
    monkeypatch.setattr(gauth, "labels", lambda: ["college", "personal", "work"])
    monkeypatch.setattr(gcal, "create_event", lambda summary, start, minutes=30, description="",
                        label="": made.append((summary, start, minutes, label)) or "https://cal/ev1")
    return made


def test_schedule_creates_an_event_on_the_right_calendar_and_a_card(calendar):
    r = board.schedule("schedule atlas review with Sam tomorrow 4pm for 45 min")
    (summary, start, minutes, label), = calendar
    assert summary == "atlas review with Sam" and minutes == 45 and label == "work"
    assert start.hour == 16 and r["calendar"] == "work"
    card = r["card"]
    assert card["remind_at"] == r["start"] - 600, "reminded ten minutes before"
    assert "on your work calendar" in r["message"]


def test_block_without_a_time_takes_the_next_free_stretch(calendar, monkeypatch):
    from argus.gsuite import gcal
    base = datetime.now().replace(hour=14, minute=7, second=0, microsecond=0)
    monkeypatch.setattr(gcal, "free_slots", lambda off, min_minutes=30: [
        {"start": (base + timedelta(days=off)).timestamp(),
         "end": (base + timedelta(days=off, hours=4)).timestamp()}])
    r = board.schedule("block 2h for portfolio")
    (summary, start, minutes, label), = calendar
    assert summary == "Focus: portfolio" and minutes == 120 and start.minute % 15 == 0
    assert label == "personal"


def test_schedule_without_google_still_lands_on_the_board(monkeypatch):
    from argus.gsuite import auth as gauth
    monkeypatch.setattr(gauth, "labels", lambda: [])
    r = board.schedule("schedule dentist friday 11am")
    assert r["event"] is None and "signed out" in r["message"]
    assert r["card"]["remind_at"]


def test_capture_and_ask_route_schedule_requests(calendar):
    from fastapi.testclient import TestClient

    from argus.web import server
    cl = TestClient(server.app, base_url="http://127.0.0.1:7842",
                    headers={"Origin": "http://127.0.0.1:7842"})
    c = cl.post("/api/board/capture", json={"text": "book 1:1 with Priya tomorrow 10am"}).json()
    assert "Scheduled" in c["message"]
    a = cl.post("/api/ask", json={"q": "schedule deploy review tomorrow 5pm"}).json()
    assert "Scheduled" in a["answer"] and len(calendar) == 2
    assert not board.is_schedule("call Sam at 5pm")


def test_drifting_off_a_doing_card_asks_once(monkeypatch):
    from argus.automations import builtin
    sent = []
    monkeypatch.setattr(builtin.notify, "send", lambda title, body="", **k: sent.append(title))
    monkeypatch.setattr(builtin, "_in_meeting", lambda: "")
    now = time.time()
    db.add_activity("Chrome", "chrome.exe", "YouTube", "media", "personal", now - 300, now - 10,
                    False, False)
    _thread("atlas", status="active", idle_days=1 / 24)           # left an hour ago
    board.sync(force=True)
    card = _auto("loop:atlas")
    board.move(card["id"], "doing")
    with db.tx() as c:
        c.execute("UPDATE tasks SET started=? WHERE id=?", (now - 2 * 3600, card["id"]))
    assert builtin.drift_check(now).startswith("nudged")
    assert sent == [f"Still on {card['title']}?"]
    _thread("atlas", status="active", idle_days=0.001)            # back on it
    sent.clear()
    assert builtin.drift_check(now) == "on track" and sent == []


def test_no_drift_nudge_while_away(monkeypatch):
    from argus.automations import builtin
    monkeypatch.setattr(builtin, "_in_meeting", lambda: "")
    assert builtin.drift_check(time.time()) == "away"


def test_a_fresh_install_catches_up_once(monkeypatch):
    from argus import backfill
    from argus.automations import builtin
    from argus.brain import threads
    calls = []
    monkeypatch.setattr(backfill, "run_all", lambda days=90, reset=True: calls.append(days) or
                        db.set_state("backfill_ran", 1) or
                        {"days_covered": 40, "browser": 900, "claude": 120, "git": 60})
    monkeypatch.setattr(threads, "rebuild", lambda: {"threads": 5})
    assert builtin.first_run_catch_up().startswith("caught up: 40 days")
    assert builtin.first_run_catch_up() == "already caught up" and calls == [90]


def test_the_newest_claude_session_for_a_project(tmp_path, monkeypatch, write_config):
    import os
    from pathlib import Path
    write_config({"watch_dir_ignores": []})
    from argus.brain import threads
    proj = tmp_path / "Desktop" / "atlas"
    proj.mkdir(parents=True)
    monkeypatch.setattr(threads, "_roots", lambda: [str(tmp_path / "Desktop")])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    d = tmp_path / ".claude" / "projects" / "C--atlas"
    d.mkdir(parents=True)
    old, new = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    for sid, title, age in ((old, "Old work", 500), (new, "Cloudflare secret", 10)):
        f = d / f"{sid}.jsonl"
        f.write_text(json.dumps({"cwd": str(proj), "sessionId": sid}) + "\n" +
                     json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": sid}) + "\n")
        os.utime(f, (time.time() - age, time.time() - age))
    (d / "not-a-session.jsonl").write_text("{}")
    s = board.last_claude_session("atlas")
    assert s["id"] == new and s["title"] == "Cloudflare secret" and s["cwd"] == str(proj)
    assert board.last_claude_session("nothing") is None


# ---------------------------------------------------------------- end of day

def test_timesheet_draft_lists_what_shipped_per_project(monkeypatch):
    from argus.brain import timesheet
    monkeypatch.setattr(timesheet, "day_row", lambda day, realm="work": {
        "first": WED_10.timestamp(), "last": WED_10.timestamp() + 7 * 3600,
        "work_seconds": 5.5 * 3600, "projects": ["atlas", "billing api"],
        "commits": ["atlas: fix: login bug in documents\nlong body", "atlas: bench v4"]})
    c = board.add("atlas: tenant export", status="doing")
    board.move(c["id"], "done")
    board.move(board.add("buy groceries", realm="personal")["id"], "done")
    d = board.timesheet_draft()
    lines = d["text"].splitlines()
    assert d["hours"] == 5.5 and "(5.5h)" in lines[0] and "10:00-17:00" in lines[0]
    assert lines[1] == "- atlas: tenant export; fix: login bug in documents; bench v4"
    assert lines[2] == "- billing api"
    assert "groceries" not in d["text"], "office work only"


def test_closing_the_day(monkeypatch):
    from argus.automations import builtin
    a = board.add("migrate users", status="doing")
    b = board.add("write docs", status="today")
    c = board.add("fix typo", status="today")
    s = board.shutdown()
    assert {x["id"] for x in s["open"]} == {a["id"], b["id"], c["id"]}
    board.close_day([{"id": a["id"], "action": "park", "note": "stopped at the orgs table"},
                     {"id": b["id"], "action": "tomorrow", "note": "half the API section"},
                     {"id": c["id"], "action": "done"}])
    assert board.get(a["id"])["status"] == "parked" and board.get(a["id"])["park_note"] == "stopped at the orgs table"
    assert board.get(b["id"])["status"] == "today" and board.get(b["id"])["park_note"] == "half the API section"
    assert board.get(c["id"])["status"] == "done"
    sent = []
    monkeypatch.setattr(builtin.notify, "send", lambda *a, **k: sent.append(a))
    assert builtin.shutdown_reminder(datetime(2026, 9, 30, 18, 45)) == "already wrapped up"


def test_the_shutdown_reminder(monkeypatch):
    from argus.automations import builtin
    sent = []
    monkeypatch.setattr(builtin.notify, "send", lambda title, body="", **k: sent.append((title, k["url"])))
    board.add("open thing", status="doing")
    assert builtin.shutdown_reminder(datetime(2026, 10, 4, 18, 45)) == "not a work day"   # Sunday
    assert builtin.shutdown_reminder(datetime(2026, 9, 30, 18, 45)).startswith("nudged")
    assert sent[0][0] == "Wrap up the day" and sent[0][1].endswith("#shutdown")


@pytest.mark.parametrize("args, step", [
    ((2, 0, "feat/x", [r"benchmarks\live\cf_check.py", "a.yml"]), "Commit 2 changed files on feat/x (cf_check.py, ...)"),
    ((1, 0, "main", ["src/app.py"]), "Commit 1 changed file on main (app.py)"),
    ((0, 5, "testing"), "Push 5 commits on testing"),
    ((0, 0, "main", None, "session: add webhooks"), "Continue: add webhooks"),
    ((0, 0, None), "Open it and decide: finish, park or drop"),
])
def test_next_step(args, step):
    assert board.next_step(*args) == step


def test_red_ci_comes_first():
    assert board.next_step(3, 2, "feat/x", ci_failing=True) == "Fix the failing CI on feat/x"


def test_cards_carry_their_next_step():
    _thread("phrasebook", dirty=0, ahead=5)
    board.sync(force=True)
    assert _auto("loop:phrasebook")["meta"]["next"] == "Push 5 commits on feat/x"


def test_reminders_missed_while_asleep_arrive_as_one(monkeypatch):
    from argus.automations import builtin
    sent = []
    monkeypatch.setattr(builtin.notify, "send", lambda title, body="", **k: sent.append(title))
    now = time.time()
    for i, title in enumerate(("call Sam", "pay rent", "review PR")):
        board.set_reminder(board.add(title)["id"], now - 3600 - i * 60)
    board.set_reminder(board.add("standup")["id"], now - 30)
    builtin.reminders(now)
    assert sorted(sent) == ["Reminder: standup", "While you were away: 3 reminders"]


def test_google_expiry_warning(monkeypatch):
    from argus.automations import builtin
    from argus.gsuite import auth as gauth
    sent = []
    monkeypatch.setattr(builtin.notify, "send", lambda title, body="", **k: sent.append(title))
    monkeypatch.setattr(gauth, "labels", lambda: ["work"])
    monkeypatch.setattr(gauth, "token_state", lambda label: {"state": "ok"})
    now = time.time()
    db.set_state("google_login_ts:work", now - 6.3 * 86400)
    assert builtin.google_expiry_check(now) == "warned: work"
    assert sent == ["Google will sign ARGUS out tomorrow"]
    db.set_state("google_login_ts:work", now - 9 * 86400)            # still working on day 9
    assert builtin.google_expiry_check(now).startswith("published")
    db.set_state("google_login_ts:work", now - 6.3 * 86400)
    assert builtin.google_expiry_check(now) == "published", "no more warnings once published"


def test_time_blocking_the_days_cards(calendar, monkeypatch):
    from argus.gsuite import gcal
    base = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=30, second=0, microsecond=0)
    monkeypatch.setattr(gcal, "free_slots", lambda off, min_minutes=30: [
        {"start": (base + timedelta(days=off - 1)).timestamp(),
         "end": (base + timedelta(days=off - 1, hours=8)).timestamp()}])
    a, b = board.add("atlas: tenant export"), board.add("write blog post")
    r = board.time_block([a["id"], b["id"]], 60, now=base - timedelta(hours=1))
    assert [x["title"] for x in r["placed"]] == ["atlas: tenant export", "write blog post"]
    s1, s2 = (datetime.fromtimestamp(x["start"]) for x in r["placed"])
    assert (s1.hour, s1.minute) == (10, 30) and s2 == s1 + timedelta(hours=1)
    assert [c[3] for c in calendar] == ["work", "personal"]
    assert board.get(a["id"])["remind_at"] == r["placed"][0]["start"] - 300


def test_meeting_prep_brings_the_project_cards_and_mail():
    _thread("atlas", dirty=2)
    board.add("atlas: tenant export for Sam")
    with db.tx() as c:
        c.execute("INSERT INTO gmail(id, ts, sender, sender_email, subject, unread) VALUES(?,?,?,?,?,1)",
                  ("m1", time.time(), "Sam Rivera", "sam.rivera@example.com", "Export format?"))
    lines = board.meeting_prep("atlas weekly sync", ["sam.rivera@example.com", "me@x.io"], "me@x.io")
    assert lines[0] == "atlas: Commit 2 changed files on feat/x"
    assert lines[1] == "Open: atlas: tenant export for Sam"
    assert lines[2] == "Unread from them: Sam Rivera: Export format?"
    assert board.meeting_prep("Ganesh Chaturthi", []) == []


def test_a_focus_session_holds_nudges_and_logs_itself(monkeypatch):
    from argus.automations import builtin
    sent = []
    monkeypatch.setattr(builtin.notify, "send", lambda title, body="", **k: sent.append(title))
    c = board.add("write the export")
    f = board.start_focus(c["id"], 25)
    assert board.get(c["id"])["status"] == "doing" and 24 * 60 < f["left"] <= 25 * 60
    assert builtin.drift_check(time.time()) in ("focusing", "away")
    assert builtin.break_reminder() == "focusing"
    assert builtin.reminders(time.time() + 26 * 60) == "focus ended"
    assert sent == ["Focus done: write the export"]
    assert board.focus_state() is None
    m = board.get(c["id"])["meta"]
    assert m["focus_sessions"] == 1 and m["focus_minutes"] == 25


def test_stopping_focus_early():
    c = board.add("deep thing")
    board.start_focus(c["id"], 50)
    r = board.stop_focus(time.time() + 10 * 60)
    assert r["spent"] == 10 and board.get(c["id"])["meta"]["focus_minutes"] == 10


# ---------------------------------------------------------------- the week

def test_week_shipped_streak_and_report():
    a = board.add("atlas: tenant export", status="today", realm="work")
    board.move(a["id"], "done")
    db.log_events([("git", "commit", "portfolio", "Redesign hero", 1, {"path": "C:/x/portfolio"}, time.time() - 60)])
    w = board.week()
    assert w["shipped"]["atlas"] == ["tenant export"] and "portfolio" in w["shipped"]
    assert w["streak"] >= 1
    rep = w["report"].splitlines()
    assert rep[1].startswith("Office") and rep[2] == "- atlas: tenant export"
    assert "Personal:" in rep and "- portfolio: Redesign hero" in rep


def test_stale_cards_and_the_review():
    old = board.add("someday refactor")
    gone = board.add("old idea")
    fresh = board.add("new thing")
    with db.tx() as c:
        c.execute("UPDATE tasks SET updated=?, created=? WHERE id IN (?,?)",
                  (time.time() - 9 * 86400, time.time() - 9 * 86400, old["id"], gone["id"]))
    w = board.week()
    assert {c["id"] for c in w["stale"]} == {old["id"], gone["id"]}
    w = board.review([{"id": old["id"], "action": "today"}, {"id": gone["id"], "action": "drop"}])
    assert board.get(old["id"])["status"] == "today" and board.get(gone["id"])["dismissed"] == 1
    assert w["stale"] == [] and board.get(fresh["id"])["status"] == "inbox"


def test_week_api():
    from fastapi.testclient import TestClient

    from argus.web import server
    cl = TestClient(server.app, base_url="http://127.0.0.1:7842", headers={"Origin": "http://127.0.0.1:7842"})
    assert len(cl.get("/api/week?offset=-1").json()["days"]) == 7
    assert cl.post("/api/review", json={"decisions": []}).status_code == 200
