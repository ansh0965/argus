# Privacy

ARGUS records what you do on your computer. That only makes sense if you can
see exactly what it keeps and trust where it goes. This page is the full
account; the code is the proof.

## The short version

- Everything is stored in `data/argus.db` on your disk. **No telemetry, no
  account, no upload.**
- **It never records keystrokes.** There is no keylogger in the codebase, and
  `privacy.keylogging` is forced to `False` on every config load.
- **Screenshots are off by default.** If you turn them on they stay local and
  are purged after 3 days.
- The dashboard listens on `127.0.0.1` only.
- ARGUS can read your Google mail and calendar if you connect them. **It never
  sends mail and never deletes anything** unless you explicitly set
  `google.allow_destructive: true`.

## What is recorded

| Sensor | What it stores |
|---|---|
| `window` | Foreground app and window title, merged into sessions; idle time from the last input. |
| `system` | CPU, RAM, disk, battery, network throughput, every 30 seconds. |
| `process` | App launches and exits (helper-process noise filtered out). |
| `files` | That a file changed under your watched folders - name and folder, never contents. Build output, caches and editor temp files are ignored. |
| `browser` | History from Chrome, Edge, Brave and Opera profiles: host, page title, time. |
| `git` | Commits, branch switches, dirty / unpushed state of the repos it finds. |
| `network` | Which hosts the machine talks to, listening ports, autostart entries. |
| `screenshot` | **Off unless you enable it.** |

With Google connected: calendar events, and for mail the sender, subject,
snippet and labels of recent messages.

## What is filtered before storage

- **Private apps** (password managers) and **private titles** (anything with
  "password", "otp", "incognito", "net banking", ...) are stored as
  `<private>` plus a duration.
- **URLs lose their query strings.** Sensitive domains (banks, health,
  account settings) keep the host only.
- **Secrets are masked**: API keys, tokens, JWTs and `password=` patterns are
  replaced before anything is written.
- **Private projects** (`privacy.private_projects`, default `_secure`) are
  never recorded at all: no files, commits, cards or titles.

All four lists are yours to extend in `config.yaml`.

## What leaves the machine

Only what you turn on, and only outbound:

| When | What is sent | To |
|---|---|---|
| A briefing or `argus ask` runs | A text **summary** of your activity for the period (apps, projects, commit subjects, meeting titles) | The LLM provider you configured |
| Google is connected | Normal API requests to read your calendar / mail / tasks, and to create events, drafts or tasks you asked for | Google |
| GitHub cards are on | Queries through your own `gh` CLI | GitHub |
| Telegram is set up | Reminders and important alerts | Your own bot chat |

No LLM configured means nothing is summarised and nothing is sent.

## Pause means pause

`argus pause 60`, the tray icon or the dashboard stop every sensor - windows,
files, processes, network, screenshots. The history collectors (browser, git,
Claude Code transcripts) skip anything timestamped inside the pause, so a
paused hour is not quietly ingested when you resume. Only `urgent` alerts
(critical battery) get through.

## The dashboard is not reachable from other websites

- It binds to `127.0.0.1`.
- The `Host` header must be this machine, which blocks DNS rebinding.
- Anything that changes state must come from the dashboard itself or the CLI,
  which blocks cross-site request forgery.
- It cannot be framed.
- Toasts can only open `https://` links and the local dashboard, so a meeting
  invite with a `file:` link cannot make a click run anything.

## At rest

- Nightly backups are encrypted to your Windows account with DPAPI.
  `argus backup --decrypt <file>` turns one back into a database.
- Google tokens live in `data/secrets/`, with permissions cut down to your
  user.
- The live database is a plain SQLite file. Turn on Windows Device encryption
  (Settings > Privacy & security) to protect it if the laptop is lost.

## Retention

`privacy.retain_days` decides how long things are kept:

| Data | Default |
|---|---|
| Events | 400 days (network snapshots 30, process launches 90) |
| Metrics | 90 days |
| Alerts | 90 days |
| Read mail | 60 days |
| Automation runs | 30 days |
| Screenshots | 3 days |

## Seeing and deleting your data

```
argus privacy                       exactly what is stored, with counts
argus export --format json          everything, as JSON
argus purge --scope all --yes       delete it
```

`argus purge` also takes a narrower `--scope` and a single `--day`.
