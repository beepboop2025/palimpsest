from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (
    ROOT
    / "ops"
    / "release-recovery"
    / "2026-08-26-interrupted-phase1-hybrid-recovery.json"
)
VERIFIER = (
    ROOT
    / "ops"
    / "release-recovery"
    / "verify_interrupted_phase1_hybrid_recovery_manifest.py"
)
EXPECTED_MANIFEST_SHA256 = (
    "8ebbec1471a60f6112c521a2783efd3fda1d5c5fea352c087f31f62dd9d153af"
)
CHECKPOINT = "927e0a8b5c82a008f3ffa08a5f5518b8efa8bffd"
PARTIAL_RUNTIME = "15edd4fe13103e68da53c651a15c7c0aa1aed4a3"


def _verify(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _document() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="ascii"))


def _write_canonical(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )


def _load_verifier_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hybrid_manifest_verifier", VERIFIER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewed_hybrid_manifest_passes_strict_verifier() -> None:
    result = _verify(MANIFEST)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == (
        "validated interrupted Phase 1 hybrid manifest: "
        f"{EXPECTED_MANIFEST_SHA256}\n"
    )


def test_manifest_retains_the_full_v2_controller_projection() -> None:
    document = _document()
    boundary = document["observed_safe_boundary"]

    assert set(document) == {
        "authority",
        "continuation",
        "failed_attempt",
        "incident_date",
        "incident_id",
        "observed_safe_boundary",
        "pre_failure_state",
        "recovery_target_constraints",
        "schema_version",
    }
    assert len(boundary["application_containers"]) == 6
    assert len(boundary["dynamic_release_instances"]) == 30
    assert len(boundary["installed_units"]) == 25
    assert len(boundary["installed_bundles"]) == 5
    assert len(boundary["installed_controller_boundary"]["present_files"]) == 6
    assert len(boundary["witness_inventory"]) == 3


def test_manifest_preserves_failed_target_and_hybrid_runtime_roles() -> None:
    document = _document()
    boundary = document["observed_safe_boundary"]
    applications = {
        item["service"]: item for item in boundary["application_containers"]
    }

    assert document["authority"] == {
        "failed_target_commit": CHECKPOINT,
        "prior_checkout_commit": CHECKPOINT,
        "prior_deployed_commit": CHECKPOINT,
    }
    assert boundary["repository"] == {
        "checkout_commit": CHECKPOINT,
        "deployed_commit": CHECKPOINT,
    }
    assert applications["api"]["revision"] == PARTIAL_RUNTIME
    assert applications["migrate"]["revision"] == PARTIAL_RUNTIME
    for service in ("worker", "worker-collectors", "worker-warehouse"):
        assert applications[service]["revision"] == CHECKPOINT
        assert applications[service]["state"] == "exited"


def test_manifest_preserves_the_hybrid_bundle_boundary() -> None:
    bundles = _document()["observed_safe_boundary"]["installed_bundles"]
    by_path = {item["current_symlink_path"]: item for item in bundles}

    assert by_path[
        "/usr/local/libexec/palimpsest-public-osint-sync/current"
    ]["revision"] == CHECKPOINT
    for path in (
        "/usr/local/libexec/palimpsest-analysis/current",
        "/usr/local/libexec/palimpsest-network-lane/current",
        "/usr/local/libexec/palimpsest-common-crawl/current",
        "/usr/local/libexec/palimpsest-node-offsite/current",
    ):
        assert by_path[path]["revision"] == PARTIAL_RUNTIME


def test_manifest_records_927_failure_without_misattributing_15edd_migration() -> None:
    failed = _document()["failed_attempt"]

    assert failed["candidate_freshness_failure"] == {
        "authoritative_bytes_equal": True,
        "candidate_age_seconds": 7409,
        "failure_code": "generation-stale",
        "maximum_age_seconds": 7200,
    }
    assert failed["migration_applied"] is False
    assert failed["migration_exit_code"] == 0
    assert failed["migration_result"]["revision"] == PARTIAL_RUNTIME
    assert failed["migration_result"]["applies_to_failed_target"] is False
    assert failed["release_transaction_id"] is None


