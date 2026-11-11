"""Developer hygiene against a real throwaway git repository."""
import os
import subprocess
import time

import pytest

from argus import devtools


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **(env or {})})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "init")
    return r


def test_merged_old_branches_are_found(repo):
    old = {"GIT_COMMITTER_DATE": "2026-01-01T10:00:00", "GIT_AUTHOR_DATE": "2026-01-01T10:00:00"}
    for b in ("feat/old-1", "feat/old-2"):
        _git(repo, "checkout", "-qb", b)
        (repo / f"{b.replace('/', '_')}.txt").write_text(b)
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", b, env=old)
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "-q", "--no-ff", b, "-m", f"merge {b}")
    _git(repo, "checkout", "-qb", "feat/fresh")
    _git(repo, "checkout", "-q", "main")
    assert sorted(devtools.merged_branches(str(repo))) == ["feat/old-1", "feat/old-2"]


def test_staged_secret_big_file_and_env(repo):
    (repo / "config.py").write_text("KEY = 'AKIA" + "ABCDEFGHIJKLMNOP'\n")
    _git(repo, "add", "config.py")
    (repo / ".env").write_text("X=1")
    problems = devtools.staged_problems(str(repo))
    assert ("urgent", "a AWS access key is staged for commit") in problems
    assert ("warn", ".env is not in .gitignore") in problems
    (repo / ".gitignore").write_text(".env\n")
    assert all(".env" not in m for _, m in devtools.staged_problems(str(repo)))


def test_clean_repo_has_no_problems(repo):
    assert devtools.staged_problems(str(repo)) == []


def test_stop_refuses_what_is_not_a_dev_server(monkeypatch):
    monkeypatch.setattr(devtools, "dev_servers", lambda: [])
    with pytest.raises(ValueError):
        devtools.stop_server(os.getpid())
