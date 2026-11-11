"""Two-way task sync: a local completion must survive the next pull."""
import pytest

from argus import db
from argus.gsuite import gtasks


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeTasks:
    def __init__(self):
        self.remote = {"g1": {"id": "g1", "title": "Ship report", "status": "needsAction"}}
        self.patched = []

    # tasklists().list()
    def tasklists(self):
        return self

    def tasks(self):
        return self

    def list(self, **kw):
        if "tasklist" not in kw:
            return _Req(lambda: {"items": [{"id": "L"}]})
        return _Req(lambda: {"items": list(self.remote.values())})

    def patch(self, tasklist, task, body):
        def _do():
            self.patched.append(task)
            self.remote[task].update(body)
            return self.remote[task]
        return _Req(_do)

    def insert(self, tasklist, body):
        def _do():
            tid = f"g{len(self.remote) + 1}"
            self.remote[tid] = {"id": tid, "status": "needsAction", **body}
            return self.remote[tid]
        return _Req(_do)


@pytest.fixture
def svc(monkeypatch):
    s = FakeTasks()
    monkeypatch.setattr(gtasks, "_svc", lambda label, interactive=False: s)
    monkeypatch.setattr(gtasks, "push_account", lambda: "personal")
    return s


def _row(ext):
    return db.q1("SELECT * FROM tasks WHERE ext_id=?", (ext,))


def test_local_completion_is_pushed_and_survives_the_pull(svc):
    gtasks.sync("personal")
    tid = _row("g1")["id"]
    assert gtasks.complete_local(tid)
    out = gtasks.sync("personal")
    assert "completed 1" in out
    assert svc.patched == ["g1"] and svc.remote["g1"]["status"] == "completed"
    assert _row("g1")["done"] == 1 and _row("g1")["pushed_done"] == 1


def test_pending_completion_is_not_undone_if_push_fails(svc, monkeypatch):
    gtasks.sync("personal")
    gtasks.complete_local(_row("g1")["id"])

    def boom(**kw):
        raise ConnectionError("offline")
    monkeypatch.setattr(svc, "patch", boom)
    gtasks.sync("personal")
    assert _row("g1")["done"] == 1, "a failed push must not let the pull revert it"
    assert _row("g1")["pushed_done"] == 0, "and it must be retried next time"


def test_reopened_in_google_is_respected(svc):
    gtasks.sync("personal")
    gtasks.complete_local(_row("g1")["id"])
    gtasks.sync("personal")
    svc.remote["g1"]["status"] = "needsAction"      # you un-ticked it on your phone
    gtasks.sync("personal")
    assert _row("g1")["done"] == 0


def test_local_task_is_pushed_to_the_push_account_only(svc):
    gtasks.add_local("Call the bank")
    assert "pushed 0" in gtasks.sync("work")
    assert "pushed 1" in gtasks.sync("personal")
    assert db.q1("SELECT account FROM tasks WHERE title='Call the bank'")["account"] == "personal"
