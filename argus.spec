# PyInstaller spec: builds the ARGUS desktop app.
#
# Two executables share one onedir bundle (one Analysis, two EXE objects), so
# the runtime lives on disk once:
#
#   Argus.exe  - windowed. The double-click / Start Menu / login-task target.
#                No console flash. Starts daemon + tray + dashboard.
#   argus.cmd  - the CLI, run from the venv (pipes and redirection work there).

#
# Both run launcher.py, which dispatches on argv.
#
# Build:  .venv\Scripts\pyinstaller.exe argus.spec --noconfirm --clean

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("argus/web/static/index.html", "argus/web/static"),
    ("assets/argus.ico", "assets"),
]
binaries = []
hiddenimports = [
    # pywin32: imported dynamically, so the import graph cannot see them.
    "win32com", "win32com.client", "pythoncom", "pywintypes", "win32timezone",
    "win32gui", "win32process", "win32api", "win32con", "win32event", "winerror",
    # tray backend is chosen at runtime
    "pystray._win32",
    # watchdog picks its observer at runtime
    "watchdog.observers.read_directory_changes", "watchdog.observers.polling",
    # uvicorn's loop / protocol / lifespan plugins are all late-bound
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan", "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # pulled in only via strings
    "encodings.idna", "sqlite3", "email.utils",
]
hiddenimports += collect_submodules("argus")

for pkg in ("googleapiclient", "google_auth_oauthlib", "google.auth", "google.oauth2",
            "win11toast", "winsdk", "mss", "psutil", "yaml", "httpx", "httpcore",
            "fastapi", "starlette", "pydantic", "anyio", "certifi"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # optional dependency; the app degrades gracefully without it

# googleapiclient ships ~95MB of static discovery documents covering every
# Google API in existence. Keep only the services ARGUS builds clients for;
# anything else falls back to network discovery, which is no loss because those
# calls need the network anyway.
KEEP_DISCOVERY = ("gmail.", "calendar.", "tasks.", "drive.", "people.",
                  "oauth2.", "sheets.")


def _keep(entry):
    dest = str(entry[0]).replace("\\", "/")
    if "discovery_cache/documents/" not in dest:
        return True
    return dest.rsplit("/", 1)[-1].startswith(KEEP_DISCOVERY)


_before = len(datas)
datas = [d for d in datas if _keep(d)]
print(f"[argus.spec] data entries after discovery trim: {_before} -> {len(datas)}")

excludes = [
    # Unused here, and collectively hundreds of megabytes.
    "torch", "torchvision", "transformers", "tokenizers", "safetensors",
    "matplotlib", "scipy", "sklearn", "pandas", "gradio", "streamlit", "altair",
    "IPython", "jupyter", "notebook", "nltk", "cv2", "PyQt5", "PySide2",
    "test", "unittest",          # (tkinter is used: the quick-capture box) "pymongo", "openai", "pytest",
    "google.generativeai", "google.adk",
]

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

# PyInstaller's own googleapiclient hook re-adds the discovery documents behind
# our back, so the filter has to run again on the collected set, not just on the
# list we hand to Analysis.
_before = len(a.datas)
a.datas = [d for d in a.datas if _keep(d)]
print(f"[argus.spec] collected datas after trim: {_before} -> {len(a.datas)}")

pyz = PYZ(a.pure, a.zipped_data)

exe_app = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Argus",
    console=False,               # no console window on double-click
    icon="assets/argus.ico",
)

COLLECT(
    exe_app,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Argus",
)

# Only the windowed app is frozen. A second console EXE from the same Analysis
# is not emitted reliably by COLLECT, and giving the CLI its own bundle would
# duplicate the whole runtime for no real gain - `argus.cmd` already runs the
# CLI from the venv with working pipes and redirection, and Argus.exe attaches
# to the calling console for one-off commands.
