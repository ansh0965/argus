import time

from argus import db
from argus.automations import builtin


def _age(table, days, **cols):
    ts = time.time() - days * 86400
    with db.tx() as c:
        if table == "runs":
            c.execute("INSERT INTO runs(ts,name,ok,ms) VALUES(?,?,1,1)", (ts, "x"))
        elif table == "alerts":
            c.execute("INSERT INTO alerts(ts,day,rule,severity,title) VALUES(?,?,?,?,?)",
                      (ts, "d", "r", "info", "t"))
        elif table == "gmail":
            c.execute("INSERT INTO gmail(id,ts,unread) VALUES(?,?,?)",
                      (cols["id"], ts, cols.get("unread", 0)))


def test_maintenance_prunes_runs_alerts_and_old_mail():
    _age("runs", 40)
    _age("runs", 1)
    _age("alerts", 120)
    _age("alerts", 5)
    _age("gmail", 90, id="old-read")
    _age("gmail", 90, id="old-unread", unread=1)
    _age("gmail", 3, id="new")
    db.set_state("llm_calls:2020-01-01", 3)
    out = builtin.maintenance()
    assert db.q1("SELECT COUNT(*) n FROM runs")["n"] == 1
    assert db.q1("SELECT COUNT(*) n FROM alerts")["n"] == 1
    assert {r["id"] for r in db.q("SELECT id FROM gmail")} == {"old-unread", "new"}
    assert db.get_state("llm_calls:2020-01-01") is None
    assert "runs=1" in out


def test_heartbeat_ignores_disabled_sensors(monkeypatch):
    from argus import daemon
    from argus.actions import notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a[0]))
    monkeypatch.setattr(daemon, "SENSOR_HEALTH",
                        {"window": {"alive": True}, "screenshot": {"alive": False,
                                                                   "reason": "disabled"}})
    assert builtin.heartbeat().startswith("ok, 1/1")
    assert sent == []


def test_heartbeat_reports_a_dead_sensor(monkeypatch):
    import threading
    from argus import daemon
    from argus.actions import notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a[0]))
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    import time as _t
    # restart budget already spent this hour, so it stays down and is reported
    monkeypatch.setattr(daemon, "SENSOR_HEALTH", {"files": {
        "alive": True, "thread": dead, "restarts": [_t.time()] * daemon.MAX_RESTARTS_PER_HOUR}})
    assert builtin.heartbeat().startswith("DEGRADED")
    assert sent == ["An ARGUS sensor stopped"]


def test_health_check_keeps_a_summary_for_the_tray():
    out = builtin.health_check()
    h = db.get_state("health_summary")
    assert h and "worst" in h and isinstance(h["fails"], list)
    assert out.startswith(("ok", "0 failing")) or "failing" in out


def test_a_dead_sensor_is_revived_but_not_spun(monkeypatch):
    import threading
    from argus import daemon
    started = []

    def fake_sensor():
        started.append(1)            # "crashes" immediately

    monkeypatch.setattr(daemon, "SENSORS", [("files", "sensors.files.enabled", fake_sensor)])
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    health = {"files": {"alive": True, "thread": dead}}
    monkeypatch.setattr(daemon, "SENSOR_HEALTH", health)
    now = 1_000_000.0
    for i in range(5):
        health["files"]["thread"].join(timeout=2)
        daemon.revive_sensors(now + i)
    assert len(started) == daemon.MAX_RESTARTS_PER_HOUR
    assert "left down" in health["files"]["reason"]
    # an hour later it gets another chance
    health["files"]["thread"].join(timeout=2)
    assert daemon.revive_sensors(now + 3700) == ["files"]


def test_core_threads_are_revived(monkeypatch):
    from argus import daemon
    runs = []
    monkeypatch.setattr(daemon, "CORE", {})
    daemon.start_core("net", lambda: runs.append(1))
    daemon.CORE["net"]["thread"].join(timeout=2)
    assert daemon.revive_core() == ["net"]
    daemon.CORE["net"]["thread"].join(timeout=2)
    assert len(runs) == 2


def test_chatty_sources_are_pruned_sooner():
    old = time.time() - 45 * 86400
    db.log_event("net", "conn", "chrome.exe", "1.1.1.1", ts=old)
    db.log_event("file", "modified", "a.py", "C:/x/a.py", ts=old)
    db.log_event("process", "start", "x", "x.exe", ts=old)
    out = builtin.maintenance()
    left = {r["source"] for r in db.q("SELECT source FROM events")}
    assert "net" not in left, out
    assert {"file", "process"} <= left, "file keeps 400 days, process 90"


def test_backup_is_a_consistent_readable_copy_and_rotates(tmp_path, write_config):
    import sqlite3

    from argus import paths
    write_config({"backup": {"encrypt": False}})     # plain copies; encryption: test_vault
    db.add_run("marker", True, 1, "present in the backup")
    out = builtin.backup(keep=2)
    files = sorted(paths.BACKUPS.glob("argus-*.db"))
    assert len(files) == 1 and "backed up" in out
    copy = sqlite3.connect(str(files[0]))
    assert copy.execute("SELECT summary FROM runs WHERE name='marker'").fetchone()[0] \
        == "present in the backup"
    copy.close()
    # rotation keeps only the newest `keep`
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (paths.BACKUPS / f"argus-{day}.db").write_bytes(b"old")
    builtin.backup(keep=2)
    assert len(list(paths.BACKUPS.glob("argus-*.db"))) == 2


def test_delete_everything_removes_backups():
    from fastapi.testclient import TestClient

    from argus import paths
    from argus.web import server
    builtin.backup(keep=3)
    assert list(paths.BACKUPS.glob("argus-*.db*"))
    c = TestClient(server.app, base_url="http://127.0.0.1:7842")
    assert c.post("/api/privacy/purge", json={"scope": "all"}).status_code == 200
    assert not list(paths.BACKUPS.glob("argus-*.db*"))
