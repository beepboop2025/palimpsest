"""Private source workflow and aggregate-only public export tests."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from core.source_workflow import (
    SourceWorkflowError,
    SourceWorkflowStore,
    summarize_source_workflow,
    validate_source_workflow_summary,
)


NOW = "2026-08-12T10:00:00Z"


def _metadata(**updates):
    value = {
        "package_id": "china-economy-evidence-gap",
        "source_id": "source-0123456789abcdef01234567",
        "voice_role": "expert",
        "consent_status": "granted",
        "consent_scope": ["publication"],
        "attribution_mode": "anonymous",
        "verification_status": "verified",
        "safety_review": "reviewed",
        "right_to_reply_status": "not_applicable",
        "created_at": NOW,
        "updated_at": NOW,
    }
    value.update(updates)
    return value


def _age_note(label="one"):
    return (
        "age-encryption.org/v1\n-> X25519 fixture\n"
        f"encrypted-{label}-payload-without-plaintext\n"
    ).encode()


def test_store_accepts_only_encrypted_bytes_and_uses_private_modes(tmp_path):
    store = SourceWorkflowStore(tmp_path / "private")
    receipt = store.ingest(_age_note(), _metadata())

    assert receipt["encrypted_note"]["format"] == "age"
    assert receipt["record_id"].startswith("note-")
    object_path = store.root / receipt["encrypted_note"]["object_path"]
    manifest_path = store.root / "manifests" / f"{receipt['record_id']}.json"
    assert object_path.read_bytes() == _age_note()
    assert os.stat(object_path).st_mode & 0o777 == 0o600
    assert os.stat(manifest_path).st_mode & 0o777 == 0o600

    with pytest.raises(SourceWorkflowError, match="not an age or OpenPGP"):
        store.ingest(b"plaintext interview notes that must never be accepted", _metadata())


def test_metadata_rejects_identity_fields_and_nonpseudonymous_ids(tmp_path):
    store = SourceWorkflowStore(tmp_path / "private")
    unsafe = _metadata()
    unsafe["name"] = "A real person"
    with pytest.raises(SourceWorkflowError, match="unknown=.*name"):
        store.ingest(_age_note(), unsafe)

    with pytest.raises(SourceWorkflowError, match="pseudonymous"):
        store.ingest(_age_note(), _metadata(source_id="Jane Example"))


def test_public_summary_contains_readiness_but_no_identity_or_note_text(tmp_path):
    store = SourceWorkflowStore(tmp_path / "private")
    expert = store.ingest(_age_note("expert"), _metadata())
    affected = store.ingest(
        _age_note("affected"),
        _metadata(
            source_id="source-89abcdef0123456789abcdef",
            voice_role="affected",
            attribution_mode="background",
        ),
    )
    summary = summarize_source_workflow(
        store.records(),
        package_ids=["china-economy-evidence-gap"],
        generated_at=datetime(2026, 8, 12, 11, tzinfo=timezone.utc),
    )

    validate_source_workflow_summary(summary)
    package = summary["packages"][0]
    assert package["readiness"]["expert_voice"] is True
    assert package["readiness"]["affected_voice"] is True
    assert package["n_usable_records"] == 2
    public = json.dumps(summary)
    assert "source-012345" not in public
    assert "anonymous" not in public
    assert "encrypted-expert" not in public
    assert expert["encrypted_note"]["sha256"] not in public
    assert affected["encrypted_note"]["sha256"] not in public


def test_withdrawn_or_unreviewed_records_never_satisfy_voice_gate(tmp_path):
    store = SourceWorkflowStore(tmp_path / "private")
    store.ingest(
        _age_note("withdrawn"),
        _metadata(consent_status="withdrawn", verification_status="pending"),
    )
    summary = summarize_source_workflow(
        store.records(),
        package_ids=["china-economy-evidence-gap"],
        generated_at=datetime(2026, 8, 12, 11, tzinfo=timezone.utc),
    )

    package = summary["packages"][0]
    assert package["voice_counts"]["expert"] == 1
    assert package["verified_voice_counts"]["expert"] == 0
    assert package["readiness"]["expert_voice"] is False
    assert package["readiness"]["all_consented"] is False


def test_institution_reply_must_be_disposed_before_public_readiness(tmp_path):
    store = SourceWorkflowStore(tmp_path / "private")
    store.ingest(
        _age_note("reply"),
        _metadata(
            voice_role="institution_response",
            right_to_reply_status="pending",
        ),
    )
    summary = summarize_source_workflow(
        store.records(),
        package_ids=["china-economy-evidence-gap"],
        generated_at=datetime(2026, 8, 12, 11, tzinfo=timezone.utc),
    )
    assert summary["packages"][0]["readiness"]["right_to_reply_complete"] is False


def test_store_detects_blob_tampering(tmp_path):
    store = SourceWorkflowStore(tmp_path / "private")
    receipt = store.ingest(_age_note(), _metadata())
    path = store.root / receipt["encrypted_note"]["object_path"]
    path.write_bytes(_age_note("tampered"))
    with pytest.raises(SourceWorkflowError, match="integrity"):
        store.records()


def test_store_detects_manifest_metadata_tampering(tmp_path):
    store = SourceWorkflowStore(tmp_path / "private")
    receipt = store.ingest(_age_note(), _metadata())
    path = store.root / "manifests" / f"{receipt['record_id']}.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["voice_role"] = "affected"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SourceWorkflowError, match="does not match"):
        store.records()


def test_public_validator_rejects_identity_broadening(tmp_path):
    summary = summarize_source_workflow(
        [],
        package_ids=["china-economy-evidence-gap"],
        generated_at=datetime(2026, 8, 12, 11, tzinfo=timezone.utc),
    )
    unsafe = deepcopy(summary)
    unsafe["packages"][0]["source_ids"] = ["source-secret"]
    with pytest.raises(SourceWorkflowError, match="unknown=.*source_ids"):
        validate_source_workflow_summary(unsafe)


def test_public_validator_recomputes_readiness_from_aggregate_counts():
    summary = summarize_source_workflow(
        [],
        package_ids=["china-economy-evidence-gap"],
        generated_at=datetime(2026, 8, 12, 11, tzinfo=timezone.utc),
    )
    inflated = deepcopy(summary)
    inflated["packages"][0]["readiness"]["expert_voice"] = True

    with pytest.raises(SourceWorkflowError, match="readiness flags"):
        validate_source_workflow_summary(inflated)
