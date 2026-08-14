"""Offline proof of the registry's two guarantees: tamper detection AND the
pre-registration rule (no result can predate the freezing of its questions).
No network. This is the property the LTFF / AI-audit pitch rests on.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import eval_registry as reg  # noqa: E402
from scripts import verify_eval_registry as verify_cli  # noqa: E402


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


def test_unknown_kind_is_rejected_even_when_its_hash_recomputes():
    # The subtler attack: an insider with write access appends a well-formed, correctly
    # hashed line of a kind verify() does not know. The chain itself is intact — seq,
    # prev_hash and entry_hash all check out — so hash verification alone would wave it
    # through. Only the kind allowlist stops it, and it must, because an unrecognised kind
    # is by definition an attestation whose rules nobody enforced.
    p = _registry()
    reg.preregister(p, PROBES)
    reg._append(p, {"ts": "2026-01-01T00:00:00+00:00", "kind": "result",  # not RUN
                    "probe_set_hash": "0" * 64, "model": "m", "metrics": {"score": 99.9}})
    entries = reg.read_ledger(p)
    # the forged line's own hash is genuinely correct — the attacker did their arithmetic
    core = {k: entries[1][k] for k in entries[1] if k != "entry_hash"}
    assert reg._entry_hash(core) == entries[1]["entry_hash"]
    ok, problems = reg.verify(entries)
    assert not ok
    assert any("unknown kind" in x for x in problems), problems


def test_attestation_missing_its_kind_is_rejected():
    # The other smuggling route: drop the field verify() dispatches on, hoping an absent
    # kind means "no rule applies". It must be reported as malformed, not skipped.
    p = _registry()
    pr = reg.preregister(p, PROBES)
    reg.submit_run(p, probe_set_hash=pr["probe_set_hash"], model="m", responses={"q": "a"})
    entries = reg.read_ledger(p)
    del entries[1]["kind"]
    ok, problems = reg.verify(entries)
    assert not ok
    assert any("malformed attestation" in x for x in problems), problems


def test_reorder_answers_before_questions_is_caught():
    # swap so the run precedes its pre-registration -> rule violation
    p = _registry()
    pr = reg.preregister(p, PROBES)
    reg.submit_run(p, probe_set_hash=pr["probe_set_hash"], model="m", responses={"q": "a"})
    entries = reg.read_ledger(p)
    entries[0], entries[1] = entries[1], entries[0]
    ok, problems = reg.verify(entries)
    assert not ok


def test_duplicate_json_keys_are_rejected_before_their_valid_hash_can_hide_them(
    tmp_path,
):
    path = tmp_path / "registry.jsonl"
    core = {
        "seq": 0,
        "prev_hash": reg.GENESIS_PREV,
        "ts": "2026-08-14T00:00:00+00:00",
        "kind": reg.PREREGISTRATION,
        "probe_set_hash": "a" * 64,
        "n_probes": 1,
        "suite": "legacy-suite",
        "note": "accepted-by-last-key-parser",
    }
    entry = {**core, "entry_hash": reg._entry_hash(core)}
    raw = json.dumps(entry, separators=(",", ":"))
    raw = raw.replace(
        '"note":"accepted-by-last-key-parser"',
        '"note":"hidden-first-value","note":"accepted-by-last-key-parser"',
    )
    path.write_text(raw + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key: note"):
        reg.read_ledger(str(path))


def test_reserved_myquant_schema_cannot_downgrade_to_legacy_rules(tmp_path):
    path = str(tmp_path / "registry.jsonl")
    entry = reg._append(
        path,
        {
            "ts": "2026-08-14T00:00:00+00:00",
            "kind": reg.PREREGISTRATION,
            "probe_set_hash": "a" * 64,
            "n_probes": 1,
            "suite": reg.MYQUANT_DIGEST_SUITE,
            "note": "",
            "receipt_schema": reg.MYQUANT_PREREGISTRATION_RECEIPT_SCHEMA,
        },
    )
    ok, problems = reg.verify([entry])
    assert not ok
    assert any("reserved MyQuant fields require" in problem for problem in problems)


def test_concurrent_registry_writers_get_unique_contiguous_chain_slots(tmp_path):
    path = str(tmp_path / "registry.jsonl")

    def append(index):
        return reg.preregister(path, [f"probe-{index}"], suite="concurrency-test")

    with ThreadPoolExecutor(max_workers=12) as pool:
        written = list(pool.map(append, range(48)))

    entries = reg.read_ledger(path)
    assert len(entries) == 48
    assert sorted(entry["seq"] for entry in written) == list(range(48))
    assert reg.verify(entries) == (True, [])


def test_noop_summary_refresh_preserves_head_time_and_exact_bytes(tmp_path):
    registry = str(tmp_path / "registry.jsonl")
    output = tmp_path / "eval-registry-latest.json"
    entry = reg.preregister(registry, ["probe"], suite="stable-summary")
    first = reg.refresh_summary(registry, output)
    first_bytes = output.read_bytes()

    second = reg.refresh_summary(registry, output)
    assert second == first
    assert output.read_bytes() == first_bytes
    assert second["generated_at"] == entry["ts"]


def test_registry_append_rejects_a_symlink_without_touching_its_target(tmp_path):
    external = tmp_path / "external.jsonl"
    external.write_text("external bytes stay exact\n", encoding="utf-8")
    registry = tmp_path / "registry.jsonl"
    registry.symlink_to(external)

    with pytest.raises(ValueError, match="regular file"):
        reg.preregister(str(registry), ["probe"])
    assert external.read_text(encoding="utf-8") == "external bytes stay exact\n"


def test_production_verifier_fails_when_public_registry_is_missing(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(verify_cli, "REGISTRY", str(tmp_path / "missing.jsonl"))
    monkeypatch.setattr(
        verify_cli,
        "REGISTRY_LATEST",
        str(tmp_path / "missing-latest.json"),
    )

    assert verify_cli.main() == 1
    assert "BROKEN" in capsys.readouterr().out


def test_production_verifier_uses_a_stable_read_only_snapshot_without_a_lock_file(
    monkeypatch, tmp_path
):
    registry = tmp_path / "eval-registry.jsonl"
    summary = tmp_path / "eval-registry-latest.json"
    reg.preregister(str(registry), ["probe"], suite="read-only-verifier")
    reg.write_summary(summary, reg.read_ledger(str(registry)))
    lock = tmp_path / ".eval-registry.jsonl.lock"
    lock.unlink()
    monkeypatch.setattr(verify_cli, "REGISTRY", str(registry))
    monkeypatch.setattr(verify_cli, "REGISTRY_LATEST", str(summary))
    monkeypatch.setattr(
        verify_cli,
        "MYQUANT_STORE",
        str(tmp_path / "myquant-model-evidence" / "sha256"),
    )
    monkeypatch.setattr(
        verify_cli,
        "MYQUANT_LATEST",
        str(tmp_path / "myquant-model-evidence-latest.json"),
    )

    assert verify_cli.main() == 0
    assert not lock.exists()


def test_production_verifier_reports_malformed_registry_without_traceback(
    monkeypatch, tmp_path, capsys
):
    registry = tmp_path / "eval-registry.jsonl"
    summary = tmp_path / "eval-registry-latest.json"
    registry.write_text("{}\n", encoding="utf-8")
    summary.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(verify_cli, "REGISTRY", str(registry))
    monkeypatch.setattr(verify_cli, "REGISTRY_LATEST", str(summary))

    assert verify_cli.main() == 1
    assert "BROKEN" in capsys.readouterr().out


def test_unlocked_read_only_verifier_rejects_a_changed_registry_snapshot(
    monkeypatch, tmp_path, capsys
):
    registry = tmp_path / "eval-registry.jsonl"
    summary = tmp_path / "eval-registry-latest.json"
    reg.preregister(str(registry), ["probe"], suite="snapshot-race")
    entries = reg.read_ledger(str(registry))
    reg.write_summary(summary, entries)
    (tmp_path / ".eval-registry.jsonl.lock").unlink()
    monkeypatch.setattr(verify_cli, "REGISTRY", str(registry))
    monkeypatch.setattr(verify_cli, "REGISTRY_LATEST", str(summary))
    monkeypatch.setattr(
        verify_cli,
        "MYQUANT_STORE",
        str(tmp_path / "myquant-model-evidence" / "sha256"),
    )
    monkeypatch.setattr(
        verify_cli,
        "MYQUANT_LATEST",
        str(tmp_path / "myquant-model-evidence-latest.json"),
    )
    original_snapshot = reg.read_ledger_snapshot
    reads = 0

    def changed_snapshot(path):
        nonlocal reads
        held_entries, raw = original_snapshot(path)
        reads += 1
        return held_entries, raw if reads == 1 else raw + b"changed"

    monkeypatch.setattr(reg, "read_ledger_snapshot", changed_snapshot)

    assert verify_cli.main() == 1
    assert "changed during" in capsys.readouterr().out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"=== eval_registry: {passed}/{len(fns)} passed ===")
    sys.exit(0 if passed == len(fns) else 1)
