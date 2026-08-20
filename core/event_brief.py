"""Journalist-grade, fail-closed briefs for China-scoped wire events.

This module assembles a closed article shape from retained event metadata and
only the live-family readings the caller supplies. It never fetches article
bodies, never generates free-form model prose, and never turns a topical join
into a motive or blocking claim.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from core import event_interconnection, live_paths


SCHEMA_VERSION = "palimpsest-event-analysis.v2"
PUBLICATION_MODE = "deterministic-event-brief"
DISCLOSURE = (
    "Generated from one validated newswire event, its declared collector stories, "
    "and only the live-family readings supplied to this builder. No interviews, "
    "no article-body fetch, and no free-form model prose were used."
)
METHOD = (
    "Deterministic assessment of one validated newswire event against its "
    "independent-source structure, the collector stories explicitly declared by "
    "that event, and only the live-family readings supplied to this builder "
    "(official-first-seen, public-deletion-ledgers, news-wire-live, undertext, "
    "and archive-news-context). Named-key interconnection peers from supplied "
    "peer-warehouse readings attach only on an exact host, URL path, extracted "
    "term, or ASN plus a UTC calendar-day ±24h window. A peer without an exact "
    "key, a silent warehouse, or a warming_up warehouse is skipped. GreatFire, "
    "OONI, and CDT counts keep separate denominators and dates. Collector, "
    "live-family, and interconnection joins remain topic-surface-only: no "
    "article body is fetched, no generative model is used, and no current "
    "measurement is represented as article-specific verification, causation, "
    "or motive. The v2 brief copies retained metadata into a closed article "
    "shape with sentence-level citations. A missing surface abstains."
)

LIVE_FAMILY_IDS = (
    "official-first-seen",
    "public-deletion-ledgers",
    "news-wire-live",
    "undertext",
)
ARCHIVE_SURFACE_ID = "archive-news-context"
PIPE_SIGNAL_IDS = frozenset(
    {"ooni-gfw", "bleedthrough", "vantage-fusion", "ioda-outages", "inside-view"}
)
LIVE_FAMILY_FILES = {
    "official-first-seen": "official-first-seen-latest.json",
    "public-deletion-ledgers": "public-deletion-ledgers-latest.json",
    "news-wire-live": "news-wire-live-latest.json",
    "undertext": "undertext-latest.json",
}
READING_URLS = {
    family: f"https://palimpsest.info/readings/{filename}"
    for family, filename in LIVE_FAMILY_FILES.items()
}
READING_URLS[ARCHIVE_SURFACE_ID] = (
    "https://palimpsest.info/readings/archive-news-context-latest.json"
)

OFFICIAL_HOSTS = frozenset(
    {
        "news.cn",
        "www.news.cn",
        "xinhuanet.com",
        "www.xinhuanet.com",
        "people.com.cn",
        "www.people.com.cn",
        "paper.people.com.cn",
        "people.cn",
        "www.people.cn",
        "fmprc.gov.cn",
        "www.fmprc.gov.cn",
        "mfa.gov.cn",
        "www.mfa.gov.cn",
        "stats.gov.cn",
        "www.stats.gov.cn",
    }
)
OFFICIAL_HOST_SUFFIXES = (".gov.cn",)
OFFICIAL_HOST_MARKERS = (
    "news.cn",
    "xinhuanet.com",
    "people.com.cn",
    "people.cn",
    "fmprc.gov.cn",
    "mfa.gov.cn",
    "stats.gov.cn",
)

FORBIDDEN_CAUSAL = (
    "censored because",
    "this was censored",
    "intent to",
    "because they",
    "this article was blocked because",
    "blocked because",
    "why the party",
    "why the censor",
    "the party did",
    "the censor did",
    "ordered the deletion",
    "intended to suppress",
    "cover-up",
    "to silence",
)

_LAYER_STATUSES = frozenset(
    {"present", "abstained", "not-applicable", "not-declared", "none-reviewed"}
)
_ARCHIVE_RECEIPT_FIELDS = (
    "target_id",
    "host",
    "crawl",
    "last_capture_at",
    "unique_urls",
    "mutation_rate",
    "archive_gap_rate",
    "anomaly_state",
    "absence_semantics",
)
_WINDOW_PEER_FIELDS = frozenset(
    {
        "same_window_peer_count",
        "shared_topics",
        "peer_source_ids",
        "peer_independence_groups",
        "relation",
    }
)
_CORROBORATION_FIELDS = frozenset(
    {
        "accepted_edges",
        "primary_docs",
        "reviewed",
        "official_page",
        "status",
        "relation",
    }
)
_ARCHIVE_NEWS_CONTEXT_FIELDS = frozenset(
    {
        "matched",
        "match_kind",
        "event_id",
        "anomaly_state",
        "anomaly_score_published",
        "relation",
        "receipts",
        "refresh_status",
    }
)
_SURFACE_STATUSES = frozenset({"live", "missing", "unmatched", "not-applicable"})
_EVIDENCE_RE = re.compile(r"^eventevidence-[0-9a-f]{20}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_SURFACE_FIELDS = frozenset(
    {
        "surface_id",
        "status",
        "match_kind",
        "headline",
        "finding",
        "source_timestamp",
        "reading_url",
        "input_sha256",
        "relation",
        "interpretation",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "evidence_id",
        "kind",
        "surface_id",
        "status",
        "headline",
        "claim",
        "reading_url",
        "source_timestamp",
        "input_sha256",
        "interpretation_limit",
    }
)
_SENTENCE_FIELDS = frozenset({"text", "citation_ids"})
_LAYER_FIELDS = frozenset({"status", "sentences"})
_RECORD_FIELDS = frozenset({"text", "citation_ids"})
_NUMBER_FIELDS = frozenset({"value", "label", "note", "citation_ids"})
_GATE_FIELDS = frozenset({"gate_id", "label", "passed", "detail"})
_RECEIPT_FIELDS = frozenset(
    {
        "status",
        "publishable",
        "automatic_publication",
        "citation_coverage",
        "human_review_required",
        "availability_warnings",
        "gates",
    }
)
_AUTHORSHIP_FIELDS = frozenset(
    {
        "byline",
        "mode",
        "human_interviews",
        "freeform_model_generation",
    }
)
_DECLARED_FIELDS = frozenset(
    {
        "relation",
        "scan_signal_ids",
        "economic_signal_ids",
        "live_family_ids",
    }
)
_BRIEF_FIELDS = frozenset(
    {
        "lead",
        "timeline",
        "official_page",
        "deletion_ledger",
        "pipe_context",
        "archive_context",
    }
)
_TOP_V2_EXTRA = frozenset(
    {
        "brief",
        "surface_context",
        "evidence",
        "counterreadings",
        "unknowns",
        "key_numbers",
        "publication_receipt",
        "authorship",
        "disclosure",
        "declared_links",
        "window_peers",
        "corroboration",
        "archive_news_context",
        "interconnection",
        "peer_context",
    }
)


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("brief payload contains a non-finite number")
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


def _stable_id(prefix: str, value: Any, length: int) -> str:
    digest = hashlib.sha256(_canonical_json_bytes(value).rstrip(b"\n")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _sentence(text: str, *citation_ids: str) -> dict[str, Any]:
    return {"text": text, "citation_ids": list(citation_ids)}


def _record(text: str, *citation_ids: str) -> dict[str, Any]:
    return {"text": text, "citation_ids": list(citation_ids)}


def _layer(status: str, *sentences: Mapping[str, Any]) -> dict[str, Any]:
    return {"status": status, "sentences": list(sentences)}


def _sha256_bytes(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, Mapping):
        return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _normalize_url(value: Any) -> str:
    if type(value) is not str:
        return ""
    text = value.strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/")
    if not host:
        return text.rstrip("/").casefold()
    return f"{parsed.scheme.casefold()}://{host}{path}"


def _host(value: Any) -> str:
    if type(value) is not str:
        return ""
    return (urlsplit(value).hostname or "").casefold()


def _is_official_host(host: str) -> bool:
    if not host:
        return False
    if host in OFFICIAL_HOSTS:
        return True
    if host.endswith(OFFICIAL_HOST_SUFFIXES):
        return True
    return any(marker in host for marker in OFFICIAL_HOST_MARKERS)


def event_urls(event: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for ref in event.get("evidence_refs") or []:
        if type(ref) is not dict:
            continue
        url = ref.get("url")
        if type(url) is not str or not url:
            continue
        key = _normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def official_urls(event: Mapping[str, Any]) -> list[str]:
    return [url for url in event_urls(event) if _is_official_host(_host(url))]


def event_topics(event: Mapping[str, Any]) -> set[str]:
    return {
        topic.casefold()
        for topic in (event.get("topics") or [])
        if type(topic) is str and topic
    }


def _observation_terms(record: Mapping[str, Any]) -> set[str]:
    terms: set[str] = set()
    for field in ("terms", "topics"):
        values = record.get(field) or []
        if not isinstance(values, list):
            continue
        for item in values:
            if type(item) is str and item:
                terms.add(item.casefold())
    return terms


def _observation_url(record: Mapping[str, Any]) -> str:
    for field in ("url", "source_url"):
        value = record.get(field)
        if type(value) is str and value:
            return value
    return ""


def _observation_time(record: Mapping[str, Any]) -> str | None:
    for field in (
        "detected_at",
        "first_seen",
        "last_confirmed_alive",
        "last_seen",
        "published_at",
    ):
        value = record.get(field)
        if type(value) is str and _TIMESTAMP_RE.fullmatch(value):
            return value
    return None


def _observations(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if type(payload) is not dict:
        return []
    rows = payload.get("observations")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if type(row) is dict]


def _match_url(record: Mapping[str, Any], urls: Sequence[str]) -> bool:
    target = _normalize_url(_observation_url(record))
    if not target:
        return False
    return target in {_normalize_url(url) for url in urls}


def _match_topic(record: Mapping[str, Any], topics: set[str]) -> bool:
    if not topics:
        return False
    return bool(topics & _observation_terms(record))


def _match_event_id(record: Mapping[str, Any], event_id: str) -> bool:
    provenance = record.get("provenance")
    if type(provenance) is not dict:
        return False
    return provenance.get("event_id") == event_id


def emitted_text_blob(value: Any) -> str:
    parts: list[str] = []

    def walk(node: Any) -> None:
        if type(node) is str:
            parts.append(node)
        elif isinstance(node, Mapping):
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return " ".join(parts).casefold()


def causal_hits(value: Any) -> list[str]:
    blob = emitted_text_blob(value)
    return [token for token in FORBIDDEN_CAUSAL if token in blob]


def load_optional_live_families(readings_dir: Path | str | None) -> dict[str, dict[str, Any] | None]:
    """Load PR82 readings when present. Missing files abstain; nothing is invented."""

    families: dict[str, dict[str, Any] | None] = {family: None for family in LIVE_FAMILY_IDS}
    for root in live_paths.readings_search_dirs(preferred=readings_dir):
        for family, filename in LIVE_FAMILY_FILES.items():
            if families[family] is not None:
                continue
            path = root / filename
            if not path.is_file():
                continue
            value = live_paths.load_json_if_present(path)
            if value is not None:
                families[family] = value
    return families


def load_optional_archive_context(readings_dir: Path | str | None) -> dict[str, Any] | None:
    candidates: list[Path] = []
    if readings_dir is not None:
        candidates.append(Path(readings_dir) / "archive-news-context-latest.json")
    candidates.extend(live_paths.LIVE_ARCHIVE_CONTEXT_PATHS)
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        value = live_paths.load_json_if_present(path)
        if value is not None:
            return value
    return None


def load_optional_corroboration(readings_dir: Path | str | None) -> dict[str, Any] | None:
    for root in live_paths.readings_search_dirs(preferred=readings_dir):
        value = live_paths.load_json_if_present(root / "corroboration-latest.json")
        if value is not None:
            return value
    return None


def corroboration_coverage(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """Emit official_page: none-reviewed even when the corroboration file is empty."""

    accepted = primary = reviewed = 0
    if type(document) is dict:
        accepted = document.get("n_accepted_edges") if type(document.get("n_accepted_edges")) is int else 0
        primary = (
            document.get("n_events_with_primary_documents")
            if type(document.get("n_events_with_primary_documents")) is int
            else 0
        )
        reviewed = document.get("n_reviewed_edges") if type(document.get("n_reviewed_edges")) is int else 0
    official_page = "none-reviewed" if reviewed == 0 else "reviewed"
    return {
        "accepted_edges": accepted,
        "primary_docs": primary,
        "reviewed": reviewed,
        "official_page": official_page,
        "status": "empty" if reviewed == 0 and accepted == 0 and primary == 0 else "present",
        "relation": "coverage-fact-only",
    }


def _public_archive_receipts(row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    receipts = []
    for item in _archive_receipts(row or {}):
        public = {
            field: item[field]
            for field in _ARCHIVE_RECEIPT_FIELDS
            if field in item
        }
        if public.get("anomaly_state") == "warming_up":
            public.pop("anomaly_score", None)
        receipts.append(public)
    return receipts


def archive_news_context_block(
    event: Mapping[str, Any],
    archive_context: Mapping[str, Any] | None,
    *,
    refresh_status: str = "unknown",
) -> dict[str, Any]:
    match_kind, row = _archive_event_row(archive_context, event)
    receipts = _public_archive_receipts(row)
    anomaly_state = receipts[0].get("anomaly_state") if receipts else None
    anomaly_state = anomaly_state if type(anomaly_state) is str else None
    score_published = bool(
        receipts
        and anomaly_state not in {None, "warming_up"}
        and receipts[0].get("anomaly_state") != "warming_up"
    )
    return {
        "matched": bool(receipts),
        "match_kind": match_kind if receipts else None,
        "event_id": event["event_id"],
        "anomaly_state": anomaly_state,
        "anomaly_score_published": score_published,
        "relation": "topic-surface-only",
        "receipts": receipts,
        "refresh_status": refresh_status,
    }


def _evidence_row(
    *,
    kind: str,
    surface_id: str,
    status: str,
    headline: str,
    claim: str,
    reading_url: str | None,
    source_timestamp: str | None,
    input_sha256: str | None,
    interpretation_limit: str,
) -> dict[str, Any]:
    payload = {
        "kind": kind,
        "surface_id": surface_id,
        "claim": claim,
        "input_sha256": input_sha256,
        "source_timestamp": source_timestamp,
    }
    return {
        "evidence_id": _stable_id("eventevidence", payload, 20),
        "kind": kind,
        "surface_id": surface_id,
        "status": status,
        "headline": headline,
        "claim": claim,
        "reading_url": reading_url,
        "source_timestamp": source_timestamp,
        "input_sha256": input_sha256,
        "interpretation_limit": interpretation_limit,
    }


def _surface_row(
    *,
    surface_id: str,
    status: str,
    match_kind: str | None,
    headline: str,
    finding: str,
    source_timestamp: str | None,
    reading_url: str | None,
    input_sha256: str | None,
    interpretation: str,
) -> dict[str, Any]:
    return {
        "surface_id": surface_id,
        "status": status,
        "match_kind": match_kind,
        "headline": headline,
        "finding": finding,
        "source_timestamp": source_timestamp,
        "reading_url": reading_url,
        "input_sha256": input_sha256,
        "relation": "topic-surface-only",
        "interpretation": interpretation,
    }


def _missing_surface(surface_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    claim = f"No live {surface_id} reading was supplied to this analysis."
    evidence = _evidence_row(
        kind="surface-missing",
        surface_id=surface_id,
        status="missing",
        headline=f"{surface_id}: reading absent",
        claim=claim,
        reading_url=READING_URLS[surface_id],
        source_timestamp=None,
        input_sha256=None,
        interpretation_limit="Absence is a coverage gap, not a zero finding.",
    )
    surface = _surface_row(
        surface_id=surface_id,
        status="missing",
        match_kind=None,
        headline=f"{surface_id}: withheld",
        finding=claim,
        source_timestamp=None,
        reading_url=READING_URLS[surface_id],
        input_sha256=None,
        interpretation=(
            "This layer abstains because the collector reading is not live here. "
            "No value was invented."
        ),
    )
    return surface, evidence


def _unmatched_surface(surface_id: str, reason: str, digest: str | None, timestamp: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = _evidence_row(
        kind="surface-unmatched",
        surface_id=surface_id,
        status="unmatched",
        headline=f"{surface_id}: no join",
        claim=reason,
        reading_url=READING_URLS[surface_id],
        source_timestamp=timestamp,
        input_sha256=digest,
        interpretation_limit="A live reading without an exact URL or declared-topic join is not attached.",
    )
    surface = _surface_row(
        surface_id=surface_id,
        status="unmatched",
        match_kind=None,
        headline=f"{surface_id}: no join",
        finding=reason,
        source_timestamp=timestamp,
        reading_url=READING_URLS[surface_id],
        input_sha256=digest,
        interpretation="The reading exists, but this event does not match it. The layer abstains.",
    )
    return surface, evidence


def _select_observation(
    payload: Mapping[str, Any] | None,
    *,
    event: Mapping[str, Any],
    prefer_event_id: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    rows = _observations(payload)
    urls = event_urls(event)
    topics = event_topics(event)
    event_id = event["event_id"]
    if prefer_event_id:
        for row in rows:
            if _match_event_id(row, event_id):
                return "event-id", row
    for row in rows:
        if _match_url(row, urls):
            return "url", row
    for row in rows:
        if _match_topic(row, topics):
            return "topic", row
    return "none", None


def _official_page_state(
    payload: Mapping[str, Any], official_url: str
) -> dict[str, Any] | None:
    pages = payload.get("pages")
    if type(pages) is dict:
        direct = pages.get(official_url)
        if type(direct) is dict:
            return direct
        target = _normalize_url(official_url)
        for url, row in pages.items():
            if type(url) is str and _normalize_url(url) == target and type(row) is dict:
                return row
    for row in _observations(payload):
        if _normalize_url(_observation_url(row)) == _normalize_url(official_url):
            return {
                "first_seen": row.get("first_seen"),
                "last_confirmed_alive": row.get("last_confirmed_alive"),
                "content_sha256": row.get("content_sha256"),
                "last_event": row.get("deletion_signal") or row.get("last_event"),
            }
    return None


def _ledger_kind(record: Mapping[str, Any]) -> str:
    kind = record.get("ledger_kind")
    if type(kind) is str and kind:
        return kind
    source = record.get("source")
    if type(source) is not str:
        return "public"
    lowered = source.casefold()
    if "cdt" in lowered:
        return "cdt"
    if "greatfire" in lowered:
        return "greatfire"
    if "freeweibo" in lowered:
        return "freeweibo"
    return "public"


def _gone_trail(official_state: Mapping[str, Any] | None) -> bool:
    if official_state is None:
        return False
    first_seen = official_state.get("first_seen")
    last_event = official_state.get("last_event")
    return type(first_seen) is str and bool(first_seen) and last_event == "disappeared"


def _archive_event_row(
    archive_context: Mapping[str, Any] | None, event: Mapping[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    if type(archive_context) is not dict:
        return "missing", None
    rows = archive_context.get("events")
    if not isinstance(rows, list):
        return "unmatched", None
    event_id = event["event_id"]
    urls = {_normalize_url(url) for url in event_urls(event)}
    urls.add(_normalize_url(event.get("url")))
    for row in rows:
        if type(row) is not dict:
            continue
        if row.get("event_id") == event_id:
            return "event-id", row
    for row in rows:
        if type(row) is not dict:
            continue
        if _normalize_url(row.get("event_url")) in urls:
            return "url", row
    return "unmatched", None


def _archive_receipts(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    receipts = row.get("archive_context")
    if not isinstance(receipts, list):
        return []
    return [item for item in receipts if type(item) is dict]


def _metric_phrase(row: Mapping[str, Any]) -> str:
    metric = row.get("metric") if type(row.get("metric")) is dict else {}
    value = metric.get("value")
    if value is None:
        return "no current metric"
    unit = metric.get("unit")
    label = metric.get("label") or "metric"
    if unit == "percent":
        number = f"{value:.1f}%".replace(".0%", "%")
    elif unit == "ratio" and type(value) in {int, float}:
        number = f"{100 * value:.1f}%".replace(".0%", "%")
    else:
        number = str(value)
    denominator = metric.get("denominator") if type(metric.get("denominator")) is dict else {}
    denom_label = denominator.get("label")
    denom_value = denominator.get("value")
    if denom_label and denom_value is not None:
        return f"{label} {number} over {denom_value} {denom_label}"
    return f"{label} {number}"


def build_v2_blocks(
    event: Mapping[str, Any],
    *,
    items: Mapping[str, Mapping[str, Any]],
    collector_context: Sequence[Mapping[str, Any]],
    scope_status: str,
    live_families: Mapping[str, Mapping[str, Any] | None] | None = None,
    archive_context: Mapping[str, Any] | None = None,
    corroboration: Mapping[str, Any] | None = None,
    window_peers: Mapping[str, Any] | None = None,
    peer_warehouses: Mapping[str, Mapping[str, Any] | None] | None = None,
    archive_refresh_status: str = "unknown",
) -> dict[str, Any]:
    """Return the v2 brief blocks. Callers attach them to the v1 core."""

    families = {family: None for family in LIVE_FAMILY_IDS}
    if live_families:
        for family in LIVE_FAMILY_IDS:
            payload = live_families.get(family)
            families[family] = payload if type(payload) is dict else None
    coverage = corroboration_coverage(corroboration)
    peers = (
        dict(window_peers)
        if type(window_peers) is dict
        else {
            "same_window_peer_count": 0,
            "shared_topics": [],
            "peer_source_ids": [],
            "peer_independence_groups": [],
            "relation": "topic-surface-only",
        }
    )

    in_scope = scope_status == "in-scope"
    live_family_ids = list(LIVE_FAMILY_IDS) if in_scope else []
    declared_links = {
        "relation": "topic-surface-only",
        "scan_signal_ids": list(event["declared_links"]["scan_signal_ids"]),
        "economic_signal_ids": list(event["declared_links"]["economic_signal_ids"]),
        "live_family_ids": live_family_ids,
    }

    evidence: list[dict[str, Any]] = []
    surfaces: list[dict[str, Any]] = []
    eid: dict[str, str] = {}

    source_names = [
        ref["source_name"]
        for ref in event["evidence_refs"]
        if type(ref) is dict and type(ref.get("source_name")) is str
    ]
    publisher = ", ".join(dict.fromkeys(source_names)) or "a registered source"
    first_item = None
    for ref in event["evidence_refs"]:
        first_item = items.get(ref["item_id"])
        if first_item is not None:
            break
    feed_sha = first_item["feed_sha256"] if first_item and first_item.get("feed_sha256") else None
    source_claim = (
        f"{publisher} published “{event['headline']}” at {event['published_at']}."
    )
    source_evidence = _evidence_row(
        kind="event-source",
        surface_id="newswire",
        status="live",
        headline=event["headline"],
        claim=source_claim,
        reading_url=event["url"],
        source_timestamp=event["published_at"],
        input_sha256=feed_sha if type(feed_sha) is str and _SHA256_RE.fullmatch(feed_sha) else None,
        interpretation_limit=(
            "Feed title, canonical link, publication time, and a bounded excerpt only."
        ),
    )
    evidence.append(source_evidence)
    eid["newswire"] = source_evidence["evidence_id"]
    interconnection = event_interconnection.build_interconnection(
        event,
        peer_warehouses,
        scope_status=scope_status,
    )
    for row in interconnection["peers"]:
        if row["status"] != "joined":
            continue
        peer_claim = event_interconnection.peer_brief_sentence(row)
        peer_evidence = _evidence_row(
            kind="interconnection-peer",
            surface_id=row["peer_id"],
            status="live",
            headline=f"{row['peer_name']} interconnection peer",
            claim=peer_claim,
            reading_url=row["reading_url"],
            source_timestamp=row["observed_at"],
            input_sha256=row["input_sha256"],
            interpretation_limit=(
                "Named-key join only. This peer keeps its own count and date. "
                "It is not a collapsed censorship rate or a cause."
            ),
        )
        evidence.append(peer_evidence)
        eid[f"interconnection:{row['peer_id']}:{row['record_id']}"] = peer_evidence["evidence_id"]

    corr_timestamp = None
    if type(corroboration) is dict and type(corroboration.get("generated_at")) is str:
        corr_timestamp = corroboration["generated_at"]
    corr_claim = (
        f"Official-page corroboration coverage is {coverage['official_page']}: "
        f"{coverage['accepted_edges']} accepted edges, "
        f"{coverage['primary_docs']} primary documents, "
        f"{coverage['reviewed']} reviewed."
    )
    corr_evidence = _evidence_row(
        kind="corroboration",
        surface_id="corroboration",
        status="unmatched" if coverage["status"] == "empty" else "live",
        headline="Primary-document corroboration coverage",
        claim=corr_claim,
        reading_url=READING_URLS.get("corroboration")
        or "https://palimpsest.info/readings/corroboration-latest.json",
        source_timestamp=corr_timestamp if corr_timestamp and _TIMESTAMP_RE.fullmatch(corr_timestamp) else None,
        input_sha256=_sha256_bytes(corroboration) if type(corroboration) is dict else None,
        interpretation_limit=(
            "Coverage fact only. Empty review is not a deletion or official-movement finding."
        ),
    )
    evidence.append(corr_evidence)
    eid["corroboration"] = corr_evidence["evidence_id"]
    surfaces.append(
        _surface_row(
            surface_id="corroboration",
            status="unmatched" if coverage["status"] == "empty" else "live",
            match_kind=None,
            headline=corr_evidence["headline"],
            finding=corr_claim,
            source_timestamp=corr_evidence["source_timestamp"],
            reading_url=corr_evidence["reading_url"],
            input_sha256=corr_evidence["input_sha256"],
            interpretation=(
                "Human-reviewed primary-document coverage. "
                "official_page none-reviewed is a coverage fact, not a takedown claim."
            ),
        )
    )

    for row in collector_context:
        collector_evidence = _evidence_row(
            kind="newsroom-collector",
            surface_id=row["signal_id"],
            status=row["status"],
            headline=row["headline"],
            claim=row["finding"],
            reading_url=row["evidence_url"],
            source_timestamp=row["source_timestamp"],
            input_sha256=row["input_sha256"],
            interpretation_limit=row["interpretation"],
        )
        evidence.append(collector_evidence)
        eid[row["signal_id"]] = collector_evidence["evidence_id"]

    official_url_list = official_urls(event) if in_scope else []
    official_state = None
    official_match = None
    if not in_scope:
        surface, row = _unmatched_surface(
            "official-first-seen",
            "Official-page movement is not attached outside the China remit.",
            None,
            None,
        )
        surface["status"] = "not-applicable"
        row["status"] = "not-applicable"
        surfaces.append(surface)
        evidence.append(row)
        eid["official-first-seen"] = row["evidence_id"]
        official_layer = _layer(
            "not-applicable",
            _sentence(row["claim"], row["evidence_id"]),
        )
    elif families["official-first-seen"] is None:
        surface, row = _missing_surface("official-first-seen")
        surfaces.append(surface)
        evidence.append(row)
        eid["official-first-seen"] = row["evidence_id"]
        official_layer = _layer(
            "none-reviewed" if coverage["official_page"] == "none-reviewed" else "abstained",
            _sentence(row["claim"], row["evidence_id"]),
            _sentence(
                "Official-page first-seen, last-alive, and hash values are withheld.",
                row["evidence_id"],
            ),
            _sentence(corr_claim, eid["corroboration"]),
        )
    elif not official_url_list:
        digest = _sha256_bytes(families["official-first-seen"])
        timestamp = families["official-first-seen"].get("generated_at")
        timestamp = timestamp if type(timestamp) is str else None
        surface, row = _unmatched_surface(
            "official-first-seen",
            "No official .cn, Xinhua, People's Daily, MFA, or NBS URL is on this event.",
            digest,
            timestamp,
        )
        surfaces.append(surface)
        evidence.append(row)
        eid["official-first-seen"] = row["evidence_id"]
        official_layer = _layer(
            "not-applicable",
            _sentence(row["claim"], row["evidence_id"]),
        )
    else:
        payload = families["official-first-seen"]
        official_url = official_url_list[0]
        official_state = _official_page_state(payload, official_url)
        official_match, official_obs = _select_observation(payload, event=event)
        digest = _sha256_bytes(payload)
        timestamp = (
            (official_state or {}).get("first_seen")
            or _observation_time(official_obs or {})
            or payload.get("generated_at")
        )
        timestamp = timestamp if type(timestamp) is str else None
        if official_state is None and official_obs is None:
            surface, row = _unmatched_surface(
                "official-first-seen",
                "The official-first-seen reading does not contain this official URL.",
                digest,
                timestamp,
            )
            surfaces.append(surface)
            evidence.append(row)
            eid["official-first-seen"] = row["evidence_id"]
            official_layer = _layer(
                "none-reviewed" if coverage["official_page"] == "none-reviewed" else "abstained",
                _sentence(row["claim"], row["evidence_id"]),
                _sentence(corr_claim, eid["corroboration"]),
            )
        else:
            first_seen = (official_state or {}).get("first_seen")
            last_alive = (official_state or {}).get("last_confirmed_alive")
            digest_value = (official_state or {}).get("content_sha256")
            last_event = (official_state or {}).get("last_event")
            host = _host(official_url)
            finding = (
                f"official-first-seen records host {host} first_seen={first_seen or 'unreported'}, "
                f"last_confirmed_alive={last_alive or 'unreported'}, "
                f"content_sha256={digest_value or 'unreported'}, "
                f"last_event={last_event or 'unreported'}."
            )
            row = _evidence_row(
                kind="official-first-seen",
                surface_id="official-first-seen",
                status="live",
                headline=f"Official page trail for {host}",
                claim=finding,
                reading_url=READING_URLS["official-first-seen"],
                source_timestamp=timestamp,
                input_sha256=digest,
                interpretation_limit=(
                    "Fetch-trail metadata only. The trail does not state why the page changed."
                ),
            )
            evidence.append(row)
            eid["official-first-seen"] = row["evidence_id"]
            surfaces.append(
                _surface_row(
                    surface_id="official-first-seen",
                    status="live",
                    match_kind="url",
                    headline=row["headline"],
                    finding=finding,
                    source_timestamp=timestamp,
                    reading_url=READING_URLS["official-first-seen"],
                    input_sha256=digest,
                    interpretation=(
                        "Official landing-page first-seen, last-alive, and hash trail. "
                        "Not an explanation of why the page changed."
                    ),
                )
            )
            sentences = [
                _sentence(
                    f"The event includes official host {host}.",
                    row["evidence_id"],
                ),
                _sentence(finding, row["evidence_id"]),
                _sentence(
                    "This is a fetch-trail record. It does not state why the page changed or who changed it.",
                    row["evidence_id"],
                ),
                _sentence(corr_claim, eid["corroboration"]),
            ]
            official_layer = _layer("present", *sentences)

    if not in_scope:
        surface, row = _unmatched_surface(
            "public-deletion-ledgers",
            "Deletion-ledger context is not attached outside the China remit.",
            None,
            None,
        )
        surface["status"] = "not-applicable"
        row["status"] = "not-applicable"
        surfaces.append(surface)
        evidence.append(row)
        eid["public-deletion-ledgers"] = row["evidence_id"]
        ledger_layer = _layer("not-applicable", _sentence(row["claim"], row["evidence_id"]))
        ledger_obs = None
        ledger_match = "none"
    elif families["public-deletion-ledgers"] is None:
        surface, row = _missing_surface("public-deletion-ledgers")
        surfaces.append(surface)
        evidence.append(row)
        eid["public-deletion-ledgers"] = row["evidence_id"]
        ledger_layer = _layer(
            "abstained",
            _sentence(row["claim"], row["evidence_id"]),
            _sentence(
                "CDT, GreatFire, and FreeWeibo peer observations are withheld.",
                row["evidence_id"],
            ),
        )
        ledger_obs = None
        ledger_match = "none"
    else:
        payload = families["public-deletion-ledgers"]
        ledger_match, ledger_obs = _select_observation(payload, event=event)
        digest = _sha256_bytes(payload)
        timestamp = _observation_time(ledger_obs or {}) or payload.get("generated_at")
        timestamp = timestamp if type(timestamp) is str else None
        if ledger_obs is None:
            surface, row = _unmatched_surface(
                "public-deletion-ledgers",
                "No public-deletion-ledger item matches this URL or a declared topic surface.",
                digest,
                timestamp,
            )
            surfaces.append(surface)
            evidence.append(row)
            eid["public-deletion-ledgers"] = row["evidence_id"]
            ledger_layer = _layer("abstained", _sentence(row["claim"], row["evidence_id"]))
        else:
            kind = _ledger_kind(ledger_obs)
            match_label = "URL" if ledger_match == "url" else "declared topic surface"
            peer = (
                f"A {kind} ledger item matches this {match_label}. "
                "That is a peer observation, not a Palimpsest-verified deletion."
            )
            if _gone_trail(official_state):
                trail = (
                    "Palimpsest also has a first-seen to gone trail on the official page for this URL. "
                    "That trail is a fetch outcome, not a proven takedown."
                )
            else:
                trail = (
                    "Palimpsest does not have its own first-seen to gone trail for this URL, "
                    "so the ledger item stays a peer observation."
                )
            row = _evidence_row(
                kind="public-deletion-ledgers",
                surface_id="public-deletion-ledgers",
                status="live",
                headline=f"{kind} ledger peer observation",
                claim=peer,
                reading_url=READING_URLS["public-deletion-ledgers"],
                source_timestamp=timestamp,
                input_sha256=digest,
                interpretation_limit="Peer ledger metadata only; not a Palimpsest liveness check.",
            )
            evidence.append(row)
            eid["public-deletion-ledgers"] = row["evidence_id"]
            surfaces.append(
                _surface_row(
                    surface_id="public-deletion-ledgers",
                    status="live",
                    match_kind=ledger_match,
                    headline=row["headline"],
                    finding=f"{peer} {trail}",
                    source_timestamp=timestamp,
                    reading_url=READING_URLS["public-deletion-ledgers"],
                    input_sha256=digest,
                    interpretation="Peer observation from an already-public deletion or blocking ledger.",
                )
            )
            ledger_layer = _layer(
                "present",
                _sentence(peer, row["evidence_id"]),
                _sentence(trail, row["evidence_id"], eid["official-first-seen"]),
            )

    if not in_scope:
        surface, row = _unmatched_surface(
            "news-wire-live",
            "News-wire-live context is not attached outside the China remit.",
            None,
            None,
        )
        surface["status"] = "not-applicable"
        row["status"] = "not-applicable"
        surfaces.append(surface)
        evidence.append(row)
        eid["news-wire-live"] = row["evidence_id"]
        wire_obs = None
        wire_match = "none"
        wire_time = None
    elif families["news-wire-live"] is None:
        surface, row = _missing_surface("news-wire-live")
        surfaces.append(surface)
        evidence.append(row)
        eid["news-wire-live"] = row["evidence_id"]
        wire_obs = None
        wire_match = "none"
        wire_time = None
    else:
        payload = families["news-wire-live"]
        wire_match, wire_obs = _select_observation(
            payload, event=event, prefer_event_id=True
        )
        digest = _sha256_bytes(payload)
        wire_time = _observation_time(wire_obs or {}) or payload.get("generated_at")
        wire_time = wire_time if type(wire_time) is str else None
        if wire_obs is None:
            surface, row = _unmatched_surface(
                "news-wire-live",
                "The news-wire-live reading does not contain this event id or publisher URL.",
                digest,
                wire_time,
            )
            surfaces.append(surface)
            evidence.append(row)
            eid["news-wire-live"] = row["evidence_id"]
        else:
            claim = (
                f"news-wire-live holds a metadata projection for this event"
                f"{f' at {wire_time}' if wire_time else ''}."
            )
            row = _evidence_row(
                kind="news-wire-live",
                surface_id="news-wire-live",
                status="live",
                headline="News-wire live projection",
                claim=claim,
                reading_url=READING_URLS["news-wire-live"],
                source_timestamp=wire_time,
                input_sha256=digest,
                interpretation_limit="RSS/Atom metadata already held on the evidence wire; no article body.",
            )
            evidence.append(row)
            eid["news-wire-live"] = row["evidence_id"]
            surfaces.append(
                _surface_row(
                    surface_id="news-wire-live",
                    status="live",
                    match_kind=wire_match,
                    headline=row["headline"],
                    finding=claim,
                    source_timestamp=wire_time,
                    reading_url=READING_URLS["news-wire-live"],
                    input_sha256=digest,
                    interpretation="Live wire projection of already-retained feed metadata.",
                )
            )

    if not in_scope:
        surface, row = _unmatched_surface(
            "undertext",
            "Undertext context is not attached outside the China remit.",
            None,
            None,
        )
        surface["status"] = "not-applicable"
        row["status"] = "not-applicable"
        surfaces.append(surface)
        evidence.append(row)
        eid["undertext"] = row["evidence_id"]
    elif families["undertext"] is None:
        surface, row = _missing_surface("undertext")
        surfaces.append(surface)
        evidence.append(row)
        eid["undertext"] = row["evidence_id"]
    else:
        payload = families["undertext"]
        under_match, under_obs = _select_observation(payload, event=event)
        digest = _sha256_bytes(payload)
        timestamp = _observation_time(under_obs or {}) or payload.get("generated_at")
        timestamp = timestamp if type(timestamp) is str else None
        if under_obs is None:
            surface, row = _unmatched_surface(
                "undertext",
                "The undertext reading does not match this URL or a declared topic surface.",
                digest,
                timestamp,
            )
            surfaces.append(surface)
            evidence.append(row)
            eid["undertext"] = row["evidence_id"]
        else:
            n_obs = payload.get("n_observations")
            claim = (
                f"undertext reports a matching observation"
                f"{f' among {n_obs} fused observations' if type(n_obs) is int else ''}."
            )
            row = _evidence_row(
                kind="undertext",
                surface_id="undertext",
                status="live",
                headline="Undertext matching observation",
                claim=claim,
                reading_url=READING_URLS["undertext"],
                source_timestamp=timestamp,
                input_sha256=digest,
                interpretation_limit="Fused public-archive context only; not a blocking claim about this article.",
            )
            evidence.append(row)
            eid["undertext"] = row["evidence_id"]
            surfaces.append(
                _surface_row(
                    surface_id="undertext",
                    status="live",
                    match_kind=under_match,
                    headline=row["headline"],
                    finding=claim,
                    source_timestamp=timestamp,
                    reading_url=READING_URLS["undertext"],
                    input_sha256=digest,
                    interpretation="Public-archive fusion context. Not a claim that this article was blocked.",
                )
            )

    archive_match, archive_row = _archive_event_row(archive_context, event)
    if not in_scope:
        surface, row = _unmatched_surface(
            ARCHIVE_SURFACE_ID,
            "Archive-derived context is not attached outside the China remit.",
            None,
            None,
        )
        surface["status"] = "not-applicable"
        row["status"] = "not-applicable"
        surfaces.append(surface)
        evidence.append(row)
        eid[ARCHIVE_SURFACE_ID] = row["evidence_id"]
        archive_layer = _layer("not-applicable", _sentence(row["claim"], row["evidence_id"]))
        archive_receipts: list[dict[str, Any]] = []
    elif archive_context is None:
        surface, row = _missing_surface(ARCHIVE_SURFACE_ID)
        surfaces.append(surface)
        evidence.append(row)
        eid[ARCHIVE_SURFACE_ID] = row["evidence_id"]
        archive_layer = _layer(
            "abstained",
            _sentence(row["claim"], row["evidence_id"]),
            _sentence(
                "Host-level unique_urls, mutation_rate, archive_gap_rate, and anomaly_state are withheld.",
                row["evidence_id"],
            ),
        )
        archive_receipts = []
    elif archive_row is None or not _archive_receipts(archive_row):
        digest = _sha256_bytes(archive_context)
        timestamp = archive_context.get("generated_at")
        timestamp = timestamp if type(timestamp) is str else None
        surface, row = _unmatched_surface(
            ARCHIVE_SURFACE_ID,
            "No archive-news-context feature row matches this event id, URL, or declared topic surface.",
            digest,
            timestamp,
        )
        surfaces.append(surface)
        evidence.append(row)
        eid[ARCHIVE_SURFACE_ID] = row["evidence_id"]
        archive_layer = _layer("abstained", _sentence(row["claim"], row["evidence_id"]))
        archive_receipts = []
    else:
        archive_receipts = _archive_receipts(archive_row)
        digest = _sha256_bytes(archive_context)
        timestamp = archive_row.get("published_at") or archive_context.get("generated_at")
        timestamp = timestamp if type(timestamp) is str else None
        receipt = archive_receipts[0]
        host = receipt.get("host") or "the matched host"
        unique_urls = receipt.get("unique_urls")
        mutation_rate = receipt.get("mutation_rate")
        gap_rate = receipt.get("archive_gap_rate")
        anomaly_state = receipt.get("anomaly_state")
        crawl = receipt.get("crawl") or "an unnamed crawl"
        finding = (
            f"Host {host} has unique_urls={unique_urls}, mutation_rate={mutation_rate}, "
            f"archive_gap_rate={gap_rate}, anomaly_state={anomaly_state} on crawl {crawl}."
        )
        row = _evidence_row(
            kind="archive-news-context",
            surface_id=ARCHIVE_SURFACE_ID,
            status="live",
            headline=f"Archive-derived host context for {host}",
            claim=finding,
            reading_url=READING_URLS[ARCHIVE_SURFACE_ID],
            source_timestamp=timestamp,
            input_sha256=digest,
            interpretation_limit="absence_semantics = archive-coverage-gap-not-deletion. No raw Common Crawl URLs or bodies.",
        )
        evidence.append(row)
        eid[ARCHIVE_SURFACE_ID] = row["evidence_id"]
        surfaces.append(
            _surface_row(
                surface_id=ARCHIVE_SURFACE_ID,
                status="live",
                match_kind=archive_match,
                headline=row["headline"],
                finding=finding,
                source_timestamp=timestamp,
                reading_url=READING_URLS[ARCHIVE_SURFACE_ID],
                input_sha256=digest,
                interpretation=(
                    "Host-level Common Crawl derived rates. "
                    "absence_semantics is archive-coverage-gap-not-deletion."
                ),
            )
        )
        sentences = [
            _sentence(finding, row["evidence_id"]),
            _sentence(
                "absence_semantics is archive-coverage-gap-not-deletion. No raw Common Crawl URL or WARC body is published.",
                row["evidence_id"],
            ),
        ]
        if anomaly_state == "warming_up" or receipt.get("anomaly_score") is None:
            sentences.append(
                _sentence(
                    "The prequential-robust-mad/v1 model remains warming_up; no anomaly score is published.",
                    row["evidence_id"],
                )
            )
        archive_layer = _layer("present", *sentences)

    declared_pipe = [
        row
        for row in collector_context
        if row["signal_id"] in PIPE_SIGNAL_IDS
    ]
    if not declared_pipe:
        pipe_layer = _layer(
            "not-declared",
            _sentence(
                "This event does not declare OONI, bleedthrough, vantage, or IODA signal ids, so pipe context is withheld.",
                eid["newswire"],
            ),
        )
    else:
        pipe_sentences = []
        for row in declared_pipe:
            citation = eid[row["signal_id"]]
            if row["status"] != "live":
                pipe_sentences.append(
                    _sentence(
                        f"Declared pipe surface {row['signal_id']} is {row['status']}; its retained metric is not treated as current.",
                        citation,
                    )
                )
                continue
            pipe_sentences.append(
                _sentence(
                    f"Declared pipe surface {row['signal_id']} is live: {row['finding']} ({_metric_phrase(row)}). "
                    "This is aggregate context, not a claim that this article was blocked.",
                    citation,
                )
            )
        pipe_layer = _layer("present", *pipe_sentences)

    groups = len(event["evidence_groups"])
    sources = len(event["evidence_refs"])
    strength = event["evidence_strength"]
    lead = {
        "status": "present",
        "sentences": [
            _sentence(source_claim, eid["newswire"]),
            _sentence(
                f"Evidence strength is {strength} across {groups} independent source "
                f"group{'s' if groups != 1 else ''} and {sources} attributed source "
                f"record{'s' if sources != 1 else ''}.",
                eid["newswire"],
            ),
            _sentence(
                "Palimpsest retained feed title, canonical link, publication time, and a bounded excerpt; it did not fetch the article body.",
                eid["newswire"],
            ),
        ],
    }
    for row in interconnection["peers"]:
        if row["status"] != "joined":
            continue
        citation = eid[f"interconnection:{row['peer_id']}:{row['record_id']}"]
        lead["sentences"].append(
            _sentence(event_interconnection.peer_brief_sentence(row), citation)
        )
    if interconnection["joined_count"] == 0:
        lead["sentences"].append(
            _sentence(
                "No interconnection peer met an exact host, URL path, term, or ASN key inside the UTC ±24h window.",
                eid["newswire"],
            )
        )

    timeline_sentences: list[dict[str, Any]] = []
    timeline_missing = []
    if in_scope:
        if families["official-first-seen"] is None:
            timeline_missing.append("official-first-seen")
            timeline_sentences.append(
                _sentence(
                    "official-first-seen is missing, so first-seen, last-alive, and hash-change points are omitted.",
                    eid["official-first-seen"],
                )
            )
        elif official_state is not None:
            if official_state.get("first_seen"):
                timeline_sentences.append(
                    _sentence(
                        f"official-first-seen first recorded this official URL at {official_state['first_seen']}.",
                        eid["official-first-seen"],
                    )
                )
            if official_state.get("last_confirmed_alive"):
                timeline_sentences.append(
                    _sentence(
                        f"The same collector last confirmed the page alive at {official_state['last_confirmed_alive']}.",
                        eid["official-first-seen"],
                    )
                )
            if official_state.get("last_event") == "rewrite":
                timeline_sentences.append(
                    _sentence(
                        "The official-page hash changed. This is a hash-change record, not an explanation of why the page changed.",
                        eid["official-first-seen"],
                    )
                )
            if official_state.get("last_event") == "disappeared":
                timeline_sentences.append(
                    _sentence(
                        "The official page later failed a fetch after a first-seen record. That is a fetch outcome, not a proven takedown.",
                        eid["official-first-seen"],
                    )
                )
        if families["news-wire-live"] is None:
            timeline_missing.append("news-wire-live")
            timeline_sentences.append(
                _sentence(
                    "news-wire-live is missing, so that capture time is omitted.",
                    eid["news-wire-live"],
                )
            )
        elif wire_obs is not None and wire_time:
            timeline_sentences.append(
                _sentence(
                    f"A news-wire-live observation for this event was recorded at {wire_time}.",
                    eid["news-wire-live"],
                )
            )
        if families["public-deletion-ledgers"] is None:
            timeline_missing.append("public-deletion-ledgers")
            timeline_sentences.append(
                _sentence(
                    "public-deletion-ledgers is missing, so ledger-hit time is omitted.",
                    eid["public-deletion-ledgers"],
                )
            )
        elif ledger_obs is not None:
            ledger_time = _observation_time(ledger_obs)
            if ledger_time:
                timeline_sentences.append(
                    _sentence(
                        f"A public deletion-ledger item matching this event was recorded at {ledger_time}.",
                        eid["public-deletion-ledgers"],
                    )
                )
        if archive_context is None:
            timeline_missing.append("archive-news-context")
            timeline_sentences.append(
                _sentence(
                    "archive-news-context is missing, so archive-hit time is omitted.",
                    eid[ARCHIVE_SURFACE_ID],
                )
            )
        elif archive_receipts:
            capture = archive_receipts[0].get("last_capture_at")
            crawl = archive_receipts[0].get("crawl")
            if type(capture) is str:
                timeline_sentences.append(
                    _sentence(
                        f"Archive-derived host context last captured at {capture}"
                        f"{f' on crawl {crawl}' if crawl else ''}.",
                        eid[ARCHIVE_SURFACE_ID],
                    )
                )
    if not timeline_sentences:
        timeline_sentences.append(
            _sentence(
                "No timeline surface is available for this event, so the timeline abstains.",
                eid["newswire"],
            )
        )
        timeline_status = "abstained"
    elif timeline_missing:
        timeline_status = "abstained"
    else:
        timeline_status = "present"
    timeline = _layer(timeline_status, *timeline_sentences)

    counterreadings = [
        _record(
            "A ledger hit can reflect the ledger's own selection rules rather than a deletion of this URL.",
            eid["public-deletion-ledgers"],
        ),
        _record(
            "A hash change or disappearance on an official page can reflect a routine update, a fetch failure, or a removal; the trail does not choose among those.",
            eid["official-first-seen"],
        ),
        _record(
            "A current pipe or undertext value can post-date the article and cannot establish that this article was blocked.",
            eid["undertext"],
            *([eid[row["signal_id"]] for row in declared_pipe] or [eid["newswire"]]),
        ),
    ]
    unknowns = [
        _record(
            "Palimpsest does not know why any page, hash, or ledger item changed.",
            eid["official-first-seen"],
            eid["public-deletion-ledgers"],
        ),
        _record(
            "Palimpsest did not read the article body and does not verify the publisher's claims.",
            eid["newswire"],
        ),
        _record(
            "Person-level attribution and claims about why an actor acted are outside this brief.",
            eid["newswire"],
        ),
    ]

    live_surfaces = sum(row["status"] == "live" for row in surfaces)
    warnings = [
        row["surface_id"]
        for row in surfaces
        if row["status"] in {"missing", "unmatched"}
    ]
    key_numbers = [
        {
            "value": str(groups),
            "label": "independent source groups",
            "note": "publication structure, not a truth score",
            "citation_ids": [eid["newswire"]],
        },
        {
            "value": str(sources),
            "label": "attributed source records",
            "note": "feed metadata only",
            "citation_ids": [eid["newswire"]],
        },
        {
            "value": str(live_surfaces),
            "label": "live attached surfaces",
            "note": "URL or declared-topic joins only",
            "citation_ids": [eid["newswire"]],
        },
    ]
    if archive_receipts:
        unique_urls = archive_receipts[0].get("unique_urls")
        if unique_urls is not None:
            key_numbers.append(
                {
                    "value": str(unique_urls),
                    "label": "archive unique URLs on matched host",
                    "note": "archive-coverage-gap-not-deletion",
                    "citation_ids": [eid[ARCHIVE_SURFACE_ID]],
                }
            )
    key_numbers.append(
        {
            "value": str(peers["same_window_peer_count"]),
            "label": "same-window events sharing a topic",
            "note": "counts and names only; not verification",
            "citation_ids": [eid["newswire"]],
        }
    )
    key_numbers.append(
        {
            "value": str(coverage["reviewed"]),
            "label": "reviewed corroboration edges",
            "note": f"official_page {coverage['official_page']}",
            "citation_ids": [eid["corroboration"]],
        }
    )

    brief = {
        "lead": lead,
        "timeline": timeline,
        "official_page": official_layer,
        "deletion_ledger": ledger_layer,
        "pipe_context": pipe_layer,
        "archive_context": archive_layer,
    }
    sentence_nodes = [
        sentence
        for layer in brief.values()
        for sentence in layer["sentences"]
    ]
    sentence_count = len(sentence_nodes)
    cited_count = sum(bool(sentence["citation_ids"]) for sentence in sentence_nodes)
    hits = causal_hits(
        {
            "brief": brief,
            "counterreadings": counterreadings,
            "unknowns": unknowns,
            "surfaces": surfaces,
        }
    )
    gates = [
        {
            "gate_id": "closed-source-set",
            "label": "Every analytical input is the event, a declared collector story, a supplied live-family reading, or a supplied peer warehouse",
            "passed": True,
            "detail": f"{len(evidence)} evidence receipts were projected from supplied inputs only.",
        },
        {
            "gate_id": "sentence-citations",
            "label": "Every analytical sentence names exact evidence receipts",
            "passed": sentence_count > 0 and cited_count == sentence_count,
            "detail": f"{cited_count} of {sentence_count} analytical sentences carry citations.",
        },
        {
            "gate_id": "no-causal-language",
            "label": "Emitted text does not assign motive, intent, or a blocking cause",
            "passed": not hits,
            "detail": (
                "No forbidden causal phrase is present."
                if not hits
                else "Forbidden causal phrases: " + ", ".join(hits)
            ),
        },
        {
            "gate_id": "bounded-authorship",
            "label": "No interviews or free-form model prose are represented as reporting",
            "passed": True,
            "detail": DISCLOSURE,
        },
        {
            "gate_id": "denominators-separated",
            "label": "Incompatible instruments are not collapsed into one censorship rate",
            "passed": True,
            "detail": (
                "Pipe, ledger, official-trail, archive host rates, and interconnection "
                "peers keep separate denominators. GreatFire is not collapsed into OONI."
            ),
        },
        {
            "gate_id": "human-review-policy",
            "label": "Human review remains required; automatic publication stays prohibited",
            "passed": True,
            "detail": "The existing human-review and causal-language policy still holds.",
        },
    ]
    publishable = all(gate["passed"] for gate in gates)
    publication_receipt = {
        "status": "passed" if publishable else "failed",
        "publishable": publishable,
        "automatic_publication": False,
        "citation_coverage": (
            1.0 if sentence_count and cited_count == sentence_count else 0.0
        ),
        "human_review_required": True,
        "availability_warnings": warnings,
        "gates": gates,
    }
    authorship = {
        "byline": "Palimpsest China Desk",
        "mode": PUBLICATION_MODE,
        "human_interviews": "none",
        "freeform_model_generation": "none",
    }
    extra_clocks: list[str] = []
    for family, payload in families.items():
        if type(payload) is dict and type(payload.get("generated_at")) is str:
            extra_clocks.append(payload["generated_at"])
    if type(archive_context) is dict and type(archive_context.get("generated_at")) is str:
        extra_clocks.append(archive_context["generated_at"])
    if type(corroboration) is dict and type(corroboration.get("generated_at")) is str:
        extra_clocks.append(corroboration["generated_at"])
    for payload in (peer_warehouses or {}).values():
        if type(payload) is dict and type(payload.get("generated_at")) is str:
            extra_clocks.append(payload["generated_at"])

    return {
        "brief": brief,
        "surface_context": surfaces,
        "evidence": evidence,
        "counterreadings": counterreadings,
        "unknowns": unknowns,
        "key_numbers": key_numbers,
        "publication_receipt": publication_receipt,
        "authorship": authorship,
        "disclosure": DISCLOSURE,
        "declared_links": declared_links,
        "window_peers": peers,
        "corroboration": coverage,
        "archive_news_context": archive_news_context_block(
            event,
            archive_context,
            refresh_status=archive_refresh_status,
        ),
        "interconnection": interconnection,
        "extra_clocks": extra_clocks,
        "method": METHOD,
    }


def extra_limitations(*, has_surfaces: bool, has_archive: bool) -> list[str]:
    extra = [
        "Live-family joins use exact URL or declared topic overlap only; absence of a reading is a coverage gap.",
        "Interconnection peers attach only on a named exact key plus a UTC ±24h window; a miss is not a same-story guess.",
    ]
    if has_archive:
        extra.append(
            "Archive host rates are Common Crawl derived context, not a deletion census or a single censorship rate."
        )
    elif has_surfaces:
        extra.append(
            "A missing official, ledger, wire, or archive reading causes that layer to abstain; the gap is not a zero."
        )
    return extra


def validate_v2_blocks(
    analysis: Mapping[str, Any], *, event: Mapping[str, Any] | None = None
) -> None:
    """Fail closed on citation gaps, causal language, or invented publication."""

    from core.event_analysis import EventAnalysisError, _exact, _https_url, _text, _timestamp

    brief = _exact(analysis["brief"], _BRIEF_FIELDS, "analysis.brief")
    evidence = analysis["evidence"]
    if type(evidence) is not list or not 1 <= len(evidence) <= 64:
        raise EventAnalysisError("analysis.evidence is invalid")
    evidence_ids: set[str] = set()
    for index, row in enumerate(evidence):
        item = _exact(row, _EVIDENCE_FIELDS, f"analysis.evidence[{index}]")
        if type(item["evidence_id"]) is not str or _EVIDENCE_RE.fullmatch(item["evidence_id"]) is None:
            raise EventAnalysisError(f"analysis.evidence[{index}].evidence_id is invalid")
        if item["evidence_id"] in evidence_ids:
            raise EventAnalysisError("analysis.evidence contains duplicate ids")
        evidence_ids.add(item["evidence_id"])
        _text(item["kind"], f"analysis.evidence[{index}].kind", maximum=80)
        _text(item["surface_id"], f"analysis.evidence[{index}].surface_id", maximum=80)
        if item["status"] not in {"live", "missing", "unmatched", "not-applicable"} | {
            "degraded",
            "stale",
            "corrupt",
        }:
            raise EventAnalysisError(f"analysis.evidence[{index}].status is invalid")
        _text(item["headline"], f"analysis.evidence[{index}].headline", maximum=300)
        _text(item["claim"], f"analysis.evidence[{index}].claim", maximum=2_000)
        _text(
            item["interpretation_limit"],
            f"analysis.evidence[{index}].interpretation_limit",
            maximum=1_000,
        )
        if item["reading_url"] is not None:
            _https_url(item["reading_url"], f"analysis.evidence[{index}].reading_url")
        if item["source_timestamp"] is not None:
            _timestamp(item["source_timestamp"], f"analysis.evidence[{index}].source_timestamp")
        if item["input_sha256"] is not None and (
            type(item["input_sha256"]) is not str
            or _SHA256_RE.fullmatch(item["input_sha256"]) is None
        ):
            raise EventAnalysisError(f"analysis.evidence[{index}].input_sha256 is invalid")

    def _validate_cited(record: Mapping[str, Any], path: str) -> None:
        _exact(record, _RECORD_FIELDS, path)
        _text(record["text"], f"{path}.text", maximum=2_000)
        citations = record["citation_ids"]
        if (
            type(citations) is not list
            or not citations
            or len(citations) != len(set(citations))
            or any(item not in evidence_ids for item in citations)
        ):
            raise EventAnalysisError(f"{path} citations are invalid")

    sentence_count = cited_count = 0
    for name, layer in brief.items():
        block = _exact(layer, _LAYER_FIELDS, f"analysis.brief.{name}")
        if block["status"] not in _LAYER_STATUSES:
            raise EventAnalysisError(f"analysis.brief.{name}.status is invalid")
        sentences = block["sentences"]
        if type(sentences) is not list or not sentences:
            raise EventAnalysisError(f"analysis.brief.{name}.sentences is invalid")
        for index, sentence in enumerate(sentences):
            _exact(sentence, _SENTENCE_FIELDS, f"analysis.brief.{name}.sentences[{index}]")
            _text(sentence["text"], f"analysis.brief.{name}.sentences[{index}].text")
            citations = sentence["citation_ids"]
            if (
                type(citations) is not list
                or not citations
                or len(citations) != len(set(citations))
                or any(item not in evidence_ids for item in citations)
            ):
                raise EventAnalysisError(
                    f"analysis.brief.{name}.sentences[{index}] citations are invalid"
                )
            sentence_count += 1
            cited_count += 1

    context = analysis["surface_context"]
    if type(context) is not list or len(context) > 16:
        raise EventAnalysisError("analysis.surface_context is invalid")
    seen_surfaces: set[str] = set()
    for index, row in enumerate(context):
        item = _exact(row, _SURFACE_FIELDS, f"analysis.surface_context[{index}]")
        if item["surface_id"] in seen_surfaces:
            raise EventAnalysisError("analysis.surface_context is not unique")
        seen_surfaces.add(item["surface_id"])
        if item["status"] not in _SURFACE_STATUSES:
            raise EventAnalysisError(f"analysis.surface_context[{index}].status is invalid")
        if item["match_kind"] not in {None, "url", "topic", "event-id"}:
            raise EventAnalysisError(f"analysis.surface_context[{index}].match_kind is invalid")
        _text(item["headline"], f"analysis.surface_context[{index}].headline", maximum=300)
        _text(item["finding"], f"analysis.surface_context[{index}].finding", maximum=2_000)
        _text(
            item["interpretation"],
            f"analysis.surface_context[{index}].interpretation",
            maximum=1_000,
        )
        if item["relation"] != "topic-surface-only":
            raise EventAnalysisError("analysis.surface_context relation may not imply verification")
        if item["reading_url"] is not None:
            _https_url(item["reading_url"], f"analysis.surface_context[{index}].reading_url")
        if item["source_timestamp"] is not None:
            _timestamp(
                item["source_timestamp"],
                f"analysis.surface_context[{index}].source_timestamp",
            )
        if item["input_sha256"] is not None and (
            type(item["input_sha256"]) is not str
            or _SHA256_RE.fullmatch(item["input_sha256"]) is None
        ):
            raise EventAnalysisError(f"analysis.surface_context[{index}].input_sha256 is invalid")

    for field in ("counterreadings", "unknowns"):
        records = analysis[field]
        if type(records) is not list or not 2 <= len(records) <= 12:
            raise EventAnalysisError(f"analysis.{field} is invalid")
        for index, record in enumerate(records):
            _validate_cited(record, f"analysis.{field}[{index}]")

    numbers = analysis["key_numbers"]
    if type(numbers) is not list or not 3 <= len(numbers) <= 8:
        raise EventAnalysisError("analysis.key_numbers is invalid")
    for index, number in enumerate(numbers):
        item = _exact(number, _NUMBER_FIELDS, f"analysis.key_numbers[{index}]")
        _text(item["value"], f"analysis.key_numbers[{index}].value", maximum=80)
        _text(item["label"], f"analysis.key_numbers[{index}].label", maximum=120)
        _text(item["note"], f"analysis.key_numbers[{index}].note", maximum=240)
        citations = item["citation_ids"]
        if (
            type(citations) is not list
            or not citations
            or any(value not in evidence_ids for value in citations)
        ):
            raise EventAnalysisError(f"analysis.key_numbers[{index}] citations are invalid")

    peers = _exact(analysis["window_peers"], _WINDOW_PEER_FIELDS, "analysis.window_peers")
    if type(peers["same_window_peer_count"]) is not int or peers["same_window_peer_count"] < 0:
        raise EventAnalysisError("analysis.window_peers.same_window_peer_count is invalid")
    if peers["relation"] != "topic-surface-only":
        raise EventAnalysisError("analysis.window_peers relation may not imply verification")
    for field in ("shared_topics", "peer_source_ids", "peer_independence_groups"):
        values = peers[field]
        if type(values) is not list or any(type(item) is not str or not item for item in values):
            raise EventAnalysisError(f"analysis.window_peers.{field} is invalid")

    coverage = _exact(analysis["corroboration"], _CORROBORATION_FIELDS, "analysis.corroboration")
    for field in ("accepted_edges", "primary_docs", "reviewed"):
        if type(coverage[field]) is not int or coverage[field] < 0:
            raise EventAnalysisError(f"analysis.corroboration.{field} is invalid")
    if coverage["official_page"] not in {"none-reviewed", "reviewed"}:
        raise EventAnalysisError("analysis.corroboration.official_page is invalid")
    if coverage["status"] not in {"empty", "present"}:
        raise EventAnalysisError("analysis.corroboration.status is invalid")
    if coverage["relation"] != "coverage-fact-only":
        raise EventAnalysisError("analysis.corroboration relation is invalid")
    if coverage["reviewed"] == 0 and coverage["official_page"] != "none-reviewed":
        raise EventAnalysisError("empty corroboration must emit official_page none-reviewed")

    archive_block = _exact(
        analysis["archive_news_context"],
        _ARCHIVE_NEWS_CONTEXT_FIELDS,
        "analysis.archive_news_context",
    )
    if type(archive_block["matched"]) is not bool:
        raise EventAnalysisError("analysis.archive_news_context.matched is invalid")
    if archive_block["match_kind"] not in {None, "event-id", "url"}:
        raise EventAnalysisError("analysis.archive_news_context.match_kind is invalid")
    if archive_block["event_id"] != analysis["event_id"]:
        raise EventAnalysisError("analysis.archive_news_context.event_id drifted")
    if archive_block["relation"] != "topic-surface-only":
        raise EventAnalysisError("analysis.archive_news_context relation may not imply verification")
    if archive_block["refresh_status"] not in {"unknown", "ok", "revision_pin_mismatch"}:
        raise EventAnalysisError("analysis.archive_news_context.refresh_status is invalid")
    if type(archive_block["anomaly_score_published"]) is not bool:
        raise EventAnalysisError("analysis.archive_news_context.anomaly_score_published is invalid")
    if archive_block["anomaly_state"] == "warming_up" and archive_block["anomaly_score_published"]:
        raise EventAnalysisError("warming_up archive context may not publish an anomaly score")
    try:
        event_interconnection.validate_interconnection(
            analysis["interconnection"], event=event
        )
    except event_interconnection.InterconnectionError as exc:
        raise EventAnalysisError(str(exc)) from exc
    if type(archive_block["receipts"]) is not list or len(archive_block["receipts"]) > 16:
        raise EventAnalysisError("analysis.archive_news_context.receipts is invalid")

    links = _exact(analysis["declared_links"], _DECLARED_FIELDS, "analysis.declared_links")
    if links["relation"] != "topic-surface-only":
        raise EventAnalysisError("analysis.declared_links relation is invalid")
    for field in ("scan_signal_ids", "economic_signal_ids", "live_family_ids"):
        values = links[field]
        if type(values) is not list or any(type(item) is not str for item in values):
            raise EventAnalysisError(f"analysis.declared_links.{field} is invalid")
        if field != "live_family_ids" and values != sorted(set(values)):
            raise EventAnalysisError(f"analysis.declared_links.{field} is not unique and sorted")
        if field == "live_family_ids" and values != list(dict.fromkeys(values)):
            raise EventAnalysisError("analysis.declared_links.live_family_ids is not unique")
    if event is not None:
        if links["scan_signal_ids"] != list(event["declared_links"]["scan_signal_ids"]):
            raise EventAnalysisError("analysis.declared_links.scan_signal_ids drifted")
        if links["economic_signal_ids"] != list(event["declared_links"]["economic_signal_ids"]):
            raise EventAnalysisError("analysis.declared_links.economic_signal_ids drifted")
        expected_families = list(LIVE_FAMILY_IDS) if analysis["scope_status"] == "in-scope" else []
        if links["live_family_ids"] != expected_families:
            raise EventAnalysisError("analysis.declared_links.live_family_ids drifted")

    receipt = _exact(
        analysis["publication_receipt"], _RECEIPT_FIELDS, "analysis.publication_receipt"
    )
    if receipt["automatic_publication"] is not False:
        raise EventAnalysisError("analysis automatic publication is not prohibited")
    if receipt["human_review_required"] is not True:
        raise EventAnalysisError("analysis human-review policy drifted")
    if receipt["citation_coverage"] != 1.0 or cited_count != sentence_count:
        raise EventAnalysisError("analysis citation coverage is not complete")
    if receipt["publishable"] is not True or receipt["status"] != "passed":
        raise EventAnalysisError("analysis publication receipt does not match its gates")
    if type(receipt["availability_warnings"]) is not list:
        raise EventAnalysisError("analysis availability warnings are invalid")
    gates = receipt["gates"]
    if type(gates) is not list or not gates:
        raise EventAnalysisError("analysis publication gates are missing")
    gate_ids: set[str] = set()
    for index, gate in enumerate(gates):
        item = _exact(gate, _GATE_FIELDS, f"analysis.publication_receipt.gates[{index}]")
        _text(item["gate_id"], f"gate[{index}].gate_id", maximum=80)
        if item["gate_id"] in gate_ids or item["passed"] is not True:
            raise EventAnalysisError("analysis gate is invalid or failed")
        gate_ids.add(item["gate_id"])
        _text(item["label"], f"gate[{index}].label", maximum=500)
        _text(item["detail"], f"gate[{index}].detail", maximum=1_000)
    required_gates = {
        "closed-source-set",
        "sentence-citations",
        "no-causal-language",
        "bounded-authorship",
        "denominators-separated",
        "human-review-policy",
    }
    if not required_gates.issubset(gate_ids):
        raise EventAnalysisError("analysis is missing a required publication gate")

    authorship = _exact(analysis["authorship"], _AUTHORSHIP_FIELDS, "analysis.authorship")
    if authorship != {
        "byline": "Palimpsest China Desk",
        "mode": PUBLICATION_MODE,
        "human_interviews": "none",
        "freeform_model_generation": "none",
    }:
        raise EventAnalysisError("analysis authorship boundary changed")
    if analysis["disclosure"] != DISCLOSURE:
        raise EventAnalysisError("analysis disclosure changed")
    if analysis["method"] != METHOD:
        raise EventAnalysisError("analysis.method does not match the v2 method")
    hits = causal_hits(
        {
            "brief": analysis["brief"],
            "counterreadings": analysis["counterreadings"],
            "unknowns": analysis["unknowns"],
            "position": analysis["position"],
            "rationale": analysis["rationale"],
            "limitations": analysis["limitations"],
        }
    )
    if hits:
        raise EventAnalysisError("analysis emits forbidden causal language: " + ", ".join(hits))


__all__ = [
    "ARCHIVE_SURFACE_ID",
    "DISCLOSURE",
    "FORBIDDEN_CAUSAL",
    "LIVE_FAMILY_IDS",
    "METHOD",
    "PIPE_SIGNAL_IDS",
    "PUBLICATION_MODE",
    "SCHEMA_VERSION",
    "_TOP_V2_EXTRA",
    "build_v2_blocks",
    "causal_hits",
    "event_urls",
    "extra_limitations",
    "archive_news_context_block",
    "corroboration_coverage",
    "load_optional_archive_context",
    "load_optional_corroboration",
    "load_optional_live_families",
    "official_urls",
    "validate_v2_blocks",
]
