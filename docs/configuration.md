# Configuration

ARGUS runs with no configuration at all. To change something, copy
`config.example.yaml` to `config.yaml` in the ARGUS folder and edit it
(`argus config --edit` opens it). The daemon picks up a saved change within a
minute.

- A key you leave out falls back to the default in
  [`argus/config.py`](../argus/config.py).
- Dictionaries merge with the defaults; **a list you set replaces the default
  list**.
- A `config.yaml` that fails to parse does not reset you to defaults: the last
  good settings stay in force and `argus doctor` names the broken line.
- Unquoted times such as `at: 20:00` work.

The dashboard's **Settings** page changes the everyday options (name, work
hours, quiet hours, which nudges are on) without touching your file - those
are saved to `data/settings.json` and layered on top.

## First things to set

### Work and personal

ARGUS splits your time into `work` and `personal`. Nothing is built in, because
the words that mean "work" are different for everyone. List yours:

```yaml
realms:
  work_markers: [acme, atlas, jira.acme.example]     # company, projects, internal hosts
  personal_markers: [portfolio, side-project]
  remote_markers: [build-box]                         # terminal titles that mean "remote session"
  work_github_logins: [jane-acme]                     # gh accounts whose PRs are office work
```

Markers are matched, case-insensitively, against window titles, project names
and paths. Personal wins when both match. Two exact joins fill in the rest: a
window whose title names a project inherits that project's realm, and a Claude
Code terminal inherits the realm of the project its session ran in. Nothing is
guessed from timing.

You can also teach it one rule at a time:

```
argus teach --match "atlas" --category code --realm work
argus memory                  what it has learned
```

A correction re-labels matching past sessions straight away and reaches the
running daemon within 30 seconds.

### Where your projects live

```yaml
watch_dirs: [~/Desktop, ~/Documents, ~/Downloads]    # file changes are watched here
git_repos:
  scan_roots: [~/Desktop, '~']                        # repos are discovered here
  scan_depth: 2
threads:
  roots: [~/work, ~/Desktop, '~']                     # a project = first folder below a root
  non_projects: [archive, scratch]                    # folder names that are never a project
```

Leave `threads.roots` empty for the built-in list: `~/Desktop`, `~/Projects`,
`~/code`, `~/dev`, `~/src`, `~/repos`, `~/git`, `~/source/repos`,
`~/Documents/GitHub`, `~`.

### Your hours

```yaml
identity:
  work_hours: {start: '10:30', end: '18:30', days: [0, 1, 2, 3, 4]}   # 0 = Monday
  day_starts_at: '04:00'
```

A day starts at `day_starts_at` everywhere - timesheet, Today tab, briefings -
so work after midnight counts towards the day it belongs to.

## Common edits

| I want to... | Setting |
|---|---|
| Stop notifications at night | `notify.quiet_hours` (only `urgent` breaks through) |
| Have alerts spoken | `notify.voice: true` |
| Turn on screenshots | `sensors.screenshot.enabled: true` |
| Open meeting links automatically | `automations.meeting_sentinel.auto_open_link: true` |
| Mark senders as important | `google.mail_vip: [boss@example.com]` |
| Change the quick-capture hotkey | `board.hotkey` (`''` turns it off) |
| Move the daily reminders | `board.plan_at`, `board.shutdown_at`, `digests.*.at` |
| Never record a folder | `privacy.private_projects` |
| Change the briefing language | `brain.style: "British English, 12h clock."` |
| Use a different port | `daemon.web_port` |
| Switch an automation off | `automations.<name>.enabled: false` |

## The brain

```yaml
brain:
  providers: [claude_cli, gemini, anthropic]   # tried in this order
  claude_cli_model: sonnet
  gemini_model: gemini-2.5-flash
  anthropic_model: claude-sonnet-5
  max_daily_calls: 200
```

| Provider | What it needs |
|---|---|
| `claude_cli` | [Claude Code](https://claude.com/claude-code) installed and signed in. No API key. |
| `gemini` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in your environment. |
| `anthropic` | `ANTHROPIC_API_KEY` in your environment. |

API keys are read from the environment only; they are never written to
`config.yaml` or the database.

## Environment variables

| Variable | Purpose |
|---|---|
| `ARGUS_HOME` | Run against a different data folder. |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Gemini provider. |
| `ANTHROPIC_API_KEY` | Anthropic provider. |

## Section reference

| Section | What it controls |
|---|---|
| `identity` | Your name, working hours, when a day starts. |
| `realms` | Work / personal / remote markers. |
| `daemon` | Dashboard port, tray icon, heartbeat. |
| `sensors` | Each sensor on or off, and how often it polls. |
| `watch_dirs`, `watch_dir_ignores` | Folders watched for file changes, and names skipped inside them. |
| `git_repos` | Where repositories are discovered. |
| `threads` | Project roots and non-project folder names. |
| `privacy` | Private apps, title words, sensitive domains, private projects, retention. |
| `backup` | Nightly snapshot on or off, how many to keep. |
| `brain` | LLM providers, models, limits, style. |
| `notify` | Toasts, voice, quiet hours, minimum severity. |
| `board` | Plan and wrap-up times, hotkey, drift nudges. |
| `digests` | Morning brief, evening wrap, weekly retro, journal. |
| `focus` | What counts as a distraction, deep-work and nudge thresholds. |
| `github`, `devtools` | GitHub cards; commit-hygiene checks. |
| `google` | Accounts, features, VIP senders, polling. See [google.md](google.md). |
| `automations` | Each automation on or off, with its thresholds. |
