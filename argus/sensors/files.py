"""Filesystem sensor. Watches your real working directories via watchdog.

Debounced per path so an editor's save-storm becomes one event, and heavily
filtered so build output and node_modules never reach the database.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .. import config, db, pause, privacy
from ..util import expand, log

lg = log("sensor.files")

SKIP_SUFFIX = {
    ".tmp", ".temp", ".swp", ".swo", ".lock", ".log", ".pyc", ".pyo", ".part",
    ".crdownload", ".ldb", ".db-wal", ".db-shm", ".~lock", ".bak", ".orig",
    ".tsbuildinfo",
}
INTERESTING = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".cs", ".go",
    ".rs", ".rb", ".php", ".html", ".css", ".scss", ".json", ".yaml", ".yml",
    ".md", ".txt", ".sql", ".sh", ".ps1", ".ipynb", ".docx", ".xlsx", ".pptx",
    ".pdf", ".csv", ".png", ".jpg", ".jpeg", ".svg", ".zip", ".env", ".toml",
}


# Directories that tools write and people do not. Always skipped, on top of
# watch_dir_ignores: config.yaml keeps its own copy of the ignore list, so
# additions to the default never reached an existing install - and one
# `next build` into out/ was 13,000 "file touches" in a day, which made a
# seven-minute project the top thread.
GENERATED_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".cache", "site-packages", "$recycle.bin", ".pytest_cache",
    "out", ".history", ".turbo", ".svelte-kit", ".nuxt", ".output",
    ".parcel-cache", ".vercel", ".angular", ".docusaurus", "storybook-static",
    ".expo", "coverage", "htmlcov", ".mypy_cache", ".ruff_cache", ".tox",
    ".gradle",
}
# Editors' atomic-save temp files ("App.tsx.tmp.5536.7d6dff2f"), backups
# ("notes.md~"), emacs locks, vim's write test, Vite's bundled config
# ("vite.config.ts.timestamp-1790193987049-c9ee.mjs") and `sed -i` scratch
# files ("sedJGqNNT"), all of which reached the briefings' "Files touched".
_TEMP_NAME = re.compile(
    r"\.tmp\.|~$|^\.#|^4913$|\.timestamp-\d{10,}|^sed[a-z0-9]{6}$", re.I)


def generated(path: str) -> bool:
    """True for build output, caches, temp files and configured ignores."""
    low = path.lower().replace("/", "\\")
    for frag in config.get("watch_dir_ignores", []) or []:
        frag = str(frag).lower()
        if f"\\{frag}\\" in low or low.endswith(f"\\{frag}"):
            return True
    parts = [s for s in low.split("\\") if s]
    if not parts:
        return False
    name = parts[-1]
    if any(d in GENERATED_DIRS for d in parts[:-1]):
        return True
    if Path(name).suffix in SKIP_SUFFIX:
        return True
    if name.startswith("~$") or name.startswith("."):
        return True
    return bool(_TEMP_NAME.search(name))


# A directory written faster than anyone types is a tool at work (a build, a
# formatter, a checkout, a copied cache). Past this many files in the window
# it goes quiet for BURST_MUTE seconds, extended while the burst lasts.
BURST_FILES = 25
BURST_WINDOW = 10.0
BURST_MUTE = 120.0


class _Handler(FileSystemEventHandler):
    def __init__(self) -> None:
        self.seen: dict[str, float] = {}
        self.bursts: dict[str, tuple[float, int, float]] = {}
        self.lock = threading.Lock()

    def _ignored(self, path: str) -> bool:
        return generated(path)

    def _bursting(self, parent: str, now: float) -> bool:
        start, n, muted = self.bursts.get(parent, (now, 0, 0.0))
        if now - start > BURST_WINDOW:
            start, n = now, 0
        n += 1
        if n > BURST_FILES:
            muted = now + BURST_MUTE
        self.bursts[parent] = (start, n, muted)
        if len(self.bursts) > 2000:  # bound memory
            self.bursts = {k: v for k, v in self.bursts.items()
                           if now - v[0] < BURST_WINDOW or now < v[2]}
        return now < muted

    def _emit(self, kind: str, path: str, extra: str = "") -> None:
        if self._ignored(path) or pause.is_paused() or privacy.private_project_in(path):
            return
        debounce = float(config.get("sensors.files.debounce_seconds", 20))
        now = time.time()
        key = f"{kind}:{path}"
        with self.lock:
            if now - self.seen.get(key, 0) < debounce:
                return
            self.seen[key] = now
            if len(self.seen) > 4000:  # bound memory
                cutoff = now - 3600
                self.seen = {k: v for k, v in self.seen.items() if v > cutoff}
            if self._bursting(str(Path(path).parent).lower(), now):
                return

        p = Path(path)
        try:
            size = p.stat().st_size if p.exists() else 0
        except Exception:
            size = 0
        db.log_event("file", kind, p.name, privacy.clean_path(path), value=size,
                     meta={"dir": privacy.clean_path(str(p.parent)),
                           "ext": p.suffix.lower(), "extra": extra} if extra else
                          {"dir": privacy.clean_path(str(p.parent)), "ext": p.suffix.lower()})

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("created", str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("modified", str(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("deleted", str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("moved", str(event.dest_path),
                       extra=privacy.clean_path(str(event.src_path)))


def prune_generated(batch: int = 500) -> int:
    """Delete file events for paths the sensor now skips (build output, caches,
    temp files) that were recorded before it did. Read paths already ignore
    them; this keeps them out of search, export and a 400-day retention."""
    ids = [r["id"] for r in db.q("SELECT id, detail FROM events WHERE source='file'")
           if generated(r["detail"] or "")]
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        with db.tx() as c:
            c.execute(f"DELETE FROM events WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return len(ids)


def recent_files(hours: float = 24, limit: int = 40, only_interesting: bool = True) -> list[dict]:
    since = time.time() - hours * 3600
    rows = db.q(
        "SELECT subject, detail, kind, ts, meta FROM events "
        "WHERE source='file' AND ts > ? ORDER BY ts DESC LIMIT 3000", (since,))
    out, seen = [], set()
    for r in rows:
        d = dict(r)
        ext = Path(d["subject"] or "").suffix.lower()
        if only_interesting and ext not in INTERESTING:
            continue
        if generated(d["detail"] or ""):    # recorded before the filter existed
            continue
        if d["detail"] in seen:
            continue
        seen.add(d["detail"])
        out.append(d)
        if len(out) >= limit:
            break
    return out


def run(stop) -> None:
    obs = Observer()
    handler = _Handler()
    watched = 0
    for d in config.get("watch_dirs", []):
        p = expand(d)
        if not p.is_dir():
            continue
        try:
            obs.schedule(handler, str(p), recursive=True)
            watched += 1
        except Exception as e:
            lg.warning("cannot watch %s: %s", p, e)
    if not watched:
        lg.warning("no watchable directories; file sensor idle")
        stop.wait()
        return

    obs.start()
    lg.info("watching %d directories", watched)
    try:
        while not stop.is_set():
            stop.wait(5)
    finally:
        obs.stop()
        obs.join(timeout=10)
    lg.info("file sensor stopped")
