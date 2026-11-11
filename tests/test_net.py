import time

from argus import net


def _reset(monkeypatch):
    monkeypatch.setattr(net, "_online", None)
    monkeypatch.setattr(net, "_since", 0.0)
    monkeypatch.setattr(net, "_last_outage", 0.0)
    monkeypatch.setattr(net, "_fail_streak", 0)
    monkeypatch.setattr(net, "_ok_streak", 0)


def test_outage_length_survives_the_reconnect(monkeypatch):
    _reset(monkeypatch)
    net._set(True)                       # first observation
    net._set(False)
    monkeypatch.setattr(net, "_since", time.time() - 40 * 60)   # down 40 minutes
    assert net._set(True) is True        # rising edge
    st = net.state()
    assert st["for_seconds"] < 5
    assert 39 * 60 < st["last_outage_seconds"] < 41 * 60


def test_reconnect_notice_fires_after_a_long_outage(monkeypatch):
    from argus import daemon
    from argus.actions import notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, body="", **k: sent.append((title, body)))
    monkeypatch.setattr(daemon, "google_connected", lambda: True)
    monkeypatch.setattr(daemon.gmail, "unread_counts", lambda: {"needs_reply": 2})
    monkeypatch.setattr(daemon.gcal, "next_event", lambda: None)
    monkeypatch.setattr(net, "state", lambda: {"last_outage_seconds": 40 * 60,
                                               "for_seconds": 1})
    out = daemon._reconnect_notice()
    assert "2 mail" in out and sent and sent[0][0] == "Back online"
    monkeypatch.setattr(net, "state", lambda: {"last_outage_seconds": 60, "for_seconds": 1})
    assert daemon._reconnect_notice() == "brief blip, staying quiet"


def test_hysteresis_needs_two_failures(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(net, "probe", lambda: True)
    monkeypatch.setattr(net, "behind_captive_portal", lambda: False)
    monkeypatch.setattr(net, "_fire_reconnect", lambda: None)
    net.poll_once()
    assert net.is_online()
    monkeypatch.setattr(net, "probe", lambda: False)
    net.poll_once()
    assert net.is_online(), "one dropped probe is not an outage"
    net.poll_once()
    assert not net.is_online()
