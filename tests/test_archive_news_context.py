"""Offline tests for live-family Common Crawl *derived* context joins."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.common_crawl_lake import (
    FEATURE_SCHEMA_VERSION,
    MODEL_ID,
    _canonical_json,
)
from processors import archive_context
from processors.archive_context import CONTEXT_METHOD


UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "common_crawl_targets.json"
FORBIDDEN = (
    "censored because",
    "this was censored",
    "intent to",
    "because they",
)


def _feature_row(**overrides):
    features = {
        "unique_urls": 12,
        "live_urls": 10,
        "error_urls": 1,
        "chinese_language_urls": 8,
        "archive_record_bytes": 2048,
        "previous_unique_urls": 0,
        "retained_urls": 0,
        "appeared_urls": 12,
        "not_observed_urls": 0,
        "comparable_urls": 0,
        "mutated_urls": 0,
        "coverage_ratio": 1.0,
        "retention_ratio": None,
        "archive_gap_rate": 0.0,
        "mutation_rate": None,
        "error_rate": 0.08333333,
    }
    features.update(overrides.pop("features", {}))
    model = {
        "id": MODEL_ID,
        "minimum_prior_crawls": 6,
        "state": "warming_up",
        "score": None,
        "component_scores": {
            "mutation_rate": None,
            "archive_gap_rate": None,
            "error_rate": None,
        },
    }
    model.update(overrides.pop("model", {}))
    row = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "method_version": 1,
        "target_id": "pbc",
        "host": "www.pbc.gov.cn",
        "aliases": ["pbc.gov.cn"],
        "topics": ["economy", "funding", "government", "policy"],
        "products": ["liquilens", "palimpsest", "seiche"],
        "crawl": "CC-MAIN-2026-30",
        "previous_crawl": None,
        "first_capture_at": "2026-07-24T12:30:00Z",
        "last_capture_at": "2026-07-24T12:30:00Z",
        "available_at": "2026-08-01T00:00:00Z",
        "scope": "institution-level public record",
        "source": "Common Crawl URL Index and WARC locators",
        "features": features,
        "label": {
            "censorship": "unlabeled",
            "absence_semantics": "archive-coverage-gap-not-deletion",
        },
        "model": model,
        "rights": {
            "training_use": "derived_only",
            "license_or_terms_ref": "Common Crawl Terms of Use",
        },
    }
    row.update(overrides)
    unsigned = dict(row)
    unsigned.pop("feature_sha256", None)
    row["feature_sha256"] = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    return row


def _write_features(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _official_obs():
    return {
        "title": "PBOC landing",
        "text": "Official landing text that is long enough.",
        "url": "https://www.pbc.gov.cn/",
        "source": "official-first-seen",
        "detected_at": "2026-08-20T06:00:00Z",
        "first_seen": "2026-08-20T06:00:00Z",
    }


def _wire_obs():
    return {
        "title": "SCMP economy note",
        "text": "A public RSS excerpt about funding policy.",
        "url": "https://chinadigitaltimes.net/2026/08/economy/",
        "source": "news-wire-live",
        "detected_at": "2026-08-20T01:00:00Z",
        "topics": ["economy", "policy"],
        "provenance": {"event_id": "event-aaaaaaaaaaaaaaaaaaaaaaaa"},
    }


def _unmatched_obs():
    return {
        "title": "Unrelated publisher",
        "text": "A story that does not share a watched host or topic.",
        "url": "https://example.com/unrelated/",
        "source": "public-deletion-ledgers",
        "detected_at": "2026-08-20T02:00:00Z",
        "topics": ["sports"],
    }


def test_pull_abstains_when_the_derived_lake_is_missing(tmp_path, monkeypatch):
    import scripts.archive_news_context_pull as pull

    monkeypatch.delenv("PALIMPSEST_COMMON_CRAWL_FEATURES", raising=False)
    monkeypatch.delenv("PALIMPSEST_COMMON_CRAWL_WAREHOUSE_DIR", raising=False)
    monkeypatch.setattr(pull, "KillSwitch", lambda: type("K", (), {"is_halted": lambda self: False})())

    assert pull.main(root=tmp_path) is None
    assert not (tmp_path / "readings" / "archive-news-context-latest.json").exists()


def test_host_match_attaches_derived_coverage_not_motive(tmp_path):
    features = _write_features(tmp_path / "common-crawl-features.jsonl", [_feature_row()])
    config = archive_context.load_config(CONFIG)
    rows, digest = archive_context.load_feature_rows(features, config)
    attached = archive_context.attach_derived_archive_context(
        [_official_obs()],
        feature_rows=rows,
        feature_export_sha256=digest,
        config=config,
    )

    assert attached[0]["archive_context_match"]["match_kind"] == "host"
    assert attached[0]["archive_context_match"]["public_copy"] == (
        archive_context.HOST_PUBLIC_COPY
    )
    receipt = attached[0]["archive_context"][0]
    assert receipt["host"] == "www.pbc.gov.cn"
    assert receipt["crawl"] == "CC-MAIN-2026-30"
    assert receipt["unique_urls"] == 12
    assert receipt["mutation_rate"] is None
    assert receipt["archive_gap_rate"] == 0.0
    assert receipt["anomaly_state"] == "warming_up"
    assert receipt["anomaly_score"] is None
    blob = json.dumps(attached)
    assert all(token not in blob.casefold() for token in FORBIDDEN)


def test_topic_match_and_unmatched_rows_never_invent_joins(tmp_path):
    features = _write_features(tmp_path / "common-crawl-features.jsonl", [_feature_row()])
    config = archive_context.load_config(CONFIG)
    rows, digest = archive_context.load_feature_rows(features, config)

    attached = archive_context.attach_derived_archive_context(
        [_wire_obs(), _unmatched_obs()],
        feature_rows=rows,
        feature_export_sha256=digest,
        config=config,
    )

    assert attached[0]["archive_context_match"]["match_kind"] == "topic"
    assert attached[0]["archive_context_match"]["public_copy"] == (
        archive_context.TOPIC_PUBLIC_COPY
    )
    assert "archive_context" not in attached[1]


def test_point_in_time_join_refuses_later_feature_rows(tmp_path):
    late = _feature_row(
        last_capture_at="2026-08-21T00:00:00Z",
        available_at="2026-08-21T00:00:00Z",
    )
    features = _write_features(tmp_path / "common-crawl-features.jsonl", [late])
    config = archive_context.load_config(CONFIG)
    rows, digest = archive_context.load_feature_rows(features, config)
    attached = archive_context.attach_derived_archive_context(
        [_official_obs()],
        feature_rows=rows,
        feature_export_sha256=digest,
        config=config,
    )
    assert "archive_context" not in attached[0]


def test_public_reading_reuses_schema_and_abstains_without_features(tmp_path, monkeypatch):
    import scripts.archive_news_context_pull as pull

    monkeypatch.setattr(pull, "KillSwitch", lambda: type("K", (), {"is_halted": lambda self: False})())
    readings = tmp_path / "readings"
    readings.mkdir()
    (readings / "official-first-seen-latest.json").write_text(
        json.dumps({
            "generated_at": "2026-08-20T06:00:00Z",
            "source": "official landings",
            "method": "keyless GET",
            "scope": "landings",
            "n_observations": 1,
            "observations": [_official_obs()],
        }),
        encoding="utf-8",
    )
    (readings / "news-wire-live-latest.json").write_text(
        json.dumps({
            "generated_at": "2026-08-20T01:00:00Z",
            "source": "wire",
            "method": "rss",
            "scope": "metadata",
            "n_observations": 1,
            "observations": [_wire_obs()],
        }),
        encoding="utf-8",
    )
    (readings / "public-deletion-ledgers-latest.json").write_text(
        json.dumps({
            "generated_at": "2026-08-20T02:00:00Z",
            "source": "ledgers",
            "method": "rss",
            "scope": "ledgers",
            "n_observations": 1,
            "observations": [_unmatched_obs()],
        }),
        encoding="utf-8",
    )

    assert pull.main(root=tmp_path) is None

    features = _write_features(tmp_path / "common-crawl-features.jsonl", [_feature_row()])
    monkeypatch.setenv("PALIMPSEST_COMMON_CRAWL_FEATURES", str(features))
    result = pull.main(
        root=tmp_path,
        now=datetime(2026, 8, 20, 7, tzinfo=UTC),
    )
    assert result is not None
    document = json.loads((readings / "archive-news-context-latest.json").read_text())
    assert document["schema_version"] == archive_context.CONTEXT_SCHEMA_VERSION
    assert document["method"] == CONTEXT_METHOD
    assert document["n_observations_considered"] == 3
    assert document["n_observations_joined"] == 2
    assert document["n_events_contextualized"] == 2
    kinds = {event["match_kind"] for event in document["events"]}
    assert kinds == {"host", "topic"}
    copies = {event["public_copy"] for event in document["events"]}
    assert copies == {
        archive_context.HOST_PUBLIC_COPY,
        archive_context.TOPIC_PUBLIC_COPY,
    }
    blob = json.dumps(document)
    assert all(token not in blob.casefold() for token in FORBIDDEN)
    assert "warc_filename" not in blob
    assert "warc_record" not in blob
    assert "https://www.pbc.gov.cn/" in blob
    assert document["events"][0]["archive_context"][0]["anomaly_state"] == "warming_up"
    assert document["publication_policy"]["automatic_publication"] == "prohibited"


def test_repo_does_not_commit_a_fake_live_latest_file():
    assert not (ROOT / "readings" / "archive-news-context-latest.json").exists()


def test_raw_text_policy_and_prequential_warmup_are_unchanged():
    source = (ROOT / "collectors" / "common_crawl_lake.py").read_text(encoding="utf-8")
    assert (
        'raw_text_policy": "excluded-unless-a-separate-rights-review-allows-it"'
        in source
    )
    assert '"minimum_prior_crawls": 6' in source
    assert "if value is None or len(history) < 6:" in source
    assert MODEL_ID == "prequential-robust-mad/v1"
