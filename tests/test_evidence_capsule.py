"""Evidence Capsule v1 interoperability and fail-closed security tests."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

import evidence.capsule as capsule_module
import evidence.palimpsest as palimpsest_module
from evidence.capsule import (
    CANONICALIZATION,
    IJSON_SAFE_INTEGER,
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS,
    MAX_JSON_DEPTH,
    MAX_TOTAL_ARTIFACT_BYTES,
    SPEC_VERSION,
    _OTS_MAGIC,
    CapsuleError,
    build_capsule,
    canonical_bytes,
    content_sha256,
    load_capsule,
    strict_json_loads,
    verify_capsule,
)
from evidence.palimpsest import capsule_from_reading

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "protocol" / "test-vectors"
ADAPTER_FIXTURE = VECTORS / "palimpsest-adapter-v1"
CONFORMANCE = ROOT / "protocol" / "conformance-v1.json"


def _vector(name: str) -> dict:
    return load_capsule(VECTORS / name)


def _rehash(capsule: dict) -> dict:
    capsule["content_sha256"] = content_sha256(capsule["content"])
    return capsule


def _ots_varuint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        encoded.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(encoded)


def _ots_varbytes(value: bytes) -> bytes:
    return _ots_varuint(len(value)) + value


def _ots_attestation(tag: bytes, payload: bytes) -> bytes:
    return b"\x00" + tag + _ots_varbytes(payload)


def _ots_envelope(subject: bytes, timestamp: bytes) -> bytes:
    return (
        _OTS_MAGIC + b"\x01\x08" + hashlib.sha256(subject).digest() + timestamp
    )


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
            "binding_refs": [],
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
    assert palimpsest["ledger"]["status"] == "entry_membership_verified"
    assert palimpsest["anchor"]["status"] == "envelope_bound"
    assert palimpsest["claims"]["status"] == "references_verified"
    assert palimpsest["recomputability"]["status"] == "verified"

    nemesis = verify_capsule(_vector("nemesis-ddti-v1.json"))
    assert nemesis["ok"]
    assert nemesis["ledger"]["status"] == "not_present"
    assert nemesis["anchor"]["status"] == "not_present"
    assert nemesis["recomputability"]["status"] == "partial"
    assert nemesis["claims"]["status"] == "references_partially_recomputable"
    assert any(item["status"] == "not_recomputable"
               for item in nemesis["recomputability"]["items"])


def test_cross_repo_verifier_implementation_is_pinned() -> None:
    expected = (VECTORS / "verifier-v1.sha256").read_text().strip()
    actual = hashlib.sha256((ROOT / "evidence" / "capsule.py").read_bytes()).hexdigest()
    assert actual == expected
    manifest = strict_json_loads(CONFORMANCE.read_bytes())
    assert expected == manifest["files"]["verifier/capsule.py"]


def test_conformance_release_pins_protocol_schema_verifier_and_vectors() -> None:
    manifest = strict_json_loads(CONFORMANCE.read_bytes())
    assert set(manifest) == {
        "conformance_release", "spec_version", "hash_algorithm", "files",
    }
    assert manifest["spec_version"] == SPEC_VERSION
    assert manifest["hash_algorithm"] == "sha256"
    release_digest = hashlib.sha256(canonical_bytes(manifest["files"])).hexdigest()
    assert manifest["conformance_release"] == f"sha256:{release_digest}"
    paths = {
        "protocol/evidence-capsule-v1.md": ROOT / "protocol" / "evidence-capsule-v1.md",
        "protocol/evidence-capsule-v1.schema.json": (
            ROOT / "protocol" / "evidence-capsule-v1.schema.json"
        ),
        "test-vectors/canonicalization-v1.json": VECTORS / "canonicalization-v1.json",
        "test-vectors/nemesis-ddti-source.json": VECTORS / "nemesis-ddti-source.json",
        "test-vectors/nemesis-ddti-v1.json": VECTORS / "nemesis-ddti-v1.json",
        "test-vectors/palimpsest-erasure-v1.json": VECTORS / "palimpsest-erasure-v1.json",
        "verifier/capsule.py": ROOT / "evidence" / "capsule.py",
    }
    assert set(manifest["files"]) == set(paths)
    for logical_name, path in paths.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["files"][logical_name]


def test_canonicalization_vector_fixes_all_json_string_escape_classes() -> None:
    vector = strict_json_loads((VECTORS / "canonicalization-v1.json").read_bytes())
    assert vector["canonicalization"] == CANONICALIZATION
    expected = base64.b64decode(vector["canonical_utf8_base64"], validate=True)
    actual = canonical_bytes(vector["value"])
    assert actual == expected
    assert hashlib.sha256(actual).hexdigest() == vector["sha256"]


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
    artifacts = {item["id"]: item for item in generated["content"]["artifacts"]}
    assert artifacts["anchor-input"]["untrusted"] is False
    assert artifacts["anchor-proof"]["untrusted"] is True


def test_adapter_deduplicates_anchor_candidates_before_reading_proofs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = ADAPTER_FIXTURE / "readings"
    entries = palimpsest_module._read_jsonl(
        readings / "erasure-ledger.jsonl",
        maximum_records=palimpsest_module.MAX_LEDGER_ENTRIES,
        label="test ledger",
    )
    records = palimpsest_module._read_jsonl(
        readings / "anchors.jsonl",
        maximum_records=palimpsest_module.MAX_ANCHOR_RECORDS,
        label="test anchors",
    )
    calls = 0
    original = palimpsest_module._verify_ots_detached

    def counted(proof: bytes, subject: bytes):
        nonlocal calls
        calls += 1
        return original(proof, subject)

    monkeypatch.setattr(palimpsest_module, "_verify_ots_detached", counted)
    record, _anchor_input, _anchor_proof = palimpsest_module._find_anchor(
        entries=entries,
        target_seq=120,
        anchor_records=[copy.deepcopy(records[0]) for _ in range(500)],
        repo_root=ADAPTER_FIXTURE,
        ledger_name="erasure",
    )
    assert record == records[0]
    assert calls == 1


def test_adapter_bounds_jsonl_records_anchor_work_and_referenced_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tiny_records = tmp_path / "tiny.jsonl"
    tiny_records.write_text("{}\n{}\n{}\n", encoding="utf-8")
    with pytest.raises(CapsuleError, match="exceeds 2 records"):
        palimpsest_module._read_jsonl(
            tiny_records, maximum_records=2, label="hostile registry"
        )

    readings = ADAPTER_FIXTURE / "readings"
    entries = palimpsest_module._read_jsonl(
        readings / "erasure-ledger.jsonl",
        maximum_records=palimpsest_module.MAX_LEDGER_ENTRIES,
        label="test ledger",
    )
    base = palimpsest_module._read_jsonl(
        readings / "anchors.jsonl",
        maximum_records=palimpsest_module.MAX_ANCHOR_RECORDS,
        label="test anchors",
    )[0]
    hostile = []
    for count in range(1, 6):
        record = copy.deepcopy(base)
        record["roots"]["erasure_entries"] = count
        record["roots"]["erasure_root"] = "0" * 64
        record["roots"]["erasure_head"] = "0" * 64
        hostile.append(record)
    monkeypatch.setattr(palimpsest_module, "MAX_ANCHOR_CANDIDATES", 4)
    with pytest.raises(CapsuleError, match="exceeds 4 attempts"):
        palimpsest_module._find_anchor(
            entries=entries, target_seq=0, anchor_records=hostile,
            repo_root=ADAPTER_FIXTURE, ledger_name="erasure",
        )

    monkeypatch.setattr(palimpsest_module, "MAX_ANCHOR_CANDIDATES", 128)
    monkeypatch.setattr(palimpsest_module, "MAX_ANCHOR_SCAN_BYTES", 1)
    with pytest.raises(CapsuleError, match="candidate bytes exceed 1"):
        palimpsest_module._find_anchor(
            entries=entries, target_seq=120, anchor_records=[base],
            repo_root=ADAPTER_FIXTURE, ledger_name="erasure",
        )


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


def test_adapter_translates_symlink_loop_input_resolution(tmp_path: Path) -> None:
    readings = ADAPTER_FIXTURE / "readings"
    loop = tmp_path / "loop"
    try:
        loop.symlink_to("loop")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(CapsuleError, match="cannot be resolved safely"):
        capsule_from_reading(
            loop, source="censored-planet",
            ledger_path=readings / "erasure-ledger.jsonl",
            anchors_path=readings / "anchors.jsonl",
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
    assert report["ledger"]["status"] == "entry_membership_verified"
    assert report["anchor"]["status"] == "failed"
    assert not report["ok"]


def test_header_only_ots_blob_is_not_a_complete_envelope() -> None:
    capsule = _vector("palimpsest-erasure-v1.json")
    artifacts = {item["id"]: item for item in capsule["content"]["artifacts"]}
    anchor_input = base64.b64decode(artifacts["anchor-input"]["location"]["data"])
    fake = _OTS_MAGIC + b"\x01\x08" + hashlib.sha256(anchor_input).digest()
    proof = artifacts["anchor-proof"]
    proof["location"]["data"] = base64.b64encode(fake).decode("ascii")
    proof["sha256"] = hashlib.sha256(fake).hexdigest()
    proof["size"] = len(fake)
    _rehash(capsule)

    report = verify_capsule(capsule)
    assert not report["ok"]
    assert report["anchor"]["status"] == "failed"
    assert "timestamp" not in report["anchor"]["items"][0]["status"]


def test_ots_operations_enforce_result_and_hexlify_input_limits() -> None:
    subject = b"exact anchor input"
    opaque_leaf = _ots_attestation(b"unknown!", b"\x80opaque")

    valid_edge = (
        b"\xf0" + _ots_varbytes(b"x" * 2016) + b"\xf3" + opaque_leaf
    )
    shape = capsule_module._verify_ots_detached(
        _ots_envelope(subject, valid_edge), subject
    )
    assert shape == {"nodes": 3, "attestations": 1}

    oversized_result = b"\xf0" + _ots_varbytes(b"x" * 4065) + opaque_leaf
    with pytest.raises(CapsuleError, match="operation result exceeds 4096"):
        capsule_module._verify_ots_detached(
            _ots_envelope(subject, oversized_result), subject
        )

    oversized_hexlify_input = (
        b"\xf0" + _ots_varbytes(b"x" * 2017) + b"\xf3" + opaque_leaf
    )
    with pytest.raises(CapsuleError, match="hexlify input exceeds 2048"):
        capsule_module._verify_ots_detached(
            _ots_envelope(subject, oversized_hexlify_input), subject
        )


def test_ots_branching_preserves_message_lengths_and_depth() -> None:
    subject = b"exact anchor input"
    opaque_leaf = _ots_attestation(b"unknown!", b"opaque")

    valid_length_branch = (
        b"\xff\xf0"
        + _ots_varbytes(b"x" * 2016)
        + b"\xf3"
        + opaque_leaf
        + b"\xf3"
        + opaque_leaf
    )
    shape = capsule_module._verify_ots_detached(
        _ots_envelope(subject, valid_length_branch), subject
    )
    assert shape == {"nodes": 5, "attestations": 2}

    oversized_hexlify_branch = (
        b"\xff\xf0"
        + _ots_varbytes(b"x" * 2017)
        + b"\xf3"
        + opaque_leaf
        + b"\xf3"
        + opaque_leaf
    )
    with pytest.raises(CapsuleError, match="hexlify input exceeds 2048"):
        capsule_module._verify_ots_detached(
            _ots_envelope(subject, oversized_hexlify_branch), subject
        )

    valid_depth_branch = b"\xff" + (b"\x08" * 64) + opaque_leaf + opaque_leaf
    assert capsule_module._verify_ots_detached(
        _ots_envelope(subject, valid_depth_branch), subject
    )["attestations"] == 2

    oversized_depth_branch = b"\xff" + (b"\x08" * 65) + opaque_leaf + opaque_leaf
    with pytest.raises(CapsuleError, match="depth limit"):
        capsule_module._verify_ots_detached(
            _ots_envelope(subject, oversized_depth_branch), subject
        )


@pytest.mark.parametrize(
    "tag,payload",
    [
        (bytes.fromhex("83dfe30d2ef90c8e"), _ots_varbytes(b"https://calendar.example")),
        (bytes.fromhex("0588960d73d71901"), _ots_varuint(900_000)),
        (bytes.fromhex("06869a0d73d71b45"), _ots_varuint(3_000_000)),
    ],
)
def test_known_ots_attestation_payloads_are_fully_parsed(
    tag: bytes, payload: bytes
) -> None:
    subject = b"exact anchor input"
    valid = _ots_envelope(subject, _ots_attestation(tag, payload))
    assert capsule_module._verify_ots_detached(valid, subject)["attestations"] == 1

    trailing = _ots_envelope(subject, _ots_attestation(tag, payload + b"\x00"))
    with pytest.raises(CapsuleError, match="payload has trailing bytes"):
        capsule_module._verify_ots_detached(trailing, subject)


@pytest.mark.parametrize(
    "tag,payload",
    [
        (
            bytes.fromhex("83dfe30d2ef90c8e"),
            _ots_varbytes(b"https://calendar.example/?query"),
        ),
        (bytes.fromhex("0588960d73d71901"), b"\x81\x00"),
        (bytes.fromhex("06869a0d73d71b45"), b""),
    ],
)
def test_malformed_known_ots_attestation_payloads_fail_closed(
    tag: bytes, payload: bytes
) -> None:
    subject = b"exact anchor input"
    envelope = _ots_envelope(subject, _ots_attestation(tag, payload))
    with pytest.raises(CapsuleError, match="OpenTimestamps"):
        capsule_module._verify_ots_detached(envelope, subject)


def test_unknown_ots_attestations_remain_bounded_and_opaque() -> None:
    subject = b"exact anchor input"
    opaque = _ots_envelope(
        subject, _ots_attestation(b"unknown!", b"\x80not-a-known-payload")
    )
    assert capsule_module._verify_ots_detached(opaque, subject)["attestations"] == 1

    oversized = _ots_envelope(
        subject, _ots_attestation(b"unknown!", b"x" * 8193)
    )
    with pytest.raises(CapsuleError, match="length exceeds"):
        capsule_module._verify_ots_detached(oversized, subject)


def test_referenced_anchor_proof_artifact_must_declare_untrusted_true() -> None:
    capsule = _vector("palimpsest-erasure-v1.json")
    artifacts = {item["id"]: item for item in capsule["content"]["artifacts"]}
    artifacts["anchor-proof"]["untrusted"] = False
    _rehash(capsule)

    report = verify_capsule(capsule)
    assert not report["ok"]
    assert report["schema"]["status"] == "failed"
    assert any("must declare untrusted true" in error for error in report["errors"])


def test_integrity_claim_must_reference_its_exact_binding() -> None:
    capsule = _vector("palimpsest-erasure-v1.json")
    claim = capsule["content"]["claims"][0]
    claim["artifact_refs"] = ["anchor-input", "anchor-proof"]
    _rehash(capsule)

    report = verify_capsule(capsule)
    assert not report["ok"]
    assert report["schema"]["status"] == "failed"
    assert report["claims"]["status"] == "not_evaluated"
    assert any("does not cite" in error for error in report["errors"])


def test_reports_entry_membership_without_claiming_chain_or_time() -> None:
    report = verify_capsule(_vector("palimpsest-erasure-v1.json"))
    ledger_item = report["ledger"]["items"][0]
    anchor_item = report["anchor"]["items"][0]
    claim_item = report["claims"]["items"][0]

    assert ledger_item["entry_integrity"] == "verified"
    assert ledger_item["membership"] == "verified"
    assert ledger_item["chain_integrity"] == "not_verifiable_from_capsule"
    assert anchor_item["status"] == "envelope_bound"
    assert anchor_item["cryptographic_timestamp"] == "not_verified"
    assert claim_item["binding_refs"] == ["reading-membership"]
    assert claim_item["natural_language_truth"] == "not_evaluated"


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


def test_symlink_loop_returns_a_structured_failure(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    loop = root / "loop"
    try:
        loop.symlink_to("loop")
    except OSError:
        pytest.skip("symlinks unavailable")
    capsule = _minimal_capsule(b"x", {"type": "path", "path": "loop"})
    report = verify_capsule(capsule, base_dir=root)
    assert not report["ok"]
    assert report["artifacts"]["status"] == "failed"
    assert any("resolved safely" in problem
               for problem in report["artifacts"]["items"][0]["problems"])


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


@pytest.mark.parametrize("field", ["artifacts", "derivations", "bindings"])
@pytest.mark.parametrize("bad_value", [None, 1, True])
def test_malformed_containers_return_a_structured_failure(field, bad_value) -> None:
    capsule = _vector("nemesis-ddti-v1.json")
    capsule["content"][field] = bad_value
    report = verify_capsule(capsule)
    assert not report["ok"]
    assert report["schema"]["status"] == "failed"
    assert report["artifacts"]["status"] == "not_evaluated"


def test_unhashable_enum_value_returns_a_structured_failure() -> None:
    capsule = _vector("nemesis-ddti-v1.json")
    capsule["content"]["intents"][0]["type"] = {}
    report = verify_capsule(capsule)
    assert not report["ok"]
    assert report["schema"]["status"] == "failed"
    assert report["artifacts"]["status"] == "not_evaluated"


@pytest.mark.parametrize("pointer", ["/ranked/٠/term", "/ranked/²/term"])
def test_json_pointer_indices_are_ascii_and_never_raise(pointer: str) -> None:
    capsule = _vector("nemesis-ddti-v1.json")
    capsule["content"]["derivations"][0]["proof"]["pointer"] = pointer
    _rehash(capsule)
    report = verify_capsule(capsule)
    assert not report["ok"]
    assert report["recomputability"]["status"] == "failed"


def test_i_json_integer_range_and_collection_caps_are_enforced() -> None:
    with pytest.raises(CapsuleError, match="I-JSON"):
        content_sha256({"unsafe": IJSON_SAFE_INTEGER + 1})
    with pytest.raises(CapsuleError, match="I-JSON"):
        strict_json_loads('{"unsafe": 9007199254740992}')

    capsule = _minimal_capsule(b"x")
    capsule["content"]["artifacts"] = [
        capsule["content"]["artifacts"][0]
        for _ in range(MAX_ARTIFACTS + 1)
    ]
    with pytest.raises(CapsuleError, match="exceeds 64 items"):
        build_capsule(capsule["content"])

    capsule = _minimal_capsule(b"x")
    prototype = capsule["content"]["artifacts"][0]
    capsule["content"]["artifacts"] = []
    for index in range(MAX_TOTAL_ARTIFACT_BYTES // MAX_ARTIFACT_BYTES + 1):
        artifact = copy.deepcopy(prototype)
        artifact["id"] = f"payload-{index}"
        artifact["size"] = MAX_ARTIFACT_BYTES
        capsule["content"]["artifacts"].append(artifact)
    capsule["content"]["claims"][0]["artifact_refs"] = [
        artifact["id"] for artifact in capsule["content"]["artifacts"]
    ]
    with pytest.raises(CapsuleError, match="declared bytes exceed"):
        build_capsule(capsule["content"])


def test_json_depth_unicode_and_file_size_limits_fail_before_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(CapsuleError, match="lone Unicode surrogate"):
        strict_json_loads(b'"\\ud800"')

    nested = "0"
    for _ in range(MAX_JSON_DEPTH + 1):
        nested = f"[{nested}]"
    with pytest.raises(CapsuleError, match="nesting exceeds"):
        strict_json_loads(nested)

    oversized = tmp_path / "oversized.capsule.json"
    oversized.write_bytes(b" " * 9)
    monkeypatch.setattr(capsule_module, "MAX_CAPSULE_BYTES", 8)
    with pytest.raises(CapsuleError, match="capsule exceeds the v1 byte limit"):
        capsule_module.load_capsule(oversized)


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


def test_protocol_documents_mutable_artifact_root_precondition() -> None:
    protocol = " ".join(
        (ROOT / "protocol" / "evidence-capsule-v1.md").read_text().split()
    )
    assert "must stay stable for the duration of verification" in protocol
    assert "untrusted local process" in protocol
    assert "calendar-returned material" in protocol


def test_public_evidence_capsule_guide_is_indexed_and_downloadable() -> None:
    page = (ROOT / "evidence-capsules.html").read_text()
    example = "/protocol/test-vectors/palimpsest-erasure-v1.json"
    assert '<meta name="robots" content="index, follow' in page
    assert f'href="{example}"' in page
    assert "not a moving “latest” alias" in page
    assert "scripts/evidence_capsule.py verify" in page
    assert "https://palimpsest.info/evidence-capsules.html" in (
        ROOT / "sitemap.xml"
    ).read_text()
    assert "evidence-capsules.html" in (ROOT / "llms.txt").read_text()
    assert "evidence-capsules.html" in (ROOT / "README.md").read_text()