def test_manifest_binds_snapshot_and_broker_receipt_evidence() -> None:
    failed = _document()["failed_attempt"]
    snapshot = failed["snapshot_ceiling"]

    assert [item["status"] for item in failed["backup_bridge_receipts"]] == [
        "empty",
        "fenced",
        "empty",
    ]
    assert snapshot["latest_snapshot_id"] == "20260826T110720Z"
    assert snapshot["verification"]["counts"] == {
        "artifact_directories": 2513,
        "artifact_files": 12463,
        "artifact_members": 14976,
        "checksum_entries": 5,
        "snapshot_files": 6,
        "witness_history_records": 2253,
    }
    assert snapshot["verification_receipt"]["sha256"] == (
        "c9446ca105d20e2f8310c8bcc43c87249954fa12ed047c43164f7776cf6e4be3"
    )


def test_manifest_binds_both_archived_attempts_and_no_canonical_receipt() -> None:
    continuation = _document()["continuation"]

    assert [
        item["sha256"] for item in continuation["predecessor_prepared_attempts"]
    ] == [
        "498b4b53679ae5a963752b1684b7399b27471f4958cbcbefccc5f1cd5b622d17",
        "56f687021b54f1fe7acba2dc9cb5e98e4ce857ec0169ac0dff99257630ab8751",
    ]
    assert continuation["predecessor_prepared_receipt"] == (
        continuation["predecessor_prepared_attempts"][-1]
    )
    assert continuation["canonical_prepared_receipt"]["expected_absent"] is True
    assert continuation["predecessor_completion_receipt"]["expected_absent"] is True


def test_verifier_rejects_homogeneous_application_substitution(
    tmp_path: Path,
) -> None:
    document = _document()
    document["observed_safe_boundary"]["application_containers"][0][
        "revision"
    ] = CHECKPOINT
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "required heterogeneous application boundary was normalized" in result.stderr


def test_verifier_rejects_homogeneous_bundle_substitution(tmp_path: Path) -> None:
    document = _document()
    document["observed_safe_boundary"]["installed_bundles"][3][
        "revision"
    ] = PARTIAL_RUNTIME
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "required heterogeneous bundle boundary was normalized" in result.stderr


def test_verifier_rejects_unsupported_runtime_revision(tmp_path: Path) -> None:
    document = _document()
    document["observed_safe_boundary"]["application_containers"][3][
        "revision"
    ] = "f" * 40
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "application container revision is unsupported" in result.stderr


def test_verifier_rejects_container_identifier_drift(tmp_path: Path) -> None:
    document = _document()
    document["observed_safe_boundary"]["application_containers"][3][
        "container_id"
    ] = "0" * 64
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "application container identity drifted" in result.stderr


def test_verifier_rejects_snapshot_drift(tmp_path: Path) -> None:
    document = _document()
    document["failed_attempt"]["snapshot_ceiling"]["verification"]["counts"][
        "artifact_files"
    ] += 1
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "verified checkpoint snapshot projection is invalid" in result.stderr


def test_verifier_rejects_snapshot_receipt_drift(tmp_path: Path) -> None:
    document = _document()
    document["failed_attempt"]["snapshot_ceiling"]["verification_receipt"][
        "sha256"
    ] = "0" * 64
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "verified checkpoint snapshot projection is invalid" in result.stderr


def test_verifier_rejects_broker_receipt_drift(tmp_path: Path) -> None:
    document = _document()
    document["failed_attempt"]["backup_bridge_receipts"][0]["sha256"] = "0" * 64
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "backup bridge receipt bindings are invalid" in result.stderr


def test_verifier_rejects_archived_attempt_drift(tmp_path: Path) -> None:
    document = _document()
    document["continuation"]["predecessor_prepared_attempts"][0][
        "transaction_id"
    ] = "0" * 32
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "archived prepared-attempt bindings are invalid" in result.stderr


def test_verifier_rejects_latest_attempt_projection_drift(tmp_path: Path) -> None:
    document = _document()
    document["continuation"]["predecessor_prepared_receipt"]["sha256"] = "0" * 64
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "latest prepared-attempt projection is invalid" in result.stderr


def test_verifier_rejects_migration_misattribution(tmp_path: Path) -> None:
    document = _document()
    document["failed_attempt"]["migration_applied"] = True
    document["failed_attempt"]["migration_result"][
        "applies_to_failed_target"
    ] = True
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "failed-attempt phase facts are invalid" in result.stderr


