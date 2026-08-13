"""Join the RSS evidence wire to historical archive and instrument context.

This is a deterministic context builder, not a story generator. It consumes the
existing metadata-only newswire, Common Crawl aggregate feature rows, and the
normalized OSINT China board. It emits topic-level receipts and model-ready ranking
features without copying article bodies, asserting causality, or assigning truth.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from collectors.common_crawl_lake import (
    DEFAULT_CONFIG,
    FEATURE_SCHEMA_VERSION,
    LakeConfig,
    LimitExceeded,
    ValidationError,
    _canonical_json,
    _iso_now,
    _strict_json_bytes,
    load_config,
)
from processors.editorial_priority import editorial_priority


UTC = timezone.utc
CONTEXT_SCHEMA_VERSION = "palimpsest-archive-news-context/v1"
TRAINING_SCHEMA_VERSION = "palimpsest-story-ranking-features/v1"
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
        "method": (
            "deterministic point-in-time topical join; no article-body fetch, causal inference, "
            "truth score, or automatic publication"
        ),
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
