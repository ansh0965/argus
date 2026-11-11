"""Work Threads: what you were doing, and where you left off.

A peer of `insights.py`, not an extension of it. `insights` answers "how much did
I work"; this answers "what was I working on". Both read the same raw tables and
neither writes to them.

The whole pipeline is deterministic - no LLM anywhere. Two consequences that are
features, not compromises:

  - `resume` produces byte-identical output on two consecutive runs, so it can be
    diffed and regression-tested.
  - it costs nothing and works with the network down.

That is possible because the evidence is already human-readable. Claude Code
writes `{"type":"ai-title","aiTitle":"atlas landing page SEO audit"}` into
every session log, VS Code puts the workspace in the window title, and git
branches are already feature names. There is nothing for a model to summarise
that the machine has not already written down.

Writes only to `threads`, `thread_touches` and `feedback`. Never to `events`,
`activity` or any other pre-existing table.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from .. import config, db, pause, privacy
from ..util import human_dur, log

lg = log("brain.threads")

HOME = os.path.expanduser("~")

# --------------------------------------------------------------------------- ruleset

# Folders that *contain* projects: the first folder below one of these is a
# project. Only the ones that exist on this machine are used. Override with
# `threads.roots` in config.yaml when your work lives somewhere else.
DEFAULT_ROOTS = [
    "~/Desktop",
    "~/Projects",
    "~/code",
    "~/dev",
    "~/src",
    "~/repos",
    "~/git",
    "~/source/repos",
    "~/Documents/GitHub",
    "~",
]

# Folders that hold work but are not themselves a unit of work, plus the usual
# Windows profile furniture.
NON_PROJECTS = {
    "downloads", "documents", "pictures", "videos", "music", "appdata", "onedrive",
    "desktop", "backup", "contacts", "favorites", "links", "searches", "sendto",
    "templates", "nethood", "printhood", "recent", "saved-games", "start-menu",
    "local-settings", "application-data", "my-documents", "movies", "node-modules",
    "anaconda3", "eclipse", "eclipse-workspace", "vsdbg", "postman", "qbittorrent",
    "virtualbox-vms", "source", "bin", "argus",
    "claude", "docs", "assets", "media", "images", "img", "src",
    "dist", "build", "public", "static", "temp", "tmp", "new-folder", "untitled",
}


def non_project(key: str) -> bool:
    """True for a folder slug that holds work but is not a unit of work.

    The built-in set above, a company OneDrive folder ("OneDrive - Acme Ltd"
    slugs to "onedrive-acme-ltd"), and whatever you list yourself under
    `threads.non_projects` in config.yaml.
    """
    if key in NON_PROJECTS or key.startswith("onedrive-"):
        return True
    extra = config.get("threads.non_projects", []) or []
    return key in {normalise(str(x)) for x in extra}

# Editor and tool artefact directories. Their churn is not evidence of thinking.
# `_archive` alone contributed 1,627 events to one project before this filter.
_NOISE_NAMES = (".history", "_archive", "node_modules", ".git", "__pycache__",
                ".next", "site-packages", ".cache", "venv", ".venv", ".idea")
NOISE_DIRS = tuple(f"{os.sep}{n}{os.sep}".lower() for n in _NOISE_NAMES)

# github.com paths shaped like <org>/<repo> that are really the site's furniture.
GH_RESERVED = {
    "login", "sessions", "settings", "notifications", "orgs", "users", "search",
    "marketplace", "features", "pricing", "about", "explore", "topics", "new",
    "collections", "events", "codespaces", "apps", "dashboard", "account",
    "pulls", "issues", "stars", "watching", "sponsors", "organizations", "join",
}

# Window titles that must never mint or attach to a project.
TITLE_STOP = {
    "", "new tab", "mozilla firefox", "program manager", "idle", "locked",
    "<private>", "google chrome", "microsoft edge", "settings", "task manager",
    "windows shell experience host", "search", "start", "file explorer",
    "notification center", "untitled", "home",
}

# How confirmatory each source is. Used for the diversity term of confidence.
SRC_W = {"claude": 4, "git": 4, "file": 3, "browser": 2, "window": 1}
SRC_W_TOTAL = sum(SRC_W.values())

WINDOW_DAYS = 45
DORMANT_GAP = 10 * 86400     # a new run of work only after 10 days of total silence
MIN_WEIGHT = 3.0
MIN_ALIAS = 4
SOLO_MIN_WEIGHT = 50         # a single-source thread must be substantial
SOLO_MIN_DAYS = 2

_slug_bad = re.compile(r"[^a-z0-9]+")
_GH = re.compile(r"(?:github|gitlab|bitbucket)\.(?:com|org)/([^/]+)/([^/?#]+)", re.I)


_roots_cache: tuple[float, tuple, list[str]] = (0.0, (), [])


def _roots() -> list[str]:
    """Container folders, longest first. Cached for a minute: key_from_path()
    runs once per file event (~30k per rebuild), and re-checking every root on
    disk each time was two thirds of the whole rebuild."""
    global _roots_cache
    raw = tuple(config.get("threads.roots", DEFAULT_ROOTS) or DEFAULT_ROOTS)
    ts, key, cached = _roots_cache
    if key == raw and time.time() - ts < 60:
        return cached
    out = []
    for r in raw:
        p = os.path.abspath(os.path.expanduser(r))
        if os.path.isdir(p):
            out.append(p)
    out = sorted(out, key=len, reverse=True)
    _roots_cache = (time.time(), raw, out)
    return out


def normalise(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"-{2,}", "-", _slug_bad.sub("-", s)).strip("-")[:60]


def key_from_path(path: str) -> str | None:
    """Absolute path -> stable project slug, or None when it is not project work.

    The key is the slug, never the path, so a project that moves on disk keeps
    one continuous thread. That is not theoretical: the same project can have
    git evidence at ~/Desktop and file evidence in a folder one level down.
    """
    if not path or privacy.private_project_in(path):
        return None
    p = path.replace("/", os.sep).rstrip(os.sep)
    low = p.lower()
    roots = _roots()
    if any(low == r.lower().rstrip(os.sep) for r in roots):
        return None                       # the folder IS a container root
    if any(n in low for n in NOISE_DIRS):
        return None                       # editor/tool artefact directory
    for root in roots:
        rl = root.lower().rstrip(os.sep)
        if not low.startswith(rl + os.sep):
            continue
        seg = p[len(root) + 1:].split(os.sep, 1)[0]
        if not seg:
            return None
        k = normalise(seg)
        if not k or non_project(k):
            return None
        if k.endswith("-files"):
            return None                   # browser "save page complete" asset dump
        return k
    return None


# --------------------------------------------------------------------------- evidence

class Ev:
    __slots__ = ("ts", "src", "key", "weight", "seconds", "label",
                 "ref_table", "ref_id", "resolver")

    def __init__(self, ts, src, key, weight, seconds, label,
                 ref_table, ref_id, resolver):
        self.ts, self.src, self.key = ts, src, key
        self.weight, self.seconds, self.label = weight, seconds, label
        self.ref_table, self.ref_id, self.resolver = ref_table, ref_id, resolver


def _meta(row) -> dict:
    try:
        return json.loads(row["meta"] or "{}")
    except Exception:
        return {}


FILE_TOUCHES_PER_MINUTE = 5


def collect_minting(since: float) -> list[Ev]:
    """Sources allowed to MINT a key: file, git, Claude Code transcripts."""
    evs: list[Ev] = []

    # File events count for what a person did, not how many files a tool
    # wrote: build output and temp files recorded before the sensor filtered
    # them are skipped, and a project gets at most FILE_TOUCHES_PER_MINUTE
    # file touches a minute (a formatter or checkout is one act, not 400).
    from ..sensors.files import generated
    per_minute: dict[tuple[str, int], int] = defaultdict(int)
    for r in db.q("SELECT id, ts, subject, detail, meta FROM events WHERE source='file' "
                  "AND kind IN ('created','modified','moved') AND ts > ? ORDER BY ts",
                  (since,)):
        if generated(r["detail"] or ""):
            continue
        k = key_from_path(_meta(r).get("dir", ""))
        if k:
            slot = (k, int(r["ts"] // 60))
            per_minute[slot] += 1
            if per_minute[slot] > FILE_TOUCHES_PER_MINUTE:
                continue
            evs.append(Ev(r["ts"], "file", k, 1.0, 0.0, r["subject"],
                          "events", r["id"], "file:meta.dir"))

    for r in db.q("SELECT id, ts, subject, detail, meta FROM events "
                  "WHERE source='git' AND kind='commit' AND ts > ?", (since,)):
        m = _meta(r)
        k = key_from_path(m.get("path", "")) or normalise(r["subject"] or "")
        if k and not non_project(k):
            evs.append(Ev(r["ts"], "git", k, 3.0, 0.0, (r["detail"] or "")[:90],
                          "events", r["id"], "git:meta.path"))

    evs += collect_claude(since)
    return evs


# Claude Code transcripts: stated intent, which nothing else on the machine has.
_HUMAN = b'"origin":{"kind":"human"}'
_TITLE = b'"type":"ai-title"'
_MAX_LINE = 262_144          # the largest real line observed is 2.39 MB of tool output
_FENCE = re.compile(r"```.*?```", re.S)
_OPAQUE = re.compile(r"[A-Za-z0-9_\-]{28,}")
_FILLER = {"continue", "??", "?", "yes", "ok", "okay", "go on", "do it", "y", "n",
           "next", "fix it", "proceed", "done", "thanks", "k", "yep", "sure"}


def _iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _human_text(o: dict) -> str:
    """The user's own words, scrubbed. Never assistant text, never tool output."""
    msg = o.get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        txt = c
    elif isinstance(c, list):
        txt = " ".join(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") == "text")
    else:
        return ""

    t = txt.strip()
    if not t or t.startswith("/"):
        return ""
    # Anything opening with an XML-ish tag is injected by the IDE or the harness,
    # not typed by the user - e.g. "<ide_opened_file>The user opened the file ...".
    # These carry origin.kind == "human" but are not human intent.
    if t.startswith("<"):
        return ""
    for marker in ("<command-name>", "<system-reminder>", "<local-command-stdout>",
                   "<ide_opened_file>", "<ide_selection>", "<ide_diagnostics>",
                   "Caveat: The messages below", "The user opened the file",
                   "This session is being continued from"):
        if marker in t:
            return ""

    t = _FENCE.sub(" ", t)                       # drop pasted code blocks
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    t = " ".join(lines[:2])                      # intent lives at the top
    if len(t) < 12 or t.lower().strip(".!? ") in _FILLER:
        return ""

    t = privacy.scrub(t)
    t = privacy.mask_email(t)
    t = _OPAQUE.sub("<token>", t)                # keys/hashes the pattern list misses
    if privacy.is_private_title(t):
        return ""
    t = t.replace(HOME, "~")
    return re.sub(r"\s+", " ", t).strip()[:400]


def collect_claude(since: float) -> list[Ev]:
    base = os.path.join(HOME, ".claude", "projects")
    if not os.path.isdir(base):
        return []
    if not config.get("threads.transcripts", True):
        return []

    evs: list[Ev] = []
    sess_key: dict[str, str] = {}
    titles: list[tuple[float, str, str]] = []
    paused = pause.intervals()

    for fp in glob.glob(os.path.join(base, "*", "*.jsonl")):
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if st.st_mtime < since:
            continue
        try:
            with open(fp, "rb") as fh:
                for raw in fh:
                    if len(raw) > _MAX_LINE:
                        continue                 # never parse a giant tool result
                    if _HUMAN in raw:
                        try:
                            o = json.loads(raw)
                        except Exception:
                            continue
                        if o.get("isSidechain"):
                            continue             # subagent, not the user
                        k = key_from_path(o.get("cwd") or "")
                        if not k:
                            continue
                        sid = o.get("sessionId") or ""
                        if sid:
                            sess_key[sid] = k
                        text = _human_text(o)
                        ts = _iso(o.get("timestamp")) or st.st_mtime
                        if text and not pause.was_paused_at(ts, paused):
                            evs.append(Ev(ts, "claude", k, 3.0, 0.0, text,
                                          "claude", 0, "claude:cwd"))
                    elif _TITLE in raw:
                        try:
                            o = json.loads(raw)
                        except Exception:
                            continue
                        t = (o.get("aiTitle") or "").strip()
                        sid = o.get("sessionId") or ""
                        if t and sid:
                            titles.append((st.st_mtime, sid, t))
        except OSError:
            continue

    seen = set()
    for mt, sid, title in titles:
        k = sess_key.get(sid)
        if not k or (sid, title) in seen:
            continue
        seen.add((sid, title))
        evs.append(Ev(mt, "claude", k, 2.0, 0.0, f"session: {title}",
                      "claude", 0, "claude:ai-title"))
    return evs


def build_alias_index(keys) -> dict:
    """Aliases for attach-only matching, longest first so the specific key wins."""
    pairs = []
    for k in keys:
        if k.startswith("gh:"):
            continue
        for a in {k, k.replace("-", " "), k.replace("-", "")}:
            if len(a) >= MIN_ALIAS:
                pairs.append((a.lower(), k))
    pairs.sort(key=lambda p: -len(p[0]))
    return {"pairs": pairs, "keys": set(keys)}


def _alias_hit(text: str, idx: dict) -> str | None:
    t = (text or "").lower()
    for alias, key in idx["pairs"]:
        if alias in t:
            return key
    return None


def collect_attaching(since: float, idx: dict) -> list[Ev]:
    """Browser and window titles may only ATTACH to a key that already exists.

    Without that rule `Stremio`, `Mozilla Firefox` and `New chat - Claude` all
    become projects.
    """
    evs: list[Ev] = []

    for r in db.q("SELECT id, ts, subject, detail, meta FROM events "
                  "WHERE source='browser' AND kind='visit' AND ts > ?", (since,)):
        m = _meta(r)
        gh = _GH.search(m.get("url", ""))
        key = resolver = None
        if gh and gh.group(1).lower() not in GH_RESERVED:
            repo = normalise(gh.group(2))
            if repo in idx["keys"]:
                key, resolver = repo, "browser:repo-url"
            else:
                # Real work can live in a repo with no local checkout, so an
                # external repo gets a first-class key rather than being dropped.
                key, resolver = f"gh:{gh.group(1).lower()}/{repo}", "browser:external-repo"
        else:
            hit = _alias_hit(f"{r['subject']} {r['detail']}", idx)
            if hit:
                key, resolver = hit, "browser:title-alias"
        if key:
            typed = m.get("transition") in ("typed", "form_submit")
            evs.append(Ev(r["ts"], "browser", key, 1.0 if typed else 0.5, 0.0,
                          (r["detail"] or r["subject"] or "")[:90],
                          "events", r["id"], resolver))

    for r in db.q("SELECT id, started, seconds, title, app, exe FROM activity "
                  "WHERE afk=0 AND private=0 AND seconds >= 60 AND started > ?", (since,)):
        title = (r["title"] or "").strip()
        if title.lower() in TITLE_STOP:
            continue
        hit = _alias_hit(f"{title} {r['app']}", idx)
        if not hit:
            continue
        # Backfilled rows are reconstructed with implied spans, not observed. They
        # are 91% of the activity table and 720 synthesised hours; at full weight
        # they bury every real session.
        inferred = (r["exe"] or "").startswith("<inferred:")
        damp = 0.1 if inferred else 1.0
        evs.append(Ev(r["started"], "window", hit,
                      min(2.0, r["seconds"] / 600.0) * damp, r["seconds"] * damp,
                      f"{r['app']}: {title[:70]}", "activity", r["id"],
                      "activity:title-alias"))
    return evs


# --------------------------------------------------------------------------- assembly

def _git_states(wanted: set[str]) -> dict:
    """Repo state for open-loop detection, filtered to keys we care about.

    A fresh scan shells out ~6 times per repo and cost more than the rest of
    the pipeline combined; the git sensor now keeps a scan at most ten
    minutes old, which is plenty for "is there uncommitted work here".
    """
    from ..sensors import git as gitsensor
    out = {}
    try:
        for st in gitsensor.all_states(max_age=900):
            name = normalise(os.path.basename(str(st.get("path", "")).rstrip(os.sep)))
            if name in wanted:
                out[name] = st
    except Exception:
        lg.exception("git state scan failed")
    return out


def _realm_for(key: str, run: list, repo: dict | None) -> str:
    """work / personal / unknown, by the same markers and taught rules that
    classify every window - applied to the project name, its repo path and
    what was said and touched while working on it."""
    from .. import classify
    blob = " ".join([key.replace("-", " "), key, str((repo or {}).get("path", ""))]
                    + [e.label for e in run[:40]]).lower()
    for needle, _cat, realm in classify._learned():
        if realm and needle in blob:
            return realm
    return classify.realm_of(blob)


def _title_for(key: str) -> str:
    """A readable name. An external repo keeps its real repo name, not a slug."""
    if key.startswith("gh:"):
        org, _, repo = key[3:].partition("/")
        return f"{repo} ({org})" if org else repo
    return key.replace("-", " ")


def confidence(weight: float, sources: set[str], days: int) -> tuple[float, dict]:
    """Deterministic and explainable. Never an LLM's self-reported number.

    Log on evidence so 3,000 file writes cannot drown 5 commits; weighted
    diversity because sources differ in how confirmatory they are; persistence
    because a one-day blip is not a thread.
    """
    E = min(1.0, math.log10(1 + weight) / math.log10(61.0))
    D = sum(SRC_W[s] for s in sources) / SRC_W_TOTAL
    P = min(1.0, days / 5.0)
    return round(0.45 * E + 0.35 * D + 0.20 * P, 2), {
        "evidence": round(E, 2), "diversity": round(D, 2), "persistence": round(P, 2),
        "weight": round(weight, 1), "sources": sorted(sources), "days": days,
    }


def derive_status(age_days: float, repo: dict | None, last: float) -> tuple[str, str]:
    """Derived from evidence, and deliberately asymmetric.

    `completed` needs positive proof of an ending. Inferring it from silence
    would tell you that you finished things you actually abandoned.
    """
    if repo and not repo["dirty"] and not repo["ahead"] and age_days >= 7 \
            and repo["last_commit_ts"] >= last - 3600:
        return "completed", (f"clean tree on {repo['branch']}, nothing unpushed, "
                             f"untouched {age_days:.0f}d")
    if age_days <= 1.5:
        return "active", f"touched {human_dur(age_days * 86400)} ago"
    if repo and (repo["dirty"] or repo["ahead"]):
        return "incomplete", (f"{repo['dirty']} uncommitted, {repo['ahead']} unpushed "
                              f"on {repo['branch']}, idle {age_days:.0f}d")
    if age_days <= 45:
        return "paused", f"no evidence for {age_days:.0f}d, working tree clean"
    return "abandoned", f"no evidence for {age_days:.0f}d"


def extract(now: float | None = None) -> list[dict]:
    """The pure function: (events, activity, transcripts) -> ranked threads.

    Reads only. Materialising the result is `rebuild()`'s job.
    """
    now = now or time.time()
    since = now - WINDOW_DAYS * 86400

    minting = collect_minting(since)
    idx = build_alias_index({e.key for e in minting})
    evs = minting + collect_attaching(since, idx)
    repos = _git_states({e.key for e in evs})

    by_key: dict[str, list[Ev]] = defaultdict(list)
    for e in evs:
        by_key[e.key].append(e)

    out: list[dict] = []
    for key, rows in by_key.items():
        rows.sort(key=lambda e: e.ts)
        runs, cur = [], [rows[0]]
        for e in rows[1:]:
            if e.ts - cur[-1].ts > DORMANT_GAP:
                runs.append(cur)
                cur = [e]
            else:
                cur.append(e)
        runs.append(cur)

        for seq, run in enumerate(runs, 1):
            srcs = {e.src for e in run}
            weight = sum(e.weight for e in run)
            days = len({time.strftime("%Y-%m-%d", time.localtime(e.ts)) for e in run})
            last = max(e.ts for e in run)

            if weight < MIN_WEIGHT or now - last > WINDOW_DAYS * 86400:
                continue
            if srcs == {"window"} and all(e.weight <= 0.2 for e in run):
                continue                     # inferred window rows alone are not a thread
            if len(srcs) == 1 and not key.startswith("gh:"):
                if weight < SOLO_MIN_WEIGHT or days < SOLO_MIN_DAYS:
                    continue                 # a lone source is usually an artefact

            repo = repos.get(key)
            open_loop = bool(repo and (repo["dirty"] or repo["ahead"]))
            age_days = (now - last) / 86400
            conf, parts = confidence(weight, srcs, days)
            status, reason = derive_status(age_days, repo, last)
            out.append({
                "key": key,
                # A project can have several distinct runs of work separated by
                # dormancy, so the stable identity is key+seq, not key alone.
                "seq": seq,
                "uid": key if len(runs) == 1 else f"{key}:{seq}",
                "_title": _title_for(key),
                "realm": _realm_for(key, run, repo),
                "run": run, "sources": srcs, "weight": weight, "days": days,
                "seconds": sum(e.seconds for e in run),
                "first_ts": min(e.ts for e in run), "last_ts": last,
                "repo": repo, "open_loop": open_loop, "age_days": age_days,
                "confidence": conf, "confidence_parts": parts,
                "status": status, "status_reason": reason,
                "score": (math.exp(-age_days / 3.0) * math.log1p(weight)
                          * (1 + 0.5 * open_loop)),
            })

    out.sort(key=lambda t: -t["score"])
    return out


# --------------------------------------------------------------------------- persistence

def rebuild(now: float | None = None) -> dict:
    """Re-derive and materialise. DELETE + INSERT in one transaction.

    `threads` and `thread_touches` hold only derived state, so a bad run is
    fixed by running it again and losing them loses nothing.
    """
    t0 = time.time()
    found = extract(now)
    suppressed = _suppressed()

    with db.tx() as c:
        c.execute("DELETE FROM thread_touches")
        c.execute("DELETE FROM threads")
        for t in found:
            if t["key"] in suppressed:
                continue
            cur = c.execute(
                "INSERT INTO threads(key,seq,uid,title,realm,first_ts,last_ts,"
                "seconds_total,touch_count,source_count,status,status_reason,"
                "confidence,confidence_parts,score,open_loop,meta,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["key"], t["seq"], t["uid"], t["_title"], t["realm"],
                 t["first_ts"], t["last_ts"], t["seconds"],
                 len(t["run"]), len(t["sources"]), t["status"], t["status_reason"],
                 t["confidence"], json.dumps(t["confidence_parts"]), t["score"],
                 int(t["open_loop"]),
                 json.dumps({"repo": (t["repo"] or {}).get("path"),
                             "branch": (t["repo"] or {}).get("branch"),
                             "dirty": (t["repo"] or {}).get("dirty"),
                             "ahead": (t["repo"] or {}).get("ahead"),
                             "days": t["days"]}),
                 time.time()))
            tid = cur.lastrowid
            c.executemany(
                "INSERT INTO thread_touches(thread_id,ts,source,ref_table,ref_id,"
                "evidence,seconds,weight,resolver) VALUES(?,?,?,?,?,?,?,?,?)",
                [(tid, e.ts, e.src, e.ref_table, e.ref_id, e.label[:300],
                  e.seconds, e.weight, e.resolver) for e in t["run"]])

    ms = int((time.time() - t0) * 1000)
    lg.info("rebuilt %d threads in %dms", len(found), ms)
    return {"threads": len(found), "ms": ms}


