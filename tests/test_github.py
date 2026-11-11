"""GitHub cards: reviews asked of you, your failing CI, changes requested."""
from argus import db, github
from argus.brain import board


def _pr(n, title, repo="org/atlas", ci="", decision=None, draft=False, author="sam"):
    return {"number": n, "title": title, "url": f"https://github.com/{repo}/pull/{n}",
            "isDraft": draft, "reviewDecision": decision, "repository": {"nameWithOwner": repo},
            "author": {"login": author},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": ci} if ci else None}}]}}


def _cards():
    return {r["origin"]: board.get(r["id"]) for r in db.q("SELECT * FROM tasks WHERE origin LIKE 'gh-%'")}


def test_the_three_kinds_of_card():
    w = github.wanted_cards({"jane-acme": {
        "review": [_pr(50, "Tenant export")],
        "mine": [_pr(49, "Feat/enterprise v2", ci="FAILURE", decision="CHANGES_REQUESTED"),
                 _pr(48, "Green one", ci="SUCCESS"),
                 _pr(47, "Draft", ci="FAILURE", draft=True)]}})
    assert set(w) == {"gh-review:org/atlas#50", "gh-ci:org/atlas#49", "gh-changes:org/atlas#49"}
    assert w["gh-review:org/atlas#50"]["title"] == "Review: Tenant export"
    assert w["gh-ci:org/atlas#49"]["realm"] == "work", "office account"


def test_cards_appear_and_close_themselves():
    github.apply({"gh-ci:org/a#1": {"title": "CI failing: x", "notes": "org/a#1",
                                    "url": "https://github.com/org/a/pull/1/checks",
                                    "kind": "ci", "realm": "work"}})
    c = _cards()["gh-ci:org/a#1"]
    assert c["status"] == "inbox" and c["meta"]["url"].endswith("/checks")
    github.apply({})                                   # CI went green
    assert _cards()["gh-ci:org/a#1"]["status"] == "done"
    github.apply({"gh-ci:org/a#1": {"title": "CI failing: x", "notes": "org/a#1",
                                    "url": "https://github.com/org/a/pull/1/checks",
                                    "kind": "ci", "realm": "work"}})
    assert _cards()["gh-ci:org/a#1"]["status"] == "inbox", "red again: back on the board"


def test_a_dismissed_github_card_stays_dismissed():
    w = {"gh-review:org/a#2": {"title": "Review: y", "notes": "", "url": "https://github.com/x",
                               "kind": "review", "realm": ""}}
    github.apply(w)
    board.dismiss(_cards()["gh-review:org/a#2"]["id"])
    r = github.apply(w)
    assert r["made"] == 0


def test_sync_without_gh(monkeypatch):
    monkeypatch.setattr(github, "accounts", lambda: [])
    assert github.sync() == "gh not logged in"
