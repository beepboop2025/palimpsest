"""Coverage-honest synthesis of reports, social observations, and measurements.

This module performs no network access and no semantic inference.  It projects
already-validated Evidence Wire events, event analyses, an optional social
observation ledger, and reviewed Dragon Whispers into one navigable China situation
index. Social records join an event only through an exact canonical publisher URL;
reviewed source-free Telegram context stays in a separate briefing; measurement
records remain the event analysis's predeclared ``topic-surface-only`` context.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from core import event_analysis as event_analysis_model
from core import newswire as newswire_model
from core import peer_context as peer_context_model


SCHEMA_VERSION = "palimpsest-china-situation.v1"
SITE = "https://palimpsest.info"
RELATION_POLICY = (
    "Publisher reports, exact-link social observations, reviewed source-free "
    "Telegram signals, declared Observatory measurements, public OSINT "
    "observations joined by exact publisher URL or topic/term overlap, and "
    "attributed peer_context rows from GreatFire, OONI, CDT, or Weiboscope "
    "are shown together without converting social circulation, reviewed context, "
    "topic-level measurement context, OSINT topic overlap, or a peer's dated "
    "verdict into claim verification, causation, Palimpsest capture, or an "
    "additional independent source group."
)
MAX_SITUATIONS = 8_192
MAX_SOCIAL_PER_SITUATION = 128
MAX_REVIEWED_TELEGRAM = 10_000

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EVENT_ID_RE = re.compile(r"^event-[0-9a-f]{24}$")
_EVENT_VERSION_RE = re.compile(r"^eventv-[0-9a-f]{24}$")
_ANALYSIS_ID_RE = re.compile(r"^analysisv-[0-9a-f]{24}$")
_SITUATION_ID_RE = re.compile(r"^situation-[0-9a-f]{24}$")
_SITUATION_VERSION_RE = re.compile(r"^situationv-[0-9a-f]{24}$")
_SOCIAL_ID_RE = re.compile(r"^social-[0-9a-f]{32}$")
_SOCIAL_VERSION_RE = re.compile(r"^socialv-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_POSTURES = frozenset(
    {
        "report-only",
        "report-plus-social-context",
        "report-plus-measurement-context",
        "three-layer-context",
    }
)
_MEASUREMENT_STATES = frozenset({"none", "live", "mixed", "nonlive"})

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "url",
        "scope",
        "relation_policy",
        "inputs",
        "coverage",
        "reviewed_telegram",
        "situations",
    }
)
_INPUT_FIELDS = frozenset(
    {
        "newswire_generated_at",
        "newswire_sha256",
        "analysis_sha256",
        "social_status",
        "social_generated_at",
        "social_sha256",
        "telegram_status",
        "telegram_generated_at",
        "telegram_sha256",
    }
)
_COVERAGE_FIELDS = frozenset(
    {
        "wire_events",
        "in_scope_events",
        "publisher_reports",
        "independent_publisher_groups",
        "events_with_measurement_context",
        "measurement_context_rows",
        "social_observations",
        "social_observations_linked",
        "social_observations_unmatched",
        "social_observations_ambiguous",
        "situations_with_three_layers",
        "reviewed_telegram_signals",
        "events_with_osint_context",
        "osint_context_rows",
        "events_with_peer_context",
        "peer_context_rows",
    }
)
_SITUATION_FIELDS = frozenset(
    {
        "situation_id",
        "version_id",
        "event_id",
        "event_version_id",
        "analysis_id",
        "url",
        "headline",
        "dek",
        "desk",
        "topics",
        "published_at",
        "updated_at",
        "posture",
        "measurement_state",
        "reporting",
        "social_context",
        "measurement_context",
        "osint_context",
        "peer_context",
        "synthesis",
    }
)
_REPORTING_FIELDS = frozenset(
    {"relation", "evidence_strength", "source_count", "independent_groups", "sources"}
)
_REPORT_SOURCE_FIELDS = frozenset(
    {
        "item_id",
        "version_id",
        "source_id",
        "source_name",
        "role",
        "independence_group",
        "published_at",
        "title",
        "url",
    }
)
_SOCIAL_FIELDS = frozenset(
    {
        "observation_id",
        "version_id",
        "platform",
        "source_id",
        "source_name",
        "independence_group",
        "published_at",
        "permalink",
        "title",
        "excerpt",
        "state",
        "matched_article_url",
        "same_publisher_lineage",
        "relation",
    }
)
_MEASUREMENT_FIELDS = frozenset(
    {
        "signal_id",
        "status",
        "headline",
        "finding",
        "metric",
        "source_timestamp",
        "story_url",
        "evidence_url",
        "relation",
        "interpretation",
    }
)
_SYNTHESIS_FIELDS = frozenset({"summary", "known_unknowns", "next_checks"})
_OSINT_FIELDS = frozenset(
    {
        "observation_key",
        "source",
        "title",
        "url",
        "text",
        "language",
        "uncertainty",
        "deletion_signal",
        "confirmation_count",
        "first_seen",
        "last_seen",
        "last_confirmed_alive",
        "content_sha256",
        "gazetteer_hits",
        "archive",
        "cross_links",
        "common_crawl_match_kind",
        "common_crawl_host",
        "common_crawl_capture_at",
        "relation",
    }
)
_OSINT_LANGUAGES = frozenset({"zh", "en", "mixed", "unknown"})
_OSINT_LINK_KEYS = frozenset(
    {
        "cdt",
        "gdelt",
        "ooni",
        "greatfire",
        "weibo",
        "undertext",
        "bleedthrough",
        "common_crawl",
    }
)
_OSINT_CC_MATCH_KINDS = frozenset({"url", "host", "digest"})
MAX_OSINT_PER_SITUATION = 12
_REVIEWED_TELEGRAM_FIELDS = frozenset(
    {
        "whisper_id",
        "observed_at",
        "published_at",
        "tier",
        "families",
        "headline",
        "summary",
        "why_it_matters",
        "uncertainty",
        "next_checks",
        "limitations",
        "relation",
    }
)


class ChinaSituationError(ValueError):
    """A situation projection violates the public evidence boundary."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic strict JSON bytes for identities and receipts."""

    def reject(node: Any, path: str = "situation") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise ChinaSituationError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise ChinaSituationError(f"{path} contains a non-string key")
                reject(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                reject(child, f"{path}[{index}]")

    reject(value)
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


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:24]}"


