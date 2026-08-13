"""Citation-bound private analytical packets and deterministic templates.

The deterministic candidate builder remains upstream. This layer projects its
bounded aggregate evidence into a compact packet, then builds and validates an
exact working template from that packet. Nothing here publishes, calls a
network, or accepts free-form model prose.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime
from typing import Any, Mapping

from core.investigative_candidates import validate_candidates


PACKET_SCHEMA = "palimpsest-analytical-packets.v1"
DRAFT_SET_SCHEMA = "palimpsest-analytical-draft-set.v1"
DRAFT_SCHEMA = "palimpsest-analytical-draft.v1"
PUBLICATION_POLICY = "private-review-only"
DISCLOSURE = (
    "Private deterministic evidence template. Every assertion requires human "
    "evidence review; "
    "this artifact cannot be published automatically."
)
MAX_PACKETS = 128
MAX_FINDINGS = 8

_PACKET_ID = re.compile(r"^packet-[0-9a-f]{24}$")
_PACKET_SET_ID = re.compile(r"^packetset-[0-9a-f]{24}$")
_EVIDENCE_ID = re.compile(r"^evidence-[0-9a-f]{20}$")
_DRAFT_ID = re.compile(r"^draft-[0-9a-f]{24}$")
_DRAFT_SET_ID = re.compile(r"^draftset-[0-9a-f]{24}$")
_FINDING_ID = re.compile(r"^finding-[0-9a-f]{16}$")
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_CANDIDATE_STATES = {
    "editorial_review",
    "needs_corroboration",
    "collection_target",
    "blocked_by_coverage",
}
_CANDIDATE_KINDS = {
    "signal_change",
    "cross_layer",
    "method_disagreement",
    "data_gap",
}
_PRIORITIES = {"urgent", "high", "normal"}

_PACKET_ROOT_KEYS = {
    "schema_version",
    "generated_at",
    "edition_id",
    "candidate_edition_id",
    "candidate_input_fingerprint",
    "scope",
    "publication_policy",
    "n_packets",
    "packets",
}
_PACKET_KEYS = {
    "packet_id",
    "candidate_id",
    "candidate_version_id",
    "kind",
    "priority",
    "candidate_state",
    "draft_mode",
    "question",
    "trigger",
    "evidence",
    "countercase_prompts",
    "verification_steps",
    "publication_policy",
}
_PACKET_EVIDENCE_KEYS = {
    "evidence_id",
    "artifact",
    "artifact_sha256",
    "artifact_generated_at",
    "selector",
    "observed_value",
    "limitation",
}
_DRAFT_SET_KEYS = {
    "schema_version",
    "generated_at",
    "edition_id",
    "packet_edition_id",
    "publication_policy",
    "n_drafts",
    "drafts",
}
_DRAFT_KEYS = {
    "schema_version",
    "draft_id",
    "packet_id",
    "candidate_version_id",
    "generated_at",
    "status",
    "generator",
    "headline",
    "dek",
    "thesis",
    "findings",
    "countercase",
    "verification_step",
    "limitations",
    "abstention_reason",
    "disclosure",
    "publication_policy",
}
_GENERATOR_KEYS = {"kind", "provider", "model", "run_id"}
_CLAIM_KEYS = {"text", "evidence_ids"}
_FINDING_KEYS = {"finding_id", "classification", "text", "evidence_ids"}
_CHALLENGE_KEYS = {"text", "basis"}


class AnalyticalPieceError(ValueError):
    """An analytical packet or draft violated its fail-closed contract."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AnalyticalPieceError("analytical artifact is not canonical JSON") from exc


