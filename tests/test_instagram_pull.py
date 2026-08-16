"""Publication and last-good tests for the Instagram social pull service."""

from __future__ import annotations

import hashlib
import json

from collectors import instagram_graph
from core import social_observations as social
from scripts import instagram_pull


def _paths(tmp_path):
    return (
        tmp_path / "latest.json",
        tmp_path / "versions.jsonl",
        tmp_path / "pull.lock",
    )


def _receipts(first_status="success"):
    config = instagram_graph.load_config(registry=social.load_source_registry())
    return [
        {
            "source_id": binding.source_id,
            "status": first_status if index == 0 else "not-attempted",
            "rejected": 0,
            "error_code": "instagram-source-failed"
            if index == 0 and first_status == "failure"
            else None,
        }
        for index, binding in enumerate(config.bindings)
    ]


def _record():
    return {
        "source_id": "cecc-instagram",
        "native_id": "17901234567890",
        "permalink": "https://www.instagram.com/p/ABC_123/",
        "published_at": "2026-08-16T10:00:00Z",
        "observed_at": "2026-08-16T12:00:00Z",
        "title": "CECC China briefing",
        "excerpt": "A bounded publisher caption.",
        "content_type": "image",
        "content_sha256": hashlib.sha256(b"caption").hexdigest(),
        "state": "published",
        "china_relevance_labels": ["china"],
        "related_urls": ["https://www.cecc.gov/events/hearing"],
    }


def _registry_without_cgtn(tmp_path):
    document = json.loads(
        (instagram_pull.ROOT / "config" / "social_sources.json").read_text()
    )
    document["sources"] = [
        source for source in document["sources"] if source["id"] != "cgtn-telegram"
    ]
    path = tmp_path / "old-social-sources.json"
    path.write_bytes(social.canonical_json_bytes(document))
    return social.load_source_registry(path)


def test_first_disabled_run_publishes_explicit_not_attempted_bootstrap(tmp_path):
    latest_path, ledger_path, lock_path = _paths(tmp_path)
    assert (
        instagram_pull.run(
            latest_path=latest_path,
            ledger_path=ledger_path,
            lock_path=lock_path,
            environment={},
            observed_at="2026-08-16T12:00:00Z",
        )
        == 0
    )

    document = json.loads(latest_path.read_text())
    social.validate_latest(document)
    assert document["n_observations"] == 0
    assert document["coverage"]["configured"] == 8
    assert {row["status"] for row in document["coverage"]["receipts"]} == {
        "not-attempted"
    }
    assert ledger_path.read_bytes() == b""


def test_disabled_run_preserves_existing_bytes(monkeypatch, tmp_path):
    latest_path, ledger_path, lock_path = _paths(tmp_path)
    instagram_pull.run(
        latest_path=latest_path,
        ledger_path=ledger_path,
        lock_path=lock_path,
        environment={},
        observed_at="2026-08-16T12:00:00Z",
    )
    before = latest_path.read_bytes()
    assert (
        instagram_pull.run(
            latest_path=latest_path,
            ledger_path=ledger_path,
            lock_path=lock_path,
            environment={},
            observed_at="2026-08-16T13:00:00Z",
        )
        == 0
    )
    assert latest_path.read_bytes() == before


def test_success_appends_version_then_publishes_latest(monkeypatch, tmp_path):
    latest_path, ledger_path, lock_path = _paths(tmp_path)
    monkeypatch.setattr(
        instagram_graph,
        "collect_from_environment",
        lambda **_kwargs: ([_record()], _receipts()),
    )
    assert (
        instagram_pull.run(
            latest_path=latest_path,
            ledger_path=ledger_path,
            lock_path=lock_path,
            environment={instagram_graph.ENABLED_ENV: "1"},
            observed_at="2026-08-16T12:00:00Z",
        )
        == 0
    )

    document = json.loads(latest_path.read_text())
    social.validate_latest(document)
    assert document["n_observations"] == 1
    assert document["observations"][0]["source_id"] == "cecc-instagram"
    assert "17901234567890" not in latest_path.read_text()
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    social.validate_ledger_rows(rows)
    assert len(rows) == 1


