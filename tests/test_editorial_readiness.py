"""Publication-profile gates combine machine evidence and human receipts."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.corroboration import build_corroboration
from core.editorial_readiness import (
    EditorialReadinessError,
    build_editorial_readiness,
    load_editorial_packages,
    validate_editorial_readiness,
)
from core.evidence_documents import EvidenceDocumentStore
from core.primary_documents import collect_primary_documents, load_primary_source_registry
from core.source_workflow import summarize_source_workflow
from scripts.build_editorial_readiness import render_standards_page


ROOT = Path(__file__).resolve().parents[1]


def _json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _primary(tmp_path):
    registry = load_primary_source_registry()

    def fetch(url, **_kwargs):
        source = next(row for row in registry.sources if row.url == url)
        if source.media_type == "application/pdf":
            return f"%PDF-1.7\n{source.id}\n%%EOF\n".encode()
        if source.media_type == "application/json":
            return json.dumps({"source": source.id}).encode()
        return f"<!doctype html><html><body>{source.id}</body></html>".encode()

    return collect_primary_documents(
        registry,
        fetch,
        EvidenceDocumentStore(
            tmp_path / "documents",
            acceptance_clock=lambda request: request["metadata"]["collected_at"],
        ),
        now=datetime(2026, 8, 12, 10, tzinfo=timezone.utc),
    )


def _build(tmp_path, *, config=None):
    wire = _json("readings/newswire-latest.json")
    investigations = _json("readings/investigations-latest.json")
    primary = _primary(tmp_path)
    corroboration = build_corroboration(wire, primary)
    config = config or load_editorial_packages()
    source = summarize_source_workflow(
        [],
        package_ids=[row["package_id"] for row in config["packages"]],
        generated_at=datetime(2026, 8, 12, 12, tzinfo=timezone.utc),
    )
    network_rounds = _json("readings/network-rounds-latest.json")
    return build_editorial_readiness(
        wire,
        primary,
        corroboration,
        investigations,
        network_rounds,
        source,
        config=config,
    ), primary


def test_current_wire_passes_its_attributed_single_source_profile(tmp_path):
    document, _ = _build(tmp_path)

    expected = _json("readings/newswire-latest.json")["n_events"]
    assert document["wire"]["n_events"] == expected
    assert document["wire"]["eligible_events"] == expected
    single = next(
        row for row in document["wire"]["events"] if row["n_independent_groups"] == 1
    )
    assert single["status"] == "eligible"
    assert single["evidence_label"].startswith("single-")
    assert single["failed_check_ids"] == []


def test_deeper_packages_remain_blocked_on_missing_reporting_not_story_count(tmp_path):
    document, _ = _build(tmp_path)
    expected = _json("readings/newswire-latest.json")["n_events"]

    assert document["summary"] == {
        "wire_events": expected,
        "wire_eligible": expected,
        "wire_blocked": 0,
        "explainers": 1,
        "explainers_publishable": 0,
        "investigations": 2,
        "investigations_publishable": 0,
    }
    economy = next(
        row for row in document["packages"] if row["package_id"] == "china-economy-explainer"
    )
    assert {
        "primary-document",
        "historical-context",
        "expert-voice",
        "affected-voice",
        "explanatory-visual",
        "sentence-citations",
        "human-edit",
    } <= set(economy["failed_check_ids"])
    network = next(
        row
        for row in document["packages"]
        if row["package_id"] == "china-network-filtering-investigation"
    )
    assert "skeptical-expert-voice" in network["failed_check_ids"]
    assert "fact-check" in network["failed_check_ids"]
    assert "falsification-assessed" in network["failed_check_ids"]


def test_captured_release_counts_only_when_explicitly_attached(tmp_path):
    config = deepcopy(load_editorial_packages())
    primary = _primary(tmp_path)
    housing = next(
        row for row in primary["documents"] if row["source_id"] == "nbs-70-city-housing"
    )
    config["packages"][0]["primary_document_ids"] = [housing["document_id"]]
    # Reuse a separate store because the helper owns deterministic private state.
    document, _ = _build(tmp_path / "second", config=config)
    package = next(
        row for row in document["packages"] if row["package_id"] == "china-economy-explainer"
    )
    check = next(row for row in package["checks"] if row["check_id"] == "primary-document")
    assert check["passed"] is True
    assert package["publishable"] is False


def test_network_history_attestation_cannot_bypass_longitudinal_round_gate(tmp_path):
    config = deepcopy(load_editorial_packages())
    package = next(
        row
        for row in config["packages"]
        if row["package_id"] == "china-network-filtering-investigation"
    )
    package["historical_context"] = {
        "status": "complete",
        "citation_urls": [
            "https://palimpsest.info/readings/network-rounds-latest.json"
        ],
        "note": "The editor attached the current longitudinal ledger.",
    }

    document, _ = _build(tmp_path, config=config)
    assessed = next(
        row
        for row in document["packages"]
        if row["package_id"] == "china-network-filtering-investigation"
    )
    history = next(
        row for row in assessed["checks"] if row["check_id"] == "historical-context"
    )
    assert history["passed"] is False
    assert "0/3" in history["detail"]


def test_config_rejects_automatic_publication_and_fake_complete_visual(tmp_path):
    raw = _json("config/editorial_packages.json")
    raw["automatic_publication"] = True
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(EditorialReadinessError, match="automatic publication"):
        load_editorial_packages(path)

    raw["automatic_publication"] = False
    raw["packages"][0]["visual"]["status"] = "complete"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(EditorialReadinessError, match="needs type and URL"):
        load_editorial_packages(path)


def test_public_validator_rejects_gate_broadening(tmp_path):
    document, _ = _build(tmp_path)
    tampered = deepcopy(document)
    tampered["automatic_publication"] = True
    with pytest.raises(EditorialReadinessError, match="enabled"):
        validate_editorial_readiness(tampered)

    tampered = deepcopy(document)
    tampered["packages"][0]["checks"][0]["passed"] = True
    tampered["packages"][0]["failed_check_ids"].remove("primary-document")
    with pytest.raises(EditorialReadinessError, match="contradicts required"):
        validate_editorial_readiness(tampered)

    tampered = deepcopy(document)
    tampered["wire"]["checks"]["attribution"] -= 1
    with pytest.raises(EditorialReadinessError, match="check totals"):
        validate_editorial_readiness(tampered)

    tampered = deepcopy(document)
    tampered["generated_at"] = "2026-02-31T10:00:00Z"
    with pytest.raises(EditorialReadinessError, match="real timestamp"):
        validate_editorial_readiness(tampered)


def test_standards_page_exposes_failed_checks_and_structured_receipts(tmp_path):
    document, _ = _build(tmp_path)
    page = render_standards_page(document).decode("utf-8")

    assert "Evidence can nominate a story" in page
    assert "china-network-filtering-investigation" in page
    assert "fact-check" in page
    assert "/readings/editorial-readiness-latest.json" in page
    assert "Passing never publishes automatically" in page
