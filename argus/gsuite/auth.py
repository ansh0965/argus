"""Google OAuth for a desktop app, across several accounts.

You will typically have more than one Google identity - work, personal, college -
and they are worth different amounts to ARGUS. So tokens are stored one file per
account under data/secrets/google/<label>.json and every Google call takes a
label. Nothing is shared between accounts except the OAuth client itself.

You create ONE OAuth client (a two-minute job in the Google Cloud console that
cannot be automated - Google has no API for minting OAuth clients) and reuse it
for every account you add.

Per-account feature toggles live in config.yaml, so you can connect a work
account for calendar only and leave its mail alone:

    google:
      accounts:
        work:
          features: [calendar]
        personal:
          features: [calendar, mail, tasks, drive]

Tokens never leave this machine.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .. import config
from ..paths import SECRETS, harden_secrets
from ..util import log

lg = log("google.auth")

# The narrowest set that still supports the features. Two scopes were removed
# on 2026-09-11 because nothing used them and both were dangerous to hold:
#   spreadsheets    - full write access to every Sheet, including the timesheet
#   contacts.readonly - never read by any module
# Google offers no "create but not delete" variant of calendar/tasks/gmail.modify,
# so delete power is unavoidable in the token itself and is blocked in code
# instead - see gsuite/guard.py.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

ALL_FEATURES = ("calendar", "mail", "tasks", "drive")

TOKEN_DIR = SECRETS / "google"
CLIENT_PATH = SECRETS / "google_client.json"
LEGACY_TOKEN = SECRETS / "google_token.json"

SEARCH_DIRS = [
    SECRETS,
    Path("~/Downloads").expanduser(),
    Path("~/Desktop").expanduser(),
    Path(__file__).resolve().parent.parent.parent,
]

SETUP_HELP = """
Google is not connected yet. One-time setup, about two minutes. Do this in
whichever Google account you can freely create a Cloud project in - your
PERSONAL account is usually the right choice, because a company Workspace often
restricts project creation. The same client then works for every account you add.

  1. Open  https://console.cloud.google.com/projectcreate
     Create a project (or reuse one you already have).

  2. Enable the APIs you want ARGUS to use. Open each link with that
     project selected and press Enable:
       Gmail      https://console.cloud.google.com/apis/library/gmail.googleapis.com
       Calendar   https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
       Tasks      https://console.cloud.google.com/apis/library/tasks.googleapis.com
       Drive      https://console.cloud.google.com/apis/library/drive.googleapis.com
       People     https://console.cloud.google.com/apis/library/people.googleapis.com

  3. Configure the consent screen:
     https://console.cloud.google.com/auth/overview
       User type: External.  App name: ARGUS.  Your email in both contact fields.
       Then under Audience, press PUBLISH APP. In Testing mode Google expires
       every refresh token 7 days after sign-in, so every account silently
       stops syncing once a week. Publishing a personal app does not need
       Google's review; sign-in just shows an "unverified app" warning
       (Advanced -> Go to ARGUS). If you would rather stay in Testing, add
       EVERY account you want ARGUS to read as a Test user and expect to run
       `argus google relogin` weekly.

  4. Create the client:
     https://console.cloud.google.com/auth/clients
       Create client -> Application type: Desktop app -> name it ARGUS -> Create
       -> Download JSON.

  5. Save that file as:
       {client_path}
     (or just leave it in Downloads - ARGUS will find it)

  6. Connect each account, giving it a short label:
       argus google login --label personal
       argus google login --label work
       argus google login --label college

