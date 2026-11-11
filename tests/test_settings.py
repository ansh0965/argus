"""Settings from the dashboard: layered over config.yaml, validated, never rewriting it."""
import pytest
from fastapi.testclient import TestClient

from argus import config
from argus.paths import CONFIG_PATH


@pytest.fixture(autouse=True)
def clean():
    config.SETTINGS_PATH.unlink(missing_ok=True)
    config.load(force=True)
    yield
    config.SETTINGS_PATH.unlink(missing_ok=True)
    config.load(force=True)


def test_a_setting_wins_over_the_defaults_and_config_yaml(write_config):
    write_config({"board": {"plan_at": "10:00"}})
    before = CONFIG_PATH.read_text(encoding="utf-8")
    config.save_settings({"board.plan_at": "9:05", "automations.break_reminder.enabled": False})
    assert config.get("board.plan_at") == "09:05"
    assert config.get("automations.break_reminder.enabled") is False
    assert CONFIG_PATH.read_text(encoding="utf-8") == before, "config.yaml untouched"


@pytest.mark.parametrize("key, value", [
    ("board.plan_at", "25:00"), ("board.plan_at", "noon"),
    ("automations.break_reminder.enabled", "yes"),
    ("identity.work_hours.days", [7]), ("identity.work_hours.days", []),
    ("identity.user_name", ""),
])
def test_bad_values_are_refused(key, value):
    with pytest.raises(ValueError):
        config.save_settings({key: value})


def test_only_listed_keys_can_be_set():
    with pytest.raises(KeyError):
        config.save_settings({"privacy.keylogging": True})
    with pytest.raises(KeyError):
        config.save_settings({"daemon.web_port": 80})


def test_settings_api():
    from argus.web import server
    cl = TestClient(server.app, base_url="http://127.0.0.1:7842", headers={"Origin": "http://127.0.0.1:7842"})
    assert "board.plan_at" in cl.get("/api/settings").json()
    r = cl.post("/api/settings", json={"identity.user_name": "Alex"})
    assert r.status_code == 200 and r.json()["identity.user_name"] == "Alex"
    assert cl.post("/api/settings", json={"board.plan_at": "99:99"}).status_code == 400
    assert cl.post("/api/settings", json={"daemon.web_port": 1}).status_code == 400
