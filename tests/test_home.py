"""The Home page: one thing to do now, today, what needs you, what's unfinished."""
from argus.brain import board


def test_friendly_titles():
    assert board.friendly("Pick back up: budgetapp?") == "Unfinished: budgetapp"
    assert board.friendly("atlas: finish feat/enterprise-v2") == "Finish your work on atlas (feat/enterprise-v2)"
    assert board.friendly("phrasebook: finish testing") == "Finish your work on phrasebook (testing)"
    assert board.friendly("call Sam") == "call Sam"
    assert board.friendly("atlas: tenant export") == "atlas: tenant export"


def test_home_sections_and_the_now_card():
    assert board.home()["now"]["kind"] == "empty"
    hang = board.add("Pick back up: formkit?", origin="drop:formkit", source="auto",
                     meta={"kind": "dropped"})
    h = board.home()
    assert h["now"]["kind"] == "candidate" and h["now"]["headline"] == "Unfinished: formkit"
    assert [c["id"] for c in h["hanging"]] == [hang["id"]]
    t = board.add("write the export", status="today")
    h = board.home()
    assert h["now"]["kind"] == "today" and h["now"]["action"] == "start"
    board.move(t["id"], "doing")
    h = board.home()
    assert h["now"]["kind"] == "doing" and h["now"]["action"] == "done"
    assert [c["id"] for c in h["today"]] == [t["id"]]
    mine = board.add("buy a charger")
    assert [c["id"] for c in board.home()["inbox"]] == [mine["id"]]


def test_home_api():
    from fastapi.testclient import TestClient

    from argus.web import server
    cl = TestClient(server.app, base_url="http://127.0.0.1:7842")
    j = cl.get("/api/home").json()
    assert {"now", "today", "needs", "hanging", "inbox", "progress"} <= set(j)
