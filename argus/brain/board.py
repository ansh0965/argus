"""The board: every task you have, in one place, kept up to date by evidence.

Five columns - inbox, today, doing, parked, done. Cards come from three
places:

  - you, through quick capture ("call Sam about the login bug fix tomorrow"),
    the dashboard, the CLI or Google Tasks;
  - the thread engine, which already knows where work was left hanging: a
    repo with uncommitted or unpushed work becomes a "loop" card, and a
    project that went quiet mid-way becomes a "dropped" card carrying the
    last thing you asked Claude Code about it;
  - the morning plan, which moves the few cards you pick into Today.

Evidence also moves cards: a loop card goes to Done by itself once the tree
is clean and pushed, and a dropped or parked card goes to Doing when you are
back on that project. Dismissing an automatic card is remembered, so it is
not re-created on the next sync.

Cards are rows of the existing `tasks` table (so Google Tasks sync, the CLI
and the briefings keep working); the board adds a status column and friends.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from .. import classify, config, db
from ..util import human_ago, human_dur, log
from . import insights

lg = log("brain.board")

COLUMNS = ("inbox", "today", "doing", "parked", "done")
DONE_SHOWN_DAYS = 7
SYNC_EVERY = 60.0
# A project untouched this long, with nothing left uncommitted, is a "dropped"
# card if it was real work (enough evidence) and not that long ago.
DROPPED_AFTER_DAYS = 2.0
DROPPED_UNTIL_DAYS = 21.0
DROPPED_MIN_CONFIDENCE = 0.6
BACK_ON_IT_SECONDS = 20 * 60
PICKED_UP_AFTER = 20 * 60          # worked on a project this long after planning it = picked up

_last_sync = 0.0


# --------------------------------------------------------------------------- dates

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_SHORT = {d[:3]: i for i, d in enumerate(_WEEKDAYS)}
_DAY_RX = re.compile(
    r"\b(?:(?:by|on|due)\s+)?(today|tonight|tomorrow|tmrw|"
    r"(?:next\s+)?(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|in\s+(\d+)\s+days?|next\s+week)\b",
    re.I)
_REL_RX = re.compile(r"\bin\s+(an?|\d+)\s*(h|hrs?|hours?|m|mins?|minutes?)\b", re.I)
_TIME_RX = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b"      # 5pm, 5:30 pm, at 11am
    r"|\bat\s+(\d{1,2})(?::(\d{2}))?\b"                    # at 17, at 9:15
    r"|\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b"           # 17:30
    r"|\b(?:at\s+)?(noon|midday|midnight)\b", re.I)


def _cut(text: str, m: re.Match) -> str:
    return text[:m.start()] + " " + text[m.end():]


def _tidy(title: str, fallback: str) -> str:
    title = re.sub(r"\s{2,}", " ", title).strip(" ,.-")
    title = re.sub(r"\s+(at|by|on|due)$", "", title, flags=re.I)
    return title or fallback.strip()


def parse_when(text: str, now: datetime | None = None) -> tuple[str, float | None, bool]:
    """'call Sam at 5pm' -> ('call Sam', <today 17:00>, True).

    Understands what people type into a capture box: a day (today, tonight,
    tomorrow, friday, next week, in 3 days), a time (5pm, 17:30, at 9, noon),
    both in either order, or a span from now (in 2 hours, in 30 min). The
    third value says whether a clock time was given - only then is there a
    reminder at that minute; a day alone means "by the end of it" (18:00).
    """
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    text = text or ""
    rel = _REL_RX.search(text)
    if rel:
        n = 1 if rel.group(1).lower() in ("a", "an") else int(rel.group(1))
        unit = rel.group(2).lower()
        delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
        return _tidy(_cut(text, rel), text), (now + delta).timestamp(), True

    day_m = _DAY_RX.search(text)
    rest = _cut(text, day_m) if day_m else text
    time_m = _TIME_RX.search(rest)
    if not day_m and not time_m:
        return text.strip(), None, False

    hour, minute, has_time = 18, 0, False
    if time_m:
        g = time_m.groups()
        if g[0]:
            hour, minute = int(g[0]) % 12 + (12 if g[2].lower() == "pm" else 0), int(g[1] or 0)
        elif g[3]:
            hour, minute = int(g[3]), int(g[4] or 0)
            if hour < 8:                    # "at 5" means the afternoon, not 5 a.m.
                hour += 12
        elif g[5]:
            hour, minute = int(g[5]), int(g[6])
        else:
            hour, minute = (0, 0) if g[7].lower() == "midnight" else (12, 0)
        if not 0 <= hour <= 23:
            return text.strip(), None, False
        has_time = True
        rest = _cut(rest, time_m)

    day = now
    if day_m:
        word = day_m.group(1).lower()
        if word == "tonight" and not has_time:
            hour = 21
        elif word in ("tomorrow", "tmrw"):
            day = now + timedelta(days=1)
        elif word.startswith("in "):
            day = now + timedelta(days=int(day_m.group(2)))
        elif word == "next week":
            day = now + timedelta(days=7 - now.weekday())
        elif word not in ("today", "tonight"):          # a weekday: the next one
            wd = _SHORT.get(word.split()[-1][:3])
            if wd is None:
                return text.strip(), None, False
            day = now + timedelta(days=(wd - now.weekday()) % 7 or 7)
    due = day.replace(hour=hour, minute=minute)
    if not day_m and due <= now:            # "at 9am" said after 9am: tomorrow
        due += timedelta(days=1)
    return _tidy(rest, text), due.timestamp(), has_time


def parse_due(text: str, now: datetime | None = None) -> tuple[str, float | None]:
    title, due, _ = parse_when(text, now)
    return title, due


# --------------------------------------------------------------------------- rows

def _card(r) -> dict:
    d = dict(r)
    try:
        d["meta"] = json.loads(d.get("meta") or "{}")
    except ValueError:
        d["meta"] = {}
    # `done` is the truth (Google Tasks sync and the CLI only set that column).
    st = d.get("status") or "inbox"
    d["status"] = "done" if d.get("done") else ("inbox" if st == "done" else st)
    d["auto"] = bool(d.get("origin"))
    now = time.time()
    d["overdue"] = bool(d.get("due") and d["due"] < now and not d.get("done"))
    d["age"] = human_ago(d.get("updated") or d.get("created") or now)
    return d


def get(card_id: int) -> dict | None:
    r = db.q1("SELECT * FROM tasks WHERE id=?", (card_id,))
    return _card(r) if r else None


def add(title: str, status: str = "inbox", due: float | None = None, notes: str = "",
        origin: str = "", realm: str = "", meta: dict | None = None,
        source: str = "argus") -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("a card needs a title")
    if status not in COLUMNS:
        raise ValueError(f"status must be one of {', '.join(COLUMNS)}")
    now = time.time()
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO tasks(created,due,title,notes,source,status,origin,realm,meta,"
            "updated,sort,done,done_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, due, title[:400], (notes or "")[:2000], source, status, origin,
             realm or classify.realm_of(title), json.dumps(meta or {}), now, -now,
             1 if status == "done" else 0, now if status == "done" else None))
        cid = int(cur.lastrowid)
    return get(cid)


def capture(text: str) -> dict:
    """Quick capture: one line in, one card out, due date understood.

    With a clock time ("at 5pm", "in 2 hours") the card also gets a reminder
    at that minute.
    """
    if is_schedule(text):                   # "schedule ...", "block ...", "book ..."
        r = schedule(text)
        return {**r["card"], "message": r["message"]}
    title, due, has_time = parse_when(text)
    same = find_similar(title)
    if same:
        # Already on the board: the new words become a note on it, so one
        # piece of work never splits into two cards that drift apart.
        notes = ((same.get("notes") or "") + f"\n+ {title}").strip()[:2000]
        with db.tx() as c:
            c.execute("UPDATE tasks SET notes=?, updated=?, due=COALESCE(?, due) WHERE id=?",
                      (notes, time.time(), due, same["id"]))
        card = set_reminder(same["id"], due) if has_time else get(same["id"])
        return {**card, "message": f"Already on your board as “{same['title']}” - added as a note."}
    status = "inbox"
    if due and datetime.fromtimestamp(due).date() <= datetime.now().date():
        status = "today"
    card = add(title, status=status, due=due, source="capture")
    if has_time:
        card = set_reminder(card["id"], due)
    return card


_FILLER = {"the", "a", "an", "to", "for", "on", "in", "of", "and", "with", "my", "finish", "fix",
           "do", "work", "pick", "back", "up", "task"}


def _sig(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if w not in _FILLER and len(w) > 1}


def find_similar(title: str, threshold: float = 0.6) -> dict | None:
    """An open card that is the same piece of work: most of the meaningful
    words in common (Jaccard), and at least two of them."""
    want = _sig(title)
    if len(want) < 2:
        return None
    best, best_score = None, 0.0
    for r in db.q("SELECT * FROM tasks WHERE done=0 AND COALESCE(dismissed,0)=0"):
        have = _sig(r["title"])
        common = want & have
        if len(common) < 2:
            continue
        score = len(common) / len(want | have)
        if score > best_score:
            best, best_score = r, score
    return _card(best) if best is not None and best_score >= threshold else None


def set_reminder(card_id: int, at: float | None) -> dict:
    """Remind at `at` (epoch seconds); None removes the reminder."""
    with db.tx() as c:
        c.execute("UPDATE tasks SET remind_at=?, reminded=0, updated=? WHERE id=?",
                  (at, time.time(), card_id))
    card = get(card_id)
    if card is None:
        raise KeyError(card_id)
    return card


def snooze(card_id: int, minutes: int = 15) -> dict:
    return set_reminder(card_id, time.time() + max(1, int(minutes)) * 60)


def due_reminders(now: float | None = None) -> list[dict]:
    """Cards whose reminder time has come and that have not been reminded."""
    now = now or time.time()
    return [_card(r) for r in db.q(
        "SELECT * FROM tasks WHERE remind_at IS NOT NULL AND remind_at <= ? "
        "AND COALESCE(reminded,0)=0 AND done=0 AND COALESCE(dismissed,0)=0 "
        "ORDER BY remind_at", (now,))]


def mark_reminded(card_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE tasks SET reminded=1 WHERE id=?", (card_id,))


def move(card_id: int, status: str, before: int | None = None) -> dict:
    """Into a column, optionally just above another card (drag and drop)."""
    if status not in COLUMNS:
        raise ValueError(f"status must be one of {', '.join(COLUMNS)}")
    now = time.time()
    sort = -now
    if before:
        r = db.q1("SELECT sort FROM tasks WHERE id=?", (before,))
        if r and r["sort"] is not None:
            above = db.q1("SELECT MAX(sort) s FROM tasks WHERE status=? AND sort < ? AND id!=?",
                          (status, r["sort"], card_id))
            lo = above["s"] if above and above["s"] is not None else r["sort"] - 2
            sort = (lo + r["sort"]) / 2
    with db.tx() as c:
        if status == "done":
            c.execute("UPDATE tasks SET status='done', done=1, done_ts=COALESCE(done_ts, ?), "
                      "pushed_done=0, updated=?, sort=? WHERE id=?", (now, now, sort, card_id))
        else:
            c.execute("UPDATE tasks SET status=?, done=0, done_ts=NULL, updated=?, sort=?, "
                      "started=CASE WHEN ?='doing' THEN ? ELSE started END WHERE id=?",
                      (status, now, sort, status, now, card_id))
    card = get(card_id)
    if card is None:
        raise KeyError(card_id)
    return card


def edit(card_id: int, **fields) -> dict:
    allowed = {"title", "notes", "due", "pinned", "realm"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "title" in sets:
        sets["title"] = (sets["title"] or "").strip()[:400]
        if not sets["title"]:
            raise ValueError("a card needs a title")
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets)
        with db.tx() as c:
            c.execute(f"UPDATE tasks SET {cols}, updated=? WHERE id=?",
                      (*sets.values(), time.time(), card_id))
    card = get(card_id)
    if card is None:
        raise KeyError(card_id)
    return card


def park(card_id: int, note: str = "") -> dict:
    """Set it down with a note to your future self about where you were."""
    with db.tx() as c:
        c.execute("UPDATE tasks SET park_note=?, parked_ts=? WHERE id=?",
                  ((note or "").strip()[:500], time.time(), card_id))
    return move(card_id, "parked")


def restore(card_id: int) -> dict:
    """Undo a dismiss: the card is back where it was."""
    with db.tx() as c:
        c.execute("UPDATE tasks SET dismissed=0, updated=? WHERE id=?", (time.time(), card_id))
    card = get(card_id)
    if card is None:
        raise KeyError(card_id)
    return card


def dismiss(card_id: int) -> bool:
    """Off the board for good. An automatic card is not re-created."""
    with db.tx() as c:
        return c.execute("UPDATE tasks SET dismissed=1, updated=? WHERE id=?",
                         (time.time(), card_id)).rowcount > 0


# --------------------------------------------------------------------------- evidence

def _last_intent(thread_id: int) -> str:
    r = db.q1("SELECT evidence FROM thread_touches WHERE thread_id=? AND source='claude' "
              "ORDER BY ts DESC LIMIT 1", (thread_id,))
    return (r["evidence"] or "").strip()[:200] if r else ""


def _asked_claude(thread_id: int) -> bool:
    return db.q1("SELECT 1 FROM thread_touches WHERE thread_id=? AND source='claude' LIMIT 1",
                 (thread_id,)) is not None


def _threads() -> list[dict]:
    # One row per key: the best-scoring run of work on that project.
    rows = db.rows_to_dicts(db.q(
        "SELECT * FROM threads WHERE confidence >= ? ORDER BY score DESC",
        (float(config.get("threads.min_confidence", 0.3)),)))
    seen, out = set(), []
    for r in rows:
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        try:
            r["meta"] = json.loads(r["meta"] or "{}")
        except ValueError:
            r["meta"] = {}
        out.append(r)
    return out


def sync(now: float | None = None, force: bool = False) -> dict:
    """Create, refresh and close automatic cards from the thread engine."""
    global _last_sync
    now = now or time.time()
    if not force and now - _last_sync < SYNC_EVERY:
        return {"skipped": True}
    _last_sync = now
    made = closed = moved = 0
    existing = {r["origin"]: dict(r) for r in db.q(
        "SELECT * FROM tasks WHERE origin IS NOT NULL AND origin != ''")}
    for t in _threads():
        key, title = t["key"], t.get("title_user") or t.get("title") or t["key"]
        m = t["meta"]
        idle_days = (now - (t["last_ts"] or now)) / 86400
        intent = _last_intent(t["id"])
        loop_origin, drop_origin = f"loop:{key}", f"drop:{key}"

        # Open loop: something on disk is not committed or not pushed.
        loop = existing.get(loop_origin)
        if t["open_loop"] and m.get("repo"):
            bits = []
            if m.get("dirty"):
                bits.append(f"{m['dirty']} uncommitted")
            if m.get("ahead"):
                bits.append(f"{m['ahead']} unpushed")
            notes = f"{' and '.join(bits)} on {m.get('branch') or '?'}"
            meta = {"thread": key, "intent": intent, "repo": m.get("repo"),
                    "branch": m.get("branch"), "kind": "loop",
                    "next": next_step(m.get("dirty"), m.get("ahead"), m.get("branch"),
                                      intent=intent)}
            if not loop:
                add(f"{title}: finish {m.get('branch') or 'the open work'}", status="inbox",
                    notes=notes, origin=loop_origin, realm=t.get("realm") or "",
                    meta=meta, source="auto")
                made += 1
            elif not loop["dismissed"] and not loop["done"]:
                try:
                    old = json.loads(loop.get("meta") or "{}")
                except ValueError:
                    old = {}
                with db.tx() as c:
                    c.execute("UPDATE tasks SET notes=?, meta=? WHERE id=?",
                              (notes, json.dumps({**old, **meta}), loop["id"]))
        elif loop and not loop["done"] and not loop["dismissed"]:
            # Evidence of an ending: the tree is clean and nothing is unpushed.
            with db.tx() as c:
                c.execute("UPDATE tasks SET notes=? WHERE id=?",
                          ("closed itself: committed and pushed", loop["id"]))
            move(loop["id"], "done")
            closed += 1

        # Dropped: real work that went quiet without an open loop to show for it.
        # "Real" needs a repository or something you asked Claude Code about it:
        # a folder that only collected files (Desktop\PERSONAL, "research",
        # a meeting's downloads) is not a project you can pick back up, and a
        # list full of those teaches you to ignore the list.
        drop = existing.get(drop_origin)
        real = bool(m.get("repo")) or _asked_claude(t["id"])
        dropped = (real and not t["open_loop"] and t["status"] in ("paused", "incomplete")
                   and DROPPED_AFTER_DAYS <= idle_days <= DROPPED_UNTIL_DAYS
                   and (t["confidence"] or 0) >= DROPPED_MIN_CONFIDENCE)
        if drop and not real and not drop["done"] and not drop["dismissed"]                 and (drop.get("status") or "inbox") == "inbox":
            with db.tx() as c:                  # made under the older, looser rule
                c.execute("UPDATE tasks SET dismissed=1 WHERE id=?", (drop["id"],))
            drop = {**drop, "dismissed": 1}
        if dropped and not drop:
            add(f"Pick back up: {title}?", status="inbox",
                notes=f"quiet for {idle_days:.0f} days", origin=drop_origin,
                realm=t.get("realm") or "", source="auto",
                meta={"thread": key, "intent": intent, "kind": "dropped",
                      "repo": m.get("repo"), "next": next_step(0, 0, m.get("branch"), intent=intent)})
            made += 1
        elif drop and not drop["done"] and not drop["dismissed"]:
            try:                                    # keep what else the card carries (carried over, focus time)
                old = json.loads(drop.get("meta") or "{}")
            except ValueError:
                old = {}
            with db.tx() as c:                      # keep the idle days and next step current
                c.execute("UPDATE tasks SET notes=?, meta=? WHERE id=?",
                          (f"quiet for {idle_days:.0f} days",
                           json.dumps({**old, "thread": key, "intent": intent, "kind": "dropped",
                                       "repo": m.get("repo"),
                                       "next": next_step(0, 0, m.get("branch"), intent=intent)}),
                           drop["id"]))

        # Back on it: you are working on a project you *chose* (Today or
        # Parked) - that card is now in progress. Inbox cards stay where they
        # are: touching a project is not deciding to work on it, and moving
        # them filled "in progress" with things nobody picked.
        if now - (t["last_ts"] or 0) <= BACK_ON_IT_SECONDS:
            for c_ in (drop, loop):
                if c_ and not c_["dismissed"] and not c_["done"] \
                        and (c_.get("status") or "inbox") in ("parked", "today"):
                    move(c_["id"], "doing")
                    moved += 1
        elif drop and not drop["done"] and not drop["dismissed"] and not dropped \
                and t["status"] == "active" and (drop.get("status") or "inbox") == "inbox":
            # It came back to life on its own (a commit, a window): the
            # reminder to pick it up has done its job. Cleared, not "done" -
            # you did not finish anything, and Done, the day's count and the
            # streak must not say you did.
            dismiss(drop["id"])
            closed += 1
        # You planned to pick it back up, and you did: worked on the project for
        # a while after choosing it, and have since stepped away. That is the
        # card done - otherwise it sat "in progress" for days (seen live: five
        # cards stuck from Oct 6), and Home showed the same card every day.
        if drop and not drop["done"] and not drop["dismissed"] \
                and (drop.get("status") or "") in ("today", "doing"):
            chosen = drop.get("started") or drop.get("updated") or 0
            if (t["last_ts"] or 0) >= chosen + PICKED_UP_AFTER \
                    and now - (t["last_ts"] or 0) > BACK_ON_IT_SECONDS:
                with db.tx() as c:
                    c.execute("UPDATE tasks SET notes=? WHERE id=?",
                              ("picked back up", drop["id"]))
                move(drop["id"], "done")
                closed += 1
    return {"made": made, "closed": closed, "moved": moved}


def rollover(now: float | None = None) -> int:
    """A new day starts fresh. Cards left on Today or in progress that you
    have not touched since the day began go back to the Inbox, marked as
    carried over, so the day's plan is a choice again instead of a list
    filled once and frozen ("the daily cards seem stuck after one filling").
    Anything you are actually working on - its project active today - stays.
    """
    now = now or time.time()
    day = insights.current_day(datetime.fromtimestamp(now))
    if db.get_state("rollover_day") == day:
        return 0
    start, _ = insights.day_bounds(day)
    active = {r["key"] for r in db.q("SELECT key FROM threads WHERE last_ts >= ?", (start,))}
    moved = 0
    for r in db.q("SELECT * FROM tasks WHERE done=0 AND COALESCE(dismissed,0)=0 "
                  "AND status IN ('today','doing') AND COALESCE(updated, created) < ?", (start,)):
        c = _card(r)
        if (c["meta"] or {}).get("thread") in active:
            continue
        meta = {**(c["meta"] or {}), "carried": datetime.fromtimestamp(
            c.get("updated") or c["created"]).strftime("%Y-%m-%d")}
        with db.tx() as cx:
            cx.execute("UPDATE tasks SET status='inbox', meta=? WHERE id=?", (json.dumps(meta), c["id"]))
        moved += 1
    db.set_state("rollover_day", day)
    if moved:
        lg.info("new day %s: %d unfinished card(s) carried back to the inbox", day, moved)
    return moved


# --------------------------------------------------------------------------- scheduling

SCHEDULE_RX = re.compile(r"^\s*(schedule|block|book|meeting|meet)\b[:\s]*", re.I)
_LEN_RX = re.compile(r"\b(?:for\s+)?(\d+(?:\.\d+)?|an?|half an?)\s*(h|hrs?|hours?|m|mins?|minutes?)\b(?!\s*(?:ago|from))", re.I)


def is_schedule(text: str) -> bool:
    return bool(SCHEDULE_RX.match(text or ""))


def _length(text: str, default: int) -> tuple[str, int]:
    m = _LEN_RX.search(text)
    if not m:
        return text, default
    n = m.group(1).lower()
    qty = 0.5 if n.startswith("half") else 1.0 if n in ("a", "an") else float(n)
    minutes = int(qty * 60) if m.group(2).lower().startswith("h") else int(qty)
    return text[:m.start()] + " " + text[m.end():], max(5, min(minutes, 12 * 60))


def _calendar_label(realm: str) -> str:
    from ..gsuite import auth as gauth
    labels = gauth.labels()
    pref = config.get(f"board.calendar.{realm}") or ("work" if realm == "work" else "personal")
    return pref if pref in labels else (labels[0] if labels else "")


def _next_free(minutes: int, now: datetime | None = None) -> float | None:
    """Start of the next free stretch of `minutes` in working hours, today or
    the next few days, on a quarter hour."""
    from ..gsuite import gcal
    now = now or datetime.now()
    for offset in range(0, 5):
        for s in gcal.free_slots(offset, min_minutes=minutes):
            # the rest of a free stretch that began before `now` still counts
            start = max(datetime.fromtimestamp(s["start"]), now.replace(second=0, microsecond=0))
            q = (start.minute + 14) // 15 * 15
            start = start.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=q)
            if start.timestamp() + minutes * 60 <= s["end"] and start >= now:
                return start.timestamp()
    return None


def _clashes(start: float, minutes: int) -> list[str]:
    end = start + minutes * 60
    return [r["summary"] for r in db.q(
        "SELECT summary FROM gcal WHERE start_ts < ? AND end_ts > ? AND status!='cancelled' "
        "AND COALESCE(all_day,0)=0 AND COALESCE(self_status,'')!='declined'", (end, start))]


def schedule(text: str, now: datetime | None = None) -> dict:
    """'schedule call with Sam tomorrow 4pm for 30 min' -> a calendar event
    (when Google is connected) and, always, a board card with a reminder ten
    minutes before. 'block 2h for atlas' with no time takes the next free
    stretch of working hours."""
    from ..gsuite import gcal
    verb = SCHEDULE_RX.match(text or "")
    body = text[verb.end():] if verb else (text or "")
    rest, minutes = _length(body, 60 if verb and verb.group(1).lower() == "block" else 30)
    title, start, has_time = parse_when(rest, now)
    title = re.sub(r"^\s*(a|an|for|on)\s+", "", title, flags=re.I).strip() or "Focus block"
    if verb and verb.group(1).lower() == "block" and not title.lower().startswith(("focus", "block")):
        title = f"Focus: {title}"
    if not has_time:
        # No clock time: the first free stretch from now - or from the start
        # of the day that was named ("block 2h for atlas tomorrow").
        frm = now or datetime.now()
        if start and datetime.fromtimestamp(start).date() > frm.date():
            frm = datetime.combine(datetime.fromtimestamp(start).date(), datetime.min.time())
        start = _next_free(minutes, frm)
        if start is None:
            raise ValueError("no free stretch that long in the next few days - give a time")
    realm = classify.realm_of(title)
    label = _calendar_label(realm)
    clashes = _clashes(start, minutes)
    result = gcal.create_event(title, datetime.fromtimestamp(start), minutes,
                               description="Scheduled from ARGUS", label=label) if label else "not connected"
    created = result.startswith("http") or result == "created"
    card = add(title, status="today" if datetime.fromtimestamp(start).date() <= datetime.now().date()
               else "inbox", due=start, source="schedule",
               notes=f"{minutes} min" + (f" on the {label} calendar" if created else ""),
               meta={"event": result if created else "", "minutes": minutes})
    card = set_reminder(card["id"], start - 10 * 60 if start - 600 > time.time() else start)
    when = datetime.fromtimestamp(start).strftime("%a %d %b, %H:%M")
    if created:
        msg = f"Scheduled “{title}” {when} for {minutes} min on your {label} calendar."
    elif result in ("not connected",) or "invalid_grant" in result or "expired" in result.lower():
        msg = (f"Google is signed out, so it is not on your calendar yet - “{title}” {when} "
               f"is on your board with a reminder. Run `argus google relogin` to fix the calendar.")
    else:
        msg = f"Calendar said: {result[:120]}. “{title}” {when} is on your board with a reminder."
    if clashes:
        msg += f" Heads up: it overlaps {', '.join(clashes[:2])}."
    return {"card": card, "event": result if created else None, "message": msg,
            "start": start, "minutes": minutes, "calendar": label if created else None}


def time_block(ids: list[int], minutes: int = 60, now: datetime | None = None) -> dict:
    """Give each chosen card the next free stretch of working hours, in the
    order picked: a calendar event when Google is connected, and always a due
    time and a reminder five minutes before."""
    from ..gsuite import gcal
    frm = now or datetime.now()
    placed, on_calendar = [], 0
    for cid in ids:
        card = get(int(cid))
        if not card or card["status"] == "done":
            continue
        start = _next_free(minutes, frm)
        if start is None:
            break
        label = _calendar_label(card.get("realm") or classify.realm_of(card["title"]))
        res = gcal.create_event(card["title"], datetime.fromtimestamp(start), minutes,
                                description="Time-blocked from ARGUS", label=label) if label else ""
        ok = res.startswith("http") or res == "created"
        on_calendar += ok
        meta = {**(card.get("meta") or {}), "block": {"start": start, "minutes": minutes,
                                                      "event": res if ok else ""}}
        with db.tx() as c:
            c.execute("UPDATE tasks SET due=?, meta=?, updated=? WHERE id=?",
                      (start + minutes * 60, json.dumps(meta), time.time(), card["id"]))
        set_reminder(card["id"], max(time.time() + 60, start - 5 * 60))
        placed.append({"id": card["id"], "title": card["title"], "start": start})
        frm = datetime.fromtimestamp(start + minutes * 60)
    when = ", ".join(f"{datetime.fromtimestamp(x['start']):%H:%M} {x['title'][:30]}" for x in placed)
    msg = (f"Blocked {len(placed)} card(s): {when}." if placed else "No free time left to block today.")
    if placed and on_calendar < len(placed):
        msg += " Google is signed out, so they are reminders, not calendar events yet."
    return {"placed": placed, "on_calendar": on_calendar, "message": msg}


# --------------------------------------------------------------------------- resume

def _uniq(xs, n: int) -> list:
    out = []
    for x in xs:
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
        if len(out) >= n:
            break
    return out


def next_step(dirty: int | None, ahead: int | None, branch: str | None,
              changed: list[str] | None = None, intent: str = "", ci_failing: bool = False) -> str:
    """The one concrete thing to do next on a project, from its state.

    In order of what blocks others or loses work: red CI, uncommitted
    changes, unpushed commits, then the last thing you were asking Claude
    to do. Plain and specific - a file name, a branch, a count."""
    b = branch or "the branch"
    if ci_failing:
        return f"Fix the failing CI on {b}"
    if dirty:
        first = (changed or [""])[0].replace("\\", "/").split("/")[-1]
        return f"Commit {dirty} changed file{'s' if dirty != 1 else ''} on {b}" + (
            f" ({first}{', ...' if dirty > 1 else ''})" if first else "")
    if ahead:
        return f"Push {ahead} commit{'s' if ahead != 1 else ''} on {b}"
    intent = re.sub(r"^session:\s*", "", (intent or "").strip(), flags=re.I)
    if intent:
        return f"Continue: {intent[:90]}"
    return "Open it and decide: finish, park or drop"


def _thread_key_for(card: dict) -> str | None:
    key = (card.get("meta") or {}).get("thread")
    if key:
        return key
    # A card you typed yourself: the project whose name it mentions, if any.
    title = (card.get("title") or "").lower()
    best = None
    for r in db.q("SELECT key, title FROM threads ORDER BY score DESC LIMIT 60"):
        name = (r["title"] or r["key"] or "").lower()
        if len(name) >= 4 and name in title and (best is None or len(name) > len(best[1])):
            best = (r["key"], name)
    return best[0] if best else None


def _changed_files(repo: str, limit: int = 12) -> list[str]:
    from ..util import run
    rc, out, _ = run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=normal"],
                     timeout=10)
    if rc != 0:
        return []
    return [line[3:].strip().strip('"') for line in out.splitlines() if line.strip()][:limit]


def resume(card_id: int) -> dict:
    """Exactly where you were on a card's project: the state on disk and the
    last things you did there, newest first."""
    from . import threads
    card = get(card_id)
    if card is None:
        raise KeyError(card_id)
    key = _thread_key_for(card)
    out: dict = {"card": card, "project": None}
    if not key:
        return out
    t = threads.context_for(key)
    if not t:
        return out
    by = t.get("by_source") or {}
    ev = lambda src, n: _uniq((x.get("evidence") for x in by.get(src, [])), n)  # noqa: E731
    m = t.get("meta") or {}
    repo = m.get("repo")
    asks = [a for a in ev("claude", 8) if not a.lower().startswith("session:")][:4]
    session = next((a[8:].strip() for a in ev("claude", 20) if a.lower().startswith("session:")), "")
    out["project"] = {
        "key": key, "title": t.get("title_user") or t.get("title") or key,
        "realm": t.get("realm"), "last_seen": human_ago(t["last_ts"]) if t.get("last_ts") else "",
        "repo": repo, "branch": m.get("branch"), "dirty": m.get("dirty"), "ahead": m.get("ahead"),
        "changed": _changed_files(repo) if repo and Path(repo).is_dir() else [],
        "commits": ev("git", 3),
        "files": ev("file", 8),
        "asks": asks, "session": session,
        "windows": [w.split(": ", 1)[-1] for w in ev("window", 4)],
        "pages": ev("browser", 4),
        "time": human_dur(t.get("seconds_total") or 0),
    }
    pr = out["project"]
    ci = db.q1("SELECT 1 FROM tasks WHERE origin LIKE 'gh-ci:%' AND done=0 AND "
               "COALESCE(dismissed,0)=0 AND lower(title) LIKE ?", (f"%{key.split('-')[0]}%",))
    pr["next"] = next_step(pr["dirty"], pr["ahead"], pr["branch"], pr["changed"],
                           (asks or [session])[0] if (asks or session) else "", bool(ci))
    try:
        out["project"]["claude_session"] = last_claude_session(key)
    except Exception:
        out["project"]["claude_session"] = None
    return out


_SID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def last_claude_session(key: str, scan: int = 300) -> dict | None:
    """The newest Claude Code session whose working folder is this project.

    A transcript is ~/.claude/projects/<folder>/<session-id>.jsonl, and its
    lines carry the session's cwd; the newest file for the project is the
    one to resume."""
    import glob
    import os
    from . import threads
    base = Path.home() / ".claude" / "projects"
    files = sorted(glob.glob(str(base / "*" / "*.jsonl")), key=lambda f: -os.path.getmtime(f))
    for fp in files[:scan]:
        sid = Path(fp).stem
        if not _SID.match(sid):
            continue
        cwd = title = ""
        try:
            with open(fp, "rb") as fh:
                for i, raw in enumerate(fh):
                    if i > 400 or (cwd and title):
                        break
                    if len(raw) > 262_144:
                        continue
                    if not cwd and b'"cwd"' in raw:
                        try:
                            cwd = json.loads(raw).get("cwd") or ""
                        except ValueError:
                            pass
                    if not title and b'"ai-title"' in raw:
                        try:
                            title = json.loads(raw).get("aiTitle") or ""
                        except ValueError:
                            pass
        except OSError:
            continue
        if cwd and threads.key_from_path(cwd) == key:
            return {"id": sid, "cwd": cwd, "title": title,
                    "when": human_ago(os.path.getmtime(fp))}
    return None


def resume_claude(card_id: int) -> str:
    """Open a terminal in the project running `claude --resume <session>`."""
    import shutil
    import subprocess
    card = get(card_id)
    if card is None:
        raise KeyError(card_id)
    key = _thread_key_for(card)
    s = last_claude_session(key) if key else None
    if not s:
        raise ValueError("no Claude Code session found for this project")
    if not _SID.match(s["id"]) or not Path(s["cwd"]).is_dir():
        raise ValueError("that session's folder is gone")
    claude = shutil.which("claude") or "claude"
    wt = shutil.which("wt")
    if wt:
        subprocess.Popen([wt, "-d", s["cwd"], claude, "--resume", s["id"]])
    else:
        subprocess.Popen(["cmd", "/c", "start", "", "/d", s["cwd"], claude, "--resume", s["id"]])
    return f"resuming “{s['title'] or s['id'][:8]}” in {Path(s['cwd']).name}"


def open_project(card_id: int, how: str = "code") -> str:
    """Open the card's repository in VS Code (or the folder in Explorer)."""
    import shutil
    import subprocess
    info = resume(card_id).get("project") or {}
    repo = info.get("repo")
    if not repo or not Path(repo).is_dir():
        raise ValueError("this card has no project folder on this machine")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if how == "folder":
        subprocess.Popen(["explorer", repo])
        return f"opened {repo}"
    code = shutil.which("code") or shutil.which("code.cmd")
    if not code:
        raise ValueError("VS Code's `code` command is not on PATH")
    subprocess.Popen([code, repo], creationflags=flags)
    return f"opened {repo} in VS Code"


