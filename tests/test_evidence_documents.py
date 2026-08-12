"""Adversarial contract tests for the private EvidenceDocument v1 lane."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

import core.evidence_documents as evidence_documents
from core.evidence_documents import (
    ACCEPTANCE_RECEIPT_SPEC_VERSION,
    CAPTURE_REQUEST_SPEC_VERSION,
    CANONICALIZATION,
    CUT_SPEC_VERSION,
    RIGHTS_DECISION_SPEC_VERSION,
    RIGHTS_LEDGER_SPEC_VERSION,
    SPEC_VERSION,
    TEXT_TRAINING_POLICY_ID,
    AcceptanceClockError,
    DurabilityError,
    EvidenceDocumentError,
    EvidenceDocumentStore,
    HardLinkUnsupportedError,
    IntegrityError,
    RightsConflictError,
    StoreSafetyError,
    TrainingCut,
    TrainingPolicyError,
    canonical_json_bytes,
    capture_request_sha256,
    empty_rights_ledger,
    make_rights_decision_entry,
    rights_ledger_sha256,
    strict_json_loads,
    validate_acceptance_receipt,
    validate_manifest,
    validate_rights_decision,
    validate_rights_ledger,
    validate_training_cut,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "protocol" / "evidence-document-v1.schema.json"
DEFAULT_AS_OF = "2026-08-12T09:00:00Z"


@pytest.fixture(autouse=True)
def deterministic_test_acceptance_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests inject a trusted deterministic clock equal to source collection."""

    monkeypatch.setattr(
        evidence_documents,
        "_system_acceptance_clock",
        lambda request: request["metadata"]["collected_at"],
    )


def metadata(
    *,
    source_id: str = "example-wire",
    canonical_url: str | None = None,
    training_use: str = "full_text",
    event_time: str | None = "2026-08-12T08:00:00Z",
    publication_time: str | None = "2026-08-12T08:05:00Z",
    knowledge_time: str = "2026-08-12T08:05:00Z",
    collected_at: str = "2026-08-12T08:06:00Z",
    run_id: str = "wire-20260812T080600Z",
    parent_feed_sha256: str | None = None,
    media_type: str = "text/plain",
    language: str = "en",
) -> dict[str, Any]:
    return {
        "source": {
            "id": source_id,
            "canonical_url": canonical_url
            or f"https://example.org/evidence/{source_id}",
        },
        "media_type": media_type,
        "language": language,
        "event_time": event_time,
        "publication_time": publication_time,
        "knowledge_time": knowledge_time,
        "collected_at": collected_at,
        "collection": {
            "run_id": run_id,
            "parent_feed_sha256": parent_feed_sha256,
        },
        "retention_class": "standard",
        "rights": {
            "training_use": training_use,
            "license_or_terms_ref": "https://example.org/terms-at-collection",
        },
    }


def decision_entry(
    stored: evidence_documents.StoredEvidenceDocument,
    *,
    training_use: str = "full_text",
    decision_type: str = "policy_set",
    effective_at: str = "2026-08-12T08:05:00Z",
    knowledge_time: str = "2026-08-12T08:05:00Z",
    supersedes: tuple[str, ...] = (),
    source_id: str | None = None,
    content_sha256: str | None = None,
    reason: str = "Bounded caller-supplied rights review.",
) -> dict[str, Any]:
    return make_rights_decision_entry(
        {
            "spec_version": RIGHTS_DECISION_SPEC_VERSION,
            "canonicalization": CANONICALIZATION,
            "subject": {
                "source_id": source_id or stored.manifest["source"]["id"],
                "content_sha256": content_sha256 or stored.content_sha256,
            },
            "decision_type": decision_type,
            "training_use": training_use,
            "effective_at": effective_at,
            "knowledge_time": knowledge_time,
            "license_or_terms_ref": "https://example.org/reviewed-terms",
            "reason": reason,
            "supersedes": list(supersedes),
        }
    )


def decision_entry_for_subject(
    source_id: str,
    content_sha256: str,
    *,
    training_use: str = "full_text",
) -> dict[str, Any]:
    return make_rights_decision_entry(
        {
            "spec_version": RIGHTS_DECISION_SPEC_VERSION,
            "canonicalization": CANONICALIZATION,
            "subject": {
                "source_id": source_id,
                "content_sha256": content_sha256,
            },
            "decision_type": "policy_set",
            "training_use": training_use,
            "effective_at": "2026-08-12T08:05:00Z",
            "knowledge_time": "2026-08-12T08:05:00Z",
            "license_or_terms_ref": "https://example.org/reviewed-terms",
            "reason": "Precomputed subject decision for transaction testing.",
            "supersedes": [],
        }
    )


def ledger(*entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "spec_version": RIGHTS_LEDGER_SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "decisions": list(entries),
    }


def build_cut(
    store: EvidenceDocumentStore,
    as_of: str = DEFAULT_AS_OF,
    *,
    rights_ledger: dict[str, Any] | None = None,
    policy: str = TEXT_TRAINING_POLICY_ID,
) -> TrainingCut:
    complete = empty_rights_ledger() if rights_ledger is None else rights_ledger
    return store.build_training_cut(
        as_of,
        rights_ledger=complete,
        trusted_rights_ledger_sha256=rights_ledger_sha256(complete),
        policy=policy,
    )


def manifest_document(
    raw: bytes,
    value: dict[str, Any] | None = None,
    *,
    accepted_at: str | None = None,
) -> dict[str, Any]:
    meta = metadata() if value is None else deepcopy(value)
    content = {"sha256": hashlib.sha256(raw).hexdigest(), "byte_size": len(raw)}
    request = {
        "spec_version": CAPTURE_REQUEST_SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "metadata": deepcopy(meta),
        "content": content,
    }
    return {
        "spec_version": SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        **meta,
        "content": content,
        "acceptance": {
            "accepted_at": accepted_at or meta["collected_at"],
            "capture_request_sha256": capture_request_sha256(request),
            "receipt_sha256": "0" * 64,
        },
    }


def trace_content_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Path]:
    reads: list[Path] = []
    original = evidence_documents._read_regular_file

    def traced(path: Path, *, maximum_bytes: int, purpose: str) -> bytes:
        if path.suffix == ".bin":
            reads.append(path)
        return original(path, maximum_bytes=maximum_bytes, purpose=purpose)

    monkeypatch.setattr(evidence_documents, "_read_regular_file", traced)
    return reads