def _stable_id(prefix: str, payload: Any, length: int) -> str:
    digest = hashlib.sha256(canonical_json_bytes(payload).rstrip(b"\n")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise AnalyticalPieceError(f"{field} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnalyticalPieceError(f"{field} is not a real timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalyticalPieceError(f"{field} must be timezone-aware")
    if parsed.utcoffset().total_seconds() != 0:
        raise AnalyticalPieceError(f"{field} must use UTC")
    return value


def _text(value: Any, field: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise AnalyticalPieceError(f"{field} must be text")
    value = unicodedata.normalize("NFC", value)
    if (not allow_empty and not value.strip()) or len(value) > maximum:
        raise AnalyticalPieceError(f"{field} has invalid length")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise AnalyticalPieceError(f"{field} contains unsafe Unicode")
    return value


def _json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise AnalyticalPieceError("observed evidence is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise AnalyticalPieceError("observed evidence contains a non-finite number")
    if isinstance(value, list):
        if len(value) > 32:
            raise AnalyticalPieceError("observed evidence list is not bounded")
        for item in value:
            _json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 32 or any(
            not isinstance(key, str) or len(key) > 200 for key in value
        ):
            raise AnalyticalPieceError("observed evidence object is not bounded")
        for item in value.values():
            _json_value(item, depth=depth + 1)
        return
    raise AnalyticalPieceError("observed evidence contains a non-JSON value")


def _bounded_text_list(
    value: Any, field: str, *, maximum_items: int = 16, maximum_chars: int = 1_000
) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum_items:
        raise AnalyticalPieceError(f"{field} must be a non-empty bounded list")
    return [
        _text(item, f"{field}[{index}]", maximum=maximum_chars)
        for index, item in enumerate(value)
    ]


def _draft_mode(state: str) -> str:
    modes = {
        "editorial_review": "evidence_memo",
        "needs_corroboration": "research_plan",
        "collection_target": "research_plan",
        "blocked_by_coverage": "abstain",
    }
    try:
        return modes[state]
    except KeyError as exc:
        raise AnalyticalPieceError("candidate state is invalid") from exc


def build_packet_set(candidates: Mapping[str, Any]) -> dict[str, Any]:
    """Project deterministic private leads into bounded evidence packets."""

    try:
        validate_candidates(candidates)
    except ValueError as exc:
        raise AnalyticalPieceError("candidate edition is invalid") from exc

    packets: list[dict[str, Any]] = []
    for lead in candidates["candidates"][:MAX_PACKETS]:
        evidence = []
        for row in lead["evidence_refs"]:
            evidence_payload = {
                "artifact": row["artifact"],
                "artifact_sha256": row["sha256"],
                "selector": row["selector"],
                "observed_value": row["observed_value"],
            }
            evidence.append(
                {
                    "evidence_id": _stable_id("evidence", evidence_payload, 20),
                    "artifact": row["artifact"],
                    "artifact_sha256": row["sha256"],
                    "artifact_generated_at": row["artifact_generated_at"],
                    "selector": row["selector"],
                    "observed_value": row["observed_value"],
                    "limitation": row["limitation"],
                }
            )
        packet = {
            "candidate_id": lead["candidate_id"],
            "candidate_version_id": lead["version_id"],
            "kind": lead["kind"],
            "priority": lead["priority"],
            "candidate_state": lead["state"],
            "draft_mode": _draft_mode(lead["state"]),
            "question": lead["question"],
            "trigger": lead["trigger"],
            "evidence": evidence,
            "countercase_prompts": list(lead["blockers"]),
            "verification_steps": list(lead["editorial_next_steps"]),
            "publication_policy": PUBLICATION_POLICY,
        }
        packet["packet_id"] = _stable_id("packet", packet, 24)
        packets.append(packet)

    document: dict[str, Any] = {
        "schema_version": PACKET_SCHEMA,
        "generated_at": candidates["generated_at"],
        "edition_id": "",
        "candidate_edition_id": candidates["edition_id"],
        "candidate_input_fingerprint": candidates["input_fingerprint"],
        "scope": (
            "Bounded aggregate evidence for private deterministic editorial review; "
            "no raw documents, person-level records, or publication authority."
        ),
        "publication_policy": PUBLICATION_POLICY,
        "n_packets": len(packets),
        "packets": packets,
    }
    document["edition_id"] = _stable_id(
        "packetset", _packet_edition_payload(document), 24
    )
    validate_packet_set(document)
    return document


def _validate_packet(packet: Any) -> None:
    if not isinstance(packet, dict) or set(packet) != _PACKET_KEYS:
        raise AnalyticalPieceError("analytical packet fields are not exact")
    if not _PACKET_ID.fullmatch(str(packet["packet_id"])):
        raise AnalyticalPieceError("packet_id is invalid")
    for field, pattern in (
        ("candidate_id", re.compile(r"^lead-[0-9a-f]{20}$")),
        ("candidate_version_id", re.compile(r"^leadv-[0-9a-f]{24}$")),
    ):
        if not pattern.fullmatch(str(packet[field])):
            raise AnalyticalPieceError(f"{field} is invalid")
    if packet["kind"] not in _CANDIDATE_KINDS:
        raise AnalyticalPieceError("packet kind is invalid")
    if packet["priority"] not in _PRIORITIES:
        raise AnalyticalPieceError("packet priority is invalid")
    if packet["candidate_state"] not in _CANDIDATE_STATES:
        raise AnalyticalPieceError("packet candidate state is invalid")
    if packet["draft_mode"] not in {"evidence_memo", "research_plan", "abstain"}:
        raise AnalyticalPieceError("packet draft_mode is invalid")
    if packet["draft_mode"] != _draft_mode(str(packet["candidate_state"])):
        raise AnalyticalPieceError("packet draft_mode does not match candidate state")
    if packet["publication_policy"] != PUBLICATION_POLICY:
        raise AnalyticalPieceError("packet publication policy is not private")
    _text(packet["question"], "packet.question", maximum=500)
    _text(packet["trigger"], "packet.trigger", maximum=1_000)
    _bounded_text_list(packet["countercase_prompts"], "countercase_prompts")
    _bounded_text_list(packet["verification_steps"], "verification_steps")
    evidence = packet["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 8:
        raise AnalyticalPieceError("packet evidence is not a bounded list")
    evidence_ids = []
    for row in evidence:
        if not isinstance(row, dict) or set(row) != _PACKET_EVIDENCE_KEYS:
            raise AnalyticalPieceError("packet evidence fields are not exact")
        if not _EVIDENCE_ID.fullmatch(str(row["evidence_id"])):
            raise AnalyticalPieceError("evidence_id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(row["artifact_sha256"])):
            raise AnalyticalPieceError("evidence artifact hash is invalid")
        _timestamp(row["artifact_generated_at"], "artifact_generated_at")
        artifact = _text(row["artifact"], "evidence.artifact", maximum=120)
        if not _ARTIFACT_NAME.fullmatch(artifact):
            raise AnalyticalPieceError("evidence artifact name is unsafe")
        selector = _text(row["selector"], "evidence.selector", maximum=300)
        if not selector.startswith("/") or ".." in selector:
            raise AnalyticalPieceError("evidence selector is unsafe")
        _text(row["limitation"], "evidence.limitation", maximum=1_000)
        _json_value(row["observed_value"])
        if len(canonical_json_bytes(row["observed_value"])) > 64 * 1024:
            raise AnalyticalPieceError("observed evidence exceeds 64 KiB")
        expected = _stable_id(
            "evidence",
            {
                "artifact": row["artifact"],
                "artifact_sha256": row["artifact_sha256"],
                "selector": row["selector"],
                "observed_value": row["observed_value"],
            },
            20,
        )
        if row["evidence_id"] != expected:
            raise AnalyticalPieceError("evidence_id does not match content")
        evidence_ids.append(row["evidence_id"])
    if len(evidence_ids) != len(set(evidence_ids)):
        raise AnalyticalPieceError("packet contains duplicate evidence IDs")
    payload = {key: value for key, value in packet.items() if key != "packet_id"}
    if packet["packet_id"] != _stable_id("packet", payload, 24):
        raise AnalyticalPieceError("packet_id does not match content")


def _packet_edition_payload(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "edition_id"}


def validate_packet_set(document: Mapping[str, Any]) -> None:
    if not isinstance(document, dict) or set(document) != _PACKET_ROOT_KEYS:
        raise AnalyticalPieceError("packet-set fields are not exact")
    if document.get("schema_version") != PACKET_SCHEMA:
        raise AnalyticalPieceError("unsupported packet-set schema")
    _timestamp(document.get("generated_at"), "generated_at")
    if not _PACKET_SET_ID.fullmatch(str(document.get("edition_id", ""))):
        raise AnalyticalPieceError("packet-set edition_id is invalid")
    if not re.fullmatch(
        r"leadset-[0-9a-f]{24}", str(document.get("candidate_edition_id", ""))
    ) or not re.fullmatch(
        r"[0-9a-f]{64}", str(document.get("candidate_input_fingerprint", ""))
    ):
        raise AnalyticalPieceError("packet-set candidate identity is invalid")
    _text(document.get("scope"), "scope", maximum=500)
    if document.get("publication_policy") != PUBLICATION_POLICY:
        raise AnalyticalPieceError("packet set is not private review only")
    packets = document.get("packets")
    if not isinstance(packets, list) or len(packets) > MAX_PACKETS:
        raise AnalyticalPieceError("packets must be a bounded list")
    if document.get("n_packets") != len(packets):
        raise AnalyticalPieceError("n_packets does not match packets")
    for packet in packets:
        _validate_packet(packet)
    packet_ids = [packet["packet_id"] for packet in packets]
    if len(packet_ids) != len(set(packet_ids)):
        raise AnalyticalPieceError("packet set contains duplicate packet IDs")
    if document["edition_id"] != _stable_id(
        "packetset", _packet_edition_payload(document), 24
    ):
        raise AnalyticalPieceError("packet-set edition_id does not match content")


def _validate_copied_claim(
    value: Any, *, field: str, expected_text: str, expected_ids: list[str]
) -> None:
    if not isinstance(value, dict) or set(value) != _CLAIM_KEYS:
        raise AnalyticalPieceError(f"{field} fields are not exact")
    _text(value["text"], f"{field}.text", maximum=2_000)
    if value["text"] != expected_text or value["evidence_ids"] != expected_ids:
        raise AnalyticalPieceError(
            f"{field} must reproduce deterministic packet-backed copy"
        )


def _validate_draft(
    draft: Any,
    packet: Mapping[str, Any],
    *,
    generated_at: str,
    packet_edition_id: str,
) -> None:
    if not isinstance(draft, dict) or set(draft) != _DRAFT_KEYS:
        raise AnalyticalPieceError("draft fields are not exact")
    if draft.get("schema_version") != DRAFT_SCHEMA:
        raise AnalyticalPieceError("unsupported draft schema")
    if not _DRAFT_ID.fullmatch(str(draft.get("draft_id", ""))):
        raise AnalyticalPieceError("draft_id is invalid")
    if (
        draft.get("packet_id") != packet["packet_id"]
        or draft.get("candidate_version_id") != packet["candidate_version_id"]
    ):
        raise AnalyticalPieceError("draft is not bound to its packet")
    _timestamp(draft.get("generated_at"), "draft.generated_at")
    if draft.get("generated_at") != generated_at:
        raise AnalyticalPieceError("draft clock does not match its packet set")
    if (
        draft.get("publication_policy") != PUBLICATION_POLICY
        or draft.get("disclosure") != DISCLOSURE
    ):
        raise AnalyticalPieceError("draft weakened its publication boundary")
    generator = draft.get("generator")
    if not isinstance(generator, dict) or set(generator) != _GENERATOR_KEYS:
        raise AnalyticalPieceError("draft generator fields are not exact")
    expected_generator = {
        "kind": "deterministic-template",
        "provider": "palimpsest",
        "model": "none",
        "run_id": packet_edition_id,
    }
    if generator != expected_generator:
        raise AnalyticalPieceError("draft generator identity is not deterministic")
    limitations = _bounded_text_list(
        draft.get("limitations"), "draft.limitations", maximum_items=16
    )
    for text in limitations:
        _text(text, "draft.limitation", maximum=1_000)

    status = draft.get("status")
    if status not in {"draft", "abstained"}:
        raise AnalyticalPieceError("draft status is invalid")
    if status == "abstained":
        if (
            any(
                draft[field] is not None
                for field in (
                    "headline",
                    "dek",
                    "thesis",
                    "countercase",
                    "verification_step",
                )
            )
            or draft["findings"] != []
        ):
            raise AnalyticalPieceError("abstained draft contains proposed copy")
        _text(draft.get("abstention_reason"), "abstention_reason", maximum=1_000)
    else:
        if packet["draft_mode"] == "abstain":
            raise AnalyticalPieceError("coverage-blocked packet cannot produce a draft")
        if packet["draft_mode"] == "research_plan":
            raise AnalyticalPieceError(
                "research-plan packet cannot produce an assertion draft"
            )
        evidence_by_id = {row["evidence_id"]: row for row in packet["evidence"]}
        if not evidence_by_id:
            raise AnalyticalPieceError("draft packet has no evidence")
        ids = [row["evidence_id"] for row in packet["evidence"]]
        _validate_copied_claim(
            draft.get("headline"),
            field="headline",
            expected_text=packet["question"],
            expected_ids=ids,
        )
        _validate_copied_claim(
            draft.get("dek"),
            field="dek",
            expected_text=packet["trigger"],
            expected_ids=ids,
        )
        _validate_copied_claim(
            draft.get("thesis"),
            field="thesis",
            expected_text=f"Working research question: {packet['question']}",
            expected_ids=ids,
        )
        findings = draft.get("findings")
        if (
            not isinstance(findings, list)
            or not findings
            or len(findings) > MAX_FINDINGS
        ):
            raise AnalyticalPieceError("findings must be a non-empty bounded list")
        finding_ids = []
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict) or set(finding) != _FINDING_KEYS:
                raise AnalyticalPieceError("finding fields are not exact")
            if finding.get("classification") != "observed":
                raise AnalyticalPieceError(
                    "working draft findings must remain deterministic observations"
                )
            evidence_id = finding.get("evidence_ids")
            if not isinstance(evidence_id, list) or len(evidence_id) != 1:
                raise AnalyticalPieceError("finding must cite exactly one observation")
            row = evidence_by_id.get(evidence_id[0])
            if row is None:
                raise AnalyticalPieceError("finding contains an unknown evidence ID")
            observed = json.dumps(
                row["observed_value"],
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            expected_text = f"The scoped evidence records this observation: {observed}."
            if finding.get("text") != expected_text:
                raise AnalyticalPieceError(
                    "finding must reproduce a deterministic evidence projection"
                )
            payload = {
                "classification": finding["classification"],
                "text": finding["text"],
                "evidence_ids": finding["evidence_ids"],
            }
            expected = _stable_id("finding", payload, 16)
            if finding.get("finding_id") != expected or not _FINDING_ID.fullmatch(
                str(finding.get("finding_id", ""))
            ):
                raise AnalyticalPieceError("finding_id does not match content")
            finding_ids.append(finding["finding_id"])
        if len(finding_ids) != len(set(finding_ids)):
            raise AnalyticalPieceError("draft contains duplicate findings")
        for field, basis in (
            ("countercase", "candidate_blockers"),
            ("verification_step", "candidate_next_steps"),
        ):
            value = draft.get(field)
            if not isinstance(value, dict) or set(value) != _CHALLENGE_KEYS:
                raise AnalyticalPieceError(f"{field} fields are not exact")
            _text(value["text"], f"{field}.text", maximum=2_000)
            if value["basis"] != basis:
                raise AnalyticalPieceError(f"{field} basis is invalid")
            allowed = (
                packet["countercase_prompts"]
                if field == "countercase"
                else packet["verification_steps"]
            )
            if value["text"] not in allowed:
                raise AnalyticalPieceError(
                    f"{field} must reproduce an authoritative packet prompt"
                )
        if draft.get("abstention_reason") is not None:
            raise AnalyticalPieceError("non-abstained draft has an abstention reason")

    required_limitations = {row["limitation"] for row in packet["evidence"]}
    if not required_limitations.issubset(set(limitations)):
        raise AnalyticalPieceError("draft omitted an evidence limitation")

    payload = {key: value for key, value in draft.items() if key != "draft_id"}
    if draft["draft_id"] != _stable_id("draft", payload, 24):
        raise AnalyticalPieceError("draft_id does not match content")


def validate_draft_set(packets: Mapping[str, Any], document: Mapping[str, Any]) -> None:
    validate_packet_set(packets)
    if not isinstance(document, dict) or set(document) != _DRAFT_SET_KEYS:
        raise AnalyticalPieceError("draft-set fields are not exact")
    if document.get("schema_version") != DRAFT_SET_SCHEMA:
        raise AnalyticalPieceError("unsupported draft-set schema")
    _timestamp(document.get("generated_at"), "draft_set.generated_at")
    if not _DRAFT_SET_ID.fullmatch(str(document.get("edition_id", ""))):
        raise AnalyticalPieceError("draft-set edition_id is invalid")
    if document.get("packet_edition_id") != packets["edition_id"]:
        raise AnalyticalPieceError("draft set is not bound to the packet edition")
    if document.get("publication_policy") != PUBLICATION_POLICY:
        raise AnalyticalPieceError("draft set is not private review only")
    drafts = document.get("drafts")
    if not isinstance(drafts, list) or len(drafts) > MAX_PACKETS:
        raise AnalyticalPieceError("drafts must be a bounded list")
    if document.get("n_drafts") != len(drafts):
        raise AnalyticalPieceError("n_drafts does not match drafts")
    if document.get("generated_at") != packets.get("generated_at"):
        raise AnalyticalPieceError("draft-set clock does not match its packet set")
    packet_by_id = {packet["packet_id"]: packet for packet in packets["packets"]}
    seen = set()
    for draft in drafts:
        packet_id = draft.get("packet_id") if isinstance(draft, dict) else None
        if packet_id not in packet_by_id or packet_id in seen:
            raise AnalyticalPieceError("draft set has an unknown or duplicate packet")
        _validate_draft(
            draft,
            packet_by_id[packet_id],
            generated_at=packets["generated_at"],
            packet_edition_id=packets["edition_id"],
        )
        seen.add(packet_id)
    expected_packet_ids = [packet["packet_id"] for packet in packets["packets"]]
    actual_packet_ids = [draft["packet_id"] for draft in drafts]
    if actual_packet_ids != expected_packet_ids:
        raise AnalyticalPieceError(
            "draft set must cover every packet exactly once and in packet order"
        )
    edition_payload = {
        key: value for key, value in document.items() if key != "edition_id"
    }
    if document["edition_id"] != _stable_id("draftset", edition_payload, 24):
        raise AnalyticalPieceError("draft-set edition_id does not match content")

    expected = _project_template_draft_set(packets)
    if canonical_json_bytes(document) != canonical_json_bytes(expected):
        raise AnalyticalPieceError(
            "draft set is not the exact deterministic projection of its packet set"
        )


def _project_template_draft_set(packets: Mapping[str, Any]) -> dict[str, Any]:
    """Project an already-validated packet set without recursively validating."""

    drafts = []
    for packet in packets["packets"]:
        limitations = list(
            dict.fromkeys(
                [row["limitation"] for row in packet["evidence"]]
                + list(packet["countercase_prompts"])
            )
        )[:16]
        if not limitations:
            limitations = [
                "No evidence limitation was available; human review is required."
            ]
        draft: dict[str, Any] = {
            "schema_version": DRAFT_SCHEMA,
            "packet_id": packet["packet_id"],
            "candidate_version_id": packet["candidate_version_id"],
            "generated_at": packets["generated_at"],
            "generator": {
                "kind": "deterministic-template",
                "provider": "palimpsest",
                "model": "none",
                "run_id": packets["edition_id"],
            },
            "limitations": limitations,
            "disclosure": DISCLOSURE,
            "publication_policy": PUBLICATION_POLICY,
        }
        if packet["draft_mode"] != "evidence_memo" or not packet["evidence"]:
            draft.update(
                status="abstained",
                headline=None,
                dek=None,
                thesis=None,
                findings=[],
                countercase=None,
                verification_step=None,
                abstention_reason=(
                    "The deterministic gate permits only a research plan or abstention, "
                    "not assertion-bearing analytical copy, from this packet."
                ),
            )
        else:
            ids = [row["evidence_id"] for row in packet["evidence"]]
            findings = []
            for row in packet["evidence"][:MAX_FINDINGS]:
                observed = json.dumps(
                    row["observed_value"],
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                text = f"The scoped evidence records this observation: {observed}."
                payload = {
                    "classification": "observed",
                    "text": text,
                    "evidence_ids": [row["evidence_id"]],
                }
                findings.append(
                    {"finding_id": _stable_id("finding", payload, 16), **payload}
                )
            draft.update(
                status="draft",
                headline={"text": packet["question"], "evidence_ids": ids},
                dek={"text": packet["trigger"], "evidence_ids": ids},
                thesis={
                    "text": f"Working research question: {packet['question']}",
                    "evidence_ids": ids,
                },
                findings=findings,
                countercase={
                    "text": packet["countercase_prompts"][0],
                    "basis": "candidate_blockers",
                },
                verification_step={
                    "text": packet["verification_steps"][0],
                    "basis": "candidate_next_steps",
                },
                abstention_reason=None,
            )
        draft["draft_id"] = _stable_id("draft", draft, 24)
        drafts.append(draft)
    document: dict[str, Any] = {
        "schema_version": DRAFT_SET_SCHEMA,
        "generated_at": packets["generated_at"],
        "edition_id": "",
        "packet_edition_id": packets["edition_id"],
        "publication_policy": PUBLICATION_POLICY,
        "n_drafts": len(drafts),
        "drafts": drafts,
    }
    document["edition_id"] = _stable_id(
        "draftset",
        {key: value for key, value in document.items() if key != "edition_id"},
        24,
    )
    return document


def build_template_draft_set(packets: Mapping[str, Any]) -> dict[str, Any]:
    """Build safe deterministic working memos; useful without any model provider."""

    validate_packet_set(packets)
    document = _project_template_draft_set(packets)
    validate_draft_set(packets, document)
    return document


__all__ = [
    "AnalyticalPieceError",
    "DISCLOSURE",
    "DRAFT_SCHEMA",
    "DRAFT_SET_SCHEMA",
    "PACKET_SCHEMA",
    "PUBLICATION_POLICY",
    "build_packet_set",
    "build_template_draft_set",
    "canonical_json_bytes",
    "validate_draft_set",
    "validate_packet_set",
]
