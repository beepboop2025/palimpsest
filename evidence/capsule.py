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
LEDGER_CANONICALIZATION = "palimpsest-ledger-python-json-v1"
MERKLE_PROOF = "palimpsest-merkle-duplicate-last-v1"
LEDGER_BINDING = "palimpsest-ledger-anchor-v1"
ANCHOR_PROOF = "opentimestamps-detached-envelope-v1"

# V1 is intentionally bounded so an offline verifier can safely accept hostile
# capsules.  These limits are part of the wire contract and mirrored in the
# normative schema and both repository implementations.
IJSON_SAFE_INTEGER = 9_007_199_254_740_991
MAX_CAPSULE_BYTES = 32 * 1024 * 1024
MAX_CANONICAL_CONTENT_BYTES = 24 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_INLINE_BASE64_CHARS = ((MAX_ARTIFACT_BYTES + 2) // 3) * 4
MAX_TEXT_CHARS = 16 * 1024
MAX_JSON_DEPTH = 32
MAX_COLLECTION_ITEMS = 512
MAX_ARTIFACTS = 64
MAX_CLAIMS = 128
MAX_DERIVATIONS = 128
MAX_INTENTS = 32
MAX_BINDINGS = 32
MAX_ATTESTATIONS = 64
MAX_OTS_BYTES = 1024 * 1024
MAX_OTS_DEPTH = 64
MAX_OTS_NODES = 2048
MAX_OTS_OP_ARG_BYTES = 4096
MAX_OTS_OP_RESULT_BYTES = 4096
MAX_OTS_HEXLIFY_INPUT_BYTES = 2048
MAX_OTS_ATTESTATION_BYTES = 8192
MAX_OTS_PENDING_URI_BYTES = 1000

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
_OTS_PENDING_ATTESTATION = bytes.fromhex("83dfe30d2ef90c8e")
_OTS_BITCOIN_ATTESTATION = bytes.fromhex("0588960d73d71901")
_OTS_LITECOIN_ATTESTATION = bytes.fromhex("06869a0d73d71b45")
_OTS_PENDING_URI_CHARS = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._/:"
)


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


def _bounded_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > 16:
        raise CapsuleError("JSON integer exceeds the I-JSON interoperable range")
    parsed = int(value)
    if abs(parsed) > IJSON_SAFE_INTEGER:
        raise CapsuleError("JSON integer exceeds the I-JSON interoperable range")
    return parsed


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CapsuleError(f"JSON number overflows to a non-finite value: {value}")
    return parsed


