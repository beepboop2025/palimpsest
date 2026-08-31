"""Contracts for the independent direct-publication continuity guard."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "ops/railway/palimpsest-continuity-guard"


def _load_guard():
    loader = SourceFileLoader("palimpsest_continuity_guard", str(GUARD_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()
REAL_MAINTENANCE_LOCK = guard._maintenance_lock


@pytest.fixture(autouse=True)
def _isolate_guard_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "_maintenance_lock", nullcontext)


def _state(
    unit: str,
    *,
    active: str = "active",
    enablement: str = "enabled",
) -> object:
    return guard.TimerState(
        unit=unit,
        load_state="loaded",
        unit_file_state=enablement,
        active_state=active,
    )


def _canonical(document: dict[str, object]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _hold(now: datetime) -> dict[str, object]:
    def clock(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    pre_state = {
        unit: {
            "load_state": "loaded",
            "unit_file_state": "enabled",
            "active_state": "active",
            "fragment_sha256": "c" * 64,
        }
        for unit in guard.TIMERS
    }
    return {
        "schema_version": "palimpsest.continuity-maintenance-hold.v1",
        "status": "active",
        "transaction_id": "a" * 32,
        "reason_code": "reviewed-release",
        "created_at": clock(now - timedelta(minutes=5)),
        "expires_at": clock(now + timedelta(minutes=55)),
        "controller_commit": "b" * 40,
        "pre_state": pre_state,
        "restore_profile_sha256": guard._restore_profile_sha256(pre_state),
    }


def _write_authority(path: Path, document: dict[str, object], mode: int) -> str:
    payload = _canonical(document)
    path.write_bytes(payload)
    path.chmod(mode)
    return hashlib.sha256(payload).hexdigest()


def _celery_receipt() -> dict[str, object]:
    topology = [
        {"node": "collectors@fixture", "queue": "collectors"},
        {"node": "default@fixture", "queue": "celery"},
        {"node": "warehouse@fixture", "queue": "warehouse"},
    ]
    nodes = {item["node"]: item["queue"] for item in topology}
    topology_payload = json.dumps(
        {
            "schema_version": "palimpsest-celery-release-topology.v1",
            "nodes": topology,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "cancellations": [],
        "consumer_state": "consuming",
        "drain_samples": 0,
        "final": {
            "active_queues": {node: [queue] for node, queue in nodes.items()},
            "broker_depth": {queue: 0 for queue in guard.BROKER_QUEUES},
            "task_counts": {
                node: {"active": 0, "reserved": 0, "scheduled": 0}
                for node in nodes
            },
            "unacknowledged": {"hash": 0, "index": 0},
        },
        "generated_at": "2026-08-31T08:00:30Z",
        "required_zero_samples": 2,
        "samples_observed": 2,
        "schema_version": "palimpsest-celery-release-gate.v1",
        "status": "quiet",
        "topology": topology,
        "topology_sha256": hashlib.sha256(topology_payload).hexdigest(),
    }


def _collector_recovery() -> dict[str, object]:
    lanes = []
    for index, (name, output_path) in enumerate(guard.RECOVERY_LANE_OUTPUTS.items()):
        lanes.append(
            {
                "collector": name,
                "duration_seconds": 1.5,
                "generated_at": "2026-08-31T07:58:00Z",
                "output": {
                    "bytes": 100 + index,
                    "path": output_path,
                    "sha256": f"{index + 1:x}" * 64,
                },
                "records_collected": index + 1,
                "status": "success",
            }
        )
    return {
        "failed_stage": None,
        "failure_code": None,
        "generated_at": "2026-08-31T07:59:00Z",
        "lanes": lanes,
        "node_status": {
            "generated_at": "2026-08-31T07:59:00Z",
            "status": "healthy",
        },
        "schema_version": "palimpsest-deployment-snapshot-recovery.v1",
        "status": "ok",
    }


def _release_authority(
    receipt_dir: Path,
    recovery_dir: Path,
    *,
    stamp: str = "20260831T080000Z",
    recovery: bool = False,
) -> tuple[Path, str, Path]:
    transaction = "a" * 32
    controller = "b" * 40
    prior = "927e0a8b5c82a008f3ffa08a5f5518b8efa8bffd"
    stem = f"{stamp}-{controller[:12]}-{transaction}"
    proof_path = receipt_dir / f"{stem}.proof-complete.json"
    finalized_path = receipt_dir / f"{stem}.finalized.json"
    deployment = {
        "direction": "forward",
        "previous_checkout_sha": prior,
        "previous_deployment_receipt_sha": prior,
        "deployed_sha": controller,
        "controller_sha": controller,
        "controller_tree_sha256": "3" * 64,
        "candidate_image_id": "sha256:" + "4" * 64,
        "candidate_render_gateway_image_id": None,
    }
    handoff = {
        "artifact_sha256": "7" * 64,
        "expected_deploy_sha": controller,
        "fetched_main": "3" * 40,
        "ledger_sha256": "8" * 64,
        "public_ledger_sha256": "9" * 64,
        "public_manifest_sha256": "a" * 64,
        "public_osint_stub_sha256": "b" * 64,
        "public_release_commit": "3" * 40,
        "public_rights_status_sha256": "c" * 64,
        "publication_commit": "4" * 40,
        "railway_canary_run_id": 81,
        "resume_token": transaction,
        "schema": "palimpsest-public-osint-release-proof.v2",
        "workflow_head_sha": controller,
        "workflow_receipt_sha256": "d" * 64,
        "workflow_run_attempt": 1,
        "workflow_run_id": 80,
    }
    sync = {
        "artifact_canonical_sha256": "e" * 64,
        "artifact_sha256": handoff["artifact_sha256"],
        "deployed_commit": controller,
        "fetched_main": handoff["fetched_main"],
        "generated_at": "2026-08-31T07:57:00Z",
        "input_commit": controller,
        "installed_at": "2026-08-31T07:59:00Z",
        "ledger_entries": 4,
        "ledger_head": "f" * 64,
        "ledger_sha256": handoff["ledger_sha256"],
        "public_ledger_sha256": handoff["public_ledger_sha256"],
        "public_manifest_sha256": handoff["public_manifest_sha256"],
        "public_osint_stub_sha256": handoff["public_osint_stub_sha256"],
        "public_release_commit": handoff["public_release_commit"],
        "public_rights_status_sha256": handoff["public_rights_status_sha256"],
        "publication_commit": handoff["publication_commit"],
        "release_proof_sha256": hashlib.sha256(_canonical(handoff)).hexdigest(),
        "schema": "palimpsest-public-osint-sync.v3",
        "status": "installed",
        "sync_mode": "release-pinned",
    }
    compose_before = {
        name: {
            "container_id": "1" * 64,
            "image_id": "sha256:" + "2" * 64,
            "was_running": True,
        }
        for name in guard.COMPOSE_WRITERS
    }
    proof: dict[str, object] = {
        "schema_version": "palimpsest-host-release.v1",
        "status": "proof-complete",
        "generated_at": "2026-08-31T08:00:00Z",
        "transaction_id": transaction,
        "deployment": deployment,
        "backup": {
            "core_snapshot": "/private/core",
            "legacy_witness_status": {
                "path": None,
                "preserved": False,
                "sha256": None,
            },
            "pre_change_v4_snapshot": "/private/v4",
            "verification": {"schema_version": "fixture.backup.v1", "status": "ok"},
        },
        "publication": {
            "bleedthrough_normalized_sha256": "6" * 64,
            "handoff": handoff,
            "ledger_sha256": handoff["ledger_sha256"],
            "osint_sha256": handoff["artifact_sha256"],
            "sync_receipt": sync,
        },
        "observers": {
            "policy_sha256": "7" * 64,
            "watchdog": {
                "baseline_token_sha256": "8" * 64,
                "exit_pair": "0:0",
                "proof": {"status": "ok"},
            },
            "witness": {
                "baseline_token_sha256": "9" * 64,
                "exit_pair": "0:0",
                "proof": {"status": "ok"},
            },
        },
        "celery": {
            "candidate_consuming": {"status": "quiet"},
            "candidate_fenced": {"status": "quiet"},
            "pre_change": {"status": "quiet"},
            "v4_backup_fenced": {"status": "quiet"},
        },
        "recovery": _collector_recovery(),
        "compose_before": compose_before,
        "controller_manifest_sha256": "5" * 64,
        "installed_unit_sha256": {
            path: "a" * 64 for path in guard.PROOF_UNIT_PATHS
        },
        "release_proof_present": True,
        "writers_restored": False,
    }
    binding: dict[str, object] | None = None
    if recovery:
        incident = "2026-08-26-interrupted-phase1-hybrid-recovery"
        snapshot = "20260826T080000Z"
        manifest = json.loads(
            (
                ROOT
                / "ops/release-recovery/2026-08-26-interrupted-phase1-hybrid-recovery.json"
            ).read_text(encoding="utf-8")
        )
        prepared = {
            "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
            "status": "prepared",
            "prepared_at": "2026-08-31T07:00:00Z",
            "transaction_id": transaction,
            "incident_id": incident,
            "manifest_sha256": guard.INTERRUPTED_RECOVERY_AUTHORITIES[incident][
                "manifest_sha256"
            ],
            "hybrid_fingerprint_sha256": "a" * 64,
            "restore_profile_sha256": "b" * 64,
            "compose_environment_sha256": "c" * 64,
            "broker_queue_sha256": "d" * 64,
            "prior_checkout_commit": prior,
            "prior_deployed_commit": prior,
            "failed_target_commit": prior,
            "recovery_controller_commit": controller,
            "minimum_recovery_ancestor": prior,
            "target_commit": controller,
        }
        broker = {
            "schema_version": "palimpsest-celery-broker-release-gate.v1",
            "generated_at": "2026-08-31T07:10:00Z",
            "status": "empty",
            "closed_queues_sha256": "d" * 64,
            "closed_queues": ["celery", "collectors", "warehouse", "censorwatch"],
            "required_zero_samples": 2,
            "samples_observed": 2,
            "final": {
                "broker_depth": {
                    "celery": 0,
                    "collectors": 0,
                    "warehouse": 0,
                    "censorwatch": 0,
                },
                "unacknowledged": {"hash": 0, "index": 0},
            },
        }
        migration = {
            "schema_version": "palimpsest-interrupted-phase1-migration.v1",
            "status": "succeeded",
            "container_id": "e" * 64,
            "image_id": deployment["candidate_image_id"],
            "revision": controller,
            "backup_verified_at": "2026-08-31T07:20:00Z",
            "started_at": "2026-08-31T07:21:00Z",
            "exit_code": 0,
        }
        backup_verification = {
            "counts": {
                "artifact_directories": 1,
                "artifact_files": 1,
                "artifact_members": 1,
                "checksum_entries": 5,
                "snapshot_files": 6,
                "witness_history_records": 1,
            },
            "digests": {
                "MANIFEST.txt": "1" * 64,
                "artifacts.list": "2" * 64,
                "artifacts.tar.gz": "3" * 64,
                "postgres.dump": "4" * 64,
                "postgres.list": "5" * 64,
            },
            "schema": "palimpsest-node-backup-verification.v1",
            "snapshot": snapshot,
            "status": "verified",
        }
        binding = {
            "schema_version": "palimpsest-interrupted-phase1-binding.v2",
            "incident_id": incident,
            "transaction_id": transaction,
            "target_commit": controller,
            "failed_target_commit": prior,
            "recovery_controller_commit": controller,
            "minimum_recovery_ancestor": prior,
            "manifest_sha256": guard.INTERRUPTED_RECOVERY_AUTHORITIES[incident][
                "manifest_sha256"
            ],
            "manifest": manifest,
            "hybrid_fingerprint_sha256": "a" * 64,
            "restore_profile_sha256": "b" * 64,
            "compose_environment_sha256": "c" * 64,
            "broker_queue_sha256": "d" * 64,
            "prepared_receipt_path": str(
                recovery_dir
                / "2026-08-26-interrupted-phase1-hybrid-recovery.prepared.json"
            ),
            "prepared_receipt_sha256": hashlib.sha256(
                _canonical(prepared)
            ).hexdigest(),
            "prepared_receipt": prepared,
            "broker_empty_receipt_sha256": hashlib.sha256(
                _canonical(broker)
            ).hexdigest(),
            "broker_empty_receipt": broker,
            "migration_receipt": migration,
            "backup": {
                "reason": "interrupted-phase1-hybrid-recovery-fresh-target-backup",
                "core_snapshot": snapshot,
                "current_snapshot": snapshot,
                "verification": backup_verification,
            },
        }
        proof["interrupted_phase1_resume"] = binding
    proof_sha256 = _write_authority(proof_path, proof, 0o600)

    compose = {
        name: {
            "container_id": "6" * 64,
            "hostname": f"{name}-host",
            "image_id": "sha256:" + "7" * 64,
            "state": "running",
            "was_running": True,
        }
        for name in guard.COMPOSE_WRITERS
    }
    finalized: dict[str, object] = {
        "schema_version": "palimpsest-host-release-finalization.v1",
        "status": "finalized",
        "finalized_at": "2026-08-31T08:01:00Z",
        "transaction_id": transaction,
        "previous_checkout_sha": prior,
        "previous_deployment_receipt_sha": prior,
        "deployed_sha": controller,
        "proof_complete_receipt": str(proof_path),
        "proof_complete_receipt_sha256": proof_sha256,
        "release_proof_present": False,
        "writers_restored": True,
        "restored_celery": _celery_receipt(),
        "restored_activators": {
            name: {
                "active_state": "active",
                "before_enablement": "enabled",
                "enablement": "enabled",
                "was_active": True,
            }
            for name in guard.RELEASE_ACTIVATORS
        },
        "restored_compose_writers": compose,
        "restored_beat": compose["beat"],
        "backup_on_success": "snapshot",
        "backup_release_quiesce_present": False,
    }
    completion_path = (
        recovery_dir
        / "2026-08-26-interrupted-phase1-hybrid-recovery.complete.json"
    )
    if binding is not None:
        finalized.update(
            {
                "interrupted_phase1_resume": binding,
                "interrupted_phase1_completion_required": True,
                "interrupted_phase1_completion_receipt": str(completion_path),
            }
        )
    finalized_sha256 = _write_authority(finalized_path, finalized, 0o600)
    if binding is not None:
        application_image = binding["migration_receipt"]["image_id"]
        application = {
            "image_id": application_image,
            "revision": controller,
            "state": "running",
        }
        runtime = {
            "schema_version": "palimpsest-interrupted-phase1-final-runtime.v1",
            "verified_at": "2026-08-31T08:01:15Z",
            "infrastructure": {
                "postgres": {
                    "container_id": "1" * 64,
                    "image_id": "sha256:" + "2" * 64,
                    "state": "running",
                },
                "redis": {
                    "container_id": "3" * 64,
                    "image_id": "sha256:" + "4" * 64,
                    "state": "running",
                },
            },
            "api": {"container_id": "5" * 64, **application},
            "migration": {
                "container_id": binding["migration_receipt"]["container_id"],
                "image_id": application_image,
                "revision": controller,
                "state": "exited",
                "exit_code": 0,
            },
            "beat": {"container_id": "6" * 64, **application},
            "workers": {
                "worker": {"container_id": "7" * 64, **application},
                "worker-collectors": {
                    "container_id": "8" * 64,
                    **application,
                },
                "worker-warehouse": {
                    "container_id": "9" * 64,
                    **application,
                },
            },
            "node_offsite": {
                "enablement": "disabled",
                "active_state": "inactive",
            },
            "velocity": {"presence": "absent"},
        }
        completion = {
            "schema_version": "palimpsest-interrupted-phase1-completion.v2",
            "status": "completed",
            "completed_at": "2026-08-31T08:01:30Z",
            "incident_id": binding["incident_id"],
            "transaction_id": transaction,
            "target_commit": controller,
            "failed_target_commit": binding["failed_target_commit"],
            "recovery_controller_commit": controller,
            "minimum_recovery_ancestor": binding["minimum_recovery_ancestor"],
            "manifest_sha256": binding["manifest_sha256"],
            "compose_environment_sha256": binding["compose_environment_sha256"],
            "broker_queue_sha256": binding["broker_queue_sha256"],
            "prepared_receipt_path": binding["prepared_receipt_path"],
            "prepared_receipt_sha256": binding["prepared_receipt_sha256"],
            "phase3_binding_sha256": hashlib.sha256(
                _canonical(binding)
            ).hexdigest(),
            "finalized_receipt_path": str(finalized_path),
            "finalized_receipt_sha256": finalized_sha256,
            "backup_reason": binding["backup"]["reason"],
            "recovery_snapshot": binding["backup"]["current_snapshot"],
            "final_runtime_sha256": hashlib.sha256(_canonical(runtime)).hexdigest(),
            "final_runtime": runtime,
        }
        _write_authority(completion_path, completion, 0o400)
    return finalized_path, finalized_sha256, completion_path


def _configure_maintenance_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "active",
) -> tuple[
    dict[str, object],
    Path,
    Path,
    Path,
    dict[str, str],
    dict[str, tuple[Path, str] | None],
]:
    document = _hold(datetime.now(UTC))
    document["status"] = status
    state_dir = tmp_path / "state"
    receipt_dir = tmp_path / "receipts"
    recovery_dir = tmp_path / "recovery"
    for directory in (state_dir, receipt_dir, recovery_dir):
        directory.mkdir()
        directory.chmod(0o700)
    hold_path = state_dir / "maintenance-hold.json"
    hold_path.write_bytes(_canonical(document))
    hold_path.chmod(0o600)
    monkeypatch.setattr(guard, "HOLD_PATH", hold_path)
    monkeypatch.setattr(guard, "RECEIPT_DIR", receipt_dir)
    monkeypatch.setattr(guard, "RECOVERY_RECEIPT_DIR", recovery_dir)
    monkeypatch.setattr(
        guard,
        "_maintenance_assert_unlocked",
        lambda **_arguments: document,
    )
    monkeypatch.setattr(
        guard,
        "_timer_states",
        lambda: [_state(unit) for unit in guard.TIMERS],
    )
    monkeypatch.setattr(
        guard,
        "_fragment_sha256",
        lambda _unit, *, load_state, expected_drop_in: "e" * 64
        if load_state == "loaded"
        else "f" * 64,
    )
    expected = {unit: "e" * 64 for unit in guard.TIMERS}
    expected_drop_ins = {unit: None for unit in guard.TIMERS}
    return (
        document,
        hold_path,
        receipt_dir,
        recovery_dir,
        expected,
        expected_drop_ins,
    )


def test_guard_inventory_is_the_complete_direct_publication_lane() -> None:
    assert guard.TIMERS == (
        "palimpsest-evidence-wire.timer",
        "palimpsest-measurement-refresh.timer",
        "palimpsest-railway-publish.timer",
        "palimpsest-direct-watchdog.timer",
    )
    assert os.access(GUARD_PATH, os.X_OK)


def test_guard_exposes_an_exact_capability_handshake(capsys) -> None:
    assert guard.main(["capabilities"]) == 0
    assert capsys.readouterr().out == (
        '{"commands":["capabilities","check","maintenance-assert",'
        '"maintenance-begin","maintenance-end","maintenance-fail-closed",'
        '"maintenance-inspect","maintenance-reconcile-finalized"],"hold_schema":'
        '"palimpsest.continuity-maintenance-hold.v1","schema_version":'
        '"palimpsest.continuity-guard-capabilities.v1"}\n'
    )


def test_drop_in_cli_profile_requires_all_four_timer_authorities() -> None:
    evidence_path = Path(
        "/etc/systemd/system/palimpsest-evidence-wire.timer.d/"
        "90-five-minute-live.conf"
    )
    values = [
        f"{guard.TIMERS[0]}={evidence_path}={'a' * 64}",
        *(f"{unit}=absent" for unit in guard.TIMERS[1:]),
    ]

    assert guard._expected_drop_in_arguments(values) == {
        guard.TIMERS[0]: (evidence_path, "a" * 64),
        guard.TIMERS[1]: None,
        guard.TIMERS[2]: None,
        guard.TIMERS[3]: None,
    }
    with pytest.raises(guard.GuardError, match="inventory"):
        guard._expected_drop_in_arguments(values[:-1])


def test_only_enabled_inactive_timers_are_repair_candidates() -> None:
    states = [
        _state(guard.TIMERS[0], active="inactive"),
        _state(guard.TIMERS[1]),
        _state(guard.TIMERS[2], active="failed", enablement="disabled"),
        _state(guard.TIMERS[3], active="activating"),
    ]

    assert guard.repair_candidates(states, blockers=[]) == [guard.TIMERS[0]]
    assert guard.repair_candidates(states, blockers=["DATA HOLD"]) == []


def test_maintenance_lock_serializes_two_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = multiprocessing.get_context("fork")
    monkeypatch.setattr(guard, "MAINTENANCE_LOCK_PATH", tmp_path / "maintenance.lock")
    monkeypatch.setattr(guard, "_state_directory", lambda: tmp_path)
    first_parent, first_child = context.Pipe(duplex=False)
    second_parent, second_child = context.Pipe(duplex=False)
    release_first = context.Event()
    release_second = context.Event()

    def contender(connection, release) -> None:  # type: ignore[no-untyped-def]
        with REAL_MAINTENANCE_LOCK():
            connection.send("entered")
            if not release.wait(5):
                raise RuntimeError("test lock release timed out")
            connection.send("left")
        connection.close()

    first = context.Process(target=contender, args=(first_child, release_first))
    second = context.Process(target=contender, args=(second_child, release_second))
    first.start()
    try:
        assert first_parent.poll(5)
        assert first_parent.recv() == "entered"
        second.start()
        assert not second_parent.poll(0.25)
        release_first.set()
        assert first_parent.poll(5)
        assert first_parent.recv() == "left"
        assert second_parent.poll(5)
        assert second_parent.recv() == "entered"
        release_second.set()
        assert second_parent.poll(5)
        assert second_parent.recv() == "left"
    finally:
        release_first.set()
        release_second.set()
        first.join(timeout=5)
        if second.pid is not None:
            second.join(timeout=5)
        if first.is_alive():
            first.terminate()
        if second.pid is not None and second.is_alive():
            second.terminate()
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_maintenance_hold_is_canonical_root_owned_and_bounded() -> None:
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    document = _hold(now)
    raw = _canonical(document)

    assert guard.validate_maintenance_hold(
        raw, now=now, owner_uid=0, owner_gid=0, mode=0o100600
    ) == document
    with pytest.raises(guard.GuardError, match="ownership or mode"):
        guard.validate_maintenance_hold(
            raw, now=now, owner_uid=501, owner_gid=0, mode=0o100600
        )
    with pytest.raises(guard.GuardError, match="not canonical"):
        guard.validate_maintenance_hold(
            json.dumps(document).encode(),
            now=now,
            owner_uid=0,
            owner_gid=0,
            mode=0o100600,
        )
    document["expires_at"] = (
        now + timedelta(seconds=guard.MAX_HOLD_SECONDS + 1)
    ).isoformat().replace("+00:00", "Z")
    with pytest.raises(guard.GuardError, match="bounded lease"):
        guard.validate_maintenance_hold(
            _canonical(document),
            now=now,
            owner_uid=0,
            owner_gid=0,
            mode=0o100600,
        )


def test_maintenance_begin_never_downgrades_a_fail_closed_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    existing = _hold(now)
    existing["status"] = "fail_closed"
    written: list[bytes] = []
    monkeypatch.setattr(
        guard,
        "_read_maintenance_hold",
        lambda **_arguments: existing,
    )
    monkeypatch.setattr(
        guard,
        "_snapshot_pre_state",
        lambda: (_ for _ in ()).throw(AssertionError("must preserve pre-state")),
    )
    monkeypatch.setattr(
        guard,
        "_atomic_write",
        lambda _path, raw, *, mode: written.append(raw)
        if mode == 0o600
        else (_ for _ in ()).throw(AssertionError("wrong mode")),
    )

    document = guard.maintenance_begin(
        transaction_id="a" * 32,
        controller_commit="b" * 40,
        reason_code="reviewed-release",
    )

    assert document["status"] == "fail_closed"
    assert document["pre_state"] == existing["pre_state"]
    assert written == [_canonical(document)]


def test_maintenance_end_requires_the_reviewed_fragment_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        _, hold_path, receipt_dir, recovery_dir, expected, expected_drop_ins
    ) = _configure_maintenance_end(tmp_path, monkeypatch)
    finalized_path, finalized_sha256, _ = _release_authority(
        receipt_dir, recovery_dir
    )
    wrong = dict(expected)
    wrong[guard.TIMERS[0]] = "f" * 64

    with pytest.raises(guard.GuardError, match="not the reviewed target"):
        guard.maintenance_end(
            transaction_id="a" * 32,
            controller_commit="b" * 40,
            expected_fragments=wrong,
            expected_drop_ins=expected_drop_ins,
            finalized_receipt=finalized_path,
            finalized_sha256=finalized_sha256,
        )
    assert hold_path.exists()

    guard.maintenance_end(
        transaction_id="a" * 32,
        controller_commit="b" * 40,
        expected_fragments=expected,
        expected_drop_ins=expected_drop_ins,
        finalized_receipt=finalized_path,
        finalized_sha256=finalized_sha256,
    )
    assert not hold_path.exists()


def test_effective_timer_profile_rejects_drop_ins_and_pending_daemon_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = guard.TIMERS[0]
    unit_path = tmp_path / unit
    payload = b"[Timer]\nOnCalendar=hourly\n"
    unit_path.write_bytes(payload)
    unit_path.chmod(0o644)
    profile = {
        "FragmentPath": str(unit_path),
        "NeedDaemonReload": "no",
        "DropInPaths": "",
    }
    monkeypatch.setattr(guard, "SYSTEMD_UNIT_DIR", tmp_path)
    monkeypatch.setattr(
        guard,
        "_property",
        lambda _unit, name: profile[name],
    )
    monkeypatch.setattr(
        guard,
        "_optional_property",
        lambda _unit, name: profile[name],
    )

    assert guard._fragment_sha256(
        unit, load_state="loaded", expected_drop_in=None
    ) == hashlib.sha256(payload).hexdigest()

    profile["DropInPaths"] = "/etc/systemd/system/example.timer.d/injected.conf"
    with pytest.raises(guard.GuardError, match="unreviewed drop-ins"):
        guard._fragment_sha256(unit, load_state="loaded", expected_drop_in=None)

    drop_in_dir = tmp_path / f"{unit}.d"
    drop_in_dir.mkdir()
    drop_in_path = drop_in_dir / "90-five-minute-live.conf"
    drop_in_payload = b"[Timer]\nOnUnitActiveSec=5m\n"
    drop_in_path.write_bytes(drop_in_payload)
    drop_in_path.chmod(0o644)
    drop_in_sha256 = hashlib.sha256(drop_in_payload).hexdigest()
    profile["DropInPaths"] = str(drop_in_path)
    assert guard._fragment_sha256(
        unit,
        load_state="loaded",
        expected_drop_in=(drop_in_path, drop_in_sha256),
    ) == hashlib.sha256(payload).hexdigest()
    with pytest.raises(guard.GuardError, match="reviewed drop-in profile"):
        guard._fragment_sha256(
            unit,
            load_state="loaded",
            expected_drop_in=(drop_in_path, "0" * 64),
        )
    profile["DropInPaths"] = f"{drop_in_path} /tmp/injected.conf"
    with pytest.raises(guard.GuardError, match="reviewed drop-in profile"):
        guard._fragment_sha256(
            unit,
            load_state="loaded",
            expected_drop_in=(drop_in_path, drop_in_sha256),
        )

    profile["DropInPaths"] = ""
    profile["NeedDaemonReload"] = "yes"
    with pytest.raises(guard.GuardError, match="unapplied daemon reload"):
        guard._fragment_sha256(unit, load_state="loaded", expected_drop_in=None)


@pytest.mark.parametrize("status", ["active", "fail_closed"])
def test_finalized_authority_reconciles_either_safe_hold_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    (
        _, hold_path, receipt_dir, recovery_dir, expected, expected_drop_ins
    ) = _configure_maintenance_end(tmp_path, monkeypatch, status=status)
    finalized_path, finalized_sha256, _ = _release_authority(
        receipt_dir, recovery_dir
    )

    observed_path, observed_sha256 = guard.maintenance_reconcile_finalized(
        transaction_id="a" * 32,
        controller_commit="b" * 40,
        expected_fragments=expected,
        expected_drop_ins=expected_drop_ins,
    )

    assert (observed_path, observed_sha256) == (
        finalized_path,
        finalized_sha256,
    )
    assert not hold_path.exists()


@pytest.mark.parametrize("partial", ["prepared", "completion"])
def test_partial_recovery_authority_never_removes_the_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    partial: str,
) -> None:
    (
        _, hold_path, _, recovery_dir, expected, expected_drop_ins
    ) = _configure_maintenance_end(tmp_path, monkeypatch)
    suffix = "prepared" if partial == "prepared" else "complete"
    partial_path = recovery_dir / f"incident.{suffix}.json"
    _write_authority(partial_path, {"status": partial}, 0o400)

    with pytest.raises(guard.GuardError, match="absent or ambiguous"):
        guard.maintenance_reconcile_finalized(
            transaction_id="a" * 32,
            controller_commit="b" * 40,
            expected_fragments=expected,
            expected_drop_ins=expected_drop_ins,
        )
    assert hold_path.exists()


def test_malformed_or_ambiguous_finalized_authority_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        _, hold_path, receipt_dir, recovery_dir, expected, expected_drop_ins
    ) = _configure_maintenance_end(tmp_path, monkeypatch)
    malformed = (
        receipt_dir
        / f"20260831T080000Z-{'b' * 12}-{'a' * 32}.finalized.json"
    )
    _write_authority(malformed, {"status": "finalized"}, 0o600)

    with pytest.raises(guard.GuardError):
        guard.maintenance_reconcile_finalized(
            transaction_id="a" * 32,
            controller_commit="b" * 40,
            expected_fragments=expected,
            expected_drop_ins=expected_drop_ins,
        )
    assert hold_path.exists()

    malformed.unlink()
    _release_authority(
        receipt_dir, recovery_dir, stamp="20260831T080000Z"
    )
    _release_authority(
        receipt_dir, recovery_dir, stamp="20260831T080100Z"
    )
    with pytest.raises(guard.GuardError, match="absent or ambiguous"):
        guard.maintenance_reconcile_finalized(
            transaction_id="a" * 32,
            controller_commit="b" * 40,
            expected_fragments=expected,
            expected_drop_ins=expected_drop_ins,
        )
    assert hold_path.exists()


def test_incident_finalization_requires_its_bound_completion_and_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        _, hold_path, receipt_dir, recovery_dir, expected, expected_drop_ins
    ) = _configure_maintenance_end(tmp_path, monkeypatch)
    finalized_path, finalized_sha256, completion_path = _release_authority(
        receipt_dir, recovery_dir, recovery=True
    )

    completion_path.unlink()
    with pytest.raises(guard.GuardError, match="cannot open recovery completion"):
        guard.maintenance_reconcile_finalized(
            transaction_id="a" * 32,
            controller_commit="b" * 40,
            expected_fragments=expected,
            expected_drop_ins=expected_drop_ins,
        )
    assert hold_path.exists()

    finalized_path.unlink()
    proof_path = receipt_dir / finalized_path.name.replace(
        ".finalized.json", ".proof-complete.json"
    )
    proof_path.unlink()
    finalized_path, finalized_sha256, _ = _release_authority(
        receipt_dir, recovery_dir, recovery=True
    )
    observed_path, observed_sha256 = guard.maintenance_reconcile_finalized(
        transaction_id="a" * 32,
        controller_commit="b" * 40,
        expected_fragments=expected,
        expected_drop_ins=expected_drop_ins,
    )
    assert (observed_path, observed_sha256) == (
        finalized_path,
        finalized_sha256,
    )
    assert not hold_path.exists()


def test_finalized_receipt_without_its_exact_proof_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        _, hold_path, receipt_dir, recovery_dir, expected, expected_drop_ins
    ) = _configure_maintenance_end(tmp_path, monkeypatch)
    finalized_path, _, _ = _release_authority(receipt_dir, recovery_dir)
    proof_path = receipt_dir / finalized_path.name.replace(
        ".finalized.json", ".proof-complete.json"
    )
    proof_path.unlink()

    with pytest.raises(guard.GuardError, match="cannot open proof-complete"):
        guard.maintenance_reconcile_finalized(
            transaction_id="a" * 32,
            controller_commit="b" * 40,
            expected_fragments=expected,
            expected_drop_ins=expected_drop_ins,
        )
    assert hold_path.exists()


def test_digest_consistent_but_semantically_empty_proof_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        _, hold_path, receipt_dir, recovery_dir, expected, expected_drop_ins
    ) = _configure_maintenance_end(tmp_path, monkeypatch)
    finalized_path, _, _ = _release_authority(receipt_dir, recovery_dir)
    proof_path = receipt_dir / finalized_path.name.replace(
        ".finalized.json", ".proof-complete.json"
    )
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["publication"] = {}
    proof_sha256 = _write_authority(proof_path, proof, 0o600)
    finalized = json.loads(finalized_path.read_text(encoding="utf-8"))
    finalized["proof_complete_receipt_sha256"] = proof_sha256
    _write_authority(finalized_path, finalized, 0o600)

    with pytest.raises(guard.GuardError, match="proof publication"):
        guard.maintenance_reconcile_finalized(
            transaction_id="a" * 32,
            controller_commit="b" * 40,
            expected_fragments=expected,
            expected_drop_ins=expected_drop_ins,
        )
    assert hold_path.exists()


def test_explicit_end_rejects_a_digest_not_bound_to_finalized_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        _, hold_path, receipt_dir, recovery_dir, expected, expected_drop_ins
    ) = _configure_maintenance_end(tmp_path, monkeypatch)
    finalized_path, _, _ = _release_authority(receipt_dir, recovery_dir)

    with pytest.raises(guard.GuardError, match="digest does not match"):
        guard.maintenance_end(
            transaction_id="a" * 32,
            controller_commit="b" * 40,
            expected_fragments=expected,
            expected_drop_ins=expected_drop_ins,
            finalized_receipt=finalized_path,
            finalized_sha256="0" * 64,
        )
    assert hold_path.exists()


def test_guard_repairs_an_accidentally_stopped_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = [_state(unit) for unit in guard.TIMERS]
    before[1] = _state(guard.TIMERS[1], active="inactive")
    after = [_state(unit) for unit in guard.TIMERS]
    snapshots = iter((before, after))
    commands: list[tuple[str, ...]] = []
    written: list[dict[str, object]] = []

    monkeypatch.setattr(guard, "_timer_states", lambda: next(snapshots))
    monkeypatch.setattr(guard, "_blockers", lambda _now: [])

    def command(*arguments: str) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(guard, "_systemctl", command)
    monkeypatch.setattr(guard, "_property", lambda _unit, _name: "active")
    monkeypatch.setattr(guard, "_write_status", written.append)

    assert guard.main() == 0
    assert commands == [
        ("reset-failed", guard.TIMERS[1]),
        ("start", guard.TIMERS[1]),
    ]
    assert written[0]["status"] == "repaired"
    assert written[0]["repairs"] == [guard.TIMERS[1]]
    assert written[0]["timers"][1]["active_state"] == "active"


def test_systemctl_transport_failures_become_guard_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("systemctl", 20)
        ),
    )

    with pytest.raises(guard.GuardError, match="control plane is unavailable"):
        guard._systemctl("show", guard.TIMERS[0])


def test_dangling_safety_marker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "DATA-HOLD.json"
    marker.symlink_to(tmp_path / "missing-target")
    monkeypatch.setattr(guard, "SAFETY_MARKERS", (marker,))
    monkeypatch.setattr(guard, "PUBLICATION_LOCKS", ())
    monkeypatch.setattr(
        guard,
        "_systemctl",
        lambda *_arguments: subprocess.CompletedProcess((), 0, "", ""),
    )

    with pytest.raises(guard.GuardError, match="not a regular file"):
        guard._blockers(datetime(2026, 8, 31, 8, 0, tzinfo=UTC))


def test_failed_release_disable_cannot_trigger_guard_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = [_state(unit) for unit in guard.TIMERS]
    states[0] = _state(guard.TIMERS[0], active="inactive")
    written: list[dict[str, object]] = []
    monkeypatch.setattr(guard, "_timer_states", lambda: states)
    monkeypatch.setattr(
        guard,
        "_blockers",
        lambda _now: ["maintenance-hold:fail_closed"],
    )
    monkeypatch.setattr(
        guard,
        "_systemctl",
        lambda *_arguments: (_ for _ in ()).throw(
            AssertionError("maintenance hold must block restart")
        ),
    )
    monkeypatch.setattr(guard, "_write_status", written.append)

    assert guard.main() == 0
    assert written[0]["status"] == "abstained"
    assert written[0]["repairs"] == []


def test_guard_abstains_instead_of_repairing_across_a_safety_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = [_state(unit) for unit in guard.TIMERS]
    states[0] = _state(guard.TIMERS[0], active="inactive")
    written: list[dict[str, object]] = []

    monkeypatch.setattr(guard, "_timer_states", lambda: states)
    monkeypatch.setattr(guard, "_blockers", lambda _now: ["pending-candidate"])
    monkeypatch.setattr(
        guard,
        "_systemctl",
        lambda *_arguments: (_ for _ in ()).throw(AssertionError("must abstain")),
    )
    monkeypatch.setattr(guard, "_write_status", written.append)

    assert guard.main() == 0
    assert written[0]["status"] == "abstained"
    assert written[0]["repairs"] == []


def test_disabled_steady_state_is_alerted_but_not_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = [_state(unit) for unit in guard.TIMERS]
    states[2] = _state(guard.TIMERS[2], active="inactive", enablement="disabled")
    written: list[dict[str, object]] = []

    monkeypatch.setattr(guard, "_timer_states", lambda: states)
    monkeypatch.setattr(guard, "_blockers", lambda _now: [])
    monkeypatch.setattr(
        guard,
        "_systemctl",
        lambda *_arguments: (_ for _ in ()).throw(AssertionError("must not repair")),
    )
    monkeypatch.setattr(guard, "_write_status", written.append)

    assert guard.main() == 1
    assert written[0]["status"] == "degraded"
    assert written[0]["repairs"] == []
    assert written[0]["problems"] == [
        f"steady-state timer is disabled: {guard.TIMERS[2]}"
    ]


def test_guard_units_are_independent_hardened_and_not_manually_stoppable() -> None:
    service = (
        ROOT / "ops/systemd/palimpsest-continuity-guard.service"
    ).read_text(encoding="utf-8")
    timer = (
        ROOT / "ops/systemd/palimpsest-continuity-guard.timer"
    ).read_text(encoding="utf-8")
    runbook = (
        ROOT / "docs/HETZNER-RAILWAY-CONTINUOUS-PUBLICATION.md"
    ).read_text(encoding="utf-8")

    assert "User=root" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "StateDirectory=palimpsest-continuity" in service
    assert "RefuseManualStop=yes" in timer
    assert "OnUnitActiveSec=2m" in timer
    assert "Persistent=true" in timer
    assert "disable-before-stop/start-before-enable" in runbook
    transaction = (ROOT / "ops/DEPLOY-HETZNER.md").read_text(encoding="utf-8")
    assert "palimpsest-continuity-guard.timer" not in transaction.split(
        "RELEASE_ACTIVATORS=(", 1
    )[1].split(")", 1)[0]
    begin = transaction.index(
        "\ncontinuity_maintenance_begin\n",
        transaction.index("release_quiesce_all() {"),
    )
    target_blob = transaction.index(
        '"${EXPECTED_DEPLOY_SHA}:${CONTINUITY_GUARD_SOURCE}"'
    )
    atomic_install = transaction.index(
        "os.replace(temporary, os.path.basename(destination)"
    )
    capability_proof = transaction.index(
        'test "$(sudo "$CONTINUITY_GUARD" capabilities)"'
    )
    fail_safe_armed = transaction.index("PHASE1_FAIL_SAFE_ARMED=1")
    assert target_blob < atomic_install < capability_proof < begin < fail_safe_armed
    phase1_fail_safe = transaction.split("phase1_fail_safe() {", 1)[1].split(
        "phase1_exit()", 1
    )[0]
    assert phase1_fail_safe.index("continuity_maintenance_fail_closed") < (
        phase1_fail_safe.index("release_quiesce_all")
    )
    phase3_fail_safe = transaction.split("phase3_fail_safe() {", 1)[1].split(
        "phase3_exit()", 1
    )[0]
    assert phase3_fail_safe.index("continuity_maintenance_fail_closed") < (
        phase3_fail_safe.index("release_quiesce_all")
    )
    restored_inventory = transaction.index("ACTIVATOR_RESTORED_PATH=")
    end = transaction.rindex("\ncontinuity_maintenance_end\n")
    finalized = transaction.rindex("\npublish_finalized_receipt\n")
    release_committed = transaction.rindex("\nrelease_finalized=1\n")
    assert restored_inventory < finalized < end < release_committed


def test_hold_recovery_only_reconciles_final_authority_and_exits() -> None:
    transaction = (ROOT / "ops/DEPLOY-HETZNER.md").read_text(encoding="utf-8")
    identity = transaction.split(
        "# Authenticate the exact pre-existing hold", 1
    )[1].split("# Prevent the predecessor", 1)[0]
    recovery = transaction.split(
        "if (( CONTINUITY_HOLD_RECOVERY == 1 )); then\n"
        "  # No publication or release step may be replayed", 1
    )[1].split("\nread_enablement() {", 1)[0]

    assert "maintenance-inspect" in identity
    assert 'fields[1] not in {"active", "fail_closed"}' in identity
    assert 'test "$CONTINUITY_RECOVERY_COMMIT" = "$EXPECTED_DEPLOY_SHA"' in identity
    assert 'test "$CONTINUITY_RECOVERY_REASON" = "$CONTINUITY_REASON_CODE"' in identity
    assert 'read -r RELEASE_RESUME_TOKEN' in identity
    assert 'sudo test ! -e "$CONTINUITY_HOLD_PATH"' in identity
    assert 'sudo test ! -L "$CONTINUITY_HOLD_PATH"' in identity
    assert "maintenance-reconcile-finalized" in recovery
    assert '"${CONTINUITY_EXPECTED_FRAGMENT_ARGUMENTS[@]}"' in recovery
    assert '"${CONTINUITY_EXPECTED_DROPIN_ARGUMENTS[@]}"' in recovery
    assert "continuity_maintenance_begin" not in recovery
    assert "publish_finalized_receipt" not in recovery
    assert "exit 0" in recovery
