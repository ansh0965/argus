"""Privacy gate. Every string a sensor captures passes through here before storage.

Three defences, in order:
  1. private_app / private_title  -> the title is dropped entirely, only duration survives
  2. secret scrubbing             -> tokens, keys and password-ish substrings are masked
  3. url_trim                     -> query strings and fragments are stripped from URLs

There is deliberately no keystroke capture anywhere in this codebase.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from . import config

# Well-known secret shapes. Ordered most-specific first.
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "<anthropic-key>"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "<api-key>"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "<google-key>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "<github-token>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "<slack-token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<aws-key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "<jwt>"),
    (re.compile(r"-----BEGIN[^-]{0,40}PRIVATE KEY-----"), "<private-key>"),
    (re.compile(r"\b[0-9]{12,19}\b(?=.*(?:card|cvv|expiry))", re.I), "<card-number>"),
    (re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_\-]?key)\s*[:=]\s*\S+"),
     r"\g<0>"),  # replaced below with a masking function
]

_KV_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_\-]?key|auth|bearer)\b\s*[:=]\s*[^\s,;]{3,}"
)
_EMAILISH = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _cfg() -> dict:
    return config.get("privacy", {}) or {}


def scrub(text: str) -> str:
    """Mask secret-looking substrings. Safe to call on anything."""
    if not text:
        return ""
    out = text
    for pat, repl in SECRET_PATTERNS[:-1]:
        out = pat.sub(repl, out)
    out = _KV_SECRET.sub(lambda m: f"{m.group(1)}=<redacted>", out)
    return out


def mask_email(text: str) -> str:
    """Keep the domain, drop the local part: a****@gmail.com."""
    def _m(m: re.Match[str]) -> str:
        addr = m.group(0)
        local, _, dom = addr.partition("@")
        return f"{local[:1]}***@{dom}"
    return _EMAILISH.sub(_m, text or "")


def is_private_app(exe: str, app: str) -> bool:
    names = {(exe or "").lower(), (app or "").lower()}
    for p in _cfg().get("private_apps", []):
        p = p.lower()
        if any(p in n for n in names if n):
            return True
    return False


def _word_rx(word: str) -> re.Pattern[str]:
    return _WORD_CACHE.get(word) or _WORD_CACHE.setdefault(
        word, re.compile(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])"))


_WORD_CACHE: dict[str, re.Pattern[str]] = {}


def is_private_title(title: str) -> bool:
    """Short words match as whole words, longer phrases as substrings.

    Plain substring matching made "otp" hide any title containing
    "Footprint" or "hotpot", and "cvv" anything with it inside a longer
    token - dropping real activity for no privacy gain. Phrases of five or
    more characters ("password", "net banking") still match anywhere, so
    "Passwords - Settings" and "ResetPassword.tsx" stay private.
    """
    t = (title or "").lower()
    for w in _cfg().get("private_title_words", []):
        w = str(w).lower().strip()
        if not w:
            continue
        if len(w) <= 4:
            if _word_rx(w).search(t):
                return True
        elif w in t:
            return True
    return False


def is_sensitive_domain(host: str) -> bool:
    h = (host or "").lower()
    return any(d.lower() in h for d in _cfg().get("sensitive_domains", []))


def private_project_in(text: str) -> bool:
    """True when a path or title belongs to a project marked private
    (privacy.private_projects): a folder name, matched as a whole path
    component or a whole word - "_secure" hides "_SECURE (keys & passwords)",
    "clientx" hides C:\\work\\clientx\\... and "clientx - Visual Studio Code"."""
    t = (text or "").lower()
    if not t:
        return False
    for name in _cfg().get("private_projects", []) or []:
        n = str(name).lower().strip()
        if n and re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", t):
            return True
    return False


def clean_title(exe: str, app: str, title: str) -> tuple[str, bool]:
    """Return (safe_title, was_private). Private titles become '<private>'."""
    if is_private_app(exe, app) or is_private_title(title) or private_project_in(title):
        return "<private>", True
    return scrub(title or "")[:500], False


def trim_url(url: str) -> str:
    """Strip query + fragment; drop the path too on sensitive hosts."""
    if not url:
        return ""
    try:
        p = urlsplit(url)
    except Exception:
        return scrub(url)[:300]
    if is_sensitive_domain(p.netloc):
        return f"{p.scheme}://{p.netloc}/<private>"
    return scrub(urlunsplit((p.scheme, p.netloc, p.path, "", "")))[:300]


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def clean_path(path: str) -> str:
    """File paths are stored as-is except for secret-bearing filenames."""
    p = scrub(path or "")
    low = p.lower()
    for marker in (".env", "credentials", "id_rsa", "secret", ".pem", ".key", "token"):
        if marker in low:
            # keep the directory, mask the leaf so you still see *where* it happened
            head, sep, leaf = p.rpartition("\\")
            if sep:
                return f"{head}\\<sensitive:{leaf[-6:]}>"
            return "<sensitive-file>"
    return p[:400]