# --------------------------------------------------------------------------- realm inference
#
# Windows that do the most work often carry no project name - a Claude Code
# terminal is titled "✳ Multiple GitHub accounts setup" - so their realm stayed
# "unknown" and a day spent shipping atlas showed under two hours of work.
#
# Only exact joins are used, never time proximity. Proximity ("what else
# happened within ten minutes") was tried and measured on the live data: with
# several Claude sessions running in different projects at once, it labelled
# the generic Claude app "personal" because portfolio files were changing in
# another window. Two joins are precise:
#
#   1. the thread engine already ties windows whose title names a project
#      ("frolicinfraproject-website - Visual Studio Code") to that project's
#      thread, row by row;
#   2. a Claude Code terminal's title is its session's ai-title, and the
#      transcript records which project directory that session ran in.

_SPINNER = re.compile(r"^[^\w(\[]+")   # ✳ ◐ ◑ ✻ and friends in front of the title


def _session_title(title: str) -> str:
    return re.sub(r"\s+", " ", _SPINNER.sub("", title or "")).strip().lower()


def infer_realms(days: float = 14, now: float | None = None) -> dict:
    """Give unknown-realm sessions the realm of the project they demonstrably
    belong to. Only ever fills in "unknown": anything the markers or your own
    teachings labelled is left alone. Returns counts for the run log."""
    now = now or time.time()
    since = now - days * 86400
    updates: dict[int, str] = {}

    # 1. windows the thread engine attached to a project by name
    for r in db.q(
            "SELECT tt.ref_id, t.realm FROM thread_touches tt "
            "JOIN threads t ON t.id = tt.thread_id WHERE tt.source='window' "
            "AND tt.ref_table='activity' AND tt.ts > ? AND t.realm IN ('work','personal')",
            (since,)):
        updates[int(r["ref_id"])] = r["realm"]

    # 2. Claude Code terminals, by session title -> the session's project
    titles: dict[str, set] = defaultdict(set)
    for r in db.q(
            "SELECT tt.evidence, t.realm FROM thread_touches tt "
            "JOIN threads t ON t.id = tt.thread_id WHERE tt.source='claude' "
            "AND tt.evidence LIKE 'session: %' AND t.realm IN ('work','personal')"):
        titles[_session_title(r["evidence"][len("session: "):])].add(r["realm"])
    known = {t: next(iter(rs)) for t, rs in titles.items() if len(rs) == 1 and len(t) >= 8}
    rows = db.q("SELECT id, title FROM activity WHERE started > ? AND afk=0 AND private=0 "
                "AND realm='unknown' AND category IN ('terminal','ai','code')", (since,))
    for r in rows:
        realm = known.get(_session_title(r["title"]))
        if realm:
            updates[r["id"]] = realm

    pairs = [(realm, rid) for rid, realm in updates.items()]
    labelled = 0
    for i in range(0, len(pairs), 2000):
        with db.tx() as c:
            labelled += c.executemany(
                "UPDATE activity SET realm=? WHERE id=? AND realm='unknown'",
                pairs[i:i + 2000]).rowcount
    return {"checked": len(rows), "labelled": labelled}