def _situation_url(situation_id: str, *, page: int = 1) -> str:
    path = (
        "/news/china/situation/" if page == 1 else f"/news/china/situation/page/{page}/"
    )
    return f"{SITE}{path}#{situation_id}"


def _exact(value: Any, fields: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        missing = (
            sorted(fields - set(value))
            if isinstance(value, Mapping)
            else sorted(fields)
        )
        extra = sorted(set(value) - fields) if isinstance(value, Mapping) else []
        raise ChinaSituationError(
            f"{path} fields differ (missing={missing}, extra={extra})"
        )
    return value


def _text(value: Any, path: str, *, maximum: int = 4_000, empty: bool = False) -> str:
    if (
        type(value) is not str
        or len(value) > maximum
        or (not empty and not value.strip())
    ):
        raise ChinaSituationError(f"{path} must be bounded text")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise ChinaSituationError(f"{path} contains unsafe Unicode")
    return value


def _timestamp(value: Any, path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        raise ChinaSituationError(f"{path} must be canonical UTC")
    return value


def _https(
    value: Any,
    path: str,
    *,
    host: str | None = None,
    allow_fragment: bool = False,
) -> str:
    _text(value, path, maximum=2_048)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ChinaSituationError(f"{path} is malformed") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or (parsed.fragment and not allow_fragment)
    ):
        raise ChinaSituationError(f"{path} must be credential-free HTTPS")
    if host is not None and parsed.hostname != host:
        raise ChinaSituationError(f"{path} must use {host}")
    return value


def _count(value: Any, path: str) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise ChinaSituationError(f"{path} must be a bounded non-negative integer")
    return value


def _social_document_digest(social: Mapping[str, Any] | None) -> str | None:
    return (
        hashlib.sha256(canonical_json_bytes(social)).hexdigest()
        if social is not None
        else None
    )


def _social_status(social: Mapping[str, Any] | None) -> str:
    if social is None:
        return "not-configured"
    coverage = social["coverage"]
    configured = coverage["configured"]
    successful = coverage["successful"]
    failed = coverage["failed"]
    if configured == 0:
        return "registry-empty"
    if failed == configured:
        return "failed"
    if failed:
        return "degraded"
    if successful:
        return "active"
    return "not-attempted"


def _telegram_document_digest(telegram: Mapping[str, Any] | None) -> str | None:
    return (
        hashlib.sha256(canonical_json_bytes(telegram)).hexdigest()
        if telegram is not None
        else None
    )


def _telegram_status(telegram: Mapping[str, Any] | None) -> str:
    if telegram is None:
        return "not-configured"
    return "reviewed-signals" if telegram["entries"] else "awaiting-review"


def _validate_social_input(social: Mapping[str, Any]) -> None:
    """Delegate the social contract while keeping this module import-safe at bootstrap."""

    try:
        from core import social_observations as social_model
    except ImportError as exc:  # pragma: no cover - deployment packaging failure
        raise ChinaSituationError("social observation model is unavailable") from exc
    try:
        social_model.validate_latest(social)
    except (TypeError, ValueError) as exc:
        raise ChinaSituationError("social observation input is invalid") from exc


def _validate_telegram_input(telegram: Mapping[str, Any]) -> None:
    """Validate reviewed Telegram context without weakening its privacy contract."""

    try:
        from core import dragon_whispers as telegram_model
    except ImportError as exc:  # pragma: no cover - deployment packaging failure
        raise ChinaSituationError("Dragon Whispers model is unavailable") from exc
    try:
        telegram_model.validate_dragon_whispers(telegram)
    except (TypeError, ValueError) as exc:
        raise ChinaSituationError("reviewed Telegram input is invalid") from exc


def _reviewed_telegram_rows(
    telegram: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Project only the already-public, human-reviewed Dragon Whispers fields."""

    if telegram is None:
        return []
    return [
        {
            "whisper_id": entry["whisper_id"],
            "observed_at": entry["observed_at"],
            "published_at": entry["published_at"],
            "tier": entry["signal"]["tier"],
            "families": list(entry["signal"]["families"]),
            "headline": entry["analysis"]["headline"],
            "summary": entry["analysis"]["summary"],
            "why_it_matters": entry["analysis"]["why_it_matters"],
            "uncertainty": entry["analysis"]["uncertainty"],
            "next_checks": list(entry["analysis"]["next_checks"]),
            "limitations": list(entry["limitations"]),
            "relation": "human-reviewed-source-free-context-not-evidence",
        }
        for entry in telegram["entries"]
    ]


def _measurement_state(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "none"
    statuses = {row["status"] for row in rows}
    if statuses == {"live"}:
        return "live"
    if "live" in statuses:
        return "mixed"
    return "nonlive"


def _posture(*, has_social: bool, has_measurement: bool) -> str:
    if has_social and has_measurement:
        return "three-layer-context"
    if has_social:
        return "report-plus-social-context"
    if has_measurement:
        return "report-plus-measurement-context"
    return "report-only"


def _summary(
    *, groups: int, reports: int, social: int, measurements: int, peers: int = 0
) -> str:
    layers = [f"{reports} attributed publisher report{'s' if reports != 1 else ''}"]
    if social:
        layers.append(
            f"{social} exact-link social observation{'s' if social != 1 else ''}"
        )
    if measurements:
        layers.append(
            f"{measurements} declared Observatory surface{'s' if measurements != 1 else ''}"
        )
    if peers:
        layers.append(
            f"{peers} attributed peer sentence{'s' if peers != 1 else ''}"
        )
    extra = (
        " Attributed peer sentences name GreatFire, OONI, CDT, or Weiboscope "
        "and do not increase that count."
        if peers
        else ""
    )
    return (
        f"This dossier places {', '.join(layers)} in one view. The reporting represents "
        f"{groups} independent publisher group{'s' if groups != 1 else ''}; social and "
        "measurement rows remain context-only and do not increase that count."
        + extra
    )


def _next_checks(
    event: Mapping[str, Any],
    analysis: Mapping[str, Any],
    social_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    checks: list[str] = []
    if len(event["evidence_groups"]) < 2:
        checks.append(
            "Find an independently produced publisher or primary record before treating the reported claim as corroborated."
        )
    if not social_rows:
        checks.append(
            "Check the reviewed social-source registry for an exact publisher link; absence is a coverage gap, not evidence of silence."
        )
    elif any(row["state"] != "published" for row in social_rows):
        checks.append(
            "Compare social revisions with the publisher record and preserve the correction chronology."
        )
    measurement_rows = analysis["collector_context"]
    if not measurement_rows:
        checks.append(
            "Identify a relevant Observatory instrument or state explicitly that no current measurement applies."
        )
    elif any(row["status"] != "live" for row in measurement_rows):
        checks.append(
            "Refresh non-live Observatory surfaces before drawing a measurement-backed contextual conclusion."
        )
    checks.append(
        "Test the specific claim against a dated primary document or claim-specific measurement; topic co-occurrence is not verification."
    )
    return checks[:4]


def _link_osint_observations(
    event: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Join public OSINT observations by exact URL or exact topic/term overlap."""

    from core.china_observation import situation_osint_row

    urls = {ref["url"] for ref in event.get("evidence_refs", []) if ref.get("url")}
    topics = {str(t) for t in event.get("topics") or []}
    headline = str(event.get("headline") or "")
    linked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for obs in observations:
        if not isinstance(obs, Mapping):
            continue
        url = str(obs.get("url") or obs.get("source_url") or "")
        terms = [str(t) for t in obs.get("terms") or [] if t]
        url_hit = bool(url) and url in urls
        term_hit = any(term in topics for term in terms if term)
        headline_hit = any(len(term) >= 4 and term in headline for term in terms)
        if not url_hit and not term_hit and not headline_hit:
            continue
        row = situation_osint_row(obs)
        key = row["observation_key"]
        if key in seen:
            continue
        seen.add(key)
        linked.append(row)
        if len(linked) >= MAX_OSINT_PER_SITUATION:
            break
    return linked


def build_china_situation(
    wire: Mapping[str, Any],
    analyses: Mapping[str, Mapping[str, Any]],
    *,
    social: Mapping[str, Any] | None = None,
    reviewed_telegram: Mapping[str, Any] | None = None,
    osint_observations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic three-layer situation index from validated inputs."""

    newswire_model.validate_newswire_document(wire)
    expected_events = {event["event_id"] for event in wire["events"]}
    if set(analyses) != expected_events:
        raise ChinaSituationError("analyses do not account for every wire event")
    for event in wire["events"]:
        event_analysis_model.validate_event_analysis(
            analyses[event["event_id"]], event=event
        )
    if social is not None:
        _validate_social_input(social)
    if reviewed_telegram is not None:
        _validate_telegram_input(reviewed_telegram)

    telegram_rows = _reviewed_telegram_rows(reviewed_telegram)

    item_to_event: dict[str, Mapping[str, Any]] = {}
    url_to_event_ids: dict[str, set[str]] = {}
    for event in wire["events"]:
        in_scope = analyses[event["event_id"]]["scope_status"] == "in-scope"
        for reference in event["evidence_refs"]:
            item_id = reference["item_id"]
            if item_id in item_to_event:
                raise ChinaSituationError(
                    f"wire item belongs to multiple events: {item_id}"
                )
            item_to_event[item_id] = event
            if in_scope:
                url_to_event_ids.setdefault(reference["url"], set()).add(
                    event["event_id"]
                )

    linked: dict[str, list[tuple[Mapping[str, Any], str]]] = {}
    unmatched = 0
    ambiguous = 0
    social_rows = list(social.get("observations", [])) if social is not None else []
    seen_social_ids: set[str] = set()
    for observation in social_rows:
        observation_id = observation["observation_id"]
        if observation_id in seen_social_ids:
            raise ChinaSituationError(f"duplicate social observation: {observation_id}")
        seen_social_ids.add(observation_id)
        matches: dict[str, str] = {}
        for related_url in observation["related_urls"]:
            for event_id in url_to_event_ids.get(related_url, set()):
                matches[event_id] = related_url
        if not matches:
            unmatched += 1
            continue
        if len(matches) != 1:
            ambiguous += 1
            continue
        event_id, matched_url = next(iter(matches.items()))
        linked.setdefault(event_id, []).append((observation, matched_url))

    situations: list[dict[str, Any]] = []
    measurement_rows_total = 0
    osint_rows_total = 0
    peer_rows_total = 0
    all_groups: set[str] = set()
    publisher_reports = 0
    with_measurements = 0
    with_osint = 0
    with_peers = 0
    with_three_layers = 0
    for event in wire["events"]:
        analysis = analyses[event["event_id"]]
        if analysis["scope_status"] != "in-scope":
            continue
        event_social = sorted(
            linked.get(event["event_id"], []),
            key=lambda pair: (pair[0]["published_at"], pair[0]["observation_id"]),
            reverse=True,
        )
        if len(event_social) > MAX_SOCIAL_PER_SITUATION:
            raise ChinaSituationError("social context exceeds the per-situation cap")
        event_groups = {group["group_id"] for group in event["evidence_groups"]}
        all_groups.update(event_groups)
        publisher_reports += len(event["evidence_refs"])

        social_context = [
            {
                "observation_id": observation["observation_id"],
                "version_id": observation["version_id"],
                "platform": observation["platform"],
                "source_id": observation["source_id"],
                "source_name": observation["source_name"],
                "independence_group": observation["independence_group"],
                "published_at": observation["published_at"],
                "permalink": observation["permalink"],
                "title": observation["title"],
                "excerpt": observation["excerpt"],
                "state": observation["state"],
                "matched_article_url": matched_url,
                "same_publisher_lineage": observation["independence_group"]
                in event_groups,
                "relation": "publisher-link-context-not-corroboration",
            }
            for observation, matched_url in event_social
        ]
        measurement_context = [
            {
                "signal_id": row["signal_id"],
                "status": row["status"],
                "headline": row["headline"],
                "finding": row["finding"],
                "metric": {
                    "label": row["metric"]["label"],
                    "value": row["metric"]["value"],
                    "unit": row["metric"]["unit"],
                    "denominator": {
                        "label": row["metric"]["denominator"]["label"],
                        "value": row["metric"]["denominator"]["value"],
                    },
                },
                "source_timestamp": row["source_timestamp"],
                "story_url": row["story_url"],
                "evidence_url": row["evidence_url"],
                "relation": row["relation"],
                "interpretation": row["interpretation"],
            }
            for row in analysis["collector_context"]
        ]
        measurement_rows_total += len(measurement_context)
        if measurement_context:
            with_measurements += 1
        if social_context and measurement_context:
            with_three_layers += 1
        osint_context = _link_osint_observations(event, osint_observations or [])
        osint_rows_total += len(osint_context)
        if osint_context:
            with_osint += 1
        peer_rows = [dict(row) for row in analysis.get("peer_context") or []]
        peer_rows_total += len(peer_rows)
        if peer_rows:
            with_peers += 1

        reporting_sources = [
            {
                "item_id": reference["item_id"],
                "version_id": reference["version_id"],
                "source_id": reference["source_id"],
                "source_name": reference["source_name"],
                "role": reference["role"],
                "independence_group": reference["independence_group"],
                "published_at": reference["published_at"],
                "title": reference["title"],
                "url": reference["url"],
            }
            for reference in event["evidence_refs"]
        ]
        updated_candidates = [event["updated_at"], analysis["generated_at"]]
        updated_candidates.extend(
            observation["first_observed_at"]
            for observation, _matched_url in event_social
        )
        situation_id = _stable_id("situation", {"event_id": event["event_id"]})
        core = {
            "situation_id": situation_id,
            "event_id": event["event_id"],
            "event_version_id": event["version_id"],
            "analysis_id": analysis["analysis_id"],
            "url": _situation_url(situation_id),
            "headline": event["headline"],
            "dek": event["dek"],
            "desk": event["desk"],
            "topics": list(event["topics"]),
            "published_at": event["published_at"],
            "updated_at": max(updated_candidates),
            "posture": _posture(
                has_social=bool(social_context),
                has_measurement=bool(measurement_context),
            ),
            "measurement_state": _measurement_state(measurement_context),
            "reporting": {
                "relation": "attributed-publisher-reporting",
                "evidence_strength": event["evidence_strength"],
                "source_count": len(reporting_sources),
                "independent_groups": len(event_groups),
                "sources": reporting_sources,
            },
            "social_context": social_context,
            "measurement_context": measurement_context,
            "osint_context": osint_context,
            "peer_context": peer_rows,
            "synthesis": {
                "summary": _summary(
                    groups=len(event_groups),
                    reports=len(reporting_sources),
                    social=len(social_context),
                    measurements=len(measurement_context),
                    peers=len(peer_rows),
                ),
                "known_unknowns": list(analysis["limitations"]),
                "next_checks": _next_checks(event, analysis, social_context),
            },
        }
        version_payload = {
            key: value
            for key, value in core.items()
            if key not in {"situation_id", "url"}
        }
        situations.append(
            {
                "situation_id": situation_id,
                "version_id": _stable_id("situationv", version_payload),
                **{key: value for key, value in core.items() if key != "situation_id"},
            }
        )

    situations.sort(
        key=lambda row: (row["updated_at"], row["situation_id"]), reverse=True
    )
    if len(situations) > MAX_SITUATIONS:
        raise ChinaSituationError("situation index exceeds its publication cap")

    analysis_digest = hashlib.sha256(
        canonical_json_bytes([analyses[event_id] for event_id in sorted(analyses)])
    ).hexdigest()
    social_generated_at = social["generated_at"] if social is not None else None
    telegram_generated_at = (
        reviewed_telegram["generated_at"] if reviewed_telegram is not None else None
    )
    generated_candidates = [wire["generated_at"]]
    generated_candidates.extend(row["generated_at"] for row in analyses.values())
    if social_generated_at is not None:
        generated_candidates.append(social_generated_at)
    if telegram_generated_at is not None:
        generated_candidates.append(telegram_generated_at)
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": max(generated_candidates),
        "url": f"{SITE}/news/china/situation/",
        "scope": (
            "Every in-scope current-window Evidence Wire event, its predeclared "
            "Observatory context, social observations joined by exact allowlisted "
            "publisher article URL, public OSINT observations joined by exact URL or "
            "topic/term overlap, plus a separate briefing of human-reviewed, "
            "source-free Telegram signals. Coverage is bounded by declared registries, "
            "review receipts, and APIs."
        ),
        "relation_policy": RELATION_POLICY,
        "inputs": {
            "newswire_generated_at": wire["generated_at"],
            "newswire_sha256": hashlib.sha256(canonical_json_bytes(wire)).hexdigest(),
            "analysis_sha256": analysis_digest,
            "social_status": _social_status(social),
            "social_generated_at": social_generated_at,
            "social_sha256": _social_document_digest(social),
            "telegram_status": _telegram_status(reviewed_telegram),
            "telegram_generated_at": telegram_generated_at,
            "telegram_sha256": _telegram_document_digest(reviewed_telegram),
        },
        "coverage": {
            "wire_events": len(wire["events"]),
            "in_scope_events": len(situations),
            "publisher_reports": publisher_reports,
            "independent_publisher_groups": len(all_groups),
            "events_with_measurement_context": with_measurements,
            "measurement_context_rows": measurement_rows_total,
            "social_observations": len(social_rows),
            "social_observations_linked": sum(len(rows) for rows in linked.values()),
            "social_observations_unmatched": unmatched,
            "social_observations_ambiguous": ambiguous,
            "situations_with_three_layers": with_three_layers,
            "reviewed_telegram_signals": len(telegram_rows),
            "events_with_osint_context": with_osint,
            "osint_context_rows": osint_rows_total,
            "events_with_peer_context": with_peers,
            "peer_context_rows": peer_rows_total,
        },
        "reviewed_telegram": telegram_rows,
        "situations": situations,
    }
    validate_china_situation(document)
    return document


def bind_situation_page_urls(
    document: Mapping[str, Any], *, page_size: int
) -> dict[str, Any]:
    """Bind presentation URLs to deterministic archive pages without changing IDs."""

    validate_china_situation(document)
    if type(page_size) is not int or not 1 <= page_size <= MAX_SITUATIONS:
        raise ChinaSituationError("situation page_size is outside its bounds")
    situations = [
        {
            **row,
            "url": _situation_url(row["situation_id"], page=(index // page_size) + 1),
        }
        for index, row in enumerate(document["situations"])
    ]
    bound = {**document, "situations": situations}
    validate_china_situation(bound)
    return bound


def validate_china_situation(document: Mapping[str, Any]) -> None:
    """Validate the closed public synthesis contract and its key invariants."""

    top = _exact(document, _TOP_FIELDS, "situation")
    if top["schema_version"] != SCHEMA_VERSION:
        raise ChinaSituationError("unsupported situation schema")
    _timestamp(top["generated_at"], "generated_at")
    _https(top["url"], "url", host="palimpsest.info")
    _text(top["scope"], "scope")
    if top["relation_policy"] != RELATION_POLICY:
        raise ChinaSituationError("relation policy may not imply verification")

    inputs = _exact(top["inputs"], _INPUT_FIELDS, "inputs")
    _timestamp(inputs["newswire_generated_at"], "inputs.newswire_generated_at")
    for field in ("newswire_sha256", "analysis_sha256"):
        if (
            type(inputs[field]) is not str
            or _SHA256_RE.fullmatch(inputs[field]) is None
        ):
            raise ChinaSituationError(f"inputs.{field} is invalid")
    _text(inputs["social_status"], "inputs.social_status", maximum=80)
    social_generated = _timestamp(
        inputs["social_generated_at"], "inputs.social_generated_at", nullable=True
    )
    social_digest = inputs["social_sha256"]
    if (social_generated is None) != (social_digest is None):
        raise ChinaSituationError("social timestamp and digest must appear together")
    if social_digest is not None and (
        type(social_digest) is not str or _SHA256_RE.fullmatch(social_digest) is None
    ):
        raise ChinaSituationError("inputs.social_sha256 is invalid")
    _text(inputs["telegram_status"], "inputs.telegram_status", maximum=80)
    telegram_generated = _timestamp(
        inputs["telegram_generated_at"], "inputs.telegram_generated_at", nullable=True
    )
    telegram_digest = inputs["telegram_sha256"]
    if (telegram_generated is None) != (telegram_digest is None):
        raise ChinaSituationError("Telegram timestamp and digest must appear together")
    if telegram_digest is not None and (
        type(telegram_digest) is not str
        or _SHA256_RE.fullmatch(telegram_digest) is None
    ):
        raise ChinaSituationError("inputs.telegram_sha256 is invalid")

    coverage = _exact(top["coverage"], _COVERAGE_FIELDS, "coverage")
    for field, value in coverage.items():
        _count(value, f"coverage.{field}")
    if coverage["in_scope_events"] > coverage["wire_events"]:
        raise ChinaSituationError("in-scope event count exceeds wire events")
    social_partition = (
        coverage["social_observations_linked"]
        + coverage["social_observations_unmatched"]
        + coverage["social_observations_ambiguous"]
    )
    if social_partition != coverage["social_observations"]:
        raise ChinaSituationError("social coverage does not partition observations")

    reviewed_telegram = top["reviewed_telegram"]
    if (
        type(reviewed_telegram) is not list
        or len(reviewed_telegram) > MAX_REVIEWED_TELEGRAM
    ):
        raise ChinaSituationError("reviewed_telegram must be a bounded array")
    if coverage["reviewed_telegram_signals"] != len(reviewed_telegram):
        raise ChinaSituationError("reviewed Telegram count does not match the briefing")
    telegram_ids: set[str] = set()
    telegram_order: list[tuple[str, str]] = []
    for index, value in enumerate(reviewed_telegram):
        path = f"reviewed_telegram[{index}]"
        row = _exact(value, _REVIEWED_TELEGRAM_FIELDS, path)
        if (
            type(row["whisper_id"]) is not str
            or re.fullmatch(r"whisper-[0-9a-f]{24}", row["whisper_id"]) is None
            or row["whisper_id"] in telegram_ids
        ):
            raise ChinaSituationError(f"{path}.whisper_id is invalid or repeated")
        telegram_ids.add(row["whisper_id"])
        _timestamp(row["observed_at"], f"{path}.observed_at")
        _timestamp(row["published_at"], f"{path}.published_at")
        telegram_order.append((row["published_at"], row["whisper_id"]))
        for field, maximum in (
            ("tier", 80),
            ("headline", 180),
            ("summary", 1_200),
            ("why_it_matters", 1_800),
            ("uncertainty", 1_200),
        ):
            _text(row[field], f"{path}.{field}", maximum=maximum)
        for field, minimum, maximum, text_maximum in (
            ("families", 0, 32, 80),
            ("next_checks", 2, 8, 500),
            ("limitations", 3, 8, 500),
        ):
            items = row[field]
            if type(items) is not list or not minimum <= len(items) <= maximum:
                raise ChinaSituationError(f"{path}.{field} must be a bounded array")
            for item_index, item in enumerate(items):
                _text(item, f"{path}.{field}[{item_index}]", maximum=text_maximum)
        if row["families"] != sorted(set(row["families"])):
            raise ChinaSituationError(f"{path}.families must be sorted and unique")
        if row["relation"] != "human-reviewed-source-free-context-not-evidence":
            raise ChinaSituationError(f"{path}.relation is invalid")
    if telegram_order != sorted(telegram_order, reverse=True):
        raise ChinaSituationError("reviewed Telegram rows are not newest-first")

    rows = top["situations"]
    if type(rows) is not list or len(rows) > MAX_SITUATIONS:
        raise ChinaSituationError("situations must be a bounded array")
    ids: set[str] = set()
    social_ids: set[str] = set()
    previous_key: tuple[str, str] | None = None
    total_reports = 0
    total_measurements = 0
    measured_events = 0
    total_osint = 0
    osint_events = 0
    total_peers = 0
    peer_events = 0
    three_layers = 0
    groups: set[str] = set()
    for index, value in enumerate(rows):
        path = f"situations[{index}]"
        row = _exact(value, _SITUATION_FIELDS, path)
        for field, pattern in (
            ("situation_id", _SITUATION_ID_RE),
            ("version_id", _SITUATION_VERSION_RE),
            ("event_id", _EVENT_ID_RE),
            ("event_version_id", _EVENT_VERSION_RE),
            ("analysis_id", _ANALYSIS_ID_RE),
        ):
            if type(row[field]) is not str or pattern.fullmatch(row[field]) is None:
                raise ChinaSituationError(f"{path}.{field} is invalid")
        if row["situation_id"] in ids:
            raise ChinaSituationError("duplicate situation ID")
        ids.add(row["situation_id"])
        situation_url = _https(
            row["url"],
            f"{path}.url",
            host="palimpsest.info",
            allow_fragment=True,
        )
        parsed_situation_url = urlsplit(situation_url)
        page_path = re.fullmatch(
            r"/news/china/situation/page/(?:[2-9]|[1-9][0-9]+)/",
            parsed_situation_url.path,
        )
        if (
            parsed_situation_url.netloc != "palimpsest.info"
            or parsed_situation_url.query
            or parsed_situation_url.fragment != row["situation_id"]
            or (
                parsed_situation_url.path != "/news/china/situation/"
                and page_path is None
            )
        ):
            raise ChinaSituationError(f"{path}.url is not a canonical situation anchor")
        for field, maximum in (("headline", 1_000), ("dek", 4_000), ("desk", 80)):
            _text(row[field], f"{path}.{field}", maximum=maximum)
        if type(row["topics"]) is not list or len(row["topics"]) > 32:
            raise ChinaSituationError(f"{path}.topics must be bounded")
        for topic in row["topics"]:
            _text(topic, f"{path}.topics", maximum=80)
        _timestamp(row["published_at"], f"{path}.published_at")
        _timestamp(row["updated_at"], f"{path}.updated_at")
        if row["posture"] not in _POSTURES:
            raise ChinaSituationError(f"{path}.posture is invalid")
        if row["measurement_state"] not in _MEASUREMENT_STATES:
            raise ChinaSituationError(f"{path}.measurement_state is invalid")
        order_key = (row["updated_at"], row["situation_id"])
        if previous_key is not None and order_key > previous_key:
            raise ChinaSituationError("situations are not reverse chronological")
        previous_key = order_key

        reporting = _exact(row["reporting"], _REPORTING_FIELDS, f"{path}.reporting")
        if reporting["relation"] != "attributed-publisher-reporting":
            raise ChinaSituationError("reporting relation is invalid")
        _text(
            reporting["evidence_strength"],
            f"{path}.reporting.evidence_strength",
            maximum=80,
        )
        source_count = _count(
            reporting["source_count"], f"{path}.reporting.source_count"
        )
        group_count = _count(
            reporting["independent_groups"], f"{path}.reporting.independent_groups"
        )
        sources = reporting["sources"]
        if type(sources) is not list or len(sources) != source_count or not sources:
            raise ChinaSituationError(f"{path}.reporting.sources does not match count")
        local_groups: set[str] = set()
        for source_index, source_value in enumerate(sources):
            source_path = f"{path}.reporting.sources[{source_index}]"
            source = _exact(source_value, _REPORT_SOURCE_FIELDS, source_path)
            for field in (
                "item_id",
                "version_id",
                "source_id",
                "source_name",
                "role",
                "independence_group",
                "title",
            ):
                _text(source[field], f"{source_path}.{field}", maximum=1_000)
            _timestamp(source["published_at"], f"{source_path}.published_at")
            _https(source["url"], f"{source_path}.url")
            local_groups.add(source["independence_group"])
        if len(local_groups) != group_count:
            raise ChinaSituationError(f"{path} independence count is inconsistent")
        groups.update(local_groups)
        total_reports += source_count

        social_context = row["social_context"]
        if (
            type(social_context) is not list
            or len(social_context) > MAX_SOCIAL_PER_SITUATION
        ):
            raise ChinaSituationError(f"{path}.social_context must be bounded")
        for social_index, social_value in enumerate(social_context):
            social_path = f"{path}.social_context[{social_index}]"
            social_row = _exact(social_value, _SOCIAL_FIELDS, social_path)
            if (
                type(social_row["observation_id"]) is not str
                or _SOCIAL_ID_RE.fullmatch(social_row["observation_id"]) is None
                or social_row["observation_id"] in social_ids
            ):
                raise ChinaSituationError(
                    f"{social_path}.observation_id is invalid or repeated"
                )
            social_ids.add(social_row["observation_id"])
            if (
                type(social_row["version_id"]) is not str
                or _SOCIAL_VERSION_RE.fullmatch(social_row["version_id"]) is None
            ):
                raise ChinaSituationError(f"{social_path}.version_id is invalid")
            for field, maximum in (
                ("platform", 40),
                ("source_id", 80),
                ("source_name", 200),
                ("independence_group", 80),
                ("title", 1_000),
                ("excerpt", 2_000),
                ("state", 40),
            ):
                _text(
                    social_row[field],
                    f"{social_path}.{field}",
                    maximum=maximum,
                    empty=field == "excerpt",
                )
            _timestamp(social_row["published_at"], f"{social_path}.published_at")
            for field in ("permalink", "matched_article_url"):
                _https(social_row[field], f"{social_path}.{field}")
            if type(social_row["same_publisher_lineage"]) is not bool:
                raise ChinaSituationError(
                    f"{social_path}.same_publisher_lineage must be boolean"
                )
            if social_row["relation"] != "publisher-link-context-not-corroboration":
                raise ChinaSituationError(f"{social_path}.relation is invalid")

        measurements = row["measurement_context"]
        if type(measurements) is not list:
            raise ChinaSituationError(f"{path}.measurement_context must be an array")
        for measurement_index, measurement_value in enumerate(measurements):
            measurement_path = f"{path}.measurement_context[{measurement_index}]"
            measurement = _exact(
                measurement_value, _MEASUREMENT_FIELDS, measurement_path
            )
            for field, maximum in (
                ("signal_id", 80),
                ("status", 40),
                ("headline", 1_000),
                ("finding", 4_000),
                ("interpretation", 2_000),
            ):
                _text(
                    measurement[field], f"{measurement_path}.{field}", maximum=maximum
                )
            _timestamp(
                measurement["source_timestamp"], f"{measurement_path}.source_timestamp"
            )
            for field in ("story_url", "evidence_url"):
                _https(
                    measurement[field],
                    f"{measurement_path}.{field}",
                    host="palimpsest.info",
                )
            if measurement["relation"] != "topic-surface-only":
                raise ChinaSituationError(f"{measurement_path}.relation is invalid")
            if type(measurement["metric"]) is not dict:
                raise ChinaSituationError(
                    f"{measurement_path}.metric must be an object"
                )
            canonical_json_bytes(measurement["metric"])
        expected_measurement_state = _measurement_state(measurements)
        if row["measurement_state"] != expected_measurement_state:
            raise ChinaSituationError(f"{path}.measurement_state is inconsistent")
        total_measurements += len(measurements)
        measured_events += bool(measurements)

        expected_posture = _posture(
            has_social=bool(social_context), has_measurement=bool(measurements)
        )
        if row["posture"] != expected_posture:
            raise ChinaSituationError(f"{path}.posture is inconsistent")
        three_layers += bool(social_context and measurements)
        osint_rows = row["osint_context"]
        if type(osint_rows) is not list or len(osint_rows) > MAX_OSINT_PER_SITUATION:
            raise ChinaSituationError(f"{path}.osint_context must be bounded")
        for osint_index, osint_value in enumerate(osint_rows):
            osint_path = f"{path}.osint_context[{osint_index}]"
            osint_row = _exact(osint_value, _OSINT_FIELDS, osint_path)
            _text(osint_row["observation_key"], f"{osint_path}.observation_key", maximum=64)
            _text(osint_row["source"], f"{osint_path}.source", maximum=80)
            _text(osint_row["title"], f"{osint_path}.title", maximum=240)
            if osint_row["url"]:
                _https(osint_row["url"], f"{osint_path}.url")
            if osint_row["relation"] != "topic-or-url-context-not-corroboration":
                raise ChinaSituationError(f"{osint_path}.relation is invalid")
            for stamp in ("first_seen", "last_seen", "last_confirmed_alive"):
                _timestamp(osint_row[stamp], f"{osint_path}.{stamp}", nullable=True)
            if osint_row["content_sha256"] is not None and (
                type(osint_row["content_sha256"]) is not str
                or _SHA256_RE.fullmatch(osint_row["content_sha256"]) is None
            ):
                raise ChinaSituationError(f"{osint_path}.content_sha256 is invalid")
            _text(osint_row["text"], f"{osint_path}.text", maximum=2_000, empty=True)
            if osint_row["language"] not in _OSINT_LANGUAGES:
                raise ChinaSituationError(f"{osint_path}.language is invalid")
            _text(
                osint_row["deletion_signal"],
                f"{osint_path}.deletion_signal",
                maximum=80,
                empty=True,
            )
            if (
                type(osint_row["confirmation_count"]) is not int
                or osint_row["confirmation_count"] < 0
                or osint_row["confirmation_count"] > 12
            ):
                raise ChinaSituationError(f"{osint_path}.confirmation_count is invalid")
            notes = osint_row["uncertainty"]
            if type(notes) is not list or len(notes) > 8:
                raise ChinaSituationError(f"{osint_path}.uncertainty must be bounded")
            for note in notes:
                _text(note, f"{osint_path}.uncertainty", maximum=240)
            links = _exact(osint_row["cross_links"], _OSINT_LINK_KEYS, f"{osint_path}.cross_links")
            for link_key, link in links.items():
                if link is None:
                    continue
                if type(link) is not dict:
                    raise ChinaSituationError(f"{osint_path}.cross_links.{link_key} is invalid")
                extra_link = set(link) - {"id", "url", "note"}
                if extra_link:
                    raise ChinaSituationError(
                        f"{osint_path}.cross_links.{link_key} has unexpected keys"
                    )
                if link_key == "common_crawl" and link.get("url"):
                    raise ChinaSituationError(
                        f"{osint_path}.cross_links.common_crawl must not publish a lake URL"
                    )
            cc_kind = osint_row["common_crawl_match_kind"]
            if cc_kind is not None and cc_kind not in _OSINT_CC_MATCH_KINDS:
                raise ChinaSituationError(f"{osint_path}.common_crawl_match_kind is invalid")
            if osint_row["common_crawl_host"] is not None:
                _text(osint_row["common_crawl_host"], f"{osint_path}.common_crawl_host", maximum=253)
            _timestamp(
                osint_row["common_crawl_capture_at"],
                f"{osint_path}.common_crawl_capture_at",
                nullable=True,
            )
        total_osint += len(osint_rows)
        osint_events += bool(osint_rows)
        peer_rows = row["peer_context"]
        if type(peer_rows) is not list or len(peer_rows) > peer_context_model.MAX_PEERS_PER_EVENT:
            raise ChinaSituationError(f"{path}.peer_context must be bounded")
        for peer_index, peer_value in enumerate(peer_rows):
            peer_path = f"{path}.peer_context[{peer_index}]"
            peer_row = _exact(peer_value, peer_context_model.PEER_FIELDS, peer_path)
            if peer_row["peer"] not in peer_context_model.PEERS:
                raise ChinaSituationError(f"{peer_path}.peer is invalid")
            if peer_row["status"] not in peer_context_model.STATUSES:
                raise ChinaSituationError(f"{peer_path}.status is invalid")
            _text(peer_row["sentence"], f"{peer_path}.sentence", maximum=600)
            if peer_row["relation"] != peer_context_model.RELATION:
                raise ChinaSituationError(f"{peer_path}.relation is invalid")
            _timestamp(peer_row["as_of"], f"{peer_path}.as_of", nullable=True)
            if peer_row["peer_url"]:
                _https(peer_row["peer_url"], f"{peer_path}.peer_url")
            if peer_row["excerpt"] is not None:
                _text(
                    peer_row["excerpt"],
                    f"{peer_path}.excerpt",
                    maximum=peer_context_model.CDT_EXCERPT_LIMIT,
                    empty=True,
                )
        total_peers += len(peer_rows)
        peer_events += bool(peer_rows)
        synthesis = _exact(row["synthesis"], _SYNTHESIS_FIELDS, f"{path}.synthesis")
        _text(synthesis["summary"], f"{path}.synthesis.summary", maximum=2_000)
        for field in ("known_unknowns", "next_checks"):
            values = synthesis[field]
            if type(values) is not list or not values or len(values) > 16:
                raise ChinaSituationError(f"{path}.synthesis.{field} must be bounded")
            for item in values:
                _text(item, f"{path}.synthesis.{field}", maximum=4_000)

        expected_id = _stable_id("situation", {"event_id": row["event_id"]})
        if row["situation_id"] != expected_id:
            raise ChinaSituationError(f"{path}.situation_id is not canonical")
        version_payload = {
            key: value
            for key, value in row.items()
            if key not in {"situation_id", "version_id", "url"}
        }
        if row["version_id"] != _stable_id("situationv", version_payload):
            raise ChinaSituationError(f"{path}.version_id is not canonical")

    if len(rows) != coverage["in_scope_events"]:
        raise ChinaSituationError("situation row count differs from coverage")
    if total_reports != coverage["publisher_reports"]:
        raise ChinaSituationError("publisher report coverage is inconsistent")
    if len(groups) != coverage["independent_publisher_groups"]:
        raise ChinaSituationError("publisher group coverage is inconsistent")
    if measured_events != coverage["events_with_measurement_context"]:
        raise ChinaSituationError("measured-event coverage is inconsistent")
    if total_measurements != coverage["measurement_context_rows"]:
        raise ChinaSituationError("measurement-row coverage is inconsistent")
    if len(social_ids) != coverage["social_observations_linked"]:
        raise ChinaSituationError("linked-social coverage is inconsistent")
    if three_layers != coverage["situations_with_three_layers"]:
        raise ChinaSituationError("three-layer coverage is inconsistent")
    if osint_events != coverage["events_with_osint_context"]:
        raise ChinaSituationError("osint-event coverage is inconsistent")
    if total_osint != coverage["osint_context_rows"]:
        raise ChinaSituationError("osint-row coverage is inconsistent")
    if peer_events != coverage["events_with_peer_context"]:
        raise ChinaSituationError("peer-event coverage is inconsistent")
    if total_peers != coverage["peer_context_rows"]:
        raise ChinaSituationError("peer-row coverage is inconsistent")
    canonical_json_bytes(document)


__all__ = [
    "SCHEMA_VERSION",
    "RELATION_POLICY",
    "ChinaSituationError",
    "bind_situation_page_urls",
    "build_china_situation",
    "canonical_json_bytes",
    "validate_china_situation",
]
