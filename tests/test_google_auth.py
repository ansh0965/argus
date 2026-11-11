"""A dead refresh token must be recognised, remembered, and not hammered."""
import pytest

from argus import db
from argus.gsuite import auth


class FakeCreds:
    valid = False
    expired = True
    refresh_token = "r"

    def __init__(self, error=None):
        self.error = error
        self.refreshes = 0

    def refresh(self, _req):
        self.refreshes += 1
        if self.error:
            raise self.error
        self.valid = True

    def to_json(self):
        return "{}"


@pytest.fixture
def token(monkeypatch):
    auth.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    p = auth.token_path("work")
    p.write_text("{}", encoding="utf-8")
    holder = {}
    monkeypatch.setattr(auth, "_load", lambda label: holder["c"])
    monkeypatch.setattr(auth, "harden_secrets", lambda: None)
    yield holder
    p.unlink(missing_ok=True)
    db.set_state("google_token_state:work", {})


def test_invalid_grant_is_expired_and_not_retried(token):
    c = FakeCreds(Exception("invalid_grant: Token has been expired or revoked."))
    token["c"] = c
    assert auth.creds("work") is None
    assert auth.token_state("work")["state"] == "expired"
    assert "Testing mode" in auth.token_state("work")["hint"]
    for _ in range(5):
        assert auth.creds("work") is None
    assert c.refreshes == 1, "a known-dead token must not be refreshed again"


def test_new_login_file_clears_the_dead_mark(token):
    token["c"] = FakeCreds(Exception("invalid_grant"))
    auth.creds("work")
    assert auth.token_state("work")["state"] == "expired"
    import os
    import time
    p = auth.token_path("work")
    later = time.time() + 5
    os.utime(p, (later, later))            # what a fresh login's write does
    assert auth.token_state("work") == {}
    token["c"] = FakeCreds()
    assert auth.creds("work") is not None


def test_network_error_is_transient_and_retried(token):
    c = FakeCreds(Exception("HTTPSConnectionPool: Max retries exceeded"))
    token["c"] = c
    assert auth.creds("work") is None
    assert auth.token_state("work")["state"] == "error"
    auth.creds("work")
    assert c.refreshes == 2


def test_accounts_reports_state(token, monkeypatch):
    monkeypatch.setattr(auth, "email_of", lambda label: "me@example.com")
    token["c"] = FakeCreds(Exception("invalid_grant"))
    [a] = auth.accounts()
    assert a["valid"] is False and a["state"] == "expired" and a["hint"]


def test_google_sync_does_not_cry_wolf_on_transient_errors(token, monkeypatch):
    from argus import daemon
    from argus.actions import notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a[0]))
    monkeypatch.setattr(auth, "email_of", lambda label: "me@example.com")
    token["c"] = FakeCreds(Exception("timed out"))
    out = daemon.google_sync()
    assert "unreachable" in out and sent == []
    token["c"] = FakeCreds(Exception("invalid_grant"))
    db.set_state("google_token_state:work", {})
    out = daemon.google_sync()
    assert "SIGN-IN NEEDED" in out and len(sent) == 1


def test_abandoned_consent_times_out_cleanly(token, monkeypatch):
    token["c"] = FakeCreds(Exception("invalid_grant"))
    monkeypatch.setattr(auth, "find_client_secret", lambda: auth.CLIENT_PATH)

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, *a, **k):
            return cls()

    import google_auth_oauthlib.flow as gflow
    monkeypatch.setattr(gflow, "InstalledAppFlow", Flow)

    def timed_out(flow, label):
        None.replace("http", "https")          # what the library does on timeout
    monkeypatch.setattr(auth, "_run_consent", timed_out)
    with pytest.raises(RuntimeError, match="5 minutes"):
        auth.creds("work", interactive=True)


def test_setup_help_recommends_publishing():
    assert "PUBLISH APP" in auth.help_text()
    assert "Testing mode is correct here" not in auth.help_text()
