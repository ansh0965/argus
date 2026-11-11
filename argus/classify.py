"""Turn a raw (exe, window title) pair into (app, category, realm).

Category answers "what kind of activity is this".
Realm answers "is this office work, personal, or unclear".
Both are heuristics that improve over time via db.memory entries of category
'classify' written by the learn/feedback path.
"""
from __future__ import annotations

import re
import threading
import time

from . import config, db

# exe stem -> (pretty name, category)
APPS: dict[str, tuple[str, str]] = {
    "code": ("VS Code", "code"),
    "code - insiders": ("VS Code Insiders", "code"),
    "cursor": ("Cursor", "code"),
    "antigravity": ("Antigravity", "code"),
    "devenv": ("Visual Studio", "code"),
    "idea64": ("IntelliJ", "code"),
    "pycharm64": ("PyCharm", "code"),
    "studio64": ("Android Studio", "code"),
    "eclipse": ("Eclipse", "code"),
    "sublime_text": ("Sublime", "code"),
    "notepad++": ("Notepad++", "code"),
    "windowsterminal": ("Terminal", "terminal"),
    "wt": ("Terminal", "terminal"),
    "powershell": ("PowerShell", "terminal"),
    "pwsh": ("PowerShell", "terminal"),
    "cmd": ("Command Prompt", "terminal"),
    "mintty": ("Git Bash", "terminal"),
    "conhost": ("Console", "terminal"),
    "ubuntu": ("WSL", "terminal"),
    "wsl": ("WSL", "terminal"),
    "chrome": ("Chrome", "browse"),
    "msedge": ("Edge", "browse"),
    "brave": ("Brave", "browse"),
    "firefox": ("Firefox", "browse"),
    "opera": ("Opera", "browse"),
    "arc": ("Arc", "browse"),
    "slack": ("Slack", "comms"),
    "teams": ("Teams", "comms"),
    "ms-teams": ("Teams", "comms"),
    "outlook": ("Outlook", "comms"),
    "olk": ("Outlook", "comms"),
    "discord": ("Discord", "comms"),
    "whatsapp": ("WhatsApp", "comms"),
    "telegram": ("Telegram", "comms"),
    "zoom": ("Zoom", "meeting"),
    "skype": ("Skype", "meeting"),
    "winword": ("Word", "docs"),
    "excel": ("Excel", "docs"),
    "powerpnt": ("PowerPoint", "docs"),
    "onenote": ("OneNote", "docs"),
    "acrobat": ("Acrobat", "docs"),
    "acrord32": ("Acrobat Reader", "docs"),
    "sumatrapdf": ("SumatraPDF", "docs"),
    "notion": ("Notion", "docs"),
    "obsidian": ("Obsidian", "docs"),
    "figma": ("Figma", "design"),
    "photoshop": ("Photoshop", "design"),
    "illustrator": ("Illustrator", "design"),
    "blender": ("Blender", "design"),
    "upscayl": ("Upscayl", "design"),
    "vlc": ("VLC", "media"),
    "mpc-hc64": ("MPC-HC", "media"),
    "spotify": ("Spotify", "media"),
    "steam": ("Steam", "game"),
    "epicgameslauncher": ("Epic Games", "game"),
    "explorer": ("File Explorer", "system"),
    "taskmgr": ("Task Manager", "system"),
    "systemsettings": ("Settings", "system"),
    "virtualboxvm": ("VirtualBox", "vm"),
    "virtualbox": ("VirtualBox", "vm"),
    "docker desktop": ("Docker", "vm"),
    "vmware": ("VMware", "vm"),
    "postman": ("Postman", "code"),
    "wireshark": ("Wireshark", "code"),
    "ssms": ("SSMS", "code"),
    "mongodbcompass": ("Compass", "code"),
    "packettracer": ("Packet Tracer", "study"),
    "anaconda-navigator": ("Anaconda", "code"),
    "jupyter": ("Jupyter", "code"),
    "lockapp": ("Lock Screen", "idle"),
    "searchhost": ("Search", "system"),
    "shellexperiencehost": ("Shell", "system"),
    "textinputhost": ("Input", "system"),
    # Added after measuring what landed in "other": the Claude desktop app was
    # 1.4h/fortnight of uncategorised time, Remote Desktop to the company VM
    # another 0.8h.
    "claude": ("Claude", "ai"),
    "chatgpt": ("ChatGPT", "ai"),
    "mstsc": ("Remote Desktop", "remote"),
    "anydesk": ("AnyDesk", "remote"),
    "teamviewer": ("TeamViewer", "remote"),
    "rustdesk": ("RustDesk", "remote"),
    "putty": ("PuTTY", "remote"),
    "mobaxterm": ("MobaXterm", "remote"),
    "termius": ("Termius", "remote"),
    "winscp": ("WinSCP", "remote"),
    "filezilla": ("FileZilla", "remote"),
    "windsurf": ("Windsurf", "code"),
    "zed": ("Zed", "code"),
    "rider64": ("Rider", "code"),
    "webstorm64": ("WebStorm", "code"),
    "goland64": ("GoLand", "code"),
    "clion64": ("CLion", "code"),
    "datagrip64": ("DataGrip", "code"),
    "phpstorm64": ("PhpStorm", "code"),
    "rustrover64": ("RustRover", "code"),
    "dbeaver": ("DBeaver", "code"),
    "pgadmin4": ("pgAdmin", "code"),
    "mysqlworkbench": ("MySQL Workbench", "code"),
    "insomnia": ("Insomnia", "code"),
    "githubdesktop": ("GitHub Desktop", "code"),
    "gitkraken": ("GitKraken", "code"),
    "sourcetree": ("Sourcetree", "code"),
    "warp": ("Warp", "terminal"),
    "alacritty": ("Alacritty", "terminal"),
    "wezterm-gui": ("WezTerm", "terminal"),
    "tabby": ("Tabby", "terminal"),
    "vivaldi": ("Vivaldi", "browse"),
    "zen": ("Zen", "browse"),
    "mattermost": ("Mattermost", "comms"),
    "signal": ("Signal", "comms"),
    "todoist": ("Todoist", "planning"),
    "ticktick": ("TickTick", "planning"),
    "notepad": ("Notepad", "docs"),
    "stremio": ("Stremio", "media"),
    "photos": ("Photos", "media"),
    "obs64": ("OBS", "media"),
    "vmconnect": ("Hyper-V", "vm"),
    "snippingtool": ("Snipping Tool", "system"),
    "applicationframehost": ("Windows App", "system"),
    "credentialuibroker": ("Credentials", "system"),
    "openwith": ("Open With", "system"),
    "pickerhost": ("File Picker", "system"),
    "shellhost": ("Shell", "system"),
}