# --------------------------------------------------------------------------- views

def board(now: float | None = None) -> dict:
    now = now or time.time()
    try:
        rollover(now)
        sync(now)
    except Exception:
        lg.exception("board sync failed")
    rows = db.q("SELECT * FROM tasks WHERE COALESCE(dismissed,0)=0 AND "
                "(done=0 OR done_ts > ?) ORDER BY COALESCE(pinned,0) DESC, sort, created DESC",
                (now - DONE_SHOWN_DAYS * 86400,))
    cols: dict[str, list] = {c: [] for c in COLUMNS}
    for r in rows:
        card = _card(r)
        card["display"] = friendly(card["title"])
        cols.setdefault(card["status"], []).append(card)
    for c in ("today", "inbox"):
        cols[c].sort(key=lambda d: (-(d.get("pinned") or 0), not d["overdue"],
                                    d.get("due") or 9e18, d.get("sort") or 0))
    cols["done"].sort(key=lambda d: -(d.get("done_ts") or 0))
    day = insights.current_day()
    s, _ = insights.day_bounds(day)
    done_today = sum(1 for d in cols["done"] if (d.get("done_ts") or 0) >= s)
    planned = len(cols["today"]) + len(cols["doing"]) + done_today
    return {"columns": cols, "day": day, "planned": db.get_state("plan_day") == day,
            "progress": {"done": done_today, "total": planned}}


