"""Palimpsest Evidence Capsule v1.

The verifier in this module is deliberately boring: standard library only, no
network imports, no subprocesses, no dynamic dispatch.  Source URIs and every
artifact byte are treated as inert evidence.  The only supported derivation is
a JSON-pointer equality check; every other calculation must be represented by
the explicit, honest ``declared-nonrecomputable-v1`` proof.

The wire protocol is documented in ``protocol/evidence-capsule-v1.md``.  This
module performs strict validation itself so verification does not depend on a
JSON Schema package being installed.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

SPEC_VERSION = "palimpsest-evidence-capsule/v1"
CANONICALIZATION = "palimpsest-json-sorted-utf8-v1"
LEDGER_CANONICALIZATION = "palimpsest-ledger-json-v1"
MERKLE_PROOF = "palimpsest-merkle-duplicate-last-v1"
LEDGER_BINDING = "palimpsest-ledger-anchor-v1"
ANCHOR_PROOF = "opentimestamps-detached-v1"

CLAIM_TYPES = frozenset({
    "observation", "measurement", "provenance", "integrity",
    "analytical-lead",
})
DERIVATION_TYPES = frozenset({
    "extraction", "calculation", "ranking", "analyst-judgment",
})
EVIDENCE_LEVELS = frozenset({"direct", "derived", "sampled", "reported"})
INTENT_TYPES = frozenset({"human-review", "investigate", "preserve", "compare", "cite"})
PROOF_TYPES = frozenset({"json-pointer-equals-v1", "declared-nonrecomputable-v1"})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_MEDIA = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_ANCHOR_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_OTS_MAGIC = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"


class CapsuleError(ValueError):
    """The capsule is malformed or uses an unsupported protocol feature."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapsuleError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CapsuleError(f"non-finite JSON number is forbidden: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CapsuleError(f"JSON number overflows to a non-finite value: {value}")
    return parsed