def make_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def test_exact_repeat_is_idempotent_private_durable_and_has_no_latest(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    raw = b"exact evidence bytes\n"

    first = store.ingest(raw, metadata())
    second = store.ingest(raw, metadata())

    assert first.manifest_sha256 == second.manifest_sha256
    assert first.content_sha256 == second.content_sha256
    assert first.content_created and first.manifest_created
    assert not second.content_created and not second.manifest_created
    assert store.read_content(first.manifest_sha256) == raw
    assert canonical_json_bytes(first.manifest) == first.manifest_path.read_bytes()
    assert hashlib.sha256(first.manifest_path.read_bytes()).hexdigest() == (
        first.manifest_sha256
    )
    assert len(list(store.objects_root.rglob("*.bin"))) == 1
    assert len(list(store.manifests_root.rglob("*.json"))) == 1
    assert not list(store.root.rglob("*latest*"))
    assert sorted(path.name for path in store.staging_root.iterdir()) == [
        ".recovery.lock"
    ]
    for path in (
        store.root,
        store.staging_root,
        store.objects_root,
        store.receipts_root,
        store.manifests_root,
        first.receipt_path.parent.parent,
        first.receipt_path.parent,
        first.content_path.parent,
        first.manifest_path.parent,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert path.stat().st_uid == os.geteuid()
    for path in (
        store.staging_root / ".recovery.lock",
        first.receipt_path,
        first.content_path,
        first.manifest_path,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_uid == os.geteuid()
        assert path.stat().st_nlink == 1


def test_source_mutations_and_metadata_revisions_preserve_versions(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    first = store.ingest(b"version one\n", metadata())
    second = store.ingest(b"version two\n", metadata())
    recollected = store.ingest(
        b"version two\n",
        metadata(
            collected_at="2026-08-12T08:07:00Z",
            run_id="wire-20260812T080700Z",
        ),
    )

    assert (
        len(
            {first.manifest_sha256, second.manifest_sha256, recollected.manifest_sha256}
        )
        == 3
    )
    assert first.content_sha256 != second.content_sha256
    assert recollected.content_sha256 == second.content_sha256
    assert not recollected.content_created and recollected.manifest_created
    assert store.read_content(first.manifest_sha256) == b"version one\n"
    assert store.read_content(second.manifest_sha256) == b"version two\n"
    assert len(list(store.objects_root.rglob("*.bin"))) == 2
    assert len(list(store.manifests_root.rglob("*.json"))) == 3


def test_cut_requires_explicit_authoritative_ledger_and_denials_never_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    stored: dict[str, evidence_documents.StoredEvidenceDocument] = {}
    entries: list[dict[str, Any]] = []
    for training_use in ("prohibited", "metadata_only", "derived_only", "full_text"):
        source = f"source-{training_use.replace('_', '-')}"
        item = store.ingest(
            f"secret-{training_use}\n".encode(),
            # Manifest declarations are provenance only, so make every one look
            # permissive and let the external decision ledger control access.
            metadata(
                source_id=source, training_use="full_text", run_id=f"run-{source}"
            ),
        )
        stored[training_use] = item
        entries.append(decision_entry(item, training_use=training_use))

    reads = trace_content_reads(monkeypatch)
    with pytest.raises(TypeError):
        store.build_training_cut(DEFAULT_AS_OF)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        store.build_training_cut(
            DEFAULT_AS_OF, rights_ledger=empty_rights_ledger()
        )  # type: ignore[call-arg]
    with pytest.raises(TrainingPolicyError, match="explicit complete"):
        store.build_training_cut(
            DEFAULT_AS_OF,
            rights_ledger=None,  # type: ignore[arg-type]
            trusted_rights_ledger_sha256=rights_ledger_sha256(empty_rights_ledger()),
        )

    empty = build_cut(store, DEFAULT_AS_OF, rights_ledger=None)
    assert empty.records == ()
    assert reads == []

    cut = build_cut(store, DEFAULT_AS_OF, rights_ledger=ledger(*entries))
    assert [record["source"]["id"] for record in cut.records] == ["source-full-text"]
    assert [record["content"]["text"] for record in cut.records] == [
        "secret-full_text\n"
    ]
    assert reads == [stored["full_text"].content_path]
    for denied in ("prohibited", "metadata_only", "derived_only"):
        assert f"secret-{denied}".encode() not in cut.canonical_bytes
    assert cut.policy["rights_actions"] == {
        "prohibited": "exclude",
        "metadata_only": "exclude",
        "derived_only": "exclude",
        "full_text": "include",
    }


def test_default_cut_gates_both_knowledge_and_collection_before_content_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    exact = store.ingest(
        b"available exactly at cutoff\n",
        metadata(
            source_id="exact",
            publication_time="2026-08-12T09:59:59Z",
            knowledge_time="2026-08-12T10:00:00Z",
            collected_at="2026-08-12T10:00:00Z",
            run_id="exact-run",
        ),
    )
    backfill = store.ingest(
        b"collected after cutoff\n",
        metadata(
            source_id="backfill",
            publication_time="2026-08-12T09:00:00Z",
            knowledge_time="2026-08-12T09:00:00Z",
            collected_at="2026-08-12T13:00:00Z",
            run_id="backfill-run",
        ),
    )
    future_knowledge = store.ingest(
        b"known after cutoff\n",
        metadata(
            source_id="future",
            publication_time="2026-08-12T12:00:00Z",
            knowledge_time="2026-08-12T12:00:00Z",
            collected_at="2026-08-12T13:01:00Z",
            run_id="future-run",
        ),
    )
    rights = ledger(
        decision_entry(
            exact,
            effective_at="2026-08-12T09:00:00Z",
            knowledge_time="2026-08-12T09:00:00Z",
        ),
        decision_entry(
            backfill,
            effective_at="2026-08-12T09:00:00Z",
            knowledge_time="2026-08-12T09:00:00Z",
        ),
        decision_entry(
            future_knowledge,
            effective_at="2026-08-12T09:00:00Z",
            knowledge_time="2026-08-12T09:00:00Z",
        ),
    )
    reads = trace_content_reads(monkeypatch)

    cut = build_cut(store, "2026-08-12T10:00:00Z", rights_ledger=rights)

    assert [record["source"]["id"] for record in cut.records] == ["exact"]
    assert reads == [exact.content_path]
    assert backfill.content_path not in reads
    assert future_knowledge.content_path not in reads
    assert cut.policy["temporal_cutoff"] == {
        "manifest_clocks": [
            "knowledge_time",
            "collected_at",
            "acceptance.accepted_at",
        ],
        "comparison": "each_lte_as_of",
        "trusted_store_clock": "acceptance.accepted_at",
        "source_metadata_clocks": ["knowledge_time", "collected_at"],
    }


def test_scheduled_future_event_is_valid_but_availability_clock_reversals_fail(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    scheduled = store.ingest(
        b"scheduled event\n",
        metadata(event_time="2030-01-01T00:00:00Z"),
    )
    cut = build_cut(
        store, DEFAULT_AS_OF, rights_ledger=ledger(decision_entry(scheduled))
    )
    assert cut.records[0]["provenance"][0]["event_time"] == ("2030-01-01T00:00:00Z")

    with pytest.raises(EvidenceDocumentError, match="publication_time cannot follow"):
        store.ingest(
            b"bad publication clock\n",
            metadata(
                publication_time="2026-08-12T08:10:00Z",
                knowledge_time="2026-08-12T08:05:00Z",
            ),
        )
    with pytest.raises(EvidenceDocumentError, match="knowledge_time cannot follow"):
        store.ingest(
            b"bad collection clock\n",
            metadata(
                knowledge_time="2026-08-12T08:10:00Z",
                collected_at="2026-08-12T08:09:59Z",
                publication_time=None,
            ),
        )


def test_acceptance_receipt_is_create_once_idempotent_and_is_the_trust_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def one_shot_clock(request: dict[str, Any]) -> str:
        calls.append(request["metadata"]["source"]["id"])
        if len(calls) > 1:
            raise AssertionError("exact retry must reuse its create-once receipt")
        return "2026-08-12T08:10:00Z"

    store = EvidenceDocumentStore(
        tmp_path / "private-evidence", acceptance_clock=one_shot_clock
    )
    raw = b"receipt-bound evidence\n"
    first = store.ingest(raw, metadata(source_id="receipt-bound"))
    second = store.ingest(raw, metadata(source_id="receipt-bound"))

    assert calls == ["receipt-bound"]
    assert first.accepted_at == second.accepted_at == "2026-08-12T08:10:00Z"
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.receipt_created and not second.receipt_created
    assert first.receipt_path == second.receipt_path
    assert first.receipt_path.stem == first.receipt_sha256
    assert first.manifest["acceptance"] == {
        "accepted_at": first.accepted_at,
        "capture_request_sha256": first.receipt_path.parent.name,
        "receipt_sha256": first.receipt_sha256,
    }

    rights = ledger(decision_entry(first))
    reads = trace_content_reads(monkeypatch)
    before_acceptance = build_cut(store, "2026-08-12T08:09:59Z", rights_ledger=rights)
    assert before_acceptance.records == ()
    assert reads == []
    at_acceptance = build_cut(store, "2026-08-12T08:10:00Z", rights_ledger=rights)
    assert len(at_acceptance.records) == 1
    assert at_acceptance.records[0]["provenance"][0]["acceptance"] == (
        first.manifest["acceptance"]
    )


def test_acceptance_clock_rollback_predating_collection_and_receipt_collision_fail(
    tmp_path: Path,
) -> None:
    values = iter(["2026-08-12T08:20:00Z", "2026-08-12T08:10:00Z"])
    store = EvidenceDocumentStore(
        tmp_path / "rollback-store", acceptance_clock=lambda request: next(values)
    )
    first = store.ingest(b"first receipt\n", metadata(source_id="first-receipt"))
    with pytest.raises(AcceptanceClockError, match="moved backwards"):
        store.ingest(
            b"second receipt\n",
            metadata(
                source_id="second-receipt",
                collected_at="2026-08-12T08:07:00Z",
                run_id="second-receipt-run",
            ),
        )
    assert len(list(store.manifests_root.rglob("*.json"))) == 1

    too_early = EvidenceDocumentStore(
        tmp_path / "early-store",
        acceptance_clock=lambda request: "2026-08-12T08:05:59Z",
    )
    with pytest.raises(AcceptanceClockError, match="cannot precede"):
        too_early.ingest(b"too early\n", metadata())
    assert not list(too_early.receipts_root.rglob("*.json"))

    receipt = strict_json_loads(
        first.receipt_path.read_bytes(),
        maximum_bytes=evidence_documents.MAX_ACCEPTANCE_RECEIPT_BYTES,
        purpose="test receipt",
    )
    receipt["accepted_at"] = "2026-08-12T08:21:00Z"
    first.receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(IntegrityError, match="filename/content mismatch"):
        store.ingest(b"first receipt\n", metadata(source_id="first-receipt"))

    first.receipt_path.write_bytes(
        canonical_json_bytes(
            {
                **receipt,
                "accepted_at": first.accepted_at,
            }
        )
    )
    collision = first.receipt_path.with_name(f"{'f' * 64}.json")
    collision.write_bytes(first.receipt_path.read_bytes())
    collision.chmod(0o600)
    with pytest.raises(IntegrityError, match="colliding create-once receipts"):
        store.ingest(b"first receipt\n", metadata(source_id="first-receipt"))


def test_complete_rights_ledger_must_match_out_of_band_trusted_head(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    stored = store.ingest(b"head-bound evidence\n", metadata())
    grant = decision_entry(stored)
    future_revocation = decision_entry(
        stored,
        training_use="prohibited",
        decision_type="revocation",
        effective_at="2026-08-13T00:00:00Z",
        knowledge_time="2026-08-13T00:00:00Z",
        supersedes=(grant["decision_sha256"],),
    )
    complete = ledger(grant, future_revocation)
    trusted_head = rights_ledger_sha256(complete)

    with pytest.raises(IntegrityError, match="trusted out-of-band head"):
        store.build_training_cut(
            DEFAULT_AS_OF,
            rights_ledger=ledger(grant),
            trusted_rights_ledger_sha256=trusted_head,
        )
    with pytest.raises(IntegrityError, match="trusted out-of-band head"):
        store.build_training_cut(
            DEFAULT_AS_OF,
            rights_ledger=complete,
            trusted_rights_ledger_sha256="0" * 64,
        )
    cut = store.build_training_cut(
        DEFAULT_AS_OF,
        rights_ledger=complete,
        trusted_rights_ledger_sha256=trusted_head,
    )
    assert len(cut.records) == 1
    assert len(cut.rights_ledger["decisions"]) == 1


def test_explicit_revocation_is_immutable_visible_and_effective_at_cut_time(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    stored = store.ingest(b"revocable evidence\n", metadata())
    manifest_before = stored.manifest_path.read_bytes()
    grant = decision_entry(stored)
    revocation = decision_entry(
        stored,
        training_use="prohibited",
        decision_type="revocation",
        effective_at="2026-08-12T10:00:00Z",
        knowledge_time="2026-08-12T09:00:00Z",
        supersedes=(grant["decision_sha256"],),
        reason="The prior full-text permission was revoked.",
    )
    complete = ledger(revocation, grant)

    before_effect = build_cut(store, "2026-08-12T09:30:00Z", rights_ledger=complete)
    assert len(before_effect.records) == 1
    assert [
        entry["decision_sha256"] for entry in before_effect.rights_ledger["decisions"]
    ] == sorted(
        [grant["decision_sha256"], revocation["decision_sha256"]],
        key=lambda digest: next(
            (
                entry["decision"]["knowledge_time"],
                entry["decision"]["effective_at"],
                digest,
            )
            for entry in (grant, revocation)
            if entry["decision_sha256"] == digest
        ),
    )
    assert before_effect.records[0]["effective_rights"]["basis"][
        "decision_sha256s"
    ] == [grant["decision_sha256"]]

    after_effect = build_cut(store, "2026-08-12T10:00:00Z", rights_ledger=complete)
    assert after_effect.records == ()
    assert stored.manifest_path.read_bytes() == manifest_before
    assert hashlib.sha256(manifest_before).hexdigest() == stored.manifest_sha256


def test_future_knowledge_cannot_rewrite_historical_cut_identity(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    stored = store.ingest(b"historical evidence\n", metadata())
    grant = decision_entry(stored)
    later_known_retroactive_revocation = decision_entry(
        stored,
        training_use="prohibited",
        decision_type="revocation",
        effective_at="2026-08-12T08:30:00Z",
        knowledge_time="2026-08-12T12:00:00Z",
        supersedes=(grant["decision_sha256"],),
    )

    historical_without_future = build_cut(
        store, "2026-08-12T10:00:00Z", rights_ledger=ledger(grant)
    )
    historical_with_future = build_cut(
        store,
        "2026-08-12T10:00:00Z",
        rights_ledger=ledger(later_known_retroactive_revocation, grant),
    )
    assert (
        historical_with_future.canonical_bytes
        == historical_without_future.canonical_bytes
    )
    assert historical_with_future.cut_sha256 == historical_without_future.cut_sha256
    assert len(historical_with_future.records) == 1

    after_knowledge = build_cut(
        store,
        "2026-08-12T12:00:00Z",
        rights_ledger=ledger(grant, later_known_retroactive_revocation),
    )
    assert after_knowledge.records == ()
    assert len(after_knowledge.rights_ledger["decisions"]) == 2


def test_unresolved_rights_terminals_fail_closed_until_explicitly_superseded(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    stored = store.ingest(b"conflicted evidence\n", metadata())
    first = decision_entry(stored, reason="First independent review.")
    second = decision_entry(
        stored,
        training_use="metadata_only",
        reason="Second independent review.",
    )
    with pytest.raises(RightsConflictError, match="unresolved terminal"):
        build_cut(store, DEFAULT_AS_OF, rights_ledger=ledger(second, first))

    resolution = decision_entry(
        stored,
        training_use="full_text",
        knowledge_time="2026-08-12T08:30:00Z",
        supersedes=(first["decision_sha256"], second["decision_sha256"]),
        reason="Conflict adjudicated explicitly.",
    )
    cut = build_cut(
        store, DEFAULT_AS_OF, rights_ledger=ledger(second, resolution, first)
    )
    assert len(cut.records) == 1
    assert cut.records[0]["effective_rights"]["basis"]["decision_sha256s"] == [
        resolution["decision_sha256"]
    ]


def test_rights_ledger_hash_graph_subject_and_revocation_validation(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    stored = store.ingest(b"ledger validation\n", metadata())
    grant = decision_entry(stored, knowledge_time="2026-08-12T08:30:00Z")

    mismatched = deepcopy(grant)
    mismatched["decision_sha256"] = "0" * 64
    with pytest.raises(IntegrityError, match="does not match"):
        validate_rights_ledger(ledger(mismatched))

    unknown = decision_entry(stored, supersedes=("0" * 64,))
    with pytest.raises(EvidenceDocumentError, match="unknown decision"):
        validate_rights_ledger(ledger(unknown))

    cross_subject = decision_entry(
        stored,
        source_id="other-source",
        supersedes=(grant["decision_sha256"],),
    )
    with pytest.raises(EvidenceDocumentError, match="different subject"):
        validate_rights_ledger(ledger(grant, cross_subject))

    backwards = decision_entry(
        stored,
        knowledge_time="2026-08-12T08:00:00Z",
        supersedes=(grant["decision_sha256"],),
    )
    with pytest.raises(EvidenceDocumentError, match="cannot precede"):
        validate_rights_ledger(ledger(grant, backwards))

    invalid_revocation = deepcopy(grant["decision"])
    invalid_revocation["decision_type"] = "revocation"
    with pytest.raises(EvidenceDocumentError, match="must set training_use"):
        validate_rights_decision(invalid_revocation)


def test_same_source_content_is_deduplicated_with_all_provenance_and_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    raw = b"same immutable content\n"
    first = store.ingest(
        raw,
        metadata(
            source_id="dedupe-source",
            canonical_url="https://example.org/evidence/first",
            run_id="first-run",
            training_use="prohibited",
        ),
    )
    second = store.ingest(
        raw,
        metadata(
            source_id="dedupe-source",
            canonical_url="https://example.org/evidence/second",
            collected_at="2026-08-12T08:07:00Z",
            run_id="second-run",
            training_use="metadata_only",
        ),
    )
    reads = trace_content_reads(monkeypatch)

    cut = build_cut(store, DEFAULT_AS_OF, rights_ledger=ledger(decision_entry(first)))

    assert len(cut.records) == 1
    record = cut.records[0]
    assert record["manifest_sha256s"] == sorted(
        [first.manifest_sha256, second.manifest_sha256]
    )
    assert [item["manifest_sha256"] for item in record["provenance"]] == record[
        "manifest_sha256s"
    ]
    assert {item["collection"]["run_id"] for item in record["provenance"]} == {
        "first-run",
        "second-run",
    }
    assert {
        item["declared_rights"]["training_use"] for item in record["provenance"]
    } == {
        "prohibited",
        "metadata_only",
    }
    assert (
        record["effective_rights"]["basis"]["manifest_sha256s"]
        == record["manifest_sha256s"]
    )
    assert reads == [first.content_path]

    other_source = store.ingest(
        raw,
        metadata(
            source_id="other-source",
            collected_at="2026-08-12T08:08:00Z",
            run_id="other-run",
        ),
    )
    expanded = build_cut(
        store,
        DEFAULT_AS_OF,
        rights_ledger=ledger(decision_entry(first), decision_entry(other_source)),
    )
    assert [record["source"]["id"] for record in expanded.records] == [
        "dedupe-source",
        "other-source",
    ]


def test_cut_is_byte_deterministic_across_ingest_ledger_and_enumeration_order(
    tmp_path: Path,
) -> None:
    rows = [
        (b"zeta\n", metadata(source_id="zeta", run_id="run-zeta")),
        (b"alpha\n", metadata(source_id="alpha", run_id="run-alpha")),
        (b"beta\n", metadata(source_id="beta", run_id="run-beta")),
    ]
    left = EvidenceDocumentStore(tmp_path / "left")
    right = EvidenceDocumentStore(tmp_path / "right")
    left_items = [left.ingest(raw, value) for raw, value in rows]
    right_items = [right.ingest(raw, value) for raw, value in reversed(rows)]
    left_entries = [decision_entry(item) for item in left_items]
    right_entries = [decision_entry(item) for item in right_items]

    left_cut = build_cut(
        left, DEFAULT_AS_OF, rights_ledger=ledger(*reversed(left_entries))
    )
    right_cut = build_cut(right, DEFAULT_AS_OF, rights_ledger=ledger(*right_entries))
    assert left_cut.canonical_bytes == right_cut.canonical_bytes
    assert left_cut.cut_sha256 == right_cut.cut_sha256
    assert left_cut.cut_sha256 == hashlib.sha256(left_cut.canonical_bytes).hexdigest()
    assert [row["source"]["id"] for row in left_cut.records] == [
        "alpha",
        "beta",
        "zeta",
    ]

    later_cut = build_cut(
        left, "2026-08-12T09:00:01Z", rights_ledger=ledger(*left_entries)
    )
    assert later_cut.records == left_cut.records
    assert later_cut.cut_sha256 != left_cut.cut_sha256
    with pytest.raises(TrainingPolicyError, match="unsupported fail-closed"):
        build_cut(
            left,
            DEFAULT_AS_OF,
            rights_ledger=ledger(*left_entries),
            policy="allow-everything",
        )


def test_training_cut_nested_access_is_detached_and_digest_stays_immutable(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    stored = store.ingest(b"immutable cut\n", metadata())
    cut = build_cut(store, DEFAULT_AS_OF, rights_ledger=ledger(decision_entry(stored)))
    original_bytes = cut.canonical_bytes
    original_digest = cut.cut_sha256

    policy = cut.policy
    policy["rights_actions"]["full_text"] = "exclude"
    rights = cut.rights_ledger
    rights["decisions"][0]["decision"]["training_use"] = "prohibited"
    records = cut.records
    records[0]["content"]["text"] = "mutated"
    document = cut.to_dict()
    document["records"].clear()

    assert cut.policy["rights_actions"]["full_text"] == "include"
    assert cut.rights_ledger["decisions"][0]["decision"]["training_use"] == (
        "full_text"
    )
    assert cut.records[0]["content"]["text"] == "immutable cut\n"
    assert cut.canonical_bytes == original_bytes
    assert cut.cut_sha256 == original_digest
    with pytest.raises((AttributeError, TypeError)):
        cut.as_of = "2030-01-01T00:00:00Z"  # type: ignore[misc]


def test_direct_training_cut_construction_rejects_consistent_forgeries(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    stored = store.ingest(b"validated cut constructor\n", metadata())
    grant = decision_entry(stored)
    complete = ledger(grant)
    trusted_head = rights_ledger_sha256(complete)
    valid = build_cut(store, rights_ledger=complete)
    document = valid.to_dict()

    with pytest.raises(TrainingPolicyError, match="direct TrainingCut construction"):
        TrainingCut(
            cut_sha256=valid.cut_sha256,
            as_of=valid.as_of,
            canonical_bytes=valid.canonical_bytes,
        )

    def verify(payload: bytes, *, digest: str, as_of: str = valid.as_of) -> None:
        validate_training_cut(
            cut_sha256=digest,
            as_of=as_of,
            canonical_bytes=payload,
            complete_rights_ledger=complete,
            trusted_rights_ledger_sha256=trusted_head,
        )

    with pytest.raises(IntegrityError, match="do not match cut_sha256"):
        verify(valid.canonical_bytes, digest="0" * 64)
    with pytest.raises(IntegrityError, match="document as_of"):
        verify(
            valid.canonical_bytes,
            digest=valid.cut_sha256,
            as_of="2026-08-12T09:00:01Z",
        )

    forged_documents: list[tuple[dict[str, Any], str]] = []
    wrong_schema = deepcopy(document)
    wrong_schema["spec_version"] = "palimpsest-evidence-training-cut/v999"
    forged_documents.append((wrong_schema, "spec_version"))
    wrong_policy = deepcopy(document)
    wrong_policy["policy"]["rights_actions"]["full_text"] = "exclude"
    forged_documents.append((wrong_policy, "policy"))
    wrong_rights = deepcopy(document)
    wrong_rights["records"][0]["effective_rights"]["basis"]["decision_sha256s"] = [
        "0" * 64
    ]
    forged_documents.append((wrong_rights, "effective_rights"))
    wrong_record = deepcopy(document)
    wrong_record["records"][0]["content"]["text"] = "forged text"
    forged_documents.append((wrong_record, "content identities"))

    for forged, message in forged_documents:
        forged_bytes = canonical_json_bytes(forged)
        with pytest.raises(EvidenceDocumentError, match=message):
            verify(
                forged_bytes,
                digest=hashlib.sha256(forged_bytes).hexdigest(),
            )

    noncanonical = json.dumps(document, ensure_ascii=False).encode("utf-8")
    assert noncanonical != valid.canonical_bytes
    with pytest.raises(IntegrityError, match="not canonical JSON"):
        verify(
            noncanonical,
            digest=hashlib.sha256(noncanonical).hexdigest(),
        )

    revocation = decision_entry(
        stored,
        training_use="prohibited",
        decision_type="revocation",
        knowledge_time="2026-08-12T08:30:00Z",
        effective_at="2026-08-12T08:30:00Z",
        supersedes=(grant["decision_sha256"],),
    )
    authoritative = ledger(grant, revocation)
    # The grant-only bytes and their top-level hash are internally consistent,
    # but omitting the visible revocation must fail against the trusted head.
    with pytest.raises(IntegrityError, match="rights projection"):
        validate_training_cut(
            cut_sha256=valid.cut_sha256,
            as_of=valid.as_of,
            canonical_bytes=valid.canonical_bytes,
            complete_rights_ledger=authoritative,
            trusted_rights_ledger_sha256=rights_ledger_sha256(authoritative),
        )


def test_media_policy_exact_prefix_suffix_and_utf8_rules_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    media = {
        "plain": "text/plain",
        "json": "application/json",
        "suffix": "application/activity+json",
        "xml": "application/xml",
        "pdf": "application/pdf",
        "near": "application/notjson",
    }
    stored = {
        name: store.ingest(
            f"utf8-{name}\n".encode(),
            metadata(
                source_id=f"media-{name}", media_type=media_type, run_id=f"run-{name}"
            ),
        )
        for name, media_type in media.items()
    }
    rights = ledger(*(decision_entry(item) for item in stored.values()))
    reads = trace_content_reads(monkeypatch)

    cut = build_cut(store, DEFAULT_AS_OF, rights_ledger=rights)

    assert [record["source"]["id"] for record in cut.records] == [
        "media-json",
        "media-plain",
        "media-suffix",
        "media-xml",
    ]
    assert set(reads) == {
        stored["plain"].content_path,
        stored["json"].content_path,
        stored["suffix"].content_path,
        stored["xml"].content_path,
    }
    assert cut.policy["media_types"] == {
        "exact": [
            "application/javascript",
            "application/json",
            "application/x-ndjson",
            "application/xml",
        ],
        "type_prefixes": ["text/"],
        "structured_syntax_suffixes": ["+json", "+xml"],
        "parameters": "forbidden_at_ingest",
        "content_encoding": "utf-8",
    }

    invalid_store = EvidenceDocumentStore(tmp_path / "invalid-utf8")
    invalid = invalid_store.ingest(b"\xff\xfe", metadata(source_id="invalid-utf8"))
    with pytest.raises(TrainingPolicyError, match="not valid UTF-8"):
        build_cut(
            invalid_store, DEFAULT_AS_OF, rights_ledger=ledger(decision_entry(invalid))
        )


def test_training_record_preserves_exact_utf8_bytes(tmp_path: Path) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    raw = "证据 / evidence \U0001f50d\n".encode("utf-8")
    stored = store.ingest(raw, metadata())

    cut = build_cut(store, DEFAULT_AS_OF, rights_ledger=ledger(decision_entry(stored)))
    content = cut.records[0]["content"]
    assert content["text"].encode("utf-8") == raw
    assert base64.b64decode(content["base64"], validate=True) == raw
    assert hashlib.sha256(raw).hexdigest() == content["sha256"]


def test_duplicate_keys_nonfinite_values_invalid_clocks_and_oversize_fail_closed(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence", max_document_bytes=8)
    duplicate = (
        json.dumps(metadata())
        .encode()
        .replace(b'"language": "en",', b'"language": "en", "language": "fr",', 1)
    )
    with pytest.raises(EvidenceDocumentError, match="duplicate JSON key"):
        store.ingest(b"ok", duplicate)

    nonfinite = (
        json.dumps(metadata())
        .encode()
        .replace(b'"language": "en"', b'"language": NaN', 1)
    )
    with pytest.raises(EvidenceDocumentError, match="non-finite"):
        store.ingest(b"ok", nonfinite)

    with pytest.raises(EvidenceDocumentError, match="real timestamp"):
        store.ingest(
            b"ok",
            metadata(knowledge_time="2026-02-30T08:05:00Z"),
        )
    with pytest.raises(EvidenceDocumentError, match="limit is 8"):
        store.ingest(b"123456789", metadata())
    assert not store.root.exists()


def test_content_manifest_and_decision_collisions_or_mismatches_are_rejected(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    raw = b"committed bytes\n"
    saved = store.ingest(raw, metadata())

    saved.content_path.write_bytes(b"tampered bytes\n")
    with pytest.raises(IntegrityError, match="collision or mismatch"):
        store.ingest(raw, metadata())
    with pytest.raises(IntegrityError, match="mismatch"):
        store.read_content(saved.manifest_sha256)

    saved.content_path.write_bytes(raw)
    saved.manifest_path.write_bytes(b"{}")
    with pytest.raises(IntegrityError, match="filename does not match"):
        store.load_manifest(saved.manifest_sha256)
    with pytest.raises(IntegrityError, match="collision or mismatch"):
        store.ingest(raw, metadata())

    entry = decision_entry(saved)
    entry["decision"]["reason"] = "Hash-bound body was changed."
    with pytest.raises(IntegrityError, match="does not match"):
        validate_rights_ledger(ledger(entry))


def test_manifest_content_binding_rejects_false_size_or_digest() -> None:
    raw = b"bound bytes\n"
    manifest = manifest_document(raw)
    assert validate_manifest(manifest, content=raw) == manifest

    wrong_size = deepcopy(manifest)
    wrong_size["content"]["byte_size"] += 1
    wrong_size["acceptance"]["capture_request_sha256"] = capture_request_sha256(
        {
            "spec_version": CAPTURE_REQUEST_SPEC_VERSION,
            "canonicalization": CANONICALIZATION,
            "metadata": {key: wrong_size[key] for key in metadata()},
            "content": wrong_size["content"],
        }
    )
    with pytest.raises(IntegrityError, match="does not match"):
        validate_manifest(wrong_size, content=raw)
    wrong_digest = deepcopy(manifest)
    wrong_digest["content"]["sha256"] = "0" * 64
    wrong_digest["acceptance"]["capture_request_sha256"] = capture_request_sha256(
        {
            "spec_version": CAPTURE_REQUEST_SPEC_VERSION,
            "canonicalization": CANONICALIZATION,
            "metadata": {key: wrong_digest[key] for key in metadata()},
            "content": wrong_digest["content"],
        }
    )
    with pytest.raises(IntegrityError, match="does not match"):
        validate_manifest(wrong_digest, content=raw)


def test_failure_before_manifest_commit_leaves_no_accepted_manifest_or_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    original = evidence_documents._commit_temp_file

    def fail_manifest_commit(temporary: Path, destination: Path, **kwargs: Any) -> None:
        if store.manifests_root in destination.parents:
            raise OSError("simulated crash before manifest commit")
        original(temporary, destination, **kwargs)

    monkeypatch.setattr(evidence_documents, "_commit_temp_file", fail_manifest_commit)
    with pytest.raises(StoreSafetyError, match="simulated crash"):
        store.ingest(b"orphan-safe bytes\n", metadata())

    assert not list(store.manifests_root.rglob("*.json"))
    assert not list(store.staging_root.glob(".intent-*"))
    assert len(list(store.objects_root.rglob("*.bin"))) == 1
    # An orphan content object is not accepted evidence and cannot enter a cut.
    assert build_cut(store, DEFAULT_AS_OF, rights_ledger=None).records == ()


def crash_after_manifest_link(
    store_root: Path, raw: bytes, value: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    script = """
import json
import os
import sys
import core.evidence_documents as evidence_documents

def crash_after_link(temporary, destination):
    if 'manifests' in destination.parts:
        os._exit(73)

evidence_documents._after_hard_link_for_testing = crash_after_link
store = evidence_documents.EvidenceDocumentStore(
    sys.argv[1],
    acceptance_clock=lambda request: request['metadata']['collected_at'],
)
store.ingest(bytes.fromhex(sys.argv[2]), json.loads(sys.argv[3]))
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(store_root),
            raw.hex(),
            json.dumps(value, separators=(",", ":")),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


def crash_after_receipt_stage(
    store_root: Path, raw: bytes, value: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    script = """
import json
import os
import sys
import core.evidence_documents as evidence_documents

original = evidence_documents._commit_temp_file
def crash_before_receipt_link(temporary, destination, **kwargs):
    if 'receipts' in destination.parts:
        os._exit(74)
    original(temporary, destination, **kwargs)

evidence_documents._commit_temp_file = crash_before_receipt_link
store = evidence_documents.EvidenceDocumentStore(
    sys.argv[1],
    acceptance_clock=lambda request: '2026-08-12T08:10:00Z',
)
store.ingest(bytes.fromhex(sys.argv[2]), json.loads(sys.argv[3]))
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(store_root),
            raw.hex(),
            json.dumps(value, separators=(",", ":")),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


def test_sealed_pending_receipt_survives_interleaved_later_ingest(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "private-evidence"
    first_raw = b"pending receipt A\n"
    first_metadata = metadata(source_id="pending-a", run_id="pending-a-run")
    crashed = crash_after_receipt_stage(store_root, first_raw, first_metadata)
    assert crashed.returncode == 74, crashed.stderr

    pending = list((store_root / ".staging").glob(".intent-receipt-*.tmp"))
    assert len(pending) == 1
    assert pending[0].stat().st_nlink == 1

    def interleaved_clock(request: dict[str, Any]) -> str:
        source_id = request["metadata"]["source"]["id"]
        if source_id == "pending-a":
            raise AssertionError("sealed pending receipt must not resample its clock")
        if source_id == "too-early-after-pending":
            return "2026-08-12T08:09:59Z"
        return "2026-08-12T08:20:00Z"

    store = EvidenceDocumentStore(store_root, acceptance_clock=interleaved_clock)
    with pytest.raises(AcceptanceClockError, match="moved backwards"):
        store.ingest(
            b"new sample older than pending\n",
            metadata(
                source_id="too-early-after-pending",
                run_id="too-early-after-pending-run",
            ),
        )

    later = store.ingest(
        b"later accepted request B\n",
        metadata(source_id="later-b", run_id="later-b-run"),
    )
    assert later.accepted_at == "2026-08-12T08:20:00Z"

    retried = store.ingest(first_raw, first_metadata)
    assert retried.accepted_at == "2026-08-12T08:10:00Z"
    assert retried.receipt_created
    assert not list(store.staging_root.glob(".intent-receipt-*.tmp"))


def test_link_before_unlink_crash_is_invisible_and_retry_self_heals_exact_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "private-evidence"
    raw = b"crash-recoverable manifest\n"
    value = metadata(source_id="crash-subject", run_id="crash-run")
    crashed = crash_after_manifest_link(store_root, raw, value)
    assert crashed.returncode == 73, crashed.stderr

    store = EvidenceDocumentStore(store_root)
    manifest_paths = list(store.manifests_root.rglob("*.json"))
    intent_paths = list(store.staging_root.glob(".intent-manifest-*.tmp"))
    assert len(manifest_paths) == len(intent_paths) == 1
    destination_before = manifest_paths[0].stat()
    intent_before = intent_paths[0].stat()
    assert destination_before.st_nlink == intent_before.st_nlink == 2
    assert (destination_before.st_dev, destination_before.st_ino) == (
        intent_before.st_dev,
        intent_before.st_ino,
    )

    rights = ledger(
        decision_entry_for_subject("crash-subject", hashlib.sha256(raw).hexdigest())
    )
    # A link that has not crossed the destination fsync barrier is skipped.
    assert build_cut(store, rights_ledger=rights).records == ()

    synced: list[str] = []
    original_sync = evidence_documents._fsync_descriptor

    def traced_sync(descriptor: int, purpose: str) -> None:
        synced.append(purpose)
        original_sync(descriptor, purpose)

    monkeypatch.setattr(evidence_documents, "_fsync_descriptor", traced_sync)
    retried = store.ingest(raw, value)
    assert not retried.manifest_created
    assert not list(store.staging_root.glob(".intent-manifest-*.tmp"))
    assert retried.manifest_path.stat().st_nlink == 1
    assert "recovered destination" in synced
    assert "recovered staging" in synced
    assert len(build_cut(store, rights_ledger=rights).records) == 1


def test_retry_never_unlinks_a_misbound_staging_alias(tmp_path: Path) -> None:
    store_root = tmp_path / "private-evidence"
    raw = b"misbound recovery state\n"
    value = metadata(source_id="misbound", run_id="misbound-run")
    crashed = crash_after_manifest_link(store_root, raw, value)
    assert crashed.returncode == 73, crashed.stderr
    store = EvidenceDocumentStore(store_root)
    destination = next(store.manifests_root.rglob("*.json"))
    bound = next(store.staging_root.glob(".intent-manifest-*.tmp"))
    misbound = bound.with_name(
        bound.name.replace(".intent-manifest-", ".intent-content-")
    )
    bound.rename(misbound)
    with pytest.raises(StoreSafetyError, match="bound staging"):
        store.ingest(raw, value)
    assert misbound.exists()
    assert destination.exists()
    assert misbound.stat().st_nlink == destination.stat().st_nlink == 2


def test_store_lock_blocks_training_for_the_entire_writer_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    raw = b"transaction-barrier evidence\n"
    value = metadata(source_id="transaction-subject", run_id="transaction-run")
    rights = ledger(
        decision_entry_for_subject(
            "transaction-subject", hashlib.sha256(raw).hexdigest()
        )
    )
    linked = threading.Event()
    release = threading.Event()
    reader_done = threading.Event()
    writer_errors: list[BaseException] = []
    reader_results: list[TrainingCut] = []
    original_hook = evidence_documents._after_hard_link_for_testing

    def block_after_manifest_link(temporary: Path, destination: Path) -> None:
        original_hook(temporary, destination)
        if store.manifests_root in destination.parents:
            linked.set()
            if not release.wait(5):
                raise AssertionError("test did not release blocked writer")

    monkeypatch.setattr(
        evidence_documents, "_after_hard_link_for_testing", block_after_manifest_link
    )

    def write() -> None:
        try:
            store.ingest(raw, value)
        except BaseException as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)

    def read() -> None:
        reader_results.append(build_cut(store, rights_ledger=rights))
        reader_done.set()

    writer = threading.Thread(target=write)
    writer.start()
    assert linked.wait(5)
    reader = threading.Thread(target=read)
    reader.start()
    assert not reader_done.wait(0.2)
    release.set()
    writer.join(5)
    reader.join(5)
    assert not writer.is_alive() and not reader.is_alive()
    assert writer_errors == []
    assert reader_done.is_set()
    assert len(reader_results) == 1
    assert len(reader_results[0].records) == 1


def test_store_lock_keeps_recovery_out_of_an_active_writer_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    linked = threading.Event()
    release = threading.Event()
    recovery_done = threading.Event()
    errors: list[BaseException] = []
    original_hook = evidence_documents._after_hard_link_for_testing

    def block_after_receipt_link(temporary: Path, destination: Path) -> None:
        original_hook(temporary, destination)
        if store.receipts_root in destination.parents:
            linked.set()
            if not release.wait(5):
                raise AssertionError("test did not release blocked writer")

    monkeypatch.setattr(
        evidence_documents, "_after_hard_link_for_testing", block_after_receipt_link
    )

    def write() -> None:
        try:
            store.ingest(
                b"recovery barrier evidence\n",
                metadata(source_id="recovery-barrier", run_id="recovery-barrier-run"),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def recover() -> None:
        try:
            store.recover_staging(older_than_seconds=60, now=2_000_000_000.0)
            recovery_done.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    writer = threading.Thread(target=write)
    writer.start()
    assert linked.wait(5)
    recovery = threading.Thread(target=recover)
    recovery.start()
    assert not recovery_done.wait(0.2)
    release.set()
    writer.join(5)
    recovery.join(5)

    assert not writer.is_alive() and not recovery.is_alive()
    assert errors == []
    assert recovery_done.is_set()
    assert not list(store.staging_root.glob(".intent-*"))


def test_stale_staging_is_unscanned_and_recovery_is_locked_age_and_count_bounded(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    store._ensure_layout()
    now = 1_000_000.0
    old = store.staging_root / ".partial-abcdef.tmp"
    recent = store.staging_root / ".partial-ghijkl.tmp"
    old.write_bytes(b"old crash debris")
    recent.write_bytes(b"live-looking debris")
    old.chmod(0o600)
    recent.chmod(0o600)
    os.utime(old, (now - 1_000, now - 1_000))
    os.utime(recent, (now - 30, now - 30))

    # Staging is not an accepted-tree scan input.
    assert build_cut(store, DEFAULT_AS_OF, rights_ledger=None).records == ()
    assert store.recover_staging(older_than_seconds=60, now=now) == 1
    assert not old.exists() and recent.exists()

    another = store.staging_root / ".partial-mnopqr.tmp"
    another.write_bytes(b"second old file")
    another.chmod(0o600)
    os.utime(recent, (now - 1_000, now - 1_000))
    os.utime(another, (now - 1_000, now - 1_000))
    assert store.recover_staging(older_than_seconds=60, maximum_files=1, now=now) == 1
    assert sum(path.exists() for path in (recent, another)) == 1
    assert store.recover_staging(older_than_seconds=60, maximum_files=1, now=now) == 1
    assert not recent.exists() and not another.exists()


def test_recovery_rejects_unexpected_or_symlink_staging_entries(tmp_path: Path) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    store._ensure_layout()
    unexpected = store.staging_root / "not-a-staged-file"
    unexpected.write_bytes(b"do not delete")
    unexpected.chmod(0o600)
    with pytest.raises(StoreSafetyError, match="unexpected staging entry"):
        store.recover_staging(older_than_seconds=60, now=1_000_000.0)
    unexpected.unlink()

    target = tmp_path / "target"
    target.write_bytes(b"external")
    link = store.staging_root / ".partial-abcdef.tmp"
    link.symlink_to(target)
    with pytest.raises(StoreSafetyError, match="unexpected staging entry"):
        store.recover_staging(older_than_seconds=60, now=1_000_000.0)
    assert target.read_bytes() == b"external"


def test_staging_cleanup_processes_more_than_1024_entries_in_bounded_batches(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    store._ensure_layout()
    now = 2_000_000.0
    for index in range(1_025):
        path = store.staging_root / f".partial-{index:06d}.tmp"
        path.write_bytes(b"bounded legacy debris")
        path.chmod(0o600)
        os.utime(path, (now - 1_000, now - 1_000))

    assert (
        store.recover_staging(older_than_seconds=60, maximum_files=1_024, now=now)
        == 1_024
    )
    assert len(list(store.staging_root.glob(".partial-*"))) == 1
    assert (
        store.recover_staging(older_than_seconds=60, maximum_files=1_024, now=now) == 1
    )
    assert not list(store.staging_root.glob(".partial-*"))


def test_recovery_scan_budget_resumes_past_many_fresh_ineligible_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    store._ensure_layout()
    now = 2_000_000.0
    fresh: list[Path] = []
    for index in range(257):
        path = store.staging_root / f".partial-fresh-{index:06d}.tmp"
        path.write_bytes(b"fresh ineligible debris")
        path.chmod(0o600)
        os.utime(path, (now - 30, now - 30))
        fresh.append(path)
    old = store.staging_root / ".partial-oldest.tmp"
    old.write_bytes(b"eligible old debris")
    old.chmod(0o600)
    os.utime(old, (now - 1_000, now - 1_000))

    real_scandir = os.scandir
    next_calls = 0

    class CountingIterator:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate

        def __iter__(self) -> "CountingIterator":
            return self

        def __next__(self) -> os.DirEntry[str]:
            nonlocal next_calls
            next_calls += 1
            return next(self.delegate)

        def close(self) -> None:
            self.delegate.close()

        def __enter__(self) -> "CountingIterator":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def counted_scandir(path: str | os.PathLike[str]) -> Any:
        delegate = real_scandir(path)
        if Path(path) == store.staging_root:
            return CountingIterator(delegate)
        return delegate

    monkeypatch.setattr(evidence_documents.os, "scandir", counted_scandir)
    completed = 0
    calls = 0
    while calls < 20:
        before = next_calls
        completed += store.recover_staging(
            older_than_seconds=60,
            maximum_files=32,
            maximum_entries=32,
            now=now,
        )
        calls += 1
        # Thirty-two staged entries, plus at most the lock entry and one
        # StopIteration probe, are inspected in any call.
        assert next_calls - before <= 34
        if store._recovery_iterator is None:
            break

    assert calls <= 10
    assert completed == 1
    assert not old.exists()
    assert all(path.exists() for path in fresh)
    assert store._recovery_iterator is None


def test_root_path_traversal_symlinks_privacy_and_ownership_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(StoreSafetyError, match="filesystem root"):
        EvidenceDocumentStore(Path("/"))
    with pytest.raises(StoreSafetyError, match="absolute"):
        EvidenceDocumentStore(Path("relative-store"))
    with pytest.raises(StoreSafetyError, match="path traversal"):
        EvidenceDocumentStore(tmp_path / "a" / ".." / "escape")

    target = tmp_path / "target-directory"
    make_private_directory(target)
    linked_root = tmp_path / "linked-store"
    linked_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(StoreSafetyError, match="symlink"):
        EvidenceDocumentStore(linked_root)

    broad = tmp_path / "broad-root"
    broad.mkdir(mode=0o755)
    broad.chmod(0o755)
    with pytest.raises(StoreSafetyError, match="mode 0700"):
        EvidenceDocumentStore(broad)

    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    with pytest.raises(StoreSafetyError, match="writable by group or other"):
        EvidenceDocumentStore(unsafe_parent / "store")
    unsafe_parent.chmod(0o700)

    owned = tmp_path / "owned-root"
    make_private_directory(owned)
    actual_uid = os.geteuid()
    # Isolate the existing-root owner assertion from the separately tested
    # ancestor-chain assertion while simulating a different effective UID.
    monkeypatch.setattr(
        evidence_documents, "_assert_trusted_ancestor_chain", lambda path: None
    )
    monkeypatch.setattr(evidence_documents.os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(StoreSafetyError, match="owned by the effective user"):
        EvidenceDocumentStore(owned)
    monkeypatch.undo()

    store_root = tmp_path / "private-evidence"
    make_private_directory(store_root)
    escape = tmp_path / "escape-directory"
    make_private_directory(escape)
    (store_root / "objects").symlink_to(escape, target_is_directory=True)
    store = EvidenceDocumentStore(store_root)
    with pytest.raises(StoreSafetyError, match="symlink"):
        store.ingest(b"must not escape\n", metadata())
    assert not (escape / "sha256").exists()

    with pytest.raises(EvidenceDocumentError, match="SHA-256"):
        store.manifest_path("../manifest")


def test_manifest_symlink_and_extra_hardlink_are_rejected(tmp_path: Path) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    saved = store.ingest(b"real bytes\n", metadata())
    leak = tmp_path / "leaked-content-link"
    os.link(saved.content_path, leak)
    with pytest.raises(StoreSafetyError, match="exactly 1 hard link"):
        store.read_content(saved.manifest_sha256)
    leak.unlink()

    saved.manifest_path.unlink()
    saved.manifest_path.symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(StoreSafetyError, match="symlink manifest entry"):
        build_cut(store, DEFAULT_AS_OF, rights_ledger=None)


@pytest.mark.parametrize(
    ("link_errno", "expected", "message"),
    [
        (errno.EXDEV, HardLinkUnsupportedError, "required atomic hard links"),
        (errno.EPERM, StoreSafetyError, "atomic hard-link commit failed"),
    ],
)
def test_hardlink_capability_and_permission_failures_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_errno: int,
    expected: type[BaseException],
    message: str,
) -> None:
    staging = tmp_path / f"staging-{link_errno}"
    destination = tmp_path / f"destination-{link_errno}"
    make_private_directory(staging)
    make_private_directory(destination)
    temporary = staging / ".partial-abcdef.tmp"
    temporary.write_bytes(b"fully staged")
    temporary.chmod(0o600)

    def failing_link(*args: Any, **kwargs: Any) -> None:
        raise OSError(link_errno, os.strerror(link_errno))

    monkeypatch.setattr(evidence_documents.os, "link", failing_link)
    monkeypatch.setattr(
        evidence_documents.os,
        "supports_dir_fd",
        set(os.supports_dir_fd) | {failing_link},
    )
    with pytest.raises(expected, match=message) as caught:
        evidence_documents._commit_temp_file(
            temporary,
            destination / "object.bin",
            payload_sha256=hashlib.sha256(b"fully staged").hexdigest(),
            maximum_bytes=len(b"fully staged"),
            purpose="test object",
        )
    if link_errno == errno.EPERM:
        assert not isinstance(caught.value, HardLinkUnsupportedError)
    assert temporary.exists()
    assert not (destination / "object.bin").exists()


def test_destination_directory_swap_is_detected_and_link_is_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    moved = tmp_path / "destination-moved"
    make_private_directory(staging)
    make_private_directory(destination)
    temporary = staging / ".partial-abcdef.tmp"
    temporary.write_bytes(b"fully staged")
    temporary.chmod(0o600)
    real_link = os.link

    def link_then_swap(*args: Any, **kwargs: Any) -> None:
        real_link(*args, **kwargs)
        destination.rename(moved)
        make_private_directory(destination)

    monkeypatch.setattr(evidence_documents.os, "link", link_then_swap)
    monkeypatch.setattr(
        evidence_documents.os,
        "supports_dir_fd",
        set(os.supports_dir_fd) | {link_then_swap},
    )
    with pytest.raises(StoreSafetyError, match="directory changed"):
        evidence_documents._commit_temp_file(
            temporary,
            destination / "object.bin",
            payload_sha256=hashlib.sha256(b"fully staged").hexdigest(),
            maximum_bytes=len(b"fully staged"),
            purpose="test object",
        )
    assert temporary.exists()
    assert not (destination / "object.bin").exists()
    assert not (moved / "object.bin").exists()


def test_directory_and_exact_existing_paths_establish_fsync_barriers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    directory_syncs: list[Path] = []
    file_syncs: list[Path] = []
    real_directory_sync = evidence_documents._fsync_directory
    real_file_sync = evidence_documents._fsync_regular_file

    def traced_directory(path: Path) -> None:
        directory_syncs.append(path)
        real_directory_sync(path)

    def traced_file(path: Path, purpose: str) -> None:
        file_syncs.append(path)
        real_file_sync(path, purpose)

    monkeypatch.setattr(evidence_documents, "_fsync_directory", traced_directory)
    monkeypatch.setattr(evidence_documents, "_fsync_regular_file", traced_file)
    first = store.ingest(b"durable repeat\n", metadata())
    assert store.root in directory_syncs
    assert store.root.parent in directory_syncs
    assert store.objects_root in directory_syncs
    assert store.manifests_root in directory_syncs

    directory_syncs.clear()
    file_syncs.clear()
    repeated = store.ingest(b"durable repeat\n", metadata())
    assert not repeated.content_created and not repeated.manifest_created
    assert file_syncs == [first.receipt_path, first.content_path, first.manifest_path]
    assert first.receipt_path.parent in directory_syncs
    assert first.content_path.parent in directory_syncs
    assert first.manifest_path.parent in directory_syncs


def test_existing_transaction_lock_privacy_is_not_silently_repaired(
    tmp_path: Path,
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    store._ensure_layout()
    lock = store.staging_root / ".recovery.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)

    with pytest.raises(StoreSafetyError, match="transaction lock is not private"):
        store.ingest(b"must remain rejected\n", metadata())
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644
    assert not list(store.manifests_root.rglob("*.json"))


def test_durability_barrier_failures_are_typed_and_accept_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    real_fsync = os.fsync

    def fail_regular_file_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "simulated durability failure")
        real_fsync(descriptor)

    monkeypatch.setattr(evidence_documents.os, "fsync", fail_regular_file_fsync)
    with pytest.raises(DurabilityError, match="store transaction lock"):
        store.ingest(b"must not be accepted\n", metadata())
    assert not list(store.manifests_root.rglob("*.json"))
    assert not list(store.objects_root.rglob("*.bin"))
    assert not list(store.staging_root.glob(".intent-*"))


@pytest.mark.parametrize(
    "invalid_url",
    [
        "HTTPS://example.org/a",
        "https://user@example.org/a",
        "https://example.org/a#",
        "https://example.org/a?",
        "https://example.org:/a",
        "https://example.org:443/a",
        "https://example.org:0080/a",
        "https://Example.org/a",
        "https://example.org/a/../b",
        "https://example.org/a/%2E%2E/b",
        "https://example.org/a%2fb",
        "https://[fe80::1%25eth0]/a",
    ],
)
def test_runtime_rejects_noncanonical_urls(tmp_path: Path, invalid_url: str) -> None:
    store = EvidenceDocumentStore(
        tmp_path / hashlib.sha256(invalid_url.encode()).hexdigest()
    )
    with pytest.raises(EvidenceDocumentError, match="canonical_url"):
        store.ingest(b"url test\n", metadata(canonical_url=invalid_url))
    assert not store.root.exists()


@pytest.mark.parametrize(
    "valid_url",
    [
        "https://example.org",
        "https://example.org/a%2Fb?q=x",
        "http://example.org:8080/a",
        "https://[2001:db8::1]/a",
    ],
)
def test_runtime_accepts_canonical_urls(tmp_path: Path, valid_url: str) -> None:
    store = EvidenceDocumentStore(
        tmp_path / hashlib.sha256(valid_url.encode()).hexdigest()
    )
    stored = store.ingest(b"url test\n", metadata(canonical_url=valid_url))
    assert stored.manifest["source"]["canonical_url"] == valid_url


def test_schema_examples_policy_and_runtime_only_invariants_match_implementation(
    tmp_path: Path,
) -> None:
    schema = strict_json_loads(
        SCHEMA_PATH.read_bytes(),
        maximum_bytes=256 * 1024,
        purpose="EvidenceDocument schema",
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert (
        "publication_time <= knowledge_time <= collected_at <= "
        "acceptance.accepted_at"
    ) in schema["$comment"]
    assert "event_time is independent" in schema["$comment"]
    assert "out-of-band" in schema["$comment"]

    def references(value: object) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            if "$ref" in value:
                found.append(value["$ref"])
            for child in value.values():
                found.extend(references(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(references(child))
        return found

    for reference in references(schema):
        assert reference.startswith("#/$defs/")
        assert reference.removeprefix("#/$defs/") in schema["$defs"]

    assert "acceptance" not in schema["$defs"]["ingestMetadata"]["required"]
    assert "acceptance" in schema["required"]
    assert "acceptance" in schema["$defs"]["trainingProvenance"]["required"]

    raw = b"Example evidence.\n"
    manifest_example = schema["examples"][0]
    assert validate_manifest(manifest_example, content=raw) == manifest_example
    metadata_example = schema["$defs"]["ingestMetadata"]["examples"][0]
    receipt_example = schema["$defs"]["acceptanceReceipt"]["examples"][0]
    assert validate_acceptance_receipt(receipt_example) == receipt_example
    assert receipt_example["spec_version"] == ACCEPTANCE_RECEIPT_SPEC_VERSION
    assert receipt_example["capture_request"]["spec_version"] == (
        CAPTURE_REQUEST_SPEC_VERSION
    )
    assert capture_request_sha256(receipt_example["capture_request"]) == (
        receipt_example["capture_request_sha256"]
    )
    assert hashlib.sha256(canonical_json_bytes(receipt_example)).hexdigest() == (
        manifest_example["acceptance"]["receipt_sha256"]
    )
    store = EvidenceDocumentStore(tmp_path / "private-evidence")
    stored = store.ingest(raw, metadata_example)
    assert stored.manifest == manifest_example

    decision_example = schema["$defs"]["rightsDecision"]["examples"][0]
    assert validate_rights_decision(decision_example) == decision_example
    ledger_example = schema["$defs"]["rightsLedger"]["examples"][0]
    assert validate_rights_ledger(ledger_example) == ledger_example
    assert ledger_example == empty_rights_ledger()

    empty_cut = build_cut(
        EvidenceDocumentStore(tmp_path / "empty-store"),
        "2026-08-12T08:00:00Z",
        rights_ledger=ledger_example,
    )
    assert empty_cut.to_dict() == schema["$defs"]["trainingCut"]["examples"][0]
    assert empty_cut.policy == schema["$defs"]["trainingPolicy"]["const"]
    assert set(empty_cut.to_dict()) == set(schema["$defs"]["trainingCut"]["required"])
    assert empty_cut.to_dict()["spec_version"] == CUT_SPEC_VERSION
    assert empty_cut.policy["id"] == TEXT_TRAINING_POLICY_ID

    url_pattern = re.compile(schema["$defs"]["canonicalURL"]["pattern"])
    assert url_pattern.fullmatch("https://example.org/a?q=x")
    for invalid in (
        "HTTPS://example.org/a",
        "https://example.org:/a",
        "https://example.org/a?",
        "https://example.org/a#",
    ):
        assert url_pattern.fullmatch(invalid) is None