def plan(now: float | None = None) -> dict:
    """What the morning ritual offers, in the order it is worth looking at."""
    b = board(now)
    cols = b["columns"]
    # still on your plate: what is open today, plus yesterday's leftovers
    carried = cols["today"] + cols["doing"] + [c for c in cols["inbox"] if (c.get("meta") or {}).get("carried")]
    pool = [c for c in cols["inbox"] + cols["parked"] if c not in carried]
    due = [c for c in pool if c.get("due") and c["due"] <= time.time() + 86400]
    loops = [c for c in pool if c["auto"] and c["status"] == "inbox" and c not in due]
    rest = [c for c in pool if c not in due and c not in loops]
    events = []
    s, e = insights.day_bounds(b["day"])
    try:
        for r in db.q("SELECT summary, start_ts, end_ts, all_day FROM gcal WHERE start_ts BETWEEN ? AND ? "
                      "AND status!='cancelled' AND COALESCE(self_status,'')!='declined' "
                      "ORDER BY start_ts LIMIT 12", (s, e)):
            events.append({"summary": r["summary"], "all_day": bool(r["all_day"]),
                           "at": datetime.fromtimestamp(r["start_ts"]).strftime("%H:%M")})
    except Exception:
        pass
    from . import digest
    synced = digest.last_synced("calendar")
    return {"day": b["day"], "planned": b["planned"], "carried": carried, "due": due,
            "loops": loops, "rest": rest[:15], "events": events,
            "calendar_stale": bool(synced and time.time() - synced > digest.STALE_AFTER),
            "yesterday": _yesterday(b["day"]), "sharp": sharp_hours()}


