"""A live integrity record (anchors) is its own instrument kind, never a crash.

Regression for 2026-11-02 23:44Z. Once OpenTimestamps stamping was repaired,
the anchors signal went live for the first time since the strict record-kind
check arrived (70b569bbe3). Its story carries the all-null metric and one
``integrity`` claim, as core/newsroom.py builds it and the feed protocol
allows, but the renderer accepted only measurements and availability notices,
so every publication failed at render_evidence_index.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core import newsroom
from scripts import build_newsroom

ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = ROOT / "readings" / "osint-china-latest.json"
CONFIG_PATH = ROOT / "config" / "newsroom.json"
WIRE_PATH = ROOT / "readings" / "newswire-latest.json"


def _live_anchors_source(tmp_path: Path) -> Path:
    """Copy the committed snapshot with the anchors signal made healthy."""

    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    anchors = next(signal for signal in source["signals"] if signal["id"] == "anchors")
    previous = anchors["status"]
    assert previous != "live", "fixture assumption: committed anchors is not live"
    assert anchors["metric"] is None, "anchors declares no metric by design"
    # Borrow the clocks and health shape of a signal that is live in the same
    # snapshot, so every cross-field check in core/newsroom.py still holds.
    reference = next(signal for signal in source["signals"] if signal["status"] == "live")
    anchors["status"] = "live"
    anchors["live"] = True
    anchors["health"] = copy.deepcopy(reference["health"])
    anchors["freshness_deadline"] = reference["freshness_deadline"]
    anchors["source_timestamp"] = reference["source_timestamp"]
    source["n_signals_live"] += 1
    counts = source["health"]["counts"]
    counts["live"] += 1
    counts[previous] -= 1
    path = tmp_path / "osint-china-latest.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    return path


@pytest.fixture
def live_feed(tmp_path: Path) -> dict:
    return newsroom.build_news_feed(_live_anchors_source(tmp_path), CONFIG_PATH)


def _anchors(feed: dict) -> dict:
    return next(story for story in feed["stories"] if story["signal_id"] == "anchors")


def test_live_anchors_story_is_an_integrity_record_counted_in_neither_total(live_feed):
    story = _anchors(live_feed)
    assert story["type"] == "integrity" and story["status"] == "live"
    assert story["claims"][0]["type"] == "integrity"
    assert build_newsroom._is_integrity_story(story)
    assert not build_newsroom._is_availability_story(story)
    assert build_newsroom._instrument_record_kind(story) == "integrity"

    measurements, availability = build_newsroom._instrument_record_counts(live_feed)
    assert measurements + availability == len(live_feed["stories"]) - 1


def test_full_edition_renders_with_a_live_integrity_record(live_feed):
    wire = json.loads(WIRE_PATH.read_text(encoding="utf-8"))
    outputs = build_newsroom.build_outputs(live_feed, wire=wire)

    index = outputs[Path("news/index.html")].decode("utf-8")
    assert 'data-palimpsest-kind="instrument-integrity"' in index
    assert "Integrity record" in index

    rss = outputs[Path("news/instruments/feed.xml")].decode("utf-8")
    assert "[Palimpsest integrity] " in rss
    assert "<palimpsest:kind>instrument_integrity</palimpsest:kind>" in rss


def test_integrity_card_shows_no_value_and_no_metric_markup(live_feed):
    card = build_newsroom._story_card(_anchors(live_feed), "Integrity")
    assert 'data-claim-type="integrity"' in card
    assert "Integrity record" in card and "not a measurement" in card
    assert "nw-card__metric" not in card
    assert "No current value" not in card


def test_integrity_record_never_leads_an_edition(live_feed):
    lead = build_newsroom._select_instrument_lead(live_feed["stories"])
    assert lead["signal_id"] != "anchors"

    # Even when it is the only live record, another record leads instead.
    stories = copy.deepcopy(live_feed["stories"])
    for story in stories:
        if story["signal_id"] != "anchors":
            story["status"] = "stale"
    lead = build_newsroom._select_instrument_lead(stories)
    assert lead["signal_id"] != "anchors"

    spec = build_newsroom._edition_share_card_spec(live_feed)
    assert spec["title"] != _anchors(live_feed)["headline"]


def test_json_feed_labels_the_integrity_record_and_keeps_other_kinds(live_feed):
    items = {
        item["id"].split(":")[1]: item
        for item in build_newsroom.build_json_feed(live_feed)["items"]
        if item["id"].startswith("palimpsest-news:")
    }
    anchors = items["anchors"]
    assert anchors["title"].startswith("[Palimpsest integrity] ")
    assert anchors["content_text"].startswith("ITEM TYPE: PALIMPSEST INTEGRITY")
    assert "palimpsest-integrity" in anchors["tags"]
    assert anchors["_palimpsest"]["kind"] == "instrument_integrity"
    assert anchors["_palimpsest"]["verification_status"] == "palimpsest_integrity_record"
    others = {item["_palimpsest"]["kind"] for key, item in items.items() if key != "anchors"}
    assert others <= {"instrument_measurement", "instrument_availability"}


def test_stale_anchors_stay_an_availability_notice():
    feed = newsroom.build_news_feed(SOURCE_PATH, CONFIG_PATH)
    story = _anchors(feed)
    assert story["status"] != "live"
    assert build_newsroom._instrument_record_kind(story) == "availability"
    assert not build_newsroom._is_integrity_story(story)


def test_an_ambiguous_record_still_fails_closed(live_feed):
    story = copy.deepcopy(_anchors(live_feed))
    story["claims"] = [{"type": "finding", "statement": "Roots published."}]
    with pytest.raises(newsroom.NewsroomError, match="integrity record"):
        build_newsroom._instrument_record_kind(story)
