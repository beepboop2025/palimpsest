"""Rumour-board desk stays context-only and labeled as rumour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collectors import fourchan_catalog
from core import live_event
from core import rumour_board
from core.place_gazetteer import validate_place_gazetteer
from scripts import build_rumour_board


ROOT = Path(__file__).resolve().parent.parent


def _event():
    return {
        "schema_version": live_event.SCHEMA_VERSION,
        "event_id": live_event.event_id(
            "fourchan-catalog",
            "fourchan-news",
            "https://boards.4chan.org/news/thread/9",
            "2026-08-20T12:00:00Z",
        ),
        "tap_id": "fourchan-catalog",
        "source_id": "fourchan-news",
        "url": "https://boards.4chan.org/news/thread/9",
        "title": "Guangdong factory fire reported on a rumour board",
        "content_sha256": live_event.content_digest("news", "9"),
        "observed_at": "2026-08-20T12:00:00Z",
        "vantage": "test-vantage",
        "relation": rumour_board.RELATION,
        "gazetteer_hits": ["guangdong"],
        "rights_class": "rumour-board",
        "review_status": "machine-accepted",
    }


def test_empty_gazetteer_is_valid_and_matches_nothing():
    gazetteer = json.loads((ROOT / "config" / "china_place_gazetteer.json").read_text())
    validate_place_gazetteer(gazetteer)
    assert gazetteer["lemmas"] == []


def test_empty_rumour_board_is_coverage_only():
    document = rumour_board.empty_document("2026-08-20T12:00:00Z")
    assert document["status"] == "COVERAGE_ONLY"
    assert document["publication_policy"]["increments_independent_groups"] is False
    page = build_rumour_board.render_page(document)
    assert "Taken from rumour boards" in page
    assert "do not add an independent source group" in page
    assert "that tuple is the product" in page
    assert "more public vantages, continuously, then join" in page


def test_project_events_keeps_only_rumour_relations():
    other = _event()
    other["relation"] = "official-list-context-not-corroboration"
    other["rights_class"] = "public-metadata"
    other["event_id"] = live_event.event_id(
        "fourchan-catalog",
        "fourchan-news",
        "https://boards.4chan.org/news/thread/8",
        "2026-08-20T12:00:00Z",
    )
    live_event.validate_live_event(other)
    document = rumour_board.project_events([_event(), other], generated_at="2026-08-20T13:00:00Z")
    assert document["n_entries"] == 1
    assert document["entries"][0]["relation"] == rumour_board.RELATION


def test_fourchan_drops_blocked_boards_media_and_minors():
    lemmas = [{"id": "guangdong", "zh": "广东", "en": "Guangdong", "kind": "province"}]
    with pytest.raises(fourchan_catalog.FourchanCatalogError, match="allow-list"):
        fourchan_catalog.collect_catalog(
            {"pol": []}, lemmas, observed_at="2026-08-20T12:00:00Z"
        )
    catalogs = {
        "news": [
            {
                "threads": [
                    {
                        "no": 1,
                        "time": 1755691200,
                        "sub": "Guangdong factory fire",
                        "ext": ".jpg",
                        "tim": 1,
                    },
                    {"no": 2, "time": 1755691200, "sub": "teen in Guangdong"},
                    {"no": 3, "time": 1755691200, "sub": "unrelated sports score"},
                ]
            }
        ]
    }
    events, dropped = fourchan_catalog.collect_catalog(
        catalogs, lemmas, observed_at="2026-08-20T12:00:00Z"
    )
    assert [row["title"] for row in events] == ["Guangdong factory fire"]
    assert "ext" not in events[0]
    assert dropped == 2


def test_fourchan_stays_not_attempted_without_injected_catalogs():
    receipt = fourchan_catalog.collect(observed_at="2026-08-20T12:00:00Z")
    assert receipt["status"] == "not-attempted"
    assert receipt["events"] == []
    assert receipt["error_code"] == "flag-off"


def test_situation_briefing_does_not_claim_corroboration():
    html = build_rumour_board.render_page(
        rumour_board.empty_document("2026-08-20T12:00:00Z")
    )
    from scripts.build_china_situation import _rumour_briefing

    briefing = _rumour_briefing(rumour_board.empty_document("2026-08-20T12:00:00Z"))
    assert "Open the rumour-board desk" in briefing
    assert "not corroboration" in briefing
    assert "independent source group" in html


def test_builder_seals_coverage_without_network():
    watch, rumour = build_rumour_board.build_documents(
        generated_at="2026-08-20T12:00:00Z"
    )
    assert watch["n_events"] == 0
    assert watch["n_taps"] >= 10
    assert {row["status"] for row in watch["taps"]} == {"not-attempted"}
    fourchan = next(row for row in watch["taps"] if row["tap_id"] == "fourchan-catalog")
    assert fourchan["error_code"] == "flag-off"
    assert rumour["status"] == "COVERAGE_ONLY"
    assert "t.me/s/" in rumour["limitations"][2]
    page = build_rumour_board.render_page(rumour, watch)
    assert "wikimedia-eventstreams" in page
    assert "Never keep the HTML" in page


def test_now_flag_normalizes_to_utc_seconds():
    assert build_rumour_board._parse_now("2026-08-20T09:50:33Z") == "2026-08-20T09:50:33Z"
    with pytest.raises(build_rumour_board.RumourBoardBuildError, match="timezone"):
        build_rumour_board._parse_now("2026-08-20T09:50:33")
