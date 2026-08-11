"""End-to-end static-rendering contract for the composite evidence wire."""

from __future__ import annotations

import copy
import json
import math
import re
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

import pytest

from core import newsroom, newswire
from scripts import build_newsroom


ROOT = Path(__file__).resolve().parent.parent
EVENT_ID = re.compile(r'class="nw-card__link" href="/news/wire/(event-[0-9a-f]{24})/"')


class _HeadingProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.h1_count = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "h1":
            self.h1_count += 1


def _one_h1(document: bytes) -> bool:
    probe = _HeadingProbe()
    probe.feed(document.decode("utf-8"))
    return probe.h1_count == 1


@pytest.fixture(scope="module")
def publication():
    feed = newsroom.build_news_feed(
        ROOT / "readings/osint-china-latest.json", ROOT / "config/newsroom.json"
    )
    wire = newswire.strict_json_loads(
        (ROOT / "readings/newswire-latest.json").read_bytes(), label="newswire"
    )
    pulse = newswire.strict_json_loads(
        (ROOT / "readings/china-economic-pulse-latest.json").read_bytes(),
        label="economic pulse",
    )
    outputs = build_newsroom.build_outputs(feed, wire=wire, pulse=pulse)
    return feed, wire, pulse, outputs


def test_renderer_desk_vocabulary_matches_the_strict_wire_contract(publication) -> None:
    _feed, wire, _pulse, _outputs = publication
    expected = {
        "economy", "politics", "rights", "security", "censorship",
        "connectivity", "technology",
    }
    schema = json.loads((ROOT / "protocol/newswire-v1.schema.json").read_text())

    assert set(build_newsroom.EVENT_DESKS) == expected
    assert set(schema["$defs"]["desk"]["enum"]) == expected
    assert {event["desk"] for event in wire["events"]} <= expected


def test_home_is_bounded_but_keeps_economic_and_accountability_context(publication) -> None:
    feed, wire, pulse, outputs = publication
    page = outputs[Path("news/index.html")]
    text = page.decode("utf-8")
    cards = EVENT_ID.findall(text)

    assert _one_h1(page)
    assert build_newsroom.HOME_EVENTS_PER_DESK == 5
    assert len(cards) == len(set(cards))
    assert len(cards) <= build_newsroom.HOME_EVENTS_PER_DESK * len(build_newsroom.EVENT_DESKS)
    assert wire["events"][0]["headline"] in text or any(
        event["headline"] in text for event in wire["events"] if event["lead"]
    )
    assert pulse["economic_state"]["claim"] in text
    assert "The composite still abstains" in text
    assert "Every feed answered for" in text
    assert f"{feed['n_stories']} instruments" in text


def test_lead_selection_prefers_evidence_then_explicit_release_then_recency() -> None:
    def event(event_id: str, strength: str, headline: str, updated: str):
        return {
            "event_id": event_id,
            "lead": True,
            "evidence_strength": strength,
            "headline": headline,
            "updated_at": updated,
            "evidence_groups": [{"group_id": event_id}],
        }

    latest_ceremony = event(
        "event-000000000000000000000001",
        "single-primary-source",
        "Official visits a regional office",
        "2026-08-11T12:00:00Z",
    )
    older_release = event(
        "event-000000000000000000000002",
        "single-primary-source",
        "Exchange Fund Bills tender results",
        "2026-08-11T11:00:00Z",
    )
    corroborated = event(
        "event-000000000000000000000003",
        "primary-corroborated",
        "Independent sources publish a policy record",
        "2026-08-11T10:00:00Z",
    )
    corroborated["evidence_groups"].append({"group_id": "independent"})

    assert build_newsroom._select_lead([latest_ceremony, older_release]) is older_release
    assert build_newsroom._select_lead(
        [latest_ceremony, older_release, corroborated]
    ) is corroborated


def test_paginated_wire_accounts_for_every_current_dossier_exactly_once(publication) -> None:
    _feed, wire, _pulse, outputs = publication
    n_pages = math.ceil(wire["n_events"] / build_newsroom.WIRE_PAGE_SIZE)
    page_paths = [Path("news/wire/index.html")] + [
        Path("news/wire/page") / str(page) / "index.html"
        for page in range(2, n_pages + 1)
    ]
    observed: list[str] = []
    for path in page_paths:
        assert path in outputs
        assert _one_h1(outputs[path])
        observed.extend(EVENT_ID.findall(outputs[path].decode("utf-8")))

    assert len(observed) == wire["n_events"]
    assert len(observed) == len(set(observed))
    assert set(observed) == {event["event_id"] for event in wire["events"]}
    sitemap = outputs[Path("news/sitemap.xml")].decode("utf-8")
    for page in range(2, n_pages + 1):
        assert f"https://palimpsest.info/news/wire/page/{page}/" in sitemap


def test_every_event_has_a_human_page_current_json_and_immutable_revision(publication) -> None:
    feed, wire, _pulse, outputs = publication
    for event in wire["events"]:
        base = Path("news/wire") / event["event_id"]
        page = outputs[base / "index.html"]
        text = page.decode("utf-8")
        assert _one_h1(page)
        assert "Evidence matrix" in text
        assert "What this dossier cannot establish" in text
        assert "cannot establish cause" in text
        assert json.loads(outputs[base / "story.json"]) == event
        assert json.loads(outputs[base / "revisions" / f"{event['version_id']}.json"]) == event

    # Existing instrument briefs remain first-class and revision-addressable.
    for story in feed["stories"]:
        base = Path("news") / story["slug"]
        assert base / "index.html" in outputs
        assert any(
            path.parent.parent == base and path.name.endswith(".json")
            for path in outputs if "revisions" in path.parts
        )


