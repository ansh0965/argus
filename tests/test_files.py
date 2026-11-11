"""File sensor: what counts as a person's file activity and what is a tool's."""
import time

import pytest

from argus import db
from argus.sensors import files

HOME = "C:\\Users\\me\\Desktop\\portfolio\\"


@pytest.mark.parametrize("path", [
    HOME + "out\\work\\pipsfusion\\__next._tree.txt",          # next export
    HOME + ".history\\src\\App_20260923.tsx",                  # VS Code local history
    HOME + ".turbo\\cache\\x.json",
    HOME + "coverage\\lcov.info",
    HOME + "src\\components\\SiteFooter.tsx.tmp.5536.7d6dff2f2ca4",   # atomic save
    HOME + "notes.md~",
    HOME + "node_modules\\react\\index.js",                    # from the built-in list
    HOME + ".env.local",                                       # dotfile, as before
    HOME + "vite.config.ts.timestamp-1790193987049-c9ee09d5a87af.mjs",
    HOME + "tsconfig.tsbuildinfo",
    HOME + "src\\sedJGqNNT",                                   # sed -i scratch file
])
def test_generated_output_is_not_a_person_at_work(path, write_config):
    write_config({"watch_dir_ignores": []})     # built-ins apply without the config list
    assert files.generated(path)


@pytest.mark.parametrize("path", [
    HOME + "src\\components\\SiteFooter.tsx",
    HOME + "layout\\page.tsx",                  # "out" only as a whole directory name
    HOME + "outline.md",
    HOME + "sedan.py",                                         # not a sed temp name
    HOME + "vite.config.ts",
    "C:/Users/me/Desktop/atlas/backend/app.py",
])
def test_real_files_are_kept(path, write_config):
    write_config({"watch_dir_ignores": []})
    assert not files.generated(path)


def test_configured_ignores_still_apply(write_config):
    write_config({"watch_dir_ignores": ["scratch"]})
    assert files.generated(HOME + "scratch\\a.py")


def test_a_burst_in_one_directory_is_recorded_only_up_to_the_limit(tmp_path, write_config):
    write_config({"watch_dir_ignores": []})
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    h = files._Handler()
    for i in range(200):                       # a build writing 200 files at once
        f = chunks / f"c{i}.js"
        f.write_text("x")
        h._emit("created", str(f))
    n = db.q1("SELECT COUNT(*) n FROM events WHERE source='file'")["n"]
    assert n == files.BURST_FILES
    # a person saving a file in another directory meanwhile is still seen
    other = tmp_path / "src"
    other.mkdir()
    (other / "app.py").write_text("x")
    h._emit("modified", str(other / "app.py"))
    assert db.q1("SELECT COUNT(*) n FROM events WHERE source='file'")["n"] == n + 1


def test_a_directory_quiets_down_after_the_burst(tmp_path, write_config, monkeypatch):
    write_config({"watch_dir_ignores": []})
    h = files._Handler()
    clock = [1000.0]
    monkeypatch.setattr(files.time, "time", lambda: clock[0])
    for i in range(files.BURST_FILES + 5):
        h._emit("created", str(tmp_path / f"c{i}.js"))
    before = db.q1("SELECT COUNT(*) n FROM events WHERE source='file'")["n"]
    clock[0] += files.BURST_MUTE + 1
    h._emit("modified", str(tmp_path / "later.py"))
    assert db.q1("SELECT COUNT(*) n FROM events WHERE source='file'")["n"] == before + 1


def test_recent_files_skips_build_output_recorded_before_the_filter():
    now = time.time()
    db.log_events([
        ("file", "created", "__next._tree.txt", HOME + "out\\work\\__next._tree.txt", 1,
         {"dir": HOME + "out\\work", "ext": ".txt"}, now - 5),
        ("file", "modified", "page.tsx", HOME + "src\\page.tsx", 1,
         {"dir": HOME + "src", "ext": ".tsx"}, now - 1),
    ])
    assert [f["subject"] for f in files.recent_files()] == ["page.tsx"]


def test_threads_count_acts_not_files(tmp_path, monkeypatch, write_config):
    """A 400-file build counted as 400 touches and put a seven-minute project
    at the top of Threads."""
    from argus.brain import threads
    write_config({"watch_dir_ignores": []})
    root = tmp_path / "Desktop"
    (root / "portfolio").mkdir(parents=True)
    monkeypatch.setattr(threads, "collect_claude", lambda since: [])
    monkeypatch.setattr(threads, "_roots", lambda: [str(root)])
    now = (time.time() // 60) * 60 - 600         # all within one clock minute
    proj = root / "portfolio"
    rows = [("file", "created", f"c{i}.txt", str(proj / "out" / f"c{i}.txt"), 1,
             {"dir": str(proj / "out"), "ext": ".txt"}, now + i * 0.01) for i in range(400)]
    rows += [("file", "modified", f"s{i}.tsx", str(proj / "src" / f"s{i}.tsx"), 1,
              {"dir": str(proj / "src"), "ext": ".tsx"}, now + i * 0.1) for i in range(100)]
    db.log_events(rows)
    evs = [e for e in threads.collect_minting(now - 60) if e.src == "file"]
    assert len(evs) == threads.FILE_TOUCHES_PER_MINUTE
    assert all(e.label.startswith("s") for e in evs)


def test_maintenance_removes_build_output_recorded_before_the_filter(write_config):
    from argus.automations import builtin
    write_config({"watch_dir_ignores": []})
    now = time.time()
    db.log_events([
        ("file", "created", "__next._tree.txt", HOME + "out\\work\\__next._tree.txt", 1,
         {"dir": HOME + "out\\work", "ext": ".txt"}, now - 50),
        ("file", "created", "a.tsx.tmp.1.abc", HOME + "src\\a.tsx.tmp.1.abc", 1,
         {"dir": HOME + "src", "ext": ".abc"}, now - 40),
        ("file", "modified", "page.tsx", HOME + "src\\page.tsx", 1,
         {"dir": HOME + "src", "ext": ".tsx"}, now - 30),
        ("git", "commit", "portfolio", "out: rebuild", 1, {}, now - 20),
    ])
    out = builtin.maintenance()
    assert "generated_files=2" in out
    left = [r["subject"] for r in db.q("SELECT subject FROM events ORDER BY ts")]
    assert left == ["page.tsx", "portfolio"], "real edits and other sources untouched"
