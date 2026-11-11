"""Entry point:  python -m argus   /   Argus.exe

This wrapper exists for a specific reason. Launching with `python -m argus.daemon`
would execute daemon.py under the name `__main__`, so any later `import argus.daemon`
(the web server does exactly that to read sensor health) would load a *second*
copy of the module with empty globals. Importing it normally from here keeps one
set of module state for the whole process.

It also gives Argus.exe app-like behaviour: double-clicking it when ARGUS is
already running opens the dashboard instead of failing on the single-instance
mutex, which is what any normal Windows app does.
"""
from __future__ import annotations

import sys
import webbrowser


def _dashboard_url() -> str:
    from . import config
    return f"http://127.0.0.1:{config.get('daemon.web_port', 7842)}"


def _attach_console() -> bool:
    """Borrow the calling terminal's console.

    Argus.exe is built windowed so a double-click never flashes a black box.
    That normally means CLI output goes nowhere. Attaching to the parent
    console and reopening the std streams onto it gives one binary that is both
    a GUI app and a command-line tool - the same trick pythonw uses.
    """
    try:
        import ctypes
        ATTACH_PARENT = -1
        if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT) == 0:
            return False
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8")
        except Exception:
            pass
        return True
    except Exception:
        return False


def main() -> int:
    from . import db
    from .daemon import daemon_alive, main as daemon_main

    argv = sys.argv[1:]
    # Anything that is not one of our own launch flags is a CLI subcommand.
    if argv and not argv[0].startswith("--"):
        _attach_console()
        from .cli import main as cli_main
        return cli_main(argv)

    db.init()
    args = set(argv)

    # Second double-click: show the UI rather than dying on the mutex.
    if daemon_alive() and "--force" not in args:
        webbrowser.open(_dashboard_url())
        return 0

    # A visible launch (double-click, Start Menu) should show something.
    if "--quiet" not in args:
        import threading
        threading.Timer(6.0, lambda: webbrowser.open(_dashboard_url())).start()

    return daemon_main()


if __name__ == "__main__":
    sys.exit(main())
