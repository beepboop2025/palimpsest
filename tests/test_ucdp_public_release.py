"""Exact public-release tests for the reviewed UCDP aggregate."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from core.ucdp_aggregate import canonical_json_bytes, sha256_bytes
from scripts.verify_ucdp_public_release import (
    AGGREGATE_SCHEMA_PATH,
    ARTIFACT_PATH,
    LOCK_SCHEMA_PATH,
    RECEIPT_PATH,
    RECEIPT_SCHEMA_PATH,
    REGISTRY_PATH,
    REVIEW_LOCK_PATH,
    VERIFIER_PATH,
    UCDPPublicReleaseError,
    build_receipt,
    canonical_receipt_bytes,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
CURRENT_AT = "2026-08-26T19:34:49Z"
PUBLIC_PATHS = (
    ARTIFACT_PATH,
    RECEIPT_PATH,
    REVIEW_LOCK_PATH,
    REGISTRY_PATH,
    AGGREGATE_SCHEMA_PATH,
    LOCK_SCHEMA_PATH,
    RECEIPT_SCHEMA_PATH,
    VERIFIER_PATH,
)


def _release_root(tmp_path: Path) -> Path:
    for relative in PUBLIC_PATHS:
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return tmp_path


def _rehash(document: dict[str, object], identity_key: str) -> None:
    payload = dict(document)
    payload.pop(identity_key, None)
    document[identity_key] = sha256_bytes(canonical_json_bytes(payload))


def _write_rehashed_artifact(root: Path, artifact: dict[str, object]) -> None:
    _rehash(artifact, "bundle_id")
    (root / ARTIFACT_PATH).write_bytes(canonical_json_bytes(artifact))


def test_checked_in_release_receipt_is_exact_closed_and_live() -> None:
    actual = (ROOT / RECEIPT_PATH).read_bytes()
    assert actual == canonical_receipt_bytes(ROOT, current_at=CURRENT_AT)
    receipt = json.loads(actual)
    schema = json.loads((ROOT / RECEIPT_SCHEMA_PATH).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)

    payload = dict(receipt)
    receipt_id = payload.pop("receipt_id")
    assert receipt_id == sha256_bytes(canonical_json_bytes(payload))
    assert receipt["publication_state"] == "live"
    assert receipt["artifact"] == {
        "bundle_id": "58dbd1b3b6ba2aca2c4257622b90ad10b07c5d78385231a75da97b0ef59ba0f2",
        "bytes": 260077,
        "media_type": "application/json",
        "path": ARTIFACT_PATH,
        "sha256": "af1965aa0c02bf58f8c7671b98531bb65338f59eddbd9f81b6c15c1f947258ae",
        "url": "https://palimpsest.info/readings/ucdp-aggregate-latest.json",
    }
    assert receipt["coverage"] == {
        "actor_registry_id_count": 1928,
        "conflict_year_records": 331,
        "country_year_records": 74,
        "end_year": 2025,
        "start_year": 1948,
    }
    assert receipt["private_evidence"] == {
        "file_count": 8,
        "publication_boundary": "aggregate_and_release_receipt_only",
        "served": False,
        "tracked": False,
    }
    assert main(["check", "--root", str(ROOT), "--current-at", CURRENT_AT]) == 0


def test_private_evidence_files_are_not_in_the_public_repository_root() -> None:
    prohibited = {
        "actor_registry.zip",
        "actor_registry.receipt.json",
        "armed_conflict.zip",
        "armed_conflict.receipt.json",
        "organized_country_year.zip",
        "organized_country_year.receipt.json",
        "rights-page.snapshot.html",
        "rights-page.receipt.json",
    }
    repository_files = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert not any(path.split("/")[-1] in prohibited for path in repository_files)


def test_public_release_rejects_expiry_artifact_tamper_and_lock_drift(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path)
    with pytest.raises(UCDPPublicReleaseError, match="rights decision has expired"):
        build_receipt(root, current_at="2026-09-25T19:26:51Z")

    artifact_path = root / ARTIFACT_PATH
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["conflict_years"][0]["side_a"] = "PRIVATE actor name"
    artifact_path.write_bytes(canonical_json_bytes(artifact))
    with pytest.raises(UCDPPublicReleaseError, match="schema validation|record_id"):
        build_receipt(root, current_at=CURRENT_AT)

    shutil.copy2(ROOT / ARTIFACT_PATH, artifact_path)
    lock_path = root / REVIEW_LOCK_PATH
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["inputs"][0]["archive_sha256"] = "0" * 64
    lock_path.write_bytes(canonical_json_bytes(lock))
    with pytest.raises(UCDPPublicReleaseError, match="reviewed lock"):
        build_receipt(root, current_at=CURRENT_AT)


def test_release_receipt_tamper_is_not_accepted(tmp_path: Path) -> None:
    root = _release_root(tmp_path)
    receipt_path = root / RECEIPT_PATH
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact"]["bytes"] -= 1
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(SystemExit) as refused:
        main(["check", "--root", str(root), "--current-at", CURRENT_AT])
    assert refused.value.code == 2


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("conflict_year_records", 1),
        ("country_year_records", 1),
        ("start_year", 2000),
        ("end_year", 2000),
    ),
)
def test_rehashed_derived_coverage_tamper_is_rejected(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    root = _release_root(tmp_path)
    artifact = json.loads((root / ARTIFACT_PATH).read_text(encoding="utf-8"))
    artifact["coverage"][field] = value
    _write_rehashed_artifact(root, artifact)
    with pytest.raises(UCDPPublicReleaseError, match="coverage counts and year bounds"):
        build_receipt(root, current_at=CURRENT_AT)


def test_rehashed_private_derived_actor_coverage_tamper_is_rejected(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path)
    artifact = json.loads((root / ARTIFACT_PATH).read_text(encoding="utf-8"))
    artifact["coverage"]["actor_registry_id_count"] = 1
    artifact["actor_registry_ids_sha256"] = "a" * 64
    _write_rehashed_artifact(root, artifact)
    with pytest.raises(UCDPPublicReleaseError, match="reviewed expected hashes and counts"):
        build_receipt(root, current_at=CURRENT_AT)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    (
        (
            ("latest_retrieved_at",),
            "2026-08-26T19:22:25.906114Z",
            "latest_retrieved_at",
        ),
        (
            ("generated_at",),
            "2026-08-26T19:26:49Z",
            "rights and publication clocks|reviewed rights interval",
        ),
        (
            ("source", "rights_reviewed_at"),
            "2026-08-26T19:26:51Z",
            "exactly bound to the reviewed rights decision",
        ),
        (
            ("source", "rights_observed_at"),
            "2026-08-26T19:22:37Z",
            "exactly bound to the reviewed rights decision",
        ),
        (
            ("source", "rights_valid_until"),
            "2026-09-25T19:26:51Z",
            "exactly bound to the reviewed rights decision",
        ),
    ),
)
def test_rehashed_publication_and_rights_clock_tamper_is_rejected(
    tmp_path: Path,
    path: tuple[str, ...],
    value: str,
    message: str,
) -> None:
    root = _release_root(tmp_path)
    artifact = json.loads((root / ARTIFACT_PATH).read_text(encoding="utf-8"))
    target = artifact
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    _write_rehashed_artifact(root, artifact)
    with pytest.raises(UCDPPublicReleaseError, match=message):
        build_receipt(root, current_at=CURRENT_AT)


def test_rehashed_public_receipt_tamper_is_rejected_by_reviewed_pin(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path)
    artifact = json.loads((root / ARTIFACT_PATH).read_text(encoding="utf-8"))
    actor_receipt = artifact["acquisition_receipts"][1]
    actor_receipt["archive_sha256"] = "a" * 64
    _rehash(actor_receipt, "acquisition_id")
    for row in artifact["conflict_years"]:
        row["actor_registry_acquisition_id"] = actor_receipt["acquisition_id"]
        _rehash(row, "record_id")
    artifact["conflict_years_sha256"] = sha256_bytes(
        canonical_json_bytes(artifact["conflict_years"])
    )
    _write_rehashed_artifact(root, artifact)
    with pytest.raises(UCDPPublicReleaseError, match="reviewed lock and registry"):
        build_receipt(root, current_at=CURRENT_AT)


def test_rehashed_fatality_projection_tamper_is_rejected_by_reviewed_hash(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path)
    artifact = json.loads((root / ARTIFACT_PATH).read_text(encoding="utf-8"))
    row = artifact["country_years"][0]
    for bound in ("low", "best", "high"):
        row["state_based"][bound] += 1
        row["total"][bound] += 1
    row["source_row_sha256"] = "a" * 64
    _rehash(row, "record_id")
    artifact["country_years_sha256"] = sha256_bytes(
        canonical_json_bytes(artifact["country_years"])
    )
    _write_rehashed_artifact(root, artifact)
    with pytest.raises(UCDPPublicReleaseError, match="reviewed expected hashes and counts"):
        build_receipt(root, current_at=CURRENT_AT)


def test_rehashed_public_receipt_reordering_is_rejected(tmp_path: Path) -> None:
    root = _release_root(tmp_path)
    artifact = json.loads((root / ARTIFACT_PATH).read_text(encoding="utf-8"))
    artifact["acquisition_receipts"][0], artifact["acquisition_receipts"][1] = (
        artifact["acquisition_receipts"][1],
        artifact["acquisition_receipts"][0],
    )
    _write_rehashed_artifact(root, artifact)
    with pytest.raises(UCDPPublicReleaseError, match="receipt order"):
        build_receipt(root, current_at=CURRENT_AT)