# Browser window titles are "<page> - <Site>" (plus " - Google Chrome"), and
# they almost never contain the domain - so DOMAINS alone left YouTube (2.6h a
# fortnight here), claude.ai, GitHub repo pages, Mattermost and WhatsApp Web
# all as neutral "browse". The site name is matched as a whole word in the last
# segment of the title.
SITES: list[tuple[str, str]] = [
    ("youtube", "media"), ("netflix", "media"), ("prime video", "media"),
    ("hotstar", "media"), ("jiocinema", "media"), ("spotify", "media"),
    ("twitch", "media"), ("stremio", "media"), ("crunchyroll", "media"),
    ("github", "code"), ("gitlab", "code"), ("bitbucket", "code"),
    ("stack overflow", "code"), ("vercel", "code"), ("netlify", "code"),
    ("supabase", "code"), ("strapi", "code"), ("postman", "code"), ("render", "code"),
    ("railway", "code"), ("firebase", "code"), ("cloudflare", "code"), ("npm", "code"),
    ("pypi", "code"), ("docker hub", "code"), ("sentry", "code"), ("grafana", "code"),
    ("jenkins", "code"), ("aws", "code"), ("google cloud", "code"), ("mdn", "code"),
    ("claude", "ai"), ("chatgpt", "ai"), ("gemini", "ai"), ("perplexity", "ai"),
    ("copilot", "ai"), ("hugging face", "ai"), ("google ai studio", "ai"),
    ("deepseek", "ai"), ("grok", "ai"), ("notebooklm", "ai"),
    ("gmail", "comms"), ("outlook", "comms"), ("whatsapp", "comms"), ("slack", "comms"),
    ("mattermost", "comms"), ("discord", "comms"), ("telegram", "comms"),
    ("microsoft teams", "comms"), ("messenger", "comms"),
    ("google meet", "meeting"), ("meet", "meeting"), ("zoom", "meeting"),
    ("google docs", "docs"), ("google sheets", "docs"), ("google slides", "docs"),
    ("google drive", "docs"), ("notion", "docs"), ("overleaf", "docs"),
    ("confluence", "docs"), ("miro", "docs"),
    ("jira", "planning"), ("linear", "planning"), ("trello", "planning"),
    ("asana", "planning"), ("clickup", "planning"), ("google calendar", "planning"),
    ("instagram", "social"), ("facebook", "social"), ("reddit", "social"),
    ("linkedin", "social"), ("quora", "social"), ("twitter", "social"),
    ("pinterest", "social"),
    ("leetcode", "study"), ("coursera", "study"), ("udemy", "study"),
    ("geeksforgeeks", "study"), ("arxiv", "study"), ("w3schools", "study"),
    ("nptel", "study"), ("codeforces", "study"), ("hackerrank", "study"),
    ("amazon", "shopping"), ("flipkart", "shopping"), ("myntra", "shopping"),
    ("swiggy", "shopping"), ("zomato", "shopping"), ("blinkit", "shopping"),
    ("zepto", "shopping"), ("meesho", "shopping"),
    ("figma", "design"), ("canva", "design"), ("dribbble", "design"), ("behance", "design"),
]
_SITE_RE = [(re.compile(r"(?<![\w.])" + re.escape(name) + r"(?![\w.])"), cat)
            for name, cat in SITES]
