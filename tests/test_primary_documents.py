"""Offline contracts for the primary-document capture plane."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.evidence_documents import EvidenceDocumentStore
from core.primary_documents import (
    InvalidPrimaryDocument,
    PrimaryDocumentError,
    PrimaryDocumentRegistryError,
    collect_primary_documents,
    load_primary_source_registry,
    validate_primary_document_index,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "primary_document_sources.json"
SCHEMA = ROOT / "protocol" / "primary-documents-v1.schema.json"


def _body(source) -> bytes:
    if source.media_type == "application/pdf":
        return f"%PDF-1.7\nfixture:{source.id}\n%%EOF\n".encode()
    if source.media_type == "application/json":
        return json.dumps({"source": source.id, "version": 1}).encode()
    return (
        "<!doctype html><html><head><title>Primary release</title></head>"
        f"<body>{source.id}</body></html>"
    ).encode()


def _payloads(registry) -> dict[str, bytes]:
    return {source.url: _body(source) for source in registry.sources}


def _fetcher(payloads, failures=()):
    failures = set(failures)

    def fetch(url, **kwargs):
        assert kwargs["max_redirects"] == 0
        assert kwargs["max_bytes"] == 8 * 1024 * 1024
        if url in failures:
            raise OSError("fixture transport failure")
        return payloads[url]

    return fetch


def _store(tmp_path):
    return EvidenceDocumentStore(
        tmp_path / "private-evidence",
        acceptance_clock=lambda request: request["metadata"]["collected_at"],
    )


def _collect(tmp_path, *, now, previous=None, payloads=None, failures=()):
    registry = load_primary_source_registry(CONFIG)
    values = payloads or _payloads(registry)
    document = collect_primary_documents(
        registry,
        _fetcher(values, failures),
        _store(tmp_path),
        now=now,
        previous=previous,
    )
    return registry, document, values


def test_registry_covers_every_prioritized_primary_source_family():
    registry = load_primary_source_registry(CONFIG)
    ids = {source.id for source in registry.sources}

    assert len(ids) == 14
    assert {
        "nbs-70-city-housing",
        "nbs-national-macro",
        "pboc-credit-tsf",
        "gacc-trade",
        "mot-transport",
        "spb-parcels",
        "nea-electricity",
        "imf-portwatch",
        "sentinel5p-no2",
        "viirs-nightlights",
        "hkex-filings",
        "sse-filings",
        "szse-filings",
        "world-bank-enterprise-survey",
    } == ids
    assert all(source.rights["training_use"] == "metadata_only" for source in registry.sources)
    assert all(source.observation_state == "not_parsed" for source in registry.sources)


def test_registry_rejects_url_broadening_and_training_permission(tmp_path):
    raw = json.loads(CONFIG.read_text())
    raw["sources"][0]["url"] = "https://example.org/unreviewed"
    path = tmp_path / "broadened.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PrimaryDocumentRegistryError, match="broadens"):
        load_primary_source_registry(path)

    raw = json.loads(CONFIG.read_text())
    raw["sources"][0]["rights"]["training_use"] = "full_text"
    path.write_text(json.dumps(raw))
    with pytest.raises(PrimaryDocumentRegistryError, match="metadata_only"):
        load_primary_source_registry(path)


def test_first_capture_commits_exact_bytes_and_public_receipts(tmp_path):
    registry, document, payloads = _collect(
        tmp_path,
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )

    validate_primary_document_index(document, registry=registry)
    assert document["coverage"]["status"] == "healthy"
    assert document["coverage"]["counts"]["captured"] == 14
    assert document["n_documents"] == document["n_vintages"] == 14
    assert document["n_new_vintages"] == 14
    assert all(row["role"] == "primary" for row in document["documents"])
    assert all(row["current_vintage"]["revision"] == 0 for row in document["documents"])

    stored = list((tmp_path / "private-evidence" / "objects").rglob("*.bin"))
    assert len(stored) == len({body for body in payloads.values()})
    serialized = json.dumps(document)
    assert "private-evidence" not in serialized
    assert "%PDF" not in serialized


def test_public_receipt_matches_the_published_json_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    _, document, _ = _collect(
        tmp_path,
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(document)


def test_unchanged_capture_advances_check_clock_without_inventing_vintage(tmp_path):
    registry, first, payloads = _collect(
        tmp_path,
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    _, second, _ = _collect(
        tmp_path,
        now=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
        previous=first,
        payloads=payloads,
    )

    validate_primary_document_index(second, registry=registry)
    assert second["coverage"]["counts"]["unchanged"] == 14
    assert second["n_new_vintages"] == 0
    assert second["n_vintages"] == first["n_vintages"]
    for before, after in zip(first["documents"], second["documents"], strict=True):
        assert after["current_vintage"] == before["current_vintage"]
        assert after["vintages"] == before["vintages"]
        assert after["retrieval_count"] == 2
        assert after["last_checked_at"] == "2026-08-13T10:00:00Z"


def test_changed_bytes_append_a_linked_revision_even_after_content_reversion(tmp_path):
    registry, first, payloads = _collect(
        tmp_path,
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    source = next(source for source in registry.sources if source.id == "mot-transport")
    changed = dict(payloads)
    changed[source.url] = changed[source.url].replace(b"</body>", b" revision</body>")
    _, second, _ = _collect(
        tmp_path,
        now=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
        previous=first,
        payloads=changed,
    )
    _, third, _ = _collect(
        tmp_path,
        now=datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc),
        previous=second,
        payloads=payloads,
    )

    row = next(row for row in third["documents"] if row["source_id"] == source.id)
    assert [vintage["revision"] for vintage in row["vintages"]] == [0, 1, 2]
    assert row["vintages"][1]["supersedes_vintage_id"] == row["vintages"][0]["vintage_id"]
    assert row["vintages"][2]["supersedes_vintage_id"] == row["vintages"][1]["vintage_id"]
    assert row["vintages"][2]["content_sha256"] == row["vintages"][0]["content_sha256"]
    assert row["vintages"][2]["vintage_id"] != row["vintages"][0]["vintage_id"]


def test_fetch_failure_retains_last_good_without_advancing_its_check_clock(tmp_path):
    registry, first, payloads = _collect(
        tmp_path,
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    failed = registry.sources[0]
    _, second, _ = _collect(
        tmp_path,
        now=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
        previous=first,
        payloads=payloads,
        failures={failed.url},
    )

    assert second["coverage"]["status"] == "degraded"
    receipt = next(
        row for row in second["coverage"]["sources"] if row["source_id"] == failed.id
    )
    assert receipt["status"] == "fetch_error"
    assert receipt["document_available"] is True
    assert receipt["retained_last_good"] is True
    before = next(row for row in first["documents"] if row["source_id"] == failed.id)
    after = next(row for row in second["documents"] if row["source_id"] == failed.id)
    assert after == before


def test_registry_migration_can_correct_only_an_uncaptured_source(tmp_path):
    registry = load_primary_source_registry(CONFIG)
    source = next(row for row in registry.sources if row.id == "nbs-national-macro")
    payloads = _payloads(registry)
    first = collect_primary_documents(
        registry,
        _fetcher(payloads, failures={source.url}),
        _store(tmp_path),
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    corrected_url = "https://data.stats.gov.cn/english/easyquery.htm?Cn=C02"
    migrated = replace(
        registry,
        sources=tuple(
            replace(row, url=corrected_url) if row.id == source.id else row
            for row in registry.sources
        ),
        sha256="f" * 64,
    )
    migrated_payloads = {
        row.url: _body(row)
        for row in migrated.sources
    }

    second = collect_primary_documents(
        migrated,
        _fetcher(migrated_payloads),
        _store(tmp_path),
        now=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
        previous=first,
    )

    assert second["source_registry_sha256"] == "f" * 64
    assert second["n_documents"] == 14
    added = next(row for row in second["documents"] if row["source_id"] == source.id)
    assert added["original_url"] == corrected_url


def test_registry_migration_rejects_changes_to_a_retained_source(tmp_path):
    registry, first, payloads = _collect(
        tmp_path,
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    source = registry.sources[0]
    corrected_url = source.url + "?replacement=1"
    migrated = replace(
        registry,
        sources=tuple(
            replace(row, url=corrected_url) if row.id == source.id else row
            for row in registry.sources
        ),
        sha256="e" * 64,
    )
    migrated_payloads = dict(payloads)
    migrated_payloads[corrected_url] = _body(source)

    with pytest.raises(PrimaryDocumentError, match="retained source metadata"):
        collect_primary_documents(
            migrated,
            _fetcher(migrated_payloads),
            _store(tmp_path),
            now=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
            previous=first,
        )


def test_historical_release_keeps_publication_and_late_knowledge_clocks_separate(tmp_path):
    _, document, _ = _collect(
        tmp_path,
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    pboc = next(row for row in document["documents"] if row["source_id"] == "pboc-credit-tsf")
    vintage = pboc["current_vintage"]

    assert vintage["publication_time"] == "2024-01-15T10:32:51Z"
    assert vintage["first_retrieved_at"] == "2026-08-12T10:00:00Z"
    assert vintage["accepted_at"] == "2026-08-12T10:00:00Z"
    assert "backfill" in pboc["interpretation_limit"].lower()


def test_media_mismatch_and_access_interstitial_do_not_become_documents(tmp_path):
    registry = load_primary_source_registry(CONFIG)
    payloads = _payloads(registry)
    html_source = next(source for source in registry.sources if source.media_type == "text/html")
    payloads[html_source.url] = b"<!doctype html><html><body>Access denied captcha</body></html>"

    document = collect_primary_documents(
        registry,
        _fetcher(payloads),
        _store(tmp_path),
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    receipt = next(
        row for row in document["coverage"]["sources"] if row["source_id"] == html_source.id
    )
    assert receipt["status"] == "invalid_document"
    assert receipt["document_available"] is False
    assert html_source.id not in {row["source_id"] for row in document["documents"]}

    pdf_source = next(source for source in registry.sources if source.media_type == "application/pdf")
    with pytest.raises(InvalidPrimaryDocument, match="PDF"):
        from core.primary_documents import _validate_fetched_bytes

        _validate_fetched_bytes(b"<html></html>", pdf_source)


def test_public_validator_rejects_vintage_or_capability_tampering(tmp_path):
    registry, document, _ = _collect(
        tmp_path,
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    tampered = deepcopy(document)
    tampered["documents"][0]["observation_state"] = "live"
    with pytest.raises(PrimaryDocumentError, match="live-observation"):
        validate_primary_document_index(tampered, registry=registry)

    tampered = deepcopy(document)
    tampered["documents"][0]["current_vintage"]["content_sha256"] = "0" * 64
    with pytest.raises(PrimaryDocumentError):
        validate_primary_document_index(tampered, registry=registry)


def test_zero_valid_sources_without_a_last_good_index_fails_loud(tmp_path):
    registry = load_primary_source_registry(CONFIG)
    with pytest.raises(PrimaryDocumentError, match="zero primary sources"):
        collect_primary_documents(
            registry,
            _fetcher(_payloads(registry), failures={source.url for source in registry.sources}),
            _store(tmp_path),
            now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
        )
