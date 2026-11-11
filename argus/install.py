"""Autostart installation.

Uses a Scheduled Task with an AtLogOn trigger rather than a registry Run key,
because a Run key cannot restart ARGUS if it dies and cannot run with the
"start when available" catch-up behaviour a laptop needs.

Registering a task for the current user does not need elevation.
"""
from __future__ import annotations

import os

from .paths import APP_EXE, FROZEN, ICON_ICO, ROOT, VENV_PYW
from .util import log, run

lg = log("install")

TASK_NAME = "ARGUS Assistant"


def _ps(script: str, timeout: int = 90) -> tuple[int, str, str]:
    return run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                "Bypass", "-Command", script], timeout=timeout)


def is_installed() -> bool:
    rc, out, _ = _ps(f"if (Get-ScheduledTask -TaskName '{TASK_NAME}' "
                     "-ErrorAction SilentlyContinue) {{ 'yes' }} else {{ 'no' }}")
    return rc == 0 and "yes" in out


def install_autostart(remove: bool = False) -> int:
    if remove:
        rc, _, err = _ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' "
                         "-Confirm:$false -ErrorAction SilentlyContinue")
        print("  Autostart removed." if rc == 0 else f"  Could not remove: {err[:200]}")
        return 0 if rc == 0 else 1

    # Prefer the frozen app when it has been built - one less moving part at login.
    if APP_EXE.exists():
        exe, args = str(APP_EXE), ""
    else:
        py = VENV_PYW if VENV_PYW.exists() else ROOT / ".venv" / "Scripts" / "python.exe"
        if not py.exists():
            print(f"  Python not found at {py}")
            return 1
        exe, args = str(py), "-m argus"

    user = os.environ.get("USERNAME", "")
    arg_clause = f"-Argument '{args}' " if args else ""
    script = f"""
$ErrorActionPreference = 'Stop'
$act = New-ScheduledTaskAction -Execute '{exe}' {arg_clause}-WorkingDirectory '{ROOT}'
$trg = New-ScheduledTaskTrigger -AtLogOn -User '{user}'
$trg.Delay = 'PT45S'
$set = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew
$prn = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Interactive -RunLevel Limited
Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $act -Trigger $trg `
    -Settings $set -Principal $prn -Description 'Personal activity assistant' | Out-Null
'registered'
"""
    rc, out, err = _ps(script, timeout=120)
    if rc != 0 or "registered" not in out:
        print(f"  Registration failed: {(err or out)[:400]}")
        print("  You can still start it manually with: argus start")
        return 1

    print("  ARGUS will now start automatically when you log in.")
    print(f"  Task name: {TASK_NAME}   (visible in Task Scheduler)")
    print("  Remove it any time with: argus uninstall")
    return 0


def write_launchers() -> None:
    """Create argus.cmd and argus.ps1 so the command works from any shell.

    Two details matter here and both have bitten us:

    - newline="" is required. Writing "\\r\\n" through Python's text mode gets
      translated again into "\\r\\r\\n", and cmd.exe then folds the stray CR into
      the last token - which corrupts a quoted path containing spaces and fails
      with a baffling "'C:\\Users\\Jane' is not recognized".
    - %~dp0 keeps the scripts self-locating, so moving the folder does not break
      them and there is no absolute path to go stale.
    """
    cmd = (
        "@echo off\r\n"
        "setlocal\r\n"
        'set "PYTHONPATH=%~dp0."\r\n'
        '"%~dp0.venv\\Scripts\\python.exe" -m argus.cli %*\r\n'
    )
    with open(ROOT / "argus.cmd", "w", encoding="utf-8", newline="") as f:
        f.write(cmd)

    ps = (
        '$env:PYTHONPATH = $PSScriptRoot\n'
        '& "$PSScriptRoot\\.venv\\Scripts\\python.exe" -m argus.cli @args\n'
    )
    with open(ROOT / "argus.ps1", "w", encoding="utf-8", newline="") as f:
        f.write(ps)


