"""Telegram: capture from the phone, alerts to it, only for the paired chat."""
import json

import pytest

from argus.actions import telegram
from argus.brain import board


@pytest.fixture
def paired(monkeypatch):
    telegram.FILE.write_text(json.dumps({"token": "T", "chat_id": 42}), encoding="utf-8")
    sent, inbox = [], []

    def fake_call(token, method, params=None, timeout=20):
        if method == "sendMessage":
            sent.append((params["chat_id"], params["text"]))
            return {"ok": True}
        if method == "getUpdates":
            out = [u for u in inbox if u["update_id"] >= int(params.get("offset", 0))]
            return {"ok": True, "result": out}
        return {"ok": True, "result": {"username": "argus_bot"}}
    monkeypatch.setattr(telegram, "_call", fake_call)
    yield sent, inbox
    telegram.FILE.unlink(missing_ok=True)


def test_messages_from_you_are_captured_and_answered(paired):
    sent, inbox = paired
    inbox.append({"update_id": 7, "message": {"chat": {"id": 42}, "text": "call Sam in 30 min"}})
    inbox.append({"update_id": 8, "message": {"chat": {"id": 999}, "text": "spam me"}})
    assert telegram.poll_once(0) == 9
    titles = [c["title"] for col in board.board()["columns"].values() for c in col]
    assert "call Sam" in titles and "spam me" not in titles, "only the paired chat"
    assert sent == [(42, "Added: call Sam (reminder set)")]


def test_today_and_next(paired):
    board.add("ship export", status="today")
    assert telegram.handle("/today").startswith("Today:\n• ship export")
    assert telegram.handle("/next").startswith("ship export")
    assert "Send me anything" in telegram.handle("/start")


def test_forward_only_what_matters(paired, monkeypatch):
    sent, _ = paired
    calls = []
    monkeypatch.setattr(telegram, "send", lambda text: calls.append(text))
    monkeypatch.setattr(telegram.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})())
    telegram.forward("Reminder: call Sam", "Due now.", "warn", "remind:3")
    telegram.forward("Morning brief", "...", "info", "morning_brief")
    telegram.forward("Disk almost full", "2 GB", "urgent", "disk")
    assert calls == ["Reminder: call Sam\nDue now.", "Disk almost full\n2 GB"]


def test_not_paired_is_quiet():
    telegram.FILE.unlink(missing_ok=True)
    assert telegram.enabled() is False and telegram.send("x") is False
