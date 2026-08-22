"""Contract tests for per-instrument newsroom/reading companions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import instrument_analysis, newsroom
from scripts import build_newsroom


ROOT = Path(__file__).resolve().parent.parent
SITE = "https://palimpsest.info"


def _feed():
    return newsroom.build_news_feed()


def _site_path(url: str) -> str:
    return url[len(SITE) :] if url.startswith(SITE) else url


def test_every_public_story_gets_one_cited_companion():
    feed = _feed()
    analyses = instrument_analysis.build_instrument_analyses(feed)

    assert set(analyses) == {story["signal_id"] for story in feed["stories"]}
    for story in feed["stories"]:
        analysis = analyses[story["signal_id"]]
        instrument_analysis.validate_instrument_analysis(analysis, story=story)
        assert analysis["schema_version"] == "palimpsest-instrument-analysis.v1"
        assert analysis["publication_receipt"]["automatic_publication"] is False
        assert analysis["publication_receipt"]["citation_coverage"] == 1.0
        assert analysis["review_rank"]["anomaly_score_published"] is False
        assert analysis["review_rank"]["editorial_priority_role"] == "review-rank-only"
        assert "why the party" not in analysis["position"].casefold()
        if story["status"] == "live" and story["signal_id"] not in instrument_analysis.PRIVATE_SIGNALS:
            assert analysis["disposition"] == "live-reading"
            assert analysis["key_numbers"][0]["value"] != "withheld"
        else:
            assert analysis["disposition"] == "availability-brief"
            assert analysis["key_numbers"][0]["value"] == "withheld"


def test_ooni_live_brief_names_number_denominator_and_peers():
    feed = _feed()
    story = next(row for row in feed["stories"] if row["signal_id"] == "ooni-gfw")
    analysis = instrument_analysis.build_instrument_analysis(story, feed)

    if story["status"] != "live":
        assert analysis["status"] == story["status"]
        assert analysis["disposition"] == "availability-brief"
        return
    assert analysis["status"] == "live"
    blob = " ".join(
        [analysis["position"], analysis["key_numbers"][0]["value"], analysis["key_numbers"][0]["note"]]
        + [item["text"] for layer in analysis["brief"].values() for item in layer["sentences"]]
    )
    assert "59.3%" in blob or analysis["key_numbers"][0]["value"].endswith("%")
    assert "completed measurements" in blob or "completed measurements" in analysis["key_numbers"][0]["note"]
    assert "does not assign motive" in blob
    assert analysis["elevated_peers"]
    assert all(row["signal_id"] != "ooni-gfw" for row in analysis["elevated_peers"])


def test_non_live_story_is_an_availability_brief():
    feed = _feed()
    story = next(row for row in feed["stories"] if row["status"] != "live")
    analysis = instrument_analysis.build_instrument_analysis(story, feed)

    assert analysis["disposition"] == "availability-brief"
    assert analysis["key_numbers"][0]["value"] == "withheld"
    assert "availability brief" in analysis["position"]
    assert story["status"] in analysis["position"]


def test_nemesis_never_becomes_a_live_finding():
    feed = _feed()
    story = next(row for row in feed["stories"] if row["signal_id"] == "nemesis")
    analysis = instrument_analysis.build_instrument_analysis(story, feed)
    assert analysis["disposition"] == "availability-brief"
    assert analysis["key_numbers"][0]["value"] == "withheld"


def test_private_signal_with_live_status_still_withholds_the_metric():
    feed = _feed()
    public = next(
        row
        for row in feed["stories"]
        if row["status"] == "live"
        and row["signal_id"] not in instrument_analysis.PRIVATE_SIGNALS
    )
    story = dict(public)
    story["signal_id"] = "nemesis"
    analysis = instrument_analysis.build_instrument_analysis(story, feed)
    assert analysis["disposition"] == "availability-brief"
    assert analysis["key_numbers"][0]["value"] == "withheld"


def test_newsroom_build_emits_story_and_reading_companions():
    feed = _feed()
    outputs = build_newsroom.build_outputs(feed)

    for story in feed["stories"]:
        assert Path("news") / story["slug"] / "analysis.json" in outputs
        assert instrument_analysis.reading_analysis_relpath(story) in outputs
        page = outputs[Path("news") / story["slug"] / "index.html"].decode()
        assert "Instrument brief" in page
        assert "analysis.json" in page
    for signal_id, relative in instrument_analysis.READING_HTML.items():
        if signal_id in {story["signal_id"] for story in feed["stories"]} and (ROOT / relative).is_file():
            html = outputs[relative].decode()
            story = next(row for row in feed["stories"] if row["signal_id"] == signal_id)
            analysis = instrument_analysis.build_instrument_analysis(story, feed)
            assert 'id="instrument-analysis"' in html
            assert "Instrument brief" in html
            assert 'href="analysis.json"' not in html
            assert _site_path(analysis["url"]) in html
            assert analysis["reading_analysis_url"].rsplit("/", 1)[-1] in html


def test_generated_document_conforms_to_public_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "protocol/instrument-analysis.schema.json").read_text())
    feed = _feed()
    story = next(row for row in feed["stories"] if row["signal_id"] == "ooni-gfw")
    analysis = instrument_analysis.build_instrument_analysis(story, feed)
    jsonschema.Draft202012Validator(schema).validate(analysis)
