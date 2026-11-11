"""ARGUS command line.

  argus start | stop | status | run       daemon control
  argus today | yesterday | day 2026-09-08
  argus brief | wrap | retro              generate a digest now
  argus ask "how did I spend Tuesday?"
  argus watch                             live activity tail
  argus google login --label work | google relogin | google list | google sync
  argus task add "…" | task list | task done 3
  argus resume                            where you left off
  argus repos | mail | cal | privacy
  argus install | uninstall               run at login
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

from . import config, db
from .paths import LOGS
from .util import human_ago, human_dur

C = {
    "reset": "\033[0m", "dim": "\033[2m", "b": "\033[1m",
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "mag": "\033[35m", "blue": "\033[34m", "grey": "\033[90m",
}
if os.name == "nt":
    os.system("")  # enable ANSI escapes on legacy consoles
    # The default cp1252 console codec cannot render the box and block glyphs
    # used in the reports, so force UTF-8 on both streams.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def c(text: str, colour: str) -> str:
    return f"{C.get(colour, '')}{text}{C['reset']}"


def hr(title: str = "") -> None:
    line = "─" * 62
    print(c(f"\n{title}\n{line}" if title else line, "grey"))


# --------------------------------------------------------------------------- daemon

def cmd_start(args) -> int:
    from .daemon import daemon_alive, start_background
    if daemon_alive():
        print(c("ARGUS is already running.", "yellow"))
        return 0
    if start_background():
        port = config.get("daemon.web_port", 7842)
        print(c("ARGUS is up.", "green"), f"Dashboard: http://127.0.0.1:{port}")
        return 0
    print(c("Started, but no heartbeat yet. Check:", "yellow"), LOGS / "argus.log")
    return 1


def cmd_run(args) -> int:
    os.environ["ARGUS_CONSOLE"] = "1"
    from .daemon import main
    return main(with_web=not args.no_web, with_tray=not args.no_tray)


def cmd_stop(args) -> int:
    from .daemon import stop_running
    graceful, killed = stop_running()
    if graceful and not killed:
        print(c("Stopped cleanly.", "green"))
    elif killed:
        print(c(f"Stopped {killed} process(es)" + ("." if graceful else " (forced)."),
                "green"))
    else:
        print("No ARGUS daemon found.")
    return 0


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(2)
    return cmd_start(args)


def cmd_status(args) -> int:
    from .daemon import daemon_alive
    alive = daemon_alive()
    hb = float(db.get_state("daemon_heartbeat", 0) or 0)
    print(c("\n  A R G U S", "cyan"), c("v1.0", "grey"))
    print(f"  daemon      {c('running', 'green') if alive else c('stopped', 'red')}"
          + (f"  {c('(last beat ' + human_ago(hb) + ')', 'grey')}" if hb else ""))
    print(f"  database    {db.db_size_mb()} MB  "
          f"{db.q1('SELECT COUNT(*) n FROM events')['n']} events, "
          f"{db.q1('SELECT COUNT(*) n FROM activity')['n']} sessions")

    from .brain import llm
    print(f"  brain       {', '.join(llm.available()) or c('none available', 'red')}"
          f"  {c(f'({llm.calls_today()} calls today)', 'grey')}")

    from .gsuite import auth as gauth
    if gauth.is_connected():
        # Token health from what the last sync learned - no network here.
        labels = gauth.labels()
        dead = [lb for lb in labels if gauth.token_state(lb).get("state") == "expired"]
        if dead:
            g = (c(f"{len(dead)} of {len(labels)} account(s) need a fresh login", "red")
                 + c("  → argus google list", "grey"))
        else:
            g = c("connected", "green") + c(f"  {', '.join(labels)}", "grey")
    elif gauth.is_configured():
        g = c("client set up, not logged in", "yellow")
    else:
        g = c("not configured", "grey") + "  → argus google login"
    print(f"  google      {g}")

    from .actions import tray
    if tray.is_paused():
        until = datetime.fromtimestamp(float(db.get_state("paused_until", 0)))
        print(f"  {c('PAUSED', 'yellow')}      until {until:%H:%M}")

    s, e, day = _range_for("today")
    from .brain import insights
    f = insights.focus_score(s, e)
    print(f"\n  today       active {human_dur(f['active_seconds'])}, "
          f"productive {human_dur(f['productive_seconds'])}"
          + (f", focus {c(str(f['score']), 'cyan')}/100" if f.get("score") else ""))

    unacked = db.q1("SELECT COUNT(*) n FROM alerts WHERE acked=0")["n"]
    if unacked:
        print(f"  alerts      {c(str(unacked) + ' unread', 'yellow')}  → argus alerts")
    problems = config.problems()
    if problems:
        print(f"  config      {c(problems[0][:70], 'yellow')}  → argus doctor")
    print(f"\n  dashboard   http://127.0.0.1:{config.get('daemon.web_port', 7842)}")
    print(c("  health      argus doctor\n", "grey"))
    return 0


# --------------------------------------------------------------------------- reporting

def _range_for(which: str, day: str | None = None):
    from .brain import insights
    if which == "today":
        return insights.day_range()
    if which == "yesterday":
        today = datetime.strptime(insights.current_day(), "%Y-%m-%d")
        return insights.day_range((today - timedelta(days=1)).strftime("%Y-%m-%d"))
    return insights.day_range(day)


def cmd_today(args) -> int:
    from .brain import insights
    day = getattr(args, "date", None)
    which = "day" if day else args.which
    s, e, day = _range_for(which, day)
    f = insights.focus_score(s, e)

    hr(f"  {datetime.strptime(day, '%Y-%m-%d'):%A %d %B}")
    from .brain import timesheet
    row = timesheet.day_row(day)
    if row["first"]:
        # From the timesheet row, so the hours match the in/out beside them
        # (a timesheet day runs from identity.day_starts_at, not midnight).
        print(f"  day          {datetime.fromtimestamp(row['first']):%H:%M} – "
              f"{datetime.fromtimestamp(row['last']):%H:%M}"
              f"   {c('work ' + human_dur(row['work_seconds']), 'cyan')}"
              f" of {human_dur(row['active_seconds'])} active")
    print(f"  at desk      {human_dur(f['at_desk_seconds'])}")
    print(f"  active       {human_dur(f['active_seconds'])}")
    print(f"  productive   {c(human_dur(f['productive_seconds']), 'green')} "
          f"({f.get('productive_pct', 0)}%)")
    if f["distracting_seconds"]:
        print(f"  distracted   {c(human_dur(f['distracting_seconds']), 'yellow')}")
    if f.get("score") is not None:
        bar = "█" * int(f["score"] / 5) + "░" * (20 - int(f["score"] / 5))
        print(f"  focus        {c(bar, 'cyan')} {f['score']}/100")
        print(f"               {f['deep_blocks']} deep block(s), "
              f"longest {human_dur(f['longest_block'])}")

    blocks = insights.deep_work_blocks(s, e)
    if blocks:
        hr("  deep work")
        for b in blocks:
            print(f"  {b['from']}–{b['to']}  {human_dur(b['seconds']):>8}  "
                  f"{c('/'.join(b['apps']), 'grey')}")

    hr("  where the time went")
    for cat in insights.by_category(s, e)[:8]:
        w = int(cat["pct"] / 3)
        print(f"  {cat['category']:<12} {c('▇' * max(1, w), 'blue')} "
              f"{cat['human']:>8}  {cat['pct']:>5}%")

    hr("  apps")
    for a in insights.by_app(s, e, 10):
        t = f"  {c(a['top_titles'][0][:52], 'grey')}" if a["top_titles"] else ""
        print(f"  {a['app']:<20} {a['human']:>8}{t}")

    d = insights.distractions(s, e)
    if d:
        hr("  distractions")
        for x in d:
            print(f"  {x['what']:<20} {c(x['human'], 'yellow')}")

    cmp_ = insights.compare_to_baseline(day)
    colour = "green" if cmp_["delta_seconds"] >= 0 else "yellow"
    print(f"\n  vs your {cmp_['days_in_baseline']}-day average: "
          f"{c(cmp_['delta_human'], colour)}\n")
    return 0


def _print_digest(d: dict) -> None:
    hr(f"  {d['title']}")
    print()
    for line in d["body"].splitlines():
        if line.startswith("**") or line.startswith("#"):
            print("  " + c(line.replace("**", "").lstrip("# "), "cyan"))
        elif line.strip().startswith(("-", "*", "•")):
            print("   " + line.strip())
        else:
            print("  " + line)
    print()


def cmd_brief(args) -> int:
    from .brain import digest
    print(c("thinking...", "grey"), end="\r")
    _print_digest(digest.morning_brief())
    return 0


def cmd_wrap(args) -> int:
    from .brain import digest
    print(c("thinking...", "grey"), end="\r")
    _print_digest(digest.evening_wrap())
    return 0


def cmd_retro(args) -> int:
    from .brain import digest
    print(c("thinking...", "grey"), end="\r")
    _print_digest(digest.weekly_retro())
    return 0


def cmd_ask(args) -> int:
    from .brain import digest
    q = " ".join(args.question)
    print(c("thinking...", "grey"), end="\r")
    a = digest.answer(q)
    print(" " * 20, end="\r")
    hr(f"  {q}")
    print()
    for line in a.splitlines():
        print("  " + line)
    print()
    if args.speak:
        from .actions.voice import say
        say(a)
        time.sleep(min(20, len(a) / 14))
    return 0


def cmd_watch(args) -> int:
    """Live tail of what you are doing, refreshed in place."""
    from .brain import insights
    from .sensors.window import foreground, idle_seconds
    from . import classify
    print(c("  watching - ctrl+c to stop\n", "grey"))
    try:
        while True:
            exe, title, _ = foreground()
            app, cat, realm = classify.classify(exe, title)
            idle = idle_seconds()
            s, e, _ = insights.day_range()
            t = insights.totals(s, e)
            state = c("AFK", "grey") if idle > 180 else c(cat.upper(), "cyan")
            print(f"\r  {datetime.now():%H:%M:%S}  {state:<18} "
                  f"{app[:22]:<22} {title[:44]:<44} "
                  f"| today {human_dur(t['productive_seconds'])} productive   ",
                  end="", flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n")
    return 0


def cmd_alerts(args) -> int:
    from .actions import notify
    rows = notify.recent(args.limit, unacked_only=args.unread, collapse=True)
    if not rows:
        print("  nothing to show")
        return 0
    hr("  alerts")
    for r in rows:
        sev = {"info": "grey", "warn": "yellow", "urgent": "red"}.get(r["severity"], "grey")
        mark = " " if r["acked"] else c("•", "cyan")
        # notify.recent() returns raw rows; only the web API adds "ago", so
        # this used to raise KeyError and `argus alerts` never worked.
        ago = human_ago(r["ts"])
        print(f"  {mark} {c(f'{ago:>9}', 'grey')}  {c(r['severity'][:4].upper(), sev):<10} "
              f"{r['title'][:56]}" + (c(f"  x{r['count']}", "grey") if r["count"] > 1 else ""))
        if r["body"] and args.verbose:
            print(f"      {c(r['body'][:110], 'grey')}")
    if args.ack:
        print(c(f"\n  acked {notify.ack()}", "green"))
    return 0


def cmd_repos(args) -> int:
    from .sensors import git
    hr("  repositories")
    for s in sorted(git.all_states(max_age=0), key=lambda x: -(x["last_commit_ts"] or 0)):
        flags = []
        if s["dirty"]:
            flags.append(c(f"{s['dirty']} dirty", "yellow"))
        if s["ahead"]:
            flags.append(c(f"{s['ahead']} unpushed", "mag"))
        if s["behind"]:
            flags.append(c(f"{s['behind']} behind", "blue"))
        ago = human_ago(s["last_commit_ts"]) if s["last_commit_ts"] else "never"
        print(f"  {s['name'][:26]:<26} {c(f"{s['branch'][:16]:<16}", 'grey')} "
              f"{ago:>10}  {' '.join(flags)}")
        if args.verbose and s["dirty_files"]:
            for f in s["dirty_files"][:5]:
                print(f"      {c(f[:70], 'grey')}")
    return 0


# --------------------------------------------------------------------------- google

def _acct_state(a: dict) -> str:
    if a["valid"]:
        return c("ok", "green")
    if a.get("state") == "expired":
        return c("expired - log in again", "red")
    return c("unreachable right now", "yellow")


def _print_token_hint(accts: list[dict]) -> None:
    dead = [a for a in accts if a.get("state") == "expired"]
    if not dead:
        return
    print()
    if len(dead) > 1:
        print(c("  argus google relogin", "cyan") + c("   (signs each one in, in turn)", "grey"))
    else:
        print(c(f"  argus google login --label {dead[0]['label']} --force", "cyan"))
    hint = next((a["hint"] for a in dead if a.get("hint")), "")
    if hint:
        import textwrap
        print()
        print(c(textwrap.fill(hint, 74, initial_indent="  ", subsequent_indent="  "),
                "grey"))


def _google_login_one(gauth, label: str) -> bool:
    """Interactive consent for one label. True when it ends up connected."""
    before = gauth.email_of(label) if label in gauth.labels() else ""
    print(f"  Signing in for label {c(label, 'cyan')}"
          + (f" ({before})" if before else "") + " - pick that Google account in the browser.")
    try:
        gauth.creds(label, interactive=True)
    except RuntimeError as e:
        print(e)
        return False
    except Exception as e:
        msg = str(e)
        print(c(f"  login failed: {msg[:300]}", "red"))
        if "access_denied" in msg or "blocked" in msg.lower():
            print(c("  That looks like a Workspace admin policy or a missing "
                    "Test user.", "grey"))
            print(c("  Add the account under Audience -> Test users, or use a "
                    "different account.", "grey"))
        return False
    db.set_state(f"google_email:{label}", "")          # re-learn who this is
    who = gauth.email_of(label)
    print(c(f"  Connected '{label}' as {who}", "green"))
    if before and who and who.lower() != before.lower():
        print(c(f"  Note: '{label}' used to be {before}. If that was the wrong account, "
                f"run this again and pick {before}.", "yellow"))
    print(f"  features: {', '.join(gauth.features(label)) or 'none'}"
          f"   {c('(tune per account in config.yaml)', 'grey')}")
    return True


def cmd_google(args) -> int:
    from .gsuite import auth as gauth

    if args.action == "login":
        try:
            label = gauth.validate_label(args.label or "default")
        except ValueError as e:
            print(c(f"  {e}", "red"))
            return 1
        if not gauth.is_configured():
            print(gauth.help_text())
            return 1
        existing = gauth.labels()
        if label in existing and not args.force:
            print(c(f"  '{label}' is already connected as "
                    f"{gauth.email_of(label)}.", "yellow"))
            print("  Re-authorise it with:  argus google login --label "
                  f"{label} --force")
            return 0
        if not _google_login_one(gauth, label):
            return 1
        print("  Syncing...")
        from .daemon import google_sync
        print("  " + google_sync())
        return 0

    if args.action == "relogin":
        # Tokens die together (Testing mode expires them all at 7 days), so
        # fixing them should not mean four separate commands.
        dead = [lb for lb in gauth.labels()
                if gauth.token_state(lb).get("state") == "expired" or gauth.creds(lb) is None]
        if not dead:
            print(c("  every connected account is working - nothing to do", "green"))
            return 0
        print(f"  {len(dead)} account(s) need a fresh login: {', '.join(dead)}")
        ok = 0
        for label in dead:
            print()
            ok += _google_login_one(gauth, label)
        print()
        if ok:
            from .daemon import google_sync
            print("  Syncing...\n  " + google_sync())
        return 0 if ok == len(dead) else 1

    if args.action == "logout":
        if not args.label:
            print("  which account?  argus google logout --label work")
            print("  connected:", ", ".join(gauth.labels()) or "none")
            return 1
        ok = gauth.disconnect(args.label)
        print(c("  disconnected" if ok else "  no such label", "yellow" if ok else "red"))
        return 0

    if args.action == "sync":
        from .daemon import google_sync
        print("  " + google_sync())
        return 0

    if args.action == "list":
        accts = gauth.accounts()
        if not accts:
            print("  no accounts connected yet  ->  argus google login --label personal")
            return 0
        hr("  google accounts")
        for a in accts:
            print(f"  {a['label']:<12} {a['email'][:34]:<34} {_acct_state(a)}")
            print(f"  {'':<12} {c('features: ' + (', '.join(a['features']) or 'none'), 'grey')}")
        _print_token_hint(accts)
        return 0

    # status
    accts = gauth.accounts()
    if not accts:
        print(gauth.help_text())
        return 0
    from .gsuite import gcal, gmail
    hr("  google")
    for a in accts:
        state = _acct_state(a)
        counts = gmail.unread_counts(a["label"]) if "mail" in a["features"] else {}
        unread = ", ".join(f"{k} {v}" for k, v in counts.items()) or "-"
        print(f"  {a['label']:<12} {a['email'][:32]:<32} {state}")
        print(f"  {'':<12} {c('features ' + (', '.join(a['features']) or 'none'), 'grey')}"
              f"  {c('unread ' + unread, 'grey')}")
    _print_token_hint(accts)
    ev = gcal.upcoming(hours=36, limit=6)
    if ev:
        hr("  next up")
        for e in ev:
            when = datetime.fromtimestamp(e["start_ts"])
            mins = (e["start_ts"] - time.time()) / 60
            soon = c(f"in {int(mins)}m", "yellow") if 0 < mins < 120 else f"{when:%a %H:%M}"
            tag = f"[{e['account']}] " if e.get("account") else ""
            print(f"  {soon:<18} {tag}{e['summary'][:44]}")
    return 0


def cmd_mail(args) -> int:
    from .gsuite import gmail
    rows = gmail.important_unread(args.limit)
    if not rows:
        print("  no important unread mail")
        return 0
    hr("  needs your attention")
    for m in rows:
        tag = c("VIP", "mag") if m["triage"] == "vip" else c("reply", "yellow")
        acct = f"{m['account']}/" if m.get("account") else ""
        print(f"  [{tag}] {acct}{m['sender'][:24]:<24} {m['subject'][:46]}")
        print(f"          {c(m['reason'] or '', 'grey')}  {c(human_ago(m['ts']), 'grey')}")
    return 0


def cmd_cal(args) -> int:
    from .gsuite import gcal
    ev = gcal.upcoming(hours=args.hours)
    hr(f"  next {args.hours}h")
    if not ev:
        print("  clear")
    for e in ev:
        when = datetime.fromtimestamp(e["start_ts"])
        tag = f"[{e['account']}] " if e.get("account") else ""
        print(f"  {when:%a %H:%M}  {tag}{e['summary'][:44]}"
              + (c("  [video]", "cyan") if e["hangout"] else ""))
    slots = gcal.free_slots()
    if slots:
        hr("  free today")
        print("  " + ", ".join(f"{s['label']} ({s['minutes']}m)" for s in slots))
    return 0


# --------------------------------------------------------------------------- tasks

def cmd_task(args) -> int:
    from .gsuite import gtasks
    if args.action == "add":
        title = " ".join(args.rest)
        if not title:
            print("  usage: argus task add <title>")
            return 1
        tid = gtasks.add_local(title, source="user")
        print(c(f"  added #{tid}: {title}", "green"))
        return 0
    if args.action == "done":
        if not args.rest:
            print("  usage: argus task done <id>")
            return 1
        ok = gtasks.complete_local(int(args.rest[0]))
        print(c("  done" if ok else "  no such task", "green" if ok else "red"))
        return 0
    rows = gtasks.open_tasks()
    if not rows:
        print("  no open tasks")
        return 0
    hr("  open tasks")
    for t in rows:
        due = ""
        if t["due"]:
            dt = datetime.fromtimestamp(t["due"])
            late = t["due"] < time.time()
            due = c(f"  due {dt:%d %b}", "red" if late else "grey")
        print(f"  #{t['id']:<4} {t['title'][:52]}{due}")
    return 0


# --------------------------------------------------------------------------- misc

def cmd_pause(args) -> int:
    from .actions import tray
    print("  " + (tray.pause(args.minutes) if args.minutes else tray.resume()))
    return 0


def cmd_unpause(args) -> int:
    from .actions import tray
    print("  " + tray.resume())
    return 0


def cmd_privacy(args) -> int:
    import httpx
    port = config.get("daemon.web_port", 7842)
    try:
        data = httpx.get(f"http://127.0.0.1:{port}/api/privacy", timeout=10).json()
    except Exception:
        from .web.server import api_privacy
        data = api_privacy()
    hr("  what ARGUS stores")
    print(f"  keystrokes captured   {c('never', 'green')}")
    print(f"  clipboard captured    "
          f"{c('yes', 'yellow') if data['clipboard_capture'] else c('no', 'green')}")
    if data["screenshots_enabled"]:
        shots = c(f"on ({data['screenshot_count']} files, {data['screenshot_mb']} MB)",
                  "yellow")
    else:
        shots = c("off", "green")
    print(f"  screenshots           {shots}")
    print(f"  database              {data['db_mb']} MB at {data['data_dir']}")
    print("  rows                  " + ", ".join(f"{k} {v}" for k, v in data["rows"].items()))
    print(f"  leaves this machine   {c('only LLM prompts you trigger', 'grey')}")
    print(f"\n  purge:  argus purge --scope all|browser|screenshots|day --day YYYY-MM-DD\n")
    return 0


def cmd_purge(args) -> int:
    from fastapi import HTTPException

    from .web.server import api_purge
    if args.scope == "all" and not args.yes:
        print(c("  This deletes every stored observation, the journal and all "
                "screenshots.", "yellow"))
        print(c("  What you taught ARGUS and your task list are kept. "
                "Re-run with --yes.", "yellow"))
        return 1
    try:
        res = api_purge({"scope": args.scope, "day": args.day})
    except HTTPException as e:
        print(c(f"  {e.detail}", "red"))
        return 1
    print(c(f"  deleted {res['rows']} rows and {res['files']} files ({res['scope']})",
            "green"))
    return 0


def cmd_teach(args) -> int:
    text = " ".join(args.text)
    if args.match:
        from . import classify
        n = classify.teach(args.match, args.category or "", args.realm or "")
        print(c(f"  learned: '{args.match}' -> {args.category}/{args.realm}", "green"))
        if n:
            print(c(f"  re-labelled {n} past session(s) to match", "grey"))
        return 0
    if not text:
        print("  usage: argus teach \"I prefer short briefs\"")
        return 1
    db.remember(args.kind, text)
    print(c(f"  remembered ({args.kind}): {text}", "green"))
    return 0


def cmd_reclassify(args) -> int:
    """Re-derive stored categories with the current rules (and your teachings)."""
    from . import classify
    r = classify.reclassify(since_days=args.days)
    scope = f"last {args.days:g} days" if args.days else "all history"
    print(c(f"  re-labelled {r['changed']} of {r['checked']} sessions ({scope})", "green"))
    return 0


def cmd_memory(args) -> int:
    rows = db.recall(limit=60)
    if not rows:
        print("  nothing learned yet")
        return 0
    hr("  what ARGUS knows about you")
    for r in rows:
        print(f"  {c(f"{r['category'][:10]:<11}", 'grey')} {r['text'][:70]}")
    return 0


def cmd_runs(args) -> int:
    from .daemon import is_quiet_run
    rows = db.q("SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (args.limit * (1 if args.all else 20),))
    if not args.all:
        rows = [r for r in rows if not is_quiet_run(r["name"], r["ok"], r["summary"])]
    rows = rows[:args.limit]
    hr("  automation log")
    for r in rows:
        mark = c("ok", "green") if r["ok"] else c("fail", "red")
        print(f"  {c(f"{human_ago(r['ts']):>9}", 'grey')}  {mark:<12} {r['name'][:22]:<22} "
              f"{c((r['summary'] or r['error'] or '')[:52], 'grey')}")
    return 0


def cmd_telegram(args) -> int:
    from .actions import telegram
    if args.action == "setup":
        if not args.token:
            print("  Make a bot: open Telegram, message @BotFather, send /newbot, copy the token.")
            print("  Then:  argus telegram setup <token>")
            return 1
        try:
            print(c("  " + telegram.setup(args.token), "green"))
        except Exception as e:
            print(c(f"  {e}", "red"))
            return 1
        return 0
    if args.action == "test":
        ok = telegram.send("ARGUS test message - you're connected.")
        print(c("  sent" if ok else "  not paired (argus telegram setup)", "green" if ok else "yellow"))
        return 0 if ok else 1
    if args.action == "off":
        telegram.FILE.unlink(missing_ok=True)
        print("  unpaired")
        return 0
    print("  paired" if telegram.enabled() else "  not paired - argus telegram setup")
    return 0


def cmd_backfill(args) -> int:
    from . import backfill
    print(c("  Reconstructing history from browser, Claude Code and git evidence...", "grey"))
    res = backfill.run_all(days=args.days, reset=not args.keep)
    hr("  backfill")
    for k in ("browser", "claude", "git", "cleared", "days_covered"):
        print(f"  {k:<14} {res.get(k, 0)}")
    print(c("\n  These rows are marked <inferred:...> so they stay distinguishable"
            "\n  from live observation.\n", "grey"))
    return 0


def cmd_resume(args) -> int:
    """Where you left off. The flagship command of the Work Thread engine."""
    from .brain import threads

    n = args.pick
    # A bare `resume` re-derives; picking an item does not, so the numbers the
    # user just read still refer to the same threads.
    if n is None and not args.why and not args.dismiss and not args.wrong:
        threads.rebuild()

    rows = threads.ranked(limit=args.limit)
    if not rows:
        print(c("\n  Nothing to resume - no work threads found yet.", "grey"))
        print(c("  ARGUS needs a day or two of file/git activity to build these.\n", "grey"))
        return 0

    def pick(i):
        if not (1 <= i <= len(rows)):
            print(c(f"  no thread {i} (1-{len(rows)} shown)", "red"))
            return None
        return rows[i - 1]

    # ---- feedback verbs ----------------------------------------------------
    if args.dismiss:
        t = pick(args.dismiss)
        if not t:
            return 1
        threads.record_feedback(t["key"], "dismissed",
                                meta={"rank": args.dismiss, "score": t["score"]})
        print(c(f"  dismissed '{t['key']}' - hidden for 7 days", "yellow"))
        return 0

    if args.wrong:
        t = pick(args.wrong)
        if not t:
            return 1
        note = " ".join(args.note) if args.note else ""
        threads.record_feedback(t["key"], "wrong", note,
                                meta={"rank": args.wrong, "score": t["score"]})
        print(c(f"  noted - '{t['key']}' was wrong" + (f": {note}" if note else ""), "yellow"))
        return 0

    if args.why:
        t = pick(args.why)
        if not t:
            return 1
        ctx = threads.context_for(t["key"])
        hr(f"  why ARGUS thinks you were working on '{t['key']}'")
        p = ctx["confidence_parts"]
        print(f"  confidence {c(str(ctx['confidence']), 'cyan')}"
              f"  = 0.45×evidence({p.get('evidence')})"
              f" + 0.35×diversity({p.get('diversity')})"
              f" + 0.20×persistence({p.get('persistence')})")
        print(f"  {p.get('weight')} weighted evidence from {len(p.get('sources', []))} "
              f"sources over {p.get('days')} days\n")
        for src, items in sorted(ctx["by_source"].items(),
                                 key=lambda kv: -threads.SRC_W.get(kv[0], 0)):
            print(c(f"  {src}  ({len(items)})", "cyan"))
            for x in items[:6]:
                when = datetime.fromtimestamp(x["ts"]).strftime("%d %b %H:%M")
                ref = f"{x['ref_table']}#{x['ref_id']}" if x["ref_table"] in (
                    "events", "activity") else x["resolver"]
                print(f"    {when}  {(x['evidence'] or '')[:62]:<62} {c(ref, 'grey')}")
            if len(items) > 6:
                print(c(f"    … and {len(items) - 6} more", "grey"))
        print()
        return 0

    # ---- one thread, in full ----------------------------------------------
    if n is not None:
        t = pick(n)
        if not t:
            return 1
        ctx = threads.context_for(t["key"])
        threads.record_feedback(t["key"], "resumed",
                                meta={"rank": n, "score": t["score"],
                                      "shown": [r["key"] for r in rows]})
        hr(f"  {ctx['title']}")
        print(f"  {c(ctx['status'].upper(), 'cyan')}  {ctx['status_reason']}")
        m = ctx["meta"]
        if m.get("branch"):
            print(f"  branch       {m['branch']}")
        if m.get("repo"):
            print(f"  repo         {m['repo']}")
        print(f"  last touched {datetime.fromtimestamp(ctx['last_ts']):%a %d %b %H:%M}"
              f"   ({human_ago(ctx['last_ts'])})")
        if ctx["seconds_total"] > 60:
            print(f"  observed     {human_dur(ctx['seconds_total'])}")

        intents = [x for x in ctx["touches"]
                   if x["source"] == "claude" and not (x["evidence"] or "").startswith("session:")]
        sessions = [x for x in ctx["touches"]
                    if x["source"] == "claude" and (x["evidence"] or "").startswith("session:")]
        if intents:
            hr("  what you said you were doing")
            for x in intents[:4]:
                print(f"  {datetime.fromtimestamp(x['ts']):%d %b %H:%M}  "
                      f"{(x['evidence'] or '')[:92]}")
        if sessions:
            hr("  sessions")
            for x in sessions[:5]:
                print(f"  {datetime.fromtimestamp(x['ts']):%d %b}  "
                      f"{(x['evidence'] or '')[8:80]}")

        commits = [x for x in ctx["touches"] if x["source"] == "git"]
        if commits:
            hr("  commits")
            for x in commits[:5]:
                print(f"  {datetime.fromtimestamp(x['ts']):%d %b %H:%M}  "
                      f"{(x['evidence'] or '')[:80]}")

        files = [x for x in ctx["touches"] if x["source"] == "file"]
        if files:
            hr("  files touched")
            seen, shown = set(), 0
            for x in files:
                nm = x["evidence"] or ""
                if nm in seen:
                    continue
                seen.add(nm)
                print(f"  {datetime.fromtimestamp(x['ts']):%d %b %H:%M}  {nm[:70]}")
                shown += 1
                if shown >= 8:
                    break

        if ctx["open_loop"]:
            hr("  open loop")
            print(c(f"  {m.get('dirty', 0)} uncommitted file(s), "
                    f"{m.get('ahead', 0)} unpushed commit(s) on {m.get('branch', '?')}",
                    "yellow"))
        print(c(f"\n  argus resume --why {n}    show every piece of evidence\n", "grey"))
        return 0

    # ---- the ranked list ---------------------------------------------------
    hr("  where you left off")
    for i, t in enumerate(rows, 1):
        colour = {"active": "green", "incomplete": "yellow",
                  "paused": "grey", "completed": "blue"}.get(t["status"], "grey")
        print(f"\n  {c(str(i), 'cyan')}  {t['title'][:44]:<44} "
              f"{human_ago(t['last_ts']):>11}  {c(t['status'], colour)}")
        bits = []
        if t.get("realm") and t["realm"] != "unknown":
            bits.append(t["realm"])
        if t["seconds_total"] > 60:
            bits.append(human_dur(t["seconds_total"]))
        bits.append(f"{t['touch_count']} touches")
        bits.append(f"{t['source_count']} sources")
        bits.append(f"conf {t['confidence']}")
        print(c("     " + " · ".join(bits), "grey"))
        if t["open_loop"]:
            m = t["meta"]
            print(c(f"     open loop: {m.get('dirty', 0)} uncommitted, "
                    f"{m.get('ahead', 0)} unpushed on {m.get('branch', '?')}", "yellow"))
    print(c("\n  argus resume 1        pick it up, with full context", "grey"))
    print(c("  argus resume --why 1  show the evidence", "grey"))
    print(c("  argus resume --not 1  not what I'm working on\n", "grey"))
    return 0


def cmd_export(args) -> int:
    """Sessions as CSV, or everything in a date range as JSON."""
    from pathlib import Path

    from . import export
    end = args.end or datetime.now().strftime("%Y-%m-%d")
    start = args.start or (datetime.strptime(end, "%Y-%m-%d")
                           - timedelta(days=args.days - 1)).strftime("%Y-%m-%d")
    text = (export.everything_json(start, end) if args.format == "json"
            else export.activity_csv(start, end, include_afk=args.afk))
    if args.out == "-":
        sys.stdout.write(text)
        return 0
    out = Path(args.out or f"argus-{start}_{end}.{args.format}")
    out.write_text(text, encoding="utf-8")
    print(c(f"  wrote {out} ({len(text) / 1e3:.0f} kB, {start} - {end})", "green"))
    return 0


def cmd_backup(args) -> int:
    """Snapshot data/argus.db (consistent even while ARGUS is writing)."""
    from pathlib import Path
    if getattr(args, "decrypt", None):
        from . import vault
        src = Path(args.decrypt)
        out = Path(args.to) if args.to else src.with_name(src.name.removesuffix(".enc") + ".restored.db")
        size = vault.decrypt_file(src, out)
        print(c(f"  decrypted {size / 1e6:.1f} MB to {out}", "green"))
        print(c("  to use it: argus stop, copy it over data\\argus.db, argus start", "grey"))
        return 0
    if args.to:
        size = db.backup(Path(args.to))
        print(c(f"  backed up {size / 1e6:.1f} MB to {args.to}", "green"))
        return 0
    from .automations import builtin
    print(c("  " + builtin.backup(), "green"))
    return 0


def cmd_timesheet(args) -> int:
    """Per-day start, stop, hours and projects - the evidence for a timesheet."""
    from pathlib import Path

    from .brain import timesheet as ts
    if args.start:
        start, end = args.start, args.end or args.start
    elif args.days:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=args.days - 1)).strftime("%Y-%m-%d")
    else:
        start, end = ts.week_bounds(-1 if args.last else 0)
    table = ts.rows(start, end, realm=args.realm)

    if args.csv:
        text = ts.to_csv(table)
        if args.csv == "-":
            sys.stdout.write(text)
        else:
            Path(args.csv).write_text(text, encoding="utf-8")
            print(c(f"  wrote {len(table)} days to {args.csv}", "green"))
        return 0

    label = "work" if args.realm == "work" else args.realm
    hr(f"  timesheet  {datetime.strptime(start, '%Y-%m-%d'):%a %d %b} – "
       f"{datetime.strptime(end, '%Y-%m-%d'):%a %d %b}   ({label} hours)")
    print(c(f"  {'day':<11}{'in':>6}{'out':>7}{'active':>9}{'work':>9}   projects", "grey"))
    for r in table:
        d = datetime.strptime(r["day"], "%Y-%m-%d")
        if not r["active_seconds"]:
            print(c(f"  {d:%a %d %b}   {'—':>5}", "grey"))
            continue
        inn = datetime.fromtimestamp(r["first"]).strftime("%H:%M")
        out = datetime.fromtimestamp(r["last"]).strftime("%H:%M")
        proj = ", ".join(r["projects"][:3]) or "—"
        extra = f" · {len(r['commits'])} commit(s)" if r["commits"] else ""
        est = c(" (estimated)", "yellow") if r["inferred"] else ""
        print(f"  {d:%a %d %b}  {inn:>5}  {out:>5}  {human_dur(r['active_seconds']):>7}  "
              f"{c(human_dur(r['work_seconds']), 'green'):>16}   "
              f"{proj[:40]}{c(extra, 'grey')}{est}")
    s = ts.summary(table)
    print(f"\n  {s['days']} day(s) · active {s['active']} · {label} "
          f"{c(s['work'], 'green')} ({s['work_hours']}h) · {s['commits']} commit(s)")
    print(c("  argus timesheet --csv week.csv   to paste into a sheet\n", "grey"))
    return 0


def cmd_doctor(args) -> int:
    """Every way ARGUS can be quietly broken, with the fix for each."""
    import textwrap

    from . import doctor
    checks = doctor.run_checks(skip=() if args.slow else ("autostart",))
    mark = {"ok": c("✓", "green"), "warn": c("!", "yellow"),
            "fail": c("✗", "red"), "info": c("·", "cyan")}
    hr("  argus doctor")
    for ch in checks:
        if args.quiet and ch["status"] == "ok":
            continue
        lines = textwrap.wrap(ch["detail"], 56) or [""]
        print(f"  {mark.get(ch['status'], '?')} {ch['name']:<18} {lines[0]}")
        for extra in lines[1:]:
            print(f"    {'':<18} {extra}")
        if ch["fix"] and ch["status"] != "ok":
            print(f"    {'':<18} {c('→ ' + ch['fix'], 'cyan')}")
    verdict = doctor.worst(checks)
    print()
    print("  " + {"ok": c("All good.", "green"), "info": c("All good.", "green"),
                  "warn": c("Working, with warnings above.", "yellow"),
                  "fail": c("Something you rely on is broken - see above.", "red")}[verdict])
    print()
    return 1 if verdict == "fail" else 0


def cmd_dash(args) -> int:
    import webbrowser
    url = f"http://127.0.0.1:{config.get('daemon.web_port', 7842)}"
    webbrowser.open(url)
    print(f"  {url}")
    return 0


def cmd_logs(args) -> int:
    f = LOGS / "argus.log"
    if not f.is_file():
        print("  no log yet")
        return 0
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines:]:
        colour = ("red" if " ERROR" in line else
                  "yellow" if " WARNI" in line else "grey")
        print(c(line, colour))
    return 0


def cmd_install(args) -> int:
    from .install import install_autostart, make_shortcuts, write_launchers
    rc = install_autostart(remove=False)
    write_launchers()
    if not args.no_shortcuts:
        print("  shortcuts:", make_shortcuts(desktop=True, start_menu=True))
    from .paths import APP_EXE
    if APP_EXE.exists():
        print(f"  app        {APP_EXE.name}  (double-click, or search 'ARGUS' in Start)")
    else:
        print(c("  note: Argus.exe not built yet - autostart uses the venv.", "grey"))
        print(c("        build it with: argus build", "grey"))
    return rc


def cmd_uninstall(args) -> int:
    from .install import install_autostart, remove_shortcuts
    rc = install_autostart(remove=True)
    print("  shortcuts:", remove_shortcuts())
    return rc


def cmd_build(args) -> int:
    """Freeze ARGUS into Argus.exe with PyInstaller, then install it in place."""
    from .install import install_app_bundle
    return install_app_bundle(clean=args.clean, restart=not args.no_restart,
                              build=not args.no_build)


def cmd_update(args) -> int:
    """Pull the latest ARGUS from its GitHub repo, test it, rebuild the app.

    Fast-forward only, and never over local changes: an update must not be
    able to lose work or rewrite history. If the tests fail the app is not
    rebuilt, so the running version stays the one that worked."""
    import subprocess
    from .paths import ROOT
    from .util import run
    git = ["git", "-C", str(ROOT)]
    rc, out, err = run(git + ["status", "--porcelain", "--untracked-files=no"], timeout=30)
    if rc != 0:
        print(c(f"  not a git checkout: {err[:120]}", "red"))
        return 1
    if out.strip():
        print(c("  you have local changes in the ARGUS folder - commit or stash them first", "yellow"))
        return 1
    rc, branch, _ = run(git + ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
    before = run(git + ["rev-parse", "HEAD"], timeout=15)[1]
    print(f"  fetching {branch} ...")
    rc, _, err = run(git + ["fetch", "origin", branch], timeout=180)
    if rc != 0:
        print(c(f"  fetch failed: {err[:200]}", "red"))
        return 1
    rc, _, err = run(git + ["merge", "--ff-only", f"origin/{branch}"], timeout=60)
    if rc != 0:
        print(c(f"  cannot fast-forward ({err[:160]}) - your branch and GitHub's have diverged", "red"))
        return 1
    after = run(git + ["rev-parse", "HEAD"], timeout=15)[1]
    if after == before and not args.force:
        print(c("  already up to date", "green"))
        return 0
    rc, log_, _ = run(git + ["log", "--oneline", f"{before}..{after}"], timeout=15)
    print(c(f"  {len(log_.splitlines())} new change(s):", "cyan"))
    for line in log_.splitlines()[:15]:
        print("    " + line)
    if not args.no_tests:
        print("  running the tests ...")
        py = ROOT / ".venv" / "Scripts" / "python.exe"
        res = subprocess.run([str(py), "-m", "pytest", "tests", "-q"], cwd=str(ROOT),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = (res.stdout or res.stderr).strip().splitlines()[-1:] or ["?"]
        if res.returncode != 0:
            print(c(f"  tests failed ({tail[0]}) - not rebuilding; the running app is unchanged", "red"))
            return 1
        print(c(f"  {tail[0]}", "green"))
    from .install import install_app_bundle
    return install_app_bundle(clean=False, restart=True, build=True)


def cmd_config(args) -> int:
    from .paths import CONFIG_PATH
    if args.edit:
        os.startfile(str(CONFIG_PATH))  # noqa: S606
        return 0
    print(json.dumps(config.load(), indent=2, default=str))
    return 0


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="argus", description="Your machine's assistant.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("start", help="start the daemon in the background").set_defaults(fn=cmd_start)
    r = sub.add_parser("run", help="run in the foreground (debug)")
    r.add_argument("--no-web", action="store_true")
    r.add_argument("--no-tray", action="store_true")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("stop", help="stop the daemon").set_defaults(fn=cmd_stop)
    sub.add_parser("restart", help="restart the daemon").set_defaults(fn=cmd_restart)
    sub.add_parser("status", help="one-screen status").set_defaults(fn=cmd_status)

    t = sub.add_parser("today", help="today's activity report")
    t.set_defaults(fn=cmd_today, which="today")
    y = sub.add_parser("yesterday", help="yesterday's report")
    y.set_defaults(fn=cmd_today, which="yesterday")
    d = sub.add_parser("day", help="a specific day: argus day 2026-09-08")
    d.add_argument("date")
    d.set_defaults(fn=cmd_today, which="day")

    sub.add_parser("brief", help="generate the morning brief now").set_defaults(fn=cmd_brief)
    sub.add_parser("wrap", help="generate the evening wrap now").set_defaults(fn=cmd_wrap)
    sub.add_parser("retro", help="generate the weekly retro now").set_defaults(fn=cmd_retro)

    a = sub.add_parser("ask", help='ask about your own activity')
    a.add_argument("question", nargs="+")
    a.add_argument("--speak", action="store_true")
    a.set_defaults(fn=cmd_ask)

    sub.add_parser("watch", help="live activity tail").set_defaults(fn=cmd_watch)

    al = sub.add_parser("alerts", help="recent alerts")
    al.add_argument("--limit", type=int, default=25)
    al.add_argument("--unread", action="store_true")
    al.add_argument("--ack", action="store_true", help="mark all as read")
    al.add_argument("-v", "--verbose", action="store_true")
    al.set_defaults(fn=cmd_alerts)

    rp = sub.add_parser("repos", help="git repo state")
    rp.add_argument("-v", "--verbose", action="store_true")
    rp.set_defaults(fn=cmd_repos)

    g = sub.add_parser("google", help="connect and manage Google accounts")
    g.add_argument("action", nargs="?", default="status",
                   choices=["status", "list", "login", "relogin", "logout", "sync"])
    g.add_argument("--label", help="short name for the account, e.g. work / personal")
    g.add_argument("--force", action="store_true", help="re-authorise an existing label")
    g.set_defaults(fn=cmd_google)

    m = sub.add_parser("mail", help="important unread mail")
    m.add_argument("--limit", type=int, default=10)
    m.set_defaults(fn=cmd_mail)

    cal = sub.add_parser("cal", help="upcoming calendar")
    cal.add_argument("--hours", type=int, default=36)
    cal.set_defaults(fn=cmd_cal)

    tk = sub.add_parser("task", help="tasks: add / list / done")
    tk.add_argument("action", nargs="?", default="list", choices=["list", "add", "done"])
    tk.add_argument("rest", nargs="*")
    tk.set_defaults(fn=cmd_task)

    pa = sub.add_parser("pause", help="stop all watching and alerts for N minutes (0 = resume)")
    pa.add_argument("minutes", nargs="?", type=int, default=60)
    pa.set_defaults(fn=cmd_pause)
    sub.add_parser("unpause", help="end a pause early").set_defaults(
        fn=cmd_unpause)

    rs = sub.add_parser("resume", help="where you left off (work threads)")
    rs.add_argument("pick", nargs="?", type=int, help="open thread N in full")
    rs.add_argument("--why", type=int, metavar="N", help="show the evidence for N")
    rs.add_argument("--not", dest="dismiss", type=int, metavar="N",
                    help="not what I'm working on - hide for 7 days")
    rs.add_argument("--wrong", type=int, metavar="N", help="mark N as misidentified")
    rs.add_argument("--note", nargs="*", default=None,
                    help="what it actually was, to go with --wrong")
    rs.add_argument("--limit", type=int, default=8)
    rs.set_defaults(fn=cmd_resume)

    sub.add_parser("privacy", help="what is stored").set_defaults(fn=cmd_privacy)
    pu = sub.add_parser("purge", help="delete stored data")
    pu.add_argument("--scope", required=True,
                    choices=["all", "browser", "screenshots", "threads", "day"])
    pu.add_argument("--day")
    pu.add_argument("--yes", action="store_true")
    pu.set_defaults(fn=cmd_purge)

    te = sub.add_parser("teach", help="tell ARGUS something to remember")
    te.add_argument("text", nargs="*")
    te.add_argument("--kind", default="preference",
                    choices=["preference", "fact", "goal", "correction"])
    te.add_argument("--match", help="classify rule: window/title substring")
    te.add_argument("--category")
    te.add_argument("--realm", choices=["work", "personal", "unknown"])
    te.set_defaults(fn=cmd_teach)
    sub.add_parser("memory", help="what ARGUS has learned").set_defaults(fn=cmd_memory)
    rc = sub.add_parser("reclassify", help="re-derive stored categories with the current rules")
    rc.add_argument("--days", type=float, help="only the last N days (default: everything)")
    rc.set_defaults(fn=cmd_reclassify)

    ru = sub.add_parser("runs", help="automation run log")
    ru.add_argument("--limit", type=int, default=30)
    ru.add_argument("--all", action="store_true", help="include no-op runs")
    ru.set_defaults(fn=cmd_runs)

    tg = sub.add_parser("telegram", help="reminders on your phone, capture from it")
    tg.add_argument("action", nargs="?", default="status", choices=["status", "setup", "test", "off"])
    tg.add_argument("token", nargs="?", default="")
    tg.set_defaults(fn=cmd_telegram)
    bf = sub.add_parser("backfill", help="rebuild history from existing evidence")
    bf.add_argument("--days", type=int, default=90)
    bf.add_argument("--keep", action="store_true", help="do not clear previous inferred rows")
    bf.set_defaults(fn=cmd_backfill)

    ex = sub.add_parser("export", help="your data out: sessions CSV, or everything as JSON")
    ex.add_argument("--format", choices=["csv", "json"], default="csv",
                    help="csv = sessions; json = sessions, events and briefings")
    ex.add_argument("--days", type=int, default=7, help="the last N days (default 7)")
    ex.add_argument("--from", dest="start", metavar="YYYY-MM-DD")
    ex.add_argument("--to", dest="end", metavar="YYYY-MM-DD")
    ex.add_argument("--afk", action="store_true", help="include away sessions in the CSV")
    ex.add_argument("--out", metavar="FILE", help="output file (- for stdout)")
    ex.set_defaults(fn=cmd_export)

    bk = sub.add_parser("backup", help="snapshot the database (nightly copies are automatic)")
    bk.add_argument("--to", metavar="FILE", help="write the copy here instead of data/backups")
    bk.add_argument("--decrypt", metavar="FILE", help="turn an encrypted backup (.db.enc) back into a database")
    bk.set_defaults(fn=cmd_backup)

    tsp = sub.add_parser("timesheet", help="per-day in/out, hours and projects (this week)")
    tsp.add_argument("--last", action="store_true", help="last week instead of this week")
    tsp.add_argument("--days", type=int, help="the last N days, ending today")
    tsp.add_argument("--from", dest="start", metavar="YYYY-MM-DD")
    tsp.add_argument("--to", dest="end", metavar="YYYY-MM-DD")
    tsp.add_argument("--realm", default="work", choices=["work", "personal", "all"],
                     help="which hours count (default: work)")
    tsp.add_argument("--csv", metavar="FILE", help="write CSV instead (- for stdout)")
    tsp.set_defaults(fn=cmd_timesheet)

    doc = sub.add_parser("doctor", help="check everything and say how to fix what is broken")
    doc.add_argument("-q", "--quiet", action="store_true", help="only show problems")
    doc.add_argument("--slow", action="store_true", help="also check autostart (PowerShell)")
    doc.set_defaults(fn=cmd_doctor)

    sub.add_parser("dash", help="open the dashboard").set_defaults(fn=cmd_dash)
    lo = sub.add_parser("logs", help="tail the log")
    lo.add_argument("--lines", type=int, default=40)
    lo.set_defaults(fn=cmd_logs)

    ins = sub.add_parser("install", help="run ARGUS at login + desktop/Start Menu icons")
    ins.add_argument("--no-shortcuts", action="store_true")
    ins.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove autostart and shortcuts").set_defaults(fn=cmd_uninstall)
    up = sub.add_parser("update", help="pull the latest from GitHub, test, rebuild")
    up.add_argument("--no-tests", action="store_true", help="skip the test run")
    up.add_argument("--force", action="store_true", help="rebuild even if nothing changed")
    up.set_defaults(fn=cmd_update)
    bld = sub.add_parser("build", help="rebuild Argus.exe from source")
    bld.add_argument("--clean", action="store_true", default=True)
    bld.add_argument("--no-restart", action="store_true",
                     help="build, but do not stop a running app to install it")
    bld.add_argument("--no-build", action="store_true",
                     help="install the bundle the last build left in build_dist/")
    bld.set_defaults(fn=cmd_build)

    cf = sub.add_parser("config", help="show or edit config")
    cf.add_argument("--edit", action="store_true")
    cf.set_defaults(fn=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    db.init()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        return cmd_status(args)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
