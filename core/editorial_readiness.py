"""Publication profiles and fail-closed newsroom quality gates."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from core.corroboration import validate_corroboration
from core.investigations import validate_investigations
from core.network_rounds import validate_network_rounds
from core.newswire import validate_prior_newswire_document
from core.primary_documents import validate_primary_document_index
from core.source_workflow import validate_source_workflow_summary


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "editorial_packages.json"
DEFAULT_OUTPUT_PATH = ROOT / "readings" / "editorial-readiness-latest.json"
CONFIG_VERSION = "palimpsest-editorial-packages.v1"
SCHEMA_VERSION = "palimpsest-editorial-readiness.v1"


class EditorialReadinessError(ValueError):
    """An editorial manifest or computed gate violated the publication floor."""


_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EDITOR_RE = re.compile(r"^editor-[0-9a-f]{12}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_ID_RE = re.compile(r"^document-[0-9a-f]{24}$")

_CONFIG_FIELDS = frozenset({"schema_version", "automatic_publication", "packages"})
_PACKAGE_CONFIG_FIELDS = frozenset(
    {
        "package_id",
        "subject_case_slug",
        "profile",
        "primary_document_ids",
        "affected_voice",
        "historical_context",
        "visual",
        "sentence_citations",
        "human_edit",
        "fact_check",
        "update_history",
    }
)
_AFFECTED_FIELDS = frozenset({"required", "waiver_status", "rationale"})
_HISTORY_FIELDS = frozenset({"status", "citation_urls", "note"})
_VISUAL_FIELDS = frozenset({"status", "type", "url", "explanation"})
_CITATION_FIELDS = frozenset({"status", "claim_ids"})
_REVIEW_FIELDS = frozenset({"status", "reviewer_id", "completed_at"})
_UPDATE_FIELDS = frozenset({"visible", "url"})

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "scope",
        "method",
        "automatic_publication",
        "source_inputs",
        "publication_profiles",
        "wire",
        "packages",
        "summary",
    }
)
_INPUT_FIELDS = frozenset({"url", "generated_at", "sha256"})
_PROFILE_FIELDS = frozenset({"profile", "requirements"})
_WIRE_FIELDS = frozenset(
    {"n_events", "eligible_events", "blocked_events", "checks", "events"}
)
_WIRE_EVENT_FIELDS = frozenset(
    {"event_id", "status", "evidence_label", "n_independent_groups", "failed_check_ids"}
)
_PACKAGE_FIELDS = frozenset(
    {
        "package_id",
        "subject_case_slug",
        "profile",
        "title",
        "status",
        "publishable",
        "checks",
        "failed_check_ids",
    }
)
_CHECK_FIELDS = frozenset(
    {"check_id", "label", "required", "observed", "passed", "detail"}
)
_SUMMARY_FIELDS = frozenset(
    {
        "wire_events",
        "wire_eligible",
        "wire_blocked",
        "explainers",
        "explainers_publishable",
        "investigations",
        "investigations_publishable",
    }
)

_PROFILE_REQUIREMENTS = {
    "wire": [
        "attribution",
        "source-receipt",
        "scope-limitations",
        "evidence-label",
        "no-automatic-publication",
    ],
    "explainer": [
        "primary-document",
        "independent-groups",
        "historical-context",
        "counterevidence",
        "expert-voice",
        "affected-voice",
        "explanatory-visual",
        "sentence-citations",
        "explicit-limitations",
        "human-edit",
        "no-automatic-publication",
    ],
    "investigation": [
        "primary-document",
        "independent-groups",
        "historical-context",
        "counterevidence",
        "expert-voice",
        "skeptical-expert-voice",
        "affected-voice",
        "explanatory-visual",
        "sentence-citations",
        "explicit-limitations",
        "human-edit",
        "fact-check",
        "right-to-reply",
        "corrections-and-updates",
        "falsification-assessed",
        "safety-review",
        "no-automatic-publication",
    ],
}


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
        raise EditorialReadinessError("editorial readiness is not canonical JSON") from exc


def _exact(value: Any, fields: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        actual = set(value) if type(value) is dict else set()
        raise EditorialReadinessError(
            f"{path} fields do not match contract "
            f"(missing={sorted(fields - actual)}, unknown={sorted(actual - fields)})"
        )
    return value


def _text(value: Any, path: str, *, maximum: int = 2_000) -> str:
    if type(value) is not str:
        raise EditorialReadinessError(f"{path} must be text")
    value = unicodedata.normalize("NFC", value)
    if not value.strip() or len(value) > maximum:
        raise EditorialReadinessError(f"{path} has invalid length")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise EditorialReadinessError(f"{path} contains unsafe Unicode")
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not _TS_RE.fullmatch(value):
        raise EditorialReadinessError(f"{path} is not a canonical UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise EditorialReadinessError(f"{path} is not a real timestamp") from exc
    return value


def _url(value: Any, path: str) -> str:
    value = _text(value, path, maximum=2_048)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise EditorialReadinessError(f"{path} is not a safe HTTPS URL")
    return value


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EditorialReadinessError(f"cannot read editorial config: {path}") from exc
    if type(value) is not dict:
        raise EditorialReadinessError("editorial config must be an object")
    return value


def load_editorial_packages(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    top = dict(_exact(_read_json(Path(path)), _CONFIG_FIELDS, "config"))
    if top["schema_version"] != CONFIG_VERSION:
        raise EditorialReadinessError("unsupported editorial package config")
    if top["automatic_publication"] is not False:
        raise EditorialReadinessError("automatic publication is prohibited")
    packages = top["packages"]
    if type(packages) is not list or not 1 <= len(packages) <= 256:
        raise EditorialReadinessError("editorial packages are outside their bound")
    package_ids = []
    normalized = []
    for index, raw in enumerate(packages):
        path_prefix = f"packages[{index}]"
        row = dict(_exact(raw, _PACKAGE_CONFIG_FIELDS, path_prefix))
        for field in ("package_id", "subject_case_slug"):
            value = _text(row[field], f"{path_prefix}.{field}", maximum=128)
            if not _ID_RE.fullmatch(value):
                raise EditorialReadinessError(f"{path_prefix}.{field} is invalid")
        package_ids.append(row["package_id"])
        if row["profile"] not in {"explainer", "investigation"}:
            raise EditorialReadinessError("editorial package profile is invalid")
        document_ids = row["primary_document_ids"]
        if (
            type(document_ids) is not list
            or document_ids != sorted(set(document_ids))
            or any(
                type(value) is not str or not _DOCUMENT_ID_RE.fullmatch(value)
                for value in document_ids
            )
        ):
            raise EditorialReadinessError("primary document IDs are invalid")
        affected = _exact(row["affected_voice"], _AFFECTED_FIELDS, "affected_voice")
        if type(affected["required"]) is not bool or affected["waiver_status"] not in {
            "not_requested",
            "approved",
            "rejected",
        }:
            raise EditorialReadinessError("affected-voice requirement is invalid")
        _text(affected["rationale"], "affected_voice.rationale", maximum=1_000)
        if affected["required"] and affected["waiver_status"] == "approved":
            raise EditorialReadinessError("a required affected voice cannot be waived")
        history = _exact(row["historical_context"], _HISTORY_FIELDS, "historical_context")
        if history["status"] not in {"pending", "complete"}:
            raise EditorialReadinessError("historical-context status is invalid")
        urls = history["citation_urls"]
        if type(urls) is not list or urls != sorted(set(urls)):
            raise EditorialReadinessError("historical citation URLs are invalid")
        for url in urls:
            _url(url, "historical_context.citation_url")
        _text(history["note"], "historical_context.note", maximum=1_000)
        if history["status"] == "complete" and not urls:
            raise EditorialReadinessError("complete historical context needs a citation")
        visual = _exact(row["visual"], _VISUAL_FIELDS, "visual")
        if visual["status"] not in {"pending", "complete"}:
            raise EditorialReadinessError("visual status is invalid")
        if visual["type"] not in {None, "chart", "map", "timeline"}:
            raise EditorialReadinessError("visual type is invalid")
        if visual["url"] is not None:
            _url(visual["url"], "visual.url")
        _text(visual["explanation"], "visual.explanation", maximum=1_000)
        if visual["status"] == "complete" and (
            visual["type"] is None or visual["url"] is None
        ):
            raise EditorialReadinessError("complete visual needs type and URL")
        citations = _exact(
            row["sentence_citations"], _CITATION_FIELDS, "sentence_citations"
        )
        if citations["status"] not in {"pending", "complete"}:
            raise EditorialReadinessError("sentence-citation status is invalid")
        if (
            type(citations["claim_ids"]) is not list
            or citations["claim_ids"] != sorted(set(citations["claim_ids"]))
            or any(not _ID_RE.fullmatch(value) for value in citations["claim_ids"])
        ):
            raise EditorialReadinessError("sentence citation claim IDs are invalid")
        for field in ("human_edit", "fact_check"):
            review = _exact(row[field], _REVIEW_FIELDS, field)
            if review["status"] not in {"pending", "complete"}:
                raise EditorialReadinessError(f"{field} status is invalid")
            complete = review["status"] == "complete"
            if complete:
                if not _EDITOR_RE.fullmatch(review["reviewer_id"] or ""):
                    raise EditorialReadinessError(f"{field} reviewer is invalid")
                _timestamp(review["completed_at"], f"{field}.completed_at")
            elif review["reviewer_id"] is not None or review["completed_at"] is not None:
                raise EditorialReadinessError(f"pending {field} has a completion receipt")
        updates = _exact(row["update_history"], _UPDATE_FIELDS, "update_history")
        if type(updates["visible"]) is not bool:
            raise EditorialReadinessError("update-history visible flag is invalid")
        _url(updates["url"], "update_history.url")
        normalized.append(row)
    if package_ids != sorted(set(package_ids)):
        raise EditorialReadinessError("editorial packages are not unique and sorted")
    top["packages"] = normalized
    return top


def _check(
    check_id: str,
    label: str,
    required: int | bool,
    observed: int | bool,
    detail: str,
) -> dict[str, Any]:
    if type(required) is bool:
        passed = observed is required
    else:
        passed = type(observed) is int and observed >= required
    return {
        "check_id": check_id,
        "label": label,
        "required": required,
        "observed": observed,
        "passed": passed,
        "detail": detail,
    }


def _wire_assessment(
    wire: Mapping[str, Any], corroboration: Mapping[str, Any]
) -> dict[str, Any]:
    joined = {row["event_id"]: row for row in corroboration["events"]}
    events = []
    check_totals = {requirement: 0 for requirement in _PROFILE_REQUIREMENTS["wire"]}
    for event in wire["events"]:
        joined_event = joined.get(event["event_id"])
        if joined_event is None:
            raise EditorialReadinessError("corroboration omitted a wire event")
        checks = {
            "attribution": bool(event["reported_facts"])
            and all(row["attribution"] for row in event["reported_facts"]),
            "source-receipt": bool(event["evidence_refs"]),
            "scope-limitations": bool(event["limitations"]),
            "evidence-label": event["evidence_strength"].startswith("single-")
            or joined_event["status"] == "corroborated",
            "no-automatic-publication": True,
        }
        for check_id, passed in checks.items():
            check_totals[check_id] += int(passed)
        failed = sorted(check_id for check_id, passed in checks.items() if not passed)
        events.append(
            {
                "event_id": event["event_id"],
                "status": "eligible" if not failed else "blocked",
                "evidence_label": (
                    "primary-reviewed-corroborated"
                    if joined_event["has_primary_document"]
                    else event["evidence_strength"]
                ),
                "n_independent_groups": joined_event["n_independent_groups"],
                "failed_check_ids": failed,
            }
        )
    events.sort(key=lambda row: row["event_id"])
    eligible = sum(row["status"] == "eligible" for row in events)
    return {
        "n_events": len(events),
        "eligible_events": eligible,
        "blocked_events": len(events) - eligible,
        "checks": check_totals,
        "events": events,
    }


def _right_to_reply_complete(case: Mapping[str, Any]) -> bool:
    reply = case["right_to_reply"]
    if not case["safety"]["allegations"]:
        return reply["status"] == "not_applicable" and not reply["parties"]
    return reply["status"] == "complete" and bool(reply["parties"]) and all(
        row["disposition"] != "pending" for row in reply["parties"]
    )


def _package_assessment(
    package: Mapping[str, Any],
    case: Mapping[str, Any],
    primary_by_id: Mapping[str, Any],
    source_package: Mapping[str, Any],
    network_rounds: Mapping[str, Any],
) -> dict[str, Any]:
    analytical = [row for row in case["claims"] if row["type"] == "analytical_finding"]
    evidence = {row["evidence_id"]: row for row in case["evidence"]}
    group_counts = []
    for claim in analytical:
        group_counts.append(
            len(
                {
                    evidence[evidence_id]["independence_group"]
                    for evidence_id in claim["evidence_ids"]
                    if evidence[evidence_id]["source_class"] != "derived"
                    and evidence[evidence_id]["role"] == "support"
                }
            )
        )
    primary_ids = package["primary_document_ids"]
    valid_primary = sum(
        document_id in primary_by_id
        and primary_by_id[document_id]["capture_scope"] == "release_document"
        for document_id in primary_ids
    )
    all_claim_ids = {row["claim_id"] for row in case["claims"]}
    cited_claims = set(package["sentence_citations"]["claim_ids"])
    if not cited_claims <= all_claim_ids:
        raise EditorialReadinessError("sentence citation references an unknown claim")
    source_ready = source_package["readiness"]
    affected = package["affected_voice"]
    affected_pass = (
        source_ready["affected_voice"]
        if affected["required"]
        else affected["waiver_status"] == "approved"
    )
    assessed_falsification = sum(
        row["status"] in {"passed", "failed"}
        for row in case["falsification_conditions"]
    )
    historical_complete = (
        package["historical_context"]["status"] == "complete"
        and bool(package["historical_context"]["citation_urls"])
    )
    historical_detail = package["historical_context"]["note"]
    if package["subject_case_slug"] == "china-network-filtering-no-single-rate":
        longitudinal_ready = (
            network_rounds["n_comparable_rounds"]
            >= network_rounds["minimum_comparable_rounds"]
        )
        historical_complete = historical_complete and longitudinal_ready
        historical_detail += (
            f" Comparable synchronized rounds: {network_rounds['n_comparable_rounds']}/"
            f"{network_rounds['minimum_comparable_rounds']}."
        )
    checks = [
        _check(
            "primary-document",
            "At least one captured primary release",
            1,
            valid_primary,
            f"{valid_primary} attached release-document receipt(s).",
        ),
        _check(
            "independent-groups",
            "Independent groups per analytical claim",
            2,
            min(group_counts, default=0),
            "Derived rollups do not count as independent groups.",
        ),
        _check(
            "historical-context",
            "Historical or comparable context with citations",
            True,
            historical_complete,
            historical_detail,
        ),
        _check(
            "counterevidence",
            "Every analytical claim addresses counterevidence",
            len(analytical),
            sum(bool(row["counterevidence_ids"]) for row in analytical),
            "Alternative explanations must be attached claim by claim.",
        ),
        _check(
            "expert-voice",
            "Verified, consented expert voice",
            True,
            source_ready["expert_voice"],
            "Counted only from encrypted, verified, consented, safety-reviewed records.",
        ),
        _check(
            "affected-voice",
            "Verified affected voice where relevant",
            True,
            affected_pass,
            affected["rationale"],
        ),
        _check(
            "explanatory-visual",
            "Material explanatory chart, map, or timeline",
            True,
            package["visual"]["status"] == "complete",
            package["visual"]["explanation"],
        ),
        _check(
            "sentence-citations",
            "Every structured claim mapped to sentence-level citations",
            len(all_claim_ids),
            len(cited_claims)
            if package["sentence_citations"]["status"] == "complete"
            else 0,
            "Citation completion is an explicit editorial attestation.",
        ),
        _check(
            "explicit-limitations",
            "Analytical claims carry explicit limitations",
            len(analytical),
            sum(bool(row["limitation_ids"]) for row in analytical),
            "Limitations must state consequences, not merely exist as boilerplate.",
        ),
        _check(
            "human-edit",
            "Human edit completed",
            True,
            package["human_edit"]["status"] == "complete",
            "A pseudonymous, dated editorial receipt is required.",
        ),
        _check(
            "no-automatic-publication",
            "Automatic publication disabled",
            True,
            True,
            "Passing a gate never triggers publication automatically.",
        ),
    ]
    if package["profile"] == "investigation":
        checks.extend(
            [
                _check(
                    "skeptical-expert-voice",
                    "Verified skeptical expert voice",
                    True,
                    source_ready["skeptical_expert_voice"],
                    "The challenging interpretation is recorded independently.",
                ),
                _check(
                    "fact-check",
                    "Independent fact-check completed",
                    True,
                    package["fact_check"]["status"] == "complete",
                    "A pseudonymous, dated fact-check receipt is required.",
                ),
                _check(
                    "right-to-reply",
                    "Right-to-reply complete before allegations",
                    True,
                    _right_to_reply_complete(case)
                    and source_ready["right_to_reply_complete"],
                    "Not-applicable is valid only when the case makes no allegation.",
                ),
                _check(
                    "corrections-and-updates",
                    "Visible correction and update history",
                    True,
                    package["update_history"]["visible"]
                    and bool(case["correction"]["policy_url"]),
                    "Current and immutable revision links must remain visible.",
                ),
                _check(
                    "falsification-assessed",
                    "At least one falsification condition assessed",
                    1,
                    assessed_falsification,
                    "Untested conditions keep an investigation in the lead register.",
                ),
                _check(
                    "safety-review",
                    "Human-source and case safety review complete",
                    True,
                    source_ready["all_safety_reviewed"]
                    and case["safety"]["person_level_data"] is False,
                    "Public case files remain aggregate; protected notes stay private.",
                ),
            ]
        )
    requirement_order = _PROFILE_REQUIREMENTS[package["profile"]]
    by_id = {row["check_id"]: row for row in checks}
    if set(by_id) != set(requirement_order):
        raise EditorialReadinessError("computed checks do not match the profile")
    checks = [by_id[check_id] for check_id in requirement_order]
    failed = [row["check_id"] for row in checks if not row["passed"]]
    return {
        "package_id": package["package_id"],
        "subject_case_slug": package["subject_case_slug"],
        "profile": package["profile"],
        "title": case["title"],
        "status": "publishable" if not failed else "blocked",
        "publishable": not failed,
        "checks": checks,
        "failed_check_ids": failed,
    }


def build_editorial_readiness(
    wire: Mapping[str, Any],
    primary: Mapping[str, Any],
    corroboration: Mapping[str, Any],
    investigations: Mapping[str, Any],
    network_rounds: Mapping[str, Any],
    source_workflow: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validate_prior_newswire_document(wire)
    validate_primary_document_index(primary)
    validate_corroboration(corroboration)
    validate_investigations(investigations)
    validate_network_rounds(network_rounds)
    validate_source_workflow_summary(source_workflow)
    cfg = dict(config) if config is not None else load_editorial_packages()
    if cfg.get("automatic_publication") is not False:
        raise EditorialReadinessError("automatic publication is prohibited")
    package_ids = [row["package_id"] for row in cfg["packages"]]
    source_by_package = {
        row["package_id"]: row for row in source_workflow["packages"]
    }
    if set(source_by_package) != set(package_ids):
        raise EditorialReadinessError(
            "source-workflow package coverage does not match editorial packages"
        )
    cases = {row["slug"]: row for row in investigations["cases"]}
    primary_by_id = {row["document_id"]: row for row in primary["documents"]}
    packages = []
    for package in cfg["packages"]:
        case = cases.get(package["subject_case_slug"])
        if case is None:
            raise EditorialReadinessError("editorial package references an unknown case")
        unknown_documents = set(package["primary_document_ids"]) - set(primary_by_id)
        if unknown_documents:
            raise EditorialReadinessError("editorial package references an unknown document")
        packages.append(
            _package_assessment(
                package,
                case,
                primary_by_id,
                source_by_package[package["package_id"]],
                network_rounds,
            )
        )
    wire_assessment = _wire_assessment(wire, corroboration)
    inputs = {
        "newswire": (
            "https://palimpsest.info/readings/newswire-latest.json",
            wire,
        ),
        "primary_documents": (
            "https://palimpsest.info/readings/primary-documents-latest.json",
            primary,
        ),
        "corroboration": (
            "https://palimpsest.info/readings/corroboration-latest.json",
            corroboration,
        ),
        "investigations": (
            "https://palimpsest.info/readings/investigations-latest.json",
            investigations,
        ),
        "network_rounds": (
            "https://palimpsest.info/readings/network-rounds-latest.json",
            network_rounds,
        ),
        "source_workflow": (
            "https://palimpsest.info/readings/source-workflow-latest.json",
            source_workflow,
        ),
    }
    source_inputs = {
        key: {
            "url": url,
            "generated_at": value["generated_at"],
            "sha256": hashlib.sha256(canonical_json_bytes(value)).hexdigest(),
        }
        for key, (url, value) in inputs.items()
    }
    generated_at = max(row["generated_at"] for row in source_inputs.values())
    explainers = [row for row in packages if row["profile"] == "explainer"]
    investigations_rows = [
        row for row in packages if row["profile"] == "investigation"
    ]
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "scope": (
            "Publication readiness for wire briefs, explainers, and investigations; "
            "a passing record authorizes human review, never automatic publication."
        ),
        "method": (
            "Derive evidence checks from validated public artifacts and require explicit "
            "dated editorial or protected-source receipts for human-only judgments."
        ),
        "automatic_publication": False,
        "source_inputs": source_inputs,
        "publication_profiles": [
            {"profile": profile, "requirements": requirements}
            for profile, requirements in _PROFILE_REQUIREMENTS.items()
        ],
        "wire": wire_assessment,
        "packages": packages,
        "summary": {
            "wire_events": wire_assessment["n_events"],
            "wire_eligible": wire_assessment["eligible_events"],
            "wire_blocked": wire_assessment["blocked_events"],
            "explainers": len(explainers),
            "explainers_publishable": sum(row["publishable"] for row in explainers),
            "investigations": len(investigations_rows),
            "investigations_publishable": sum(
                row["publishable"] for row in investigations_rows
            ),
        },
    }
    validate_editorial_readiness(document)
    return document


def validate_editorial_readiness(value: Any) -> None:
    top = _exact(value, _TOP_FIELDS, "editorial_readiness")
    if top["schema_version"] != SCHEMA_VERSION:
        raise EditorialReadinessError("unsupported editorial readiness version")
    generated_at = _timestamp(top["generated_at"], "generated_at")
    _text(top["scope"], "scope")
    _text(top["method"], "method")
    if top["automatic_publication"] is not False:
        raise EditorialReadinessError("automatic publication was enabled")
    inputs = top["source_inputs"]
    if type(inputs) is not dict or set(inputs) != {
        "newswire",
        "primary_documents",
        "corroboration",
        "investigations",
        "network_rounds",
        "source_workflow",
    }:
        raise EditorialReadinessError("editorial source inputs are incomplete")
    for name, raw in inputs.items():
        row = _exact(raw, _INPUT_FIELDS, f"source_inputs.{name}")
        _url(row["url"], f"source_inputs.{name}.url")
        timestamp = _timestamp(row["generated_at"], f"source_inputs.{name}.generated_at")
        if timestamp > generated_at:
            raise EditorialReadinessError("editorial input is later than output")
        if type(row["sha256"]) is not str or not _SHA_RE.fullmatch(row["sha256"]):
            raise EditorialReadinessError("editorial input hash is invalid")
    profiles = top["publication_profiles"]
    expected_profiles = [
        {"profile": profile, "requirements": requirements}
        for profile, requirements in _PROFILE_REQUIREMENTS.items()
    ]
    if profiles != expected_profiles:
        raise EditorialReadinessError("publication profiles were broadened")
    for raw in profiles:
        _exact(raw, _PROFILE_FIELDS, "publication_profile")
    wire = _exact(top["wire"], _WIRE_FIELDS, "wire")
    wire_events = wire["events"]
    if type(wire_events) is not list or len(wire_events) > 8_192:
        raise EditorialReadinessError("wire readiness events are outside their bound")
    event_ids = []
    for raw in wire_events:
        row = _exact(raw, _WIRE_EVENT_FIELDS, "wire.event")
        event_ids.append(row["event_id"])
        if row["status"] not in {"eligible", "blocked"}:
            raise EditorialReadinessError("wire event status is invalid")
        _text(row["evidence_label"], "wire.event.evidence_label", maximum=80)
        if type(row["n_independent_groups"]) is not int or row["n_independent_groups"] < 1:
            raise EditorialReadinessError("wire independent-group count is invalid")
        if type(row["failed_check_ids"]) is not list:
            raise EditorialReadinessError("wire failed checks are invalid")
        if row["failed_check_ids"] != sorted(set(row["failed_check_ids"])) or not set(
            row["failed_check_ids"]
        ) <= set(_PROFILE_REQUIREMENTS["wire"]):
            raise EditorialReadinessError("wire failed checks are unknown or duplicated")
        if (row["status"] == "eligible") is bool(row["failed_check_ids"]):
            raise EditorialReadinessError("wire event status contradicts failed checks")
    if event_ids != sorted(set(event_ids)):
        raise EditorialReadinessError("wire readiness events are not unique and sorted")
    eligible = sum(row["status"] == "eligible" for row in wire_events)
    if (
        wire["n_events"] != len(wire_events)
        or wire["eligible_events"] != eligible
        or wire["blocked_events"] != len(wire_events) - eligible
        or type(wire["checks"]) is not dict
        or set(wire["checks"]) != set(_PROFILE_REQUIREMENTS["wire"])
    ):
        raise EditorialReadinessError("wire readiness accounting is inconsistent")
    expected_wire_checks = {
        check_id: sum(
            check_id not in row["failed_check_ids"] for row in wire_events
        )
        for check_id in _PROFILE_REQUIREMENTS["wire"]
    }
    if wire["checks"] != expected_wire_checks:
        raise EditorialReadinessError("wire check totals are inconsistent")
    packages = top["packages"]
    if type(packages) is not list or len(packages) > 256:
        raise EditorialReadinessError("editorial readiness packages are outside their bound")
    package_ids = []
    for raw in packages:
        row = _exact(raw, _PACKAGE_FIELDS, "package")
        package_ids.append(row["package_id"])
        if row["profile"] not in {"explainer", "investigation"}:
            raise EditorialReadinessError("package profile is invalid")
        checks = row["checks"]
        if type(checks) is not list:
            raise EditorialReadinessError("package checks must be an array")
        check_ids = []
        for raw_check in checks:
            check = _exact(raw_check, _CHECK_FIELDS, "package.check")
            check_ids.append(check["check_id"])
            if type(check["passed"]) is not bool:
                raise EditorialReadinessError("package check result is not boolean")
            required = check["required"]
            observed = check["observed"]
            if type(required) is bool:
                if type(observed) is not bool:
                    raise EditorialReadinessError(
                        "boolean package check has a non-boolean observation"
                    )
                expected_passed = observed is required
            elif type(required) is int and required >= 0:
                if type(observed) is not int or observed < 0:
                    raise EditorialReadinessError(
                        "numeric package check has an invalid observation"
                    )
                expected_passed = observed >= required
            else:
                raise EditorialReadinessError("package check requirement is invalid")
            if check["passed"] is not expected_passed:
                raise EditorialReadinessError(
                    "package check result contradicts required and observed"
                )
            _text(check["label"], "package.check.label", maximum=240)
            _text(check["detail"], "package.check.detail", maximum=1_000)
        if check_ids != _PROFILE_REQUIREMENTS[row["profile"]]:
            raise EditorialReadinessError("package checks do not match its profile")
        failed = [check["check_id"] for check in checks if not check["passed"]]
        if row["failed_check_ids"] != failed:
            raise EditorialReadinessError("package failed-check accounting is inconsistent")
        publishable = not failed
        if row["publishable"] is not publishable or row["status"] != (
            "publishable" if publishable else "blocked"
        ):
            raise EditorialReadinessError("package publication status is inconsistent")
    if package_ids != sorted(set(package_ids)):
        raise EditorialReadinessError("editorial packages are not unique and sorted")
    summary = _exact(top["summary"], _SUMMARY_FIELDS, "summary")
    expected_summary = {
        "wire_events": wire["n_events"],
        "wire_eligible": wire["eligible_events"],
        "wire_blocked": wire["blocked_events"],
        "explainers": sum(row["profile"] == "explainer" for row in packages),
        "explainers_publishable": sum(
            row["profile"] == "explainer" and row["publishable"] for row in packages
        ),
        "investigations": sum(row["profile"] == "investigation" for row in packages),
        "investigations_publishable": sum(
            row["profile"] == "investigation" and row["publishable"]
            for row in packages
        ),
    }
    if summary != expected_summary:
        raise EditorialReadinessError("editorial summary is inconsistent")
