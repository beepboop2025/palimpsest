"""Fixture tests for journalist-grade per-event analysis v2."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core import event_analysis, event_brief
from scripts import build_newsroom


ROOT = Path(__file__).resolve().parent.parent


def _protocol_validator(schema_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    resources = []
    for path in (ROOT / "protocol").glob("*.schema.json"):
        document = json.loads(path.read_text())
        schema_id = document.get("$id")
        if schema_id:
            resources.append((schema_id, referencing.Resource.from_contents(document)))
    registry = referencing.Registry().with_resources(resources)
    schema = json.loads(schema_path.read_text())
    return jsonschema.Draft202012Validator(schema, registry=registry)


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
        "method": {
            "summary": "Aggregate collector reading used as topical context only.",
            "version": 1,
        },
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
    assert (
        analysis["url"] == f"https://palimpsest.info/news/wire/{EVENT_ID}/analysis.json"
    )
    assert analysis["declared_links"]["live_family_ids"] == list(
        event_brief.LIVE_FAMILY_IDS
    )
    assert analysis["brief"]["lead"]["status"] == "present"
    assert analysis["brief"]["timeline"]["status"] == "present"
    assert analysis["brief"]["official_page"]["status"] == "present"
    assert analysis["brief"]["deletion_ledger"]["status"] == "present"
    assert analysis["brief"]["pipe_context"]["status"] == "present"
    assert analysis["brief"]["archive_context"]["status"] == "present"
    assert (
        "China Digital Times published"
        in analysis["brief"]["lead"]["sentences"][0]["text"]
    )
    assert (
        "peer observation"
        in analysis["brief"]["deletion_ledger"]["sentences"][0]["text"]
    )
    assert (
        "not a Palimpsest-verified deletion"
        in analysis["brief"]["deletion_ledger"]["sentences"][0]["text"]
    )
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
    assert analysis["archive_news_context"]["matched"] is True
    assert analysis["archive_news_context"]["match_kind"] == "event-id"
    assert analysis["archive_news_context"]["anomaly_state"] == "warming_up"
    assert analysis["archive_news_context"]["anomaly_score_published"] is False
    assert analysis["corroboration"]["official_page"] == "none-reviewed"
    assert analysis["window_peers"]["relation"] == "topic-surface-only"
    assert "warming_up" in analysis["position"]
    assert "none-reviewed" in analysis["position"]
    assert "structurally corroborated" not in analysis["position"]


def test_layers_abstain_when_lake_ledger_and_official_are_missing() -> None:
    analysis = _build(live_families=None, archive_context=None)

    event_analysis.validate_event_analysis(analysis, event=_event())
    assert analysis["brief"]["official_page"]["status"] == "none-reviewed"
    assert analysis["corroboration"]["official_page"] == "none-reviewed"
    assert analysis["corroboration"]["accepted_edges"] == 0
    assert analysis["corroboration"]["reviewed"] == 0
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


def test_unmatched_refresh_is_not_an_event_revision() -> None:
    first_undertext = _undertext_reading()
    first_undertext["observations"] = [
        {
            "title": "Unrelated archive observation",
            "url": "https://example.net/unrelated",
            "source": "undertext:fusion",
            "topics": ["unrelated"],
            "detected_at": "2026-08-20T05:00:00Z",
        }
    ]
    second_undertext = copy.deepcopy(first_undertext)
    second_undertext["generated_at"] = "2026-08-21T05:00:00Z"
    second_undertext["observations"].append(
        {
            "title": "Another unrelated observation",
            "url": "https://example.org/elsewhere",
            "source": "undertext:fusion",
            "topics": ["unrelated"],
            "detected_at": "2026-08-21T05:00:00Z",
        }
    )

    first = _build(live_families={"undertext": first_undertext})
    second = _build(live_families={"undertext": second_undertext})

    assert first == second
    undertext = next(
        row for row in first["surface_context"] if row["surface_id"] == "undertext"
    )
    assert undertext["status"] == "unmatched"
    assert undertext["source_timestamp"] is None
    assert undertext["input_sha256"] is None


def test_topic_only_collector_refresh_is_edition_only() -> None:
    event = _event()
    first_feed = _feed()
    second_feed = copy.deepcopy(first_feed)
    second_feed["stories"][0]["claims"][0]["statement"] += " New edition."
    second_feed["stories"][0]["claim_fingerprint"] = "sha256:" + "99" * 32
    first = event_analysis.build_event_analysis(
        event, wire=_wire(event), feed=first_feed
    )
    second = event_analysis.build_event_analysis(
        event, wire=_wire(event), feed=second_feed
    )

    assert first["analysis_id"] != second["analysis_id"]
    assert event_analysis.semantically_equivalent(first, second)


def test_lazy_migration_reuses_the_prior_exact_byte_revision(tmp_path: Path) -> None:
    event = _event()
    first_feed = _feed()
    second_feed = copy.deepcopy(first_feed)
    second_feed["stories"][0]["claims"][0]["statement"] += " New edition."
    second_feed["stories"][0]["claim_fingerprint"] = "sha256:" + "99" * 32
    previous = event_analysis.build_event_analysis(
        event, wire=_wire(event), feed=first_feed
    )
    candidate = event_analysis.build_event_analysis(
        event, wire=_wire(event), feed=second_feed
    )
    base = tmp_path / "news" / "wire" / event["event_id"]
    revision = base / "analysis" / "revisions" / f"{previous['analysis_id']}.json"
    revision.parent.mkdir(parents=True)
    raw = build_newsroom._pretty_json(previous)
    revision.write_bytes(raw)
    (base / "analysis.json").write_bytes(raw)

    retained = build_newsroom._retain_semantically_unchanged_event_analysis(
        event, candidate, archive_root=tmp_path
    )

    assert retained == previous


def test_event_version_rollover_publishes_the_new_analysis_revision(
    tmp_path: Path,
) -> None:
    previous_event = _event()
    current_event = copy.deepcopy(previous_event)
    current_event["version_id"] = "eventv-" + "ef" * 12
    current_event["updated_at"] = "2026-08-21T01:00:00Z"
    current_event["mutation"] = {
        "kind": "updated",
        "previous_version_id": previous_event["version_id"],
    }
    previous = event_analysis.build_event_analysis(
        previous_event, wire=_wire(previous_event), feed=_feed()
    )
    candidate = event_analysis.build_event_analysis(
        current_event, wire=_wire(current_event), feed=_feed()
    )
    base = tmp_path / "news" / "wire" / current_event["event_id"]
    revision = base / "analysis" / "revisions" / f"{previous['analysis_id']}.json"
    revision.parent.mkdir(parents=True)
    raw = build_newsroom._pretty_json(previous)
    revision.write_bytes(raw)
    (base / "analysis.json").write_bytes(raw)

    retained = build_newsroom._retain_semantically_unchanged_event_analysis(
        current_event, candidate, archive_root=tmp_path
    )

    assert retained == candidate
    assert retained["event_version_id"] == current_event["version_id"]


def test_same_version_analysis_still_fails_closed_on_event_drift(
    tmp_path: Path,
) -> None:
    previous_event = _event()
    current_event = copy.deepcopy(previous_event)
    second_ref = copy.deepcopy(current_event["evidence_refs"][0])
    second_ref.update(
        {
            "item_id": "item-" + "12" * 12,
            "version_id": "itemv-" + "34" * 12,
            "source_id": "second-source",
            "source_name": "Second Source",
            "independence_group": "cdt",
        }
    )
    current_event["evidence_refs"].append(second_ref)
    current_event["evidence_groups"][0]["source_ids"].append("second-source")
    previous = event_analysis.build_event_analysis(
        previous_event, wire=_wire(previous_event), feed=_feed()
    )
    candidate = event_analysis.build_event_analysis(
        current_event, wire=_wire(current_event), feed=_feed()
    )
    base = tmp_path / "news" / "wire" / current_event["event_id"]
    revision = base / "analysis" / "revisions" / f"{previous['analysis_id']}.json"
    revision.parent.mkdir(parents=True)
    raw = build_newsroom._pretty_json(previous)
    revision.write_bytes(raw)
    (base / "analysis.json").write_bytes(raw)

    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="invalid current event analysis",
    ):
        build_newsroom._retain_semantically_unchanged_event_analysis(
            current_event, candidate, archive_root=tmp_path
        )


def test_corroboration_coverage_is_scoped_to_one_event() -> None:
    other_event = "event-" + "ef" * 12
    candidate_id = "candidate-" + "12" * 12
    document = {
        "events": [
            {
                "event_id": EVENT_ID,
                "candidate_ids": [],
                "accepted_candidate_ids": [],
                "accepted_document_ids": [],
            },
            {
                "event_id": other_event,
                "candidate_ids": [candidate_id],
                "accepted_candidate_ids": [candidate_id],
                "accepted_document_ids": ["document-elsewhere"],
            },
        ],
        "candidates": [
            {
                "candidate_id": candidate_id,
                "review": {"status": "accepted"},
            }
        ],
        "n_accepted_edges": 1,
        "n_events_with_primary_documents": 1,
        "n_reviewed_edges": 1,
    }

    assert event_brief.corroboration_coverage(document, event_id=EVENT_ID) == {
        "accepted_edges": 0,
        "primary_docs": 0,
        "reviewed": 0,
        "official_page": "none-reviewed",
        "status": "empty",
        "relation": "coverage-fact-only",
    }


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
    path = ROOT / "tests/fixtures/event_analysis/event-08f6cb378e35cb5da762e260-v1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == "palimpsest-event-analysis.v1"
    event_analysis.validate_event_analysis(document)
    published = json.loads(
        (ROOT / "news/wire/event-08f6cb378e35cb5da762e260/analysis.json").read_text()
    )
    event_analysis.validate_event_analysis(published)


def test_generated_v2_conforms_to_public_schema() -> None:
    analysis = _build(
        live_families=_all_families(),
        archive_context=_archive_context(),
    )
    _protocol_validator(ROOT / "protocol/event-analysis-v2.schema.json").validate(
        analysis
    )


def test_no_fake_live_latest_files_were_committed() -> None:
    """This change must not invent PR82/archive latest files to make briefs look live."""

    import subprocess

    tracked = subprocess.check_output(
        ["git", "ls-files", "readings"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    forbidden = {
        "readings/official-first-seen-latest.json",
        "readings/news-wire-live-latest.json",
        "readings/archive-news-context-latest.json",
    }
    assert forbidden.isdisjoint(tracked)
    assert "readings/event-analysis-latest.json" not in tracked


def test_single_independence_group_cannot_claim_structural_corroboration() -> None:
    analysis = _build()
    blob = " ".join(_cited_texts(analysis))
    assert "structurally corroborated" not in blob
    assert analysis["evidence_assessment"]["independent_groups"] == 1
    assert event_analysis.structural_quorum(_event()) is False


def test_two_independence_groups_may_use_structurally_corroborated() -> None:
    event = _event()
    other_id = "item-" + "ff" * 12
    event["evidence_strength"] = "multi-source"
    event["lead_reason"] = "multi-source"
    event["evidence_refs"].append(
        {
            "item_id": other_id,
            "version_id": "itemv-" + "11" * 12,
            "source_id": "reuters",
            "source_name": "Reuters",
            "role": "media",
            "independence_group": "reuters",
            "title": "NBS releases July figures",
            "url": "https://www.reuters.com/world/china/nbs-july/",
            "published_at": "2026-08-20T01:05:00Z",
        }
    )
    event["evidence_groups"].append(
        {
            "group_id": "reuters",
            "source_ids": ["reuters"],
            "roles": ["media"],
        }
    )
    wire = _wire(event)
    wire["items"].append(
        {
            "item_id": other_id,
            "title": "NBS releases July figures",
            "excerpt": "A second attributed report.",
            "feed_sha256": "99" * 32,
            "source_id": "reuters",
        }
    )
    analysis = event_analysis.build_event_analysis(
        event,
        wire=wire,
        feed=_feed(),
        live_families=_all_families(),
        archive_context=_archive_context(),
    )
    assert event_analysis.structural_quorum(event) is True
    assert "structural corroboration" in analysis["evidence_assessment"]["conclusion"]
    assert analysis["publication_receipt"]["automatic_publication"] is False


def test_same_window_peers_are_counts_and_names_only() -> None:
    event = _event()
    peer = _event()
    peer["event_id"] = "event-" + "bb" * 12
    peer["version_id"] = "eventv-" + "cc" * 12
    peer["url"] = f"https://palimpsest.info/news/wire/{peer['event_id']}/"
    peer["topics"] = ["economy"]
    peer["headline"] = "Peer economy note"
    wire = _wire(event)
    wire["events"].append(peer)
    analysis = event_analysis.build_event_analysis(event, wire=wire, feed=_feed())
    peers = analysis["window_peers"]
    assert peers["same_window_peer_count"] == 1
    assert peers["shared_topics"] == ["economy"]
    assert "china-digital-times" in peers["peer_source_ids"]
    assert "cdt" in peers["peer_independence_groups"]
    assert "1 same-window event" in analysis["position"]
    blob = json.dumps(peers)
    assert "http" not in blob
    assert "deleted because" not in blob


def test_rolling_peer_count_is_edition_only_but_peer_identity_is_semantic(
    tmp_path: Path,
) -> None:
    event = _event()

    def peer(identifier: str, *, source_id: str = "china-digital-times") -> dict:
        candidate = copy.deepcopy(event)
        candidate["event_id"] = f"event-{identifier * 24}"
        candidate["version_id"] = f"eventv-{identifier * 24}"
        candidate["url"] = (
            f"https://palimpsest.info/news/wire/{candidate['event_id']}/"
        )
        candidate["headline"] = f"Peer economy note {identifier}"
        candidate["topics"] = ["economy"]
        candidate["evidence_refs"][0]["source_id"] = source_id
        candidate["evidence_groups"][0]["source_ids"] = [source_id]
        candidate["evidence_groups"][0]["group_id"] = source_id
        return candidate

    first_wire = _wire(event)
    first_wire["events"].append(peer("b"))
    second_wire = copy.deepcopy(first_wire)
    second_wire["events"].append(peer("c"))
    previous = event_analysis.build_event_analysis(
        event, wire=first_wire, feed=_feed()
    )
    candidate = event_analysis.build_event_analysis(
        event, wire=second_wire, feed=_feed()
    )

    assert previous["window_peers"]["same_window_peer_count"] == 1
    assert candidate["window_peers"]["same_window_peer_count"] == 2
    assert previous["analysis_id"] != candidate["analysis_id"]
    assert event_analysis.semantically_equivalent(previous, candidate)

    base = tmp_path / "news" / "wire" / event["event_id"]
    revision = (
        base / "analysis" / "revisions" / f"{previous['analysis_id']}.json"
    )
    revision.parent.mkdir(parents=True)
    raw = build_newsroom._pretty_json(previous)
    revision.write_bytes(raw)
    (base / "analysis.json").write_bytes(raw)
    retained = build_newsroom._retain_semantically_unchanged_event_analysis(
        event, candidate, archive_root=tmp_path
    )
    assert retained == previous

    identity_wire = copy.deepcopy(first_wire)
    identity_wire["events"].append(peer("d", source_id="reuters"))
    identity_change = event_analysis.build_event_analysis(
        event, wire=identity_wire, feed=_feed()
    )
    assert not event_analysis.semantically_equivalent(previous, identity_change)


def test_same_topic_outside_interconnection_window_is_not_a_window_peer() -> None:
    event = _event()
    far = _event()
    far["event_id"] = "event-" + "dd" * 12
    far["version_id"] = "eventv-" + "ee" * 12
    far["url"] = f"https://palimpsest.info/news/wire/{far['event_id']}/"
    far["topics"] = ["economy"]
    far["headline"] = "Older economy note"
    far["published_at"] = "2026-08-18T00:00:00Z"
    wire = _wire(event)
    wire["events"].append(far)
    analysis = event_analysis.build_event_analysis(event, wire=wire, feed=_feed())
    peers = analysis["window_peers"]
    assert peers["same_window_peer_count"] == 0
    assert peers["shared_topics"] == []


def test_missing_newsroom_feed_abstains_collectors_instead_of_inventing() -> None:
    event = _event()
    analysis = event_analysis.build_event_analysis(
        event,
        wire=_wire(event),
        feed={"schema_version": "palimpsest-news.v1", "stories": []},
        allow_missing_collectors=True,
    )
    assert analysis["disposition"] == "collector-abstention"
    assert {row["status"] for row in analysis["collector_context"]} == {"missing"}
    assert analysis["publication_receipt"]["automatic_publication"] is False


def test_warming_up_archive_does_not_publish_a_mad_score() -> None:
    analysis = _build(archive_context=_archive_context())
    assert analysis["archive_news_context"]["anomaly_state"] == "warming_up"
    assert analysis["archive_news_context"]["anomaly_score_published"] is False
    blob = " ".join(_cited_texts(analysis)).casefold()
    assert "prequential-robust-mad" in blob
    assert "anomaly score is published" in blob
    assert "anomaly_score=" not in blob


def test_live_script_refuses_to_write_a_git_readings_latest_file() -> None:
    from scripts import event_analysis_live

    code = event_analysis_live.main(
        [
            "--wire",
            str(ROOT / "readings" / "missing-wire.json"),
            "--output",
            str(ROOT / "readings" / "event-analysis-latest.json"),
        ]
    )
    assert code == 3
    assert not (ROOT / "readings" / "event-analysis-latest.json").exists()


def test_live_script_abstains_when_the_wire_is_missing(tmp_path: Path) -> None:
    from scripts import event_analysis_live

    code = event_analysis_live.main(
        [
            "--wire",
            str(tmp_path / "newswire-latest.json"),
            "--output",
            str(tmp_path / "event-analysis-latest.json"),
        ]
    )
    assert code == 2
    assert not (tmp_path / "event-analysis-latest.json").exists()
