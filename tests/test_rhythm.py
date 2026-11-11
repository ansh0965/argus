"""Break nudges follow your own rhythm."""
import time

from argus import db
from argus.automations import builtin


def _work(start, minutes):
    db.add_activity("VS Code", "code.exe", "x", "code", "work", start, start + minutes * 60, False, False)


def test_stretches_split_at_breaks_and_gaps():
    t = time.time() - 10 * 3600
    _work(t, 30)
    _work(t + 30 * 60, 20)                                        # continues the run: 50 min
    db.add_activity("Idle", "", "", "idle", "", t + 50 * 60, t + 60 * 60, True, False)   # a break
    _work(t + 60 * 60, 40)                                        # 40 min
    _work(t + 110 * 60, 25)                                       # 10 min gap: new run, 25 min
    assert [round(s / 60) for s in builtin.stretches(t - 1, t + 4 * 3600)] == [50, 40, 25]


def test_typical_stretch_is_the_median_and_cached():
    base = time.time() - 6 * 86400
    for i, m in enumerate((40, 50, 60, 70, 80, 15)):              # 15 is too short to count
        _work(base + i * 86400, m)
    assert round(builtin.typical_stretch() / 60) == 60
    db.add_activity("x", "x.exe", "", "code", "", time.time() - 300, time.time() - 1, False, False)
    assert round(builtin.typical_stretch() / 60) == 60, "cached for a day"


def test_not_enough_history_means_the_configured_default():
    assert builtin.typical_stretch() is None
