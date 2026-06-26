"""Tests for the deletion-velocity signal core (no DB).

Verifies that a term suddenly deleted in bulk *now* (vs a flat baseline) is
flagged as a spike and ranked first, while steady background deletions are not.

    python3 -m pytest censorwatch/tests/test_signal.py
    python3 censorwatch/tests/test_signal.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from censorwatch.signal import compute_velocity_signal

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
WINDOW_MIN = 60
BASELINE = 24


def _del(term, minutes_ago):
    return {"deleted_at": NOW - timedelta(minutes=minutes_ago), "terms": [term]}


def _signal(deletions):
    return compute_velocity_signal(deletions, NOW, window_min=WINDOW_MIN,
                                   baseline_windows=BASELINE, z_threshold=3.0)


def test_spike_detected_and_ranked_first():
    deletions = []
    # "六四" — silent for 24h, then 6 deletions in the current hour → spike.
    for m in (5, 10, 20, 30, 45, 55):
        deletions.append(_del("六四", m))
    # "茅台" — steady 1/hour background across the whole window → not a spike.
    for h in range(BASELINE + 1):
        deletions.append(_del("茅台", h * 60 + 1))

    sig = _signal(deletions)
    assert sig["top_term"] == "六四", sig["ranked"][:2]
    top = sig["ranked"][0]
    assert top["count"] == 6 and top["spike"] is True and top["z"] >= 3.0

    moutai = next(r for r in sig["ranked"] if r["term"] == "茅台")
    assert moutai["spike"] is False, "steady background must not flag as spike"
    assert sig["n_spikes"] == 1


def test_single_deletion_is_not_a_spike():
    # One lone deletion of a brand-new term: high z, but below the noise floor.
    sig = _signal([_del("白纸", 3)])
    only = sig["ranked"][0]
    assert only["count"] == 1 and only["spike"] is False  # _MIN_SPIKE_COUNT guard


def test_only_current_window_terms_ranked():
    # A term deleted only in the baseline (not now) shouldn't appear.
    deletions = [_del("旧闻", 60 * 5)]   # 5 hours ago → baseline only
    sig = _signal(deletions)
    assert sig["n_deletions"] == 0 and sig["ranked"] == []


def test_velocity_per_hour():
    sig = _signal([_del("x", 5), _del("x", 10), _del("x", 15)])
    assert sig["ranked"][0]["velocity_per_hour"] == 3.0  # 3 in a 60-min window


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  PASS {name}")
    print("\nsignal checks passed")


if __name__ == "__main__":
    _run_all()