def add_to_path() -> str:
    """Put the argus folder on the user PATH so `argus` works everywhere."""
    cur = os.environ.get("PATH", "")
    if str(ROOT).lower() in cur.lower():
        return "already on PATH"
    script = (
        "$p = [Environment]::GetEnvironmentVariable('Path','User');"
        f"if ($p -notlike '*{ROOT}*') {{"
        f"  [Environment]::SetEnvironmentVariable('Path', $p + ';{ROOT}', 'User');"
        "  'added' } else { 'already' }"
    )
    rc, out, err = _ps(script)
    return out.strip() if rc == 0 else f"failed: {err[:120]}"


# --------------------------------------------------------------------------- shortcuts

def make_shortcuts(desktop: bool = True, start_menu: bool = True,
                   startup_folder: bool = False) -> str:
    """Create real Windows shortcuts so ARGUS behaves like an installed app.

    Start Menu gets it into search (press Start, type "argus"). The Desktop
    shortcut is the double-clickable one. The Startup-folder shortcut is an
    optional belt-and-braces alternative to the scheduled task - normally the
    task is better because it can restart a crashed daemon.
    """
    target = str(APP_EXE) if APP_EXE.exists() else str(VENV_PYW)
    arguments = "" if APP_EXE.exists() else "-m argus"
    icon = str(ICON_ICO) if ICON_ICO.exists() else target

    made: list[str] = []
    places: list[tuple[str, str]] = []
    if desktop:
        places.append(("Desktop", "$([Environment]::GetFolderPath('Desktop'))"))
    if start_menu:
        places.append(("StartMenu",
                       r"$env:APPDATA + '\Microsoft\Windows\Start Menu\Programs'"))
    if startup_folder:
        places.append(("Startup",
                       r"$env:APPDATA + '\Microsoft\Windows\Start Menu\Programs\Startup'"))

    for label, folder_expr in places:
        script = f"""
$ErrorActionPreference = 'Stop'
$dir = {folder_expr}
$path = Join-Path $dir 'ARGUS.lnk'
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($path)
$sc.TargetPath = '{target}'
$sc.Arguments = '{arguments}'
$sc.WorkingDirectory = '{ROOT}'
$sc.IconLocation = '{icon},0'
$sc.Description = 'ARGUS - personal activity assistant'
$sc.WindowStyle = 7
$sc.Save()
$path
"""
        rc, out, err = _ps(script, timeout=60)
        made.append(f"{label}: {'ok' if rc == 0 else 'failed ' + err[:80]}")
    return "; ".join(made)


def remove_shortcuts() -> str:
    script = r"""
$paths = @(
  (Join-Path ([Environment]::GetFolderPath('Desktop')) 'ARGUS.lnk'),
  (Join-Path ($env:APPDATA + '\Microsoft\Windows\Start Menu\Programs') 'ARGUS.lnk'),
  (Join-Path ($env:APPDATA + '\Microsoft\Windows\Start Menu\Programs\Startup') 'ARGUS.lnk')
)
$n = 0
foreach ($p in $paths) { if (Test-Path $p) { Remove-Item $p -Force; $n++ } }
"removed $n"
"""
    rc, out, _ = _ps(script, timeout=60)
    return out.strip() if rc == 0 else "failed"


# --------------------------------------------------------------------------- freezing

