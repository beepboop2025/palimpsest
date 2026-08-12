"""Conservative, human-reviewed joins between wire events and primary documents."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from core.newswire import validate_prior_newswire_document
from core.primary_documents import validate_primary_document_index


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DECISIONS_PATH = ROOT / "config" / "corroboration_decisions.json"
DEFAULT_OUTPUT_PATH = ROOT / "readings" / "corroboration-latest.json"

SCHEMA_VERSION = "palimpsest-corroboration.v1"
DECISIONS_VERSION = "palimpsest-corroboration-decisions.v1"
MAX_CANDIDATES = 16_384
MAX_PERIOD_HOURS = 31 * 24


class CorroborationError(ValueError):
    """An evidence input, review decision, or public join failed closed."""


_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EVENT_ID_RE = re.compile(r"^event-[0-9a-f]{24}$")
_DOCUMENT_ID_RE = re.compile(r"^document-[0-9a-f]{24}$")
_VINTAGE_ID_RE = re.compile(r"^documentv-[0-9a-f]{24}$")
_CANDIDATE_ID_RE = re.compile(r"^candidate-[0-9a-f]{24}$")
_REVIEWER_ID_RE = re.compile(r"^editor-[0-9a-f]{12}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_DECISION_FIELDS = frozenset(
    {"candidate_id", "status", "reviewed_at", "reviewer_id", "rationale"}
)
_DECISION_TOP_FIELDS = frozenset(
    {"config_version", "automatic_confirmation", "decisions"}
)
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "source_inputs",
        "scope",
        "method",
        "review_policy",
        "n_events",
        "n_candidate_edges",
        "n_reviewed_edges",
        "n_accepted_edges",
        "n_rejected_edges",
        "n_events_with_primary_documents",
        "n_corroborated_events",
        "candidates",
        "events",
    }
)
_INPUT_FIELDS = frozenset({"url", "generated_at", "sha256"})
_POLICY_FIELDS = frozenset(
    {
        "automatic_confirmation",
        "maximum_period_hours",
        "accepted_decision_required",
        "catalog_metadata_is_corroboration",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "event_id",
        "document_id",
        "source_id",
        "document_url",
        "document_vintage_id",
        "document_content_sha256",
        "document_publication_time",
        "document_first_retrieved_at",
        "event_published_at",
        "independence_group",
        "event_group_ids",
        "independent_of_event",
        "capture_scope",
        "subject_keys",
        "match_basis",
        "eligible_for_corroboration",
        "review",
    }
)
_REVIEW_FIELDS = frozenset(
    {"status", "reviewed_at", "reviewer_id", "rationale"}
)
_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "original_evidence_strength",
        "original_group_ids",
        "candidate_ids",
        "accepted_candidate_ids",
        "accepted_document_ids",
        "attached_primary_groups",
        "n_independent_groups",
        "has_primary_document",
        "status",
    }
)

_SUBJECT_ALIASES: dict[str, tuple[str, ...]] = {
    "housing": ("housing", "property", "properties", "home", "homes", "resale", "real estate"),
    "employment": ("employment", "unemployment", "jobs", "jobless", "labour", "labor"),
    "retail": ("retail", "consumption", "consumer spending", "sales"),
    "industrial-output": ("industrial output", "industrial production", "factory output", "manufacturing"),
    "prices": ("prices", "inflation", "deflation", "cpi", "ppi", "consumer price", "producer price"),
    "investment": ("investment", "fixed asset", "capex", "capital spending"),
    "credit": ("credit", "loans", "financing", "money supply", "social financing", "tsf"),
    "trade": ("trade", "exports", "imports", "customs", "tariff"),
    "freight": ("freight", "cargo", "throughput", "shipping", "shipment"),
    "parcels": ("parcel", "parcels", "postal", "delivery", "courier"),
    "electricity": ("electricity", "power consumption", "energy consumption"),
    "ports": ("port calls", "port", "ports", "vessel", "ships"),
    "no2": ("nitrogen dioxide", "no2", "air pollution", "emissions"),
    "nightlights": ("nighttime lights", "night lights", "nightlights", "radiance"),
    "company-filings": ("filing", "filings", "disclosure", "earnings", "annual report", "results"),
    "enterprise-survey": ("enterprise survey", "firm survey", "business survey"),
}


def _canonical_bytes(value: Any) -> bytes:
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
        raise CorroborationError("corroboration value is not canonical JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    return _canonical_bytes(value)


def _exact(value: Any, fields: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        actual = set(value) if type(value) is dict else set()
        raise CorroborationError(
            f"{path} fields do not match contract "
            f"(missing={sorted(fields - actual)}, unknown={sorted(actual - fields)})"
        )
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not _TS_RE.fullmatch(value):
        raise CorroborationError(f"{path} is not a canonical UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise CorroborationError(f"{path} is not a real timestamp") from exc
    return value


def _clock(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _text(value: Any, path: str, *, maximum: int = 2_000, empty: bool = False) -> str:
    if type(value) is not str:
        raise CorroborationError(f"{path} must be text")
    value = unicodedata.normalize("NFC", value)
    if len(value) > maximum or (not empty and not value.strip()):
        raise CorroborationError(f"{path} has invalid length")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise CorroborationError(f"{path} contains unsafe Unicode")
    return value


def _https_url(value: Any, path: str) -> str:
    value = _text(value, path, maximum=2_048)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise CorroborationError(f"{path} is not a valid URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CorroborationError(f"{path} must be an uncredentialed HTTPS URL")
    return value


def _strict_json(path: Path) -> Mapping[str, Any]:
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CorroborationError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                CorroborationError(f"non-finite JSON number: {token}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorroborationError(f"cannot read corroboration decisions: {path}") from exc
    if type(value) is not dict:
        raise CorroborationError("corroboration decisions must be an object")
    return value


def load_decisions(path: Path | str = DEFAULT_DECISIONS_PATH) -> list[dict[str, Any]]:
    top = _exact(_strict_json(Path(path)), _DECISION_TOP_FIELDS, "decisions")
    if top["config_version"] != DECISIONS_VERSION:
        raise CorroborationError("unsupported corroboration decisions version")
    if top["automatic_confirmation"] is not False:
        raise CorroborationError("automatic corroboration confirmation is prohibited")
    decisions = top["decisions"]
    if type(decisions) is not list or len(decisions) > MAX_CANDIDATES:
        raise CorroborationError("corroboration decisions are outside their bound")
    result = []
    seen = set()
    for index, raw in enumerate(decisions):
        row = dict(_exact(raw, _DECISION_FIELDS, f"decisions[{index}]"))
        candidate_id = row["candidate_id"]
        if type(candidate_id) is not str or not _CANDIDATE_ID_RE.fullmatch(candidate_id):
            raise CorroborationError("decision candidate_id is invalid")
        if candidate_id in seen:
            raise CorroborationError("duplicate corroboration decision")
        seen.add(candidate_id)
        if row["status"] not in {"accepted", "rejected"}:
            raise CorroborationError("decision status must be accepted or rejected")
        _timestamp(row["reviewed_at"], "decision.reviewed_at")
        if type(row["reviewer_id"]) is not str or not _REVIEWER_ID_RE.fullmatch(
            row["reviewer_id"]
        ):
            raise CorroborationError("decision reviewer_id must be pseudonymous")
        _text(row["rationale"], "decision.rationale", maximum=1_000)
        result.append(row)
    return sorted(result, key=lambda row: row["candidate_id"])


def _fold(value: str) -> str:
    return " ".join(
        re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", value).casefold())
    )


def _subject_keys(value: str) -> set[str]:
    folded = f" {_fold(value)} "
    keys = set()
    for key, aliases in _SUBJECT_ALIASES.items():
        if any(f" {_fold(alias)} " in folded for alias in aliases):
            keys.add(key)
    return keys


def _candidate_edges(
    wire: Mapping[str, Any], primary: Mapping[str, Any]
) -> list[dict[str, Any]]:
    edges = []
    for event in wire["events"]:
        event_groups = sorted(row["group_id"] for row in event["evidence_groups"])
        event_urls = {row["url"] for row in event["evidence_refs"]}
        event_keys = _subject_keys(f"{event['headline']} {event['dek']}")
        for document in primary["documents"]:
            vintage = document["current_vintage"]
            document_keys = _subject_keys(
                " ".join(
                    [document["name"], *document["subjects"], *document["sectors"]]
                )
            )
            exact_url = document["original_url"] in event_urls
            publication_time = vintage["publication_time"]
            hours_apart = None
            period_compatible = False
            if publication_time is not None:
                hours_apart = abs(
                    (_clock(event["published_at"]) - _clock(publication_time)).total_seconds()
                ) / 3600
                period_compatible = hours_apart <= MAX_PERIOD_HOURS
            shared_keys = sorted(event_keys & document_keys)
            topical = event["desk"] == "economy" and bool(shared_keys)
            if not exact_url and not (topical and period_compatible):
                continue
            basis = []
            if exact_url:
                basis.append("exact-canonical-url")
            if topical and period_compatible:
                basis.append("subject-period-candidate")
            identity = {
                "event_id": event["event_id"],
                "document_id": document["document_id"],
                "vintage_id": vintage["vintage_id"],
                "match_basis": basis,
            }
            candidate_id = (
                "candidate-" + hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:24]
            )
            independent = document["independence_group"] not in event_groups
            edges.append(
                {
                    "candidate_id": candidate_id,
                    "event_id": event["event_id"],
                    "document_id": document["document_id"],
                    "source_id": document["source_id"],
                    "document_url": document["original_url"],
                    "document_vintage_id": vintage["vintage_id"],
                    "document_content_sha256": vintage["content_sha256"],
                    "document_publication_time": publication_time,
                    "document_first_retrieved_at": vintage["first_retrieved_at"],
                    "event_published_at": event["published_at"],
                    "independence_group": document["independence_group"],
                    "event_group_ids": event_groups,
                    "independent_of_event": independent,
                    "capture_scope": document["capture_scope"],
                    "subject_keys": shared_keys,
                    "match_basis": basis,
                    "eligible_for_corroboration": (
                        document["capture_scope"] == "release_document"
                        and period_compatible
                        and independent
                    ),
                    "review": {
                        "status": "unreviewed",
                        "reviewed_at": None,
                        "reviewer_id": None,
                        "rationale": (
                            "Automation nominated this edge; an editor must compare the "
                            "underlying claim, period, geography, and methodology."
                        ),
                    },
                }
            )
            if len(edges) > MAX_CANDIDATES:
                raise CorroborationError("candidate edge count exceeds its v1 bound")
    return sorted(edges, key=lambda row: row["candidate_id"])


def build_corroboration(
    wire: Mapping[str, Any],
    primary: Mapping[str, Any],
    *,
    decisions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a reviewed join without automatically confirming any candidate."""

    validate_prior_newswire_document(wire)
    validate_primary_document_index(primary)
    decision_by_id = {}
    for raw in decisions:
        row = dict(_exact(raw, _DECISION_FIELDS, "decision"))
        candidate_id = row["candidate_id"]
        if candidate_id in decision_by_id:
            raise CorroborationError("duplicate corroboration decision")
        if row["status"] not in {"accepted", "rejected"}:
            raise CorroborationError("decision status is invalid")
        _timestamp(row["reviewed_at"], "decision.reviewed_at")
        if type(row["reviewer_id"]) is not str or not _REVIEWER_ID_RE.fullmatch(
            row["reviewer_id"]
        ):
            raise CorroborationError("decision reviewer_id must be pseudonymous")
        _text(row["rationale"], "decision.rationale", maximum=1_000)
        decision_by_id[candidate_id] = row

    candidates = _candidate_edges(wire, primary)
    candidate_ids = {row["candidate_id"] for row in candidates}
    unknown = set(decision_by_id) - candidate_ids
    if unknown:
        raise CorroborationError("a review decision references a stale or unknown candidate")
    for candidate in candidates:
        decision = decision_by_id.get(candidate["candidate_id"])
        if decision is None:
            continue
        if decision["status"] == "accepted" and not candidate[
            "eligible_for_corroboration"
        ]:
            raise CorroborationError(
                "an ineligible catalog, period, or dependent candidate was accepted"
            )
        candidate["review"] = {
            "status": decision["status"],
            "reviewed_at": decision["reviewed_at"],
            "reviewer_id": decision["reviewer_id"],
            "rationale": decision["rationale"],
        }

    by_event: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_event.setdefault(candidate["event_id"], []).append(candidate)
    events = []
    for event in wire["events"]:
        event_candidates = by_event.get(event["event_id"], [])
        accepted = [
            row for row in event_candidates if row["review"]["status"] == "accepted"
        ]
        original_groups = sorted(row["group_id"] for row in event["evidence_groups"])
        attached_groups = sorted({row["independence_group"] for row in accepted})
        all_groups = set(original_groups) | set(attached_groups)
        events.append(
            {
                "event_id": event["event_id"],
                "original_evidence_strength": event["evidence_strength"],
                "original_group_ids": original_groups,
                "candidate_ids": sorted(row["candidate_id"] for row in event_candidates),
                "accepted_candidate_ids": sorted(
                    row["candidate_id"] for row in accepted
                ),
                "accepted_document_ids": sorted(
                    {row["document_id"] for row in accepted}
                ),
                "attached_primary_groups": attached_groups,
                "n_independent_groups": len(all_groups),
                "has_primary_document": bool(accepted),
                "status": "corroborated" if len(all_groups) >= 2 else "single-group",
            }
        )
    events.sort(key=lambda row: row["event_id"])
    input_times = [wire["generated_at"], primary["generated_at"]]
    input_times.extend(
        row["reviewed_at"] for row in decision_by_id.values()
    )
    generated_at = max(input_times)
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "source_inputs": {
            "newswire": {
                "url": "https://palimpsest.info/readings/newswire-latest.json",
                "generated_at": wire["generated_at"],
                "sha256": hashlib.sha256(_canonical_bytes(wire)).hexdigest(),
            },
            "primary_documents": {
                "url": "https://palimpsest.info/readings/primary-documents-latest.json",
                "generated_at": primary["generated_at"],
                "sha256": hashlib.sha256(_canonical_bytes(primary)).hexdigest(),
            },
        },
        "scope": (
            "Candidate and editor-reviewed links between wire event dossiers and "
            "captured primary documents; a topical candidate is not corroboration."
        ),
        "method": (
            "Nominate exact-URL or reviewed-taxonomy subject/period matches; require "
            "a recorded human decision, release-document scope, compatible clocks, "
            "and a distinct independence group before increasing dossier support."
        ),
        "review_policy": {
            "automatic_confirmation": False,
            "maximum_period_hours": MAX_PERIOD_HOURS,
            "accepted_decision_required": True,
            "catalog_metadata_is_corroboration": False,
        },
        "n_events": len(events),
        "n_candidate_edges": len(candidates),
        "n_reviewed_edges": sum(
            row["review"]["status"] != "unreviewed" for row in candidates
        ),
        "n_accepted_edges": sum(
            row["review"]["status"] == "accepted" for row in candidates
        ),
        "n_rejected_edges": sum(
            row["review"]["status"] == "rejected" for row in candidates
        ),
        "n_events_with_primary_documents": sum(
            row["has_primary_document"] for row in events
        ),
        "n_corroborated_events": sum(row["status"] == "corroborated" for row in events),
        "candidates": candidates,
        "events": events,
    }
    validate_corroboration(document)
    return document