def _check_unicode_scalar_text(value: str, path: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CapsuleError(f"{path}: lone Unicode surrogate is forbidden")


def _check_json_shape(value: Any, path: str = "json", depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise CapsuleError(f"{path}: JSON nesting exceeds {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > IJSON_SAFE_INTEGER:
            raise CapsuleError(f"{path}: integer exceeds the I-JSON interoperable range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CapsuleError(f"{path}: non-finite JSON number is forbidden")
        return
    if isinstance(value, str):
        if len(value) > MAX_INLINE_BASE64_CHARS:
            raise CapsuleError(f"{path}: string exceeds the v1 resource limit")
        _check_unicode_scalar_text(value, path)
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise CapsuleError(f"{path}: array exceeds {MAX_COLLECTION_ITEMS} items")
        for index, item in enumerate(value):
            _check_json_shape(item, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise CapsuleError(f"{path}: object exceeds {MAX_COLLECTION_ITEMS} members")
        for key, item in value.items():
            if not isinstance(key, str):
                raise CapsuleError(f"{path}: object keys must be strings")
            if len(key) > MAX_TEXT_CHARS:
                raise CapsuleError(f"{path}: object key exceeds the v1 resource limit")
            _check_unicode_scalar_text(key, f"{path}: object key")
            _check_json_shape(item, f"{path}.{key}", depth + 1)
        return
    raise CapsuleError(f"{path}: unsupported JSON value {type(value).__name__}")


def strict_json_loads(data: bytes | str) -> Any:
    """Parse JSON while rejecting duplicate keys and NaN/Infinity."""
    try:
        if isinstance(data, bytes):
            if len(data) > MAX_CAPSULE_BYTES:
                raise CapsuleError("JSON input exceeds the v1 byte limit")
            text = data.decode("utf-8")
        elif isinstance(data, str):
            if len(data) > MAX_CAPSULE_BYTES:
                raise CapsuleError("JSON input exceeds the v1 byte limit")
            if len(data.encode("utf-8")) > MAX_CAPSULE_BYTES:
                raise CapsuleError("JSON input exceeds the v1 byte limit")
            text = data
        else:
            raise CapsuleError("JSON input must be bytes or text")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise CapsuleError(f"JSON is not UTF-8: {exc}") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            parse_int=_bounded_int,
        )
        _check_json_shape(parsed)
        return parsed
    except CapsuleError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise CapsuleError(f"invalid JSON: {exc}") from exc


def load_capsule(path: str | Path) -> dict[str, Any]:
    capsule_path = Path(path)
    try:
        with capsule_path.open("rb") as handle:
            data = handle.read(MAX_CAPSULE_BYTES + 1)
    except OSError as exc:
        raise CapsuleError(f"capsule cannot be read: {exc}") from exc
    if len(data) > MAX_CAPSULE_BYTES:
        raise CapsuleError("capsule exceeds the v1 byte limit")
    value = strict_json_loads(data)
    if not isinstance(value, dict):
        raise CapsuleError("capsule envelope must be a JSON object")
    return value


def _check_canonical_value(value: Any, path: str = "content", depth: int = 0) -> None:
    """v1 content deliberately excludes floats for cross-runtime stability."""
    if depth > MAX_JSON_DEPTH:
        raise CapsuleError(f"{path}: JSON nesting exceeds {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > MAX_INLINE_BASE64_CHARS:
            raise CapsuleError(f"{path}: string exceeds the v1 resource limit")
        _check_unicode_scalar_text(value, path)
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > IJSON_SAFE_INTEGER:
            raise CapsuleError(f"{path}: integer exceeds the I-JSON interoperable range")
        return
    if isinstance(value, float):
        raise CapsuleError(
            f"{path}: floats are forbidden in hashed content; encode decimals as strings"
        )
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise CapsuleError(f"{path}: array exceeds {MAX_COLLECTION_ITEMS} items")
        for index, item in enumerate(value):
            _check_canonical_value(item, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise CapsuleError(f"{path}: object exceeds {MAX_COLLECTION_ITEMS} members")
        for key, item in value.items():
            if not isinstance(key, str):
                raise CapsuleError(f"{path}: object keys must be strings")
            if len(key) > MAX_TEXT_CHARS:
                raise CapsuleError(f"{path}: object key exceeds the v1 resource limit")
            _check_unicode_scalar_text(key, f"{path}: object key")
            _check_canonical_value(item, f"{path}.{key}", depth + 1)
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
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CapsuleError(f"content cannot be canonicalized: {exc}") from exc
    if len(encoded) > MAX_CANONICAL_CONTENT_BYTES:
        raise CapsuleError("canonical content exceeds the v1 byte limit")
    return encoded


def _ledger_canonical_bytes(value: Any) -> bytes:
    """Match Palimpsest's explicitly Python-specific historical serialization."""
    _check_json_shape(value, "ledger_payload")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CapsuleError(f"ledger payload cannot be canonicalized: {exc}") from exc
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise CapsuleError("canonical ledger payload exceeds the v1 artifact limit")
    return encoded


def content_sha256(content: Any) -> str:
    return _sha256(canonical_bytes(content))


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CapsuleError(f"{path}: expected object")
    return value


def _list(value: Any, path: str, *, maximum: int = MAX_COLLECTION_ITEMS) -> list[Any]:
    if not isinstance(value, list):
        raise CapsuleError(f"{path}: expected array")
    if len(value) > maximum:
        raise CapsuleError(f"{path}: exceeds {maximum} items")
    return value


def _text(
    value: Any,
    path: str,
    *,
    nonempty: bool = True,
    maximum: int = MAX_TEXT_CHARS,
) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise CapsuleError(f"{path}: expected {'non-empty ' if nonempty else ''}string")
    if len(value) > maximum:
        raise CapsuleError(f"{path}: exceeds {maximum} characters")
    _check_unicode_scalar_text(value, path)
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = IJSON_SAFE_INTEGER,
) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < minimum or value > maximum):
        raise CapsuleError(f"{path}: expected integer in [{minimum}, {maximum}]")
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
    if (not isinstance(subject["type"], str)
            or subject["type"] not in {"reading", "analytical-lead", "evidence-set"}):
        raise CapsuleError(f"content.subject.type: unknown subject type {subject['type']!r}")
    _text(subject["id"], "content.subject.id")
    _text(subject["title"], "content.subject.title")

    artifact_ids: set[str] = set()
    artifact_untrusted: dict[str, bool] = {}
    declared_artifact_bytes = 0
    artifact_values = _list(
        obj["artifacts"], "content.artifacts", maximum=MAX_ARTIFACTS
    )
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
        declared_artifact_bytes += _integer(
            artifact["size"], f"{path}.size", maximum=MAX_ARTIFACT_BYTES
        )
        if declared_artifact_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise CapsuleError(
                f"content.artifacts: declared bytes exceed {MAX_TOTAL_ARTIFACT_BYTES}"
            )
        media = _text(artifact["media_type"], f"{path}.media_type")
        if not _MEDIA.fullmatch(media):
            raise CapsuleError(f"{path}.media_type: invalid media type")
        if not isinstance(artifact["untrusted"], bool):
            raise CapsuleError(f"{path}.untrusted: expected boolean")
        artifact_untrusted[aid] = artifact["untrusted"]
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
            _text(
                location["data"], f"{path}.location.data",
                nonempty=False, maximum=MAX_INLINE_BASE64_CHARS,
            )
        elif location_type == "path":
            location = _fields(location, f"{path}.location", {"type", "path"})
            _text(location["path"], f"{path}.location.path", maximum=4096)
        else:
            raise CapsuleError(f"{path}.location.type: unknown location {location_type!r}")

    claim_ids: set[str] = set()
    claims = _list(obj["claims"], "content.claims", maximum=MAX_CLAIMS)
    if not claims:
        raise CapsuleError("content.claims: at least one typed claim is required")
    claim_derivation_refs: dict[str, list[str]] = {}
    claim_binding_refs: dict[str, list[str]] = {}
    claim_artifact_refs: dict[str, list[str]] = {}
    for index, raw in enumerate(claims):
        path = f"content.claims[{index}]"
        claim = _fields(raw, path, {
            "id", "type", "statement", "artifact_refs", "derivation_refs",
            "binding_refs", "evidence_level", "limitations",
        })
        cid = _id(claim["id"], f"{path}.id")
        if cid in claim_ids:
            raise CapsuleError(f"{path}.id: duplicate claim id {cid!r}")
        claim_ids.add(cid)
        if not isinstance(claim["type"], str) or claim["type"] not in CLAIM_TYPES:
            raise CapsuleError(f"{path}.type: unknown claim type {claim['type']!r}")
        _text(claim["statement"], f"{path}.statement")
        artifact_refs = _string_refs(claim["artifact_refs"], f"{path}.artifact_refs")
        if not artifact_refs:
            raise CapsuleError(f"{path}.artifact_refs: at least one artifact is required")
        claim_artifact_refs[cid] = artifact_refs
        for ref in artifact_refs:
            if ref not in artifact_ids:
                raise CapsuleError(f"{path}.artifact_refs: unknown artifact {ref!r}")
        claim_derivation_refs[cid] = _string_refs(
            claim["derivation_refs"], f"{path}.derivation_refs"
        )
        claim_binding_refs[cid] = _string_refs(
            claim["binding_refs"], f"{path}.binding_refs"
        )
        if (not isinstance(claim["evidence_level"], str)
                or claim["evidence_level"] not in EVIDENCE_LEVELS):
            raise CapsuleError(f"{path}.evidence_level: unknown level {claim['evidence_level']!r}")
        if claim["evidence_level"] == "derived" and not claim_derivation_refs[cid]:
            raise CapsuleError(f"{path}: derived evidence requires a derivation reference")
        limitations = _list(claim["limitations"], f"{path}.limitations")
        for li, limitation in enumerate(limitations):
            _text(limitation, f"{path}.limitations[{li}]")

    derivation_ids: set[str] = set()
    derivation_claim_refs: dict[str, list[str]] = {}
    derivation_input_refs: dict[str, list[str]] = {}
    for index, raw in enumerate(_list(
        obj["derivations"], "content.derivations", maximum=MAX_DERIVATIONS
    )):
        path = f"content.derivations[{index}]"
        derivation = _fields(raw, path, {
            "id", "type", "description", "input_artifact_refs",
            "supports_claim_refs", "proof",
        })
        did = _id(derivation["id"], f"{path}.id")
        if did in derivation_ids:
            raise CapsuleError(f"{path}.id: duplicate derivation id {did!r}")
        derivation_ids.add(did)
        if (not isinstance(derivation["type"], str)
                or derivation["type"] not in DERIVATION_TYPES):
            raise CapsuleError(f"{path}.type: unknown derivation type {derivation['type']!r}")
        _text(derivation["description"], f"{path}.description")
        input_refs = _string_refs(derivation["input_artifact_refs"],
                                  f"{path}.input_artifact_refs")
        if not input_refs:
            raise CapsuleError(f"{path}.input_artifact_refs: at least one input is required")
        derivation_input_refs[did] = input_refs
        for ref in input_refs:
            if ref not in artifact_ids:
                raise CapsuleError(f"{path}: unknown input artifact {ref!r}")
        claim_refs = _string_refs(derivation["supports_claim_refs"],
                                  f"{path}.supports_claim_refs")
        if not claim_refs:
            raise CapsuleError(f"{path}.supports_claim_refs: at least one claim is required")
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
            unlisted_inputs = set(derivation_input_refs[ref]) - set(claim_artifact_refs[claim_id])
            if unlisted_inputs:
                raise CapsuleError(
                    f"claim {claim_id!r}: derivation {ref!r} uses artifact(s) not cited "
                    f"by the claim: {', '.join(sorted(unlisted_inputs))}"
                )
    for derivation_id, refs in derivation_claim_refs.items():
        for claim_id in refs:
            if derivation_id not in claim_derivation_refs[claim_id]:
                raise CapsuleError(
                    f"derivation {derivation_id!r} and claim {claim_id!r} do not reference each other"
                )

    for index, raw in enumerate(_list(
        obj["intents"], "content.intents", maximum=MAX_INTENTS
    )):
        path = f"content.intents[{index}]"
        intent = _fields(raw, path, {"type", "summary", "advisory"})
        if not isinstance(intent["type"], str) or intent["type"] not in INTENT_TYPES:
            raise CapsuleError(f"{path}.type: unknown/non-advisory action {intent['type']!r}")
        _text(intent["summary"], f"{path}.summary")
        if intent["advisory"] is not True:
            raise CapsuleError(f"{path}.advisory: v1 intents must be true")

    binding_artifacts: dict[str, str] = {}
    for index, raw in enumerate(_list(
        obj["bindings"], "content.bindings", maximum=MAX_BINDINGS
    )):
        binding_id, artifact_ref = _validate_binding(
            raw, f"content.bindings[{index}]", artifact_ids, artifact_untrusted
        )
        if binding_id in binding_artifacts:
            raise CapsuleError(f"content.bindings[{index}].id: duplicate binding id")
        binding_artifacts[binding_id] = artifact_ref

    for claim_id, refs in claim_binding_refs.items():
        for ref in refs:
            if ref not in binding_artifacts:
                raise CapsuleError(f"claim {claim_id!r}: unknown binding {ref!r}")
            if binding_artifacts[ref] not in claim_artifact_refs[claim_id]:
                raise CapsuleError(
                    f"claim {claim_id!r}: binding {ref!r} binds artifact "
                    f"{binding_artifacts[ref]!r}, which the claim does not cite"
                )

    # Hashed content must satisfy the canonical domain after structural checks.
    _check_canonical_value(obj)


def _validate_binding(
    raw: Any,
    path: str,
    artifact_ids: set[str],
    artifact_untrusted: Mapping[str, bool],
) -> tuple[str, str]:
    binding = _fields(raw, path, {
        "id", "type", "artifact_ref", "payload_canonicalization", "entry",
        "inclusion_proof", "anchor",
    })
    binding_id = _id(binding["id"], f"{path}.id")
    if binding["type"] != LEDGER_BINDING:
        raise CapsuleError(f"{path}.type: unknown binding {binding['type']!r}")
    if binding["artifact_ref"] not in artifact_ids:
        raise CapsuleError(f"{path}.artifact_ref: unknown artifact")
    artifact_ref = binding["artifact_ref"]
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
    for pi, raw_step in enumerate(_list(
        proof["path"], f"{path}.inclusion_proof.path", maximum=64
    )):
        step = _fields(raw_step, f"{path}.inclusion_proof.path[{pi}]", {"side", "hash"})
        if not isinstance(step["side"], str) or step["side"] not in {"left", "right"}:
            raise CapsuleError(f"{path}.inclusion_proof.path[{pi}].side: unknown side")
        _hex64(step["hash"], f"{path}.inclusion_proof.path[{pi}].hash")
    anchor = _fields(binding["anchor"], f"{path}.anchor", {
        "type", "ledger", "prefix_entries", "head_hash", "merkle_root",
        "anchored_at", "input_artifact_ref", "proof_artifact_ref", "proof_type",
    })
    if anchor["type"] != "palimpsest-prefix-anchor-v1":
        raise CapsuleError(f"{path}.anchor.type: unknown anchor type {anchor['type']!r}")
    ledger_id = _id(anchor["ledger"], f"{path}.anchor.ledger")
    if not _ANCHOR_KEY.fullmatch(ledger_id):
        raise CapsuleError(f"{path}.anchor.ledger: must be usable as an anchor-input key")
    _integer(anchor["prefix_entries"], f"{path}.anchor.prefix_entries", minimum=1)
    _hex64(anchor["head_hash"], f"{path}.anchor.head_hash")
    _hex64(anchor["merkle_root"], f"{path}.anchor.merkle_root")
    _timestamp(anchor["anchored_at"], f"{path}.anchor.anchored_at")
    for field in ("input_artifact_ref", "proof_artifact_ref"):
        if anchor[field] not in artifact_ids:
            raise CapsuleError(f"{path}.anchor.{field}: unknown artifact")
    if artifact_untrusted[anchor["proof_artifact_ref"]] is not True:
        raise CapsuleError(
            f"{path}.anchor.proof_artifact_ref: detached proof artifact "
            "must declare untrusted true"
        )
    if anchor["proof_type"] != ANCHOR_PROOF:
        raise CapsuleError(f"{path}.anchor.proof_type: unknown proof {anchor['proof_type']!r}")
    return binding_id, artifact_ref


def _validate_attestations(attestations: Any, digest: str) -> None:
    for index, raw in enumerate(_list(
        attestations, "attestations", maximum=MAX_ATTESTATIONS
    )):
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
        if len(location["data"]) > MAX_INLINE_BASE64_CHARS:
            raise CapsuleError("inline artifact exceeds the v1 encoded-size limit")
        try:
            data = base64.b64decode(location["data"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CapsuleError(f"invalid base64: {exc}") from exc
        if len(data) > MAX_ARTIFACT_BYTES:
            raise CapsuleError("inline artifact exceeds the v1 decoded-size limit")
        return data
    if base_dir is None:
        raise CapsuleError("path artifact requires an explicit artifact root")
    raw = location["path"]
    if "\\" in raw:
        raise CapsuleError("artifact path must use portable '/' separators")
    rel = PurePosixPath(raw)
    if (rel.is_absolute() or not rel.parts or str(rel) != raw
            or any(part in {"", ".", ".."} for part in rel.parts)):
        raise CapsuleError("artifact path must be a clean relative path inside the artifact root")
    try:
        root = Path(base_dir).resolve(strict=True)
        target = root.joinpath(*rel.parts).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CapsuleError(f"artifact path cannot be resolved safely: {exc}") from exc
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CapsuleError("artifact path escapes the artifact root") from exc
    if not target.is_file():
        raise CapsuleError("artifact path is not a regular file")
    try:
        with target.open("rb") as handle:
            data = handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise CapsuleError(f"artifact path cannot be read: {exc}") from exc
    if len(data) > MAX_ARTIFACT_BYTES:
        raise CapsuleError("path artifact exceeds the v1 byte limit")
    return data


def _verify_artifacts(content: Mapping[str, Any], base_dir: str | Path | None) -> tuple[dict[str, Any], dict[str, bytes]]:
    items: list[dict[str, Any]] = []
    resolved: dict[str, bytes] = {}
    total_bytes = 0
    for index, raw in enumerate(content.get("artifacts", [])):
        aid = raw.get("id", f"artifact-{index}") if isinstance(raw, dict) else f"artifact-{index}"
        result: dict[str, Any] = {"id": aid, "status": "failed", "problems": []}
        try:
            artifact = _mapping(raw, f"content.artifacts[{index}]")
            data = _artifact_bytes(artifact, base_dir)
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
                result["problems"].append(
                    "total verified artifact bytes exceed the v1 limit"
                )
                items.append(result)
                return {"status": "failed", "items": items}, {}
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
            if (not re.fullmatch(r"[0-9]+", token)
                    or (len(token) > 1 and token.startswith("0"))):
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


class _OTSCursor:
    """Small bounded reader for the stable OpenTimestamps serialization grammar."""

    def __init__(self, data: bytes, offset: int) -> None:
        self.data = data
        self.offset = offset
        self.nodes = 0
        self.attestations = 0

    def read(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise CapsuleError("detached OpenTimestamps envelope is truncated")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def read_varuint(
        self, maximum: int | None = None, *, label: str = "length"
    ) -> int:
        value = 0
        shift = 0
        count = 0
        while True:
            byte = self.read(1)[0]
            count += 1
            if count > 10:
                raise CapsuleError("OpenTimestamps varuint exceeds the v1 limit")
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                if count > 1 and byte == 0:
                    raise CapsuleError("OpenTimestamps varuint is not minimally encoded")
                break
            shift += 7
        if maximum is not None and value > maximum:
            raise CapsuleError(f"OpenTimestamps {label} exceeds the v1 limit")
        return value

    def read_varbytes(self, maximum: int, *, minimum: int = 0) -> bytes:
        size = self.read_varuint(maximum)
        if size < minimum:
            raise CapsuleError("OpenTimestamps byte string is shorter than allowed")
        return self.read(size)


def _parse_ots_attestation(cursor: _OTSCursor) -> None:
    """Parse known attestation payloads exactly; keep unknown tags opaque."""
    attestation_tag = cursor.read(8)
    payload = cursor.read_varbytes(MAX_OTS_ATTESTATION_BYTES)
    if attestation_tag not in {
        _OTS_PENDING_ATTESTATION,
        _OTS_BITCOIN_ATTESTATION,
        _OTS_LITECOIN_ATTESTATION,
    }:
        return

    payload_cursor = _OTSCursor(payload, 0)
    if attestation_tag == _OTS_PENDING_ATTESTATION:
        uri = payload_cursor.read_varbytes(MAX_OTS_PENDING_URI_BYTES)
        if any(character not in _OTS_PENDING_URI_CHARS for character in uri):
            raise CapsuleError("OpenTimestamps pending attestation URI is invalid")
        label = "pending attestation"
    else:
        payload_cursor.read_varuint(label="block height")
        label = (
            "Bitcoin attestation"
            if attestation_tag == _OTS_BITCOIN_ATTESTATION
            else "Litecoin attestation"
        )
    if payload_cursor.offset != len(payload):
        raise CapsuleError(f"OpenTimestamps {label} payload has trailing bytes")


def _parse_ots_timestamp(
    cursor: _OTSCursor, message_length: int, depth: int = 0
) -> None:
    """Parse, but do not authenticate, the complete OTS timestamp tree."""
    if depth > MAX_OTS_DEPTH:
        raise CapsuleError("OpenTimestamps tree exceeds the v1 depth limit")

    def parse_node(tag: int) -> None:
        cursor.nodes += 1
        if cursor.nodes > MAX_OTS_NODES:
            raise CapsuleError("OpenTimestamps tree exceeds the v1 node limit")
        if tag == 0x00:
            _parse_ots_attestation(cursor)
            cursor.attestations += 1
            return
        if message_length > MAX_OTS_OP_RESULT_BYTES:
            raise CapsuleError("OpenTimestamps operation input exceeds 4096 bytes")
        if tag in {0xF0, 0xF1}:  # append / prepend
            argument = cursor.read_varbytes(MAX_OTS_OP_ARG_BYTES, minimum=1)
            result_length = message_length + len(argument)
        elif tag in {0x02, 0x03}:
            result_length = 20
        elif tag in {0x08, 0x67}:
            result_length = 32
        elif tag == 0xF2:  # reverse
            result_length = message_length
        elif tag == 0xF3:  # hexlify
            if message_length > MAX_OTS_HEXLIFY_INPUT_BYTES:
                raise CapsuleError("OpenTimestamps hexlify input exceeds 2048 bytes")
            result_length = message_length * 2
        else:
            raise CapsuleError(f"unknown OpenTimestamps operation tag 0x{tag:02x}")
        if not result_length or result_length > MAX_OTS_OP_RESULT_BYTES:
            raise CapsuleError("OpenTimestamps operation result exceeds 4096 bytes")
        _parse_ots_timestamp(cursor, result_length, depth + 1)

    tag = cursor.read(1)[0]
    while tag == 0xFF:
        parse_node(cursor.read(1)[0])
        tag = cursor.read(1)[0]
    parse_node(tag)


def _verify_ots_detached(data: bytes, subject: bytes) -> dict[str, int]:
    """Validate a complete detached envelope and bind it to ``subject``.

    This parses the bounded v1 timestamp-tree serialization through EOF. It does
    not contact calendars, authenticate attestations, inspect block headers, or
    validate Bitcoin consensus, so it must never be reported as a timestamp.
    """
    if len(data) > MAX_OTS_BYTES:
        raise CapsuleError("detached OpenTimestamps envelope exceeds the v1 byte limit")
    prefix = _OTS_MAGIC + b"\x01\x08"
    if len(data) <= len(prefix) + 32 or not data.startswith(prefix):
        raise CapsuleError(
            "detached envelope is not a complete OpenTimestamps v1 SHA-256 serialization"
        )
    if data[len(prefix):len(prefix) + 32] != hashlib.sha256(subject).digest():
        raise CapsuleError("detached OpenTimestamps envelope commits to a different anchor input")
    cursor = _OTSCursor(data, len(prefix) + 32)
    _parse_ots_timestamp(cursor, 32)
    if cursor.offset != len(data):
        raise CapsuleError("detached OpenTimestamps envelope has trailing bytes")
    if not cursor.attestations:
        raise CapsuleError("detached OpenTimestamps envelope contains no attestation")
    return {"nodes": cursor.nodes, "attestations": cursor.attestations}


def _verify_bindings(content: Mapping[str, Any], artifacts: Mapping[str, bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger_items: list[dict[str, Any]] = []
    anchor_items: list[dict[str, Any]] = []
    for index, raw in enumerate(content.get("bindings", [])):
        binding_id = raw.get("id", f"binding-{index}") if isinstance(raw, dict) else f"binding-{index}"
        ledger_result: dict[str, Any] = {
            "id": binding_id, "index": index, "status": "failed", "problems": [],
            "entry_integrity": "failed", "membership": "failed",
            "chain_integrity": "not_verifiable_from_capsule",
        }
        anchor_result: dict[str, Any] = {
            "id": binding_id, "index": index, "status": "failed", "problems": [],
            "cryptographic_timestamp": "not_verified",
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
            ledger_result["entry_integrity"] = "verified"
            proof = raw["inclusion_proof"]
            if proof["seq"] != entry["seq"] or proof["entry_hash"] != entry["entry_hash"]:
                raise CapsuleError("inclusion proof is for a different ledger entry")
            _verify_merkle(proof)
            ledger_result["membership"] = "verified"
            ledger_result["status"] = "entry_membership_verified"
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
            entry = raw["entry"]
            if (entry["seq"] == anchor["prefix_entries"] - 1
                    and anchor["head_hash"] != entry["entry_hash"]):
                raise CapsuleError("anchor head differs from the final included entry")
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
            ots_shape = _verify_ots_detached(
                detached, artifacts[anchor["input_artifact_ref"]]
            )
            anchor_result["status"] = "envelope_bound"
            anchor_result["structure"] = ots_shape
            anchor_result["prefix_entries"] = anchor["prefix_entries"]
            anchor_result["root"] = anchor["merkle_root"]
        except (CapsuleError, KeyError, TypeError) as exc:
            anchor_result["problems"].append(str(exc))
        ledger_items.append(ledger_result)
        anchor_items.append(anchor_result)

    ledger_status = ("not_present" if not ledger_items else
                     "entry_membership_verified" if all(
                         i["status"] == "entry_membership_verified" for i in ledger_items
                     )
                     else "failed")
    anchor_status = ("not_present" if not anchor_items else
                     "envelope_bound" if all(
                         i["status"] == "envelope_bound" for i in anchor_items
                     )
                     else "failed")
    return ({"status": ledger_status, "items": ledger_items,
             "meaning": ("each bound artifact matches a recomputed entry and Merkle "
                         "membership proof; the capsule does not carry enough entries to "
                         "verify the ledger hash chain")},
            {"status": anchor_status, "items": anchor_items,
             "meaning": ("exact anchor inputs are digest-bound to structurally complete "
                         "detached OpenTimestamps envelopes; calendar acceptance, attestation "
                         "authenticity, block headers, consensus, and time are not verified")})


def _verify_claim_support(
    content: Mapping[str, Any],
    artifact_report: Mapping[str, Any],
    recomputability: Mapping[str, Any],
    ledger: Mapping[str, Any],
    anchor: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve every typed claim to its exact referenced verification results."""
    artifact_status = {item["id"]: item["status"] for item in artifact_report["items"]}
    derivation_status = {item["id"]: item["status"] for item in recomputability["items"]}
    ledger_status = {item["id"]: item["status"] for item in ledger["items"]}
    anchor_status = {item["id"]: item["status"] for item in anchor["items"]}
    items: list[dict[str, Any]] = []
    for claim in content["claims"]:
        problems: list[str] = []
        partial = False
        for ref in claim["artifact_refs"]:
            if artifact_status.get(ref) != "verified":
                problems.append(f"artifact {ref!r} did not verify")
        for ref in claim["derivation_refs"]:
            status = derivation_status.get(ref)
            if status == "not_recomputable":
                partial = True
            elif status != "verified":
                problems.append(f"derivation {ref!r} did not verify")
        for ref in claim["binding_refs"]:
            if ledger_status.get(ref) != "entry_membership_verified":
                problems.append(f"binding {ref!r} entry/membership did not verify")
            if anchor_status.get(ref) != "envelope_bound":
                problems.append(f"binding {ref!r} anchor envelope did not bind")
        status = (
            "failed" if problems else
            "references_partially_recomputable" if partial else
            "references_verified"
        )
        items.append({
            "id": claim["id"],
            "status": status,
            "artifact_refs": list(claim["artifact_refs"]),
            "derivation_refs": list(claim["derivation_refs"]),
            "binding_refs": list(claim["binding_refs"]),
            "natural_language_truth": "not_evaluated",
            "problems": problems,
        })
    statuses = [item["status"] for item in items]
    status = (
        "failed" if "failed" in statuses else
        "references_partially_recomputable"
        if "references_partially_recomputable" in statuses else
        "references_verified"
    )
    return {
        "status": status,
        "items": items,
        "meaning": (
            "referenced bytes and declared machine proofs are checked per claim; "
            "free-text claim truth, source truth, and producer identity are not evaluated"
        ),
    }


def verify_capsule(capsule: Any, *, base_dir: str | Path | None = None) -> dict[str, Any]:
    """Verify a capsule without fetching or executing anything.

    ``base_dir`` is mandatory only for path-backed artifacts.  Paths are resolved
    beneath that directory after rejecting absolute paths, dot segments,
    backslashes, and symlink escapes.  The caller must keep that artifact root
    stable for the duration of verification and must not let an untrusted local
    process modify it concurrently; portable path resolution cannot close a
    check/open race in an adversary-writable directory on every supported OS.
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
    except (CapsuleError, KeyError, TypeError, ValueError, OverflowError,
            RecursionError) as exc:
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

    if schema_status == "verified":
        try:
            artifacts, resolved = _verify_artifacts(content, base_dir)
            recomputability = _verify_recomputability(content, resolved)
            ledger, anchor = _verify_bindings(content, resolved)
            claims = _verify_claim_support(
                content, artifacts, recomputability, ledger, anchor
            )
        except (CapsuleError, KeyError, TypeError, ValueError, OverflowError,
                RecursionError, RuntimeError, OSError) as exc:
            errors.append(f"bounded verification stage failed: {exc}")
            artifacts = {"status": "failed", "items": []}
            recomputability = {"status": "failed", "items": []}
            ledger = {"status": "failed", "items": []}
            anchor = {"status": "failed", "items": []}
            claims = {"status": "failed", "items": []}
    else:
        not_evaluated = {
            "status": "not_evaluated",
            "items": [],
            "meaning": "schema validation failed; dependent checks were not run",
        }
        artifacts = dict(not_evaluated)
        recomputability = dict(not_evaluated)
        ledger = dict(not_evaluated)
        anchor = dict(not_evaluated)
        claims = dict(not_evaluated)
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
        and ledger["status"] in {"entry_membership_verified", "not_present"}
        and anchor["status"] in {"envelope_bound", "not_present"}
        and acceptable_recompute
        and claims["status"] in {
            "references_verified", "references_partially_recomputable",
        }
    )
    if artifacts["status"] == "failed":
        errors.append("one or more artifacts failed exact-byte verification")
    if ledger["status"] == "failed":
        errors.append("one or more ledger bindings failed")
    if anchor["status"] == "failed":
        errors.append("one or more anchor bindings failed")
    if recomputability["status"] == "failed":
        errors.append("one or more derivation proofs failed")
    if claims["status"] == "failed":
        errors.append("one or more claims have failed evidence references")
    return {
        "ok": ok,
        "ok_scope": (
            "well-formed content identity, exact artifact bytes, and declared reference/proof "
            "relationships only; not natural-language truth, producer authentication, full "
            "ledger-chain integrity, or a cryptographic timestamp"
        ),
        "schema": {"status": schema_status},
        "integrity": integrity,
        "artifacts": artifacts,
        "ledger": ledger,
        "anchor": anchor,
        "recomputability": recomputability,
        "claims": claims,
        "attestations": attestation_report,
        "errors": errors,
    }