def install_app_bundle(clean: bool = True, restart: bool = True, build: bool = True) -> int:
    """Build Argus.exe with PyInstaller and install it beside the data folder.

    Builds while the current app keeps running, then stops it just long enough
    to swap the bundle in (with rollback) and starts it again - seconds of
    downtime instead of the whole build. `build=False` installs whatever the
    last build left in build_dist/.

    The frozen app is deliberately placed in ROOT rather than a subfolder: a
    frozen build resolves ROOT from the exe's own directory, so living next to
    data/ and config.yaml is what keeps the existing database and settings in
    play instead of starting a fresh, empty one.
    """
    dist = ROOT / "build_dist"
    built = dist / "Argus" / "Argus.exe"
    if build:
        spec = ROOT / "argus.spec"
        if not spec.exists():
            print(f"  missing {spec.name}")
            return 1
        pyi = ROOT / ".venv" / "Scripts" / "pyinstaller.exe"
        if not pyi.exists():
            print("  PyInstaller not installed. Run:")
            print(f"    \"{ROOT / '.venv' / 'Scripts' / 'python.exe'}\" -m pip install pyinstaller")
            return 1
        work = ROOT / "build_work"
        cmd = [str(pyi), str(spec), "--noconfirm", "--distpath", str(dist),
               "--workpath", str(work), "--log-level", "ERROR"]
        if clean:
            cmd.append("--clean")
        print("  building (this takes a couple of minutes)...")
        rc, out, err = run(cmd, timeout=1800, cwd=ROOT)
        if rc != 0 or not built.exists():
            print(f"  build failed: {(err or out)[-600:]}")
            return 1
    elif not built.exists():
        print(f"  nothing to install: {built} does not exist - run `argus build`")
        return 1

    # Windows lets you rename a file that is in use but not a directory whose
    # DLLs are loaded, so the running app has to stop for the swap itself. The
    # build above already happened while it kept running; downtime is just the
    # copy and the restart.
    from .daemon import daemon_alive, start_background, stop_running
    was_running = daemon_alive()
    if was_running:
        if not restart:
            print(f"  built into {dist / 'Argus'}; ARGUS is running, so it was not "
                  "swapped in. Run `argus build --no-build` to install it.")
            return 0
        print("  stopping ARGUS to swap in the new build...")
        stop_running()

    target_internal = ROOT / "_internal"
    if not swap_bundle(dist / "Argus", target_internal, APP_EXE):
        if was_running:
            start_background()               # bring the previous build back up
        return 1
    size = sum(f.stat().st_size for f in target_internal.rglob("*") if f.is_file())
    print(f"  installed {APP_EXE.name} ({APP_EXE.stat().st_size / 1e6:.1f} MB exe, "
          f"{size / 1e6:.0f} MB runtime)")

    if was_running:
        if not start_background():
            print("  the new build did not come up - check `argus logs`. The previous "
                  f"build is kept as {APP_EXE.name}.old / {target_internal.name}.old")
            return 1
        print("  ARGUS is running the new build.")
    return 0


def swap_bundle(src_dir, target_internal, target_exe) -> bool:
    """Install a freshly built bundle, with rollback.

    The old code deleted _internal first and copied the new one in. With the
    app running, that deletion fails halfway on locked DLLs and leaves an
    install that neither the old nor the new exe can start from. Windows does
    allow *renaming* files that are in use, so the old bundle is moved aside
    first (the running app keeps working from it), the new one copied in, and
    on any failure the old one is put back.
    """
    import shutil
    old_internal = target_internal.with_name(target_internal.name + ".old")
    old_exe = target_exe.with_name(target_exe.name + ".old")
    for leftover in (old_internal, old_exe):         # from a previous swap
        try:
            if leftover.is_dir():
                shutil.rmtree(leftover)
            elif leftover.exists():
                leftover.unlink()
        except OSError:
            pass
    moved_internal = moved_exe = False
    try:
        if target_internal.exists():
            target_internal.rename(old_internal)
            moved_internal = True
        if target_exe.exists():
            target_exe.rename(old_exe)
            moved_exe = True
        shutil.copytree(src_dir / "_internal", target_internal)
        shutil.copy2(src_dir / target_exe.name, target_exe)
    except OSError as e:
        print(f"  could not install the new build ({e}); restoring the previous one")
        try:
            if target_internal.exists() and moved_internal:
                shutil.rmtree(target_internal, ignore_errors=True)
            if moved_internal:
                old_internal.rename(target_internal)
            if moved_exe:
                if target_exe.exists():
                    target_exe.unlink()
                old_exe.rename(target_exe)
        except OSError as e2:
            print(f"  rollback incomplete ({e2}): the previous build is at "
                  f"{old_internal.name} / {old_exe.name}")
        return False
    return True
