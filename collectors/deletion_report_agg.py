"""Public deletion-report aggregation — reports, not reporters.

Ingest already-public deletion/blocking reports (CDT, GreatFire, FreeWeibo-
style ledgers, journalist and digital-rights RSS). Dedupe. Retain platform,
broad topic, timestamp bracket, public evidence receipt, removal-state
category. Drop the reporting person. Do not republish sensitive original
content.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, Sequence

from core.visibility_event import stamp_visibility_event


SCHEMA_VERSION = "palimpsest-deletion-report-agg.v1"
METHOD_VERSION = 1

REPORTER_FIELDS = frozenset(
    {
        "reporter",
        "reporter_name",
        "author",
        "byline",
        "submitter",
        "email",
        "phone",
        "handle",
        "user",
        "username",
        "real_name",
        "source_person",
    }
)

REMOVAL_CATEGORIES = frozenset(
    {
        "ledger-reported",
        "platform-notice",
        "restriction-notice",
        "unavailable",
        "unknown",
    }
)

_SENSITIVE_TRIM = 240


def _topic(row: Mapping[str, Any]) -> str:
    hits = row.get("gazetteer_hits") or row.get("terms") or []
    if isinstance(hits, list) and hits:
        first = hits[0]
        if isinstance(first, dict):
            return str(first.get("en") or first.get("zh") or "unspecified")[:80]
        return str(first)[:80]
    title = str(row.get("title") or "")
    return title[:80] if title else "unspecified"


def _platform(row: Mapping[str, Any]) -> str:
    source = str(row.get("source") or row.get("ledger_kind") or "")
    if "cdt" in source:
        return "cdt"
    if "greatfire" in source:
        return "greatfire"
    if "freeweibo" in source:
        return "weibo-ledger"
    if "freewechat" in source:
        return "wechat-ledger"
    return source.split(":")[-1][:40] or "public-ledger"


def _receipt(row: Mapping[str, Any]) -> str:
    url = str(row.get("url") or row.get("source_url") or "")
    digest = str(row.get("content_sha256") or row.get("content_hash") or "")
    if url.startswith("https://"):
        return hashlib.sha256(url.encode("utf-8")).hexdigest()
    if digest:
        return digest
    return ""


def _category(row: Mapping[str, Any]) -> str:
    trail = row.get("deletion_confirmation") or []
    if isinstance(trail, list) and trail and isinstance(trail[0], dict):
        status = str(trail[0].get("status") or "")
        if status in REMOVAL_CATEGORIES:
            return status
        if "ledger" in status:
            return "ledger-reported"
    signal = str(row.get("deletion_signal") or "")
    if signal in REMOVAL_CATEGORIES:
        return signal
    return "ledger-reported"


def _day(ts: Any) -> str | None:
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", ts):
        return ts[:10]
    return None


def aggregate_reports(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Dedupe public reports and drop reporter identity / original bodies."""

    seen: set[str] = set()
    reports: list[dict[str, Any]] = []
    dropped_identity = 0
    for raw in rows:
        dropped_identity += sum(1 for key in raw if str(key).lower() in REPORTER_FIELDS)
        loc = str(raw.get("url") or raw.get("source_url") or "")
        stamped = stamp_visibility_event(
            {k: v for k, v in raw.items() if str(k).lower() not in REPORTER_FIELDS},
            locator=loc,
            observer_class="public-ledger",
            surface="public-deletion-ledger",
            timestamp=raw.get("detected_at") or raw.get("first_seen") or raw.get("timestamp"),
            content_hash=raw.get("content_sha256") or raw.get("content_hash") or "",
        )
        receipt = _receipt(stamped)
        topic = _topic(stamped)
        platform = _platform(stamped)
        day = _day(stamped.get("timestamp") or stamped.get("first_seen"))
        key = f"{platform}|{topic}|{day}|{receipt}"
        if key in seen:
            continue
        seen.add(key)
        reports.append(
            {
                "platform": platform,
                "broad_topic": topic,
                "timestamp_bracket": {
                    "day": day,
                    "first_seen": stamped.get("first_seen"),
                    "last_seen": stamped.get("last_seen"),
                },
                "public_evidence_receipt": receipt,
                "removal_state_category": _category(stamped),
                "observer_class": "public-ledger",
                "visibility_state": stamped.get("visibility_state"),
                "visibility_label": stamped.get("visibility_label"),
                "evidence_hash": stamped.get("evidence_hash"),
                # Bounded title only — not the original deleted post.
                "public_title": str(stamped.get("title") or "")[:_SENSITIVE_TRIM],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "n_reports": len(reports),
        "n_input": len(list(rows)),
        "n_reporter_fields_dropped": dropped_identity,
        "republishes_original_content": False,
        "exposes_reporting_person": False,
        "reports": reports,
    }
