"""Filesystem layout for ARGUS. Single source of truth for every path.

Works in two modes:

  source   - running from the checkout, ROOT is the folder holding the package
  frozen   - running as Argus.exe, ROOT is the folder holding the exe

`ARGUS_HOME` overrides both, which is what makes it possible to point a frozen
build at an existing data folder while testing.

Program code and user data are deliberately kept in the same folder so the app
is fully portable: copy the folder, keep your history.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _root() -> Path:
    override = os.environ.get("ARGUS_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


FROZEN = _is_frozen()
ROOT = _root()

# Read-only bundled assets (the dashboard HTML). PyInstaller unpacks these to a
# temp dir exposed as sys._MEIPASS; from source they sit inside the package.
BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT)) if FROZEN else Path(__file__).resolve().parent

DATA = ROOT / "data"
LOGS = ROOT / "logs"
SECRETS = DATA / "secrets"
SHOTS = DATA / "screenshots"
JOURNAL = DATA / "journal"
BACKUPS = DATA / "backups"
DB_PATH = DATA / "argus.db"
CONFIG_PATH = ROOT / "config.yaml"
ICON_ICO = ROOT / "assets" / "argus.ico"

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
VENV_PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"

# The two frozen entry points, when they exist.
APP_EXE = ROOT / "Argus.exe"
CLI_EXE = ROOT / "argus.exe"

for _d in (DATA, LOGS, SECRETS, SHOTS, JOURNAL):
    _d.mkdir(parents=True, exist_ok=True)


def asset(*parts: str) -> Path:
    """Locate a bundled read-only asset, frozen or not."""
    if FROZEN:
        # --add-data puts these under <MEIPASS>/argus/<...>
        p = Path(getattr(sys, "_MEIPASS", ROOT)) / "argus" / Path(*parts)
        if p.exists():
            return p
        return Path(getattr(sys, "_MEIPASS", ROOT)) / Path(*parts)
    return Path(__file__).resolve().parent / Path(*parts)


def app_is_current() -> bool:
    """True when Argus.exe exists and is at least as new as the source."""
    if not APP_EXE.exists():
        return False
    src = ROOT / "argus"
    try:
        newest = max((p.stat().st_mtime for p in src.rglob("*")
                      if p.suffix in (".py", ".html")), default=0.0)
        return APP_EXE.stat().st_mtime >= newest - 60
    except OSError:
        return True


def daemon_command() -> list[str]:
    """How to launch the background daemon from wherever we are now.

    From the CLI this used to be the venv interpreter whenever it ran from
    source - i.e. always - so `argus start` and every rebuild's restart ran
    `pythonw -m argus` while login and the Desktop icon ran Argus.exe. Now
    the app itself is preferred, unless the source has moved on since it was
    built (then the source is what is current).
    """
    if FROZEN and APP_EXE.exists():
        return [str(APP_EXE)]
    if app_is_current():
        return [str(APP_EXE)]
    py = VENV_PYW if VENV_PYW.exists() else VENV_PY
    return [str(py), "-m", "argus"]


def cli_command() -> list[str]:
    if FROZEN and CLI_EXE.exists():
        return [str(CLI_EXE)]
    py = VENV_PY
    return [str(py), "-m", "argus.cli"]


def harden_secrets() -> None:
    """Lock the secrets dir to the current user only (best effort)."""
    try:
        import subprocess
        user = os.environ.get("USERNAME", "")
        if not user:
            return
        # icacls is a console app; ARGUS runs windowless, so without
        # CREATE_NO_WINDOW each call gets a brand-new *visible* console.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        subprocess.run(
            ["icacls", str(SECRETS), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
            capture_output=True, timeout=15,
            creationflags=flags, startupinfo=si,
        )
    except Exception:
        pass
