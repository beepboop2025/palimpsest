"""Visibility-event envelope: shared fields, labels, never missing→censorship."""

from __future__ import annotations

import pytest

from core.visibility_event import (
    VISIBILITY_LABELS,
    evidence_hash,
    stamp_visibility_event,
    validate_visibility_event,
    visibility_label_for,
    VisibilityEventError,
)


def test_stamp_fills_shared_fields_and_hashes_the_envelope():
    row = stamp_visibility_event(
        {
            "url": "https://www.gov.cn/",
            "source": "official_first_seen",
            "content_sha256": "a" * 64,
            "detected_at": "2026-08-20T12:00:00Z",
            "provenance": {
                "collector": "official_first_seen",
                "vantage": "outside-china-public-source",
                "http_status": 200,
            },
        }
    )
    assert row["observer_class"] == "official-landing"
    assert row["surface"] == "official-landing"
    assert row["locator"] == "https://www.gov.cn/"
    assert row["visibility_state"] == "visible"
    assert row["http_status"] == 200
    assert row["evidence_hash"] == evidence_hash(row)
    validate_visibility_event(row)


def test_archive_gap_is_not_a_deletion():
    row = stamp_visibility_event(
        {
            "url": "https://example.com/never-crawled",
            "note": "no_baseline",
            "source": "wayback",
            "provenance": {"collector": "wayback_vantage", "vantage": "ARCHIVE"},
        },
        observer_class="archive-crawler",
        visibility_state="unknown",
        missingness="archive_gap",
    )
    assert row["visibility_label"] == "archive_gap"
    assert row["missingness"] == "archive_gap"
    with pytest.raises(VisibilityEventError, match="archive gap"):
        validate_visibility_event({
            **row,
            "visibility_label": "confirmed_removal",
            "visibility_state": "unavailable",
        })


def test_lone_unavailable_is_not_confirmed_removal():
    assert visibility_label_for(state="unavailable") is None
    assert visibility_label_for(
        state="unavailable",
        had_live_baseline=True,
        control_unaffected=True,
        repeats=2,
        confirmed=True,
    ) == "confirmed_removal"
    row = stamp_visibility_event(
        {
            "url": "https://www.news.cn/",
            "deletion_signal": "disappeared",
            "source": "official_first_seen",
            "provenance": {
                "collector": "official_first_seen",
                "vantage": "outside-china-public-source",
                "http_status": 404,
            },
        }
    )
    assert row["visibility_state"] == "unavailable"
    assert row.get("visibility_label") != "confirmed_removal"
    assert "censorship" not in VISIBILITY_LABELS


def test_protocol_schema_locks_the_label_enum():
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "protocol/greyball-visibility-event-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    labels = [item for item in schema["properties"]["visibility_label"]["enum"] if item]
    assert set(labels) == VISIBILITY_LABELS
    assert "missing" not in labels
    assert "censorship" not in labels
