"""Deterministic, evidence-bounded assessments for every public wire event.

The newswire records what registered sources published.  This companion layer
states what Palimpsest can responsibly conclude from that source structure and
from the already-normalized collector stories.  It never fetches article bodies,
never turns a topical link into causal proof, and emits an explicit abstention
when current collector evidence is unavailable.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from core import newswire as newswire_model
from core import event_brief
from core import event_interconnection
from core import peer_context as peer_context_model
from core.claim_support import has_quorum

load_optional_live_families = event_brief.load_optional_live_families
load_optional_archive_context = event_brief.load_optional_archive_context
load_optional_corroboration = event_brief.load_optional_corroboration
load_optional_peer_warehouses = event_interconnection.load_optional_peer_warehouses


SCHEMA_VERSION_V1 = "palimpsest-event-analysis.v1"
SCHEMA_VERSION = "palimpsest-event-analysis.v2"
METHOD_V1 = (
    "Deterministic assessment of one validated newswire event against its "
    "independent-source structure and only the collector stories explicitly "
    "declared by that event. Collector joins remain topic-surface-only: no "
    "article body is fetched, no generative model is used, and no current "
    "measurement is represented as article-specific verification or causation."
)
METHOD_V1_WITH_PEERS = (
    METHOD_V1
    + " Optional peer_context rows name GreatFire, OONI, CDT, or Weiboscope and "
    "the date of that peer's verdict; they are attributed context, not "
    "Palimpsest capture, and never share a denominator with Palimpsest."
)
METHOD = event_brief.METHOD

_ANALYSIS_ID_RE = re.compile(r"^analysisv-[0-9a-f]{24}$")
_EVENT_ID_RE = re.compile(r"^event-[0-9a-f]{24}$")
_EVENT_VERSION_ID_RE = re.compile(r"^eventv-[0-9a-f]{24}$")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CLAIM_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_DISPOSITIONS = frozenset(
    {"outside-remit", "source-assessment", "collector-context", "collector-abstention"}
)
_SCOPE_STATUSES = frozenset({"in-scope", "outside-remit"})
_COLLECTOR_STATUSES = frozenset({"live", "degraded", "stale", "missing", "corrupt"})
_TOP_FIELDS_V1 = frozenset(
    {
        "schema_version",
        "analysis_id",
        "event_id",
        "event_version_id",
        "event_url",
        "url",
        "generated_at",
        "disposition",
        "scope_status",
        "position",
        "rationale",
        "evidence_assessment",
        "collector_context",
        "limitations",
        "method",
    }
)
_TOP_FIELDS_V1_WITH_PEERS = _TOP_FIELDS_V1 | {"peer_context"}
_TOP_FIELDS = _TOP_FIELDS_V1 | event_brief._TOP_V2_EXTRA
_EVIDENCE_FIELDS = frozenset(
    {"strength", "independent_groups", "source_count", "conclusion"}
)
_COLLECTOR_FIELDS = frozenset(
    {
        "signal_id",
        "status",
        "headline",
        "finding",
        "metric",
        "source_timestamp",
        "story_url",
        "evidence_url",
        "input_sha256",
        "claim_fingerprint",
        "method_summary",
        "method_version",
        "relation",
        "interpretation",
    }
)
_METRIC_FIELDS = frozenset({"label", "value", "unit", "denominator"})
_DENOMINATOR_FIELDS = frozenset({"label", "value"})


class EventAnalysisError(ValueError):
    """The per-event assessment violates its public evidence contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable JSON bytes used to derive content identities."""

    def reject_nonfinite(node: Any, path: str = "analysis") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise EventAnalysisError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise EventAnalysisError(f"{path} contains a non-string key")
                reject_nonfinite(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                reject_nonfinite(child, f"{path}[{index}]")

    reject_nonfinite(value)
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


def semantic_assessment_seed(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Return the event-relevant assessment, excluding edition-only receipts.

    ``analysis_id`` continues to bind the exact published JSON bytes.  This
    projection has a different purpose: it decides whether a newly observed
    edition deserves another immutable revision.  Whole-input hashes and
    source clocks are provenance for the mutable edition, while the cited
    claims, joins, dispositions, conclusions, and availability states are the
    semantic assessment.
    """

    if type(analysis) is not dict:
        raise EventAnalysisError("analysis fields differ (missing=[], extra=[])")
    projected = copy.deepcopy(analysis)
    projected.pop("analysis_id", None)
    projected.pop("generated_at", None)

    replacements: dict[str, str] = {}
    collector_ids = {
        row.get("signal_id")
        for row in projected.get("collector_context") or []
        if type(row) is dict and type(row.get("signal_id")) is str
    }
    evidence = projected.get("evidence")
    if isinstance(evidence, list):
        for index, row in enumerate(evidence):
            if type(row) is not dict:
                continue
            identifier = row.get("evidence_id")
            token = f"semantic-evidence-{index:04d}"
            if type(identifier) is str:
                replacements[identifier] = token
            row["evidence_id"] = token
            if row.get("kind") == "newsroom-collector":
                row.clear()
                row.update(
                    {
                        "evidence_id": token,
                        "kind": "newsroom-collector-edition",
                        "surface_id": analysis["evidence"][index].get("surface_id"),
                    }
                )

    if collector_ids:
        projected["collector_context"] = [
            {"signal_id": signal_id, "relation": "topic-surface-only"}
            for signal_id in sorted(collector_ids)
        ]
        projected["disposition"] = "collector-edition"
        rationale = projected.get("rationale")
        if isinstance(rationale, list) and rationale:
            projected["rationale"] = [
                rationale[0],
                "Declared topic-only collector state belongs to the mutable edition.",
            ]
        brief = projected.get("brief")
        if isinstance(brief, dict):
            declared_pipe = sorted(collector_ids & event_brief.PIPE_SIGNAL_IDS)
            brief["pipe_context"] = {"edition_only_declared_signal_ids": declared_pipe}

    for row in projected.get("peer_context") or []:
        if type(row) is dict and row.get("status") != "live":
            row["as_of"] = None
            row["sentence"] = (
                f"{row.get('peer')} has no live event-matched peer record."
            )

    # The wire is a rolling window. Its raw same-window event count can rise or
    # fall when otherwise unchanged events enter or leave that edition, even
    # though the retained peer sources, independence groups, and shared topics
    # express the same assessment. Normalize the count and its two rendered
    # copies here; identity-set changes remain revision-forming below.
    window_peers = projected.get("window_peers")
    if type(window_peers) is dict:
        peer_count = window_peers.get("same_window_peer_count")
        if type(peer_count) is int and peer_count >= 0:
            for row in projected.get("key_numbers") or []:
                if (
                    type(row) is dict
                    and row.get("label") == "same-window events sharing a topic"
                    and row.get("value") == str(peer_count)
                ):
                    row["value"] = "edition-count"
            peer_clause = (
                f"{peer_count} same-window "
                f"event{'s' if peer_count != 1 else ''} share a declared topic."
            )
            position = projected.get("position")
            if type(position) is str and peer_clause in position:
                projected["position"] = position.replace(
                    peer_clause,
                    "Same-window event count belongs to the mutable edition.",
                    1,
                )
            window_peers["same_window_peer_count"] = "edition-count"

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            normalized = {}
            for key, child in value.items():
                if key in {"input_sha256", "source_timestamp"}:
                    normalized[key] = None
                elif key == "refresh_status":
                    normalized[key] = "edition-observation"
                else:
                    normalized[key] = normalize(child)
            return normalized
        if isinstance(value, list):
            return [normalize(child) for child in value]
        if isinstance(value, str):
            return replacements.get(value, value)
        return value

    return normalize(projected)


