# Troubleshooting

Start here, in this order:

```
argus doctor                 every check, with the fix for each
argus logs --lines 80        the traceback behind a failing check
argus status                 sensors alive? brain available? network up?
argus restart
```

The dashboard's System tab runs the same checks, and anything failing shows as
a strip under the header on every tab.

## The dashboard will not load

The daemon is not running. `argus start`, then `argus logs`. If the port is
taken by something else, change `daemon.web_port` in `config.yaml`.

## `argus` is not recognised

Run it from the ARGUS folder as `.\argus`. To use it from anywhere, add the
ARGUS folder to your user `PATH` (Windows Settings: search for "environment
variables") and open a new terminal.

## No briefings, or "brain unavailable"

`argus doctor` says which provider is missing. Install
[Claude Code](https://claude.com/claude-code) and sign in, or set
`GEMINI_API_KEY` or `ANTHROPIC_API_KEY` in your environment, then
`argus restart`. Everything except the written briefings works without one.

## Google keeps asking me to log in again

Your OAuth consent screen is still in Testing mode, which expires tokens after
7 days. Publish the app once - see
[google.md](google.md#tokens-that-expire-every-7-days).

## Everything shows as "unknown" instead of work or personal

ARGUS has no idea what "work" means for you until you tell it. Add your
markers under `realms` in `config.yaml`
([configuration.md](configuration.md#work-and-personal)), then
`argus reclassify`.

## A project is missing, or a folder is wrongly treated as one

Projects are the first folder below one of `threads.roots`. Add the folder
that contains your projects to `threads.roots`, or add a folder name to
`threads.non_projects`. `argus resume --why N` shows the evidence behind a
thread.

## The tray icon is amber or grey

Cyan = watching. Amber = a sensor is down or you are offline. Grey = paused.
`argus status` says which.

## Piping `Argus.exe` output gives nothing

`Argus.exe` is a windowed program, so pipes and `>` do not work on it. Use the
`argus` command for anything on the command line.

## My config change did nothing

- Saved changes are picked up within a minute.
- If the file no longer parses, the last good settings stay in force and
  `argus doctor` tells you which line broke.
- A value you changed on the dashboard's Settings page wins over
  `config.yaml`; it is stored in `data/settings.json`.

## Restoring a backup

1. `argus stop`
2. Move `data/argus.db` aside.
3. Copy the backup you want from `data/backups/` to `data/argus.db`.
   An encrypted one (`.db.enc`) first: `argus backup --decrypt <file>`.
4. Delete any leftover `data/argus.db-wal` and `data/argus.db-shm`.
5. `argus start`

`argus doctor` names the newest backup if the integrity check ever fails.

## Starting over

```
argus purge --scope all --yes
```

or stop ARGUS and delete the `data/` folder. Your `config.yaml` is kept.
