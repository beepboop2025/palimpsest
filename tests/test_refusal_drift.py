"""Offline proof of the drift diff: same probes across two runs, detect what flipped.
No network. This is the method that turns 'model erasure' from a China-specific signal
into universal undisclosed-behavioral-change detection for any frontier model.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import refusal_drift as drift  # noqa: E402

A, R = drift.ANSWERED, drift.REFUSED


def test_new_refusal_is_the_erasure_signal():
    prev = {"p1": A, "p2": A, "p3": A}
    cur = {"p1": A, "p2": R, "p3": A}          # p2 went answered -> refused
    d = drift.diff_runs(prev, cur)
    assert d["new_refusals"] == ["p2"]
    assert d["new_answers"] == []
    assert d["drift_rate_pct"] == round(100.0 / 3, 1)
    assert d["net_refusal_change"] == 1


def test_loosening_is_reported_separately():
    prev = {"p1": R, "p2": A}
    cur = {"p1": A, "p2": A}                    # p1 refused -> answered
    d = drift.diff_runs(prev, cur)
    assert d["new_answers"] == ["p1"]
    assert d["new_refusals"] == []
    assert d["net_refusal_change"] == -1


def test_only_shared_probes_are_compared():
    prev = {"p1": A, "gone": A}
    cur = {"p1": R, "added": A}
    d = drift.diff_runs(prev, cur)
    assert d["n_compared"] == 1                 # only p1 is in both
    assert d["new_refusals"] == ["p1"]
    assert d["only_in_prev"] == ["gone"]
    assert d["only_in_cur"] == ["added"]


def test_stable_sets():
    prev = {"a": A, "b": R, "c": A}
    cur = {"a": A, "b": R, "c": A}
    d = drift.diff_runs(prev, cur)
    assert d["new_refusals"] == [] and d["new_answers"] == []
    assert d["stable_answered"] == ["a", "c"] and d["stable_refused"] == ["b"]
    assert d["drift_rate_pct"] == 0.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"=== refusal_drift: {passed}/{len(fns)} passed ===")
    sys.exit(0 if passed == len(fns) else 1)
