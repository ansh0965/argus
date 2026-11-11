"""GitHub: the work other people are waiting on you for, as board cards.

Three kinds of card, kept current every 15 minutes and closed by evidence:

  - review:  a pull request where your review is requested
  - ci:      one of your open pull requests whose checks are failing
  - changes: one of your open pull requests with changes requested

Every account `gh` is logged in to is asked (this machine has a personal
and an office account). Tokens come from `gh auth token --user <name>` and
are handed to `gh` through its environment only - never stored or logged.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from . import config, db
from .util import log

lg = log("github")

QUERY = """
query($q: String!) { search(query: $q, type: ISSUE, first: 30) { nodes { ... on PullRequest {
  number title url isDraft reviewDecision updatedAt
  repository { nameWithOwner } author { login }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
} } } }"""


def _gh(args: list[str], token: str | None = None, timeout: int = 40) -> tuple[int, str]:
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                           env=env, encoding="utf-8", errors="replace", creationflags=flags,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        lg.warning("gh %s failed: %s", args[:2], type(e).__name__)
        return 1, ""


def accounts() -> list[str]:
    """Logins `gh` holds credentials for, from `gh auth status`."""
    rc, out = _gh(["auth", "status"], timeout=40)
    # gh writes status to stderr on some versions; ask again capturing both
    if not out:
        try:
            p = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True,
                               timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
            out = (p.stdout or "") + (p.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            return []
    wanted = config.get("github.accounts") or []
    found = re.findall(r"account (\S+)", out)
    return [a for a in dict.fromkeys(found) if not wanted or a in wanted]


def _search(token: str, q: str) -> list[dict]:
    rc, out = _gh(["api", "graphql", "-f", f"query={QUERY}", "-f", f"q={q}"], token)
    if rc != 0 or not out:
        return []
    try:
        return [n for n in json.loads(out)["data"]["search"]["nodes"] if n]
    except (ValueError, KeyError, TypeError):
        return []


def _ci_state(pr: dict) -> str:
    try:
        roll = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] or {}
        return roll.get("state") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _work_login(login: str) -> bool:
    """Is this `gh` account an office one?

    Yes when it is listed in `realms.work_github_logins`, or when its name
    carries one of your `realms.work_markers` (a login like "jane-acme" with
    the marker "acme").
    """
    from . import classify, config
    logins = config.get("realms.work_github_logins", []) or []
    if login.lower() in {str(x).lower() for x in logins}:
        return True
    return classify.realm_of(login) == "work"


def wanted_cards(prs_by_login: dict[str, dict[str, list[dict]]]) -> dict[str, dict]:
    """origin -> card fields, for everything that should be on the board now."""
    out: dict[str, dict] = {}
    for login, groups in prs_by_login.items():
        realm = "work" if _work_login(login) else ""
        for pr in groups.get("review", []):
            ref = f"{pr['repository']['nameWithOwner']}#{pr['number']}"
            out[f"gh-review:{ref}"] = {
                "title": f"Review: {pr['title']}", "notes": f"{ref} by {pr['author']['login']}",
                "url": pr["url"], "kind": "review", "realm": realm}
        for pr in groups.get("mine", []):
            if pr.get("isDraft"):
                continue
            ref = f"{pr['repository']['nameWithOwner']}#{pr['number']}"
            if _ci_state(pr) in ("FAILURE", "ERROR"):
                out[f"gh-ci:{ref}"] = {"title": f"CI failing: {pr['title']}", "notes": ref,
                                       "url": pr["url"] + "/checks", "kind": "ci", "realm": realm}
            if pr.get("reviewDecision") == "CHANGES_REQUESTED":
                out[f"gh-changes:{ref}"] = {"title": f"Changes requested: {pr['title']}",
                                            "notes": ref, "url": pr["url"], "kind": "changes",
                                            "realm": realm}
    return out


def apply(wanted: dict[str, dict]) -> dict:
    """Make the board match: add new cards, close the ones that resolved."""
    from .brain import board
    made = closed = 0
    existing = {r["origin"]: dict(r) for r in db.q(
        "SELECT * FROM tasks WHERE origin LIKE 'gh-%'")}
    for origin, w in wanted.items():
        if origin not in existing:
            board.add(w["title"], status="inbox", notes=w["notes"], origin=origin,
                      realm=w["realm"], source="github",
                      meta={"url": w["url"], "kind": w["kind"]})
            made += 1
        elif existing[origin]["done"] and not existing[origin]["dismissed"]:
            board.move(existing[origin]["id"], "inbox")       # it came back (CI red again)
            made += 1
    for origin, c in existing.items():
        if origin not in wanted and not c["done"] and not c["dismissed"]:
            with db.tx() as cx:
                cx.execute("UPDATE tasks SET notes=? WHERE id=?",
                           ((c["notes"] or "") + " - resolved on GitHub", c["id"]))
            board.move(c["id"], "done")
            closed += 1
    return {"made": made, "closed": closed, "open": len(wanted)}


def sync() -> str:
    if not config.get("github.enabled", True):
        return "disabled"
    logins = accounts()
    if not logins:
        return "gh not logged in"
    found: dict[str, dict[str, list[dict]]] = {}
    for login in logins:
        rc, token = _gh(["auth", "token", "--user", login])
        token = token.strip()
        if rc != 0 or not token:
            continue
        found[login] = {
            "review": _search(token, f"is:pr is:open review-requested:{login} archived:false"),
            "mine": _search(token, f"is:pr is:open author:{login} archived:false"),
        }
    if not found:
        return "no usable gh account"
    r = apply(wanted_cards(found))
    return (f"github: {r['open']} open item(s), {r['made']} new, {r['closed']} resolved"
            if r["made"] or r["closed"] else "nothing new")