def test_verifier_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    raw = MANIFEST.read_text(encoding="ascii")
    marker = (
        '  "incident_id": "2026-08-26-interrupted-phase1-hybrid-recovery",\n'
    )
    candidate = tmp_path / MANIFEST.name
    candidate.write_text(raw.replace(marker, marker + marker, 1), encoding="ascii")

    result = _verify(candidate)

    assert result.returncode == 1
    assert "duplicate JSON key: incident_id" in result.stderr


def test_verifier_rejects_noncanonical_json_bytes(tmp_path: Path) -> None:
    candidate = tmp_path / MANIFEST.name
    candidate.write_text(json.dumps(_document()), encoding="ascii")

    result = _verify(candidate)

    assert result.returncode == 1
    assert "canonical sorted, indented JSON" in result.stderr


def test_verifier_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    document = _document()
    document["operator_override"] = True
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "unknown=['operator_override']" in result.stderr


def test_verifier_rejects_manifest_symlink(tmp_path: Path) -> None:
    candidate = tmp_path / MANIFEST.name
    candidate.symlink_to(MANIFEST)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "cannot open hybrid manifest without following symlinks" in result.stderr


def test_verifier_rejects_manifest_with_multiple_hard_links(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(MANIFEST.read_bytes())
    candidate = tmp_path / MANIFEST.name
    os.link(source, candidate)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "hybrid manifest is not a single-link regular file" in result.stderr


def test_host_continuation_traverses_all_four_prepared_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_verifier_module()
    receipt_calls: list[tuple[str, str]] = []
    absence_calls: list[str] = []

    def record_receipt(
        binding: dict[str, Any],
        *,
        expected_path: str,
        expected_sha256: str,
        expected_authority: dict[str, str],
        label: str,
    ) -> str:
        assert binding["path"] == expected_path
        assert binding["sha256"] == expected_sha256
        assert expected_authority["status"] == "prepared"
        receipt_calls.append((expected_path, expected_sha256))
        return expected_sha256

    def record_absence(path_value: str, expected_path: str, label: str) -> None:
        assert path_value == expected_path
        absence_calls.append(label)

    monkeypatch.setattr(verifier, "_verify_prepared_receipt", record_receipt)
    monkeypatch.setattr(verifier, "_prove_absent", record_absence)

    latest, predecessor = verifier.verify_host_continuation(_document(), ROOT)

    assert latest == verifier.LATEST_ATTEMPT_SHA256
    assert predecessor == verifier.API_PREPARED_SHA256
    assert [path for path, _digest in receipt_calls] == [
        verifier.EARLIER_ATTEMPT_PATH,
        verifier.LATEST_ATTEMPT_PATH,
        verifier.API_PREPARED_PATH,
        verifier.ORIGINAL_PREPARED_PATH,
    ]
    assert absence_calls == [
        "canonical prepared receipt",
        "predecessor completion receipt",
        "API readiness completion receipt",
        "original completion receipt",
    ]


def _prepared_receipt_fixture(
    verifier: ModuleType,
    path: Path,
    *,
    mode: int = 0o400,
    payload: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str], str]:
    authority = payload if payload is not None else {
        "broker_queue_sha256": "1" * 64,
        "compose_environment_sha256": "2" * 64,
        "failed_target_commit": "3" * 40,
        "hybrid_fingerprint_sha256": "4" * 64,
        "incident_id": "test-incident",
        "manifest_sha256": "5" * 64,
        "minimum_recovery_ancestor": "6" * 40,
        "prepared_at": "2026-08-26T11:00:00Z",
        "prior_checkout_commit": "7" * 40,
        "prior_deployed_commit": "8" * 40,
        "recovery_controller_commit": "9" * 40,
        "restore_profile_sha256": "a" * 64,
        "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
        "status": "prepared",
        "target_commit": "b" * 40,
        "transaction_id": "c" * 32,
    }
    raw = verifier._canonical_compact(authority)
    path.write_bytes(raw)
    path.chmod(mode)
    digest = verifier.hashlib.sha256(raw).hexdigest()
    binding = {
        "gid": os.getgid(),
        "link_count": 1,
        "mode": f"{mode:04o}",
        "path": str(path),
        "sha256": digest,
        "uid": os.getuid(),
    }
    return binding, authority, digest


