"""Article-by-article China stream projected from the evidence wire.

The evidence wire is event-centric: several publisher entries can share one
event dossier and one analysis.  This module provides the complementary RSS
shape requested by readers.  Every in-scope publisher item remains visible,
but analysis stays bound to the event evidence graph so repeated coverage does
not manufacture additional corroboration.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from typing import Any, Mapping

from core import event_analysis as event_analysis_model
from core import newswire as newswire_model


SCHEMA_VERSION = "palimpsest-china-article-stream.v1"
SITE = "https://palimpsest.info"
MAX_ENTRIES = 8_192
MAX_COUNT = 1_000_000_000
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ITEM_ID_RE = re.compile(r"^item-[0-9a-f]{24}$")
_ITEM_VERSION_RE = re.compile(r"^itemv-[0-9a-f]{24}$")
_EVENT_ID_RE = re.compile(r"^event-[0-9a-f]{24}$")
_ANALYSIS_ID_RE = re.compile(r"^analysisv-[0-9a-f]{24}$")


class ChinaArticleStreamError(ValueError):
    """The China article projection violates its closed public contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable bytes used by publication receipts and tests."""

    def reject_nonfinite(node: Any, path: str = "stream") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise ChinaArticleStreamError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise ChinaArticleStreamError(f"{path} contains a non-string key")
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


def _language(text: str, source_id: str) -> str:
    if any("\u3400" <= character <= "\u9fff" for character in text):
        return "zh-Hant" if any(character in text for character in "國臺灣網體門") else "zh-Hans"
    if source_id in {"rfa-mandarin", "bbc-chinese", "voa-chinese"}:
        return "zh"
    return "en"


def _next_checks(
    event: Mapping[str, Any], analysis: Mapping[str, Any]
) -> list[str]:
    """Produce evidence tasks, never speculative conclusions."""

    evidence = analysis["evidence_assessment"]
    roles = {reference["role"] for reference in event["evidence_refs"]}
    checks: list[str] = []
    if evidence["independent_groups"] < 2:
        checks.append(
            "Locate an independently produced account before treating the reported claim as corroborated."
        )
    if not roles.intersection({"primary", "measurement"}):
        checks.append(
            "Find the underlying primary record, dataset, filing, transcript, or technical measurement."
        )
    if not analysis["collector_context"]:
        desk_check = {
            "economy": "Compare the claim with a dated official release and an independently governed economic series.",
            "politics": "Check the relevant law, order, transcript, or institutional record and preserve its revision date.",
            "rights": "Seek a case-specific primary document and a second independently gathered account while protecting identities.",
            "security": "Preserve technical indicators privately and verify them against an independently obtained advisory or measurement.",
            "censorship": "Test the named surface from more than one network or archive before inferring blocking or deletion.",
            "connectivity": "Check routing, DNS, traffic, and reachability measurements across independent vantage points.",
            "technology": "Locate the product notice, repository change, filing, or benchmark that the report relies on.",
        }[event["desk"]]
        checks.append(desk_check)
    if event["mutation"]["kind"] == "updated":
        checks.append(
            "Compare this dossier revision with its prior version; an update can add evidence or merely alter metadata."
        )
    if len(event["evidence_refs"]) > 1 and evidence["independent_groups"] == 1:
        checks.append(
            "Compare same-group updates for corrections, changed wording, and chronology; repetition is not independence."
        )
    return checks[:4]


def _analysis_projection(
    event: Mapping[str, Any], analysis: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "analysis_id": analysis["analysis_id"],
        "url": analysis["url"],
        "disposition": analysis["disposition"],
        "scope_status": analysis["scope_status"],
        "position": analysis["position"],
        "rationale": list(analysis["rationale"]),
        "evidence_assessment": dict(analysis["evidence_assessment"]),
        "collector_context": [
            {
                "signal_id": row["signal_id"],
                "status": row["status"],
                "headline": row["headline"],
                "finding": row["finding"],
                "relation": row["relation"],
                "interpretation": row["interpretation"],
                "story_url": row["story_url"],
                "evidence_url": row["evidence_url"],
            }
            for row in analysis["collector_context"]
        ],
        "peer_context": [
            {
                "peer": row["peer"],
                "status": row["status"],
                "sentence": row["sentence"],
                "as_of": row["as_of"],
                "peer_url": row["peer_url"],
                "attribution": row["attribution"],
                "relation": row["relation"],
            }
            for row in analysis.get("peer_context") or []
        ],
        "known_unknowns": list(analysis["limitations"]),
        "next_checks": _next_checks(event, analysis),
        "method": analysis["method"],
    }


def _dossier_projection(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "version_id": event["version_id"],
        "url": event["url"],
        "evidence_strength": event["evidence_strength"],
        "independent_groups": len(event["evidence_groups"]),
        "source_items": len(event["evidence_refs"]),
        "mutation": dict(event["mutation"]),
    }


def _entry_projection(
    item: Mapping[str, Any],
    event: Mapping[str, Any],
    analysis: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "entry_id": item["item_id"],
        "version_id": item["version_id"],
        "published_at": item["published_at"],
        "collected_at": item["collected_at"],
        "headline": item["title"],
        "excerpt": item["excerpt"],
        "original_url": item["url"],
        "rights_policy": item["rights_policy"],
        "language": _language(
            item["title"] + " " + item["excerpt"], item["source_id"]
        ),
        "desk": item["desk"],
        "topics": list(item["topics"]),
        "publisher": {
            "source_id": item["source_id"],
            "name": item["source_name"],
            "role": item["role"],
            "independence_group": item["independence_group"],
            "feed_sha256": item["feed_sha256"],
        },
        "dossier": _dossier_projection(event),
        "analysis": _analysis_projection(event, analysis),
    }


def _validated_event_context(
    wire: Mapping[str, Any],
    analyses: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    newswire_model.validate_newswire_document(wire)
    events = {event["event_id"]: event for event in wire["events"]}
    analysis_ids = set(analyses)
    event_ids = set(events)
    if analysis_ids != event_ids:
        missing = sorted(event_ids - analysis_ids)
        extra = sorted(analysis_ids - event_ids)
        raise ChinaArticleStreamError(
            f"event analysis set differs (missing={missing}, extra={extra})"
        )

    by_item: dict[str, Mapping[str, Any]] = {}
    for event_id, event in events.items():
        event_analysis_model.validate_event_analysis(
            analyses[event_id], event=event
        )
        for reference in event["evidence_refs"]:
            item_id = reference["item_id"]
            if item_id in by_item:
                raise ChinaArticleStreamError(
                    f"item belongs to multiple events: {item_id}"
                )
            by_item[item_id] = event
    return by_item


def build_china_article_stream(
    wire: Mapping[str, Any],
    analyses: Mapping[str, Mapping[str, Any]],
    *,
    telegram_watch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a chronological, one-entry-per-item China feed projection."""

    by_item = _validated_event_context(wire, analyses)

    source_counts: Counter[str] = Counter()
    excluded_counts: Counter[str] = Counter()
    entries: list[dict[str, Any]] = []
    for item in wire["items"]:
        if not newswire_model.is_china_relevant_item(item):
            excluded_counts[item["source_id"]] += 1
            continue
        event = by_item.get(item["item_id"])
        if event is None:
            raise ChinaArticleStreamError(
                f"China item has no event dossier: {item['item_id']}"
            )
        analysis = analyses[event["event_id"]]
        source_counts[item["source_id"]] += 1
        entries.append(_entry_projection(item, event, analysis))
    entries.sort(
        key=lambda row: (row["published_at"], row["entry_id"]), reverse=True
    )
    if len(entries) > MAX_ENTRIES:
        raise ChinaArticleStreamError("China article stream exceeds its entry cap")

    telegram = (
        dict(telegram_watch)
        if telegram_watch is not None
        else {
            "status": "NO_REVIEWED_PUBLIC_CONTEXT",
            "relation": "separate-context-lane",
            "explanation": (
                "ScamShield and public-channel monitoring can inform this desk only "
                "after aggregate privacy and human-review gates pass. No Telegram "
                "observation is being used as article corroboration in this edition."
            ),
        }
    )
    generated_at = max(
        wire["generated_at"],
        str(telegram_watch.get("generated_at", wire["generated_at"]))
        if telegram_watch is not None else wire["generated_at"],
    )
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "url": f"{SITE}/news/china/",
        "feed_url": f"{SITE}/news/china/feed.xml",
        "json_feed_url": f"{SITE}/news/china/feed.json",
        "source_wire": f"{SITE}/readings/newswire-latest.json",
        "window": dict(wire["window"]),
        "scope": (
            "Every retained current-window item whose reviewed source or feed metadata "
            "places it in the China/Hong Kong remit. This is broad monitored-feed "
            "coverage, not a claim to index the entire web."
        ),
        "coverage": {
            "registered_sources": wire["coverage"]["registry_sources"],
            "successful_sources": wire["coverage"]["successful_sources"],
            "accepted_wire_items": wire["n_items"],
            "china_entries": len(entries),
            "excluded_global_feed_items": sum(excluded_counts.values()),
            "source_counts": [
                {"source_id": source_id, "entries": count}
                for source_id, count in sorted(source_counts.items())
            ],
        },
        "telegram_watch": telegram,
        "method": {
            "selection": (
                "Reviewed China-scoped feeds are included item-by-item; global feeds "
                "require an explicit China/Hong Kong term in retained title or excerpt metadata."
            ),
            "analysis": (
                "Each entry reuses its validated event analysis. Multiple entries from "
                "one independence group do not increase corroboration."
            ),
            "rights": (
                "Only feed title, canonical URL, publication time, and a bounded excerpt "
                "are retained; article bodies are not copied or fetched."
            ),
            "telegram": (
                "Reviewed Telegram aggregates occupy a separate context lane and can "
                "never establish an article claim, identity, prevalence, guilt, or causation."
            ),
        },
        "n_entries": len(entries),
        "entries": entries,
    }
    validate_china_article_stream(document, wire=wire, analyses=analyses)
    return document