# " - Google Chrome", and Edge's " - Personal - Microsoft Edge" (profile name:
# plain words only, so it can never swallow a " - Site" segment before it).
_BROWSER_SUFFIX = re.compile(
    r"(\s+and \d+ more pages?)?\s+[-—–]\s+"
    r"(?:(?:[\w.']+(?: [\w.']+)*)\s+[-—–]\s+(?=microsoft\W*edge))?"
    r"(google chrome|microsoft\W*edge|brave|opera|mozilla firefox|vivaldi|arc|zen)\s*$")
_SEP = re.compile(r"\s+[-—–·|]\s+")
# Shapes that identify a site without its name.
_REPO = re.compile(r"^[\w.-]+/[\w.-]+(?::.*)?$")               # GitHub: owner/repo
_MEET_CODE = re.compile(r"\b[a-z]{3}-[a-z]{4}-[a-z]{3}\b")      # Meet: abc-defg-hij
_UNREAD = re.compile(r"^\(\d+\)\s*")                             # "(3) WhatsApp"


def site_category(title: str) -> str | None:
    """Category from a browser page title's site name, or None."""
    t = _BROWSER_SUFFIX.sub("", (title or "").lower().replace("​", "")).strip()
    if not t:
        return None
    segs = [s for s in _SEP.split(_UNREAD.sub("", t)) if s]
    if not segs:
        return None
    if _MEET_CODE.search(t) and ("meet" in t or len(segs) <= 2):
        return "meeting"
    # A site name lives in a short final segment ("... - Acme Mattermost").
    # A one-segment title only counts when it *starts* with the name ("YouTube",
    # "(2) WhatsApp"), or "The render loop explained" becomes render.com.
    if len(segs) > 1:
        tail = segs[-1] if len(segs[-1].split()) <= 4 else ""
        hit = lambda rx: rx.search(tail)                              # noqa: E731
    elif len(segs[0].split()) <= 3:                  # "Acme Mattermost"
        tail = segs[0]
        hit = lambda rx: rx.search(tail)                              # noqa: E731
    else:
        tail = segs[0]
        hit = lambda rx: (m := rx.search(tail)) is not None and m.start() == 0  # noqa: E731
    if tail:
        for rx, cat in _SITE_RE:
            if hit(rx):
                return cat
    if any(_REPO.match(s) for s in segs):
        return "code"
    return None