def _verify_fixture_receipt(
    verifier: ModuleType,
    binding: dict[str, Any],
    authority: dict[str, str],
    digest: str,
) -> str:
    return verifier._verify_prepared_receipt(
        binding,
        expected_path=binding["path"],
        expected_sha256=digest,
        expected_authority=authority,
        label="fixture prepared receipt",
    )


def test_prepared_receipt_rejects_wrong_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier_module()
    monkeypatch.setattr(verifier, "_validate_parent_directories", lambda _path: None)
    binding, authority, digest = _prepared_receipt_fixture(
        verifier, tmp_path / "prepared.json", mode=0o600
    )

    with pytest.raises(verifier.ManifestError, match="ownership or mode is invalid"):
        _verify_fixture_receipt(verifier, binding, authority, digest)


def test_prepared_receipt_rejects_wrong_owner_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier_module()
    monkeypatch.setattr(verifier, "_validate_parent_directories", lambda _path: None)
    binding, authority, digest = _prepared_receipt_fixture(
        verifier, tmp_path / "prepared.json"
    )
    binding["uid"] = os.getuid() + 1

    with pytest.raises(verifier.ManifestError, match="ownership or mode is invalid"):
        _verify_fixture_receipt(verifier, binding, authority, digest)


def test_prepared_receipt_rejects_digest_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier_module()
    monkeypatch.setattr(verifier, "_validate_parent_directories", lambda _path: None)
    binding, authority, _digest = _prepared_receipt_fixture(
        verifier, tmp_path / "prepared.json"
    )

    with pytest.raises(verifier.ManifestError, match="SHA-256 is invalid"):
        _verify_fixture_receipt(verifier, binding, authority, "0" * 64)


def test_prepared_receipt_rejects_invalid_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier_module()
    monkeypatch.setattr(verifier, "_validate_parent_directories", lambda _path: None)
    binding, authority, digest = _prepared_receipt_fixture(
        verifier, tmp_path / "prepared.json", payload={}
    )

    with pytest.raises(verifier.ManifestError, match="fields differ"):
        _verify_fixture_receipt(verifier, binding, authority, digest)


def test_prepared_receipt_rejects_multiple_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier_module()
    monkeypatch.setattr(verifier, "_validate_parent_directories", lambda _path: None)
    path = tmp_path / "prepared.json"
    binding, authority, digest = _prepared_receipt_fixture(verifier, path)
    os.link(path, tmp_path / "second-link.json")
    binding["link_count"] = 2

    with pytest.raises(verifier.ManifestError, match="single-link regular file"):
        _verify_fixture_receipt(verifier, binding, authority, digest)


def test_prepared_receipt_rejects_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier_module()
    monkeypatch.setattr(verifier, "_validate_parent_directories", lambda _path: None)
    target = tmp_path / "target.json"
    _binding, authority, digest = _prepared_receipt_fixture(verifier, target)
    path = tmp_path / "prepared.json"
    path.symlink_to(target)
    binding = {
        "gid": os.getgid(),
        "link_count": 1,
        "mode": "0400",
        "path": str(path),
        "sha256": digest,
        "uid": os.getuid(),
    }

    with pytest.raises(verifier.ManifestError, match="without following symlinks"):
        _verify_fixture_receipt(verifier, binding, authority, digest)


@pytest.mark.parametrize("entry_kind", ["file", "symlink"])
@pytest.mark.parametrize(
    "label",
    [
        "canonical prepared receipt",
        "predecessor completion receipt",
        "API readiness completion receipt",
        "original completion receipt",
    ],
)
def test_absence_proof_rejects_existing_file_or_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
    label: str,
) -> None:
    verifier = _load_verifier_module()
    monkeypatch.setattr(verifier, "_validate_parent_directories", lambda _path: None)
    path = tmp_path / "canonical-prepared.json"
    if entry_kind == "file":
        path.write_text("unexpected\n", encoding="utf-8")
    else:
        path.symlink_to(tmp_path / "missing-target")

    with pytest.raises(verifier.ManifestError, match="exists or is a symlink"):
        verifier._prove_absent(str(path), str(path), label)
