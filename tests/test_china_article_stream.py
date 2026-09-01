"""Contracts for the one-entry-per-publisher China dispatch stream."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import china_analysis
from core import china_article_stream
from core import event_analysis
from core import newsroom
from core import newswire
from core import telegram_watch
from scripts import build_newsroom
from scripts import review_scamshield_summary


ROOT = Path(__file__).resolve().parent.parent


def _collect_synthetic_stream(
    source_id: str, rss: bytes
) -> tuple[dict, dict, dict]:
    registry = newswire.load_source_registry()
    source = next(source for source in registry.sources if source.id == source_id)

    def fetch(url: str, **_kwargs: object) -> bytes:
        if url == source.feed_url:
            return rss
        raise RuntimeError("synthetic source unavailable")

    wire = newswire.collect_newswire(
        registry,
        fetch,
        now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    analyses = event_analysis.build_event_analyses(
        wire,
        {"schema_version": "palimpsest-news.v1", "stories": []},
        allow_missing_collectors=True,
    )
    stream = china_article_stream.build_china_article_stream(wire, analyses)
    return wire, analyses, stream


def _synthetic_same_group_stream() -> tuple[dict, dict, dict]:
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        '<item><title>China publisher bulletin</title>'
        '<link>https://news.cgtn.com/news/2026-08-25/china-publisher-bulletin-a</link>'
        '<description>A bounded China report.</description>'
        '<pubDate>Tue, 25 Aug 2026 10:00:00 +0000</pubDate></item>'
        '<item><title>China publisher bulletin</title>'
        '<link>https://news.cgtn.com/news/2026-08-25/china-publisher-bulletin-b</link>'
        '<description>A second bounded China report.</description>'
        '<pubDate>Tue, 25 Aug 2026 10:01:00 +0000</pubDate></item>'
        '</channel></rss>'
    ).encode()
    return _collect_synthetic_stream("cgtn-china", rss)


def _synthetic_mixed_scope_stream() -> tuple[dict, dict, dict]:
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        '<item><title>China site policy update</title>'
        '<link>https://github.com/github/site-policy/commit/aaaaaaaa</link>'
        '<description>A China-related policy metadata update.</description>'
        '<pubDate>Tue, 25 Aug 2026 10:00:00 +0000</pubDate></item>'
        '<item><title>Site policy update</title>'
        '<link>https://github.com/github/site-policy/commit/bbbbbbbb</link>'
        '<description>A general policy metadata update.</description>'
        '<pubDate>Tue, 25 Aug 2026 10:01:00 +0000</pubDate></item>'
        '</channel></rss>'
    ).encode()
    return _collect_synthetic_stream("github-site-policy", rss)


@pytest.fixture(scope="module")
def publication():
    wire = json.loads((ROOT / "readings" / "newswire-latest.json").read_text())
    feed = newsroom.build_news_feed()
    analyses = event_analysis.build_event_analyses(wire, feed)
    stream = china_article_stream.build_china_article_stream(wire, analyses)
    return wire, feed, analyses, stream


def _private_summary() -> dict:
    return {
        "schema_version": "scamshield-telegram-monitoring-summary/v1",
        "producer": "ScamShield",
        "data_classification": "PRIVATE_ANALYST_REVIEW",
        "review_status": "HUMAN_REVIEW_REQUIRED",
        "publication_eligible": False,
        "intended_consumers": ["palimpsest_review"],
        "window": {
            "start": "2026-08-14T00:00:00Z",
            "end": "2026-08-15T00:00:00Z",
            "complete": True,
        },
        "sampling_frame": {
            "surface": "configured_public_or_operator_authorized_telegram",
            "universal_telegram_coverage": False,
            "raw_messages_included": False,
            "exact_iocs_included": False,
            "source_identifiers_included": False,
        },
        "coverage": {
            "messages_observed": 120,
            "messages_flagged": 8,
            "sources_observed": 4,
            "collection_errors": 1,
        },
        "detections": {
            "status": "AVAILABLE_FOR_REVIEW",
            "minimum_messages": 20,
            "minimum_sources": 2,
            "tier_counts": {"CLEAN": 112, "WATCH": 8},
            "family_counts": {"CYBER_FRAUD": 5, "NARCOTICS": 3},
        },
        "limitations": [
            "Counts describe classifier matches in a configured sample, not verified crimes or platform totals.",
            "No private access control is bypassed.",
            "Counts cannot establish proceeds, prevalence, guilt, ownership, or network membership.",
            "Human review remains mandatory.",
        ],
    }


def _reviewed_watch() -> dict:
    summary = _private_summary()
    raw = json.dumps(summary, sort_keys=True).encode()
    return review_scamshield_summary.promote_summary(
        summary,
        raw_sha256=telegram_watch.source_digest(raw),
        reviewed_at="2026-08-15T05:00:00Z",
        reviewer_role="china-desk-editor",
        review_note="Approved only as aggregate risk-pattern context after privacy and scope review.",
        china_families=["CYBER_FRAUD"],
    )


def test_stream_contains_each_and_only_china_relevant_wire_item(publication):
    wire, _feed, _analyses, stream = publication
    expected = {
        item["item_id"]
        for item in wire["items"]
        if newswire.is_china_relevant_item(item)
    }
    observed = {entry["entry_id"] for entry in stream["entries"]}

    assert observed == expected
    assert stream["n_entries"] == len(expected)
    assert stream["coverage"]["accepted_wire_items"] == wire["n_items"]
    assert stream["coverage"]["excluded_global_feed_items"] == wire["n_items"] - len(expected)
    assert [
        (entry["published_at"], entry["entry_id"])
        for entry in stream["entries"]
    ] == sorted(
        ((entry["published_at"], entry["entry_id"]) for entry in stream["entries"]),
        reverse=True,
    )


def test_each_item_reuses_exactly_one_validated_event_analysis(publication):
    wire, _feed, analyses, stream = publication
    items = {item["item_id"]: item for item in wire["items"]}
    event_for_item = {
        ref["item_id"]: event
        for event in wire["events"]
        for ref in event["evidence_refs"]
    }
    for entry in stream["entries"]:
        event = event_for_item[entry["entry_id"]]
        analysis = analyses[event["event_id"]]
        assert entry["dossier"]["event_id"] == event["event_id"]
        assert entry["analysis"]["analysis_id"] == analysis["analysis_id"]
        assert entry["analysis"]["position"] == analysis["position"]
        assert entry["analysis"]["next_checks"]

    entries_by_event: dict[str, list[dict]] = {}
    for entry in stream["entries"]:
        entries_by_event.setdefault(entry["dossier"]["event_id"], []).append(entry)

    for event in wire["events"]:
        expected_ids = {
            reference["item_id"]
            for reference in event["evidence_refs"]
            if newswire.is_china_relevant_item(items[reference["item_id"]])
        }
        matching = entries_by_event.get(event["event_id"], [])
        assert {entry["entry_id"] for entry in matching} == expected_ids
        if matching:
            assert {
                entry["dossier"]["independent_groups"] for entry in matching
            } == {len(event["evidence_groups"])}
            assert {entry["analysis"]["analysis_id"] for entry in matching} == {
                analyses[event["event_id"]]["analysis_id"]
            }


def test_runtime_validator_and_json_schemas_are_closed(publication):
    wire, _feed, analyses, stream = publication
    china_article_stream.validate_china_article_stream(stream)
    china_article_stream.validate_china_article_stream(
        stream, wire=wire, analyses=analyses
    )

    mutated = copy.deepcopy(stream)
    mutated["telegram_watch"]["raw_text"] = "must never publish"
    with pytest.raises(china_article_stream.ChinaArticleStreamError):
        china_article_stream.validate_china_article_stream(
            mutated, wire=wire, analyses=analyses
        )

    jsonschema = pytest.importorskip("jsonschema")
    stream_schema = json.loads(
        (ROOT / "protocol" / "china-article-stream-v1.schema.json").read_text()
    )
    watch_schema = json.loads(
        (ROOT / "protocol" / "telegram-watch-v1.schema.json").read_text()
    )
    # Inline the local public schema so the test never resolves a network ref.
    stream_schema["properties"]["telegram_watch"]["oneOf"][0] = watch_schema
    jsonschema.Draft202012Validator.check_schema(stream_schema)
    jsonschema.Draft202012Validator(stream_schema).validate(stream)


def test_runtime_validator_binds_entries_to_the_source_graph():
    wire, analyses, stream = _synthetic_same_group_stream()
    assert len(stream["entries"]) == 2
    assert len({row["dossier"]["event_id"] for row in stream["entries"]}) == 1
    china_article_stream.validate_china_article_stream(
        stream, wire=wire, analyses=analyses
    )

    forged_count = copy.deepcopy(stream)
    for entry in forged_count["entries"]:
        entry["dossier"]["source_items"] += 1
        entry["analysis"]["evidence_assessment"]["source_count"] += 1
    with pytest.raises(
        china_article_stream.ChinaArticleStreamError,
        match="source wire and event analysis",
    ):
        china_article_stream.validate_china_article_stream(
            forged_count, wire=wire, analyses=analyses
        )

    forged_analysis = copy.deepcopy(stream)
    current_id = forged_analysis["entries"][0]["analysis"]["analysis_id"]
    replacement = "analysisv-" + (
        "f" * 24 if current_id != "analysisv-" + "f" * 24 else "e" * 24
    )
    for entry in forged_analysis["entries"]:
        entry["analysis"]["analysis_id"] = replacement
    with pytest.raises(
        china_article_stream.ChinaArticleStreamError,
        match="source wire and event analysis",
    ):
        china_article_stream.validate_china_article_stream(
            forged_analysis, wire=wire, analyses=analyses
        )

    forged_position = copy.deepcopy(stream)
    for entry in forged_position["entries"]:
        entry["analysis"]["position"] = "A self-consistent but unsupported position."
    with pytest.raises(
        china_article_stream.ChinaArticleStreamError,
        match="source wire and event analysis",
    ):
        china_article_stream.validate_china_article_stream(
            forged_position, wire=wire, analyses=analyses
        )

    forged_coverage = copy.deepcopy(stream)
    forged_coverage["coverage"]["accepted_wire_items"] += 1
    with pytest.raises(
        china_article_stream.ChinaArticleStreamError,
        match="coverage does not match",
    ):
        china_article_stream.validate_china_article_stream(
            forged_coverage, wire=wire, analyses=analyses
        )


def test_runtime_validator_preserves_a_partial_event_projection():
    wire, analyses, stream = _synthetic_mixed_scope_stream()
    assert len(wire["items"]) == 2
    assert len(wire["events"]) == 1
    assert len(stream["entries"]) == 1
    assert stream["entries"][0]["dossier"]["source_items"] == 2
    china_article_stream.validate_china_article_stream(stream)
    china_article_stream.validate_china_article_stream(
        stream, wire=wire, analyses=analyses
    )


def test_rss_json_feed_and_html_publish_every_entry_with_analysis(publication):
    _wire, _feed, _analyses, stream = publication
    rss = ET.fromstring(build_newsroom.build_china_stream_rss(stream))
    rss_items = rss.findall("./channel/item")
    assert len(rss_items) == stream["n_entries"]
    assert len({item.findtext("guid") for item in rss_items}) == stream["n_entries"]
    first_description = rss_items[0].findtext("description")
    assert stream["entries"][0]["analysis"]["position"] in first_description
    assert "Next checks:" in first_description
    assert "Known unknowns:" in first_description

    json_feed = build_newsroom.build_china_stream_json_feed(stream)
    assert len(json_feed["items"]) == stream["n_entries"]
    assert json_feed["items"][0]["_palimpsest"]["analysis_id"] == (
        stream["entries"][0]["analysis"]["analysis_id"]
    )

    page = build_newsroom.render_china_article_stream(
        stream, entries=stream["entries"][:40], page=1, n_pages=7
    )
    assert page.startswith("<!doctype html>")
    assert '<main id="main">' in page
    assert page.count('class="cs-entry"') == 40
    assert "What Palimpsest adds" in page
    assert "Why this is the bounded position" in page
    assert "Next verification moves" in page
    assert "Known unknowns and method limits" in page
    assert "/news/china/feed.xml" in page
    assert "No Telegram signal is being smuggled in as fact" in page
    assert "innerHTML" not in page


def test_scamshield_requires_review_and_exposes_only_selected_aggregates(publication):
    wire, feed, analyses, _stream = publication
    watch = _reviewed_watch()
    telegram_watch.validate_telegram_watch(watch)
    stream = china_article_stream.build_china_article_stream(
        wire, analyses, telegram_watch=watch
    )
    encoded = china_article_stream.canonical_json_bytes(stream).decode()

    assert stream["generated_at"] == max(wire["generated_at"], watch["generated_at"])
    assert stream["telegram_watch"]["status"] == "REVIEWED_CONTEXT"
    assert stream["telegram_watch"]["detections"]["reviewed_china_family_counts"] == {
        "CYBER_FRAUD": 5
    }
    assert "NARCOTICS" not in encoded
    for forbidden in ("raw_text", "message_text", "source_pseudonym", "assessment_id"):
        assert forbidden not in encoded

    page = build_newsroom.render_china_article_stream(
        stream, entries=stream["entries"][:1]
    )
    assert "120" in page and "4" in page and "Cyber Fraud" in page
    assert "context only" in page.lower()

    summary = _private_summary()
    with pytest.raises(ValueError, match="absent"):
        review_scamshield_summary.promote_summary(
            summary,
            raw_sha256="a" * 64,
            reviewed_at="2026-08-15T05:00:00Z",
            reviewer_role="china-desk-editor",
            review_note="Reviewed and rejected ungrounded family selection.",
            china_families=["NOT_PRESENT"],
        )


def test_build_outputs_adds_paginated_stream_and_machine_formats(publication):
    wire, feed, _analyses, stream = publication
    outputs = build_newsroom.build_outputs(feed, wire=wire)
    n_pages = max(
        1,
        (stream["n_entries"] + build_newsroom.CHINA_STREAM_PAGE_SIZE - 1)
        // build_newsroom.CHINA_STREAM_PAGE_SIZE,
    )
    assert Path("news/china/index.html") in outputs
    assert Path("news/china/feed.xml") in outputs
    assert Path("news/china/feed.json") in outputs
    assert Path("readings/china-article-stream-latest.json") in outputs
    newsroom_index = outputs[Path("news/index.html")].decode()
    analysis_page_path = Path("news/china/analysis/index.html")
    analysis_json_path = build_newsroom.CHINA_ANALYSIS_JSON_FEED_RELATIVE
    analysis_rss_path = build_newsroom.CHINA_ANALYSIS_RSS_FEED_RELATIVE
    analysis_reading_path = build_newsroom.CHINA_ANALYSIS_READING_RELATIVE
    analysis_paths = {
        analysis_page_path,
        analysis_json_path,
        analysis_rss_path,
        analysis_reading_path,
    }
    analysis_denied = build_newsroom._china_public_values_denied(feed["generated_at"])
    if analysis_denied:
        denied_analysis_paths = {
            analysis_page_path,
            analysis_json_path,
            analysis_rss_path,
        }
        assert denied_analysis_paths <= outputs.keys()
        assert analysis_reading_path not in outputs
        assert json.loads(outputs[analysis_json_path]) == (
            build_newsroom.build_china_analysis_availability_json_feed(
                feed["generated_at"]
            )
        )
        assert outputs[analysis_rss_path] == (
            build_newsroom.build_china_analysis_availability_rss(
                feed["generated_at"]
            )
        )
        unavailable_page = outputs[analysis_page_path].decode()
        assert "public finding unavailable" in unavailable_page
        assert "Unavailable is not zero" in unavailable_page
        assert 'content="noindex, follow, max-image-preview:large"' in unavailable_page
    else:
        assert analysis_paths <= outputs.keys()
        assert 'href="/news/china/analysis/"' in newsroom_index
        assert "Open the latest cross-instrument result" in newsroom_index
        assert '<b>Censorship analysis</b>' in newsroom_index
    for page in range(2, n_pages + 1):
        assert Path("news/china/page") / str(page) / "index.html" in outputs
    sitemap = outputs[Path("news/sitemap.xml")].decode()
    assert "https://palimpsest.info/news/china/" in sitemap
    assert ("https://palimpsest.info/news/china/analysis/" in sitemap) == (
        not analysis_denied
    )
    assert f"https://palimpsest.info/news/china/page/{n_pages}/" in sitemap


def test_cross_instrument_analysis_is_cited_bounded_and_current(publication):
    _wire, feed, _analyses, _stream = publication
    article = china_analysis.build(feed)
    china_analysis.validate(article, feed=feed)

    live_ids = [
        signal_id
        for signal_id in china_analysis.SIGNAL_IDS
        if next(
            story for story in feed["stories"] if story["signal_id"] == signal_id
        )["status"]
        == "live"
    ]
    expected_state = (
        "bounded-finding"
        if len(live_ids) == len(china_analysis.SIGNAL_IDS)
        else "instrument-warning"
    )
    assert article["finding_state"] == expected_state
    assert article["publication_receipt"]["publishable"] is True
    assert article["publication_receipt"]["citation_coverage"] == 1.0
    assert article["publication_receipt"]["live_signal_count"] == len(live_ids)
    assert [row["signal_id"] for row in article["evidence"]] == list(
        china_analysis.SIGNAL_IDS
    )
    evidence_ids = {row["evidence_id"] for row in article["evidence"]}
    cited_ids = {
        citation_id
        for section in article["sections"]
        for paragraph in section["paragraphs"]
        for sentence in paragraph["sentences"]
        for citation_id in sentence["citation_ids"]
    }
    assert cited_ids == evidence_ids
    prose = json.dumps(article, ensure_ascii=False)
    assert "one censorship rate" in prose
    assert "does not identify one cause" in prose
    assert "free-form model prose" in prose


def test_cross_instrument_analysis_turns_a_nonlive_source_into_a_warning(publication):
    _wire, feed, _analyses, _stream = publication
    changed = copy.deepcopy(feed)
    ddti = next(story for story in changed["stories"] if story["signal_id"] == "ddti")
    retained = ddti["metric"]["value"]
    ddti["status"] = "stale"
    ddti["headline"] = "Deletion directive index: no current finding"
    ddti["claims"] = [{
        "type": "availability",
        "statement": "No current finding is published for the deletion directive index because the source status is stale.",
    }]
    ddti["metric"] = {
        "label": None,
        "value": None,
        "unit": None,
        "denominator": {"label": None, "value": None},
    }
    ddti["limitations"] = [
        "Current finding withheld because the source exceeded its freshness deadline."
    ]

    article = china_analysis.build(changed)
    numbers = {item["label"]: item["value"] for item in article["key_numbers"]}
    assert article["finding_state"] == "instrument-warning"
    assert "ddti" in article["publication_receipt"]["availability_warnings"]
    assert numbers["directive terms ranked"] == "withheld"
    assert str(retained) not in next(
        row["claim"] for row in article["evidence"] if row["signal_id"] == "ddti"
    )


def test_cross_instrument_analysis_rejects_a_tampered_evidence_projection(publication):
    _wire, feed, _analyses, _stream = publication
    article = china_analysis.build(feed)
    changed = copy.deepcopy(article)
    changed["evidence"][0]["claim"] = "An unsupported replacement claim."
    changed["revision_id"] = china_analysis._article_identity(changed)

    with pytest.raises(china_analysis.ChinaAnalysisError, match="evidence does not match"):
        china_analysis.validate(changed, feed=feed)


def test_cross_instrument_analysis_has_reader_and_feed_surfaces(publication):
    _wire, feed, _analyses, _stream = publication
    article = china_analysis.build(feed)
    page = build_newsroom.render_china_censorship_analysis(article, feed=feed)
    json_feed = build_newsroom.build_china_analysis_json_feed(article)
    rss = ET.fromstring(build_newsroom.build_china_analysis_rss(article))

    assert page.count("<h1") == 1
    assert 'class="ca-section"' in page
    assert page.count('class="ca-evidence"') == len(china_analysis.SIGNAL_IDS)
    assert "/assets/china-analysis.css" in page
    assert "innerHTML" not in page
    assert "\u2013" not in page and "\u2014" not in page
    assert json_feed["items"][0]["id"] == article["revision_id"]
    assert rss.findtext("./channel/item/guid") == article["revision_id"]


def test_event_revision_keeps_first_published_mutation_bytes(publication, tmp_path):
    wire, feed, _analyses, _stream = publication
    first = build_newsroom.build_outputs(feed, wire=wire, archive_root=tmp_path)
    event = wire["events"][0]
    base = Path("news/wire") / event["event_id"]
    revision = base / "revisions" / f"{event['version_id']}.json"
    for relative, payload in first.items():
        if not build_newsroom._is_wire_history_revision_path(relative):
            continue
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    next_wire = copy.deepcopy(wire)
    next_event = next(
        row for row in next_wire["events"] if row["event_id"] == event["event_id"]
    )
    current_kind = next_event["mutation"]["kind"]
    if current_kind == "new":
        next_event["mutation"] = {
            "kind": "updated", "previous_version_id": next_event["version_id"],
        }
    else:
        next_event["mutation"]["kind"] = (
            "updated" if current_kind == "unchanged" else "unchanged"
        )
    second = build_newsroom.build_outputs(
        feed, wire=next_wire, archive_root=tmp_path
    )

    assert second[revision] == first[revision]
    assert second[base / "story.json"] != first[base / "story.json"]