# domain fragment -> category, used when the foreground app is a browser
DOMAINS: list[tuple[str, str]] = [
    ("mail.google.com", "comms"), ("outlook.office", "comms"), ("web.whatsapp", "comms"),
    ("teams.microsoft", "comms"), ("slack.com", "comms"), ("discord.com", "comms"),
    ("meet.google.com", "meeting"), ("zoom.us", "meeting"), ("teams.live", "meeting"),
    ("calendar.google", "planning"), ("tasks.google", "planning"), ("trello", "planning"),
    ("jira", "planning"), ("linear.app", "planning"), ("asana", "planning"),
    ("notion.so", "docs"), ("docs.google", "docs"), ("sheets.google", "docs"),
    ("drive.google", "docs"), ("slides.google", "docs"), ("overleaf", "docs"),
    ("github.com", "code"), ("gitlab", "code"), ("stackoverflow", "code"),
    ("stackexchange", "code"), ("localhost", "code"), ("127.0.0.1", "code"),
    ("vercel.com", "code"), ("netlify", "code"), ("supabase", "code"),
    ("console.cloud.google", "code"), ("aws.amazon.com/console", "code"),
    ("claude.ai", "ai"), ("chatgpt.com", "ai"), ("chat.openai", "ai"),
    ("gemini.google", "ai"), ("perplexity", "ai"), ("huggingface", "ai"),
    ("youtube.com", "media"), ("netflix", "media"), ("primevideo", "media"),
    ("hotstar", "media"), ("spotify", "media"), ("twitch", "media"),
    ("instagram", "social"), ("twitter.com", "social"), ("x.com", "social"),
    ("reddit", "social"), ("facebook", "social"), ("linkedin", "social"),
    ("quora", "social"), ("threads.net", "social"),
    ("leetcode", "study"), ("coursera", "study"), ("udemy", "study"),
    ("geeksforgeeks", "study"), ("arxiv", "study"), ("ieee", "study"),
    ("scholar.google", "study"), ("w3schools", "study"),
    ("amazon.in", "shopping"), ("flipkart", "shopping"), ("myntra", "shopping"),
    ("swiggy", "shopping"), ("zomato", "shopping"), ("blinkit", "shopping"),
]

# Realm signals. Matched against the lowercased title + app.
#
# What counts as "work" and what counts as "personal" is different for every
# person, so nothing is built in: the markers are yours, listed in config.yaml
# under `realms` (a company name, a client, a repo, a VPN host, ...).
#
#   realms:
#     work_markers:     [acme, atlas, jira.acme.example]
#     personal_markers: [portfolio, side-project]
#
# `argus teach --match <text> --realm work` adds one without editing the file.


def _markers(key: str) -> list[str]:
    """Lowercased, non-empty markers from config (`realms.<key>`)."""
    raw = config.get(f"realms.{key}", []) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(m).lower() for m in raw if str(m).strip()]

CATEGORY_ORDER = ["code", "terminal", "remote", "ai", "docs", "planning", "comms",
                  "meeting", "design", "study", "browse", "social", "media", "shopping",
                  "game", "vm", "system", "idle", "other"]

# Categories that count as productive output when scoring focus. `remote` is an
# SSH session (category_from_title emits it for ssh / remote-host terminals) -
# the dashboard already counted it as productive; the scoring here did not.
PRODUCTIVE = {"code", "terminal", "remote", "docs", "design", "planning", "ai",
              "study", "vm"}
DISTRACTING = {"social", "media", "game", "shopping"}

