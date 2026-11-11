"""LLM access with graceful degradation.

Provider order (configurable): claude_cli -> gemini -> anthropic.

claude_cli shells out to the Claude Code CLI you already have logged in, so the
default setup needs no API key and costs nothing extra. If that is unavailable
we fall back to the Gemini key already in your environment, then to a raw
Anthropic key if you ever set one.

Every call is budgeted and logged so an automation bug cannot burn your quota.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import date

from .. import config, db
from ..paths import ROOT
from ..util import log, run

lg = log("brain.llm")


class BrainUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------- budget

def _budget_ok() -> bool:
    key = f"llm_calls:{date.today().isoformat()}"
    used = int(db.get_state(key, 0) or 0)
    return used < int(config.get("brain.max_daily_calls", 200))


def _budget_spend(provider: str, ms: int) -> None:
    # One statement, so two digests finishing together cannot both read N and
    # both write N+1.
    key = f"llm_calls:{date.today().isoformat()}"
    with db.tx() as c:
        c.execute("INSERT INTO state(key,val,ts) VALUES(?, '1', ?) "
                  "ON CONFLICT(key) DO UPDATE SET "
                  "val = CAST(CAST(val AS INTEGER) + 1 AS TEXT), ts = excluded.ts",
                  (key, time.time()))
    db.log_event("brain", "call", provider, "", value=ms)


def calls_today() -> int:
    return int(db.get_state(f"llm_calls:{date.today().isoformat()}", 0) or 0)


# --------------------------------------------------------------------------- providers

# The prompts ARGUS builds carry text other people control - mail subjects and
# senders, window titles, page titles. So the CLI runs as a pure text model:
#
#   --tools ""                  no Read/Bash/WebFetch. A prompt-injected subject
#                               line cannot make it open data/secrets or the DB.
#   --no-session-persistence    briefings stop landing in your Claude Code
#                               history (and in the transcripts threads.py mines).
#   --strict-mcp-config         no MCP servers: faster start, nothing reachable.
#
# The prompt itself goes over stdin: argv on Windows is capped at 32,767
# characters, which a weekly retro's facts can approach.
_HARDENED = ["--tools", "", "--no-session-persistence", "--strict-mcp-config"]
_hardening_ok = True


def _claude_cli(prompt: str, system: str, timeout: int) -> str:
    global _hardening_ok
    exe = shutil.which("claude")
    if not exe:
        raise BrainUnavailable("claude CLI not on PATH")
    model = config.get("brain.claude_cli_model", "sonnet")
    base = [exe, "-p", "--model", model, "--output-format", "text"]
    if system:
        base += ["--append-system-prompt", system]
    cmd = base + (_HARDENED if _hardening_ok else [])
    # A scratch dir with nothing in it, not data/ beside the database.
    cwd = ROOT / "data" / "brain"
    cwd.mkdir(parents=True, exist_ok=True)
    rc, out, err = run(cmd, timeout=timeout, cwd=cwd, input=prompt)
    if rc != 0 and _hardening_ok and "unknown option" in (err or "").lower():
        # An older CLI without these flags: still usable, just less contained.
        lg.warning("claude CLI rejected hardening flags (%s); upgrade it with "
                   "`claude update`", err[:120])
        _hardening_ok = False
        rc, out, err = run(base, timeout=timeout, cwd=cwd, input=prompt)
    if rc != 0 or not out:
        raise BrainUnavailable(f"claude cli rc={rc} {err[:200]}")
    return out.strip()


def _gemini(prompt: str, system: str, timeout: int) -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise BrainUnavailable("no GEMINI_API_KEY")
    import httpx
    model = config.get("brain.gemini_model", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    r = httpx.post(url, params={"key": key}, json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except Exception as e:
        raise BrainUnavailable(f"gemini shape {e}: {str(data)[:200]}")


def _anthropic(prompt: str, system: str, timeout: int) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise BrainUnavailable("no ANTHROPIC_API_KEY")
    import httpx
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": config.get("brain.anthropic_model", "claude-sonnet-5"),
              "max_tokens": 2000,
              "system": system or "",
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


PROVIDERS = {"claude_cli": _claude_cli, "gemini": _gemini, "anthropic": _anthropic}


# --------------------------------------------------------------------------- api

def available() -> list[str]:
    out = []
    for name in config.get("brain.providers", []):
        if name == "claude_cli" and shutil.which("claude"):
            out.append(name)
        elif name == "gemini" and (os.environ.get("GEMINI_API_KEY")
                                   or os.environ.get("GOOGLE_API_KEY")):
            out.append(name)
        elif name == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
            out.append(name)
    return out


def _order(fast: bool) -> list[str]:
    """Deep reasoning goes to the CLI; quick classification goes to Gemini first.

    Gemini answers in ~2s versus ~10s for a CLI round trip, which matters when a
    sensor loop is waiting on a triage decision.
    """
    order = list(config.get("brain.providers", ["claude_cli"]))
    if fast and "gemini" in order:
        order.remove("gemini")
        order.insert(0, "gemini")
    return order


def ask(prompt: str, system: str = "", timeout: int | None = None,
        allow_over_budget: bool = False, fast: bool = False) -> str:
    """Ask the brain. Raises BrainUnavailable only if every provider fails."""
    if not allow_over_budget and not _budget_ok():
        raise BrainUnavailable(f"daily LLM budget spent ({calls_today()} calls)")
    timeout = timeout or int(config.get("brain.timeout_seconds", 180))
    errors = []
    for name in _order(fast):
        fn = PROVIDERS.get(name)
        if not fn:
            continue
        t0 = time.time()
        try:
            out = fn(prompt, system, timeout)
            if out:
                _budget_spend(name, int((time.time() - t0) * 1000))
                return out
            errors.append(f"{name}: empty")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {str(e)[:160]}")
            lg.warning("provider %s failed: %s", name, str(e)[:200])
    raise BrainUnavailable(" | ".join(errors) or "no providers configured")


def ask_json(prompt: str, system: str = "", timeout: int | None = None,
             fast: bool = True) -> dict | list:
    """Ask for JSON and parse defensively (models like to wrap it in fences)."""
    guard = ("Respond with valid JSON only. No prose, no markdown fences, "
             "no explanation before or after.")
    raw = ask(prompt, f"{system}\n{guard}".strip(), timeout, fast=fast)
    return parse_json(raw)


def parse_json(raw: str):
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    raise ValueError(f"not JSON: {s[:300]}")


def ask_safe(prompt: str, system: str = "", fallback: str = "") -> str:
    """Never raises. Returns `fallback` when the brain is down."""
    try:
        return ask(prompt, system)
    except Exception as e:
        lg.warning("ask_safe fell back: %s", str(e)[:200])
        return fallback
