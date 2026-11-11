"""The bundle swap must never leave a half-installed app behind."""
from argus import install


def _bundle(root, tag):
    (root / "_internal").mkdir(parents=True)
    (root / "_internal" / "lib.dll").write_text(tag)
    (root / "Argus.exe").write_text(tag)


def test_swap_installs_the_new_build(tmp_path):
    live, new = tmp_path / "live", tmp_path / "new"
    _bundle(live, "old")
    _bundle(new, "new")
    assert install.swap_bundle(new, live / "_internal", live / "Argus.exe")
    assert (live / "Argus.exe").read_text() == "new"
    assert (live / "_internal" / "lib.dll").read_text() == "new"


def test_failed_copy_restores_the_previous_build(tmp_path):
    live, new = tmp_path / "live", tmp_path / "new"
    _bundle(live, "old")
    (new / "_internal").mkdir(parents=True)       # new build is missing its exe
    assert not install.swap_bundle(new, live / "_internal", live / "Argus.exe")
    assert (live / "Argus.exe").read_text() == "old"
    assert (live / "_internal" / "lib.dll").read_text() == "old"


def test_leftovers_from_a_previous_swap_are_cleared(tmp_path):
    live, new = tmp_path / "live", tmp_path / "new"
    _bundle(live, "old")
    _bundle(new, "new")
    (live / "_internal.old").mkdir()
    (live / "Argus.exe.old").write_text("ancient")
    assert install.swap_bundle(new, live / "_internal", live / "Argus.exe")
    assert (live / "Argus.exe.old").read_text() == "old"


import pytest  # noqa: E402

from argus.daemon import is_daemon_cmdline  # noqa: E402


@pytest.mark.parametrize("name,cmdline,daemon", [
    ("pythonw.exe", r'"C:\Users\A P\argus\.venv\Scripts\pythonw.exe" -m argus', True),
    ("pythonw.exe", r'"C:\x\pythonw.exe" -m argus --quiet', True),
    ("python.exe", r'"C:\x\python.exe" -m argus.cli stop', False),
    ("python.exe", r'"C:\x\python.exe" -m argus.cli build --no-build', False),
    ("python.exe", r"python -m argusx", False),
    ("Argus.exe", r'"C:\Users\A P\argus\Argus.exe"', True),
    ("Argus.exe", r'"C:\Users\A P\argus\Argus.exe" --quiet', True),
    ("Argus.exe", r'"C:\Users\A P\argus\Argus.exe" status', False),
])
def test_only_the_daemon_matches_never_a_cli(name, cmdline, daemon):
    # `-m argus.cli` used to match, so `argus stop` killed its own venv launcher
    assert is_daemon_cmdline(name, cmdline) is daemon


def test_start_prefers_a_current_app_over_the_venv(monkeypatch, tmp_path):
    import os
    import time as _t

    from argus import paths
    exe = tmp_path / "Argus.exe"
    exe.write_text("x")
    src = tmp_path / "argus"
    src.mkdir()
    (src / "m.py").write_text("x")
    monkeypatch.setattr(paths, "APP_EXE", exe)
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "FROZEN", False)
    later = _t.time() + 5
    os.utime(exe, (later, later))                     # built after the source
    assert paths.daemon_command() == [str(exe)]
    os.utime(src / "m.py", (later + 600, later + 600))  # source edited since
    assert paths.daemon_command()[-2:] == ["-m", "argus"]
