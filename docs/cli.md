# Command line

`argus` with no arguments shows the status screen. `argus <command> --help`
lists a command's options.

## Every day

| Command | What it does |
|---|---|
| `argus status` | What is running right now: sensors, brain, network. |
| `argus today` / `yesterday` / `day 2026-09-08` | The activity report for a day. |
| `argus ask "how did I spend Tuesday?"` | Ask about your own activity. Understands "Tuesday", "last Friday", "8 Sep", "this week". `--speak` reads the answer aloud. |
| `argus resume [N]` | Where you left off, by project. `--why N` shows the evidence. |
| `argus watch` | Live tail of what you are doing. |
| `argus dash` | Open the dashboard. |
| `argus brief` / `wrap` / `retro` | Generate the morning brief, evening wrap or weekly retro now. |

## Tasks, mail and calendar

| Command | What it does |
|---|---|
| `argus task add "..."` / `task list` / `task done 3` | Tasks. |
| `argus mail` | Important unread mail (after Google login). |
| `argus cal` | Upcoming calendar (`--hours 36`). |
| `argus timesheet [--last] [--csv week.csv]` | Per-day in / out, work hours, projects, commits. `--realm work\|personal\|all`. |
| `argus alerts [-v] [--ack]` | Recent alerts; `--ack` marks them read. |
| `argus repos [-v]` | State of every git repo it found. |
| `argus runs [--all]` | What the automations did (no-op runs hidden unless `--all`). |

## Control

| Command | What it does |
|---|---|
| `argus start` / `stop` / `restart` | The background daemon. `stop` asks it to exit cleanly first, so open sessions are flushed. |
| `argus run` | Run in the foreground, for debugging (`--no-web`, `--no-tray`). |
| `argus pause 60` / `unpause` | Stop all watching and alerts for N minutes. |
| `argus config [--edit]` | Show or open `config.yaml`. |
| `argus teach "..."` | Tell ARGUS something to remember. `--match TEXT --category C --realm R` adds a classification rule. |
| `argus memory` | What it has learned. |
| `argus reclassify [--days N]` | Re-derive stored categories with the current rules. |

## Accounts and integrations

| Command | What it does |
|---|---|
| `argus google login --label NAME` | Connect a Google account. Also `list`, `logout`, `relogin`, `sync`. See [google.md](google.md). |
| `argus telegram setup TOKEN` | Reminders on your phone. Also `status`, `test`, `off`. |

## Your data

| Command | What it does |
|---|---|
| `argus privacy` | Exactly what is stored. |
| `argus export [--format json] [--days N]` | Sessions as CSV, or everything as JSON. `--from` / `--to` / `--out FILE`. |
| `argus backup [--to FILE]` | A consistent snapshot of the database (also nightly). `--decrypt FILE` restores an encrypted one. |
| `argus backfill --days 90` | Rebuild history from browser, git and Claude Code evidence. Safe to re-run. |
| `argus purge --scope all --yes` | Delete stored data. Scopes: `all`, `browser`, `screenshots`, `threads`, `day` (with `--day`). |

## Install and maintain

| Command | What it does |
|---|---|
| `argus doctor [-q] [--slow]` | Check everything and say how to fix what is broken. Exits 1 if something is. |
| `argus logs --lines 60` | Tail the log. |
| `argus install` | Start at login, plus Desktop and Start Menu shortcuts (`--no-shortcuts`). |
| `argus uninstall` | Remove both. |
| `argus build` | Freeze the source into `Argus.exe` and swap it in. |
| `argus update` | Pull the latest from your git remote (fast-forward only, never over local changes), run the tests, and rebuild only if they pass. |
