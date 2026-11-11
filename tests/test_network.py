import socket
import time

import pytest

from argus.sensors import network


@pytest.mark.parametrize("ip,local", [
    ("10.1.2.3", True), ("192.168.1.9", True), ("127.0.0.1", True),
    ("172.16.0.1", True), ("172.31.255.1", True), ("169.254.3.3", True),
    ("::1", True), ("fe80::1%12", True), ("::ffff:192.168.0.4", True),
    ("172.2.3.4", False), ("172.200.1.1", False), ("8.8.8.8", False),
    ("142.250.183.14", False), ("2606:4700::1111", False), ("garbage", False),
])
def test_is_local(ip, local):
    assert network.is_local(ip) is local


def test_rdns_never_touches_the_global_socket_timeout(monkeypatch):
    before = socket.getdefaulttimeout()
    monkeypatch.setattr(network, "_lookup", lambda ip: "host.example")
    network._rdns_cache.clear()
    assert network.resolve_many(["1.2.3.4"]) == {"1.2.3.4": "host.example"}
    assert socket.getdefaulttimeout() == before


def test_rdns_respects_its_deadline(monkeypatch):
    def slow(ip):
        time.sleep(2)
        return "late.example"
    monkeypatch.setattr(network, "_lookup", slow)
    network._rdns_cache.clear()
    t0 = time.time()
    out = network.resolve_many(["9.9.9.8"], deadline=0.3)
    assert time.time() - t0 < 1.0
    assert out == {"9.9.9.8": "9.9.9.8"}


def test_rdns_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(network, "_lookup", lambda ip: calls.append(ip) or "h")
    network._rdns_cache.clear()
    network.resolve_many(["5.5.5.5"])
    network.resolve_many(["5.5.5.5"])
    assert calls == ["5.5.5.5"]


def test_a_connection_is_logged_once_an_hour_not_every_poll(monkeypatch):
    from argus import db
    conns = [{"pid": 1, "proc": "chrome.exe", "ip": "142.250.1.1", "port": 443, "local": False},
             {"pid": 1, "proc": "chrome.exe", "ip": "142.250.1.1", "port": 443, "local": False}]
    monkeypatch.setattr(network, "connections", lambda: list(conns))
    monkeypatch.setattr(network, "resolve_many", lambda ips, **k: {ip: "google" for ip in ips})
    monkeypatch.setattr(network, "listening_ports", lambda: [])
    monkeypatch.setattr(network, "_logged", {})

    def n():
        return db.q1("SELECT COUNT(*) n FROM events WHERE source='net' AND kind='conn'")["n"]
    for i in range(30):                                  # an hour of 2-minute polls
        network.collect_once(now=1000.0 + i * 120)
    assert n() == 1
    conns.append({"pid": 2, "proc": "code.exe", "ip": "140.82.1.1", "port": 443, "local": False})
    network.collect_once(now=1000.0 + 30 * 120)          # a new pair, and the hour is up
    assert n() == 3
    row = db.q1("SELECT subject, detail, value FROM events WHERE source='net' ORDER BY id LIMIT 1")
    assert (row["subject"], row["detail"], row["value"]) == ("chrome.exe", "google", 2)