def _yesterday(day: str) -> dict:
    d = datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)
    s, e = insights.day_bounds(d.strftime("%Y-%m-%d"))
    done = db.q1("SELECT COUNT(*) n FROM tasks WHERE done=1 AND done_ts >= ? AND done_ts < ?",
                 (s, e))["n"]
    t = insights.totals(s, e)
    return {"day": d.strftime("%A"), "done": done,
            "active": human_dur(t["active_seconds"]),
            "productive": human_dur(t["productive_seconds"])}


def commit_plan(ids: list[int]) -> dict:
    """Start the day: the chosen cards are Today, in the order picked."""
    now = time.time()
    for i, cid in enumerate(ids):
        c = get(int(cid))
        if c and c["status"] not in ("doing", "done"):
            with db.tx() as cx:
                cx.execute("UPDATE tasks SET status='today', done=0, sort=?, updated=? "
                           "WHERE id=?", (-now + i * 0.001 - 1e6, now, c["id"]))
    db.set_state("plan_day", insights.current_day())
    return board()


def summary_line() -> str:
    """For briefings and the tray: what is on today's list."""
    b = board()
    today = b["columns"]["today"] + b["columns"]["doing"]
    if not today:
        return ""
    return ("TODAY'S LIST: " + "; ".join(
        f"{c['title']}" + (" (overdue)" if c["overdue"] else "") for c in today[:8])
        + f" - {b['progress']['done']} of {b['progress']['total']} done")


