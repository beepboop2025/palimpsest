"""Adapter from Palimpsest readings to Evidence Capsule v1.

This module never creates a merely *plausible* integrity claim. It emits only
after finding the complete canonical reading payload in the ledger and an exact
anchor input plus detached envelope for a prefix containing that entry.
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
    MAX_ARTIFACT_BYTES,
    MAX_CAPSULE_BYTES,
    MAX_OTS_BYTES,
    MAX_TOTAL_ARTIFACT_BYTES,
    MERKLE_PROOF,
    SPEC_VERSION,
    CapsuleError,
    _ledger_canonical_bytes,
    _parse_anchor_input,
    _timestamp,
    _verify_ots_detached,
    build_capsule,
    strict_json_loads,
    verify_capsule,
)


# Adapter inputs are local repository material, but they are still parsed as
# hostile data.  A byte cap alone is insufficient for JSONL: 32 MiB of ``{}\n``
# expands into millions of Python objects, and repeated anchor records used to
# retain a fresh copy of every referenced proof.  These bounds leave years of
# headroom at the current cadence while keeping memory and verification work
# finite.
MAX_LEDGER_ENTRIES = 65_536
MAX_ANCHOR_RECORDS = 8_192
MAX_ANCHOR_CANDIDATES = 128
MAX_ANCHOR_SCAN_BYTES = MAX_TOTAL_ARTIFACT_BYTES


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_jsonl(
    path: Path,
    *,
    maximum_records: int,
    label: str,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    data = _read_bounded_file(path, MAX_CAPSULE_BYTES, label)
    for line_number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        if len(values) >= maximum_records:
            raise CapsuleError(f"{label} exceeds {maximum_records} records")
        value = strict_json_loads(line)
        if not isinstance(value, dict):
            raise CapsuleError(f"{path}:{line_number}: expected a JSON object")
        values.append(value)
    return values


def _read_bounded_file(path: Path, maximum: int, label: str) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
    except OSError as exc:
        raise CapsuleError(f"{label} cannot be read: {exc}") from exc
    if len(data) > maximum:
        raise CapsuleError(f"{label} exceeds the v1 byte limit")
    return data


def _resolve_input(path: str | Path, label: str) -> Path:
    try:
        return Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapsuleError(f"{label} cannot be resolved safely: {exc}") from exc


def _inside_repo(repo_root: Path, relative: str) -> Path:
    if "\\" in relative:
        raise CapsuleError("anchor record path uses a backslash")
    rel = PurePosixPath(relative)
    if (rel.is_absolute() or not rel.parts or str(rel) != relative
            or any(part in {"", ".", ".."} for part in rel.parts)):
        raise CapsuleError("anchor record path is not a clean repository-relative path")
    try:
        target = repo_root.joinpath(*rel.parts).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapsuleError(f"anchor record path cannot be resolved safely: {exc}") from exc
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
    # Keep metadata only, deduplicate before touching any referenced file, then
    # visit tightest prefixes first.  The first valid item is the answer, so no
    # proof/input byte blobs accumulate in memory.
    candidates: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
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
        timestamp = record.get("ts")
        root = roots.get(f"{ledger_name}_root")
        head = roots.get(f"{ledger_name}_head")
        input_name = ots.get("file")
        proof_name = ots.get("proof")
        if not all(isinstance(value, str) for value in (
            timestamp, root, head, input_name, proof_name,
        )):
            continue
        try:
            _timestamp(timestamp, "anchor record timestamp")
            sort_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (CapsuleError, ValueError, OverflowError):
            continue
        identity = (count, timestamp, root, head, input_name, proof_name)
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append((
            count, sort_time, timestamp, root, head, input_name, proof_name, record,
        ))

    candidates.sort(key=lambda item: item[:-1])
    prefix_facts: dict[int, tuple[str, str]] = {}
    attempts = 0
    scanned_bytes = 0
    for (count, _sort_time, _timestamp_text, declared_root, declared_head,
         input_name, proof_name, record) in candidates:
        attempts += 1
        if attempts > MAX_ANCHOR_CANDIDATES:
            raise CapsuleError(
                f"anchor candidate verification exceeds {MAX_ANCHOR_CANDIDATES} attempts"
            )
        if count not in prefix_facts:
            prefix = entries[:count]
            prefix_facts[count] = (merkle_root(prefix), prefix[-1]["entry_hash"])
        expected_root, expected_head = prefix_facts[count]
        if declared_root != expected_root or declared_head != expected_head:
            continue
        try:
            input_path = _inside_repo(repo_root, input_name)
            proof_path = _inside_repo(repo_root, proof_name)
            input_bytes = _read_bounded_file(
                input_path, MAX_ARTIFACT_BYTES, "anchor input"
            )
        except (CapsuleError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            continue
        scanned_bytes += len(input_bytes)
        if scanned_bytes > MAX_ANCHOR_SCAN_BYTES:
            raise CapsuleError(
                f"anchor candidate bytes exceed {MAX_ANCHOR_SCAN_BYTES}"
            )
        try:
            proof_bytes = _read_bounded_file(
                proof_path, MAX_OTS_BYTES, "OpenTimestamps envelope"
            )
        except (CapsuleError, OSError, RuntimeError, TypeError, ValueError):
            continue
        scanned_bytes += len(proof_bytes)
        if scanned_bytes > MAX_ANCHOR_SCAN_BYTES:
            raise CapsuleError(
                f"anchor candidate bytes exceed {MAX_ANCHOR_SCAN_BYTES}"
            )
        try:
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
        except (CapsuleError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            continue
        return record, input_bytes, proof_bytes

    if not candidates:
        raise CapsuleError(
            "no exact anchored ledger prefix contains this reading; refusing to emit"
        )
    raise CapsuleError(
        "no exact anchored ledger prefix passed bounded verification; refusing to emit"
    )


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
    """Create a self-contained capsule for an exact reading with entry-membership evidence.

    Raises :class:`CapsuleError` when the payload is absent, ambiguous, the chain
    is broken, or there is no exact anchored prefix containing it.
    """
    reading_path = _resolve_input(reading_path, "Palimpsest reading")
    ledger_path = _resolve_input(ledger_path, "Palimpsest ledger")
    anchors_path = _resolve_input(anchors_path, "Palimpsest anchor registry")
    repo_root = _resolve_input(
        repository_root if repository_root else anchors_path.parent.parent,
        "repository root",
    )
    try:
        reading_path.relative_to(repo_root)
        relative_reading = reading_path.relative_to(repo_root).as_posix()
    except ValueError:
        relative_reading = reading_path.name

    reading_bytes = _read_bounded_file(
        reading_path, MAX_ARTIFACT_BYTES, "Palimpsest reading"
    )
    reading = strict_json_loads(reading_bytes)
    if not isinstance(reading, dict):
        raise CapsuleError("Palimpsest reading must be a JSON object")
    payload_digest = _sha256(_ledger_canonical_bytes(reading))

    entries = _read_jsonl(
        ledger_path, maximum_records=MAX_LEDGER_ENTRIES, label="Palimpsest ledger"
    )
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

    anchor_records = _read_jsonl(
        anchors_path,
        maximum_records=MAX_ANCHOR_RECORDS,
        label="Palimpsest anchor registry",
    )
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
            collector="OpenTimestamps detached envelope", untrusted=True,
        ),
    ]
    claims: list[dict[str, Any]] = [{
        "id": "entry-membership",
        "type": "integrity",
        "statement": (
            f"The complete canonical JSON value of the {source} reading matches "
            f"{ledger_name} entry {entry['seq']} and its Merkle membership proof for the "
            f"declared {prefix_entries}-entry prefix. The prefix fields are digest-bound "
            "to the attached structurally complete OpenTimestamps envelope."
        ),
        "artifact_refs": ["reading", "anchor-input", "anchor-proof"],
        "derivation_refs": [],
        "binding_refs": ["reading-membership"],
        "evidence_level": "direct",
        "limitations": [
            "The offline v1 verifier validates the detached envelope structure and input digest only; it does not prove calendar acceptance, attestation authenticity, Bitcoin consensus, or a time.",
            "The capsule proves Merkle entry membership, not the complete ledger hash chain; the adapter checked the frozen chain before emission.",
            "Entry membership proves what Palimpsest recorded, not that the upstream source reported ground truth.",
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
            "binding_refs": [],
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
        "id": "reading-membership",
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
            "title": f"Canonical Palimpsest reading with entry-membership evidence: {source}",
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
