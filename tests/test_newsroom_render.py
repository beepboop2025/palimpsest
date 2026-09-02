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

    reading_html = sum(
        1
        for story in feed["stories"]
        if story["signal_id"] in build_newsroom.instrument_analysis_model.READING_HTML
        and (
            ROOT
            / build_newsroom.instrument_analysis_model.READING_HTML[story["signal_id"]]
        ).is_file()
    )
    # Every story owns one content-addressed 1200x630 card. The denied-value
    # edition omits the structured cross-instrument analysis, while retaining
    # the route and replacing both feed formats with a current availability item.
    assert len(outputs) == 11 + (5 * feed["n_stories"]) + reading_html
    assert Path("readings/newsroom-latest.json") in outputs
    assert Path("readings/china-censorship-analysis-latest.json") not in outputs
    assert Path("news/index.html") in outputs
    assert Path("news/china/analysis/index.html") in outputs
    assert Path("news/china/analysis/feed.json") in outputs
    assert Path("news/china/analysis/feed.xml") in outputs
    for story in feed["stories"]:
        assert Path("news") / story["slug"] / "index.html" in outputs
        assert Path("news") / story["slug"] / "story.json" in outputs
        assert Path("news") / story["slug"] / "analysis.json" in outputs


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
    fallback = {"signal_id": "fallback", "priority": "standard", "status": "live"}
    expected = {"signal_id": "expected", "priority": "lead", "status": "live"}
    stories = [fallback, expected]

    assert all("lead" not in story and "event_id" not in story for story in stories)
    assert build_newsroom._select_lead(stories) is expected


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
        item for item in json_feed["items"] if item["url"] == story_without_size["url"]
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
        node.find("s:loc", namespace).text == "https://palimpsest.info/news/standards/"
        for node in urls
    )


def test_denied_china_analysis_feeds_are_current_closed_availability_records():
    feed = _feed()
    outputs = build_newsroom.build_outputs(feed)
    json_path = build_newsroom.CHINA_ANALYSIS_JSON_FEED_RELATIVE
    rss_path = build_newsroom.CHINA_ANALYSIS_RSS_FEED_RELATIVE

    document = json.loads(outputs[json_path])
    assert document == build_newsroom.build_china_analysis_availability_json_feed(
        feed["generated_at"]
    )
    assert outputs[json_path] == build_newsroom._pretty_json(document)
    assert len(document["items"]) == 1
    item = document["items"][0]
    assert item["date_published"] == feed["generated_at"]
    assert item["date_modified"] == feed["generated_at"]
    assert item["_palimpsest"] == {
        "kind": "china_censorship_analysis_availability",
        "publication_disposition": "rights-restricted-availability-v1",
        "value_state": "withheld",
        "verification_status": "public_finding_unavailable",
    }
    assert set(item) == {
        "id",
        "url",
        "title",
        "summary",
        "content_text",
        "date_published",
        "date_modified",
        "authors",
        "tags",
        "attachments",
        "_palimpsest",
    }
    forbidden = (
        "chinaarticlev-",
        "finding_state",
        "citation_coverage",
        "board-wide e-value",
        '"metric"',
    )
    assert all(token not in outputs[json_path].decode() for token in forbidden)

    assert outputs[rss_path] == build_newsroom.build_china_analysis_availability_rss(
        feed["generated_at"]
    )
    channel = ET.fromstring(outputs[rss_path]).find("channel")
    assert channel is not None
    assert len(channel.findall("item")) == 1
    assert channel.findtext("lastBuildDate") == build_newsroom._rfc2822(
        feed["generated_at"]
    )
    rss_item = channel.find("item")
    assert rss_item is not None
    assert rss_item.findtext("guid") == build_newsroom.CHINA_ANALYSIS_AVAILABILITY_ID
    assert rss_item.findtext("pubDate") == channel.findtext("lastBuildDate")
    assert all(token not in outputs[rss_path].decode() for token in forbidden)


def test_denied_publication_overwrites_old_analysis_feeds_and_removes_old_reading(
    tmp_path,
):
    feed = _feed()
    old_article = build_newsroom.china_analysis_model.build(feed)
    old_json = build_newsroom._pretty_json(
        build_newsroom.build_china_analysis_json_feed(old_article)
    )
    old_rss = build_newsroom.build_china_analysis_rss(old_article)
    old_reading = build_newsroom.china_analysis_model.pretty_json_bytes(old_article)
    prior = {
        build_newsroom.CHINA_ANALYSIS_JSON_FEED_RELATIVE: old_json,
        build_newsroom.CHINA_ANALYSIS_RSS_FEED_RELATIVE: old_rss,
        build_newsroom.CHINA_ANALYSIS_READING_RELATIVE: old_reading,
    }
    for relative, raw in prior.items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)

    outputs = build_newsroom.build_outputs(feed)
    drift = set(build_newsroom.check(outputs, root=tmp_path))
    assert {
        f"stale {build_newsroom.CHINA_ANALYSIS_JSON_FEED_RELATIVE}",
        f"stale {build_newsroom.CHINA_ANALYSIS_RSS_FEED_RELATIVE}",
        f"extra {build_newsroom.CHINA_ANALYSIS_READING_RELATIVE}",
    } <= drift

    build_newsroom.publish(outputs, root=tmp_path)

    assert (tmp_path / build_newsroom.CHINA_ANALYSIS_JSON_FEED_RELATIVE).read_bytes() == (
        outputs[build_newsroom.CHINA_ANALYSIS_JSON_FEED_RELATIVE]
    )
    assert (tmp_path / build_newsroom.CHINA_ANALYSIS_RSS_FEED_RELATIVE).read_bytes() == (
        outputs[build_newsroom.CHINA_ANALYSIS_RSS_FEED_RELATIVE]
    )
    assert not (tmp_path / build_newsroom.CHINA_ANALYSIS_READING_RELATIVE).exists()
    assert build_newsroom.check(outputs, root=tmp_path) == []


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


def test_ephemeral_publication_defers_per_file_fsync(monkeypatch, tmp_path):
    target = tmp_path / "news" / "index.html"
    monkeypatch.setenv("PALIMPSEST_EPHEMERAL_BUILD", "1")
    monkeypatch.setattr(
        build_newsroom.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(AssertionError("unexpected fsync")),
    )

    build_newsroom._atomic_write(target, b"verified disposable output\n")

    assert target.read_bytes() == b"verified disposable output\n"


def test_parallel_check_barrier_requires_exact_regular_marker(tmp_path):
    ready = tmp_path / "ready"
    ready.write_bytes(b"ready\n")
    build_newsroom._await_check_barrier(
        ready, expected=b"ready\n", timeout_seconds=1
    )

    rendered = tmp_path / "rendered"
    build_newsroom._publish_check_barrier(rendered, payload=b"rendered\n")
    build_newsroom._await_check_barrier(
        rendered, expected=b"rendered\n", timeout_seconds=1
    )

    malformed = tmp_path / "malformed"
    malformed.write_bytes(b"not-ready\n")
    try:
        build_newsroom._await_check_barrier(
            malformed, expected=b"ready\n", timeout_seconds=1
        )
    except newsroom.NewsroomError as exc:
        assert "malformed" in str(exc)
    else:
        raise AssertionError("malformed newsroom barrier was accepted")


def test_generated_per_story_json_is_the_same_story_as_the_feed():
    feed = _feed()
    outputs = build_newsroom.build_outputs(feed)
    public_feed = json.loads(outputs[Path("readings/newsroom-latest.json")])
    for story in public_feed["stories"]:
        path = Path("news") / story["slug"] / "story.json"
        assert json.loads(outputs[path]) == story
