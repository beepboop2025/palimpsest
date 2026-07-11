"""Offline proof of the registry's two guarantees: tamper detection AND the
pre-registration rule (no result can predate the freezing of its questions).
No network. This is the property the LTFF / AI-audit pitch rests on.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import eval_registry as reg  # noqa: E402


def _registry():
    d = tempfile.mkdtemp()
    return os.path.join(d, "reg.jsonl")


PROBES = ["politics/六四事件", "rights/新疆", "politics/台湾"]


def test_preregistered_run_verifies():
    p = _registry()
    pr = reg.preregister(p, PROBES, suite="s1")
    reg.submit_run(p, probe_set_hash=pr["probe_set_hash"], model="deepseek/deepseek-chat",
                   responses={"politics/六四事件": "refused"}, metrics={"suppression_rate_pct": 66.7})
    ok, problems = reg.verify(reg.read_ledger(p))
    assert ok and not problems, problems


def test_probe_set_hash_is_order_independent():
    assert reg.probe_set_hash(["a", "b", "c"]) == reg.probe_set_hash(["c", "a", "b"])
    assert reg.probe_set_hash(["a", "b"]) != reg.probe_set_hash(["a", "b", "c"])


def test_run_without_preregistration_is_rejected():
    # answers before the questions were frozen -> must fail verification
    p = _registry()
    reg.submit_run(p, probe_set_hash="deadbeef" * 8, model="m", responses={"q": "a"})
    ok, problems = reg.verify(reg.read_ledger(p))
    assert not ok
    assert any("never pre-registered" in x for x in problems)


def test_metric_tamper_is_caught():
    p = _registry()
    pr = reg.preregister(p, PROBES)
    reg.submit_run(p, probe_set_hash=pr["probe_set_hash"], model="m",
                   responses={"q": "a"}, metrics={"suppression_rate_pct": 66.7})
    entries = reg.read_ledger(p)
    entries[1]["metrics"]["suppression_rate_pct"] = 0.0  # forge the headline number down
    ok, problems = reg.verify(entries)
    assert not ok
    assert any("does not recompute" in x for x in problems)


def test_responses_tamper_is_caught():
    # change what the model "said" after the fact -> responses_hash no longer matches the seal
    p = _registry()
    pr = reg.preregister(p, PROBES)
    reg.submit_run(p, probe_set_hash=pr["probe_set_hash"], model="m",
                   responses={"q": "refused"}, metrics={})
    entries = reg.read_ledger(p)
    entries[1]["responses_hash"] = "0" * 64
    ok, problems = reg.verify(entries)
    assert not ok


def test_reorder_answers_before_questions_is_caught():
    # swap so the run precedes its pre-registration -> rule violation
    p = _registry()
    pr = reg.preregister(p, PROBES)
    reg.submit_run(p, probe_set_hash=pr["probe_set_hash"], model="m", responses={"q": "a"})
    entries = reg.read_ledger(p)
    entries[0], entries[1] = entries[1], entries[0]
    ok, problems = reg.verify(entries)
    assert not ok


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"=== eval_registry: {passed}/{len(fns)} passed ===")
    sys.exit(0 if passed == len(fns) else 1)
