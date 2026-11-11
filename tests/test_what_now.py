"""'What should I do now?' answered from the board."""
import time
from datetime import datetime

from argus.brain import board


def test_routing():
    for q in ("what should I do now?", "What's next", "where do I start", "what should i work on"):
        assert board.is_what_now(q), q
    assert not board.is_what_now("what did I do yesterday?")


def test_order_of_advice(monkeypatch):
    monkeypatch.setattr(board, "sharp_hours", lambda: [18, 20, 12])
    assert "board is empty" in board.what_now(datetime(2026, 9, 30, 10))
    board.add("atlas: finish feat/x", meta={"kind": "loop", "next": "Push 2 commits on feat/x"})
    assert "Best candidate" in board.what_now(datetime(2026, 9, 30, 10))
    t = board.add("write the export", status="today")
    assert board.what_now(datetime(2026, 9, 30, 10)).startswith("Top of today's list")
    assert "sharpest hours" in board.what_now(datetime(2026, 9, 30, 18))
    board.move(t["id"], "doing")
    assert board.what_now(datetime(2026, 9, 30, 10)).startswith("Finish what you started")
    board.add("pay rent", status="today", due=time.time() - 3600)
    assert board.what_now(datetime(2026, 9, 30, 10)).startswith("Overdue first")
    board.start_focus(t["id"], 25)
    assert board.what_now().startswith("Keep going on")


def test_ask_answers_it_without_the_model(monkeypatch):
    from fastapi.testclient import TestClient

    from argus.brain import digest
    from argus.web import server
    monkeypatch.setattr(digest, "answer", lambda q: (_ for _ in ()).throw(AssertionError("no LLM")))
    cl = TestClient(server.app, base_url="http://127.0.0.1:7842", headers={"Origin": "http://127.0.0.1:7842"})
    assert "board" in cl.post("/api/ask", json={"q": "what should I do now?"}).json()["answer"].lower()
