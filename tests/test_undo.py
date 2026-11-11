"""Undo: a removed card comes back where it was."""
from fastapi.testclient import TestClient

from argus.brain import board


def test_restore_undoes_a_dismiss():
    c = board.add("ship the export", status="today")
    board.dismiss(c["id"])
    assert all(x["id"] != c["id"] for col in board.board()["columns"].values() for x in col)
    r = board.restore(c["id"])
    assert r["dismissed"] == 0 and r["status"] == "today"


def test_restore_api():
    from argus.web import server
    cl = TestClient(server.app, base_url="http://127.0.0.1:7842", headers={"Origin": "http://127.0.0.1:7842"})
    c = board.add("x")
    cl.post(f"/api/board/{c['id']}/dismiss")
    assert cl.post(f"/api/board/{c['id']}/restore").json()["dismissed"] == 0
    assert cl.post("/api/board/99999/restore").status_code == 404