def test_hostile_feed_text_stays_text_in_html_and_json_ld(publication) -> None:
    feed, wire, _pulse, _outputs = publication
    event = copy.deepcopy(wire["events"][0])
    hostile = '</script><script>alert("wire")</script>'
    event["headline"] = hostile
    event["dek"] = hostile
    event["reported_facts"][0]["statement"] = hostile
    event["evidence_refs"][0]["title"] = hostile

    rendered = build_newsroom.render_event(event, wire=wire, feed=feed)

    assert '<script>alert("wire")</script>' not in rendered
    assert "&lt;/script&gt;&lt;script&gt;alert" in rendered
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert" in rendered
    assert "innerHTML" not in rendered


def test_chinese_feed_text_has_script_specific_language_metadata(publication) -> None:
    feed, wire, _pulse, _outputs = publication
    copied_wire = copy.deepcopy(wire)
    event = copy.deepcopy(copied_wire["events"][0])
    headline = "中國經濟證據更新"
    dek = "這份摘要保留來源、時間和限制。"
    event["headline"] = headline
    event["dek"] = dek
    event["reported_facts"][0]["statement"] = dek
    event["evidence_refs"][0]["source_id"] = "bbc-chinese"
    event["evidence_refs"][0]["title"] = headline
    item_id = event["evidence_refs"][0]["item_id"]
    next(item for item in copied_wire["items"] if item["item_id"] == item_id)[
        "excerpt"
    ] = dek

    rendered = build_newsroom.render_event(event, wire=copied_wire, feed=feed)
    card = build_newsroom._event_card(event)
    lead = build_newsroom._event_lead(event, copied_wire)

    assert f'<h1 lang="zh-Hant">{headline}</h1>' in rendered
    assert f'<p class="nw-article__dek" lang="zh-Hant">{dek}</p>' in rendered
    assert f'<span lang="zh-Hant">{headline}</span>' in rendered
    assert f'<small lang="zh-Hant">{dek}</small>' in rendered
    assert '"inLanguage":"zh-Hant"' in rendered
    assert '<h3 lang="zh-Hant">' in card
    assert '<h1 id="lead-headline" lang="zh-Hant">' in lead


def test_language_inference_does_not_mislabel_english_translation() -> None:
    assert (
        build_newsroom._text_language(
            "English translation from a Chinese desk", source_id="bbc-chinese"
        )
        == "en"
    )
    assert build_newsroom._text_language("中文", source_id="unknown-desk") == "zh"


def test_dense_tables_are_named_keyboard_scroll_regions(publication) -> None:
    _feed, wire, _pulse, outputs = publication
    event_path = Path("news/wire") / wire["events"][0]["event_id"] / "index.html"
    event_page = outputs[event_path].decode("utf-8")
    economy_page = outputs[Path("news/economy/index.html")].decode("utf-8")

    assert (
        'class="nw-table-wrap" role="region" tabindex="0" '
        'aria-labelledby="matrix-title"' in event_page
    )
    assert "<caption>Evidence receipts for this dossier</caption>" in event_page
    assert event_page.count('scope="col"') >= 4
    assert "Scroll horizontally to inspect every column." in event_page
    assert (
        'class="nw-table-wrap" role="region" tabindex="0" '
        'aria-labelledby="coverage-matrix-title"' in economy_page
    )
    assert "<caption>Economic evidence collection coverage</caption>" in economy_page
    assert economy_page.count('scope="col"') >= 4


def test_unified_machine_feeds_have_stable_unique_dossier_ids(publication) -> None:
    feed, wire, _pulse, outputs = publication
    json_feed = json.loads(outputs[Path("news/feed.json")])
    ids = [item["id"] for item in json_feed["items"]]
    event_ids = {event["event_id"] for event in wire["events"]}

    assert len(ids) == len(set(ids)) == wire["n_events"] + feed["n_stories"]
    assert event_ids <= set(ids)
    event_items = [item for item in json_feed["items"] if item["id"] in event_ids]
    assert all(item["_palimpsest"]["kind"] == "event_dossier" for item in event_items)

    rss = ElementTree.fromstring(outputs[Path("news/feed.xml")])
    guids = [node.text for node in rss.findall("./channel/item/guid")]
    assert len(guids) == len(set(guids))
    assert event_ids <= set(guids)


def test_economic_page_abstains_and_stock_connect_keeps_hkd_units(publication) -> None:
    _feed, _wire, pulse, outputs = publication
    page = outputs[Path("news/economy/index.html")]
    text = page.decode("utf-8")
    southbound_metrics = [
        metric
        for desk in pulse["desks"]
        for metric in desk["metrics"]
        if metric["independence_group"] == "hkex_market_flows"
        and "southbound" in metric["metric_id"]
    ]

    assert _one_h1(page)
    assert pulse["economic_state"]["status"] == "warming_up"
    assert "The economic pulse abstains" in text
    assert "Prohibited shortcuts" in text
    assert southbound_metrics
    assert {metric["unit"] for metric in southbound_metrics} == {"HKD billion"}
    assert all(
        "CNY" not in metric["comparability"]["basis"]
        for metric in southbound_metrics
    )


def test_generated_manifest_is_an_exact_inventory(publication) -> None:
    _feed, _wire, _pulse, outputs = publication
    manifest = json.loads(outputs[Path("news/generated-manifest.json")])
    paths = sorted(str(path) for path in outputs)

    assert manifest["n_paths"] == len(outputs)
    assert manifest["paths"] == paths
    assert manifest["immutable_revision_paths"]
    assert all("/revisions/" in path for path in manifest["immutable_revision_paths"])
    assert set(manifest["immutable_revision_paths"]).isdisjoint(manifest["mutable_paths"])
