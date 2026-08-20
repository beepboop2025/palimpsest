"""Fixture tests for journalist-grade per-event analysis v2."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import event_analysis, event_brief


ROOT = Path(__file__).resolve().parent.parent
EVENT_ID = "event-" + "aa" * 12
ITEM_ID = "item-" + "cc" * 12
OFFICIAL_URL = "https://www.stats.gov.cn/sj/zxfb/202608/t20260820_1.html"
FORBIDDEN = event_brief.FORBIDDEN_CAUSAL


def _story(signal_id: str, statement: str) -> dict:
    return {
        "signal_id": signal_id,
        "headline": f"{signal_id} current reading",
        "status": "live",
        "url": f"https://palimpsest.info/news/{signal_id}/",
        "modified_at": "2026-08-20T01:55:29Z",
        "claim_fingerprint": "sha256:" + "ab" * 32,
        "metric": {
            "label": "index",
            "value": 59.3,
            "unit": "percent",
            "denominator": {"label": "completed measurements", "value": 268599},
        },
        "claims": [{"statement": statement}],
        "evidence": {
            "url": f"https://palimpsest.info/readings/{signal_id}-latest.json",
            "input": {"sha256": "cd" * 32},
            "source_timestamp": "2026-08-20T01:55:29Z",
        },
        "method": {"summary": "Aggregate collector reading used as topical context only.", "version": 1},
    }


def _event() -> dict:
    return {
        "event_id": EVENT_ID,
        "version_id": "eventv-" + "bb" * 12,
        "url": f"https://palimpsest.info/news/wire/{EVENT_ID}/",
        "headline": "NBS releases July figures",
        "dek": "National Bureau of Statistics published a monthly note.",
        "desk": "economy",
        "topics": ["economy", "policy"],
        "published_at": "2026-08-20T01:00:00Z",
        "updated_at": "2026-08-20T01:00:00Z",
        "lead": False,
        "lead_reason": "single-source",
        "evidence_strength": "single-source",
        "reported_facts": [
            {
                "statement": "China Digital Times published “NBS releases July figures”.",
                "attribution": "China Digital Times",
                "published_at": "2026-08-20T01:00:00Z",
                "evidence_item_id": ITEM_ID,
            }
        ],
        "evidence_refs": [
            {
                "item_id": ITEM_ID,
                "version_id": "itemv-" + "dd" * 12,
                "source_id": "china-digital-times",
                "source_name": "China Digital Times",
                "role": "media",
                "independence_group": "cdt",
                "title": "NBS releases July figures",
                "url": OFFICIAL_URL,
                "published_at": "2026-08-20T01:00:00Z",
            }
        ],
        "evidence_groups": [
            {
                "group_id": "cdt",
                "source_ids": ["china-digital-times"],
                "roles": ["media"],
            }
        ],
        "declared_links": {
            "relation": "topic-surface-only",
            "scan_signal_ids": ["ooni-gfw", "vantage-fusion"],
            "economic_signal_ids": [],
        },
        "limitations": ["Feed metadata only."],
        "mutation": {"kind": "new", "previous_version_id": None},
    }


def _item() -> dict:
    return {
        "item_id": ITEM_ID,
        "title": "NBS releases July figures",
        "excerpt": "A monthly statistical note.",
        "feed_sha256": "ee" * 32,
        "source_id": "china-digital-times",
    }


def _wire(event: dict) -> dict:
    return {
        "schema_version": "palimpsest-newswire.v1",
        "items": [_item()],
        "events": [event],
    }


def _feed() -> dict:
    return {
        "schema_version": "palimpsest-news.v1",
        "generated_at": "2026-08-20T03:00:00Z",
        "stories": [
            _story(
                "ooni-gfw",
                "The current OONI aggregate reports a 59.3% anomaly index across 268,599 completed China measurements.",
            ),
            _story(
                "vantage-fusion",
                "The fused vantage index is current and remains method-specific, not a national traffic share.",
            ),
        ],
    }


def _official_reading() -> dict:
    return {
        "generated_at": "2026-08-20T06:00:00Z",
        "n_observations": 1,
        "pages": {
            OFFICIAL_URL: {
                "content_sha256": "11" * 32,
                "first_seen": "2026-08-01T00:00:00Z",
                "last_confirmed_alive": "2026-08-20T06:00:00Z",
                "last_status": 200,
                "last_event": "rewrite",
                "term": "NBS",
            }
        },
        "observations": [
            {
                "title": "[official:rewrite] NBS",
                "url": OFFICIAL_URL,
                "source": "official_first_seen",
                "first_seen": "2026-08-01T00:00:00Z",
                "last_confirmed_alive": "2026-08-20T06:00:00Z",
                "content_sha256": "11" * 32,
                "deletion_signal": "rewrite",
                "detected_at": "2026-08-20T06:00:00Z",
            }
        ],
    }


def _ledger_reading() -> dict:
    return {
        "generated_at": "2026-08-20T04:00:00Z",
        "n_observations": 1,
        "observations": [
            {
                "title": "NBS page recorded on a public ledger",
                "url": OFFICIAL_URL,
                "source": "ledger:cdt_english_root",
                "ledger_kind": "cdt",
                "terms": ["economy"],
                "detected_at": "2026-08-20T03:30:00Z",
                "first_seen": "2026-08-20T03:30:00Z",
            }
        ],
    }


def _wire_reading() -> dict:
    return {
        "generated_at": "2026-08-20T02:00:00Z",
        "n_observations": 1,
        "observations": [
            {
                "title": "NBS releases July figures",
                "url": OFFICIAL_URL,
                "source": "news-wire-live",
                "detected_at": "2026-08-20T01:00:00Z",
                "topics": ["economy", "policy"],
                "provenance": {"event_id": EVENT_ID},
            }
        ],
    }


def _undertext_reading() -> dict:
    return {
        "generated_at": "2026-08-20T05:00:00Z",
        "n_observations": 4,
        "observations": [
            {
                "title": "Fused public-archive observation",
                "url": OFFICIAL_URL,
                "source": "undertext:fusion",
                "topics": ["economy"],
                "detected_at": "2026-08-20T05:00:00Z",
            }
        ],
    }


def _archive_context() -> dict:
    return {
        "schema_version": "palimpsest-archive-news-context/v1",
        "generated_at": "2026-08-20T06:30:00Z",
        "events": [
            {
                "event_id": EVENT_ID,
                "event_url": f"https://palimpsest.info/news/wire/{EVENT_ID}/",
                "published_at": "2026-08-20T01:00:00Z",
                "archive_context": [
                    {
                        "target_id": "nbs",
                        "host": "www.stats.gov.cn",
                        "crawl": "CC-MAIN-2026-30",
                        "last_capture_at": "2026-07-24T12:30:00Z",
                        "unique_urls": 12,
                        "mutation_rate": None,
                        "archive_gap_rate": 0.0,
                        "anomaly_state": "warming_up",
                        "anomaly_score": None,
                        "absence_semantics": "archive-coverage-gap-not-deletion",
                    }
                ],
            }
        ],
    }


def _all_families() -> dict:
    return {
        "official-first-seen": _official_reading(),
        "public-deletion-ledgers": _ledger_reading(),
        "news-wire-live": _wire_reading(),
        "undertext": _undertext_reading(),
    }


def _build(**kwargs):
    event = _event()
    return event_analysis.build_event_analysis(
        event,
        wire=_wire(event),
        feed=_feed(),
        **kwargs,
    )


def _cited_texts(analysis: dict) -> list[str]:
    texts = [analysis["position"], *analysis["rationale"], *analysis["limitations"]]
    for layer in analysis["brief"].values():
        texts.extend(item["text"] for item in layer["sentences"])
    for field in ("counterreadings", "unknowns"):
        texts.extend(item["text"] for item in analysis[field])
    for row in analysis["surface_context"]:
        texts.extend((row["headline"], row["finding"], row["interpretation"]))
    return texts


def test_thick_brief_when_all_surfaces_present() -> None:
    analysis = _build(
        live_families=_all_families(),
        archive_context=_archive_context(),
    )

    event_analysis.validate_event_analysis(analysis, event=_event())
    assert analysis["schema_version"] == "palimpsest-event-analysis.v2"
    assert analysis["url"] == f"https://palimpsest.info/news/wire/{EVENT_ID}/analysis.json"
    assert analysis["declared_links"]["live_family_ids"] == list(event_brief.LIVE_FAMILY_IDS)
    assert analysis["brief"]["lead"]["status"] == "present"
    assert analysis["brief"]["timeline"]["status"] == "present"
    assert analysis["brief"]["official_page"]["status"] == "present"
    assert analysis["brief"]["deletion_ledger"]["status"] == "present"
    assert analysis["brief"]["pipe_context"]["status"] == "present"
    assert analysis["brief"]["archive_context"]["status"] == "present"
    assert "China Digital Times published" in analysis["brief"]["lead"]["sentences"][0]["text"]
    assert "peer observation" in analysis["brief"]["deletion_ledger"]["sentences"][0]["text"]
    assert "not a Palimpsest-verified deletion" in analysis["brief"]["deletion_ledger"]["sentences"][0]["text"]
    assert "does not state why the page changed" in " ".join(
        item["text"] for item in analysis["brief"]["official_page"]["sentences"]
    )
    assert "not a claim that this article was blocked" in " ".join(
        item["text"] for item in analysis["brief"]["pipe_context"]["sentences"]
    )
    assert "archive-coverage-gap-not-deletion" in " ".join(
        item["text"] for item in analysis["brief"]["archive_context"]["sentences"]
    )
    assert "warming_up" in " ".join(
        item["text"] for item in analysis["brief"]["archive_context"]["sentences"]
    )
    statuses = {row["surface_id"]: row["status"] for row in analysis["surface_context"]}
    assert statuses["official-first-seen"] == "live"
    assert statuses["public-deletion-ledgers"] == "live"
    assert statuses["news-wire-live"] == "live"
    assert statuses["undertext"] == "live"
    assert statuses["archive-news-context"] == "live"
    assert analysis["publication_receipt"]["citation_coverage"] == 1.0
    assert analysis["publication_receipt"]["automatic_publication"] is False
    assert analysis["publication_receipt"]["human_review_required"] is True
    assert analysis["authorship"]["freeform_model_generation"] == "none"


def test_layers_abstain_when_lake_ledger_and_official_are_missing() -> None:
    analysis = _build(live_families=None, archive_context=None)

    event_analysis.validate_event_analysis(analysis, event=_event())
    assert analysis["brief"]["official_page"]["status"] == "abstained"
    assert analysis["brief"]["deletion_ledger"]["status"] == "abstained"
    assert analysis["brief"]["archive_context"]["status"] == "abstained"
    assert analysis["brief"]["timeline"]["status"] == "abstained"
    assert analysis["brief"]["pipe_context"]["status"] == "present"
    statuses = {row["surface_id"]: row["status"] for row in analysis["surface_context"]}
    assert statuses["official-first-seen"] == "missing"
    assert statuses["public-deletion-ledgers"] == "missing"
    assert statuses["news-wire-live"] == "missing"
    assert statuses["undertext"] == "missing"
    assert statuses["archive-news-context"] == "missing"
    blob = " ".join(_cited_texts(analysis)).casefold()
    assert "withheld" in blob or "missing" in blob
    assert "first_seen=" not in blob
    assert analysis["disposition"] == "collector-context"


def test_emitted_text_has_no_causal_or_motive_verbs() -> None:
    analysis = _build(
        live_families=_all_families(),
        archive_context=_archive_context(),
    )
    blob = " ".join(_cited_texts(analysis)).casefold()
    hits = [token for token in FORBIDDEN if token in blob]
    assert hits == []
    assert "motive" not in blob
    assert "the party" not in blob
    assert "the censor" not in blob
    assert event_brief.causal_hits(analysis) == []


def test_citation_coverage_is_complete() -> None:
    analysis = _build(
        live_families=_all_families(),
        archive_context=_archive_context(),
    )
    evidence_ids = {row["evidence_id"] for row in analysis["evidence"]}
    sentences = [
        sentence
        for layer in analysis["brief"].values()
        for sentence in layer["sentences"]
    ]
    assert sentences
    assert all(sentence["citation_ids"] for sentence in sentences)
    assert all(
        citation in evidence_ids
        for sentence in sentences
        for citation in sentence["citation_ids"]
    )
    assert analysis["publication_receipt"]["citation_coverage"] == 1.0
    assert analysis["publication_receipt"]["gates"]


def test_pipe_context_stays_undeclared_without_those_signal_ids() -> None:
    event = _event()
    event["declared_links"]["scan_signal_ids"] = []
    analysis = event_analysis.build_event_analysis(
        event,
        wire=_wire(event),
        feed=_feed(),
        live_families=_all_families(),
        archive_context=_archive_context(),
    )
    assert analysis["brief"]["pipe_context"]["status"] == "not-declared"
    assert analysis["collector_context"] == []
    assert analysis["disposition"] == "source-assessment"


def test_v1_documents_still_validate() -> None:
    path = ROOT / "news/wire/event-08f6cb378e35cb5da762e260/analysis.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == "palimpsest-event-analysis.v1"
    event_analysis.validate_event_analysis(document)


def test_generated_v2_conforms_to_public_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "protocol/event-analysis-v2.schema.json").read_text())
    analysis = _build(
        live_families=_all_families(),
        archive_context=_archive_context(),
    )
    jsonschema.Draft202012Validator(schema).validate(analysis)


def test_no_fake_live_latest_files_were_committed() -> None:
    readings = ROOT / "readings"
    for name in (
        "official-first-seen-latest.json",
        "public-deletion-ledgers-latest.json",
        "news-wire-live-latest.json",
        "undertext-latest.json",
        "archive-news-context-latest.json",
    ):
        assert not (readings / name).is_file()
