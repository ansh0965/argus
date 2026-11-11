"""Thread engine: the parts that do not need a real ~/.claude directory."""
import json
import time

import pytest

from argus import db
from argus.brain import threads

REAL_COLLECT_CLAUDE = threads.collect_claude


@pytest.fixture(autouse=True)
def no_transcripts(monkeypatch, tmp_path, write_config):
    write_config({"watch_dir_ignores": []})       # tmp_path lives under AppData
    monkeypatch.setattr(threads, "collect_claude", lambda since: [])
    root = tmp_path / "Desktop"
    (root / "atlas").mkdir(parents=True)
    (root / "portfolio").mkdir(parents=True)
    monkeypatch.setattr(threads, "_roots", lambda: [str(root)])
    monkeypatch.setattr(threads, "_git_states", lambda wanted: {})
    return root


def _file_events(root, project, n, days=3):
    now = time.time()
    rows = []
    for i in range(n):
        ts = now - (i % days) * 86400 - i * 60
        rows.append(("file", "modified", f"f{i}.py", str(root / project / f"f{i}.py"), 1,
                     {"dir": str(root / project), "ext": ".py"}, ts))
    db.log_events(rows)


def test_realm_comes_from_the_same_markers_as_windows(no_transcripts):
    _file_events(no_transcripts, "atlas", 60)
    _file_events(no_transcripts, "portfolio", 60)
    threads.rebuild()
    realms = {r["key"]: r["realm"] for r in db.q("SELECT key, realm FROM threads")}
    assert realms == {"atlas": "work", "portfolio": "personal"}


def test_context_for_accepts_uid_or_key(no_transcripts):
    _file_events(no_transcripts, "atlas", 60)
    threads.rebuild()
    uid = db.q1("SELECT uid FROM threads")["uid"]
    assert threads.context_for(uid)["key"] == "atlas"
    assert threads.context_for("atlas")["uid"] == uid
    assert threads.context_for("nope") is None


def test_rebuild_is_idempotent_and_dismissal_hides(no_transcripts):
    _file_events(no_transcripts, "atlas", 60)
    a = threads.rebuild()
    b = threads.rebuild()
    assert a["threads"] == b["threads"] == 1
    threads.record_feedback("atlas", "dismissed")
    threads.rebuild()
    assert db.q1("SELECT COUNT(*) n FROM threads")["n"] == 0


def test_transcript_intent_from_a_pause_is_not_used(no_transcripts, monkeypatch, tmp_path):
    from argus import pause
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "p"
    proj.mkdir(parents=True)
    now = time.time()
    cwd = str(no_transcripts / "atlas")

    def line(ts, text):
        from datetime import datetime, timezone
        return json.dumps({"origin": {"kind": "human"}, "cwd": cwd, "sessionId": "s1",
                           "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                           "message": {"content": text}},
                          separators=(",", ":")) + "\n"

    (proj / "s1.jsonl").write_text(
        line(now - 7200, "add OCR triage to the atlas ingestion pipeline")
        + line(now - 60, "wire the retrieval eval harness into CI please"),
        encoding="utf-8")
    monkeypatch.setattr(threads, "HOME", str(home))
    monkeypatch.setattr(pause, "intervals", lambda: [(now - 7300, now - 7000)])
    evs = REAL_COLLECT_CLAUDE(now - 86400)
    said = [e.label for e in evs]
    assert said == ["wire the retrieval eval harness into CI please"]


def _thread(key, realm, touches):
    """Insert a thread with touches: [(source, ref_table, ref_id, evidence)]."""
    now = time.time()
    with db.tx() as c:
        tid = c.execute(
            "INSERT INTO threads(key,seq,uid,title,realm,first_ts,last_ts) "
            "VALUES(?,1,?,?,?,?,?)", (key, key, key, realm, now - 3600, now)).lastrowid
        for src, table, ref, ev in touches:
            c.execute("INSERT INTO thread_touches(thread_id,ts,source,ref_table,ref_id,evidence)"
                      " VALUES(?,?,?,?,?,?)", (tid, now - 60, src, table, ref, ev))


def _act(title, cat="terminal", app="Terminal"):
    now = time.time()
    return db.add_activity(app, "wt.exe", title, cat, "unknown", now - 300, now, False, False)


def test_claude_code_terminal_inherits_its_sessions_project_realm():
    a = _act("✳ You have till morning")
    b = _act("◐ You have till morning")
    c = _act("✳ Some other session")
    _thread("atlas", "work", [("claude", "claude", 0, "session: You have till morning")])
    r = threads.infer_realms()
    realms = {row["id"]: row["realm"] for row in db.q("SELECT id, realm FROM activity")}
    assert realms[a] == realms[b] == "work"
    assert realms[c] == "unknown", "no matching session, no guess"
    assert r["labelled"] == 2


def test_window_attached_by_name_inherits_the_thread_realm():
    a = _act("portfolio - Visual Studio Code", cat="code", app="VS Code")
    _thread("portfolio", "personal", [("window", "activity", a, "VS Code: portfolio")])
    threads.infer_realms()
    assert db.q1("SELECT realm FROM activity WHERE id=?", (a,))["realm"] == "personal"


def test_a_session_title_seen_in_both_realms_is_left_alone():
    a = _act("✳ Fix the build")
    _thread("atlas", "work", [("claude", "claude", 0, "session: Fix the build")])
    _thread("portfolio", "personal", [("claude", "claude", 0, "session: Fix the build")])
    threads.infer_realms()
    assert db.q1("SELECT realm FROM activity WHERE id=?", (a,))["realm"] == "unknown"


def test_labelled_sessions_are_never_overwritten():
    now = time.time()
    a = db.add_activity("Terminal", "wt.exe", "✳ You have till morning", "terminal",
                        "personal", now - 300, now, False, False)
    _thread("atlas", "work", [("claude", "claude", 0, "session: You have till morning")])
    threads.infer_realms()
    assert db.q1("SELECT realm FROM activity WHERE id=?", (a,))["realm"] == "personal"