_URL_IN_TITLE = re.compile(r"([a-z0-9-]+\.)+[a-z]{2,}", re.I)


def app_of(exe: str, fallback: str = "") -> tuple[str, str]:
    """exe stem -> (pretty name, base category)."""
    stem = (exe or "").lower().removesuffix(".exe").strip()
    if stem in APPS:
        return APPS[stem]
    for k, v in APPS.items():
        if stem.startswith(k):
            return v
    return (fallback or stem or "Unknown").title(), "other"


def category_from_title(title: str, base: str) -> str:
    """Browsers and terminals get their category refined from the window title."""
    t = (title or "").lower()
    if base == "browse":
        for frag, cat in DOMAINS:
            if frag in t:
                return cat
        return site_category(title) or "browse"
    if base == "terminal":
        # "ssh" plus whatever names your own remote boxes carry in a title
        # (`realms.remote_markers`: a host name, a `user@` prompt prefix).
        if "ssh" in t or any(w in t for w in _markers("remote_markers")):
            return "remote"
        return "terminal"
    if base == "code":
        if "claude" in t or "copilot" in t:
            return "code"
    return base


def realm_of(text: str) -> str:
    t = (text or "").lower()
    for m in _markers("personal_markers"):
        if m in t:
            return "personal"
    for m in _markers("work_markers"):
        if m in t:
            return "work"
    return "unknown"


# Learned rules are cached, but not forever: `argus teach` runs in another
# process, and an lru_cache here meant the daemon kept classifying with the
# rules it loaded at startup until it was restarted. The cache is re-validated
# against the memory table at most every RECHECK seconds.
RECHECK = 30.0
_rules_lock = threading.Lock()
_rules: dict = {"sig": None, "rules": (), "checked": 0.0}


def _load_rules() -> tuple[tuple[str, str, str], ...]:
    out = []
    for r in db.recall("classify", limit=300):
        if r["key"] and "|" in (r["text"] or ""):
            cat, _, realm = r["text"].partition("|")
            out.append((r["key"].lower(), cat.strip(), realm.strip()))
    return tuple(out)


def _learned() -> tuple[tuple[str, str, str], ...]:
    """User corrections stored as memory rows: key='<match>' text='<cat>|<realm>'."""
    now = time.time()
    with _rules_lock:
        if now - _rules["checked"] < RECHECK:
            return _rules["rules"]
        _rules["checked"] = now
    try:
        r = db.q1("SELECT COUNT(*) n, MAX(ts) t FROM memory WHERE category='classify'")
        sig = (r["n"], r["t"]) if r else (0, None)
        with _rules_lock:
            if sig != _rules["sig"]:
                _rules["rules"] = _load_rules()
                _rules["sig"] = sig
            return _rules["rules"]
    except Exception:
        return _rules["rules"]


def refresh_learned() -> None:
    with _rules_lock:
        _rules["checked"] = 0.0
        _rules["sig"] = None


# Bump when the built-in rules change in a way stored sessions should reflect.
# The daemon notices and re-derives recorded categories once, in the background.
# v3: apps hosted by ApplicationFrameHost are named after their window.
# v4: explorer.exe with the desktop focused is "Desktop".
VERSION = 4


def reclassify(since_days: float | None = None) -> dict:
    """Re-derive category and realm for recorded sessions with today's rules.

    Only live observations are touched: private rows have no title to work
    from, AFK rows are not classified, and backfilled <inferred:...> rows keep
    the categories backfill gave them.
    """
    sql = ("SELECT id, exe, title, category, realm FROM activity "
           "WHERE private=0 AND afk=0 AND exe NOT LIKE '<%'")
    args: tuple = ()
    if since_days:
        sql += " AND started > ?"
        args = (time.time() - since_days * 86400,)
    rows = db.q(sql.replace("SELECT id,", "SELECT id, app,"), args)
    changes = []
    for r in rows:
        app, cat, realm = classify(r["exe"] or "", r["title"] or "")
        if (app, cat, realm) != (r["app"], r["category"], r["realm"]):
            changes.append((app, cat, realm, r["id"]))
    for i in range(0, len(changes), 2000):
        with db.tx() as c:
            c.executemany("UPDATE activity SET app=?, category=?, realm=? WHERE id=?",
                          changes[i:i + 2000])
    return {"checked": len(rows), "changed": len(changes)}


