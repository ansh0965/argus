"""Telegram: ARGUS in your pocket when you are away from the laptop.

Pair once with `argus telegram setup <bot-token>` (make the bot with
@BotFather), then send it /start. After that:

  - reminders and warn/urgent alerts are also sent to that chat;
  - anything you send the bot is captured onto the board ("call Sam at
    5pm" gets its reminder; "schedule ..." books the calendar);
  - /today lists today's cards, /next gives the top next step.

Only the paired chat is listened to; messages from anyone else are
ignored. The token and chat id live in data/secrets/telegram.json, which
`harden_secrets` locks to this Windows user.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config
from ..paths import SECRETS
from ..util import log

lg = log("actions.telegram")
FILE = SECRETS / "telegram.json"
API = "https://api.telegram.org/bot{token}/{method}"


def creds() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        return d if d.get("token") and d.get("chat_id") else {}
    except (OSError, ValueError):
        return {}


def enabled() -> bool:
    return bool(creds()) and bool(config.get("telegram.enabled", True))


def _call(token: str, method: str, params: dict | None = None, timeout: float = 20) -> dict:
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 - fixed https host
        return json.loads(r.read().decode("utf-8"))


def send(text: str) -> bool:
    c = creds()
    if not c or not config.get("telegram.enabled", True):
        return False
    try:
        return bool(_call(c["token"], "sendMessage",
                          {"chat_id": c["chat_id"], "text": text[:4000],
                           "disable_web_page_preview": "true"}).get("ok"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        lg.warning("telegram send failed: %s", type(e).__name__)
        return False


def forward(title: str, body: str, severity: str, rule: str) -> None:
    """Called by notify for every delivered alert; sends the ones that matter
    away from the desk, without blocking the toast."""
    if not enabled():
        return
    wanted = rule.startswith("remind") or severity in (config.get("telegram.severities") or
                                                      ["warn", "urgent"])
    if wanted:
        threading.Thread(target=send, args=(f"{title}\n{body}".strip(),), daemon=True).start()


def handle(text: str) -> str:
    """One message from your phone -> the reply to send back."""
    from ..brain import board
    text = (text or "").strip()
    if not text or text == "/start":
        return "ARGUS here. Send me anything to put on your board, /today for today's list, /next for what to do next."
    if text.startswith("/today"):
        b = board.board()
        cards = b["columns"]["doing"] + b["columns"]["today"]
        if not cards:
            return "Nothing on today's list yet."
        return "Today:\n" + "\n".join(
            f"{'▶' if c['status'] == 'doing' else '•'} {c['title']}" for c in cards[:12]) + \
            f"\n{b['progress']['done']}/{b['progress']['total']} done"
    if text.startswith("/now") or board.is_what_now(text):
        return board.what_now()
    if text.startswith("/next"):
        b = board.board()
        for c in b["columns"]["doing"] + b["columns"]["today"] + b["columns"]["inbox"]:
            nxt = (c.get("meta") or {}).get("next")
            return f"{c['title']}" + (f"\n→ {nxt}" if nxt else "")
        return "Board is empty."
    try:
        c = board.capture(text)
    except ValueError as e:
        return f"Couldn't add that: {e}"
    return c.get("message") or f"Added: {c['title']}" + (
        " (reminder set)" if c.get("remind_at") else "")


def poll_once(offset: int, timeout: int = 25) -> int:
    """Read new messages, answer the paired chat, return the next offset."""
    c = creds()
    res = _call(c["token"], "getUpdates", {"offset": offset, "timeout": timeout},
                timeout=timeout + 10)
    for u in res.get("result", []):
        offset = max(offset, int(u["update_id"]) + 1)
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(c["chat_id"]):
            continue                                  # not you: ignore
        reply = handle(msg.get("text") or "")
        if reply:
            send(reply)
    return offset


def run(stop: threading.Event) -> None:
    offset = 0
    while not stop.is_set():
        if not enabled():
            stop.wait(60)
            continue
        try:
            offset = poll_once(offset)
        except (urllib.error.URLError, OSError, ValueError) as e:
            lg.debug("telegram poll: %s", type(e).__name__)
            stop.wait(30)


def setup(token: str, wait_seconds: int = 180) -> str:
    """Check the token, then wait for /start from your phone to learn the chat."""
    me = _call(token, "getMe")
    if not me.get("ok"):
        raise ValueError("Telegram did not accept that token")
    name = me["result"].get("username")
    print(f"  Bot @{name} found. Open Telegram, find @{name} and send it /start ...")
    deadline, offset = time.time() + wait_seconds, 0
    while time.time() < deadline:
        res = _call(token, "getUpdates", {"offset": offset, "timeout": 20}, timeout=30)
        for u in res.get("result", []):
            offset = int(u["update_id"]) + 1
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("id"):
                FILE.write_text(json.dumps({"token": token, "chat_id": chat["id"]}), encoding="utf-8")
                _call(token, "getUpdates", {"offset": offset})     # acknowledge
                _call(token, "sendMessage", {"chat_id": chat["id"],
                                             "text": "Paired with ARGUS. Send me anything to capture it."})
                return f"paired with @{name}"
    raise TimeoutError("no /start received - run the setup again and message the bot")