# --------------------------------------------------------------------------- end of day

def timesheet_draft(day: str | None = None) -> dict:
    """Today's timesheet entry, ready to paste: work hours, the work projects
    and what shipped on each (finished cards and commits). Deterministic - the
    same day always gives the same text - and office work only."""
    from . import timesheet
    day = day or insights.current_day()
    row = timesheet.day_row(day, realm="work")
    s, e = insights.day_bounds(day)
    shipped: dict[str, list[str]] = {}
    for r in db.q("SELECT title, meta, realm FROM tasks WHERE done=1 AND done_ts >= ? AND done_ts < ? "
                  "AND COALESCE(dismissed,0)=0", (s, e)):
        if (r["realm"] or "") == "personal":
            continue
        title = re.sub(r"^(Pick back up: |Review: |CI failing: |Changes requested: )", "",
                       r["title"]).rstrip("?")
        proj = title.split(":")[0].strip() if ":" in title else "other"
        shipped.setdefault(proj, []).append(title.split(":", 1)[-1].strip())
    for c in row["commits"]:
        repo, _, msg = c.partition(": ")
        shipped.setdefault(repo, []).append(msg.split("\n")[0][:90])
    projects = row["projects"] or list(shipped)
    lines = [f"{datetime.strptime(day, '%Y-%m-%d'):%a %d %b} - {human_dur(row['work_seconds'])} "
             f"({row['work_seconds'] / 3600:.1f}h)"]
    if row["first"]:
        lines[0] += (f", {datetime.fromtimestamp(row['first']):%H:%M}-"
                     f"{datetime.fromtimestamp(row['last']):%H:%M}")
    for proj in dict.fromkeys(list(projects) + list(shipped)):
        items = list(dict.fromkeys(shipped.get(proj, [])))[:5]
        lines.append(f"- {proj}" + (": " + "; ".join(items) if items else ""))
    if len(lines) == 1:
        lines.append("- (no office work recorded today)")
    return {"day": day, "hours": round(row["work_seconds"] / 3600, 2),
            "text": "\n".join(lines), "projects": projects}


