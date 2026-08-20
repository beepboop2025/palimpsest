"""Join the RSS evidence wire to historical archive and instrument context.

This is a deterministic context builder, not a story generator. It consumes the
existing metadata-only newswire, Common Crawl aggregate feature rows, and the
normalized OSINT China board. It emits topic-level receipts and model-ready ranking
features without copying article bodies, asserting causality, or assigning truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from collectors.common_crawl_lake import (
    CHINA_JOINS_FILENAME,
    CHINA_JOINS_KIND,
    CHINA_JOINS_SCHEMA,
    DEFAULT_CONFIG,
    FEATURE_SCHEMA_VERSION,
    LakeConfig,
    LimitExceeded,
    ValidationError,
    _canonical_json,
    _iso_now,
    _strict_json_bytes,
    lake_observation_count,
    load_config,
    match_observation,
    open_existing_database,
    public_match_fields,
    public_url_identity,
)
from processors.editorial_priority import editorial_priority


UTC = timezone.utc
CONTEXT_SCHEMA_VERSION = "palimpsest-archive-news-context/v1"
TRAINING_SCHEMA_VERSION = "palimpsest-story-ranking-features/v1"
CONTEXT_METHOD = (
    "deterministic point-in-time topical join; no article-body fetch, causal inference, "
    "truth score, or automatic publication"
)
FEATURES_FILENAME = "common-crawl-features.jsonl"
LIVE_ARCHIVE_CONTEXT_FAMILIES = (
    "news-wire-live",
    "public-deletion-ledgers",
    "official-first-seen",
)
HOST_PUBLIC_COPY = "coverage on this official host moved"
TOPIC_PUBLIC_COPY = "this event shares topics with a watched host."
_FORBIDDEN_COPY = (
    "censored because",
    "this was censored",
    "intent to",
    "because they",
)
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
MAX_FEATURE_LINE_BYTES = 1024 * 1024

# These are the same intrinsically China/Hong Kong feeds reviewed by core.newswire.
# Keeping the small set here avoids treating every Mandarin-language global story as China news.
_CHINA_SCOPED_SOURCE_IDS = frozenset(
    {
        "china-digital-times",
        "gfw-report",
        "hksar-releases",
        "citizen-lab-chat-censorship",
        "hong-kong-free-press",
        "scmp-china",
        "scmp-china-economy",
        "scmp-china-tech",
    }
)

_EVIDENCE_ORDINAL = {
    "single-source": 0,
    "single-primary-source": 1,
    "single-measurement-source": 2,
    "multi-source": 3,
    "primary-corroborated": 4,
    "measurement-corroborated": 5,
}


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read {label}: {exc}") from exc
    value = _strict_json_bytes(raw, maximum=MAX_DOCUMENT_BYTES, label=label)
    if type(value) is not dict:
        raise ValidationError(f"{label} must be a JSON object")
    return value


def _verify_feature_row(value: object, line_number: int) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValidationError(f"feature line {line_number} must be an object")
    if value.get("schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValidationError(f"feature line {line_number} has an unsupported schema")
    digest = value.get("feature_sha256")
    if type(digest) is not str or len(digest) != 64:
        raise ValidationError(f"feature line {line_number} has no valid identity")
    unsigned = dict(value)
    del unsigned["feature_sha256"]
    expected = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    if digest != expected:
        raise ValidationError(f"feature line {line_number} fails its content identity")
    return value


def load_feature_rows(path: Path | str, config: LakeConfig) -> tuple[list[dict[str, Any]], str]:
    feature_path = Path(path)
    try:
        if feature_path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise LimitExceeded("feature export exceeds the 128 MiB context input cap")
        raw_document = feature_path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read feature export: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(raw_document.splitlines(), 1):
        if not raw.strip():
            continue
        if len(raw) > MAX_FEATURE_LINE_BYTES:
            raise LimitExceeded(f"feature line {line_number} exceeds 1 MiB")
        value = _strict_json_bytes(
            raw, maximum=MAX_FEATURE_LINE_BYTES, label=f"feature line {line_number}"
        )
        rows.append(_verify_feature_row(value, line_number))
        if len(rows) > config.limits.feature_rows:
            raise LimitExceeded("feature row count exceeds the configured cap")
    return rows, hashlib.sha256(raw_document).hexdigest()


def _timestamp(value: object, path: str) -> datetime:
    if type(value) is not str or not value:
        raise ValidationError(f"{path} is missing")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{path} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{path} has no timezone")
    return parsed.astimezone(UTC)


def _china_scoped(event: dict[str, Any]) -> bool:
    refs = event.get("evidence_refs")
    return isinstance(refs, list) and any(
        type(ref) is dict and ref.get("source_id") in _CHINA_SCOPED_SOURCE_IDS
        for ref in refs
    )


def _latest_before(
    rows: Iterable[dict[str, Any]], target_id: str, when: datetime
) -> dict[str, Any] | None:
    eligible = []
    for row in rows:
        if row.get("target_id") != target_id:
            continue
        capture = _timestamp(row.get("last_capture_at"), "feature.last_capture_at")
        available = _timestamp(row.get("available_at"), "feature.available_at")
        if capture <= when and available <= when:
            eligible.append((available, capture, row))
    return max(eligible, default=(None, None, None), key=lambda item: item[:2])[2]


def _signal_receipt(signal: dict[str, Any]) -> dict[str, Any]:
    health = signal.get("health") if type(signal.get("health")) is dict else {}
    metric = signal.get("metric") if type(signal.get("metric")) is dict else {}
    input_doc = signal.get("input") if type(signal.get("input")) is dict else {}
    return {
        "id": signal.get("id"),
        "layer": signal.get("layer"),
        "live": signal.get("live") is True,
        "freshness_deadline": signal.get("freshness_deadline"),
        "health_reason": health.get("reason"),
        "metric": {
            "label": metric.get("label"),
            "value": metric.get("value"),
            "unit": metric.get("unit"),
            "denominator": metric.get("denominator"),
        },
        "input_sha256": input_doc.get("sha256"),
        "relation": "topic-surface-only",
    }


def _archive_receipt(row: dict[str, Any], event_time: datetime) -> dict[str, Any]:
    capture = _timestamp(row["last_capture_at"], "feature.last_capture_at")
    available = _timestamp(row["available_at"], "feature.available_at")
    features = row["features"]
    model = row["model"]
    return {
        "target_id": row["target_id"],
        "host": row["host"],
        "crawl": row["crawl"],
        "last_capture_at": row["last_capture_at"],
        "available_at": row["available_at"],
        "age_at_event_seconds": max(0, int((event_time - capture).total_seconds())),
        "knowledge_age_at_event_seconds": max(
            0, int((event_time - available).total_seconds())
        ),
        "feature_sha256": row["feature_sha256"],
        "unique_urls": features["unique_urls"],
        "mutated_urls": features["mutated_urls"],
        "mutation_rate": features["mutation_rate"],
        "archive_gap_rate": features["archive_gap_rate"],
        "anomaly_state": model["state"],
        "anomaly_score": model["score"],
        "relation": "topic-surface-only",
        "absence_semantics": "archive-coverage-gap-not-deletion",
    }


def _bounded_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def derived_feature_paths() -> list[Path]:
    """Candidate derived feature exports. Never inbox, sqlite, or WARC."""

    from collectors.common_crawl_lake import DEFAULT_WAREHOUSE

    paths: list[Path] = []
    env_features = (os.getenv("PALIMPSEST_COMMON_CRAWL_FEATURES") or "").strip()
    if env_features:
        paths.append(Path(env_features).expanduser())
    env_warehouse = (os.getenv("PALIMPSEST_COMMON_CRAWL_WAREHOUSE_DIR") or "").strip()
    if env_warehouse:
        paths.append(Path(env_warehouse).expanduser() / "derived" / FEATURES_FILENAME)
    paths.append(Path("/var/lib/palimpsest/common-crawl") / "derived" / FEATURES_FILENAME)
    paths.append(DEFAULT_WAREHOUSE / "derived" / FEATURES_FILENAME)
    return paths


def find_feature_export(path: Path | str | None = None) -> Path | None:
    """Return the first readable derived feature export. Missing lake abstains."""

    candidates = [Path(path).expanduser()] if path is not None else derived_feature_paths()
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def load_feature_export_or_none(
    path: Path | str | None = None,
    config: LakeConfig | None = None,
) -> tuple[list[dict[str, Any]], str] | None:
    """Load derived feature rows. Missing or empty lake returns None."""

    feature_path = find_feature_export(path)
    if feature_path is None:
        return None
    cfg = config or load_config()
    return load_feature_rows(feature_path, cfg)


def _optional_timestamp(value: object) -> datetime | None:
    if type(value) is not str or not value:
        return None
    try:
        return _timestamp(value, "optional")
    except ValidationError:
        return None


def _observation_when(record: Mapping[str, Any]) -> datetime | None:
    for field in (
        "detected_at",
        "first_seen",
        "published_at",
        "updated_at",
        "last_seen",
        "last_confirmed_alive",
    ):
        parsed = _optional_timestamp(record.get(field))
        if parsed is not None:
            return parsed
    provenance = record.get("provenance")
    if isinstance(provenance, Mapping):
        parsed = _optional_timestamp(provenance.get("fetched_at"))
        if parsed is not None:
            return parsed
    return None


def _explicit_topics(record: Mapping[str, Any]) -> set[str]:
    topics = record.get("topics")
    if not isinstance(topics, list):
        return set()
    return {topic for topic in topics if type(topic) is str and topic}


def _observation_url(record: Mapping[str, Any]) -> str | None:
    for field in ("url", "source_url", "event_url"):
        value = record.get(field)
        if type(value) is str and value:
            return value
    return None


def public_copy_for_match(match_kind: str) -> str:
    """Context-only sentence. Never assigns motive, intent, or causation."""

    if match_kind == "host":
        copy = HOST_PUBLIC_COPY
    elif match_kind == "topic":
        copy = TOPIC_PUBLIC_COPY
    else:
        raise ValidationError("archive context match_kind is not host or topic")
    lowered = copy.casefold()
    if any(token in lowered for token in _FORBIDDEN_COPY):
        raise ValidationError("archive context copy is not context-only")
    return copy


def match_feature_rows_for_record(
    record: Mapping[str, Any],
    feature_rows: list[dict[str, Any]],
    config: LakeConfig,
    *,
    when: datetime | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Return (match_kind, point-in-time feature rows). Never invents a join."""

    event_time = when or _observation_when(record)
    if event_time is None:
        return None, []
    url = _observation_url(record)
    identity = public_url_identity(url, maximum_chars=config.limits.url_chars) if url else None
    if identity is not None:
        target = config.target_by_host.get(identity[1])
        if target is not None and "palimpsest" in target.products:
            row = _latest_before(feature_rows, target.id, event_time)
            if row is not None:
                return "host", [row]
    topic_set = _explicit_topics(record)
    if not topic_set:
        return None, []
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in config.targets:
        if "palimpsest" not in target.products:
            continue
        if not topic_set.intersection(target.topics):
            continue
        row = _latest_before(feature_rows, target.id, event_time)
        if row is None or row["target_id"] in seen:
            continue
        seen.add(row["target_id"])
        matched.append(row)
    if not matched:
        return None, []
    return "topic", matched