def _safe_text(value: Any, path: str, *, maximum: int = 8_000) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ChinaArticleStreamError(f"{path} must be non-empty bounded text")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise ChinaArticleStreamError(f"{path} contains unsafe Unicode")
    return value


def validate_china_article_stream(
    document: Mapping[str, Any],
    *,
    wire: Mapping[str, Any] | None = None,
    analyses: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Validate the public boundary, optionally against authoritative inputs.

    Shape-only validation cannot authenticate a partial projection. Publication
    callers pass both ``wire`` and ``analyses`` so every entry is compared with
    the exact validated item, event dossier, and event-analysis projection.
    """

    if type(document) is not dict or document.get("schema_version") != SCHEMA_VERSION:
        raise ChinaArticleStreamError("invalid China article stream schema version")
    if not _TIMESTAMP_RE.fullmatch(str(document.get("generated_at", ""))):
        raise ChinaArticleStreamError("generated_at must be canonical UTC")
    entries = document.get("entries")
    if type(entries) is not list or len(entries) > MAX_ENTRIES:
        raise ChinaArticleStreamError("entries must be a bounded array")
    if document.get("n_entries") != len(entries):
        raise ChinaArticleStreamError("n_entries does not match entries")
    coverage = document.get("coverage")
    if type(coverage) is not dict:
        raise ChinaArticleStreamError("coverage must be an object")
    if coverage.get("china_entries") != len(entries):
        raise ChinaArticleStreamError("coverage.china_entries does not match entries")
    if (wire is None) != (analyses is None):
        raise ChinaArticleStreamError(
            "wire and analyses must be supplied together for source validation"
        )
    expected_entries: dict[str, dict[str, Any]] | None = None
    if wire is not None and analyses is not None:
        by_item = _validated_event_context(wire, analyses)
        expected_entries = {}
        expected_source_counts: Counter[str] = Counter()
        excluded_items = 0
        for item in wire["items"]:
            if not newswire_model.is_china_relevant_item(item):
                excluded_items += 1
                continue
            event = by_item.get(item["item_id"])
            if event is None:
                raise ChinaArticleStreamError(
                    f"China item has no event dossier: {item['item_id']}"
                )
            expected_entries[item["item_id"]] = _entry_projection(
                item, event, analyses[event["event_id"]]
            )
            expected_source_counts[item["source_id"]] += 1
        expected_coverage = {
            "registered_sources": wire["coverage"]["registry_sources"],
            "successful_sources": wire["coverage"]["successful_sources"],
            "accepted_wire_items": wire["n_items"],
            "china_entries": len(expected_entries),
            "excluded_global_feed_items": excluded_items,
            "source_counts": [
                {"source_id": source_id, "entries": count}
                for source_id, count in sorted(expected_source_counts.items())
            ],
        }
        if document.get("source_wire") != f"{SITE}/readings/newswire-latest.json":
            raise ChinaArticleStreamError("source_wire is not canonical")
        if document.get("window") != wire["window"]:
            raise ChinaArticleStreamError("window does not match the source wire")
        if coverage != expected_coverage:
            raise ChinaArticleStreamError("coverage does not match the source wire")
    identities: set[str] = set()
    ordering: list[tuple[str, str]] = []
    event_projections: dict[str, bytes] = {}
    event_entry_counts: Counter[str] = Counter()
    event_publisher_groups: dict[str, set[str]] = {}
    event_limits: dict[str, tuple[int, int]] = {}
    forbidden_keys = {"raw_text", "message_text", "channel", "username", "ioc", "iocs"}
    for index, entry in enumerate(entries):
        path = f"entries[{index}]"
        if type(entry) is not dict:
            raise ChinaArticleStreamError(f"{path} must be an object")
        entry_id = entry.get("entry_id")
        if type(entry_id) is not str or not _ITEM_ID_RE.fullmatch(entry_id):
            raise ChinaArticleStreamError(f"{path}.entry_id is invalid")
        if entry_id in identities:
            raise ChinaArticleStreamError(f"duplicate entry_id: {entry_id}")
        identities.add(entry_id)
        if not _ITEM_VERSION_RE.fullmatch(str(entry.get("version_id", ""))):
            raise ChinaArticleStreamError(f"{path}.version_id is invalid")
        if not _TIMESTAMP_RE.fullmatch(str(entry.get("published_at", ""))):
            raise ChinaArticleStreamError(f"{path}.published_at is invalid")
        _safe_text(entry.get("headline"), f"{path}.headline", maximum=240)
        if type(entry.get("excerpt")) is not str or len(entry["excerpt"]) > 320:
            raise ChinaArticleStreamError(f"{path}.excerpt is invalid")
        dossier = entry.get("dossier")
        if type(dossier) is not dict:
            raise ChinaArticleStreamError(f"{path}.dossier must be an object")
        event_id = dossier.get("event_id")
        if type(event_id) is not str or not _EVENT_ID_RE.fullmatch(event_id):
            raise ChinaArticleStreamError(f"{path}.dossier.event_id is invalid")
        if dossier.get("url") != f"{SITE}/news/wire/{event_id}/":
            raise ChinaArticleStreamError(f"{path}.dossier.url does not bind to event_id")
        independent_groups = dossier.get("independent_groups")
        source_items = dossier.get("source_items")
        if (
            type(independent_groups) is not int
            or independent_groups < 1
            or independent_groups > MAX_COUNT
        ):
            raise ChinaArticleStreamError(
                f"{path}.dossier.independent_groups is invalid"
            )
        if (
            type(source_items) is not int
            or source_items < 1
            or source_items > MAX_COUNT
        ):
            raise ChinaArticleStreamError(f"{path}.dossier.source_items is invalid")
        if independent_groups > source_items:
            raise ChinaArticleStreamError(
                f"{path}.dossier has more groups than source items"
            )

        analysis = entry.get("analysis")
        if type(analysis) is not dict:
            raise ChinaArticleStreamError(f"{path}.analysis must be an object")
        if not _ANALYSIS_ID_RE.fullmatch(str(analysis.get("analysis_id", ""))):
            raise ChinaArticleStreamError(f"{path}.analysis.analysis_id is invalid")
        if analysis.get("url") != f"{SITE}/news/wire/{event_id}/analysis.json":
            raise ChinaArticleStreamError(f"{path}.analysis.url does not bind to event_id")
        assessment = analysis.get("evidence_assessment")
        if type(assessment) is not dict:
            raise ChinaArticleStreamError(
                f"{path}.analysis.evidence_assessment must be an object"
            )
        expected_assessment = {
            "strength": dossier.get("evidence_strength"),
            "independent_groups": independent_groups,
            "source_count": source_items,
        }
        observed_assessment = {
            field: assessment.get(field) for field in expected_assessment
        }
        if observed_assessment != expected_assessment:
            raise ChinaArticleStreamError(
                f"{path}.analysis.evidence_assessment does not match dossier"
            )
        _safe_text(analysis.get("position"), f"{path}.analysis.position")
        if type(analysis.get("next_checks")) is not list or not analysis["next_checks"]:
            raise ChinaArticleStreamError(f"{path}.analysis.next_checks is empty")

        publisher = entry.get("publisher")
        if type(publisher) is not dict:
            raise ChinaArticleStreamError(f"{path}.publisher must be an object")
        publisher_group = _safe_text(
            publisher.get("independence_group"),
            f"{path}.publisher.independence_group",
            maximum=80,
        )
        projection = canonical_json_bytes(
            {"dossier": dossier, "analysis": analysis}
        )
        prior_projection = event_projections.setdefault(event_id, projection)
        if projection != prior_projection:
            raise ChinaArticleStreamError(
                f"{path} event dossier and analysis projection differs"
            )
        event_entry_counts[event_id] += 1
        event_publisher_groups.setdefault(event_id, set()).add(publisher_group)
        event_limits[event_id] = (source_items, independent_groups)
        if expected_entries is not None:
            expected_entry = expected_entries.get(entry_id)
            if expected_entry is None or entry != expected_entry:
                raise ChinaArticleStreamError(
                    f"{path} does not match the source wire and event analysis"
                )
        ordering.append((entry["published_at"], entry_id))
    if ordering != sorted(ordering, reverse=True):
        raise ChinaArticleStreamError("entries are not newest-first")
    for event_id, entry_count in event_entry_counts.items():
        source_items, independent_groups = event_limits[event_id]
        if entry_count > source_items:
            raise ChinaArticleStreamError(
                f"{event_id} projects more entries than its dossier source count"
            )
        if len(event_publisher_groups[event_id]) > independent_groups:
            raise ChinaArticleStreamError(
                f"{event_id} projects more publisher groups than its dossier count"
            )
        if (
            entry_count == source_items
            and len(event_publisher_groups[event_id]) != independent_groups
        ):
            raise ChinaArticleStreamError(
                f"{event_id} complete projection does not match its dossier groups"
            )
    if expected_entries is not None and identities != set(expected_entries):
        raise ChinaArticleStreamError(
            "stream entries do not match the China-relevant source wire items"
        )

    def scan_keys(node: Any, path: str = "telegram_watch") -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).lower() in forbidden_keys:
                    raise ChinaArticleStreamError(
                        f"{path} contains forbidden Telegram field {key!r}"
                    )
                scan_keys(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                scan_keys(value, f"{path}[{index}]")
        elif isinstance(node, str) and len(node) > 2_000:
            raise ChinaArticleStreamError(f"{path} contains unbounded text")

    scan_keys(document.get("telegram_watch", {}))

    # Exercise canonical serialization here so non-finite values and unusual
    # mapping keys fail before publication.
    canonical_json_bytes(document)


def stream_sha256(document: Mapping[str, Any]) -> str:
    validate_china_article_stream(document)
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


__all__ = [
    "ChinaArticleStreamError",
    "SCHEMA_VERSION",
    "build_china_article_stream",
    "canonical_json_bytes",
    "stream_sha256",
    "validate_china_article_stream",
]