def shutdown(now: float | None = None) -> dict:
    """What the end-of-day screen offers: unfinished cards and the draft."""
    b = board(now)
    open_ = b["columns"]["doing"] + b["columns"]["today"]
    s, _ = insights.day_bounds(b["day"])
    done = [c for c in b["columns"]["done"] if (c.get("done_ts") or 0) >= s]
    return {"day": b["day"], "open": open_, "done": done, "draft": timesheet_draft(b["day"])}


def close_day(items: list[dict]) -> dict:
    """Apply the end-of-day choices: park (with a note), tomorrow, or done."""
    for it in items:
        cid, action, note = int(it.get("id")), it.get("action"), (it.get("note") or "").strip()
        if action == "done":
            move(cid, "done")
        elif action == "park":
            park(cid, note)
        elif action == "tomorrow":
            if note:
                with db.tx() as c:
                    c.execute("UPDATE tasks SET park_note=? WHERE id=?", (note[:500], cid))
            move(cid, "today")
    db.set_state("shutdown_day", insights.current_day())
    return board()


# --------------------------------------------------------------------------- meetings

_STOP = {"meeting", "sync", "call", "with", "weekly", "daily", "update", "review", "the", "and",
         "for", "team", "standup", "discussion", "catch", "about", "from", "into", "this", "that"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9-]{3,}", (text or "").lower()) if w not in _STOP}