def _public_archive_receipts(
    rows: list[dict[str, Any]], event_time: datetime
) -> list[dict[str, Any]]:
    return [_archive_receipt(row, event_time) for row in rows]


def attach_derived_archive_context(
    observations: Iterable[Mapping[str, Any]],
    *,
    feature_rows: list[dict[str, Any]] | None = None,
    feature_export_sha256: str | None = None,
    config: LakeConfig | None = None,
    features_path: Path | str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Attach derived host/topic receipts. Missing lake leaves observations unchanged."""

    rows = [dict(item) for item in observations if type(item) is dict or isinstance(item, Mapping)]
    loaded = None
    if feature_rows is None:
        loaded = load_feature_export_or_none(features_path, config)
        if loaded is None:
            return rows
        feature_rows, feature_export_sha256 = loaded
    cfg = config or load_config()
    attached: list[dict[str, Any]] = []
    for record in rows:
        row = dict(record)
        event_time = _observation_when(row) or now
        match_kind, matched = match_feature_rows_for_record(
            row, feature_rows, cfg, when=event_time
        )
        if match_kind is None or not matched or event_time is None:
            attached.append(row)
            continue
        receipts = _public_archive_receipts(matched, event_time)
        row["archive_context"] = receipts
        row["archive_context_match"] = {
            "match_kind": match_kind,
            "public_copy": public_copy_for_match(match_kind),
            "feature_export_sha256": feature_export_sha256,
            "relation": "context-not-causation",
        }
        attached.append(row)
    return attached


def build_archive_context(
    newswire: dict[str, Any],
    osint_board: dict[str, Any],
    feature_rows: list[dict[str, Any]],
    feature_export_sha256: str,
    config: LakeConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a metadata-only context document for the existing newsroom."""

    if newswire.get("schema_version") != "palimpsest-newswire.v1":
        raise ValidationError("newswire has an unsupported schema")
    if osint_board.get("schema_version") != "osint-china.v1":
        raise ValidationError("OSINT board has an unsupported schema")
    events = newswire.get("events")
    signals = osint_board.get("signals")
    if not isinstance(events, list) or not isinstance(signals, list):
        raise ValidationError("newswire events and OSINT signals must be lists")
    if len(events) > config.limits.news_events:
        raise LimitExceeded("newswire event count exceeds the configured context cap")
    signal_by_id = {
        signal.get("id"): signal
        for signal in signals
        if type(signal) is dict and type(signal.get("id")) is str
    }
    target_map = config.target_by_id
    context_events = []
    for event in events:
        if type(event) is not dict or not _china_scoped(event):
            continue
        event_id = event.get("event_id")
        version_id = event.get("version_id")
        topics = event.get("topics")
        if type(event_id) is not str or type(version_id) is not str or not isinstance(topics, list):
            raise ValidationError("newswire event identity or topics are invalid")
        published_at = _timestamp(event.get("published_at"), "event.published_at")
        topic_set = {topic for topic in topics if type(topic) is str}
        target_ids = [
            target.id
            for target in config.targets
            if "palimpsest" in target.products
            and topic_set.intersection(target.topics)
        ]
        archive_context = []
        for target_id in target_ids:
            if target_id not in target_map:
                continue
            row = _latest_before(feature_rows, target_id, published_at)
            if row is not None:
                archive_context.append(_archive_receipt(row, published_at))

        declared = event.get("declared_links")
        if type(declared) is not dict:
            raise ValidationError("newswire event declared_links is invalid")
        linked_ids = []
        for field in ("scan_signal_ids", "economic_signal_ids"):
            values = declared.get(field, [])
            if not isinstance(values, list) or any(type(value) is not str for value in values):
                raise ValidationError(f"newswire event {field} is invalid")
            linked_ids.extend(values)
        signal_context = [
            _signal_receipt(signal_by_id[signal_id])
            for signal_id in sorted(set(linked_ids))
            if signal_id in signal_by_id
        ]
        archive_scores = [
            score
            for score in (_bounded_number(item["anomaly_score"]) for item in archive_context)
            if score is not None
        ]
        independent_groups = event.get("evidence_groups")
        if not isinstance(independent_groups, list):
            raise ValidationError("newswire event evidence_groups is invalid")
        strength = event.get("evidence_strength")
        if strength not in _EVIDENCE_ORDINAL:
            raise ValidationError("newswire event evidence_strength is invalid")
        model_features = {
            "archive_targets": len(archive_context),
            "archive_anomaly_max": max(archive_scores) if archive_scores else None,
            "archive_anomalies": sum(
                item["anomaly_state"] == "archive_anomaly" for item in archive_context
            ),
            "linked_signals": len(signal_context),
            "live_linked_signals": sum(item["live"] for item in signal_context),
            "independent_evidence_groups": len(independent_groups),
            "evidence_strength_ordinal": _EVIDENCE_ORDINAL[strength],
        }
        priority = editorial_priority(model_features)
        if (
            type(priority) is not dict
            or set(priority) != {"status", "score", "meaning"}
            or priority["status"] not in {"unconfigured-human-policy", "configured"}
            or (
                priority["score"] is not None
                and (
                    isinstance(priority["score"], bool)
                    or not isinstance(priority["score"], (int, float))
                    or not math.isfinite(float(priority["score"]))
                    or not 0 <= float(priority["score"]) <= 100
                )
            )
            or type(priority["meaning"]) is not str
            or "review priority" not in priority["meaning"]
        ):
            raise ValidationError("editorial priority policy returned an unsafe result")
        context_events.append(
            {
                "event_id": event_id,
                "version_id": version_id,
                "event_url": event.get("url"),
                "published_at": event.get("published_at"),
                "topics": sorted(topic_set),
                "evidence_strength": strength,
                "relation": "context-not-causation",
                "archive_context": archive_context,
                "signal_context": signal_context,
                "model_features": model_features,
                "editorial_priority": priority,
                "training_label": "unreviewed",
                "automatic_publication_eligible": False,
                "limitations": [
                    "Archive and signal links are topical context, not evidence that one caused another.",
                    "Common Crawl is monthly and may substantially predate a live RSS event.",
                    "The ranking features describe review priority, not truth or public importance.",
                ],
            }
        )
    context_events.sort(key=lambda item: (item["published_at"], item["event_id"]), reverse=True)
    document: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "generated_at": _iso_now(now),
        "newswire_generated_at": newswire.get("generated_at"),
        "osint_generated_at": osint_board.get("generated_at"),
        "feature_export_sha256": feature_export_sha256,
        "scope": "China-scoped RSS metadata joined to prior aggregate archive and signal context",
        "method": CONTEXT_METHOD,
        "n_events_considered": len(events),
        "n_events_contextualized": len(context_events),
        "events": context_events,
        "publication_policy": {
            "automatic_publication": "prohibited",
            "human_review_required": True,
            "causal_language": "prohibited-without-a-declared-design",
            "person_level_analysis": "prohibited",
        },
    }
    document["context_sha256"] = hashlib.sha256(_canonical_json(document)).hexdigest()
    return document


