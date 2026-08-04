"""Adapter from Palimpsest readings to Evidence Capsule v1.

This module never creates a merely *plausible* integrity claim.  It emits only
after finding the complete canonical reading payload in the ledger and a real
OpenTimestamps anchor input for an exact prefix containing that entry.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from core.sealed_ledger import inclusion_proof, merkle_root, verify
from evidence.capsule import (
    ANCHOR_PROOF,
    CANONICALIZATION,
    LEDGER_BINDING,
    LEDGER_CANONICALIZATION,
    MERKLE_PROOF,
    SPEC_VERSION,
    CapsuleError,
    _ledger_canonical_bytes,
    _parse_anchor_input,
    _verify_ots_detached,
    build_capsule,
    strict_json_loads,
    verify_capsule,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), 1):
        if not line.strip():
            continue
        value = strict_json_loads(line)
        if not isinstance(value, dict):
            raise CapsuleError(f"{path}:{line_number}: expected a JSON object")
        values.append(value)
    return values


def _inside_repo(repo_root: Path, relative: str) -> Path:
    if "\\" in relative:
        raise CapsuleError("anchor record path uses a backslash")
    rel = PurePosixPath(relative)
    if (rel.is_absolute() or not rel.parts or str(rel) != relative
            or any(part in {"", ".", ".."} for part in rel.parts)):
        raise CapsuleError("anchor record path is not a clean repository-relative path")
    target = repo_root.joinpath(*rel.parts).resolve(strict=True)
    try:
        target.relative_to(repo_root)
    except ValueError as exc:
        raise CapsuleError("anchor record path escapes the repository") from exc
    if not target.is_file():
        raise CapsuleError("anchor record path is not a file")
    return target


def _inline_artifact(*, artifact_id: str, data: bytes, media_type: str,
                     uri: str, captured_at: str, collector: str,
                     untrusted: bool) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "sha256": _sha256(data),
        "size": len(data),
        "media_type": media_type,
        "source": {
            "uri": uri,
            "captured_at": captured_at,
            "collector": collector,
        },
        "untrusted": untrusted,
        "location": {
            "type": "inline",
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _find_anchor(*, entries: list[dict[str, Any]], target_seq: int,
                 anchor_records: list[dict[str, Any]], repo_root: Path,
                 ledger_name: str) -> tuple[dict[str, Any], bytes, bytes]:
    candidates: list[tuple[int, str, dict[str, Any], bytes, bytes]] = []
    for record in anchor_records:
        roots = record.get("roots")
        ots = record.get("ots")
        if not isinstance(roots, dict) or not isinstance(ots, dict) or ots.get("ok") is not True:
            continue
        count = roots.get(f"{ledger_name}_entries")
        if isinstance(count, bool) or not isinstance(count, int):
            continue
        if count <= target_seq or count > len(entries):
            continue
        prefix = entries[:count]
        ok, _ = verify(prefix)
        if not ok:
            continue
        expected_root = merkle_root(prefix)
        expected_head = prefix[-1]["entry_hash"]
        if (roots.get(f"{ledger_name}_root") != expected_root
                or roots.get(f"{ledger_name}_head") != expected_head):
            continue
        try:
            input_path = _inside_repo(repo_root, ots["file"])
            proof_path = _inside_repo(repo_root, ots["proof"])
            input_bytes = input_path.read_bytes()
            proof_bytes = proof_path.read_bytes()
            fields = _parse_anchor_input(input_bytes)
            expected_fields = {
                f"{ledger_name}_entries": str(count),
                f"{ledger_name}_head": expected_head,
                f"{ledger_name}_root": expected_root,
                "anchored_at": record["ts"],
            }
            if any(fields.get(key) != value for key, value in expected_fields.items()):
                continue
            _verify_ots_detached(proof_bytes, input_bytes)
        except (CapsuleError, KeyError, OSError, TypeError):
            continue
        candidates.append((count, record["ts"], record, input_bytes, proof_bytes))
    if not candidates:
        raise CapsuleError(
            "no exact anchored ledger prefix contains this sealed reading; refusing to emit"
        )
    # The earliest containing prefix gives the tightest public upper bound on
    # existence.  Ties are deterministic.
    _, _, record, input_bytes, proof_bytes = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    return record, input_bytes, proof_bytes


def capsule_from_reading(
    reading_path: str | Path,
    *,
    source: str,
    ledger_path: str | Path,
    anchors_path: str | Path,
    ledger_name: str = "erasure",
    repository_root: str | Path | None = None,
    source_uri: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create a self-contained capsule for an exactly sealed reading.

    Raises :class:`CapsuleError` when the payload is absent, ambiguous, the chain
    is broken, or there is no exact anchored prefix containing it.
    """
    reading_path = Path(reading_path).resolve(strict=True)
    ledger_path = Path(ledger_path).resolve(strict=True)
    anchors_path = Path(anchors_path).resolve(strict=True)
    repo_root = (Path(repository_root).resolve(strict=True) if repository_root
                 else anchors_path.parent.parent.resolve(strict=True))
    try:
        reading_path.relative_to(repo_root)
        relative_reading = reading_path.relative_to(repo_root).as_posix()
    except ValueError:
        relative_reading = reading_path.name

    reading_bytes = reading_path.read_bytes()
    reading = strict_json_loads(reading_bytes)
    if not isinstance(reading, dict):
        raise CapsuleError("Palimpsest reading must be a JSON object")
    payload_digest = _sha256(_ledger_canonical_bytes(reading))

    entries = _read_jsonl(ledger_path)
    chain_ok, chain_problems = verify(entries)
    if not chain_ok:
        raise CapsuleError("ledger is broken: " + "; ".join(chain_problems))
    matches = [entry for entry in entries
               if entry.get("source") == source
               and entry.get("payload_sha256") == payload_digest]
    if not matches:
        raise CapsuleError(
            "the exact complete reading payload has no ledger seal; refusing to emit"
        )
    if len(matches) != 1:
        raise CapsuleError("the exact reading payload has ambiguous duplicate seals")
    entry = matches[0]

    anchor_records = _read_jsonl(anchors_path)
    anchor_record, anchor_input, anchor_proof = _find_anchor(
        entries=entries, target_seq=entry["seq"], anchor_records=anchor_records,
        repo_root=repo_root, ledger_name=ledger_name,
    )
    roots = anchor_record["roots"]
    prefix_entries = roots[f"{ledger_name}_entries"]
    prefix = entries[:prefix_entries]
    proof = inclusion_proof(prefix, entry["seq"])
    proof = {"type": MERKLE_PROOF, **proof}

    capture_field = "generated_at" if isinstance(reading.get("generated_at"), str) else (
        "last_changed_at" if isinstance(reading.get("last_changed_at"), str) else None
    )
    captured_at = reading.get(capture_field) if capture_field else entry["ts"]
    if not isinstance(captured_at, str):
        raise CapsuleError("reading/ledger capture time is not a string")
    created = created_at or datetime.now(timezone.utc).isoformat()
    reading_uri = source_uri or f"https://palimpsest.info/{relative_reading}"
    ots = anchor_record["ots"]
    artifacts = [
        _inline_artifact(
            artifact_id="reading", data=reading_bytes, media_type="application/json",
            uri=reading_uri, captured_at=captured_at, collector=source, untrusted=True,
        ),
        _inline_artifact(
            artifact_id="anchor-input", data=anchor_input, media_type="text/plain",
            uri=f"https://palimpsest.info/{ots['file']}", captured_at=anchor_record["ts"],
            collector="Palimpsest anchor_roots", untrusted=False,
        ),
        _inline_artifact(
            artifact_id="anchor-proof", data=anchor_proof,
            media_type="application/vnd.opentimestamps.ots",
            uri=f"https://palimpsest.info/{ots['proof']}", captured_at=anchor_record["ts"],
            collector="OpenTimestamps detached proof", untrusted=False,
        ),
    ]
    claims: list[dict[str, Any]] = [{
        "id": "sealed-reading",
        "type": "integrity",
        "statement": (
            f"The complete {source} reading payload is sealed at {ledger_name} ledger "
            f"sequence {entry['seq']} and included in the exact {prefix_entries}-entry "
            "prefix supplied to OpenTimestamps."
        ),
        "artifact_refs": ["reading", "anchor-input", "anchor-proof"],
        "derivation_refs": [],
        "evidence_level": "direct",
        "limitations": [
            "The offline v1 verifier binds the detached OpenTimestamps proof bytes but does not validate Bitcoin consensus.",
            "A seal proves what Palimpsest recorded, not that the upstream source reported ground truth.",
        ],
    }]
    derivations: list[dict[str, Any]] = []
    if capture_field:
        claims.append({
            "id": "capture-time",
            "type": "provenance",
            "statement": f"The reading reports its capture time as {captured_at}.",
            "artifact_refs": ["reading"],
            "derivation_refs": ["extract-capture-time"],
            "evidence_level": "derived",
            "limitations": ["This verifies the value present in the artifact, not the upstream clock."],
        })
        derivations.append({
            "id": "extract-capture-time",
            "type": "extraction",
            "description": f"Read /{capture_field} from the complete JSON reading.",
            "input_artifact_refs": ["reading"],
            "supports_claim_refs": ["capture-time"],
            "proof": {
                "type": "json-pointer-equals-v1",
                "artifact_ref": "reading",
                "pointer": f"/{capture_field}",
                "expected": captured_at,
            },
        })

    binding = {
        "type": LEDGER_BINDING,
        "artifact_ref": "reading",
        "payload_canonicalization": LEDGER_CANONICALIZATION,
        "entry": dict(entry),
        "inclusion_proof": proof,
        "anchor": {
            "type": "palimpsest-prefix-anchor-v1",
            "ledger": ledger_name,
            "prefix_entries": prefix_entries,
            "head_hash": roots[f"{ledger_name}_head"],
            "merkle_root": roots[f"{ledger_name}_root"],
            "anchored_at": anchor_record["ts"],
            "input_artifact_ref": "anchor-input",
            "proof_artifact_ref": "anchor-proof",
            "proof_type": ANCHOR_PROOF,
        },
    }
    content = {
        "spec_version": SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "created_at": created,
        "producer": {"name": "Palimpsest", "software": "evidence.palimpsest/v1"},
        "subject": {
            "type": "reading",
            "id": f"{source}:{payload_digest}",
            "title": f"Exactly sealed Palimpsest reading: {source}",
        },
        "artifacts": artifacts,
        "claims": claims,
        "derivations": derivations,
        "intents": [{
            "type": "human-review",
            "summary": "Review the reading and its stated limitations as inert evidence.",
            "advisory": True,
        }],
        "bindings": [binding],
    }
    capsule = build_capsule(content)
    report = verify_capsule(capsule)
    if not report["ok"]:
        raise CapsuleError("adapter produced an invalid capsule: " + "; ".join(report["errors"]))
    return capsule