def validate_corroboration(value: Any) -> None:
    top = _exact(value, _TOP_FIELDS, "corroboration")
    if top["schema_version"] != SCHEMA_VERSION:
        raise CorroborationError("unsupported corroboration version")
    generated_at = _timestamp(top["generated_at"], "generated_at")
    inputs = _exact(
        top["source_inputs"],
        frozenset({"newswire", "primary_documents"}),
        "source_inputs",
    )
    for name, raw in inputs.items():
        row = _exact(raw, _INPUT_FIELDS, f"source_inputs.{name}")
        _text(row["url"], f"source_inputs.{name}.url", maximum=2_048)
        timestamp = _timestamp(row["generated_at"], f"source_inputs.{name}.generated_at")
        if timestamp > generated_at:
            raise CorroborationError("source input is later than output")
        if type(row["sha256"]) is not str or not _SHA_RE.fullmatch(row["sha256"]):
            raise CorroborationError("source input hash is invalid")
    _text(top["scope"], "scope")
    _text(top["method"], "method")
    policy = _exact(top["review_policy"], _POLICY_FIELDS, "review_policy")
    if policy != {
        "automatic_confirmation": False,
        "maximum_period_hours": MAX_PERIOD_HOURS,
        "accepted_decision_required": True,
        "catalog_metadata_is_corroboration": False,
    }:
        raise CorroborationError("review policy was broadened")
    candidates = top["candidates"]
    events = top["events"]
    if type(candidates) is not list or len(candidates) > MAX_CANDIDATES:
        raise CorroborationError("candidate array is outside its bound")
    if type(events) is not list or len(events) > 8_192:
        raise CorroborationError("event array is outside its bound")
    candidate_ids = []
    accepted_ids = set()
    rejected_ids = set()
    candidates_by_event: dict[str, set[str]] = {}
    documents_by_candidate = {}
    groups_by_candidate = {}
    for index, raw in enumerate(candidates):
        row = _exact(raw, _CANDIDATE_FIELDS, f"candidates[{index}]")
        candidate_id = row["candidate_id"]
        if type(candidate_id) is not str or not _CANDIDATE_ID_RE.fullmatch(candidate_id):
            raise CorroborationError("candidate_id is invalid")
        candidate_ids.append(candidate_id)
        if type(row["event_id"]) is not str or not _EVENT_ID_RE.fullmatch(row["event_id"]):
            raise CorroborationError("candidate event_id is invalid")
        if type(row["document_id"]) is not str or not _DOCUMENT_ID_RE.fullmatch(
            row["document_id"]
        ):
            raise CorroborationError("candidate document_id is invalid")
        if type(row["document_vintage_id"]) is not str or not _VINTAGE_ID_RE.fullmatch(
            row["document_vintage_id"]
        ):
            raise CorroborationError("candidate document vintage is invalid")
        _https_url(row["document_url"], "candidate.document_url")
        for field in ("source_id", "independence_group"):
            if type(row[field]) is not str or not _ID_RE.fullmatch(row[field]):
                raise CorroborationError(f"candidate {field} is invalid")
        if type(row["document_content_sha256"]) is not str or not _SHA_RE.fullmatch(
            row["document_content_sha256"]
        ):
            raise CorroborationError("candidate content hash is invalid")
        publication_time = row["document_publication_time"]
        if publication_time is not None:
            _timestamp(publication_time, "candidate publication time")
        retrieved_at = _timestamp(
            row["document_first_retrieved_at"], "candidate retrieval time"
        )
        event_published_at = _timestamp(
            row["event_published_at"], "candidate event time"
        )
        if publication_time is not None and _clock(publication_time) > _clock(retrieved_at):
            raise CorroborationError("candidate document publication follows retrieval")
        if (
            type(row["event_group_ids"]) is not list
            or row["event_group_ids"] != sorted(set(row["event_group_ids"]))
            or not row["event_group_ids"]
            or any(
                type(group) is not str or not _ID_RE.fullmatch(group)
                for group in row["event_group_ids"]
            )
        ):
            raise CorroborationError("candidate event groups are invalid")
        if type(row["independent_of_event"]) is not bool or type(
            row["eligible_for_corroboration"]
        ) is not bool:
            raise CorroborationError("candidate eligibility flags are invalid")
        if row["capture_scope"] not in {
            "catalog_metadata",
            "release_document",
            "structured_observations",
        }:
            raise CorroborationError("candidate capture scope is invalid")
        for field in ("subject_keys", "match_basis"):
            if type(row[field]) is not list or row[field] != sorted(set(row[field])):
                raise CorroborationError(f"candidate {field} is invalid")
        if not set(row["subject_keys"]) <= set(_SUBJECT_ALIASES):
            raise CorroborationError("candidate subject keys are outside the taxonomy")
        if not row["match_basis"] or not set(row["match_basis"]) <= {
            "exact-canonical-url",
            "subject-period-candidate",
        }:
            raise CorroborationError("candidate match basis is invalid")
        expected_candidate_id = "candidate-" + hashlib.sha256(
            _canonical_bytes(
                {
                    "event_id": row["event_id"],
                    "document_id": row["document_id"],
                    "vintage_id": row["document_vintage_id"],
                    "match_basis": row["match_basis"],
                }
            )
        ).hexdigest()[:24]
        if candidate_id != expected_candidate_id:
            raise CorroborationError("candidate_id does not match its evidence edge")
        independent = row["independence_group"] not in row["event_group_ids"]
        if row["independent_of_event"] is not independent:
            raise CorroborationError("candidate independence flag is inconsistent")
        period_compatible = publication_time is not None and abs(
            (_clock(event_published_at) - _clock(publication_time)).total_seconds()
        ) <= MAX_PERIOD_HOURS * 3600
        expected_eligible = (
            row["capture_scope"] == "release_document"
            and period_compatible
            and independent
        )
        if row["eligible_for_corroboration"] is not expected_eligible:
            raise CorroborationError("candidate eligibility is inconsistent")
        review = _exact(row["review"], _REVIEW_FIELDS, "candidate.review")
        if review["status"] not in {"unreviewed", "accepted", "rejected"}:
            raise CorroborationError("candidate review status is invalid")
        if review["status"] == "unreviewed":
            if review["reviewed_at"] is not None or review["reviewer_id"] is not None:
                raise CorroborationError("unreviewed candidate has review identity")
        else:
            reviewed_at = _timestamp(
                review["reviewed_at"], "candidate.review.reviewed_at"
            )
            if _clock(reviewed_at) < max(
                _clock(retrieved_at), _clock(event_published_at)
            ):
                raise CorroborationError("candidate review predates its evidence")
            if not _REVIEWER_ID_RE.fullmatch(review["reviewer_id"] or ""):
                raise CorroborationError("candidate reviewer_id is invalid")
        _text(review["rationale"], "candidate.review.rationale", maximum=1_000)
        if review["status"] == "accepted":
            if not row["eligible_for_corroboration"]:
                raise CorroborationError("ineligible candidate is accepted")
            accepted_ids.add(candidate_id)
        elif review["status"] == "rejected":
            rejected_ids.add(candidate_id)
        candidates_by_event.setdefault(row["event_id"], set()).add(candidate_id)
        documents_by_candidate[candidate_id] = row["document_id"]
        groups_by_candidate[candidate_id] = row["independence_group"]
    if candidate_ids != sorted(set(candidate_ids)):
        raise CorroborationError("candidates are not unique and sorted")
    candidate_lookup = {
        candidate["candidate_id"]: candidate for candidate in candidates
    }
    event_ids = []
    for index, raw in enumerate(events):
        row = _exact(raw, _EVENT_FIELDS, f"events[{index}]")
        event_id = row["event_id"]
        if type(event_id) is not str or not _EVENT_ID_RE.fullmatch(event_id):
            raise CorroborationError("event summary id is invalid")
        event_ids.append(event_id)
        if row["original_evidence_strength"] not in {
            "single-source",
            "single-primary-source",
            "single-measurement-source",
            "multi-source",
        }:
            raise CorroborationError("event evidence strength is invalid")
        for field in (
            "original_group_ids",
            "candidate_ids",
            "accepted_candidate_ids",
            "accepted_document_ids",
            "attached_primary_groups",
        ):
            if type(row[field]) is not list or row[field] != sorted(set(row[field])):
                raise CorroborationError(f"event {field} is invalid")
        if not row["original_group_ids"] or any(
            type(group) is not str or not _ID_RE.fullmatch(group)
            for group in row["original_group_ids"]
        ):
            raise CorroborationError("event original groups are invalid")
        expected_candidates = sorted(candidates_by_event.get(event_id, set()))
        expected_accepted = sorted(set(expected_candidates) & accepted_ids)
        if row["candidate_ids"] != expected_candidates or row[
            "accepted_candidate_ids"
        ] != expected_accepted:
            raise CorroborationError("event candidate accounting is inconsistent")
        if any(
            candidate_lookup[candidate_id]["event_group_ids"]
            != row["original_group_ids"]
            for candidate_id in expected_candidates
        ):
            raise CorroborationError("candidate groups do not match their event")
        if row["accepted_document_ids"] != sorted(
            {documents_by_candidate[item] for item in expected_accepted}
        ) or row["attached_primary_groups"] != sorted(
            {groups_by_candidate[item] for item in expected_accepted}
        ):
            raise CorroborationError("event primary-document accounting is inconsistent")
        group_count = len(set(row["original_group_ids"]) | set(row["attached_primary_groups"]))
        if row["n_independent_groups"] != group_count:
            raise CorroborationError("event independent-group count is inconsistent")
        if row["has_primary_document"] is not bool(expected_accepted):
            raise CorroborationError("event primary-document flag is inconsistent")
        expected_status = "corroborated" if group_count >= 2 else "single-group"
        if row["status"] != expected_status:
            raise CorroborationError("event corroboration status is inconsistent")
    if event_ids != sorted(set(event_ids)):
        raise CorroborationError("event summaries are not unique and sorted")
    if not set(candidates_by_event) <= set(event_ids):
        raise CorroborationError("candidate references an omitted event")
    expected_counts = {
        "n_events": len(events),
        "n_candidate_edges": len(candidates),
        "n_reviewed_edges": len(accepted_ids | rejected_ids),
        "n_accepted_edges": len(accepted_ids),
        "n_rejected_edges": len(rejected_ids),
        "n_events_with_primary_documents": sum(row["has_primary_document"] for row in events),
        "n_corroborated_events": sum(row["status"] == "corroborated" for row in events),
    }
    for field, expected in expected_counts.items():
        if top[field] != expected:
            raise CorroborationError(f"{field} is inconsistent")
