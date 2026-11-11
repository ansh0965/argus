"""Frozen entry point for Argus.exe.

PyInstaller executes its entry script under the name `__main__`, which strips it
of a parent package and breaks every relative import inside it. So the exe
starts here instead: a plain top-level module that imports the real entry point
by its absolute name, giving `argus/__main__.py` the package context its
`from . import ...` statements need.
"""
import os
import sys


def _ensure_streams() -> None:
    """A windowed exe has sys.stdout/stderr set to None.

    Any library that touches them - uvicorn's log formatter calling isatty(),
    a stray print() - then raises deep inside a worker thread. Pointing them at
    the null device costs nothing and removes the whole class of crash. The CLI
    path replaces these again with the real console in _attach_console().
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass


_ensure_streams()

from argus.__main__ import main  # noqa: E402  (must follow stream setup)

if __name__ == "__main__":
    sys.exit(main())