def build_training_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Project context into metadata-only rows for a future human-labeled ranker."""

    if context.get("schema_version") != CONTEXT_SCHEMA_VERSION:
        raise ValidationError("context has an unsupported schema")
    rows = []
    for event in context.get("events", []):
        row: dict[str, Any] = {
            "schema_version": TRAINING_SCHEMA_VERSION,
            "knowledge_time": context["generated_at"],
            "event_id": event["event_id"],
            "event_version_id": event["version_id"],
            "published_at": event["published_at"],
            "topics": event["topics"],
            "features": event["model_features"],
            "label": None,
            "label_source": "human-editorial-review-required",
            "rights": {"training_use": "derived_only"},
        }
        row["row_sha256"] = hashlib.sha256(_canonical_json(row)).hexdigest()
        rows.append(row)
    return rows


def _atomic_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _china_observation_candidates(
    osint_board: Mapping[str, Any] | None,
    readings_dir: Path | None,
) -> list[dict[str, Any]]:
    from core.china_observation import observation_key

    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(record: object) -> None:
        if type(record) is not dict:
            return
        key = observation_key(record)
        if key in seen:
            return
        seen.add(key)
        records.append(record)

    if readings_dir is not None:
        undertext_path = Path(readings_dir) / "undertext-latest.json"
        if undertext_path.is_file():
            payload = _read_json(undertext_path, "undertext")
            for record in payload.get("observations") or []:
                _add(record)
    if records:
        return records
    if type(osint_board) is dict:
        for signal in osint_board.get("signals") or []:
            if type(signal) is not dict or signal.get("id") != "undertext":
                continue
            payload = signal.get("payload") if type(signal.get("payload")) is dict else {}
            for record in payload.get("observations") or []:
                _add(record)
    return records


def _empty_china_joins(now: datetime | None) -> dict[str, Any]:
    return {
        "kind": CHINA_JOINS_KIND,
        "schema": CHINA_JOINS_SCHEMA,
        "generated_at": _iso_now(now),
        "status": "no_data",
        "source": "node-lake-readonly",
        "matches": [],
        "uncertainty": (
            "Common Crawl warehouse is empty or absent on this host. "
            "No join attempted. Absence is not a zero census."
        ),
    }


def build_china_lake_joins(
    osint_board: Mapping[str, Any] | None,
    readings_dir: Path | str | None,
    *,
    connection,
    config: LakeConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join public China observations to the existing lake. Read-only. No scrape."""

    from core.china_observation import observation_key

    if connection is None or lake_observation_count(connection) < 1:
        return _empty_china_joins(now)
    candidates = _china_observation_candidates(
        osint_board, Path(readings_dir) if readings_dir is not None else None
    )
    matches: list[dict[str, Any]] = []
    for record in candidates:
        hit = match_observation(connection, record, config)
        if hit is None:
            continue
        identity = public_url_identity(
            record.get("url") or record.get("source_url"),
            maximum_chars=config.limits.url_chars,
        )
        matches.append(
            {
                "observation_key": observation_key(record),
                "url_sha256": identity[0] if identity else None,
                **public_match_fields(hit),
            }
        )
    return {
        "kind": CHINA_JOINS_KIND,
        "schema": CHINA_JOINS_SCHEMA,
        "generated_at": _iso_now(now),
        "status": "ok",
        "source": "node-lake-readonly",
        "n_candidates": len(candidates),
        "n_matches": len(matches),
        "matches": matches,
        "uncertainty": (
            "Sanitized Common Crawl lake joins. URL/digest matches are archive "
            "coverage, not deletions. Host matches are institution-level context, "
            "not URL corroboration. Lake URLs, WARC paths, offsets, lengths, and "
            "bodies are not published."
        ),
    }


