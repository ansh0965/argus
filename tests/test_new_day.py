"""A new day starts fresh; a planned 'pick back up' is done once you did it."""
import json
import time
from datetime import datetime, timedelta

from argus import db
from argus.brain import board, insights


def _age(card_id, hours):
    with db.tx() as c:
        c.execute("UPDATE tasks SET updated=?, created=? WHERE id=?",
                  (time.time() - hours * 3600, time.time() - hours * 3600, card_id))


def test_yesterdays_unfinished_cards_go_back_to_the_inbox():
    db.set_state("rollover_day", "2000-01-01")
    stale = board.add("write the spec", status="today")
    started = board.add("migrate users", status="today")
    board.move(started["id"], "doing")
    fresh = board.add("call Sam", status="today")
    s, _ = insights.day_bounds(insights.current_day())
    for cid in (stale["id"], started["id"]):
        with db.tx() as c:
            c.execute("UPDATE tasks SET updated=?, created=? WHERE id=?", (s - 3600, s - 3600, cid))
    assert board.rollover() == 2
    assert board.get(stale["id"])["status"] == "inbox"
    assert board.get(started["id"])["status"] == "inbox"
    assert board.get(started["id"])["meta"]["carried"]
    assert board.get(fresh["id"])["status"] == "today", "planned today: stays"
    assert board.rollover() == 0, "once a day"
    p = board.plan()
    assert {c["id"] for c in p["carried"]} >= {stale["id"], started["id"]}
    assert all(c["id"] not in {stale["id"], started["id"]} for c in p["rest"] + p["loops"]), "listed once"


def test_a_card_whose_project_is_active_today_stays_in_progress():
    db.set_state("rollover_day", "2000-01-01")
    now = time.time()
    with db.tx() as c:
        c.execute("INSERT INTO threads(key, seq, uid, title, first_ts, last_ts, status, confidence, score, meta, updated)"
                  " VALUES('atlas',1,'atlas:1','atlas',?,?,'active',0.9,1,'{}',?)", (now - 9e5, now - 600, now))
    c_ = board.add("atlas: tenant export", status="doing", meta={"thread": "atlas"})
    s, _ = insights.day_bounds(insights.current_day())
    with db.tx() as c:
        c.execute("UPDATE tasks SET updated=? WHERE id=?", (s - 3600, c_["id"]))
    board.rollover()
    assert board.get(c_["id"])["status"] == "doing"


def test_a_planned_pick_back_up_is_done_once_you_worked_on_it():
    now = time.time()
    with db.tx() as c:
        c.execute("INSERT INTO threads(key, seq, uid, title, realm, first_ts, last_ts, status, confidence,"
                  " score, open_loop, meta, updated) VALUES('budgetapp',1,'budgetapp:1','budgetapp','work',?,?,"
                  "'active',0.9,1,0,?,?)", (now - 30 * 86400, now - 1800, json.dumps({"repo": None}), now))
    card = board.add("Pick back up: budgetapp?", status="today", origin="drop:budgetapp", source="auto",
                     meta={"thread": "budgetapp", "kind": "dropped"})
    board.move(card["id"], "doing")
    with db.tx() as c:                      # chosen 3 hours ago, worked on until 30 min ago
        c.execute("UPDATE tasks SET started=?, updated=? WHERE id=?", (now - 3 * 3600, now - 3 * 3600, card["id"]))
    board.sync(force=True)
    done = board.get(card["id"])
    assert done["status"] == "done" and done["notes"] == "picked back up"


def test_carried_marker_survives_the_project_refresh():
    db.set_state("rollover_day", "2000-01-01")
    now = time.time()
    with db.tx() as c:
        c.execute("INSERT INTO threads(key, seq, uid, title, realm, first_ts, last_ts, status, confidence,"
                  " score, open_loop, meta, updated) VALUES('formkit',1,'formkit:1','formkit','work',?,?,"
                  "'paused',0.9,1,0,?,?)", (now - 30 * 86400, now - 5 * 86400, json.dumps({"repo": "C:/x"}), now))
    card = board.add("Pick back up: formkit?", status="today", origin="drop:formkit", source="auto",
                     meta={"thread": "formkit", "kind": "dropped"})
    s, _ = insights.day_bounds(insights.current_day())
    with db.tx() as c:
        c.execute("UPDATE tasks SET updated=? WHERE id=?", (s - 3600, card["id"]))
    board.board()                                    # rollover, then the sync refresh
    assert board.get(card["id"])["meta"].get("carried")
