"""Focused contracts for synchronous deployment snapshot recovery."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest

from scripts import recover_deployment_snapshots as recovery


NOW = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)
OBSERVED_AT = "2026-08-25T01:02:00Z"


def _result(name: str, *, status: str = "success") -> dict[str, object]:
    return {
        "collector": name,
        "duration_seconds": 1.25,
        "generated_at": OBSERVED_AT if status == "success" else None,
        "records_collected": 3 if status == "success" else 0,
        "status": status,
    }


def _proof(name: str) -> dict[str, object]:
    return {
        "bytes": 17,
        "path": recovery.LANE_OUTPUTS[name],
        "sha256": "a" * 64,
    }


def test_recovery_is_sequential_and_refreshes_status_only_after_all_lanes() -> None:
    events: list[str] = []

    def run(name: str) -> dict[str, object]:
        events.append(f"run:{name}")
        status = "abstained" if name == "archive-news-context" else "success"
        return _result(name, status=status)

    def prove(name: str) -> dict[str, object]:
        events.append(f"prove:{name}")
        return _proof(name)

    def refresh() -> dict[str, str]:
        events.append("node-status")
        return {"generated_at": OBSERVED_AT, "status": "degraded"}

    receipt = recovery.run_recovery(
        lane_runner=run,
        snapshot_prover=prove,
        node_refresher=refresh,
        clock=lambda: NOW,
    )

    assert receipt["status"] == "ok"
    assert receipt["failure_code"] is None
    assert [row["collector"] for row in receipt["lanes"]] == list(recovery.LANES)
    assert receipt["node_status"] == {
        "generated_at": OBSERVED_AT,
        "status": "degraded",
    }
    assert events == [
        event for name in recovery.LANES for event in (f"run:{name}", f"prove:{name}")
    ] + ["node-status"]
    payload = recovery.canonical_receipt(receipt)
    assert len(payload.encode()) <= recovery.MAX_RECEIPT_BYTES
    assert payload == json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@pytest.mark.parametrize("status", ["failed", "halted", "skipped"])
def test_nonaccepted_lane_state_fails_fast(status: str) -> None:
    called: list[str] = []

    def run(name: str) -> dict[str, object]:
        called.append(name)
        return _result(name, status=status)

    receipt = recovery.run_recovery(
        lane_runner=run,
        snapshot_prover=lambda name: pytest.fail(f"proved {name}"),
        node_refresher=lambda: pytest.fail("refreshed node status"),
        clock=lambda: NOW,
    )

    assert called == ["wayback"]
    assert receipt["status"] == "failed"
    assert receipt["failure_code"] == "lane-not-accepted"
    assert receipt["failed_stage"] == "snapshot:wayback"
    assert receipt["lanes"][0]["status"] == status


def test_success_without_an_exact_output_fails_closed() -> None:
    receipt = recovery.run_recovery(
        lane_runner=lambda name: _result(name),
        snapshot_prover=lambda _name: None,
        node_refresher=lambda: pytest.fail("refreshed node status"),
        clock=lambda: NOW,
    )

    assert receipt["status"] == "failed"
    assert receipt["failure_code"] == "snapshot-output-missing"
    assert receipt["failed_stage"] == "snapshot:wayback"


def test_abstention_cannot_claim_collected_records() -> None:
    invalid = _result("wayback", status="abstained")
    invalid["records_collected"] = 1
    receipt = recovery.run_recovery(
        lane_runner=lambda _name: invalid,
        snapshot_prover=lambda name: pytest.fail(f"proved {name}"),
        node_refresher=lambda: pytest.fail("refreshed node status"),
        clock=lambda: NOW,
    )

    assert receipt["failure_code"] == "lane-result-invalid"
    assert receipt["lanes"][0]["status"] == "abstained"


def test_unhashable_status_is_a_bounded_lane_failure() -> None:
    invalid = _result("wayback")
    invalid["status"] = ["success"]
    receipt = recovery.run_recovery(
        lane_runner=lambda _name: invalid,
        snapshot_prover=lambda name: pytest.fail(f"proved {name}"),
        node_refresher=lambda: pytest.fail("refreshed node status"),
        clock=lambda: NOW,
    )

    assert receipt["status"] == "failed"
    assert receipt["failure_code"] == "lane-result-invalid"
    assert receipt["lanes"][0]["status"] == "invalid"


def test_malformed_node_refresh_cannot_complete_recovery() -> None:
    receipt = recovery.run_recovery(
        lane_runner=lambda name: _result(name),
        snapshot_prover=_proof,
        node_refresher=lambda: {
            "generated_at": "not-a-time",
            "status": "healthy",
        },
        clock=lambda: NOW,
    )

    assert receipt["status"] == "failed"
    assert receipt["failure_code"] == "node-status-invalid"
    assert receipt["failed_stage"] == "node-status"
    assert len(receipt["lanes"]) == len(recovery.LANES)


def test_production_lane_reuses_lease_runner_and_terminal_log(monkeypatch) -> None:
    import core.collector_fleet as fleet
    import core.tasks as tasks

    events: list[object] = []
    collector_result = _result("wayback")

    def run_snapshot_job(name: str) -> dict[str, object]:
        events.append(("snapshot", name))
        return collector_result

    def run_with_lease(
        name: str,
        operation,
        *,
        timeout_s: int,
        collector_name: str,
    ) -> dict[str, object]:
        events.append(("lease", name, timeout_s, collector_name))
        return operation()

    monkeypatch.setattr(fleet, "run_snapshot_job", run_snapshot_job)
    monkeypatch.setattr(tasks, "_run_with_lease", run_with_lease)
    monkeypatch.setattr(
        tasks, "_log_snapshot_result", lambda value: events.append(("log", value))
    )

    assert recovery._run_lane("wayback") is collector_result
    assert events == [
        ("lease", "snapshot:wayback", recovery.LEASE_SECONDS, "wayback"),
        ("snapshot", "wayback"),
        ("log", collector_result),
    ]


def test_snapshot_proof_hashes_regular_bytes_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    output = tmp_path / recovery.LANE_OUTPUTS["wayback"]
    output.parent.mkdir(parents=True)
    payload = b'{"generated_at":"2026-08-25T01:02:00Z"}\n'
    output.write_bytes(payload)

    assert recovery.prove_snapshot("wayback", repo_root=tmp_path) == {
        "bytes": len(payload),
        "path": recovery.LANE_OUTPUTS["wayback"],
        "sha256": hashlib.sha256(payload).hexdigest(),
    }

    output.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_bytes(payload)
    output.symlink_to(target)
    with pytest.raises(recovery.RecoveryDataError, match="opened safely"):
        recovery.prove_snapshot("wayback", repo_root=tmp_path)


def test_snapshot_proof_rejects_a_hard_link(tmp_path: Path) -> None:
    output = tmp_path / recovery.LANE_OUTPUTS["wayback"]
    output.parent.mkdir(parents=True)
    output.write_bytes(b"snapshot\n")
    os.link(output, tmp_path / "second-name.json")

    with pytest.raises(recovery.RecoveryDataError, match="not one regular file"):
        recovery.prove_snapshot("wayback", repo_root=tmp_path)


def test_snapshot_proof_rejects_a_named_path_swap_while_hashing(
    monkeypatch, tmp_path: Path
) -> None:
    output = tmp_path / recovery.LANE_OUTPUTS["wayback"]
    output.parent.mkdir(parents=True)
    output.write_bytes(b"original snapshot\n")
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"replacement snapshot\n")
    displaced = tmp_path / "displaced-original.json"
    real_sha256 = recovery.hashlib.sha256

    class SwappingHash:
        def __init__(self) -> None:
            self._hash = real_sha256()
            self._swapped = False

        def update(self, payload: bytes) -> None:
            self._hash.update(payload)
            if not self._swapped:
                os.replace(output, displaced)
                os.replace(replacement, output)
                self._swapped = True

        def hexdigest(self) -> str:
            return self._hash.hexdigest()

    monkeypatch.setattr(recovery.hashlib, "sha256", SwappingHash)

    with pytest.raises(recovery.RecoveryDataError, match="path changed"):
        recovery.prove_snapshot("wayback", repo_root=tmp_path)


def test_controller_source_has_no_asynchronous_dispatch_or_retry_calls() -> None:
    tree = ast.parse(inspect.getsource(recovery))
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint({"send_task", "delay", "apply_async", "retry"})


def test_main_keeps_collector_chatter_out_of_the_single_receipt(
    monkeypatch, capsys
) -> None:
    receipt = {
        "failed_stage": None,
        "failure_code": None,
        "generated_at": "2026-08-25T01:02:03Z",
        "lanes": [],
        "node_status": {"generated_at": OBSERVED_AT, "status": "healthy"},
        "schema_version": recovery.RECEIPT_SCHEMA,
        "status": "ok",
    }

    def run() -> dict[str, object]:
        print("collector progress")
        return receipt

    monkeypatch.setattr(recovery, "run_recovery", run)
    assert recovery.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == recovery.canonical_receipt(receipt) + "\n"
    assert captured.err == "collector progress\n"


def test_oversized_receipt_is_refused() -> None:
    with pytest.raises(recovery.RecoveryDataError, match="byte ceiling"):
        recovery.canonical_receipt({"value": "x" * recovery.MAX_RECEIPT_BYTES})


def test_main_pairs_receipt_serialization_failure_with_nonzero_exit(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        recovery,
        "run_recovery",
        lambda: {
            "status": "ok",
            "oversized": "x" * recovery.MAX_RECEIPT_BYTES,
        },
    )

    assert recovery.main([]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "failed"
    assert receipt["failure_code"] == "receipt-invalid"
