"""Publication tests for the public China situation desk."""

from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from core import social_observations
from scripts import build_china_situation as builder


def _social_state_path(tmp_path, *, active: bool):
    registry = social_observations.load_source_registry()
    receipts = [
        {
            "source_id": source.id,
            "status": (
                "success"
                if active and source.id == "cgtn-telegram"
                else "not-attempted"
            ),
            "rejected": 0,
            "error_code": None,
        }
        for source in registry.sources
    ]
    latest, ledger = social_observations.build_latest(
        [],
        registry=registry,
        generated_at="2026-08-16T18:00:00Z",
        collection_receipts=receipts,
    )
    assert ledger == ()
    path = tmp_path / ("social-active.json" if active else "social-zero.json")
    path.write_bytes(social_observations.canonical_json_bytes(latest))
    return path


def test_builder_emits_structured_index_page_and_two_feed_views():
    outputs, document = builder.build_outputs()

    fixed_outputs = {
        builder.OUTPUT_PATH,
        builder.PAGE_PATH,
        builder.JSON_FEED_PATH,
        builder.RSS_FEED_PATH,
    }
    assert fixed_outputs <= set(outputs)
    expected_pages = max(
        1,
        (document["coverage"]["in_scope_events"] + builder.PAGE_SIZE - 1)
        // builder.PAGE_SIZE,
    )
    assert sum(
        path.name == "index.html" and builder.PAGE_PATH.parent in path.parents
        for path in outputs
    ) == expected_pages
    assert document["coverage"]["in_scope_events"] > 0
    assert document["coverage"]["publisher_reports"] > 0
    assert document["coverage"]["measurement_context_rows"] > 0

    published = json.loads(outputs[builder.OUTPUT_PATH])
    json_feed = json.loads(outputs[builder.JSON_FEED_PATH])
    ET.fromstring(outputs[builder.RSS_FEED_PATH])
    assert published == document
    assert json_feed["home_page_url"] == "https://palimpsest.info/news/china/situation/"
    assert json_feed["items"]
    assert len(json_feed["items"]) <= builder.FEED_LIMIT


def test_page_makes_each_relation_and_zero_state_visible(tmp_path):
    outputs, document = builder.build_outputs(
        social_path=_social_state_path(tmp_path, active=False)
    )
    page = outputs[builder.PAGE_PATH].decode("utf-8")

    assert "Reports." in page
    assert "Social context." in page
    assert "Measurements." in page
    assert "More context does not automatically mean more proof." in page
    assert "topic-surface-only · not article verification" in page
    assert "publisher-link-context-not-corroboration" in page
    assert "Reviewed Telegram briefing" in page
    assert "Source-free signals stay separate from attributed reporting." in page
    assert "Raw Telegram forwards remain in Dragon Den" in page
    assert "credentials and reviewed sources are not active yet" in page
    assert "/readings/china-situation-latest.json" in page
    assert str(document["coverage"]["in_scope_events"]) in page
    assert "Page 1 of " in page
    assert "Search this archive page" in page
    assert len(page) < 700_000


def test_page_makes_active_social_state_visible(tmp_path):
    outputs, document = builder.build_outputs(
        social_path=_social_state_path(tmp_path, active=True)
    )
    page = outputs[builder.PAGE_PATH].decode("utf-8")

    assert document["inputs"]["social_status"] == "active"
    assert "Social observations are connected." in page
    assert "0 sanitized observations are present" in page
    assert "credentials and reviewed sources are not active yet" not in page


def test_situation_styles_preserve_dark_contrast_and_readable_headlines():
    css = (builder.ROOT / "assets" / "china-situation.css").read_text(
        encoding="utf-8"
    )

    assert "body.ps.newsroom-page.situation-page" in css
    assert "color-scheme: dark" in css
    assert "var(--nw-display" in css
    assert "var(--nw-mono" in css
    assert "var(--display" not in css
    assert "var(--mono" not in css
    assert "overflow-wrap: normal" in css
    assert "word-break: normal" in css
    assert "@media (max-width: 1100px)" in css
    assert "17vw" not in css
    assert "min-height: 44px" in css
    assert ".situation-page .ps-share button" in css
    assert ".situation-page .ps-share__dot" in css


def test_archive_pages_are_bounded_linked_and_canonical() -> None:
    outputs, document = builder.build_outputs()
    expected_pages = max(
        1, (len(document["situations"]) + builder.PAGE_SIZE - 1) // builder.PAGE_SIZE
    )
    last_path = builder._page_path(expected_pages)
    first = outputs[builder.PAGE_PATH].decode("utf-8")
    last = outputs[last_path].decode("utf-8")

    assert first.count('class="situation-card"') <= builder.PAGE_SIZE
    assert last.count('class="situation-card"') <= builder.PAGE_SIZE
    assert f'href="/news/china/situation/page/{expected_pages}/"' in first
    assert (
        f'<link rel="canonical" '
        f'href="https://palimpsest.info/news/china/situation/page/{expected_pages}/"'
        in last
    )
    assert 'rel="prev"' in last


def test_checked_in_situation_outputs_are_current():
    assert builder.run(check=True) == 0


def test_stale_archive_pages_are_detected_and_removed(tmp_path, monkeypatch) -> None:
    page_root = tmp_path / "situation" / "index.html"
    stale = tmp_path / "situation" / "page" / "999" / "index.html"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(builder, "PAGE_PATH", page_root)

    assert builder._archive_page_paths() == {stale}
    builder._remove_stale_archive_pages(set())
    assert not stale.exists()
