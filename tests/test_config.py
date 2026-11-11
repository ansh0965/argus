import os
import time

from argus import config, paths


def _touch_later():
    t = time.time() + 2
    os.utime(paths.CONFIG_PATH, (t, t))


def test_defaults_fill_gaps(write_config):
    cfg = write_config({"notify": {"voice": True}})
    assert cfg["notify"]["voice"] is True
    assert cfg["notify"]["quiet_hours"]["start"] == "01:30"


def test_keylogging_cannot_be_enabled(write_config):
    assert write_config({"privacy": {"keylogging": True}})["privacy"]["keylogging"] is False


def test_broken_yaml_keeps_the_last_good_settings(write_config):
    write_config({"notify": {"quiet_hours": {"start": "22:00", "end": "07:00"}}})
    paths.CONFIG_PATH.write_text("notify:\n  quiet_hours: {start: '22:00'\n\tend: x", encoding="utf-8")
    _touch_later()
    cfg = config.load(force=True)
    assert cfg["notify"]["quiet_hours"]["start"] == "22:00", "must not revert to defaults"
    assert config.last_error()
    assert any("not being applied" in p for p in config.problems())


def test_fixing_the_yaml_clears_the_error(write_config):
    paths.CONFIG_PATH.write_text("- just\n- a list\n", encoding="utf-8")
    _touch_later()
    config.load(force=True)
    assert "mapping" in config.last_error()
    write_config({"notify": {"voice": False}})
    assert config.last_error() == ""


def test_problems_catch_common_mistakes(write_config):
    write_config({
        "notfy": {},
        "daemon": {"web_port": "7842"},
        "notify": {"quiet_hours": {"start": "noon", "end": "08:00"}, "min_severity": "loud"},
        "identity": {"work_hours": {"days": ["mon"]}},
        "sensors": {"window": {"poll_seconds": 0}},
        "brain": {"providers": ["claude_cli", "gpt"]},
    })
    text = "\n".join(config.problems())
    for needle in ("unknown top-level key 'notfy'", "web_port", "quiet_hours.start",
                   "min_severity", "work_hours.days", "sensors.window.poll_seconds",
                   "'gpt'"):
        assert needle in text, needle


def test_clean_config_has_no_problems():
    assert config.problems() == []


def test_unquoted_yaml_times_still_work():
    import yaml

    from argus.util import parse_hhmm
    raw = yaml.safe_load("a: 20:00\nb: 08:30\nc: 23:59")
    assert parse_hhmm(raw["a"]) == (20, 0)
    assert parse_hhmm(raw["b"]) == (8, 30)
    assert parse_hhmm(raw["c"]) == (23, 59)
    assert parse_hhmm("99:99") == (23, 59)


def test_unquoted_time_is_not_a_problem(write_config):
    import yaml
    write_config(yaml.safe_load("digests:\n  weekly_retro:\n    at: 20:00\n    day: 6\n"))
    assert config.problems() == []


def test_config_file_is_not_statted_on_every_get(monkeypatch):
    config.load(force=True)
    calls = []
    real = type(paths.CONFIG_PATH).stat
    monkeypatch.setattr(type(paths.CONFIG_PATH), "stat",
                        lambda self, *a, **k: calls.append(1) or real(self, *a, **k))
    for _ in range(1000):
        config.get("notify.voice")
    assert len(calls) <= 1


def test_edits_are_still_picked_up(write_config, monkeypatch):
    write_config({"notify": {"voice": False}})
    import yaml
    paths.CONFIG_PATH.write_text(yaml.safe_dump({"notify": {"voice": True}}), encoding="utf-8")
    _touch_later()
    monkeypatch.setattr(config, "_checked", 0.0)      # as if CHECK_EVERY had passed
    assert config.get("notify.voice") is True