Note on work accounts: a Google Workspace admin can block third-party apps
outright. If sign-in fails with "access blocked" or "admin policy", that is your
company's setting, not a bug here - use the account for calendar only, or leave
it out.
""".strip()

_LABEL_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def help_text() -> str:
    text = SETUP_HELP.format(client_path=CLIENT_PATH)
    rejected = rejected_clients()
    if rejected:
        text += ("\n\nNote: these OAuth client files were found but not used, because "
                 "they are 'Web application' clients belonging to another project.\n"
                 "ARGUS needs an 'installed'/Desktop app client:\n  "
                 + "\n  ".join(rejected))
    return text


# --------------------------------------------------------------------------- client

def _client_kind(path: Path) -> tuple[str, dict] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for kind in ("installed", "web"):
        if kind in data and data[kind].get("client_id"):
            return kind, data[kind]
    return None


def find_client_secret() -> Path | None:
    """Locate a downloaded OAuth **desktop** client JSON.

    Web clients are deliberately not adopted: they belong to some other project,
    are pinned to a fixed redirect URI, and their consent screen will not carry
    the Gmail/Calendar scopes ARGUS needs. Silently borrowing one produces a
    confusing redirect_uri_mismatch instead of an honest "set this up".
    """
    if CLIENT_PATH.is_file():
        return CLIENT_PATH
    for d in SEARCH_DIRS:
        if not d.is_dir():
            continue
        try:
            candidates = sorted(d.glob("client_secret*.json"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            continue
        for c in candidates:
            kind = _client_kind(c)
            if kind and kind[0] == "installed":
                return c
    return None


def rejected_clients() -> list[str]:
    """Web-type client files we found but will not use - shown in setup help."""
    out = []
    for d in SEARCH_DIRS:
        if not d.is_dir():
            continue
        try:
            for c in d.glob("client_secret*.json"):
                kind = _client_kind(c)
                if kind and kind[0] == "web":
                    out.append(str(c))
        except Exception:
            continue
    return out


def adopt_client_secret(path: Path) -> Path:
    SECRETS.mkdir(parents=True, exist_ok=True)
    CLIENT_PATH.write_text(Path(path).read_text(encoding="utf-8"), encoding="utf-8")
    harden_secrets()
    return CLIENT_PATH


def is_configured() -> bool:
    return CLIENT_PATH.is_file() or find_client_secret() is not None


# --------------------------------------------------------------------------- accounts

def token_path(label: str) -> Path:
    return TOKEN_DIR / f"{label}.json"


def _migrate_legacy() -> None:
    """A single-account install becomes the 'default' labelled account."""
    if LEGACY_TOKEN.is_file():
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        target = token_path("default")
        if not target.exists():
            target.write_text(LEGACY_TOKEN.read_text(encoding="utf-8"), encoding="utf-8")
            lg.info("migrated legacy token to %s", target.name)
        LEGACY_TOKEN.unlink()


def labels() -> list[str]:
    _migrate_legacy()
    if not TOKEN_DIR.is_dir():
        return []
    return sorted(p.stem for p in TOKEN_DIR.glob("*.json"))


def is_connected(label: str | None = None) -> bool:
    if label:
        return token_path(label).is_file()
    return bool(labels())


def email_of(label: str) -> str:
    """Cached email for a label, read from the token file's id_token claims."""
    from .. import db
    cached = db.get_state(f"google_email:{label}", "")
    if cached:
        return cached
    who = whoami(label)
    if who:
        db.set_state(f"google_email:{label}", who)
    return who


def features(label: str) -> tuple[str, ...]:
    """Which Google surfaces ARGUS may touch for this account."""
    per = (config.get("google.accounts", {}) or {}).get(label, {}) or {}
    want = per.get("features")
    if want is None:
        want = config.get("google.default_features", list(ALL_FEATURES))
    return tuple(f for f in want if f in ALL_FEATURES)


def enabled(label: str, feature: str) -> bool:
    return feature in features(label)


def accounts() -> list[dict]:
    """Every connected account with its email, feature set and token health."""
    out = []
    for label in labels():
        ok = creds(label) is not None
        st = token_state(label)
        out.append({
            "label": label,
            "email": email_of(label),
            "features": list(features(label)),
            "valid": ok,
            # ok | expired (needs a fresh login) | error (transient, retry later)
            "state": "ok" if ok else st.get("state", "error"),
            "reason": "" if ok else st.get("reason", ""),
            "hint": "" if ok else st.get("hint", ""),
        })
    return out


def validate_label(label: str) -> str:
    label = (label or "").strip().lower()
    if not _LABEL_OK.match(label):
        raise ValueError(
            "label must be lowercase letters, digits, - or _ (max 32), e.g. work")
    return label


# --------------------------------------------------------------------------- creds

def _load(label: str):
    from google.oauth2.credentials import Credentials
    p = token_path(label)
    if not p.is_file():
        return None
    try:
        return Credentials.from_authorized_user_file(str(p), SCOPES)
    except Exception as e:
        lg.warning("token for %s unreadable: %s", label, e)
        return None


