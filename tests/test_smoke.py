"""The harness itself: isolation first, because everything else depends on it."""
from argus import db, paths


def test_home_is_isolated():
    assert "argus-test-" in str(paths.ROOT)
    assert paths.DB_PATH.parent == paths.ROOT / "data"


def test_state_roundtrip():
    db.set_state("k", {"a": 1})
    assert db.get_state("k") == {"a": 1}
    assert db.get_state("missing", 7) == 7