def write_china_lake_joins(
    *,
    osint_path: Path | str | None,
    readings_dir: Path | str | None,
    warehouse: Path | str | None,
    output_path: Path | str | None = None,
    config_path: Path | str = DEFAULT_CONFIG,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write a private sanitized receipt when the warehouse already exists."""

    config = load_config(config_path)
    warehouse_root = Path(warehouse) if warehouse is not None else None
    connection = open_existing_database(warehouse_root)
    osint_board: dict[str, Any] | None = None
    osint_file = Path(osint_path) if osint_path else None
    try:
        if osint_file is not None and osint_file.is_file():
            osint_board = _read_json(osint_file, "OSINT board")
        document = build_china_lake_joins(
            osint_board,
            Path(readings_dir) if readings_dir is not None else None,
            connection=connection,
            config=config,
            now=now,
        )
    finally:
        if connection is not None:
            connection.close()
    dest = Path(output_path) if output_path is not None else (
        warehouse_root / "derived" / CHINA_JOINS_FILENAME if warehouse_root is not None else None
    )
    wrote = False
    if dest is not None and warehouse_root is not None and warehouse_root.is_dir():
        _atomic_private(dest, _canonical_json(document) + b"\n")
        wrote = True
    return {
        "status": document["status"],
        "matches": len(document.get("matches") or []),
        "path": str(dest) if wrote else None,
    }


def _live_family_filename(family: str) -> str:
    return f"{family}-latest.json"


def _load_optional_json(path: Path, label: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = _read_json(path, label)
    except ValidationError:
        return None
    return value


def _live_event_id(record: Mapping[str, Any], family: str) -> str:
    from core.china_observation import observation_key, public_text

    provenance = record.get("provenance") if isinstance(record.get("provenance"), Mapping) else {}
    event_id = public_text(provenance.get("event_id"), limit=80)
    if event_id:
        return event_id
    return f"{family}:{observation_key(record)}"


def _live_context_event(
    record: Mapping[str, Any],
    *,
    family: str,
    match_kind: str,
    archive_context: list[dict[str, Any]],
    published_at: str,
) -> dict[str, Any]:
    from core.china_observation import observation_key

    topics = sorted(_explicit_topics(record))
    return {
        "event_id": _live_event_id(record, family),
        "family": family,
        "observation_key": observation_key(record),
        "event_url": _observation_url(record),
        "published_at": published_at,
        "topics": topics,
        "relation": "context-not-causation",
        "match_kind": match_kind,
        "public_copy": public_copy_for_match(match_kind),
        "archive_context": archive_context,
        "automatic_publication_eligible": False,
        "limitations": [
            "Archive links are topical or host-level context, not evidence that one caused another.",
            "Common Crawl is monthly and may substantially predate a live observation.",
            "A coverage change is an archive coverage fact, not a finding about why a page changed.",
        ],
    }


def build_public_archive_news_context(
    *,
    feature_rows: list[dict[str, Any]],
    feature_export_sha256: str,
    config: LakeConfig,
    live_families: Mapping[str, Mapping[str, Any] | None] | None = None,
    newswire: Mapping[str, Any] | None = None,
    osint_board: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Public context-only reading. Joins only when a feature row already matches."""

    context_events: list[dict[str, Any]] = []
    n_events_considered = 0
    family_status: dict[str, str] = {}
    if (
        type(newswire) is dict
        and type(osint_board) is dict
        and newswire.get("schema_version") == "palimpsest-newswire.v1"
        and osint_board.get("schema_version") == "osint-china.v1"
    ):
        wire = build_archive_context(
            dict(newswire),
            dict(osint_board),
            feature_rows,
            feature_export_sha256,
            config,
            now=now,
        )
        n_events_considered += int(wire.get("n_events_considered") or 0)
        for event in wire.get("events") or []:
            if type(event) is not dict:
                continue
            row = dict(event)
            row["family"] = "newswire"
            row["match_kind"] = "topic" if row.get("archive_context") else None
            if row.get("archive_context"):
                row["public_copy"] = public_copy_for_match("topic")
                context_events.append(row)
    families = live_families or {}
    n_observations_considered = 0
    n_observations_joined = 0
    for family in LIVE_ARCHIVE_CONTEXT_FAMILIES:
        payload = families.get(family)
        if type(payload) is not dict:
            family_status[family] = "missing"
            continue
        family_status[family] = "present"
        observations = payload.get("observations")
        if not isinstance(observations, list):
            continue
        for record in observations:
            if type(record) is not dict:
                continue
            n_observations_considered += 1
            event_time = _observation_when(record)
            match_kind, matched = match_feature_rows_for_record(
                record, feature_rows, config, when=event_time
            )
            if match_kind is None or not matched or event_time is None:
                continue
            published_at = record.get("detected_at") or record.get("first_seen") or record.get(
                "published_at"
            )
            if type(published_at) is not str or not published_at:
                published_at = _iso_now(event_time)
            context_events.append(
                _live_context_event(
                    record,
                    family=family,
                    match_kind=match_kind,
                    archive_context=_public_archive_receipts(matched, event_time),
                    published_at=published_at,
                )
            )
            n_observations_joined += 1
    context_events.sort(
        key=lambda item: (item.get("published_at") or "", item.get("event_id") or ""),
        reverse=True,
    )
    document: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "generated_at": _iso_now(now),
        "status": "ok",
        "source": (
            "Common Crawl derived host features joined to live news-wire, "
            "official-first-seen, and public-deletion-ledger observations"
        ),
        "scope": (
            "Context-only host coverage and topical overlap. No article-body fetch. "
            "No causal claim. Raw Common Crawl URLs and source bodies stay private."
        ),
        "method": CONTEXT_METHOD,
        "feature_export_sha256": feature_export_sha256,
        "families": family_status,
        "n_events_considered": n_events_considered,
        "n_events_contextualized": len(context_events),
        "n_observations_considered": n_observations_considered,
        "n_observations_joined": n_observations_joined,
        "events": context_events,
        "publication_policy": {
            "automatic_publication": "prohibited",
            "human_review_required": True,
            "causal_language": "prohibited-without-a-declared-design",
            "person_level_analysis": "prohibited",
        },
    }
    copies = [
        str(event.get("public_copy") or "")
        for event in context_events
        if type(event) is dict
    ]
    lowered = " ".join(copies).casefold()
    if any(token in lowered for token in _FORBIDDEN_COPY):
        raise ValidationError("public archive context copy is not context-only")
    document["context_sha256"] = hashlib.sha256(_canonical_json(document)).hexdigest()
    return document


