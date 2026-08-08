"""Adapter from ScamShield provenance assessments to Evidence Capsule v1.

The adapter preserves a ScamShield assessment as inert bytes and creates typed
claims only about what that assessment *records*.  It does not reclassify the
message, fetch a source URL, evaluate natural-language truth, or promote a
typology match into a source-of-funds finding.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import Any, Mapping

from .capsule import (
    CANONICALIZATION,
    SPEC_VERSION,
    CapsuleError,
    build_capsule,
    strict_json_loads,
    verify_capsule,
)

ASSESSMENT_SCHEMA = "scamshield-provenance/v1"
MAX_ASSESSMENT_BYTES = 1024 * 1024
MAX_HYPOTHESES = 32
SUPPORT_LEVELS = {"TYPOLOGY_MATCH", "CORROBORATED_LEAD", "DIRECT_LINK"}
DIMENSIONS = {"laundering_mechanism", "operating_ecosystem", "predicate_offence"}
_HEX24 = re.compile(r"^[0-9a-f]{24}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_IOC_KINDS = {"handles", "phones", "channels", "wallets", "emails", "urls"}
_SCRIPT_HINTS = {"latin", "devanagari", "han", "arabic", "cyrillic", "undetermined"}


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CapsuleError(f"{field} must be an object")
    return value


def _array(value: Any, field: str, *, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise CapsuleError(f"{field} must be an array of at most {maximum} items")
    return value


def _text(value: Any, field: str, *, maximum: int = 16_384) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CapsuleError(f"{field} must be a non-empty bounded string")
    return value


def _identifiers(value: Any, field: str, *, maximum: int) -> list[str]:
    items = _array(value, field, maximum=maximum)
    for index, item in enumerate(items):
        if not isinstance(item, str) or not _IDENTIFIER.fullmatch(item):
            raise CapsuleError(f"{field}[{index}] must be a bounded identifier")
    return items


def _validate_assessment(value: Any) -> Mapping[str, Any]:
    root = _mapping(value, "assessment")
    required = {
        "schema_version", "assessment_id", "created_at", "message_sha256",
        "detector", "threat_assessment", "collection", "market_rate",
        "intelligence_pack", "hypotheses", "origin_answer", "abstentions",
        "limitations",
    }
    missing = required - set(root)
    if missing:
        raise CapsuleError(f"assessment missing fields: {sorted(missing)}")
    if root["schema_version"] != ASSESSMENT_SCHEMA:
        raise CapsuleError("unsupported ScamShield assessment schema")
    assessment_id = _text(root["assessment_id"], "assessment.assessment_id", maximum=24)
    if not _HEX24.fullmatch(assessment_id):
        raise CapsuleError("assessment_id is not 24 lowercase hex characters")
    message_hash = _text(root["message_sha256"], "assessment.message_sha256", maximum=64)
    if not _HEX64.fullmatch(message_hash):
        raise CapsuleError("message_sha256 is not 64 lowercase hex characters")
    _text(root["created_at"], "assessment.created_at", maximum=64)
    _text(root["origin_answer"], "assessment.origin_answer")

    detector = _mapping(root["detector"], "assessment.detector")
    if detector.get("tier") not in {
        "CLEAN", "WATCH", "LIKELY_SCAM", "CONFIRMED_PATTERN",
    }:
        raise CapsuleError("assessment.detector.tier is unknown")
    score = detector.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or score < 0:
        raise CapsuleError("assessment.detector.score must be a nonnegative integer")
    _identifiers(detector.get("families", []), "assessment.detector.families", maximum=32)
    iocs = _mapping(detector.get("iocs", {}), "assessment.detector.iocs")
    for kind, values in iocs.items():
        if kind not in _IOC_KINDS:
            raise CapsuleError("assessment.detector.iocs contains an unknown kind")
        for index, item in enumerate(_array(
            values, f"assessment.detector.iocs.{kind}", maximum=256,
        )):
            _text(item, f"assessment.detector.iocs.{kind}[{index}]", maximum=2048)

    threats = _mapping(root["threat_assessment"], "assessment.threat_assessment")
    if threats:
        if threats.get("schema_version") != "scamshield-threat-assessment/v1":
            raise CapsuleError("assessment threat schema is unknown")
        if threats.get("tier") not in {
            "CLEAN", "WATCH", "LIKELY_SCAM", "CONFIRMED_PATTERN",
        }:
            raise CapsuleError("assessment threat tier is unknown")
        threat_score = threats.get("score")
        if (isinstance(threat_score, bool) or not isinstance(threat_score, int)
                or threat_score < 0):
            raise CapsuleError("assessment threat score is invalid")
        _identifiers(
            threats.get("families", []),
            "assessment.threat_assessment.families", maximum=32,
        )
        _array(threats.get("findings"), "assessment.threat_assessment.findings", maximum=32)
        threat_limitations = _array(
            threats.get("limitations", []),
            "assessment.threat_assessment.limitations", maximum=32,
        )
        for index, limitation in enumerate(threat_limitations):
            _text(
                limitation,
                f"assessment.threat_assessment.limitations[{index}]",
            )

    collection = _mapping(root["collection"], "assessment.collection")
    if collection:
        if collection.get("schema_version") != "scamshield-collection/v1":
            raise CapsuleError("assessment collection schema is unknown")
        if collection.get("surface") not in {
            "private_submission", "guardian_group", "public_channel",
            "authorized_private_channel", "offline_import",
        }:
            raise CapsuleError("assessment collection surface is unknown")
        if collection.get("authorization") not in {
            "user_submitted", "public", "administrator_authorized", "operator_authorized",
        }:
            raise CapsuleError("assessment collection authorization is unknown")
        pseudonym = collection.get("source_pseudonym", "")
        if not isinstance(pseudonym, str) or (pseudonym and not _HEX24.fullmatch(pseudonym)):
            raise CapsuleError("assessment collection source_pseudonym is not privacy-safe")
        hints = _array(
            collection.get("script_hints", []),
            "assessment.collection.script_hints", maximum=16,
        )
        if any(item not in _SCRIPT_HINTS for item in hints):
            raise CapsuleError("assessment collection contains an unknown script hint")

    rate = _mapping(root["market_rate"], "assessment.market_rate")
    rate_value = rate.get("rate")
    if rate_value is not None and (
        isinstance(rate_value, bool)
        or not isinstance(rate_value, (int, float))
        or not math.isfinite(float(rate_value))
    ):
        raise CapsuleError("assessment.market_rate.rate must be finite")
    pack = _mapping(root["intelligence_pack"], "assessment.intelligence_pack")
    pack_hash = _text(pack.get("sha256"), "assessment.intelligence_pack.sha256", maximum=64)
    if not _HEX64.fullmatch(pack_hash):
        raise CapsuleError("assessment intelligence-pack digest is invalid")

    hypotheses = _array(root["hypotheses"], "assessment.hypotheses", maximum=MAX_HYPOTHESES)
    for index, raw in enumerate(hypotheses):
        hypothesis = _mapping(raw, f"assessment.hypotheses[{index}]")
        typology_id = _text(
            hypothesis.get("typology_id"),
            f"assessment.hypotheses[{index}].typology_id", maximum=128,
        )
        if not _IDENTIFIER.fullmatch(typology_id):
            raise CapsuleError(f"assessment.hypotheses[{index}].typology_id is invalid")
        _text(hypothesis.get("label"), f"assessment.hypotheses[{index}].label", maximum=1024)
        if hypothesis.get("dimension") not in DIMENSIONS:
            raise CapsuleError(f"assessment.hypotheses[{index}].dimension is unknown")
        if hypothesis.get("support_level") not in SUPPORT_LEVELS:
            raise CapsuleError(f"assessment.hypotheses[{index}].support_level is unknown")
        backers = hypothesis.get("independent_backers")
        if isinstance(backers, bool) or not isinstance(backers, int) or backers < 1:
            raise CapsuleError(f"assessment.hypotheses[{index}].independent_backers is invalid")
        limitations = _array(
            hypothesis.get("limitations"),
            f"assessment.hypotheses[{index}].limitations", maximum=32,
        )
        for li, limitation in enumerate(limitations):
            _text(limitation, f"assessment.hypotheses[{index}].limitations[{li}]")
    limitations = _array(root["limitations"], "assessment.limitations", maximum=32)
    for index, limitation in enumerate(limitations):
        _text(limitation, f"assessment.limitations[{index}]")
    _mapping(root["abstentions"], "assessment.abstentions")
    return root


def _assessment_bytes(value: bytes | str | Mapping[str, Any]) -> tuple[bytes, Mapping[str, Any]]:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, Mapping):
        try:
            raw = json.dumps(
                value, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CapsuleError(f"assessment cannot be serialized: {exc}") from exc
    else:
        raise CapsuleError("assessment must be bytes, text, or an object")
    if len(raw) > MAX_ASSESSMENT_BYTES:
        raise CapsuleError("ScamShield assessment exceeds the 1 MiB limit")
    assessment = _validate_assessment(strict_json_loads(raw))
    return raw, assessment


def capsule_from_assessment(
    assessment: bytes | str | Mapping[str, Any],
) -> dict[str, Any]:
    """Build and self-verify one inert Evidence Capsule from an assessment."""
    raw, value = _assessment_bytes(assessment)
    assessment_id = value["assessment_id"]
    created_at = value["created_at"]
    message_hash = value["message_sha256"]
    detector = value["detector"]
    threats = value["threat_assessment"]

    artifact = {
        "id": "assessment",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "media_type": "application/vnd.scamshield.provenance+json",
        "source": {
            "uri": f"urn:scamshield:assessment:{assessment_id}",
            "captured_at": created_at,
            "collector": "ScamShield provenance/v1",
        },
        # The assessment is derived from user-supplied Telegram content even
        # though the raw message itself is intentionally not embedded.
        "untrusted": True,
        "location": {
            "type": "inline",
            "encoding": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }

    claims: list[dict[str, Any]] = [{
        "id": "assessment-identity",
        "type": "provenance",
        "statement": (
            f"The attached ScamShield assessment identifies itself as {assessment_id}, "
            f"binds message SHA-256 {message_hash}, and records money-flow detector tier "
            f"{detector['tier']} with score {detector['score']}."
        ),
        "artifact_refs": ["assessment"],
        "derivation_refs": [
            "extract-assessment-id", "extract-message-hash", "extract-detector-tier",
        ],
        "binding_refs": [],
        "evidence_level": "derived",
        "limitations": [
            "The original Telegram message is not embedded, so this capsule cannot rerun the detector.",
            "The message hash binds an external message only if a reviewer possesses the exact original bytes.",
            "Capsule verification checks bytes and declared references, not detector correctness or natural-language truth.",
        ],
    }]
    derivations: list[dict[str, Any]] = [
        {
            "id": "extract-assessment-id",
            "type": "extraction",
            "description": "Read /assessment_id from the attached assessment.",
            "input_artifact_refs": ["assessment"],
            "supports_claim_refs": ["assessment-identity"],
            "proof": {
                "type": "json-pointer-equals-v1",
                "artifact_ref": "assessment",
                "pointer": "/assessment_id",
                "expected": assessment_id,
            },
        },
        {
            "id": "extract-message-hash",
            "type": "extraction",
            "description": "Read /message_sha256 from the attached assessment.",
            "input_artifact_refs": ["assessment"],
            "supports_claim_refs": ["assessment-identity"],
            "proof": {
                "type": "json-pointer-equals-v1",
                "artifact_ref": "assessment",
                "pointer": "/message_sha256",
                "expected": message_hash,
            },
        },
        {
            "id": "extract-detector-tier",
            "type": "extraction",
            "description": "Read /detector/tier from the attached assessment.",
            "input_artifact_refs": ["assessment"],
            "supports_claim_refs": ["assessment-identity"],
            "proof": {
                "type": "json-pointer-equals-v1",
                "artifact_ref": "assessment",
                "pointer": "/detector/tier",
                "expected": detector["tier"],
            },
        },
    ]

    global_limitations = list(value["limitations"])
    if threats:
        threat_families = list(threats.get("families", []))
        family_text = ", ".join(threat_families) if threat_families else "none"
        claims.append({
            "id": "threat-assessment",
            "type": "analytical-lead",
            "statement": (
                "The ScamShield assessment records threat-pattern tier "
                f"{threats['tier']} with score {threats['score']} and family/families "
                f"{family_text}."
            ),
            "artifact_refs": ["assessment"],
            "derivation_refs": [
                "extract-threat-tier", "extract-threat-score", "extract-threat-families",
            ],
            "binding_refs": [],
            "evidence_level": "derived",
            "limitations": list(threats.get("limitations", [])) + global_limitations,
        })
        for derivation_id, pointer, expected in (
            ("extract-threat-tier", "/threat_assessment/tier", threats["tier"]),
            ("extract-threat-score", "/threat_assessment/score", threats["score"]),
            (
                "extract-threat-families",
                "/threat_assessment/families",
                threat_families,
            ),
        ):
            derivations.append({
                "id": derivation_id,
                "type": "extraction",
                "description": f"Read {pointer} from the attached assessment.",
                "input_artifact_refs": ["assessment"],
                "supports_claim_refs": ["threat-assessment"],
                "proof": {
                    "type": "json-pointer-equals-v1",
                    "artifact_ref": "assessment",
                    "pointer": pointer,
                    "expected": expected,
                },
            })

    for index, hypothesis in enumerate(value["hypotheses"]):
        claim_id = f"hypothesis-{index}"
        derivation_id = f"extract-hypothesis-{index}"
        support = hypothesis["support_level"]
        claims.append({
            "id": claim_id,
            "type": "analytical-lead",
            "statement": (
                f"The ScamShield assessment records {hypothesis['label']} "
                f"({hypothesis['dimension']}) at support level {support}, with "
                f"{hypothesis['independent_backers']} independent backer(s)."
            ),
            "artifact_refs": ["assessment"],
            "derivation_refs": [derivation_id],
            "binding_refs": [],
            "evidence_level": "derived",
            "limitations": list(hypothesis["limitations"]) + global_limitations,
        })
        derivations.append({
            "id": derivation_id,
            "type": "extraction",
            "description": f"Read /hypotheses/{index}/support_level from the assessment.",
            "input_artifact_refs": ["assessment"],
            "supports_claim_refs": [claim_id],
            "proof": {
                "type": "json-pointer-equals-v1",
                "artifact_ref": "assessment",
                "pointer": f"/hypotheses/{index}/support_level",
                "expected": support,
            },
        })

    if not value["hypotheses"]:
        claims.append({
            "id": "provenance-abstention",
            "type": "analytical-lead",
            "statement": "The ScamShield assessment records no qualifying provenance hypothesis.",
            "artifact_refs": ["assessment"],
            "derivation_refs": ["extract-empty-hypotheses"],
            "binding_refs": [],
            "evidence_level": "derived",
            "limitations": global_limitations,
        })
        derivations.append({
            "id": "extract-empty-hypotheses",
            "type": "extraction",
            "description": "Verify that /hypotheses is the empty array.",
            "input_artifact_refs": ["assessment"],
            "supports_claim_refs": ["provenance-abstention"],
            "proof": {
                "type": "json-pointer-equals-v1",
                "artifact_ref": "assessment",
                "pointer": "/hypotheses",
                "expected": [],
            },
        })

    content = {
        "spec_version": SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "created_at": created_at,
        "producer": {
            "name": "Palimpsest",
            "software": "evidence.scamshield/v1",
        },
        "subject": {
            "type": "analytical-lead",
            "id": f"scamshield:{assessment_id}",
            "title": f"ScamShield provenance assessment {assessment_id}",
        },
        "artifacts": [artifact],
        "claims": claims,
        "derivations": derivations,
        "intents": [
            {
                "type": "human-review",
                "summary": "Review the exact assessment, cited typology sources, and every stated limitation.",
                "advisory": True,
            },
            {
                "type": "preserve",
                "summary": "Preserve the original message separately if policy and consent permit; its SHA-256 is recorded here.",
                "advisory": True,
            },
        ],
        "bindings": [],
    }
    capsule = build_capsule(content)
    report = verify_capsule(capsule)
    if not report["ok"]:
        raise CapsuleError(
            "ScamShield adapter produced an invalid capsule: "
            + "; ".join(report["errors"])
        )
    return capsule


def public_record_from_capsule(capsule: Mapping[str, Any]) -> dict[str, Any]:
    """Return an aggregate-only publication candidate from a verified capsule.

    Exact IOCs, matched message fragments, external-observation summaries, and
    the original assessment bytes are intentionally excluded.  The result is
    still marked for human review; this function prepares data for a later
    publication workflow but never publishes anything itself.
    """
    report = verify_capsule(capsule)
    if not report["ok"]:
        raise CapsuleError("cannot derive a public record from an invalid capsule")
    content = _mapping(capsule.get("content"), "capsule.content")
    artifacts = _array(content.get("artifacts"), "capsule.content.artifacts", maximum=64)
    artifact = next((item for item in artifacts if item.get("id") == "assessment"), None)
    if artifact is None:
        raise CapsuleError("capsule has no ScamShield assessment artifact")
    location = _mapping(artifact.get("location"), "assessment artifact location")
    if location.get("type") != "inline" or location.get("encoding") != "base64":
        raise CapsuleError("ScamShield assessment must be inline base64")
    try:
        raw = base64.b64decode(location.get("data", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise CapsuleError("assessment artifact is not valid base64") from exc
    assessment = _validate_assessment(strict_json_loads(raw))

    detector = _mapping(assessment["detector"], "assessment.detector")
    raw_iocs = detector.get("iocs", {})
    ioc_counts: dict[str, int] = {}
    if isinstance(raw_iocs, dict):
        for kind, values in raw_iocs.items():
            if isinstance(kind, str) and isinstance(values, list):
                ioc_counts[kind] = min(len(values), 10_000)

    hypotheses = []
    for item in assessment["hypotheses"]:
        hypotheses.append({
            "typology_id": item["typology_id"],
            "dimension": item["dimension"],
            "label": item["label"],
            "support_level": item["support_level"],
            "independent_backers": item["independent_backers"],
        })

    threats = assessment["threat_assessment"]
    threat_families = []
    if threats and isinstance(threats.get("families"), list):
        threat_families = [
            item for item in threats["families"]
            if isinstance(item, str) and len(item) <= 128
        ][:32]
    collection = assessment["collection"]
    safe_collection = {
        key: collection[key]
        for key in ("surface", "authorization", "source_pseudonym", "script_hints")
        if key in collection
    }
    return {
        "schema_version": "palimpsest-scamshield-public-record/v1",
        "capsule_sha256": capsule["content_sha256"],
        "assessment_id": assessment["assessment_id"],
        "created_at": assessment["created_at"],
        "review_status": "HUMAN_REVIEW_REQUIRED",
        "detector": {
            "tier": detector["tier"],
            "score": detector["score"],
            "families": list(detector.get("families", []))[:32],
        },
        "threat_assessment": {
            "tier": threats.get("tier", "CLEAN") if threats else "CLEAN",
            "score": threats.get("score", 0) if threats else 0,
            "families": threat_families,
        },
        "collection": safe_collection,
        "ioc_counts": ioc_counts,
        "hypotheses": hypotheses,
        "origin_answer": assessment["origin_answer"],
        "limitations": list(assessment["limitations"]),
    }


__all__ = [
    "ASSESSMENT_SCHEMA", "capsule_from_assessment", "public_record_from_capsule",
]
