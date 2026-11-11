"""Text to speech through Windows SAPI. No extra dependency - pywin32 has it.

Speech runs on a single worker thread with a queue, because SAPI COM objects
are not safe to hammer from several threads and overlapping speech is useless.
"""
from __future__ import annotations

import queue
import re
import threading

from .. import config
from ..util import log

lg = log("actions.voice")

_q: "queue.Queue[str | None]" = queue.Queue(maxsize=20)
_worker: threading.Thread | None = None
_lock = threading.Lock()

_MD = re.compile(r"[*_`#>\[\]]|https?://\S+")


def _clean(text: str) -> str:
    """Strip markdown and URLs - they sound terrible read aloud."""
    t = _MD.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:900]


def voices() -> list[str]:
    try:
        import win32com.client
        v = win32com.client.Dispatch("SAPI.SpVoice")
        return [x.GetDescription() for x in v.GetVoices()]
    except Exception:
        return []


def _run() -> None:
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        engine = win32com.client.Dispatch("SAPI.SpVoice")
        want = str(config.get("notify.voice_name", "")).lower()
        if want:
            for v in engine.GetVoices():
                if want in v.GetDescription().lower():
                    engine.Voice = v
                    break
        engine.Rate = 1
        while True:
            item = _q.get()
            if item is None:
                break
            try:
                engine.Speak(item)
            except Exception:
                lg.exception("speak failed")
    except Exception:
        lg.exception("voice engine unavailable")
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def say(text: str) -> bool:
    """Queue text for speech. Non-blocking; drops if the queue is backed up."""
    global _worker
    clean = _clean(text)
    if not clean:
        return False
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="voice", daemon=True)
            _worker.start()
    try:
        _q.put_nowait(clean)
        return True
    except queue.Full:
        lg.warning("voice queue full, dropped")
        return False


def stop() -> None:
    try:
        _q.put_nowait(None)
    except Exception:
        pass
