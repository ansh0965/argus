# Changelog

All notable changes to ARGUS are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - first public release

The first version published as open source.

### What ARGUS does

- **Watches locally**: foreground windows, files, git, browser history,
  processes, network and system health, into one SQLite file. Screenshots are
  off by default; keystrokes are never recorded.
- **The board**: Inbox / Today / Doing / Parked / Done, quick capture from any
  app, reminders, calendar scheduling and time blocking, focus timers,
  plan-my-day, wrap-up and a weekly review.
- **Cards it creates itself**: uncommitted and unpushed work, projects that
  went quiet, GitHub reviews, failing CI and requested changes, stale merged
  branches.
- **Briefings**: morning brief, evening wrap, weekly retro, standup draft, and
  `argus ask` over your own activity - through the `claude` CLI, Gemini or the
  Anthropic API.
- **Automations**: focus guardian, meeting sentinel, inbox triage, repo
  janitor, system watchdog, security watch, break reminder, downloads janitor.
- **Google**: several accounts, with per-account limits on what may be read.
  Read and create only; never send, never delete.
- **Telegram**: reminders on your phone, capture from it.
- **Privacy**: redaction of private apps and titles, URL stripping, secret
  masking, private projects, a real pause, encrypted nightly backups,
  retention limits, export and purge.
- **A desktop app**: tray icon, autostart, a dashboard on `127.0.0.1`, and
  `argus build` to freeze it into `Argus.exe`.
- **`argus doctor`**: health checks that each come with their fix.

### Changed for the public release

- Work and personal markers, remote-session markers, office GitHub logins,
  project roots and non-project folder names are now settings (`realms`,
  `threads`) with no built-in values. Previously they were hardcoded.
- The briefing language is a setting (`brain.style`).
- Default watch folders and repository roots are generic.
- `requirements.txt` lists only what ARGUS itself needs.

[Unreleased]: https://github.com/ansh0965/argus/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ansh0965/argus/releases/tag/v0.1.0