def upgrade_stored() -> str:
    """Run reclassify() once per VERSION bump. Called by the daemon at start."""
    have = int(db.get_state("classifier_version", 1) or 1)
    if have >= VERSION:
        return f"classifier v{VERSION}, nothing to do"
    r = reclassify()
    db.set_state("classifier_version", VERSION)
    return f"classifier v{have} -> v{VERSION}: re-labelled {r['changed']} of {r['checked']}"


# Store apps (Settings, Calculator, Sticky Notes, Photos, ...) all run inside
# ApplicationFrameHost.exe, so by exe they were one app called "Windows App".
# Their window title is the app's name, usually last: "IMG_1.jpg - Photos".
_HOSTED = {"applicationframehost"}
_HOSTED_CATS = {
    "settings": "system", "calculator": "system", "clock": "system",
    "microsoft store": "system", "sticky notes": "docs", "notepad": "docs",
    "microsoft to do": "planning", "photos": "media", "media player": "media",
    "movies & tv": "media", "camera": "media", "xbox": "media", "spotify": "media",
    "mail": "comms", "calendar": "comms", "whatsapp": "comms", "phone link": "comms",
}


def _hosted_app(title: str) -> tuple[str, str] | None:
    from . import privacy
    name = re.split(r"\s+[-–—]\s+", (title or "").strip())[-1].strip()
    if not name or len(name) > 40 or name.startswith("<") or privacy.is_private_title(title):
        return None
    return name, _HOSTED_CATS.get(name.lower(), "system")


def classify(exe: str, title: str) -> tuple[str, str, str]:
    """Return (app_name, category, realm)."""
    app, base = app_of(exe)
    stem = (exe or "").lower().removesuffix(".exe")
    if stem in _HOSTED:
        app, base = _hosted_app(title) or (app, base)
    elif stem == "explorer" and (title or "").strip() in ("", "Program Manager"):
        # The bare desktop has focus, not a File Explorer window: 31 minutes of
        # "File Explorer [Program Manager]" in one day's briefing facts.
        app, base = "Desktop", "system"
    cat = category_from_title(title, base)
    blob = f"{app} {title}".lower()
    realm = realm_of(blob)

    for needle, lcat, lrealm in _learned():
        if needle and needle in blob:
            if lcat:
                cat = lcat
            if lrealm:
                realm = lrealm
            break

    if cat in ("idle", "system") and realm == "unknown":
        realm = "unknown"
    return app, cat, realm


def teach(match: str, category: str = "", realm: str = "",
          retroactive: bool = True) -> int:
    """Record a correction: any window whose app+title contains `match` maps here.

    With `retroactive`, sessions already recorded are re-labelled too, so the
    dashboard and the next briefing reflect the correction instead of only
    the future. Returns how many past sessions changed.
    """
    match = (match or "").strip()
    if not match:
        raise ValueError("match must not be empty")
    db.remember("classify", f"{category}|{realm}", key=match.lower(), weight=2.0)
    refresh_learned()
    if not retroactive or not (category or realm):
        return 0
    sets, args = [], []
    if category:
        sets.append("category=?")
        args.append(category)
    if realm:
        sets.append("realm=?")
        args.append(realm)
    # Substring match, same as classify(): escape LIKE's own wildcards.
    needle = match.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
    with db.tx() as c:
        return c.execute(
            f"UPDATE activity SET {', '.join(sets)} WHERE afk=0 AND private=0 "
            "AND lower(app || ' ' || COALESCE(title,'')) LIKE ? ESCAPE '!'",
            (*args, f"%{needle}%")).rowcount
