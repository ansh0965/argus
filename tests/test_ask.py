from datetime import datetime

import pytest

from argus.brain import digest

WED = datetime(2026, 9, 23, 15, 0)       # a Wednesday


@pytest.mark.parametrize("q,expected", [
    ("how did I spend Tuesday?", ["2026-09-22"]),
    ("what about last wednesday", ["2026-09-16"]),
    ("and on wednesday?", ["2026-09-23"]),
    ("what did I do yesterday", ["2026-09-22"]),
    ("the day before yesterday", ["2026-09-21"]),
    ("3 days ago", ["2026-09-20"]),
    ("what happened on 2026-09-08", ["2026-09-08"]),
    ("what did I do on 8 Sep", ["2026-09-08"]),
    ("what did I do on September 8th", ["2026-09-08"]),
    ("compare monday and friday", ["2026-09-18", "2026-09-21"]),
    ("summarise this week", ["2026-09-21", "2026-09-22", "2026-09-23"]),
    ("when am I most productive?", []),
])
def test_resolve_days(q, expected):
    assert digest.resolve_days(q, WED) == expected


def test_future_dates_are_ignored():
    assert digest.resolve_days("what about 2026-10-01", WED) == []


def test_a_date_later_in_the_year_means_last_year():
    jan = datetime(2027, 1, 5, 10, 0)
    assert digest.resolve_days("what did I do on 28 Dec", jan) == ["2026-12-28"]


def test_last_week_is_capped():
    assert len(digest.resolve_days("last week and 2026-09-01", WED)) == digest.MAX_DAYS


def test_answer_includes_the_day_asked_about(monkeypatch):
    seen = {}

    def fake_ask(prompt, system="", fallback=""):
        seen["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(digest.llm, "ask_safe", fake_ask)
    monkeypatch.setattr(digest, "_repo_context", lambda: "")
    digest.answer("what did I do on 2026-09-08?")
    assert "Tuesday 08 Sep:\nDATE: 2026-09-08" in seen["prompt"]


def test_answers_are_kept_but_fallbacks_are_not(monkeypatch):
    from argus import db
    monkeypatch.setattr(digest, "_repo_context", lambda: "")
    monkeypatch.setattr(digest.llm, "ask_safe", lambda p, s="", fallback="": "You shipped OCR.")
    digest.answer("what did I do yesterday?")
    monkeypatch.setattr(digest.llm, "ask_safe", lambda p, s="", fallback="": fallback)
    digest.answer("and the day before?")
    rows = db.q("SELECT kind, title, body FROM digests")
    assert [(r["kind"], r["title"]) for r in rows] == [("answer", "what did I do yesterday?")]