def write_public_archive_news_context(
    *,
    readings_dir: Path | str,
    output_path: Path | str | None = None,
    features_path: Path | str | None = None,
    config_path: Path | str = DEFAULT_CONFIG,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Write the public reading, or abstain when the derived lake is missing."""

    config = load_config(config_path)
    loaded = load_feature_export_or_none(features_path, config)
    if loaded is None:
        return None
    feature_rows, feature_sha256 = loaded
    readings = Path(readings_dir)
    live_families: dict[str, dict[str, Any] | None] = {}
    for family in LIVE_ARCHIVE_CONTEXT_FAMILIES:
        live_families[family] = _load_optional_json(
            readings / _live_family_filename(family), family
        )
    newswire = _load_optional_json(readings / "newswire-latest.json", "newswire")
    osint_board = _load_optional_json(readings / "osint-china-latest.json", "OSINT board")
    document = build_public_archive_news_context(
        feature_rows=feature_rows,
        feature_export_sha256=feature_sha256,
        config=config,
        live_families=live_families,
        newswire=newswire,
        osint_board=osint_board,
        now=now,
    )
    dest = Path(output_path) if output_path is not None else readings / "archive-news-context-latest.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "context_sha256": document["context_sha256"],
        "events": document["n_events_contextualized"],
        "observations_joined": document["n_observations_joined"],
        "path": str(dest),
    }


def write_archive_context(
    *,
    newswire_path: Path | str,
    osint_path: Path | str,
    features_path: Path | str,
    context_path: Path | str,
    training_path: Path | str,
    config_path: Path | str = DEFAULT_CONFIG,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    newswire = _read_json(Path(newswire_path), "newswire")
    osint_board = _read_json(Path(osint_path), "OSINT board")
    feature_rows, feature_sha256 = load_feature_rows(features_path, config)
    context = build_archive_context(
        newswire,
        osint_board,
        feature_rows,
        feature_sha256,
        config,
        now=now,
    )
    training_rows = build_training_rows(context)
    context_payload = _canonical_json(context) + b"\n"
    training_payload = b"".join(_canonical_json(row) + b"\n" for row in training_rows)
    _atomic_private(Path(context_path), context_payload)
    _atomic_private(Path(training_path), training_payload)
    return {
        "status": "success",
        "context_sha256": context["context_sha256"],
        "events": context["n_events_contextualized"],
        "training_rows": len(training_rows),
        "context_path": str(context_path),
        "training_path": str(training_path),
    }