def semantically_equivalent(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return whether two valid editions express the same event assessment."""

    validate_event_analysis(left)
    validate_event_analysis(right)
    return semantic_assessment_seed(left) == semantic_assessment_seed(right)


def _exact(value: Any, fields: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        missing = (
            sorted(fields - set(value))
            if isinstance(value, Mapping)
            else sorted(fields)
        )
        extra = sorted(set(value) - fields) if isinstance(value, Mapping) else []
        raise EventAnalysisError(
            f"{path} fields differ (missing={missing}, extra={extra})"
        )
    return value


def _text(value: Any, path: str, *, maximum: int = 4_000) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise EventAnalysisError(f"{path} must be non-empty bounded text")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise EventAnalysisError(f"{path} contains unsafe Unicode")
    return value


def _nullable_text(value: Any, path: str, *, maximum: int = 200) -> str | None:
    if value is None:
        return None
    return _text(value, path, maximum=maximum)


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        raise EventAnalysisError(f"{path} is not a UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise EventAnalysisError(f"{path} is not a real timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise EventAnalysisError(f"{path} is not a canonical timestamp")
    return value


def _https_url(value: Any, path: str, *, palimpsest_only: bool = False) -> str:
    _text(value, path, maximum=2_048)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise EventAnalysisError(f"{path} is not a safe HTTPS URL")
    if palimpsest_only and parsed.hostname != "palimpsest.info":
        raise EventAnalysisError(f"{path} is not a Palimpsest URL")
    return value


def _finite_number(value: Any, path: str) -> int | float | None:
    if value is None:
        return None
    if type(value) not in {int, float} or not math.isfinite(value):
        raise EventAnalysisError(f"{path} must be a finite number or null")
    return value


def _event_scope_status(
    event: Mapping[str, Any], items: Mapping[str, Mapping[str, Any]]
) -> str:
    """Classify remit using the same reviewed sources and terms as intake.

    A declared collector link is itself proof that intake's China gate passed.
    Intrinsically scoped feeds remain in scope even when a short headline omits a
    place name. Global feeds need either that declaration or an explicit term.
    """

    linked = (
        event["declared_links"]["scan_signal_ids"]
        or event["declared_links"]["economic_signal_ids"]
    )
    if linked or any(
        reference["source_id"] in newswire_model._CHINA_SCOPED_SOURCE_IDS
        for reference in event["evidence_refs"]
    ):
        return "in-scope"
    text_parts = [event["headline"], event["dek"]]
    for reference in event["evidence_refs"]:
        item = items.get(reference["item_id"])
        if item is None:
            raise EventAnalysisError(
                f"event {event['event_id']} references an unknown newswire item"
            )
        text_parts.extend((item["title"], item["excerpt"]))
    haystack = " ".join(text_parts).casefold()
    return (
        "in-scope"
        if any(
            newswire_model._keyword_present(haystack, term)
            for term in newswire_model._CHINA_TERMS
        )
        else "outside-remit"
    )


def structural_quorum(event: Mapping[str, Any]) -> bool:
    """True only when claim_support.has_quorum sees ≥2 independence groups."""

    refs = event.get("evidence_refs")
    if not isinstance(refs, list):
        return False
    return has_quorum(
        refs,
        lambda item: (
            item.get("independence_group") if isinstance(item, Mapping) else None
        ),
        minimum=2,
    )


def _evidence_conclusion(independent_groups: int, *, quorum: bool) -> str:
    if quorum:
        return (
            f"The dossier contains {independent_groups} independent source groups. "
            "That is structural corroboration at the retained metadata level, not "
            "a truth score or verification of every underlying claim."
        )
    if independent_groups > 1:
        return (
            f"The dossier contains {independent_groups} independent source groups, "
            "but claim_support.has_quorum is not met (2 independent groups required). "
            "Palimpsest treats it as an attributed report, not independent corroboration."
        )
    return (
        "The dossier contains one independent source group. Palimpsest treats it "
        "as an attributed report, not independent corroboration."
    )


def _copy_metric(metric: Mapping[str, Any]) -> dict[str, Any]:
    denominator = metric["denominator"]
    return {
        "label": metric["label"],
        "value": metric["value"],
        "unit": metric["unit"],
        "denominator": {
            "label": denominator["label"],
            "value": denominator["value"],
        },
    }


def _collector_row(story: Mapping[str, Any]) -> dict[str, Any]:
    status = story["status"]
    if status == "live":
        interpretation = (
            "Current aggregate collector reading. It is relevant context only; "
            "the wire declares no timed, claim-specific verification join."
        )
    else:
        interpretation = (
            f"Collector status is {status}; retained values are not treated as a "
            "current finding."
        )
    claims = story["claims"]
    if type(claims) is not list or len(claims) != 1:
        raise EventAnalysisError(
            f"collector story {story.get('signal_id')!r} must contain one normalized claim"
        )
    return {
        "signal_id": story["signal_id"],
        "status": status,
        "headline": story["headline"],
        "finding": claims[0]["statement"],
        "metric": _copy_metric(story["metric"]),
        "source_timestamp": story["evidence"]["source_timestamp"],
        "story_url": story["url"],
        "evidence_url": story["evidence"]["url"],
        "input_sha256": story["evidence"]["input"]["sha256"],
        "claim_fingerprint": story["claim_fingerprint"],
        "method_summary": story["method"]["summary"],
        "method_version": story["method"]["version"],
        "relation": "topic-surface-only",
        "interpretation": interpretation,
    }


def _named_declared_receipts(
    event: Mapping[str, Any],
    collector_context: Sequence[Mapping[str, Any]],
    live_surfaces: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Name wayback/ddti/ledger metrics only when already declared or receipted."""

    declared = set(event["declared_links"].get("scan_signal_ids") or [])
    declared.update(event["declared_links"].get("economic_signal_ids") or [])
    declared.update(row["signal_id"] for row in collector_context)
    names: list[str] = []
    for surface in ("wayback", "ddti"):
        if surface in declared:
            names.append(surface)
    for row in live_surfaces:
        if (
            row.get("surface_id") == "public-deletion-ledgers"
            and row.get("status") == "live"
        ):
            names.append("public-deletion-ledgers")
    return list(dict.fromkeys(names))


def _compose_position(
    *,
    disposition: str,
    quorum: bool,
    archive_state: str | None,
    archive_matched: bool | None,
    peer_count: int,
    official_page: str,
    named_receipts: Sequence[str],
    interconnection_clause: str | None = None,
) -> str:
    """Return Palimpsest's public editorial position for one event.

    ``disposition`` captures the evidence boundary already established by the
    pipeline.  Wording may be assertive about that boundary, but must not claim
    that topic-linked collectors verified, refuted, or caused the article event.
    """

    if disposition == "outside-remit":
        return (
            "Palimpsest's view: this item falls outside the declared China "
            "evidence remit and should not be read as a Palimpsest finding."
        )
    if disposition == "collector-context":
        head = (
            "Palimpsest's view: current collectors add relevant measured "
            "context, but they do not independently verify or refute this article."
        )
    elif disposition == "collector-abstention":
        head = (
            "Palimpsest withholds a collector-backed conclusion because one or "
            "more declared measurement surfaces are not current."
        )
    elif quorum:
        head = (
            "Palimpsest's view: the reporting is structurally corroborated by "
            "independent source groups, but the underlying claims remain bounded "
            "by the published source material."
        )
    else:
        head = (
            "Palimpsest's view: this is a single attributed report, not an "
            "independently established fact."
        )
    if archive_state == "warming_up":
        archive_clause = "Archive-news-context anomaly_state is warming_up; no anomaly score is published."
    elif archive_state:
        archive_clause = f"Archive-news-context anomaly_state is {archive_state}."
    elif archive_matched is False:
        archive_clause = "No archive-news-context row matches this event_id."
    else:
        archive_clause = "Archive-news-context is absent."
    peer_clause = (
        f"{peer_count} same-window event{'s' if peer_count != 1 else ''} "
        "share a declared topic."
    )
    official_clause = f"Official-page corroboration coverage is {official_page}."
    parts = [head, archive_clause, peer_clause, official_clause]
    if named_receipts:
        parts.append("Declared receipts: " + ", ".join(named_receipts) + ".")
    if interconnection_clause:
        parts.append(interconnection_clause)
    return " ".join(parts)


def _published_at(event: Mapping[str, Any]) -> datetime | None:
    raw = event.get("published_at")
    if type(raw) is not str:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def window_peers_for(
    event: Mapping[str, Any], wire_events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Count events that share a topic inside the interconnection ±24h window."""

    topics = {
        topic for topic in (event.get("topics") or []) if type(topic) is str and topic
    }
    shared_topics: list[str] = []
    peer_source_ids: list[str] = []
    peer_groups: list[str] = []
    count = 0
    anchor = _published_at(event)
    radius = timedelta(hours=event_interconnection.WINDOW_HOURS)
    for other in wire_events:
        if type(other) is not dict or other.get("event_id") == event.get("event_id"):
            continue
        other_clock = _published_at(other)
        if anchor is None or other_clock is None or abs(other_clock - anchor) > radius:
            continue
        other_topics = {
            topic
            for topic in (other.get("topics") or [])
            if type(topic) is str and topic
        }
        overlap = sorted(topics & other_topics)
        if not overlap:
            continue
        count += 1
        shared_topics.extend(overlap)
        for ref in other.get("evidence_refs") or []:
            if (
                type(ref) is dict
                and type(ref.get("source_id")) is str
                and ref["source_id"]
            ):
                peer_source_ids.append(ref["source_id"])
        for group in other.get("evidence_groups") or []:
            if (
                type(group) is dict
                and type(group.get("group_id")) is str
                and group["group_id"]
            ):
                peer_groups.append(group["group_id"])
    return {
        "same_window_peer_count": count,
        "shared_topics": sorted(set(shared_topics))[:32],
        "peer_source_ids": sorted(set(peer_source_ids))[:64],
        "peer_independence_groups": sorted(set(peer_groups))[:64],
        "relation": "topic-surface-only",
    }


def _missing_collector_row(signal_id: str) -> dict[str, Any]:
    digest = hashlib.sha256(f"missing:{signal_id}".encode("utf-8")).hexdigest()
    return {
        "signal_id": signal_id,
        "status": "missing",
        "headline": f"{signal_id}: newsroom feed absent",
        "finding": (
            "No current newsroom collector story was supplied for this declared signal."
        ),
        "metric": {
            "label": None,
            "value": None,
            "unit": None,
            "denominator": {"label": None, "value": None},
        },
        "source_timestamp": None,
        "story_url": f"https://palimpsest.info/news/{signal_id}/",
        "evidence_url": f"https://palimpsest.info/readings/{signal_id}-latest.json",
        "input_sha256": None,
        "claim_fingerprint": f"sha256:{digest}",
        "method_summary": "Declared collector link without a live newsroom story.",
        "method_version": 1,
        "relation": "topic-surface-only",
        "interpretation": (
            "Collector status is missing; retained values are not treated as a "
            "current finding."
        ),
    }


def build_event_analysis(
    event: Mapping[str, Any],
    *,
    wire: Mapping[str, Any],
    feed: Mapping[str, Any],
    live_families: Mapping[str, Mapping[str, Any] | None] | None = None,
    archive_context: Mapping[str, Any] | None = None,
    corroboration: Mapping[str, Any] | None = None,
    peer_warehouses: Mapping[str, Mapping[str, Any] | None] | None = None,
    peer: Mapping[str, Any] | None = None,
    allow_missing_collectors: bool = False,
    archive_refresh_status: str = "unknown",
) -> dict[str, Any]:
    """Build one content-addressed assessment without network or filesystem I/O."""

    items = {item["item_id"]: item for item in wire["items"]}
    stories = {
        story["signal_id"]: story
        for story in feed.get("stories") or []
        if isinstance(story, Mapping) and type(story.get("signal_id")) is str
    }
    scope_status = _event_scope_status(event, items)
    linked_ids = sorted(
        set(event["declared_links"]["scan_signal_ids"])
        | set(event["declared_links"]["economic_signal_ids"])
    )
    unknown = sorted(set(linked_ids) - set(stories))
    if unknown and not allow_missing_collectors:
        raise EventAnalysisError(
            f"event {event['event_id']} declares unknown collector signals: {unknown}"
        )
    collector_context = [
        _collector_row(stories[signal_id])
        if signal_id in stories
        else _missing_collector_row(signal_id)
        for signal_id in linked_ids
    ]
    collector_statuses = [row["status"] for row in collector_context]
    independent_groups = len(event["evidence_groups"])
    quorum = structural_quorum(event)
    conclusion = _evidence_conclusion(independent_groups, quorum=quorum)
    peer_rows = (
        []
        if scope_status == "outside-remit"
        else peer_context_model.peer_context_for_event(event, peer, wire=wire)
    )

    if scope_status == "outside-remit":
        disposition = "outside-remit"
        collector_context = []
        collector_statuses = []
        peer_rows = []
        rationale = [
            conclusion,
            (
                "Neither an intrinsically China-scoped source, an explicit China "
                "term, nor an intake-approved collector link places this item inside "
                "the declared remit."
            ),
        ]
    elif not collector_context:
        disposition = "source-assessment"
        rationale = [
            conclusion,
            (
                "No Palimpsest collector surface is declared for this event, so the "
                "assessment stops at source structure and attribution."
            ),
        ]
    elif all(status == "live" for status in collector_statuses):
        disposition = "collector-context"
        rationale = [
            conclusion,
            (
                f"All {len(collector_context)} declared collector surfaces are live. "
                "Their normalized findings are published below as topical context, "
                "not article-specific confirmation."
            ),
        ]
    else:
        disposition = "collector-abstention"
        nonlive = sum(status != "live" for status in collector_statuses)
        rationale = [
            conclusion,
            (
                f"{nonlive} of {len(collector_context)} declared collector surfaces "
                "are not live, so Palimpsest does not issue a collector-backed view."
            ),
        ]

    limitations = [
        (
            "Palimpsest retained feed title, canonical link, publication time, and a "
            "bounded excerpt; it did not fetch or semantically evaluate the article body."
        ),
        (
            "Independent-source counts describe publication structure, not the truth, "
            "completeness, or intent of the underlying claims."
        ),
    ]
    if collector_context:
        limitations.extend(
            [
                (
                    "Collector links are predeclared topical surfaces, not a timed or "
                    "claim-specific match to this event."
                ),
                (
                    "A current collector value may post-date the article and cannot by "
                    "itself establish cause, coordination, impact, verification, or refutation."
                ),
            ]
        )
    else:
        limitations.append(
            "No current Palimpsest measurement is used in this assessment."
        )
    limitations.extend(
        event_brief.extra_limitations(
            has_surfaces=scope_status == "in-scope",
            has_archive=type(archive_context) is dict,
        )
    )
    if peer_rows:
        limitations.append(
            "Peer rows name GreatFire, OONI, CDT, or Weiboscope and the date of "
            "that peer's verdict. They are not Palimpsest capture and do not "
            "share Palimpsest's denominator."
        )

    generated_candidates = [event["updated_at"]]
    generated_candidates.extend(
        stories[row["signal_id"]]["modified_at"]
        for row in collector_context
        if row["signal_id"] in stories
    )
    window_peers = window_peers_for(event, wire.get("events") or [])
    v2 = event_brief.build_v2_blocks(
        event,
        items=items,
        collector_context=collector_context,
        scope_status=scope_status,
        live_families=live_families,
        archive_context=archive_context,
        corroboration=corroboration,
        window_peers=window_peers,
        peer_warehouses=peer_warehouses,
        archive_refresh_status=archive_refresh_status,
    )
    generated_candidates.extend(
        clock
        for clock in v2.pop("extra_clocks")
        if type(clock) is str and _TIMESTAMP_RE.fullmatch(clock)
    )
    method = v2.pop("method")
    named_receipts = _named_declared_receipts(
        event, collector_context, v2.get("surface_context") or []
    )
    archive_block = v2.get("archive_news_context") or {}
    corroboration_block = v2.get("corroboration") or {}
    generated_candidates.extend(
        row["as_of"]
        for row in peer_rows
        if row.get("status") == "live"
        and type(row.get("as_of")) is str
        and _TIMESTAMP_RE.fullmatch(row["as_of"])
    )
    core = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event["event_id"],
        "event_version_id": event["version_id"],
        "event_url": event["url"],
        "url": f"{event['url']}analysis.json",
        "generated_at": max(generated_candidates),
        "disposition": disposition,
        "scope_status": scope_status,
        "position": _compose_position(
            disposition=disposition,
            quorum=quorum,
            archive_state=archive_block.get("anomaly_state"),
            archive_matched=archive_block.get("matched"),
            peer_count=window_peers["same_window_peer_count"],
            official_page=corroboration_block.get("official_page") or "none-reviewed",
            named_receipts=named_receipts,
            interconnection_clause=event_interconnection.interconnection_position_clause(
                v2.get("interconnection") or {}
            ),
        ),
        "rationale": rationale,
        "evidence_assessment": {
            "strength": event["evidence_strength"],
            "independent_groups": independent_groups,
            "source_count": len(event["evidence_refs"]),
            "conclusion": conclusion,
        },
        "collector_context": collector_context,
        "peer_context": peer_rows,
        "limitations": limitations,
        "method": method,
        **v2,
    }
    analysis = {
        **core,
        "analysis_id": "analysisv-"
        + hashlib.sha256(canonical_json_bytes(core)).hexdigest()[:24],
    }
    # Put the identifier near the contract/version fields in serialized output.
    analysis = {
        "schema_version": analysis["schema_version"],
        "analysis_id": analysis["analysis_id"],
        **{
            key: value
            for key, value in analysis.items()
            if key not in {"schema_version", "analysis_id"}
        },
    }
    validate_event_analysis(analysis, event=event)
    return analysis


def build_event_analyses(
    wire: Mapping[str, Any],
    feed: Mapping[str, Any] | None,
    *,
    live_families: Mapping[str, Mapping[str, Any] | None] | None = None,
    archive_context: Mapping[str, Any] | None = None,
    corroboration: Mapping[str, Any] | None = None,
    peer_warehouses: Mapping[str, Mapping[str, Any] | None] | None = None,
    peer: Mapping[str, Any] | None = None,
    allow_missing_collectors: bool = False,
    archive_refresh_status: str = "unknown",
) -> dict[str, dict[str, Any]]:
    """Return exactly one validated assessment for every validated wire event."""

    newswire_model.validate_newswire_document(wire)
    if feed is None:
        if not allow_missing_collectors:
            raise EventAnalysisError("collector feed is not palimpsest-news.v1")
        feed = {"schema_version": "palimpsest-news.v1", "stories": []}
    if type(feed) is not dict or feed.get("schema_version") != "palimpsest-news.v1":
        raise EventAnalysisError("collector feed is not palimpsest-news.v1")
    if type(feed.get("stories")) is not list:
        raise EventAnalysisError("collector feed stories are missing")
    signal_ids = [
        story.get("signal_id")
        for story in feed["stories"]
        if isinstance(story, Mapping)
    ]
    if len(signal_ids) != len(feed["stories"]) or len(signal_ids) != len(
        set(signal_ids)
    ):
        raise EventAnalysisError("collector feed signal IDs are invalid or duplicated")
    analyses = {
        event["event_id"]: build_event_analysis(
            event,
            wire=wire,
            feed=feed,
            live_families=live_families,
            archive_context=archive_context,
            corroboration=corroboration,
            peer_warehouses=peer_warehouses,
            peer=peer,
            allow_missing_collectors=allow_missing_collectors,
            archive_refresh_status=archive_refresh_status,
        )
        for event in wire["events"]
    }
    if set(analyses) != {event["event_id"] for event in wire["events"]}:
        raise EventAnalysisError("analysis output does not account for every event")
    return analyses


def _validate_metric(value: Any, path: str) -> None:
    metric = _exact(value, _METRIC_FIELDS, path)
    label = _nullable_text(metric["label"], f"{path}.label", maximum=100)
    unit = _nullable_text(metric["unit"], f"{path}.unit", maximum=64)
    number = _finite_number(metric["value"], f"{path}.value")
    denominator = _exact(
        metric["denominator"], _DENOMINATOR_FIELDS, f"{path}.denominator"
    )
    denominator_label = _nullable_text(
        denominator["label"], f"{path}.denominator.label", maximum=100
    )
    denominator_value = _finite_number(
        denominator["value"], f"{path}.denominator.value"
    )
    if number is None and any(
        item is not None for item in (label, unit, denominator_label, denominator_value)
    ):
        raise EventAnalysisError(f"{path} has labels or denominator without a value")


_FORBIDDEN_PEER_CLAIMS = (
    "proves the party",
    "greatfire proves",
    "ooni proves",
    "palimpsest measured",
    "our denominator",
)


def _validate_peer(value: Any, path: str) -> None:
    row = _exact(value, peer_context_model.PEER_FIELDS, path)
    if row["peer"] not in peer_context_model.PEERS:
        raise EventAnalysisError(f"{path}.peer is invalid")
    if row["status"] not in peer_context_model.STATUSES:
        raise EventAnalysisError(f"{path}.status is invalid")
    sentence = _text(row["sentence"], f"{path}.sentence", maximum=600)
    lowered = sentence.casefold()
    if any(token in lowered for token in _FORBIDDEN_PEER_CLAIMS):
        raise EventAnalysisError(
            f"{path}.sentence collapses a peer verdict into Palimpsest capture"
        )
    if row["peer"] == "greatfire" and "greatfire" not in lowered:
        raise EventAnalysisError(f"{path}.sentence must name GreatFire")
    if row["peer"] == "ooni" and "ooni" not in lowered:
        raise EventAnalysisError(f"{path}.sentence must name OONI")
    if row["peer"] == "cdt" and "cdt" not in lowered:
        raise EventAnalysisError(f"{path}.sentence must name CDT")
    if row["peer"] == "weiboscope" and "weiboscope" not in lowered:
        raise EventAnalysisError(f"{path}.sentence must name Weiboscope")
    if row["peer"] == "cdt" and "palimpsest did not write" not in lowered:
        raise EventAnalysisError(f"{path}.sentence must disclaim Palimpsest authorship")
    if row["as_of"] is not None:
        _timestamp(row["as_of"], f"{path}.as_of")
    if row["peer_url"] is not None:
        _https_url(row["peer_url"], f"{path}.peer_url")
    _nullable_text(row["title"], f"{path}.title", maximum=240)
    excerpt = row["excerpt"]
    if excerpt is not None:
        _text(excerpt, f"{path}.excerpt", maximum=peer_context_model.CDT_EXCERPT_LIMIT)
    _nullable_text(row["host"], f"{path}.host", maximum=253)
    if row["measurement_count"] is not None and (
        type(row["measurement_count"]) is not int or row["measurement_count"] < 0
    ):
        raise EventAnalysisError(f"{path}.measurement_count is invalid")
    _finite_number(row["anomaly_rate"], f"{path}.anomaly_rate")
    _nullable_text(row["verdict"], f"{path}.verdict", maximum=64)
    if row["window_days"] is not None and (
        type(row["window_days"]) is not int or not 1 <= row["window_days"] <= 366
    ):
        raise EventAnalysisError(f"{path}.window_days is invalid")
    _text(row["attribution"], f"{path}.attribution", maximum=400)
    if row["relation"] != peer_context_model.RELATION:
        raise EventAnalysisError(f"{path}.relation may not imply Palimpsest capture")


def _validate_collector(value: Any, path: str) -> None:
    row = _exact(value, _COLLECTOR_FIELDS, path)
    if (
        type(row["signal_id"]) is not str
        or _IDENTIFIER_RE.fullmatch(row["signal_id"]) is None
    ):
        raise EventAnalysisError(f"{path}.signal_id is invalid")
    if row["status"] not in _COLLECTOR_STATUSES:
        raise EventAnalysisError(f"{path}.status is invalid")
    for field, maximum in (
        ("headline", 300),
        ("finding", 2_000),
        ("method_summary", 8_000),
        ("interpretation", 1_000),
    ):
        _text(row[field], f"{path}.{field}", maximum=maximum)
    _validate_metric(row["metric"], f"{path}.metric")
    _https_url(row["story_url"], f"{path}.story_url", palimpsest_only=True)
    _https_url(row["evidence_url"], f"{path}.evidence_url", palimpsest_only=True)
    if row["relation"] != "topic-surface-only":
        raise EventAnalysisError(f"{path}.relation may not imply verification")
    if (
        type(row["claim_fingerprint"]) is not str
        or _CLAIM_FINGERPRINT_RE.fullmatch(row["claim_fingerprint"]) is None
    ):
        raise EventAnalysisError(f"{path}.claim_fingerprint is invalid")
    method_version = row["method_version"]
    if not (
        method_version is None
        or (type(method_version) is int and method_version > 0)
        or (
            type(method_version) is str
            and method_version
            and len(method_version) <= 100
        )
    ):
        raise EventAnalysisError(f"{path}.method_version is invalid")
    if row["status"] == "missing":
        if row["source_timestamp"] is not None or row["input_sha256"] is not None:
            raise EventAnalysisError(f"{path} missing collector claims evidence")
    else:
        _timestamp(row["source_timestamp"], f"{path}.source_timestamp")
        if (
            type(row["input_sha256"]) is not str
            or _SHA256_RE.fullmatch(row["input_sha256"]) is None
        ):
            raise EventAnalysisError(f"{path}.input_sha256 is invalid")
    if row["status"] != "live" and row["metric"]["value"] is not None:
        raise EventAnalysisError(f"{path} republishes a non-live metric")


def validate_event_analysis(
    analysis: Any, *, event: Mapping[str, Any] | None = None
) -> None:
    """Fail closed on unknown fields, unsafe text, or editorial-state mismatch."""

    if type(analysis) is not dict:
        raise EventAnalysisError("analysis fields differ (missing=[], extra=[])")
    version = analysis.get("schema_version")
    if version == SCHEMA_VERSION_V1:
        v1_fields = (
            _TOP_FIELDS_V1_WITH_PEERS
            if set(analysis) == _TOP_FIELDS_V1_WITH_PEERS
            else _TOP_FIELDS_V1
        )
        document = _exact(analysis, v1_fields, "analysis")
    elif version == SCHEMA_VERSION:
        document = _exact(analysis, _TOP_FIELDS, "analysis")
    else:
        raise EventAnalysisError("analysis.schema_version is unsupported")
    if document["schema_version"] not in {SCHEMA_VERSION_V1, SCHEMA_VERSION}:
        raise EventAnalysisError("analysis.schema_version is unsupported")
    if (
        type(document["analysis_id"]) is not str
        or _ANALYSIS_ID_RE.fullmatch(document["analysis_id"]) is None
    ):
        raise EventAnalysisError("analysis.analysis_id is invalid")
    if (
        type(document["event_id"]) is not str
        or _EVENT_ID_RE.fullmatch(document["event_id"]) is None
    ):
        raise EventAnalysisError("analysis.event_id is invalid")
    if (
        type(document["event_version_id"]) is not str
        or _EVENT_VERSION_ID_RE.fullmatch(document["event_version_id"]) is None
    ):
        raise EventAnalysisError("analysis.event_version_id is invalid")
    expected_event_url = f"https://palimpsest.info/news/wire/{document['event_id']}/"
    if document["event_url"] != expected_event_url:
        raise EventAnalysisError("analysis.event_url is not canonical")
    if document["url"] != f"{expected_event_url}analysis.json":
        raise EventAnalysisError("analysis.url is not canonical")
    _timestamp(document["generated_at"], "analysis.generated_at")
    if document["disposition"] not in _DISPOSITIONS:
        raise EventAnalysisError("analysis.disposition is invalid")
    if document["scope_status"] not in _SCOPE_STATUSES:
        raise EventAnalysisError("analysis.scope_status is invalid")
    _text(document["position"], "analysis.position", maximum=1_000)
    if (
        type(document["rationale"]) is not list
        or not 1 <= len(document["rationale"]) <= 6
    ):
        raise EventAnalysisError("analysis.rationale must contain 1 to 6 statements")
    for index, statement in enumerate(document["rationale"]):
        _text(statement, f"analysis.rationale[{index}]", maximum=2_000)
    evidence = _exact(
        document["evidence_assessment"],
        _EVIDENCE_FIELDS,
        "analysis.evidence_assessment",
    )
    if evidence["strength"] not in {
        "measurement-corroborated",
        "primary-corroborated",
        "multi-source",
        "single-measurement-source",
        "single-primary-source",
        "single-source",
    }:
        raise EventAnalysisError("analysis.evidence_assessment.strength is invalid")
    for field in ("independent_groups", "source_count"):
        if type(evidence[field]) is not int or evidence[field] < 1:
            raise EventAnalysisError(f"analysis.evidence_assessment.{field} is invalid")
    quorum = (
        structural_quorum(event)
        if event is not None
        else evidence["independent_groups"] > 1
    )
    if evidence["conclusion"] != _evidence_conclusion(
        evidence["independent_groups"], quorum=quorum
    ):
        raise EventAnalysisError("analysis evidence conclusion is not reproducible")

    context = document["collector_context"]
    if type(context) is not list or len(context) > 32:
        raise EventAnalysisError("analysis.collector_context is invalid")
    for index, row in enumerate(context):
        _validate_collector(row, f"analysis.collector_context[{index}]")
    peers = document.get("peer_context")
    if peers is None:
        peers = []
    elif type(peers) is not list or len(peers) > peer_context_model.MAX_PEERS_PER_EVENT:
        raise EventAnalysisError("analysis.peer_context is invalid")
    else:
        for index, row in enumerate(peers):
            _validate_peer(row, f"analysis.peer_context[{index}]")
    if document["scope_status"] == "outside-remit" and peers:
        raise EventAnalysisError("outside-remit analysis may not imply peer support")
    signal_ids = [row["signal_id"] for row in context]
    if signal_ids != sorted(set(signal_ids)):
        raise EventAnalysisError("analysis.collector_context is not unique and sorted")
    statuses = [row["status"] for row in context]
    expected_disposition = (
        "outside-remit"
        if document["scope_status"] == "outside-remit"
        else "source-assessment"
        if not context
        else "collector-context"
        if all(status == "live" for status in statuses)
        else "collector-abstention"
    )
    if document["disposition"] != expected_disposition:
        raise EventAnalysisError("analysis disposition does not match evidence state")
    if document["scope_status"] == "outside-remit" and context:
        raise EventAnalysisError(
            "outside-remit analysis may not imply collector support"
        )

    if (
        type(document["limitations"]) is not list
        or not 1 <= len(document["limitations"]) <= 10
    ):
        raise EventAnalysisError("analysis.limitations must contain 1 to 10 statements")
    if len(document["limitations"]) != len(set(document["limitations"])):
        raise EventAnalysisError("analysis.limitations contains duplicates")
    for index, limitation in enumerate(document["limitations"]):
        _text(limitation, f"analysis.limitations[{index}]", maximum=2_000)
    if document["schema_version"] == SCHEMA_VERSION_V1:
        expected_method = (
            METHOD_V1_WITH_PEERS if "peer_context" in document else METHOD_V1
        )
        if document["method"] != expected_method:
            raise EventAnalysisError("analysis.method does not match the v1 method")
    else:
        event_brief.validate_v2_blocks(document, event=event)

    if event is not None:
        if (
            document["event_id"] != event["event_id"]
            or document["event_version_id"] != event["version_id"]
            or document["event_url"] != event["url"]
            or evidence["strength"] != event["evidence_strength"]
            or evidence["independent_groups"] != len(event["evidence_groups"])
            or evidence["source_count"] != len(event["evidence_refs"])
        ):
            raise EventAnalysisError("analysis does not match its event receipt")
    seed = {key: value for key, value in document.items() if key != "analysis_id"}
    expected_id = (
        "analysisv-" + hashlib.sha256(canonical_json_bytes(seed)).hexdigest()[:24]
    )
    if document["analysis_id"] != expected_id:
        raise EventAnalysisError("analysis.analysis_id does not match its content")


__all__ = [
    "EventAnalysisError",
    "METHOD",
    "METHOD_V1",
    "METHOD_V1_WITH_PEERS",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_V1",
    "build_event_analysis",
    "build_event_analyses",
    "canonical_json_bytes",
    "semantic_assessment_seed",
    "semantically_equivalent",
    "load_optional_archive_context",
    "load_optional_corroboration",
    "load_optional_live_families",
    "load_optional_peer_warehouses",
    "structural_quorum",
    "validate_event_analysis",
    "window_peers_for",
]