def _suppressed() -> set[str]:
    """Keys the user dismissed in the last 7 days."""
    cutoff = time.time() - 7 * 86400
    return {r["target"] for r in db.q(
        "SELECT DISTINCT target FROM feedback WHERE kind='thread' "
        "AND signal='dismissed' AND ts > ?", (cutoff,))}


def ranked(limit: int = 10, min_confidence: float | None = None) -> list[dict]:
    mc = min_confidence if min_confidence is not None else \
        float(config.get("threads.min_confidence", 0.3))
    rows = db.rows_to_dicts(db.q(
        "SELECT * FROM threads WHERE confidence >= ? ORDER BY score DESC LIMIT ?",
        (mc, limit)))
    for r in rows:
        r["confidence_parts"] = json.loads(r["confidence_parts"] or "{}")
        r["meta"] = json.loads(r["meta"] or "{}")
    return rows


def context_for(key: str) -> dict | None:
    """Everything known about one thread, with every claim traceable to a row.

    Accepts a thread's uid ("atlas:2") or its key. A project with several
    runs of work separated by dormancy has one row per run under the same key,
    and looking up by key alone returned whichever SQLite found first; by key,
    the highest-ranked run now wins.
    """
    r = db.q1("SELECT * FROM threads WHERE uid = ?", (key,)) or \
        db.q1("SELECT * FROM threads WHERE key = ? ORDER BY score DESC LIMIT 1", (key,))
    if not r:
        return None
    t = dict(r)
    t["confidence_parts"] = json.loads(t["confidence_parts"] or "{}")
    t["meta"] = json.loads(t["meta"] or "{}")
    t["touches"] = db.rows_to_dicts(db.q(
        "SELECT * FROM thread_touches WHERE thread_id = ? ORDER BY ts DESC",
        (t["id"],)))
    by_src: dict[str, list] = defaultdict(list)
    for x in t["touches"]:
        by_src[x["source"]].append(x)
    t["by_source"] = dict(by_src)
    return t


