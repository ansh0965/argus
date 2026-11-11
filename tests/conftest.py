"""Test harness.

Every test runs against a throwaway ARGUS_HOME. That has to be set before the
first `import argus...`, because paths.py resolves ROOT at import time - which is
exactly what keeps the suite from ever opening the real data/argus.db.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_HOME = Path(tempfile.mkdtemp(prefix="argus-test-"))
os.environ["ARGUS_HOME"] = str(_HOME)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from argus import db, paths  # noqa: E402

assert paths.ROOT == _HOME.resolve(), "refusing to run tests against a real ARGUS home"

# A real install names its own work and personal markers in config.yaml
# (`realms`); out of the box there are none. The suite needs some, so it gets a
# fictional employer ("acme", project "atlas") and a personal "portfolio".
from argus import config as _config  # noqa: E402

_config.DEFAULTS["realms"].update(
    work_markers=["acme", "atlas"],
    personal_markers=["portfolio"],
    remote_markers=["build-box"],
)

_TABLES = ("events", "activity", "metrics", "alerts", "digests", "memory", "state",
           "gcal", "gmail", "tasks", "runs", "threads", "thread_touches", "feedback")


@pytest.fixture(autouse=True)
def fresh_db():
    """Every test starts from empty tables and default config."""
    db.init()
    with db.tx() as c:
        for t in _TABLES:
            c.execute(f"DELETE FROM {t}")
    from argus import config
    if paths.CONFIG_PATH.exists():
        paths.CONFIG_PATH.unlink()
    config.load(force=True)
    yield


@pytest.fixture
def write_config():
    """Write a config.yaml into the test home and force a reload."""
    import yaml

    from argus import config

    def _write(data: dict) -> dict:
        paths.CONFIG_PATH.write_text(yaml.safe_dump(data), encoding="utf-8")
        return config.load(force=True)

    return _write


def pytest_sessionfinish(session, exitstatus):
    try:
        db.conn().close()
    except Exception:
        pass
    shutil.rmtree(_HOME, ignore_errors=True)