def meeting_prep(summary: str, attendees: list[str] | None = None, me: str = "") -> list[str]:
    """Up to three lines worth reading before a meeting: the project it is
    about (state and next step), your open cards on it, and unread mail from
    the people in it."""
    lines: list[str] = []
    words = _words(summary)
    best = None
    for r in db.q("SELECT key, title, meta FROM threads ORDER BY score DESC LIMIT 80"):
        name = (r["title"] or r["key"] or "").lower()
        if len(name) >= 4 and (name in (summary or "").lower() or r["key"] in words):
            best = r
            break
    if best:
        try:
            m = json.loads(best["meta"] or "{}")
        except ValueError:
            m = {}
        lines.append(f"{best['title'] or best['key']}: " + next_step(
            m.get("dirty"), m.get("ahead"), m.get("branch"), intent=_last_intent_for(best["key"])))
    cards = [r["title"] for r in db.q(
        "SELECT title FROM tasks WHERE done=0 AND COALESCE(dismissed,0)=0 ORDER BY updated DESC LIMIT 200")
        if words & _words(r["title"])][:2]
    if cards:
        lines.append("Open: " + "; ".join(cards))
    people = [a.lower() for a in (attendees or []) if a and a.lower() != (me or "").lower()]
    if people:
        mail = db.q("SELECT sender, subject FROM gmail WHERE unread=1 AND lower(sender_email) IN ("
                    + ",".join("?" * len(people)) + ") ORDER BY ts DESC LIMIT 2", people)
        if mail:
            lines.append("Unread from them: " + "; ".join(
                f"{r['sender']}: {r['subject'][:50]}" for r in mail))
    return lines[:3]


def _last_intent_for(key: str) -> str:
    r = db.q1("SELECT tt.evidence FROM thread_touches tt JOIN threads t ON t.id=tt.thread_id "
              "WHERE t.key=? AND tt.source='claude' ORDER BY tt.ts DESC LIMIT 1", (key,))
    return (r["evidence"] or "") if r else ""


# --------------------------------------------------------------------------- focus

def focus_state(now: float | None = None) -> dict | None:
    """The running focus session, if any: {card, title, started, until}."""
    f = db.get_state("focus") or None
    if not f:
        return None
    f = dict(f)
    f["left"] = max(0, f["until"] - (now or time.time()))
    return f


def start_focus(card_id: int, minutes: int = 50) -> dict:
    card = move(card_id, "doing")
    now = time.time()
    f = {"card": card["id"], "title": card["title"], "started": now,
         "until": now + max(5, min(int(minutes), 180)) * 60, "minutes": int(minutes)}
    db.set_state("focus", f)
    return focus_state()


def stop_focus(now: float | None = None) -> dict | None:
    """End the session (early or on time) and log it on the card."""
    f = focus_state(now)
    if not f:
        return None
    now = now or time.time()
    spent = round((min(now, f["until"]) - f["started"]) / 60)
    card = get(f["card"])
    if card:
        meta = card.get("meta") or {}
        meta["focus_sessions"] = int(meta.get("focus_sessions") or 0) + 1
        meta["focus_minutes"] = int(meta.get("focus_minutes") or 0) + spent
        with db.tx() as c:
            c.execute("UPDATE tasks SET meta=? WHERE id=?", (json.dumps(meta), card["id"]))
    db.set_state("focus", None)
    return {**f, "spent": spent}


# --------------------------------------------------------------------------- the week

STALE_DAYS = 7


def week(offset: int = 0, now: datetime | None = None) -> dict:
    """One week (Mon-Sun) of output: what shipped, where the hours went, the
    finish streak, and the cards that went stale - the weekly review."""
    from . import timesheet
    today = datetime.strptime(insights.current_day(now), "%Y-%m-%d")
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    days = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    s, _ = insights.day_bounds(days[0])
    _, e = insights.day_bounds(days[-1])

    shipped: dict[str, list[str]] = {}
    per_day = []
    for d in days:
        ds, de = insights.day_bounds(d)
        done = [r["title"] for r in db.q(
            "SELECT title FROM tasks WHERE done=1 AND done_ts >= ? AND done_ts < ? "
            "AND COALESCE(dismissed,0)=0", (ds, de))]
        row = timesheet.day_row(d, realm="all") if ds < time.time() else None
        realms = insights.by_realm(ds, min(de, time.time())) if ds < time.time() else {}
        per_day.append({"day": d, "done": len(done),
                        "hours": {k: round(v["seconds"] / 3600, 2) for k, v in realms.items()},
                        "active": round((row or {}).get("active_seconds", 0) / 3600, 2)})
        for title in done:
            if title.startswith(("Pick back up:", "Review:", "CI failing:", "Changes requested:")):
                continue
            proj = title.split(":")[0].strip() if ":" in title else "other"
            shipped.setdefault(proj, []).append(title.split(":", 1)[-1].strip())
    realm_of: dict[str, str] = {}
    for r in db.q("SELECT subject, detail, meta FROM events WHERE source='git' AND kind='commit' "
                  "AND ts >= ? AND ts < ? ORDER BY ts", (s, e)):
        proj = r["subject"] or "other"
        shipped.setdefault(proj, []).append((r["detail"] or "").split("\n")[0][:90])
        try:
            path = json.loads(r["meta"] or "{}").get("path", "")
        except ValueError:
            path = ""
        if realm_of.get(proj) != "work":
            realm_of[proj] = classify.realm_of(f"{proj} {path}")

    totals: dict[str, float] = {}
    for d in per_day:
        for k, v in d["hours"].items():
            totals[k] = round(totals.get(k, 0) + v, 2)

    streak = 0
    for d in reversed([x for x in per_day if x["day"] <= insights.current_day(now)]):
        if d["done"]:
            streak += 1
        elif d["day"] != insights.current_day(now):
            break

    cutoff = time.time() - STALE_DAYS * 86400
    stale = [_card(r) for r in db.q(
        "SELECT * FROM tasks WHERE done=0 AND COALESCE(dismissed,0)=0 AND status IN ('inbox','parked') "
        "AND COALESCE(updated, created) < ? ORDER BY COALESCE(updated, created) LIMIT 30", (cutoff,))]

    lines = [f"Week of {monday:%d %b} - {(monday + timedelta(days=6)):%d %b}"]
    # Office first (the part a manager reads), personal projects after.
    def section(header: str, projs: list[str]) -> None:
        if not projs:
            return
        lines.append(header)
        for proj in sorted(projs, key=lambda p: -len(shipped[p])):
            items = list(dict.fromkeys(shipped[proj]))
            lines.append(f"- {proj}: " + "; ".join(items[:6])
                         + (f" (+{len(items) - 6} more)" if len(items) > 6 else ""))
    for proj in shipped:
        realm_of.setdefault(proj, classify.realm_of(proj))
    office = [p for p in shipped if realm_of[p] == "work"]
    section(f"Office ({totals.get('work', 0):.1f}h):", office)
    section("Personal:", [p for p in shipped if p not in office])
    if len(lines) == 1:
        lines.append("- nothing finished or committed yet this week")
    return {"from": days[0], "to": days[-1], "days": per_day, "totals": totals,
            "shipped": {k: list(dict.fromkeys(v)) for k, v in shipped.items()},
            "realms": realm_of, "streak": streak, "stale": stale, "report": "\n".join(lines)}


