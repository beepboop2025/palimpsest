"""Collector artifact envelope: complete, abstained, and projected readings."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.collector_artifact import (
    SCHEMA_VERSION,
    ArtifactError,
    build_artifact,
    project_reading,
    validate_artifact,
)


def test_complete_artifact_round_trips() -> None:
    artifact = build_artifact(
        collector_id="ddti",
        source_receipt={"url": "https://chinadigitaltimes.net/feed/"},
        freshness={"evidence_state": "fresh", "observed_at": "2026-08-22T09:00:00Z"},
        coverage={"n_terms": 2},
        abstention=None,
        payload={"ranked": []},
    )
    validate_artifact(artifact)
    assert artifact["schema_version"] == SCHEMA_VERSION
    assert len(artifact["payload_sha256"]) == 64


def test_abstention_does_not_require_a_receipt() -> None:
    artifact = build_artifact(
        collector_id="baike",
        source_receipt=None,
        freshness={"evidence_state": "abstained"},
        coverage={},
        abstention={"code": "source_refused", "reason": "HTTP 403 login wall"},
        payload=None,
    )
    validate_artifact(artifact)
    assert artifact["abstention"]["code"] == "source_refused"


def test_missing_reason_is_rejected() -> None:
    with pytest.raises(ArtifactError):
        build_artifact(
            collector_id="x",
            source_receipt=None,
            freshness={"evidence_state": "abstained"},
            coverage={},
            abstention={"code": "nope"},
            payload=None,
        )


def test_project_reading_missing_file(tmp_path: Path) -> None:
    artifact = project_reading(tmp_path / "nope-latest.json", collector_id="nope")
    assert artifact["abstention"]["code"] == "missing-file"
    assert artifact["freshness"]["evidence_state"] == "missing"


def test_project_reading_uses_feed_health(tmp_path: Path) -> None:
    path = tmp_path / "ddti-latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-22T09:00:00Z",
                "n_terms": 3,
                "feed_health": {"endpoint": "https://chinadigitaltimes.net/feed/"},
            }
        ),
        encoding="utf-8",
    )
    artifact = project_reading(path, collector_id="ddti")
    assert artifact["source_receipt"]["url"].startswith("https://")
    assert artifact["freshness"]["evidence_state"] == "fresh"
    assert artifact["coverage"]["n_terms"] == 3
