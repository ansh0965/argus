"""A hard block on destructive Google API calls, across every connected account.

Two independent layers protect your accounts, because either one alone is weak:

  1. Scopes (auth.SCOPES) - ARGUS asks for the narrowest set that supports its
     features. But a scope like `tasks` is coarse: Google has no "create but do
     not delete" variant, so the token unavoidably carries delete power.

  2. This guard - every Google service object is wrapped so that destructive
     methods raise before a request is ever built. That holds even while an
     older, broader token is still valid.

Layer 2 is what makes "ARGUS cannot delete anything" true today rather than
after your next sign-in.

This is a denylist, not an allowlist, because the legitimate writes ARGUS makes
(insert an event, create a draft, clear the UNREAD label, add a task) are the
whole point of connecting. What it forbids is anything that destroys or sends.
"""
from __future__ import annotations

from .. import config
from ..util import log

lg = log("google.guard")


class DestructiveCallBlocked(PermissionError):
    """Raised instead of performing a delete/send against a Google account."""


# Method names that destroy data, empty trash, or put mail in front of a human.
# `send` is here deliberately: ARGUS drafts replies, it never sends them.
FORBIDDEN = {
    "delete", "batchDelete", "trash", "emptyTrash", "purge", "clear",
    "remove", "send", "stop", "abort", "cancel", "revoke",
}

# Labels that amount to deletion when added via messages.modify / batchModify.
DESTRUCTIVE_LABELS = {"TRASH", "SPAM"}


def _enabled() -> bool:
    # Default is protection ON. Someone must deliberately opt out in config.yaml.
    return not bool(config.get("google.allow_destructive", False))


def _check_modify_body(path: str, kwargs: dict) -> None:
    """modify/batchModify can delete by adding the TRASH label. Catch that too."""
    body = kwargs.get("body") or {}
    if not isinstance(body, dict):
        return
    add = body.get("addLabelIds") or []
    bad = DESTRUCTIVE_LABELS.intersection(add)
    if bad:
        raise DestructiveCallBlocked(
            f"{path}: refusing to add label(s) {sorted(bad)} - that trashes mail. "
            "Set google.allow_destructive: true in config.yaml to permit it.")


class GuardedResource:
    """Transparent proxy over a googleapiclient Resource."""

    __slots__ = ("_inner", "_path")

    def __init__(self, inner, path: str = "") -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str):
        inner = object.__getattribute__(self, "_inner")
        path = object.__getattribute__(self, "_path")
        full = f"{path}.{name}" if path else name

        if name in FORBIDDEN and _enabled():
            def _blocked(*_a, **_kw):
                lg.warning("blocked destructive call: %s", full)
                raise DestructiveCallBlocked(
                    f"ARGUS is not allowed to call {full}(). It may read, and it may "
                    "create events, drafts and tasks - it may never delete or send. "
                    "Set google.allow_destructive: true in config.yaml to lift this.")
            return _blocked

        attr = getattr(inner, name)
        if not callable(attr):
            return attr

        def _wrapped(*args, **kwargs):
            if name in ("modify", "batchModify") and _enabled():
                _check_modify_body(full, kwargs)
            result = attr(*args, **kwargs)
            return _wrap(result, full)

        return _wrapped

    def __repr__(self) -> str:
        return f"<Guarded {object.__getattribute__(self, '_inner')!r}>"


def _wrap(obj, path: str):
    """Wrap sub-resources; leave HttpRequest and plain values alone."""
    try:
        from googleapiclient.discovery import Resource
    except Exception:
        return obj
    if isinstance(obj, Resource):
        return GuardedResource(obj, path)
    return obj


def protect(service, label: str = ""):
    """Wrap a built Google service. Returns it unchanged if protection is off."""
    if service is None:
        return None
    if not _enabled():
        lg.warning("destructive Google calls are ENABLED for %s (config opt-out)",
                   label or "all accounts")
        return service
    return GuardedResource(service, label or "google")


def status() -> dict:
    return {
        "protected": _enabled(),
        "forbidden_methods": sorted(FORBIDDEN),
        "destructive_labels": sorted(DESTRUCTIVE_LABELS),
        "note": ("ARGUS may read, and may create calendar events, Gmail drafts and "
                 "tasks. It may not delete anything or send mail, on any account."),
    }
