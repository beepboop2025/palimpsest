"""Static newsroom rendering and syndication contracts."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from core import newsroom
from scripts import build_newsroom


ROOT = Path(__file__).resolve().parent.parent


def _feed():
    return newsroom.build_news_feed()


def test_renderer_emits_one_html_and_json_document_per_story():
    feed = _feed()
    outputs = build_newsroom.build_outputs(feed)

    assert len(outputs) == 5 + (2 * feed["n_stories"])
    assert Path("readings/newsroom-latest.json") in outputs
    assert Path("news/index.html") in outputs
    for story in feed["stories"]:
        assert Path("news") / story["slug"] / "index.html" in outputs
        assert Path("news") / story["slug"] / "story.json" in outputs


def test_index_is_server_rendered_semantic_and_evidence_first():
    feed = _feed()
    page = build_newsroom.render_index(feed)

    assert page.startswith("<!doctype html>")
    assert '<main id="main"' in page
    assert page.count("<h1") == 1
    assert "Evidence receipt" in page
    assert "What we cannot currently claim" in page
    assert "/readings/newsroom-latest.json" in page
    assert "application/ld+json" in page
    assert '"@type":"CollectionPage"' in page
    assert "document.write" not in page
    assert "innerHTML" not in page


def test_legacy_instrument_lead_selection_does_not_require_event_fields():
    feed = _feed()
    expected = next(
        story
        for story in feed["stories"]
        if story["priority"] == "lead" and story["status"] == "live"
    )

    assert all("lead" not in story and "event_id" not in story for story in feed["stories"])
    assert build_newsroom._select_lead(feed["stories"]) is expected


def test_story_pages_publish_newsarticle_metadata_and_exact_evidence():
    feed = _feed()
    by_id = {story["signal_id"]: story for story in feed["stories"]}
    sections = {section["id"]: section for section in feed["sections"]}
    story = next(item for item in feed["stories"] if item["status"] == "live")
    page = build_newsroom.render_story(
        story,
        section=sections[story["section"]],
        by_id=by_id,
    )

    assert '<meta property="og:type" content="article">' in page
    assert '"@type":"NewsArticle"' in page
    assert story["published_at"] in page
    assert story["evidence"]["url"] in page
    assert story["evidence"]["input"]["sha256"] in page
    assert "What this cannot establish" in page
    assert 'href="story.json"' in page


def test_nonlive_story_does_not_render_a_retained_metric_as_current():
    feed = _feed()
    by_id = {story["signal_id"]: story for story in feed["stories"]}
    sections = {section["id"]: section for section in feed["sections"]}
    story = next(item for item in feed["stories"] if item["status"] != "live")
    page = build_newsroom.render_story(
        story,
        section=sections[story["section"]],
        by_id=by_id,
    )

    assert "No current finding" in page or "no current finding" in page
    assert "nw-metric-block" not in page
    assert build_newsroom._status_label(story["status"]) in page


def test_json_feed_rss_and_sitemap_are_parseable_and_complete():
    feed = _feed()
    feed_without_receipt_size = copy.deepcopy(feed)
    story_without_size = feed_without_receipt_size["stories"][0]
    story_without_size["evidence"]["input"]["bytes"] = None
    json_feed = build_newsroom.build_json_feed(feed_without_receipt_size)
    assert json_feed["version"] == "https://jsonfeed.org/version/1.1"
    assert len(json_feed["items"]) == feed["n_stories"]
    assert len({item["id"] for item in json_feed["items"]}) == feed["n_stories"]
    item_without_size = next(
        item for item in json_feed["items"]
        if item["url"] == story_without_size["url"]
    )
    assert "size_in_bytes" not in item_without_size["attachments"][0]

    rss = ET.fromstring(build_newsroom.build_rss(feed))
    assert rss.tag == "rss"
    assert len(rss.findall("./channel/item")) == feed["n_stories"]

    sitemap = ET.fromstring(build_newsroom.build_sitemap(feed))
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = sitemap.findall("s:url", namespace)
    assert len(urls) == feed["n_stories"] + 2
    assert any(
        node.find("s:loc", namespace).text
        == "https://palimpsest.info/news/standards/"
        for node in urls
    )


def test_rss_quote_escapes_source_url_attributes():
    feed = _feed()
    feed["stories"][0]["evidence"]["url"] = (
        'https://palimpsest.info/readings/example"onload="alert(1).json'
    )

    raw = build_newsroom.build_rss(feed)
    rss = ET.fromstring(raw)
    source = rss.find("./channel/item/source")

    assert source is not None
    assert source.attrib["url"].endswith('example"onload="alert(1).json')
    assert set(source.attrib) == {"url"}
    assert b" onload=" not in raw


def test_publication_is_idempotent_and_check_detects_drift(tmp_path):
    feed = _feed()
    outputs = build_newsroom.build_outputs(feed)
    changed, unchanged = build_newsroom.publish(outputs, root=tmp_path)
    assert changed == len(outputs)
    assert unchanged == 0
    assert build_newsroom.check(outputs, root=tmp_path) == []

    changed, unchanged = build_newsroom.publish(outputs, root=tmp_path)
    assert changed == 0
    assert unchanged == len(outputs)

    target = tmp_path / "news" / "index.html"
    target.write_text("drift", encoding="utf-8")
    assert build_newsroom.check(outputs, root=tmp_path) == ["stale news/index.html"]


def test_generated_per_story_json_is_the_same_story_as_the_feed():
    feed = _feed()
    outputs = build_newsroom.build_outputs(feed)
    for story in feed["stories"]:
        path = Path("news") / story["slug"] / "story.json"
        assert json.loads(outputs[path]) == story