def strict_json_loads(data: bytes | str) -> Any:
    """Parse JSON while rejecting duplicate keys and NaN/Infinity."""
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    except UnicodeDecodeError as exc:
        raise CapsuleError(f"JSON is not UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_strict_pairs,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except CapsuleError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise CapsuleError(f"invalid JSON: {exc}") from exc


def load_capsule(path: str | Path) -> dict[str, Any]:
    value = strict_json_loads(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise CapsuleError("capsule envelope must be a JSON object")
    return value


def _check_canonical_value(value: Any, path: str = "content") -> None:
    """v1 content deliberately excludes floats for cross-runtime stability."""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        raise CapsuleError(
            f"{path}: floats are forbidden in hashed content; encode decimals as strings"
        )
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_canonical_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CapsuleError(f"{path}: object keys must be strings")
            _check_canonical_value(item, f"{path}.{key}")
        return
    raise CapsuleError(f"{path}: unsupported JSON value {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """Encode v1 hashed content deterministically.

    The transform is sorted object keys, UTF-8, no insignificant whitespace,
    literal Unicode, integer numbers only.  It is intentionally versioned and
    is not claimed to be RFC 8785.
    """
    _check_canonical_value(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CapsuleError(f"content cannot be canonicalized: {exc}") from exc


def _ledger_canonical_bytes(value: Any) -> bytes:
    """Match the existing Palimpsest ledger payload/hash serialization exactly."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CapsuleError(f"ledger payload cannot be canonicalized: {exc}") from exc


def content_sha256(content: Any) -> str:
    return _sha256(canonical_bytes(content))


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CapsuleError(f"{path}: expected object")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise CapsuleError(f"{path}: expected array")
    return value


def _text(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise CapsuleError(f"{path}: expected {'non-empty ' if nonempty else ''}string")
    return value


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CapsuleError(f"{path}: expected integer >= {minimum}")
    return value


def _fields(value: Any, path: str, required: set[str], optional: set[str] = set()) -> Mapping[str, Any]:
    obj = _mapping(value, path)
    missing = required - set(obj)
    unknown = set(obj) - required - optional
    if missing:
        raise CapsuleError(f"{path}: missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise CapsuleError(f"{path}: unknown field(s): {', '.join(sorted(unknown))}")
    return obj


def _id(value: Any, path: str) -> str:
    text = _text(value, path)
    if not _ID.fullmatch(text):
        raise CapsuleError(f"{path}: invalid identifier")
    return text


def _hex64(value: Any, path: str) -> str:
    text = _text(value, path)
    if not _HEX64.fullmatch(text):
        raise CapsuleError(f"{path}: expected lowercase sha256 hex")
    return text


def _timestamp(value: Any, path: str) -> str:
    text = _text(value, path)
    if not _RFC3339.fullmatch(text):
        raise CapsuleError(f"{path}: invalid RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapsuleError(f"{path}: invalid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise CapsuleError(f"{path}: timestamp must include a UTC offset")
    return text


def _string_refs(value: Any, path: str) -> list[str]:
    refs = _list(value, path)
    result = [_id(item, f"{path}[{i}]") for i, item in enumerate(refs)]
    if len(result) != len(set(result)):
        raise CapsuleError(f"{path}: duplicate reference")
    return result


def _validate_content(content: Any) -> None:
    obj = _fields(content, "content", {
        "spec_version", "canonicalization", "created_at", "producer", "subject",
        "artifacts", "claims", "derivations", "intents", "bindings",
    })
    if obj["spec_version"] != SPEC_VERSION:
        raise CapsuleError(f"content.spec_version: unsupported version {obj['spec_version']!r}")
    if obj["canonicalization"] != CANONICALIZATION:
        raise CapsuleError(
            f"content.canonicalization: unsupported algorithm {obj['canonicalization']!r}"
        )
    _timestamp(obj["created_at"], "content.created_at")

    producer = _fields(obj["producer"], "content.producer", {"name", "software"})
    _text(producer["name"], "content.producer.name")
    _text(producer["software"], "content.producer.software")
    subject = _fields(obj["subject"], "content.subject", {"type", "id", "title"})
    if subject["type"] not in {"reading", "analytical-lead", "evidence-set"}:
        raise CapsuleError(f"content.subject.type: unknown subject type {subject['type']!r}")
    _text(subject["id"], "content.subject.id")
    _text(subject["title"], "content.subject.title")

    artifact_ids: set[str] = set()
    artifact_values = _list(obj["artifacts"], "content.artifacts")
    if not artifact_values:
        raise CapsuleError("content.artifacts: at least one exact artifact is required")
    for index, raw in enumerate(artifact_values):
        path = f"content.artifacts[{index}]"
        artifact = _fields(raw, path, {
            "id", "sha256", "size", "media_type", "source", "untrusted", "location",
        })
        aid = _id(artifact["id"], f"{path}.id")
        if aid in artifact_ids:
            raise CapsuleError(f"{path}.id: duplicate artifact id {aid!r}")
        artifact_ids.add(aid)
        _hex64(artifact["sha256"], f"{path}.sha256")
        _integer(artifact["size"], f"{path}.size")
        media = _text(artifact["media_type"], f"{path}.media_type")
        if not _MEDIA.fullmatch(media):
            raise CapsuleError(f"{path}.media_type: invalid media type")
        if not isinstance(artifact["untrusted"], bool):
            raise CapsuleError(f"{path}.untrusted: expected boolean")
        source = _fields(artifact["source"], f"{path}.source",
                         {"uri", "captured_at", "collector"})
        _text(source["uri"], f"{path}.source.uri")
        _timestamp(source["captured_at"], f"{path}.source.captured_at")
        _text(source["collector"], f"{path}.source.collector")
        location = _mapping(artifact["location"], f"{path}.location")
        location_type = location.get("type")
        if location_type == "inline":
            location = _fields(location, f"{path}.location", {"type", "encoding", "data"})
            if location["encoding"] != "base64":
                raise CapsuleError(f"{path}.location.encoding: only base64 is supported")
            _text(location["data"], f"{path}.location.data", nonempty=False)
        elif location_type == "path":
            location = _fields(location, f"{path}.location", {"type", "path"})
            _text(location["path"], f"{path}.location.path")
        else:
            raise CapsuleError(f"{path}.location.type: unknown location {location_type!r}")

    claim_ids: set[str] = set()
    claims = _list(obj["claims"], "content.claims")
    if not claims:
        raise CapsuleError("content.claims: at least one typed claim is required")
    claim_derivation_refs: dict[str, list[str]] = {}
    for index, raw in enumerate(claims):
        path = f"content.claims[{index}]"
        claim = _fields(raw, path, {
            "id", "type", "statement", "artifact_refs", "derivation_refs",
            "evidence_level", "limitations",
        })
        cid = _id(claim["id"], f"{path}.id")
        if cid in claim_ids:
            raise CapsuleError(f"{path}.id: duplicate claim id {cid!r}")
        claim_ids.add(cid)
        if claim["type"] not in CLAIM_TYPES:
            raise CapsuleError(f"{path}.type: unknown claim type {claim['type']!r}")
        _text(claim["statement"], f"{path}.statement")
        for ref in _string_refs(claim["artifact_refs"], f"{path}.artifact_refs"):
            if ref not in artifact_ids:
                raise CapsuleError(f"{path}.artifact_refs: unknown artifact {ref!r}")
        claim_derivation_refs[cid] = _string_refs(
            claim["derivation_refs"], f"{path}.derivation_refs"
        )
        if claim["evidence_level"] not in EVIDENCE_LEVELS:
            raise CapsuleError(f"{path}.evidence_level: unknown level {claim['evidence_level']!r}")
        limitations = _list(claim["limitations"], f"{path}.limitations")
        for li, limitation in enumerate(limitations):
            _text(limitation, f"{path}.limitations[{li}]")

    derivation_ids: set[str] = set()
    derivation_claim_refs: dict[str, list[str]] = {}
    for index, raw in enumerate(_list(obj["derivations"], "content.derivations")):
        path = f"content.derivations[{index}]"
        derivation = _fields(raw, path, {
            "id", "type", "description", "input_artifact_refs",
            "supports_claim_refs", "proof",
        })
        did = _id(derivation["id"], f"{path}.id")
        if did in derivation_ids:
            raise CapsuleError(f"{path}.id: duplicate derivation id {did!r}")
        derivation_ids.add(did)
        if derivation["type"] not in DERIVATION_TYPES:
            raise CapsuleError(f"{path}.type: unknown derivation type {derivation['type']!r}")
        _text(derivation["description"], f"{path}.description")
        input_refs = _string_refs(derivation["input_artifact_refs"],
                                  f"{path}.input_artifact_refs")
        for ref in input_refs:
            if ref not in artifact_ids:
                raise CapsuleError(f"{path}: unknown input artifact {ref!r}")
        claim_refs = _string_refs(derivation["supports_claim_refs"],
                                  f"{path}.supports_claim_refs")
        for ref in claim_refs:
            if ref not in claim_ids:
                raise CapsuleError(f"{path}: unknown supported claim {ref!r}")
        derivation_claim_refs[did] = claim_refs
        proof = _mapping(derivation["proof"], f"{path}.proof")
        proof_type = proof.get("type")
        if proof_type == "json-pointer-equals-v1":
            proof = _fields(proof, f"{path}.proof",
                            {"type", "artifact_ref", "pointer", "expected"})
            if proof["artifact_ref"] not in artifact_ids:
                raise CapsuleError(f"{path}.proof.artifact_ref: unknown artifact")
            if proof["artifact_ref"] not in input_refs:
                raise CapsuleError(f"{path}.proof.artifact_ref: not declared as an input")
            pointer = _text(proof["pointer"], f"{path}.proof.pointer", nonempty=False)
            if pointer and not pointer.startswith("/"):
                raise CapsuleError(f"{path}.proof.pointer: must be empty or start with /")
            _check_canonical_value(proof["expected"], f"{path}.proof.expected")
        elif proof_type == "declared-nonrecomputable-v1":
            proof = _fields(proof, f"{path}.proof", {"type", "reason"})
            _text(proof["reason"], f"{path}.proof.reason")
        else:
            raise CapsuleError(f"{path}.proof.type: unknown proof {proof_type!r}")

    for claim_id, refs in claim_derivation_refs.items():
        for ref in refs:
            if ref not in derivation_ids:
                raise CapsuleError(f"claim {claim_id!r}: unknown derivation {ref!r}")
            if claim_id not in derivation_claim_refs[ref]:
                raise CapsuleError(
                    f"claim {claim_id!r} and derivation {ref!r} do not reference each other"
                )
    for derivation_id, refs in derivation_claim_refs.items():
        for claim_id in refs:
            if derivation_id not in claim_derivation_refs[claim_id]:
                raise CapsuleError(
                    f"derivation {derivation_id!r} and claim {claim_id!r} do not reference each other"
                )

    for index, raw in enumerate(_list(obj["intents"], "content.intents")):
        path = f"content.intents[{index}]"
        intent = _fields(raw, path, {"type", "summary", "advisory"})
        if intent["type"] not in INTENT_TYPES:
            raise CapsuleError(f"{path}.type: unknown/non-advisory action {intent['type']!r}")
        _text(intent["summary"], f"{path}.summary")
        if intent["advisory"] is not True:
            raise CapsuleError(f"{path}.advisory: v1 intents must be true")

    for index, raw in enumerate(_list(obj["bindings"], "content.bindings")):
        _validate_binding(raw, f"content.bindings[{index}]", artifact_ids)

    # Hashed content must satisfy the canonical domain after structural checks.
    _check_canonical_value(obj)


def _validate_binding(raw: Any, path: str, artifact_ids: set[str]) -> None:
    binding = _fields(raw, path, {
        "type", "artifact_ref", "payload_canonicalization", "entry",
        "inclusion_proof", "anchor",
    })
    if binding["type"] != LEDGER_BINDING:
        raise CapsuleError(f"{path}.type: unknown binding {binding['type']!r}")
    if binding["artifact_ref"] not in artifact_ids:
        raise CapsuleError(f"{path}.artifact_ref: unknown artifact")
    if binding["payload_canonicalization"] != LEDGER_CANONICALIZATION:
        raise CapsuleError(f"{path}.payload_canonicalization: unsupported algorithm")
    entry = _fields(binding["entry"], f"{path}.entry", {
        "seq", "ts", "source", "payload_sha256", "prev_hash", "entry_hash",
    })
    _integer(entry["seq"], f"{path}.entry.seq")
    _timestamp(entry["ts"], f"{path}.entry.ts")
    _text(entry["source"], f"{path}.entry.source")
    for name in ("payload_sha256", "prev_hash", "entry_hash"):
        _hex64(entry[name], f"{path}.entry.{name}")
    proof = _fields(binding["inclusion_proof"], f"{path}.inclusion_proof", {
        "type", "seq", "entry_hash", "n_entries", "path", "merkle_root",
    })
    if proof["type"] != MERKLE_PROOF:
        raise CapsuleError(f"{path}.inclusion_proof.type: unknown proof {proof['type']!r}")
    _integer(proof["seq"], f"{path}.inclusion_proof.seq")
    _integer(proof["n_entries"], f"{path}.inclusion_proof.n_entries", minimum=1)
    _hex64(proof["entry_hash"], f"{path}.inclusion_proof.entry_hash")
    _hex64(proof["merkle_root"], f"{path}.inclusion_proof.merkle_root")
    for pi, raw_step in enumerate(_list(proof["path"], f"{path}.inclusion_proof.path")):
        step = _fields(raw_step, f"{path}.inclusion_proof.path[{pi}]", {"side", "hash"})
        if step["side"] not in {"left", "right"}:
            raise CapsuleError(f"{path}.inclusion_proof.path[{pi}].side: unknown side")
        _hex64(step["hash"], f"{path}.inclusion_proof.path[{pi}].hash")
    anchor = _fields(binding["anchor"], f"{path}.anchor", {
        "type", "ledger", "prefix_entries", "head_hash", "merkle_root",
        "anchored_at", "input_artifact_ref", "proof_artifact_ref", "proof_type",
    })
    if anchor["type"] != "palimpsest-prefix-anchor-v1":
        raise CapsuleError(f"{path}.anchor.type: unknown anchor type {anchor['type']!r}")
    _id(anchor["ledger"], f"{path}.anchor.ledger")
    _integer(anchor["prefix_entries"], f"{path}.anchor.prefix_entries", minimum=1)
    _hex64(anchor["head_hash"], f"{path}.anchor.head_hash")
    _hex64(anchor["merkle_root"], f"{path}.anchor.merkle_root")
    _timestamp(anchor["anchored_at"], f"{path}.anchor.anchored_at")
    for field in ("input_artifact_ref", "proof_artifact_ref"):
        if anchor[field] not in artifact_ids:
            raise CapsuleError(f"{path}.anchor.{field}: unknown artifact")
    if anchor["proof_type"] != ANCHOR_PROOF:
        raise CapsuleError(f"{path}.anchor.proof_type: unknown proof {anchor['proof_type']!r}")


def _validate_attestations(attestations: Any, digest: str) -> None:
    for index, raw in enumerate(_list(attestations, "attestations")):
        path = f"attestations[{index}]"
        item = _fields(raw, path, {
            "type", "content_sha256", "actor", "issued_at", "statement",
        })
        if item["type"] != "statement-v1":
            raise CapsuleError(f"{path}.type: unknown attestation type {item['type']!r}")
        _hex64(item["content_sha256"], f"{path}.content_sha256")
        if item["content_sha256"] != digest:
            raise CapsuleError(f"{path}.content_sha256: does not target this capsule")
        _text(item["actor"], f"{path}.actor")
        _timestamp(item["issued_at"], f"{path}.issued_at")
        _text(item["statement"], f"{path}.statement")


def build_capsule(content: dict[str, Any], attestations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    _validate_content(content)
    digest = content_sha256(content)
    attestation_list = list(attestations or [])
    _validate_attestations(attestation_list, digest)
    return {"content": content, "content_sha256": digest,
            "attestations": attestation_list}


def _artifact_bytes(artifact: Mapping[str, Any], base_dir: str | Path | None) -> bytes:
    location = artifact["location"]
    if location["type"] == "inline":
        try:
            return base64.b64decode(location["data"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CapsuleError(f"invalid base64: {exc}") from exc
    if base_dir is None:
        raise CapsuleError("path artifact requires an explicit artifact root")
    raw = location["path"]
    if "\\" in raw:
        raise CapsuleError("artifact path must use portable '/' separators")
    rel = PurePosixPath(raw)
    if (rel.is_absolute() or not rel.parts or str(rel) != raw
            or any(part in {"", ".", ".."} for part in rel.parts)):
        raise CapsuleError("artifact path must be a clean relative path inside the artifact root")
    root = Path(base_dir).resolve(strict=True)
    target = root.joinpath(*rel.parts).resolve(strict=True)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CapsuleError("artifact path escapes the artifact root") from exc
    if not target.is_file():
        raise CapsuleError("artifact path is not a regular file")
    return target.read_bytes()


def _verify_artifacts(content: Mapping[str, Any], base_dir: str | Path | None) -> tuple[dict[str, Any], dict[str, bytes]]:
    items: list[dict[str, Any]] = []
    resolved: dict[str, bytes] = {}
    for index, raw in enumerate(content.get("artifacts", [])):
        aid = raw.get("id", f"artifact-{index}") if isinstance(raw, dict) else f"artifact-{index}"
        result: dict[str, Any] = {"id": aid, "status": "failed", "problems": []}
        try:
            artifact = _mapping(raw, f"content.artifacts[{index}]")
            data = _artifact_bytes(artifact, base_dir)
            actual = _sha256(data)
            result["actual_sha256"] = actual
            if actual != artifact.get("sha256"):
                result["problems"].append("sha256 mismatch")
            if len(data) != artifact.get("size"):
                result["problems"].append("size mismatch")
            if not result["problems"]:
                result["status"] = "verified"
                resolved[str(aid)] = data
        except (CapsuleError, KeyError, OSError, TypeError) as exc:
            result["problems"].append(str(exc))
        items.append(result)
    status = "verified" if all(item["status"] == "verified" for item in items) else "failed"
    return {"status": status, "items": items}, resolved


def _json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        # Reject malformed escape sequences rather than interpreting them loosely.
        if re.search(r"~(?![01])", raw_token):
            raise CapsuleError("malformed JSON pointer escape")
        if isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise CapsuleError("JSON pointer array token is not a canonical index")
            index = int(token)
            if index >= len(current):
                raise CapsuleError("JSON pointer array index is out of range")
            current = current[index]
        elif isinstance(current, dict):
            if token not in current:
                raise CapsuleError("JSON pointer object key is absent")
            current = current[token]
        else:
            raise CapsuleError("JSON pointer descends through a scalar")
    return current


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return bool(left == right)


def _verify_recomputability(content: Mapping[str, Any], artifacts: Mapping[str, bytes]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(content.get("derivations", [])):
        did = raw.get("id", f"derivation-{index}") if isinstance(raw, dict) else f"derivation-{index}"
        result: dict[str, Any] = {"id": did, "status": "failed", "problems": []}
        try:
            proof = _mapping(raw["proof"], f"derivation {did}.proof")
            proof_type = proof.get("type")
            if proof_type == "declared-nonrecomputable-v1":
                result["status"] = "not_recomputable"
                result["reason"] = proof.get("reason", "")
            elif proof_type == "json-pointer-equals-v1":
                ref = proof["artifact_ref"]
                if ref not in artifacts:
                    raise CapsuleError(f"input artifact {ref!r} did not verify")
                document = strict_json_loads(artifacts[ref])
                actual = _json_pointer(document, proof["pointer"])
                if not _json_equal(actual, proof["expected"]):
                    raise CapsuleError("JSON pointer value does not equal expected value")
                result["status"] = "verified"
            else:
                raise CapsuleError(f"unknown proof {proof_type!r}")
        except (CapsuleError, KeyError, TypeError) as exc:
            result["problems"].append(str(exc))
        items.append(result)
    statuses = [item["status"] for item in items]
    if not statuses:
        status = "not_applicable"
    elif "failed" in statuses:
        status = "failed"
    elif all(value == "verified" for value in statuses):
        status = "verified"
    elif all(value == "not_recomputable" for value in statuses):
        status = "not_recomputable"
    else:
        status = "partial"
    return {"status": status, "items": items}


def _entry_hash(entry: Mapping[str, Any]) -> str:
    committed = {name: entry[name] for name in
                 ("seq", "ts", "source", "payload_sha256", "prev_hash")}
    return _sha256(_ledger_canonical_bytes(committed))


def _verify_merkle(proof: Mapping[str, Any]) -> None:
    seq = proof["seq"]
    width = proof["n_entries"]
    if seq < 0 or seq >= width:
        raise CapsuleError("inclusion proof seq is outside its claimed prefix")
    index = seq
    current = proof["entry_hash"]
    path = proof["path"]
    step_index = 0
    while width > 1:
        if step_index >= len(path):
            raise CapsuleError("inclusion proof path is too short")
        step = path[step_index]
        expected_side = "right" if index % 2 == 0 else "left"
        if step["side"] != expected_side:
            raise CapsuleError("inclusion proof sibling side is inconsistent with seq")
        sibling = step["hash"]
        if index % 2 == 0 and index + 1 >= width and sibling != current:
            raise CapsuleError("odd-width duplicate-last sibling is not the current hash")
        pair = current + sibling if expected_side == "right" else sibling + current
        current = _sha256(pair.encode("ascii"))
        width = (width + 1) // 2
        index //= 2
        step_index += 1
    if step_index != len(path):
        raise CapsuleError("inclusion proof path is too long")
    if current != proof["merkle_root"]:
        raise CapsuleError("inclusion proof does not fold to the claimed root")


def _parse_anchor_input(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapsuleError("anchor input is not UTF-8") from exc
    fields: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line or " " not in line:
            raise CapsuleError(f"anchor input line {number} is malformed")
        key, value = line.split(" ", 1)
        if not _ANCHOR_KEY.fullmatch(key) or not value or key in fields:
            raise CapsuleError(f"anchor input line {number} is malformed/duplicate")
        fields[key] = value
    return fields


def _verify_ots_detached(data: bytes, subject: bytes) -> None:
    """Verify the detached file-hash header without interpreting the timestamp tree.

    OpenTimestamps detached files begin with a fixed magic value, version 1,
    SHA-256 operation tag 0x08, and the 32-byte digest of the exact input file.
    This binds proof bytes to the capsule's anchor input.  It deliberately does
    not evaluate calendars, attestations, or Bitcoin consensus.
    """
    prefix = _OTS_MAGIC + b"\x01\x08"
    if len(data) < len(prefix) + 32 or not data.startswith(prefix):
        raise CapsuleError("detached proof is not a supported OpenTimestamps v1 SHA-256 proof")
    if data[len(prefix):len(prefix) + 32] != hashlib.sha256(subject).digest():
        raise CapsuleError("detached OpenTimestamps proof commits to a different anchor input")


def _verify_bindings(content: Mapping[str, Any], artifacts: Mapping[str, bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger_items: list[dict[str, Any]] = []
    anchor_items: list[dict[str, Any]] = []
    for index, raw in enumerate(content.get("bindings", [])):
        ledger_result: dict[str, Any] = {
            "index": index, "status": "failed", "problems": [],
        }
        anchor_result: dict[str, Any] = {
            "index": index, "status": "failed", "problems": [],
            "cryptographic_timestamp": "not_verified_offline",
        }
        try:
            artifact_ref = raw["artifact_ref"]
            if artifact_ref not in artifacts:
                raise CapsuleError(f"bound artifact {artifact_ref!r} did not verify")
            reading = strict_json_loads(artifacts[artifact_ref])
            payload_digest = _sha256(_ledger_canonical_bytes(reading))
            entry = raw["entry"]
            if payload_digest != entry["payload_sha256"]:
                raise CapsuleError("artifact payload does not match ledger payload_sha256")
            if _entry_hash(entry) != entry["entry_hash"]:
                raise CapsuleError("full ledger entry hash does not recompute")
            proof = raw["inclusion_proof"]
            if proof["seq"] != entry["seq"] or proof["entry_hash"] != entry["entry_hash"]:
                raise CapsuleError("inclusion proof is for a different ledger entry")
            _verify_merkle(proof)
            ledger_result["status"] = "verified"
            ledger_result["seq"] = entry["seq"]
            ledger_result["root"] = proof["merkle_root"]
        except (CapsuleError, KeyError, TypeError) as exc:
            ledger_result["problems"].append(str(exc))

        try:
            anchor = raw["anchor"]
            proof = raw["inclusion_proof"]
            if anchor["prefix_entries"] != proof["n_entries"]:
                raise CapsuleError("anchor prefix length and inclusion prefix differ")
            if anchor["merkle_root"] != proof["merkle_root"]:
                raise CapsuleError("anchor root and inclusion root differ")
            if anchor["input_artifact_ref"] not in artifacts:
                raise CapsuleError("anchor input artifact did not verify")
            if anchor["proof_artifact_ref"] not in artifacts:
                raise CapsuleError("detached timestamp proof artifact did not verify")
            fields = _parse_anchor_input(artifacts[anchor["input_artifact_ref"]])
            prefix = anchor["ledger"]
            expected = {
                f"{prefix}_entries": str(anchor["prefix_entries"]),
                f"{prefix}_head": anchor["head_hash"],
                f"{prefix}_root": anchor["merkle_root"],
                "anchored_at": anchor["anchored_at"],
            }
            for key, value in expected.items():
                if fields.get(key) != value:
                    raise CapsuleError(f"anchor input field {key!r} does not match binding")
            detached = artifacts[anchor["proof_artifact_ref"]]
            _verify_ots_detached(detached, artifacts[anchor["input_artifact_ref"]])
            anchor_result["status"] = "bound"
            anchor_result["prefix_entries"] = anchor["prefix_entries"]
            anchor_result["root"] = anchor["merkle_root"]
        except (CapsuleError, KeyError, TypeError) as exc:
            anchor_result["problems"].append(str(exc))
        ledger_items.append(ledger_result)
        anchor_items.append(anchor_result)

    ledger_status = ("not_present" if not ledger_items else
                     "verified" if all(i["status"] == "verified" for i in ledger_items)
                     else "failed")
    anchor_status = ("not_present" if not anchor_items else
                     "bound" if all(i["status"] == "bound" for i in anchor_items)
                     else "failed")
    return ({"status": ledger_status, "items": ledger_items},
            {"status": anchor_status, "items": anchor_items,
             "meaning": ("exact anchor inputs and detached proof envelopes are bound; "
                         "the timestamp tree and Bitcoin attestation are not evaluated by this "
                         "offline stdlib verifier")})


def verify_capsule(capsule: Any, *, base_dir: str | Path | None = None) -> dict[str, Any]:
    """Verify a capsule without fetching or executing anything.

    ``base_dir`` is mandatory only for path-backed artifacts.  Paths are resolved
    beneath that directory after rejecting absolute paths, dot segments,
    backslashes, and symlink escapes.
    """
    errors: list[str] = []
    schema_status = "failed"
    content: Mapping[str, Any] = {}
    declared_digest = ""
    attestations: Any = []
    try:
        envelope = _fields(capsule, "capsule", {"content", "content_sha256", "attestations"})
        content = _mapping(envelope["content"], "content")
        declared_digest = _hex64(envelope["content_sha256"], "content_sha256")
        attestations = envelope["attestations"]
        _validate_content(content)
        _validate_attestations(attestations, declared_digest)
        schema_status = "verified"
    except CapsuleError as exc:
        errors.append(str(exc))

    canonicalization = content.get("canonicalization") if isinstance(content, dict) else None
    version = content.get("spec_version") if isinstance(content, dict) else None
    integrity: dict[str, Any]
    if version != SPEC_VERSION or canonicalization != CANONICALIZATION:
        integrity = {"status": "unsupported", "expected": declared_digest,
                     "actual": None}
    else:
        try:
            actual = content_sha256(content)
            integrity = {"status": "verified" if actual == declared_digest else "failed",
                         "expected": declared_digest, "actual": actual}
            if actual != declared_digest:
                errors.append("content_sha256 does not match canonical content")
        except CapsuleError as exc:
            integrity = {"status": "failed", "expected": declared_digest, "actual": None}
            errors.append(str(exc))

    artifacts, resolved = _verify_artifacts(content, base_dir)
    recomputability = _verify_recomputability(content, resolved)
    ledger, anchor = _verify_bindings(content, resolved)
    attestation_count = len(attestations) if isinstance(attestations, list) else 0
    attestation_report = {
        "status": "not_present" if not attestation_count else
                  ("bound_not_authenticated" if schema_status == "verified" else "failed"),
        "count": attestation_count,
        "meaning": "attestations target content_sha256 but v1 does not authenticate actors",
    }

    acceptable_recompute = recomputability["status"] in {
        "verified", "partial", "not_recomputable", "not_applicable",
    }
    ok = (
        schema_status == "verified" and integrity["status"] == "verified"
        and artifacts["status"] == "verified"
        and ledger["status"] in {"verified", "not_present"}
        and anchor["status"] in {"bound", "not_present"}
        and acceptable_recompute
    )
    if artifacts["status"] == "failed":
        errors.append("one or more artifacts failed exact-byte verification")
    if ledger["status"] == "failed":
        errors.append("one or more ledger bindings failed")
    if anchor["status"] == "failed":
        errors.append("one or more anchor bindings failed")
    if recomputability["status"] == "failed":
        errors.append("one or more derivation proofs failed")
    return {
        "ok": ok,
        "schema": {"status": schema_status},
        "integrity": integrity,
        "artifacts": artifacts,
        "ledger": ledger,
        "anchor": anchor,
        "recomputability": recomputability,
        "attestations": attestation_report,
        "errors": errors,
    }