def test_total_attempt_failure_preserves_last_good(monkeypatch, tmp_path):
    latest_path, ledger_path, lock_path = _paths(tmp_path)
    monkeypatch.setattr(
        instagram_graph,
        "collect_from_environment",
        lambda **_kwargs: ([_record()], _receipts()),
    )
    instagram_pull.run(
        latest_path=latest_path,
        ledger_path=ledger_path,
        lock_path=lock_path,
        environment={instagram_graph.ENABLED_ENV: "1"},
        observed_at="2026-08-16T12:00:00Z",
    )
    before_latest = latest_path.read_bytes()
    before_ledger = ledger_path.read_bytes()
    monkeypatch.setattr(
        instagram_graph,
        "collect_from_environment",
        lambda **_kwargs: ([], _receipts("failure")),
    )
    assert (
        instagram_pull.run(
            latest_path=latest_path,
            ledger_path=ledger_path,
            lock_path=lock_path,
            environment={instagram_graph.ENABLED_ENV: "1"},
            observed_at="2026-08-16T13:00:00Z",
        )
        == 2
    )
    assert latest_path.read_bytes() == before_latest
    assert ledger_path.read_bytes() == before_ledger


def test_disabled_run_migrates_checked_seven_source_zero_state_additively(tmp_path):
    latest_path, ledger_path, lock_path = _paths(tmp_path)
    latest_path.write_bytes(
        (
            instagram_pull.ROOT / "readings" / "social-observations-latest.json"
        ).read_bytes()
    )
    ledger_path.write_bytes(b"")

    assert (
        instagram_pull.run(
            latest_path=latest_path,
            ledger_path=ledger_path,
            lock_path=lock_path,
            environment={},
            observed_at="2026-08-16T18:00:00Z",
        )
        == 0
    )
    migrated = json.loads(latest_path.read_text())
    registry = social.load_source_registry()
    social.validate_latest(migrated, registry)
    assert migrated["source_registry_sha256"] == registry.sha256
    assert migrated["coverage"]["configured"] == 8
    assert next(
        row
        for row in migrated["coverage"]["receipts"]
        if row["source_id"] == "cgtn-telegram"
    ) == {
        "source_id": "cgtn-telegram",
        "platform": "telegram",
        "status": "not-attempted",
        "accepted": 0,
        "rejected": 0,
        "error_code": None,
    }
    assert ledger_path.read_bytes() == b""


def test_disabled_additive_migration_preserves_populated_ledger(tmp_path):
    latest_path, ledger_path, lock_path = _paths(tmp_path)
    old_registry = _registry_without_cgtn(tmp_path)
    receipts = [
        {
            "source_id": source.id,
            "status": "success" if source.id == "cecc-instagram" else "not-attempted",
            "rejected": 0,
            "error_code": None,
        }
        for source in old_registry.sources
    ]
    old_latest, old_ledger = social.build_latest(
        [_record()],
        registry=old_registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=receipts,
    )
    latest_path.write_text(json.dumps(old_latest), encoding="utf-8")
    ledger_path.write_bytes(social.ledger_jsonl_bytes(old_ledger, old_registry))
    before_ledger = ledger_path.read_bytes()

    assert (
        instagram_pull.run(
            latest_path=latest_path,
            ledger_path=ledger_path,
            lock_path=lock_path,
            environment={},
            observed_at="2026-08-16T13:00:00Z",
        )
        == 0
    )
    migrated = json.loads(latest_path.read_text())
    social.validate_latest(migrated)
    assert migrated["observations"] == old_latest["observations"]
    assert ledger_path.read_bytes() == before_ledger
    social.validate_ledger_rows(
        [json.loads(line) for line in ledger_path.read_text().splitlines()]
    )
