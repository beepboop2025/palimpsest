"""Contracts for the one-entry-per-publisher China dispatch stream."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from core import china_article_stream
from core import event_analysis
from core import newsroom
from core import newswire
from core import telegram_watch
from scripts import build_newsroom
from scripts import review_scamshield_summary


ROOT = Path(__file__).resolve().parent.parent


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

    same_group_event = next(
        event for event in wire["events"]
        if len(event["evidence_refs"]) > 1 and len(event["evidence_groups"]) == 1
    )
    matching = [
        entry for entry in stream["entries"]
        if entry["dossier"]["event_id"] == same_group_event["event_id"]
    ]
    assert len(matching) == len(same_group_event["evidence_refs"])
    assert {entry["dossier"]["independent_groups"] for entry in matching} == {1}
    assert len({entry["analysis"]["analysis_id"] for entry in matching}) == 1


def test_runtime_validator_and_json_schemas_are_closed(publication):
    _wire, _feed, _analyses, stream = publication
    china_article_stream.validate_china_article_stream(stream)

    mutated = copy.deepcopy(stream)
    mutated["telegram_watch"]["raw_text"] = "must never publish"
    with pytest.raises(china_article_stream.ChinaArticleStreamError):
        china_article_stream.validate_china_article_stream(mutated)

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
    assert "Why Palimpsest says that" in page
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
    for page in range(2, n_pages + 1):
        assert Path("news/china/page") / str(page) / "index.html" in outputs
    sitemap = outputs[Path("news/sitemap.xml")].decode()
    assert "https://palimpsest.info/news/china/" in sitemap
    assert f"https://palimpsest.info/news/china/page/{n_pages}/" in sitemap


def test_event_revision_keeps_first_published_mutation_bytes(publication, tmp_path):
    wire, feed, _analyses, _stream = publication
    first = build_newsroom.build_outputs(feed, wire=wire, archive_root=tmp_path)
    event = wire["events"][0]
    base = Path("news/wire") / event["event_id"]
    revision = base / "revisions" / f"{event['version_id']}.json"
    destination = tmp_path / revision
    destination.parent.mkdir(parents=True)
    destination.write_bytes(first[revision])

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
