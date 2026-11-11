"""Private projects are never recorded: files, titles, commits, cards."""
from argus import db, privacy
from argus.brain import threads
from argus.sensors import files


def test_matching_is_by_whole_name(write_config):
    write_config({"privacy": {"private_projects": ["_secure", "clientx"]}})
    assert privacy.private_project_in(r"C:\Users\me\Documents\_SECURE (keys & passwords)\aws.txt")
    assert privacy.private_project_in("clientx - Visual Studio Code")
    assert not privacy.private_project_in(r"C:\work\clientxyz\app.py")
    assert not privacy.private_project_in("atlas - Visual Studio Code")


def test_titles_become_private(write_config):
    write_config({"privacy": {"private_projects": ["clientx"]}})
    assert privacy.clean_title("code.exe", "VS Code", "main.py - clientx - Visual Studio Code") == ("<private>", True)


def test_no_file_events_and_no_project(tmp_path, write_config):
    write_config({"watch_dir_ignores": [], "privacy": {"private_projects": ["clientx"]}})
    d = tmp_path / "clientx"
    d.mkdir()
    (d / "contract.md").write_text("x")
    files._Handler()._emit("modified", str(d / "contract.md"))
    assert db.q1("SELECT COUNT(*) n FROM events WHERE source='file'")["n"] == 0
    assert threads.key_from_path(str(d)) is None
