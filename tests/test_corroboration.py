"""Human-reviewed primary-document joins never invent corroboration."""

from __future__ import annotations

import html
import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from core.corroboration import (
    CorroborationError,
    build_corroboration,
    load_decisions,
    validate_corroboration,
)
from core.evidence_documents import EvidenceDocumentStore
from core.newswire import SourceRegistry, collect_newswire, load_source_registry
from core.primary_documents import collect_primary_documents, load_primary_source_registry


def _body(source) -> bytes:
    if source.media_type == "application/pdf":
        return f"%PDF-1.7\nfixture:{source.id}\n%%EOF\n".encode()
    if source.media_type == "application/json":
        return json.dumps({"source": source.id}).encode()
    return f"<!doctype html><html><body>{source.id}</body></html>".encode()


def _primary(tmp_path):
    registry = load_primary_source_registry()
    payloads = {source.url: _body(source) for source in registry.sources}

    def fetch(url, **_kwargs):
        return payloads[url]

    return collect_primary_documents(
        registry,
        fetch,
        EvidenceDocumentStore(
            tmp_path / "documents",
            acceptance_clock=lambda request: request["metadata"]["collected_at"],
        ),
        now=datetime(2026, 8, 12, 10, tzinfo=timezone.utc),
    )


def _wire(
    *,
    title="China housing prices fall across major cities",
    url="https://www.scmp.com/economy/china-economy/article/fixture",
    published="Fri, 13 Feb 2026 10:00:00 +0000",
    now=datetime(2026, 2, 14, 10, tzinfo=timezone.utc),
):
    source = next(
        row
        for row in load_source_registry().sources
        if row.id == "scmp-china-economy"
    )
    registry = SourceRegistry(
        schema_version="palimpsest-news-sources.v1",
        window_hours=168,
        max_items_per_source=16,
        max_events=32,
        sources=(source,),
        sha256="0" * 64,
    )
    raw = (
        '<?xml version="1.0"?><rss version="2.0"><channel><item>'
        f"<title>{html.escape(title)}</title>"
        f"<link>{html.escape(url)}</link>"
        "<description>Official release context and independent reporting.</description>"
        f"<pubDate>{html.escape(published)}</pubDate>"
        "</item></channel></rss>"
    ).encode()
    return collect_newswire(
        registry,
        lambda _url, **_kwargs: raw,
        now=now,
    )


def _decision(candidate_id, status="accepted"):
    return {
        "candidate_id": candidate_id,
        "status": status,
        "reviewed_at": "2026-08-12T12:00:00Z",
        "reviewer_id": "editor-012345abcdef",
        "rationale": "The editor compared the release period, geography, and claim.",
    }


def test_subject_and_period_create_only_an_unreviewed_candidate(tmp_path):
    document = build_corroboration(_wire(), _primary(tmp_path))

    validate_corroboration(document)
    housing = next(
        row for row in document["candidates"] if row["source_id"] == "nbs-70-city-housing"
    )
    event = document["events"][0]
    assert housing["match_basis"] == ["subject-period-candidate"]
    assert housing["review"]["status"] == "unreviewed"
    assert housing["eligible_for_corroboration"] is True
    assert event["n_independent_groups"] == 1
    assert event["has_primary_document"] is False
    assert document["n_accepted_edges"] == 0


def test_accepted_review_adds_the_distinct_primary_group(tmp_path):
    wire = _wire()
    primary = _primary(tmp_path)
    draft = build_corroboration(wire, primary)
    candidate = next(
        row for row in draft["candidates"] if row["source_id"] == "nbs-70-city-housing"
    )
    document = build_corroboration(
        wire,
        primary,
        decisions=[_decision(candidate["candidate_id"])],
    )

    event = document["events"][0]
    assert document["n_accepted_edges"] == 1
    assert document["n_events_with_primary_documents"] == 1
    assert event["n_independent_groups"] == 2
    assert event["status"] == "corroborated"
    assert event["attached_primary_groups"] == ["nbs-housing-prices"]


def test_stale_or_unknown_editorial_decision_fails_closed(tmp_path):
    with pytest.raises(CorroborationError, match="stale or unknown"):
        build_corroboration(
            _wire(),
            _primary(tmp_path),
            decisions=[_decision("candidate-000000000000000000000000")],
        )


def test_catalog_metadata_can_never_be_accepted_as_corroboration(tmp_path):
    primary = _primary(tmp_path)
    # Subject and period make the catalog page a candidate while scope still
    # makes it ineligible to support a factual claim.
    wire = _wire(
        title="World Bank enterprise survey reports on business firms",
        published="Thu, 15 May 2025 01:00:00 +0000",
        now=datetime(2025, 5, 16, 10, tzinfo=timezone.utc),
    )
    draft = build_corroboration(wire, primary)
    candidate = next(
        row
        for row in draft["candidates"]
        if row["source_id"] == "world-bank-enterprise-survey"
    )
    assert candidate["capture_scope"] == "catalog_metadata"
    assert candidate["eligible_for_corroboration"] is False
    with pytest.raises(CorroborationError, match="ineligible"):
        build_corroboration(
            wire,
            primary,
            decisions=[_decision(candidate["candidate_id"])],
        )


def test_decisions_config_prohibits_automatic_confirmation(tmp_path):
    config = {
        "config_version": "palimpsest-corroboration-decisions.v1",
        "automatic_confirmation": True,
        "decisions": [],
    }
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(CorroborationError, match="prohibited"):
        load_decisions(path)


def test_public_validator_rejects_inflated_group_count(tmp_path):
    document = build_corroboration(_wire(), _primary(tmp_path))
    tampered = deepcopy(document)
    tampered["events"][0]["n_independent_groups"] = 99
    with pytest.raises(CorroborationError, match="group count"):
        validate_corroboration(tampered)


def test_public_validator_recomputes_candidate_independence(tmp_path):
    document = build_corroboration(_wire(), _primary(tmp_path))
    tampered = deepcopy(document)
    housing = next(
        row
        for row in tampered["candidates"]
        if row["source_id"] == "nbs-70-city-housing"
    )
    housing["independent_of_event"] = False

    with pytest.raises(CorroborationError, match="independence flag"):
        validate_corroboration(tampered)