def record_feedback(key: str, signal: str, detail: str = "",
                    meta: dict | None = None) -> None:
    """Captured at the moment of use, because it is unrecoverable afterwards.

    Deliberately its own table, not `memory`: `db.recall()` orders by weight and
    `digest._memory_context()` reads 12 rows unfiltered, so writing here would
    starve every learned preference out of the digests.
    """
    db.set_state  # noqa: B018  (documents the deliberate non-use of `memory`)
    with db.tx() as c:
        c.execute("INSERT INTO feedback(ts,kind,target,signal,detail,meta) "
                  "VALUES(?,?,?,?,?,?)",
                  (time.time(), "thread", key, signal, detail[:500],
                   json.dumps(meta or {})))


# --------------------------------------------------------------------------- selftest

def selftest() -> tuple[bool, list[str]]:
    """The regression suite, until there is reason for more than this.

    Asserts the acceptance criteria against whatever is actually in the DB.
    """
    out, ok = [], True

    def check(cond, msg):
        nonlocal ok
        out.append(("PASS " if cond else "FAIL ") + msg)
        ok = ok and cond

    # Row counts are useless here: the daemon is collecting concurrently, so
    # `events` legitimately grows mid-test. SQLite's total_changes counts rows
    # written by THIS connection only, which is exactly the question.
    changes_before = db.conn().total_changes

    a = extract()
    b = extract()
    check([t["uid"] for t in a] == [t["uid"] for t in b], "extract() is idempotent")

    noise = {"new tab", "program manager", "<private>", "idle", "locked",
             "mozilla firefox"}
    bad = [t["key"] for t in a if t["key"].lower() in noise]
    check(not bad, f"no noise threads (found {bad})")

    top3 = a[:3]
    solo_inferred = [t["key"] for t in top3
                     if t["sources"] == {"window"} and t["weight"] < 5]
    check(not solo_inferred, f"no inferred-only thread in top 3 ({solo_inferred})")

    for t in a[:5]:
        for e in t["run"][:3]:
            if e.ref_table in ("events", "activity"):
                row = db.q1(f"SELECT 1 FROM {e.ref_table} WHERE id = ?", (e.ref_id,))
                if not row:
                    check(False, f"evidence {e.ref_table}#{e.ref_id} does not resolve")
                    break

    check(all(0 <= t["confidence"] <= 1 for t in a), "confidence within 0..1")

    check(db.conn().total_changes == changes_before,
          "extract() wrote nothing at all")

    return ok, out
