# Contributing to ARGUS

Thanks for wanting to help. ARGUS is a small project with a clear idea of what
it is: a **local-first** assistant that a person can trust with a record of
their working day. Most of what follows comes from that.

## Ground rules

These are not negotiable, and a change that breaks one will not be merged:

1. **Nothing leaves the machine by default.** No telemetry, no analytics, no
   "anonymous" pings. Anything that talks to the network is opt-in, outbound
   only, and described in [docs/privacy.md](docs/privacy.md).
2. **No keystroke logging. Ever.**
3. **ARGUS never sends mail and never deletes a user's Google data** unless
   they set `google.allow_destructive`. Go through `argus/gsuite/guard.py`.
4. **Text from outside is data, not instructions.** Mail subjects, page titles
   and window titles end up in LLM prompts. Keep the model sandboxed and never
   act on what such text says.
5. **No real personal data in the repository** - not in code, comments, tests
   or fixtures. Use `example.com`, "acme", "atlas", "Sam".

## Getting set up

ARGUS is a Windows app (Windows 10 or 11) and needs Python 3.12.

```powershell
git clone https://github.com/ansh0965/argus.git
cd argus
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests -q
```

Run it in the foreground while you work on it:

```powershell
.\argus run          # Ctrl+C to stop
```

`.\argus run` uses the real `data/` folder next to the code. To experiment
without touching your own history, point it somewhere else first:

```powershell
$env:ARGUS_HOME = "$env:TEMP\argus-dev"
.\argus run
```

## Tests

```powershell
.venv\Scripts\python -m pytest tests -q
```

- The suite runs against a throwaway `ARGUS_HOME` and refuses to start
  otherwise, so it can never touch your data.
- **New behaviour needs a test.** A bug fix needs a test that fails without it.
- You cannot drive the desktop app from a test. Verify UI changes by reading
  the served HTML and JSON through FastAPI's `TestClient` (see
  `tests/test_web_endpoints.py`), and test Windows-only code through its pure
  functions.
- A test that genuinely needs Windows may be skipped elsewhere with
  `pytest.mark.skipif(sys.platform != "win32", ...)`. Never delete one to make
  the suite pass.

## Style

Match the code around you:

- Plain functions and modules; classes only where state really needs one.
- **Comments explain why**, especially where behaviour is non-obvious or a bug
  taught us something. Many of the long comments in this codebase are the
  record of a real failure - keep that habit.
- User-facing text is plain and specific: say what happened and what to do.
- No new dependency without discussing it in an issue first. Every dependency
  is something users have to trust and the frozen app has to carry.
- The dashboard is one HTML file with no build step. Keep it that way; use
  the existing CSS variables and the `api`, `esc`, `jsattr`, `dur` and `flash`
  helpers.
- Anything machine- or person-specific belongs in `config.yaml`, with a
  default in `argus/config.py` and a line in
  [docs/configuration.md](docs/configuration.md).

## Commits and pull requests

- One logical change per commit, with a message that says what changed and
  why. The history uses [Conventional Commits](https://www.conventionalcommits.org):
  `feat(board): ...`, `fix(config): ...`, `docs: ...`, `chore: ...`.
- Open an issue before a large change, so we agree on the shape first.
- Fill in the pull request template, including the privacy checklist.
- Add a line to [CHANGELOG.md](CHANGELOG.md) under *Unreleased* for anything a
  user would notice.

## Where things are

[docs/architecture.md](docs/architecture.md) maps the codebase. Good first
places to look:

| You want to... | Start in |
|---|---|
| Teach it a new app or site | `argus/classify.py` |
| Add a sensor | `argus/sensors/`, then register it in `argus/daemon.py` |
| Add an automation | `argus/automations/builtin.py` |
| Add a CLI command | `argus/cli.py` |
| Add a health check | `argus/doctor.py` |
| Change the dashboard | `argus/web/static/index.html`, `argus/web/server.py` |

## Reporting bugs and security issues

- Bugs and ideas: [open an issue](https://github.com/ansh0965/argus/issues).
  **Strip private details from logs first** - window titles, file names, mail
  subjects.
- Security problems: please do not open a public issue. See
  [SECURITY.md](SECURITY.md).

By contributing you agree that your contribution is licensed under the
project's [MIT License](LICENSE), and to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
