"""Quick capture: a global hotkey that drops a line onto the board from any app.

Ctrl+Alt+Space (board.hotkey) opens a small box on top of whatever you are
doing; type "call Sam about the login bug fix tomorrow", press Enter, and it is
an Inbox card with a due date. Esc closes it.

This is RegisterHotKey - Windows tells us when that one combination is
pressed. Nothing else you type is seen; there is no keyboard hook. Ctrl+Space
was the first idea and is deliberately not the default: VS Code and most
editors use it for completion, and a global hotkey would steal it.
"""
from __future__ import annotations

import ctypes
import threading

from ..util import log

lg = log("actions.capture")

MODS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
_NAMED = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "insert": 0x2D,
          **{f"f{i}": 0x6F + i for i in range(1, 13)}}
HOTKEY_ID = 0xA7C5


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+space' -> (modifiers, virtual key). Raises ValueError."""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mods, key = parts
    m = 0
    for x in mods:
        if x not in MODS:
            raise ValueError(f"unknown modifier {x!r}")
        m |= MODS[x]
    if not m:
        raise ValueError("a global hotkey needs at least one modifier")
    if key in _NAMED:
        vk = _NAMED[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    else:
        raise ValueError(f"unknown key {key!r}")
    return m, vk


def _popup() -> None:
    """The capture box. Runs on the hotkey thread and returns when closed."""
    import tkinter as tk

    from ..brain import board
    from . import notify

    root = tk.Tk()
    root.title("ARGUS - capture")
    root.attributes("-topmost", True)
    root.overrideredirect(True)
    root.configure(bg="#11141b")
    w, h = 620, 64
    x = (root.winfo_screenwidth() - w) // 2
    y = int(root.winfo_screenheight() * 0.28)
    root.geometry(f"{w}x{h}+{x}+{y}")
    frame = tk.Frame(root, bg="#11141b", highlightbackground="#22d3ee", highlightthickness=1)
    frame.pack(fill="both", expand=True)
    var = tk.StringVar()
    entry = tk.Entry(frame, textvariable=var, font=("Segoe UI", 15), bg="#11141b", fg="#e7eaf0",
                     insertbackground="#22d3ee", relief="flat", bd=0)
    entry.pack(fill="both", expand=True, padx=18, pady=14)
    hint = "Add to ARGUS… (Enter to save, Esc to close)"
    entry.insert(0, hint)
    entry.configure(fg="#5b6373")

    def clear_hint(_e=None):
        if var.get() == hint:
            entry.delete(0, "end")
            entry.configure(fg="#e7eaf0")

    def save(_e=None):
        text = var.get().strip()
        root.destroy()
        if text and text != hint:
            try:
                c = board.capture(text)
                notify.send("Added to your board", c.get("message") or c["title"], severity="info",
                            rule="capture", record=False, url=notify.dashboard("board"))
            except Exception as e:
                lg.warning("capture failed: %s", e)

    entry.bind("<Key>", clear_hint, add="+")
    entry.bind("<Return>", save)
    root.bind("<Escape>", lambda _e: root.destroy())
    root.bind("<FocusOut>", lambda _e: root.after(150, lambda: root.focus_get() or root.destroy()))
    root.after(30, lambda: (root.focus_force(), entry.focus_set(), entry.icursor(0)))
    root.mainloop()


def run(stop: threading.Event, spec: str = "ctrl+alt+space") -> None:
    """Register the hotkey and serve it until `stop`. Windows only."""
    try:
        mods, vk = parse_hotkey(spec)
    except ValueError as e:
        lg.warning("capture hotkey %r not usable: %s", spec, e)
        return
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except (AttributeError, ImportError):
        return                                   # not Windows
    if not user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
        lg.warning("capture hotkey %s is taken by another app; capture from the dashboard", spec)
        return
    lg.info("quick capture on %s", spec)
    msg = wintypes.MSG()
    PM_REMOVE = 0x0001
    try:
        while not stop.is_set():
            # PeekMessage rather than GetMessage, so stop is noticed.
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    try:
                        _popup()
                    except Exception:
                        lg.exception("capture box failed")
            stop.wait(0.05)
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)
