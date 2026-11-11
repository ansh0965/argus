"""The same piece of work captured twice stays one card."""
from argus.brain import board


def test_a_repeat_capture_becomes_a_note():
    first = board.capture("atlas cloudflare access fix")
    again = board.capture("fix the cloudflare access in atlas tomorrow")
    assert again["id"] == first["id"] and "Already on your board" in again["message"]
    card = board.get(first["id"])
    assert "fix the cloudflare access in atlas" in card["notes"] and card["due"]


def test_different_work_stays_separate():
    a = board.capture("atlas cloudflare access fix")
    b = board.capture("atlas tenant export")
    c = board.capture("call Sam")
    assert len({a["id"], b["id"], c["id"]}) == 3


def test_done_cards_are_not_merged_into():
    a = board.capture("write weekly report for Sam")
    board.move(a["id"], "done")
    b = board.capture("write weekly report for Sam")
    assert b["id"] != a["id"]
