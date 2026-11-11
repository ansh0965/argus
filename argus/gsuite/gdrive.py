"""Drive: which documents moved recently. Metadata only, no file contents."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from .. import db
from ..util import log
from . import auth

lg = log("google.drive")

TYPE_NAMES = {
    "application/vnd.google-apps.document": "Doc",
    "application/vnd.google-apps.spreadsheet": "Sheet",
    "application/vnd.google-apps.presentation": "Slides",
    "application/vnd.google-apps.form": "Form",
    "application/vnd.google-apps.folder": "Folder",
    "application/pdf": "PDF",
}


def _svc(label: str):
    return auth.service("drive", "v3", label)


def recent(label: str, days: int = 7, limit: int = 40) -> list[dict]:
    svc = _svc(label)
    if not svc:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        res = svc.files().list(
            q=f"modifiedTime > '{since}' and trashed = false",
            orderBy="modifiedTime desc", pageSize=limit,
            fields="files(id,name,mimeType,modifiedTime,webViewLink,owners(displayName),"
                   "lastModifyingUser(displayName),shared)",
        ).execute()
    except Exception as e:
        lg.warning("drive list failed: %s", str(e)[:200])
        return []

    out = []
    for f in res.get("files", []):
        try:
            mt = datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00")).timestamp()
        except Exception:
            mt = 0.0
        out.append({
            "id": f["id"],
            "name": f.get("name", ""),
            "kind": TYPE_NAMES.get(f.get("mimeType", ""), f.get("mimeType", "").split(".")[-1]),
            "modified": mt,
            "link": f.get("webViewLink", ""),
            "by": (f.get("lastModifyingUser") or {}).get("displayName", ""),
            "shared": bool(f.get("shared")),
        })
    return out


def sync(label: str) -> str:
    files = recent(label)
    if not files:
        return "nothing or not connected"
    seen = set(db.get_state("drive_seen", []) or [])
    rows, fresh = [], []
    for f in files:
        key = f"{label}:{f['id']}:{int(f['modified'])}"
        if key in seen:
            continue
        fresh.append(key)
        rows.append(("drive", "modified", f["name"], f["kind"], f["modified"],
                     {"link": f["link"], "by": f["by"], "shared": f["shared"],
                      "account": label}, f["modified"]))
    db.log_events(rows)
    db.set_state("drive_seen", (list(seen) + fresh)[-800:])
    return f"{len(rows)} changed"