def _save(label: str, c) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    p = token_path(label)
    p.write_text(c.to_json(), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    harden_secrets()


# --------------------------------------------------------------------------- token health
#
# Two very different failures used to look identical ("SIGN-IN NEEDED"):
#
#   expired - Google answered invalid_grant. The refresh token is dead and no
#             amount of retrying revives it; only a fresh login does. Retrying
#             it every five minutes cost ~13s of network per sync and wrote a
#             warning each time, for days.
#   error   - DNS, TLS or timeout. Nothing is wrong with the account; try again
#             next round and do not tell the user to sign in.
#
# A dead token is remembered together with the token file's mtime, so the
# moment `argus google login --force` rewrites the file, the account is tried
# again without anyone clearing anything.

TESTING_MODE_HINT = (
    "Google expires refresh tokens after 7 days while an OAuth app's consent "
    "screen is in Testing mode. To stop this recurring: open "
    "https://console.cloud.google.com/auth/audience in the project that owns "
    "the ARGUS client, press 'Publish app', then log in once more. (Google will "
    "warn that the app is unverified - that is expected for a personal app; "
    "choose Advanced -> Go to ARGUS.)")


def _token_mtime(label: str) -> float:
    try:
        return token_path(label).stat().st_mtime
    except OSError:
        return 0.0


def token_state(label: str) -> dict:
    """What we last learned about this account's token. {} when nothing is known."""
    from .. import db
    st = db.get_state(f"google_token_state:{label}", {}) or {}
    if st.get("state") == "expired" and st.get("mtime") != _token_mtime(label):
        return {}          # the file was rewritten since - a new login, try again
    return st


def _set_token_state(label: str, state: str, reason: str = "") -> None:
    from .. import db
    if state == "ok":
        if db.get_state(f"google_token_state:{label}"):
            db.set_state(f"google_token_state:{label}", {})
        return
    hint = ""
    if state == "expired":
        login = float(db.get_state(f"google_login_ts:{label}", 0) or 0)
        age = (time.time() - login) / 86400 if login else None
        hint = TESTING_MODE_HINT
        if age is not None and 6 <= age <= 8.5:
            hint = f"It died {age:.1f} days after login - the signature of Testing mode. " + hint
    db.set_state(f"google_token_state:{label}", {
        "state": state, "reason": reason[:300], "hint": hint,
        "mtime": _token_mtime(label), "ts": time.time()})


def _is_invalid_grant(e: Exception) -> bool:
    s = str(e).lower()
    return "invalid_grant" in s or "token has been expired or revoked" in s


def creds(label: str, interactive: bool = False):
    """Valid credentials for one account, refreshing or consenting as needed."""
    from google.auth.transport.requests import Request
    c = _load(label)

    if c and c.valid:
        return c
    if c and not interactive and token_state(label).get("state") == "expired":
        return None                      # known dead: do not hammer the endpoint
    if c and c.expired and c.refresh_token:
        try:
            c.refresh(Request())
            _save(label, c)
            _set_token_state(label, "ok")
            return c
        except Exception as e:
            if _is_invalid_grant(e):
                if token_state(label).get("state") != "expired":
                    lg.warning("google token for %s is expired or revoked - "
                               "a fresh login is needed: %s", label, str(e)[:160])
                _set_token_state(label, "expired", str(e))
            else:
                lg.warning("token refresh for %s failed (transient): %s",
                           label, str(e)[:160])
                _set_token_state(label, "error", str(e))
            c = None

    if not interactive:
        return None

    src = find_client_secret()
    if not src:
        raise RuntimeError(help_text())
    if src != CLIENT_PATH:
        adopt_client_secret(src)

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_PATH), SCOPES)
    try:
        c = _run_consent(flow, label)
    except AttributeError:
        # google_auth_oauthlib's way of saying the local server timed out
        # with no redirect: 'NoneType' object has no attribute 'replace'.
        raise RuntimeError("sign-in was not completed within 5 minutes") from None
    if c is None or not getattr(c, "token", None):
        raise RuntimeError("sign-in was not completed within 5 minutes")
    _save(label, c)
    from .. import db
    db.set_state(f"google_login_ts:{label}", time.time())
    _set_token_state(label, "ok")
    return c


def _run_consent(flow, label: str):
    return flow.run_local_server(
        port=0, open_browser=True, prompt="consent",
        authorization_prompt_message=(
            f"Opening your browser to authorise ARGUS for '{label}'.\n"
            "Sign in with the Google account you want to use for this label.\n"
            "If it does not open, visit:\n{url}"),
        success_message="ARGUS is connected. You can close this tab.",
        # An abandoned consent tab must not park a thread (or the CLI) forever.
        timeout_seconds=300,
    )


def service(api: str, version: str, label: str, interactive: bool = False):
    """Build a Google API client for one account, or None when unavailable."""
    c = creds(label, interactive=interactive)
    if not c:
        return None
    from googleapiclient.discovery import build
    from .guard import protect
    svc = build(api, version, credentials=c, cache_discovery=False)
    return protect(svc, f"{api}:{label}")


def whoami(label: str) -> str:
    try:
        svc = service("oauth2", "v2", label)
        if not svc:
            return ""
        return svc.userinfo().get().execute().get("email", "")
    except Exception:
        return ""


def disconnect(label: str) -> bool:
    from .. import db
    p = token_path(label)
    if p.is_file():
        p.unlink()
        db.set_state(f"google_email:{label}", "")
        db.set_state(f"google_token_state:{label}", {})
        return True
    return False