def review(decisions: list[dict]) -> dict:
    """The weekly review's choices for stale cards: keep, today, or drop."""
    for d in decisions:
        cid, action = int(d["id"]), d.get("action")
        if action == "today":
            move(cid, "today")
        elif action == "drop":
            dismiss(cid)
        elif action == "keep":
            with db.tx() as c:
                c.execute("UPDATE tasks SET updated=? WHERE id=?", (time.time(), cid))
    db.set_state("review_week", insights.current_day())
    return week()


# --------------------------------------------------------------------------- what now

WHAT_NOW_RX = re.compile(r"\b(what (should|do) i (do|work on)|what('s| is) next|what now|"
                         r"where (should|do) i start|next task|what to do)\b", re.I)


def is_what_now(text: str) -> bool:
    return bool(WHAT_NOW_RX.search(text or ""))


def sharp_hours() -> list[int]:
    try:
        return insights.peak_hours()
    except Exception:
        return []


def what_now(now: datetime | None = None) -> str:
    """One recommendation, from the board, with the reason - no model needed."""
    now = now or datetime.now()
    f = focus_state()
    if f and f["left"] > 0:
        return f"Keep going on “{friendly(f['title'])}” - {int(f['left'] // 60)} minutes left in this focus session."
    b = board()
    cols = b["columns"]
    peak = sharp_hours()
    in_peak = now.hour in peak[:2]
    hint = (" You're in one of your sharpest hours - good time for the hardest one." if in_peak else "")
    late = [c for c in cols["today"] + cols["doing"] + cols["inbox"] if c["overdue"]]
    if late:
        c = late[0]
        return f"Overdue first: “{friendly(c['title'])}”." + (f" Next step: {c['meta'].get('next')}." if c["meta"].get("next") else "")
    if cols["doing"]:
        c = cols["doing"][0]
        nxt = (c.get("meta") or {}).get("next")
        return (f"Finish what you started: “{friendly(c['title'])}”." + (f" Next step: {nxt}." if nxt else "")
                + (f" You left a note: {c['park_note']}" if c.get("park_note") else "") + hint)
    if cols["today"]:
        c = cols["today"][0]
        nxt = (c.get("meta") or {}).get("next")
        more = len(cols["today"]) - 1
        return (f"Top of today's list: “{friendly(c['title'])}”." + (f" Next step: {nxt}." if nxt else "")
                + (f" Then {more} more." if more else "") + hint)
    pick = next((c for c in cols["inbox"] if (c.get("meta") or {}).get("kind") == "loop"), None)         or (cols["inbox"][0] if cols["inbox"] else None)
    if pick:
        nxt = (pick.get("meta") or {}).get("next")
        return (f"Nothing planned yet. Best candidate: “{friendly(pick['title'])}”." +
                (f" Next step: {nxt}." if nxt else "") + " Or press Plan my day." + hint)
    return "Your board is empty. Capture what's on your mind (Ctrl+Alt+Space), then plan the day."


# --------------------------------------------------------------------------- home

def friendly(title: str) -> str:
    """Card titles as a person would say them."""
    t = title or ""
    m = re.match(r"^Pick back up: (.+?)\??$", t)
    if m:
        return f"Unfinished: {m.group(1)}"
    m = re.match(r"^(.+?): finish (.+)$", t)
    if m and "/" in m.group(2) or (m and m.group(2) in ("main", "master", "testing", "develop")):
        return f"Finish your work on {m.group(1)} ({m.group(2)})"
    return t


def _now_card(cols: dict, now: datetime) -> dict:
    f = focus_state()
    if f and f["left"] > 0:
        c = get(f["card"]) or {"id": f["card"], "title": f["title"], "meta": {}}
        return {"kind": "focus", "card": c, "headline": friendly(c["title"]),
                "why": f"Focus session - {int(f['left'] // 60)} min left", "action": "stop"}
    late = [c for c in cols["today"] + cols["doing"] + cols["inbox"] if c["overdue"]]
    pick, kind, why = None, "", ""
    if late:
        pick, kind, why = late[0], "overdue", "This is overdue"
    elif cols["doing"]:
        pick, kind, why = cols["doing"][0], "doing", "You started this and haven't finished"
    elif cols["today"]:
        pick, kind, why = cols["today"][0], "today", "First thing on today's list"
    else:
        pick = next((c for c in cols["inbox"] if (c.get("meta") or {}).get("kind") == "loop"), None) \
            or (cols["inbox"][0] if cols["inbox"] else None)
        kind, why = "candidate", "Nothing planned yet - this is the best place to start"
    if not pick:
        return {"kind": "empty", "card": None, "headline": "You're all clear",
                "why": "Write down what's on your mind below, and it will show up here.",
                "action": "capture"}
    peak = now.hour in sharp_hours()[:2]
    return {"kind": kind, "card": pick, "headline": friendly(pick["title"]),
            "why": why + (" - and it's one of your sharpest hours" if peak else ""),
            "next": (pick.get("meta") or {}).get("next") or pick.get("park_note") or "",
            # unfinished code work: the useful first move is getting back into
            # it (the panel with "Resume Claude session" / "Open in VS Code")
            "action": "resume" if (pick.get("meta") or {}).get("kind") in ("loop", "dropped")
                      else ("done" if kind == "doing" else "start")}


def home(now: datetime | None = None) -> dict:
    """Everything the Home page shows, in plain words."""
    now = now or datetime.now()
    b = board()
    cols = b["columns"]
    s, _ = insights.day_bounds(b["day"])
    soon = time.time() + 12 * 3600
    for col in cols.values():
        for c in col:
            c["display"] = friendly(c["title"])
    today = cols["doing"] + cols["today"]
    done_today = [c for c in cols["done"] if (c.get("done_ts") or 0) >= s]
    needs = [c for c in cols["inbox"] + cols["parked"]
             if c["overdue"] or (c.get("origin") or "").startswith("gh-")
             or (c.get("remind_at") and c["remind_at"] <= soon)]
    hanging = [c for c in cols["inbox"] if c["auto"] and c not in needs]
    mine = [c for c in cols["inbox"] if not c["auto"] and c not in needs]
    health = db.get_state("health_summary", {}) or {}
    return {"day": b["day"], "planned": b["planned"], "now": _now_card(cols, now),
            "today": today, "done_today": done_today, "needs": needs,
            "inbox": mine, "hanging": hanging, "parked": cols["parked"],
            "issues": len(health.get("fails") or []), "progress": b["progress"],
            "wrapped": db.get_state("shutdown_day") == b["day"],
            "name": (config.get("identity.user_name") or "").strip()}
