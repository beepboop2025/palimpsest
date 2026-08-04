"""Evidence Capsule v1 interoperability and fail-closed security tests."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from evidence.capsule import (
    CANONICALIZATION,
    SPEC_VERSION,
    CapsuleError,
    build_capsule,
    content_sha256,
    load_capsule,
    verify_capsule,
)
from evidence.palimpsest import capsule_from_reading

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "protocol" / "test-vectors"
ADAPTER_FIXTURE = VECTORS / "palimpsest-adapter-v1"


def _vector(name: str) -> dict:
    return load_capsule(VECTORS / name)


def _rehash(capsule: dict) -> dict:
    capsule["content_sha256"] = content_sha256(capsule["content"])
    return capsule


def _minimal_capsule(data: bytes, location: dict | None = None) -> dict:
    location = location or {
        "type": "inline", "encoding": "base64",
        "data": base64.b64encode(data).decode("ascii"),
    }
    content = {
        "spec_version": SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "created_at": "2026-08-04T10:00:00+00:00",
        "producer": {"name": "test", "software": "test/v1"},
        "subject": {"type": "evidence-set", "id": "test", "title": "Inert test evidence"},
        "artifacts": [{
            "id": "payload", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "media_type": "text/plain", "untrusted": True,
            "source": {
                "uri": "https://example.invalid/must-not-fetch",
                "captured_at": "2026-08-04T09:00:00+00:00",
                "collector": "test",
            },
            "location": location,
        }],
        "claims": [{
            "id": "payload-present", "type": "observation",
            "statement": "The inert payload bytes are present.",
            "artifact_refs": ["payload"], "derivation_refs": [],
            "evidence_level": "direct", "limitations": ["Test data only."],
        }],
        "derivations": [],
        "intents": [{
            "type": "human-review", "summary": "Review bytes only.", "advisory": True,
        }],
        "bindings": [],
    }
    return build_capsule(content)


def test_cross_repo_golden_vectors_verify() -> None:
    palimpsest = verify_capsule(_vector("palimpsest-erasure-v1.json"))
    assert palimpsest["ok"]
    assert palimpsest["integrity"]["status"] == "verified"
    assert palimpsest["artifacts"]["status"] == "verified"
    assert palimpsest["ledger"]["status"] == "verified"
    assert palimpsest["anchor"]["status"] == "bound"
    assert palimpsest["recomputability"]["status"] == "verified"

    nemesis = verify_capsule(_vector("nemesis-ddti-v1.json"))
    assert nemesis["ok"]
    assert nemesis["ledger"]["status"] == "not_present"
    assert nemesis["anchor"]["status"] == "not_present"
    assert nemesis["recomputability"]["status"] == "partial"
    assert any(item["status"] == "not_recomputable"
               for item in nemesis["recomputability"]["items"])


def test_cross_repo_verifier_implementation_is_pinned() -> None:
    expected = (VECTORS / "verifier-v1.sha256").read_text().strip()
    actual = hashlib.sha256((ROOT / "evidence" / "capsule.py").read_bytes()).hexdigest()
    assert actual == expected


def test_palimpsest_adapter_recreates_golden_vector_from_frozen_exact_prefix() -> None:
    readings = ADAPTER_FIXTURE / "readings"
    generated = capsule_from_reading(
        readings / "censored-planet-latest.json",
        source="censored-planet",
        ledger_path=readings / "erasure-ledger.jsonl",
        anchors_path=readings / "anchors.jsonl",
        repository_root=ADAPTER_FIXTURE,
        created_at="2026-08-04T09:26:52.865414+00:00",
    )
    assert generated == _vector("palimpsest-erasure-v1.json")


def test_adapter_refuses_unsealed_payload_and_missing_anchor(tmp_path: Path) -> None:
    readings = ADAPTER_FIXTURE / "readings"
    changed = json.loads((readings / "censored-planet-latest.json").read_text())
    changed["cn_interference_rate_pct"] = 99
    changed_path = tmp_path / "changed.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(CapsuleError, match="no ledger seal"):
        capsule_from_reading(
            changed_path, source="censored-planet",
            ledger_path=readings / "erasure-ledger.jsonl",
            anchors_path=readings / "anchors.jsonl",
            repository_root=ADAPTER_FIXTURE,
        )

    empty_anchors = tmp_path / "anchors.jsonl"
    empty_anchors.write_text("", encoding="utf-8")
    with pytest.raises(CapsuleError, match="no exact anchored ledger prefix"):
        capsule_from_reading(
            readings / "censored-planet-latest.json", source="censored-planet",
            ledger_path=readings / "erasure-ledger.jsonl", anchors_path=empty_anchors,
            repository_root=ADAPTER_FIXTURE,
        )


def test_content_tamper_fails_integrity() -> None:
    capsule = _vector("palimpsest-erasure-v1.json")
    capsule["content"]["claims"][0]["statement"] = "tampered"
    report = verify_capsule(capsule)
    assert not report["ok"]
    assert report["integrity"]["status"] == "failed"


def test_artifact_substitution_cannot_survive_ledger_binding() -> None:
    capsule = _vector("palimpsest-erasure-v1.json")
    artifact = capsule["content"]["artifacts"][0]
    substitute = b"{}"
    artifact["location"]["data"] = base64.b64encode(substitute).decode("ascii")
    artifact["sha256"] = hashlib.sha256(substitute).hexdigest()
    artifact["size"] = len(substitute)
    _rehash(capsule)  # model an attacker who can rewrite the unsigned envelope
    report = verify_capsule(capsule)
    assert report["integrity"]["status"] == "verified"
    assert report["artifacts"]["status"] == "verified"
    assert report["ledger"]["status"] == "failed"
    assert not report["ok"]


def test_merkle_path_and_root_tampering_fail_independently() -> None:
    path_tamper = _vector("palimpsest-erasure-v1.json")
    step = path_tamper["content"]["bindings"][0]["inclusion_proof"]["path"][0]
    step["side"] = "left" if step["side"] == "right" else "right"
    _rehash(path_tamper)
    path_report = verify_capsule(path_tamper)
    assert path_report["ledger"]["status"] == "failed"
    assert not path_report["ok"]

    root_tamper = _vector("palimpsest-erasure-v1.json")
    binding = root_tamper["content"]["bindings"][0]
    binding["inclusion_proof"]["merkle_root"] = "0" * 64
    binding["anchor"]["merkle_root"] = "0" * 64
    _rehash(root_tamper)
    root_report = verify_capsule(root_tamper)
    assert root_report["ledger"]["status"] == "failed"
    assert root_report["anchor"]["status"] == "failed"
    assert not root_report["ok"]


def test_detached_timestamp_proof_must_commit_to_exact_anchor_input() -> None:
    capsule = _vector("palimpsest-erasure-v1.json")
    artifacts = {item["id"]: item for item in capsule["content"]["artifacts"]}
    anchor_input = base64.b64decode(artifacts["anchor-input"]["location"]["data"])
    proof_artifact = artifacts["anchor-proof"]
    proof = bytearray(base64.b64decode(proof_artifact["location"]["data"]))
    committed = hashlib.sha256(anchor_input).digest()
    offset = proof.index(committed)
    proof[offset] ^= 1
    changed = bytes(proof)
    proof_artifact["location"]["data"] = base64.b64encode(changed).decode("ascii")
    proof_artifact["sha256"] = hashlib.sha256(changed).hexdigest()
    proof_artifact["size"] = len(changed)
    _rehash(capsule)
    report = verify_capsule(capsule)
    assert report["artifacts"]["status"] == "verified"
    assert report["ledger"]["status"] == "verified"
    assert report["anchor"]["status"] == "failed"
    assert not report["ok"]


def test_path_artifacts_are_root_confined_and_detect_substitution(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    payload = root / "payload.txt"
    payload.write_bytes(b"safe bytes")
    capsule = _minimal_capsule(b"safe bytes", {"type": "path", "path": "payload.txt"})
    assert verify_capsule(capsule, base_dir=root)["ok"]

    payload.write_bytes(b"substituted")
    substituted = verify_capsule(capsule, base_dir=root)
    assert substituted["artifacts"]["status"] == "failed"
    assert not substituted["ok"]


@pytest.mark.parametrize("bad_path", ["../outside", "/tmp/outside", "a\\b", "nested//file"])
def test_path_traversal_and_noncanonical_paths_fail(tmp_path: Path, bad_path: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    capsule = _minimal_capsule(b"x", {"type": "path", "path": bad_path})
    report = verify_capsule(capsule, base_dir=root)
    assert report["artifacts"]["status"] == "failed"
    assert not report["ok"]


def test_symlink_escape_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    capsule = _minimal_capsule(b"x", {"type": "path", "path": "link.txt"})
    assert not verify_capsule(capsule, base_dir=root)["ok"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["content"].__setitem__("spec_version", "palimpsest-evidence-capsule/v2"),
        lambda c: c["content"].__setitem__("canonicalization", "mystery-json-v9"),
        lambda c: c["content"]["derivations"][0]["proof"].__setitem__("type", "shell-proof-v1"),
        lambda c: c["content"]["intents"][0].__setitem__("type", "execute"),
        lambda c: c["content"]["intents"][0].__setitem__("action", "open-url"),
    ],
)
def test_unknown_version_canonicalization_proof_and_action_fail_closed(mutation) -> None:
    capsule = _vector("nemesis-ddti-v1.json")
    mutation(capsule)
    _rehash(capsule)
    report = verify_capsule(capsule)
    assert not report["ok"]
    assert report["schema"]["status"] == "failed"


def test_unknown_ledger_proof_fails_closed() -> None:
    capsule = _vector("palimpsest-erasure-v1.json")
    capsule["content"]["bindings"][0]["inclusion_proof"]["type"] = "unknown-tree-v9"
    _rehash(capsule)
    assert not verify_capsule(capsule)["ok"]


def test_untrusted_artifact_and_advisory_intent_never_fetch_or_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "executed"
    dangerous = f"touch {marker}\nhttps://example.invalid/payload".encode()
    capsule = _minimal_capsule(dangerous)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verifier attempted a network or execution primitive")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    report = verify_capsule(capsule)
    assert report["ok"]
    assert not marker.exists()


def test_attestations_are_outside_content_identity() -> None:
    capsule = _minimal_capsule(b"evidence")
    original = capsule["content_sha256"]
    capsule["attestations"].append({
        "type": "statement-v1", "content_sha256": original,
        "actor": "independent reviewer", "issued_at": "2026-08-04T11:00:00+00:00",
        "statement": "I reviewed this content identity.",
    })
    report = verify_capsule(capsule)
    assert report["ok"]
    assert capsule["content_sha256"] == original
    assert report["attestations"]["status"] == "bound_not_authenticated"


def test_protocol_schema_is_valid_json() -> None:
    schema = json.loads((ROOT / "protocol" / "evidence-capsule-v1.schema.json").read_text())
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
