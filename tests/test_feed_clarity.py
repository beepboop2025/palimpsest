"""Public feeds remain distinct, attributable, and evidence bounded."""
from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SITE = "https://palimpsest.info"
ATOM = {"atom": "http://www.w3.org/2005/Atom"}

FEEDS = {
    "journal": {
        "rss": "journal/feed.xml",
        "json": "journal/feed.json",
        "prefixes": ("[Palimpsest eval finding]",),
        "kinds": {"eval_finding"},
    },
    "eval_methods": {
        "rss": "evals/feed.xml",
        "json": "evals/feed.json",
        "prefixes": ("[Palimpsest method article]",),
        "kinds": {"eval_method_article"},
    },
    "measurements": {
        "rss": "news/instruments/feed.xml",
        "json": "news/instruments/feed.json",
        "prefixes": ("[Palimpsest measurement]",),
        "kinds": {"instrument_measurement"},
    },
    "mixed": {
        "rss": "news/feed.xml",
        "json": "news/feed.json",
        "prefixes": (
            "[Palimpsest measurement]",
            "[Source report]",
            "[Corroborated source report]",
        ),
        "kinds": {"instrument_measurement", "publisher_source_record"},
    },
    "china_sources": {
        "rss": "news/china/feed.xml",
        "json": "news/china/feed.json",
        "prefixes": ("[Source report]", "[Corroborated source report]"),
        "kinds": {"publisher_source_record_with_analysis"},
    },
    "china_analysis": {
        "rss": "news/china/analysis/feed.xml",
        "json": "news/china/analysis/feed.json",
        "prefixes": ("[Palimpsest analysis]",),
        "kinds": {"china_censorship_analysis"},
    },
    "reviewed_context": {
        "rss": "news/china/whispers/feed.xml",
        "json": "news/china/whispers/feed.json",
        "prefixes": ("[Unverified context]",),
        "kinds": {"reviewed_sanitized_telegram_context"},
    },
}


def _starts_with_one(value: str, prefixes: tuple[str, ...]) -> bool:
    return any(value.startswith(prefix) for prefix in prefixes)


def test_all_seven_feed_pairs_parse_and_identify_themselves() -> None:
    rss_self_urls: set[str] = set()
    json_self_urls: set[str] = set()

    for contract in FEEDS.values():
        rss_path = ROOT / contract["rss"]
        json_path = ROOT / contract["json"]
        assert rss_path.is_file()
        assert json_path.is_file()

        channel = ET.parse(rss_path).getroot().find("channel")
        assert channel is not None
        self_link = channel.find("atom:link[@rel='self']", ATOM)
        assert self_link is not None
        expected_rss = f"{SITE}/{contract['rss']}"
        assert self_link.attrib["href"] == expected_rss
        assert channel.findtext("title", "").strip()
        assert channel.findtext("description", "").strip()
        rss_self_urls.add(self_link.attrib["href"])

        document = json.loads(json_path.read_text(encoding="utf-8"))
        expected_json = f"{SITE}/{contract['json']}"
        assert document["version"] == "https://jsonfeed.org/version/1.1"
        assert document["feed_url"] == expected_json
        assert document["title"].strip()
        assert document["description"].strip()
        json_self_urls.add(document["feed_url"])

    assert len(rss_self_urls) == len(FEEDS)
    assert len(json_self_urls) == len(FEEDS)


def test_every_published_item_has_a_plain_label_stable_id_and_typed_metadata() -> None:
    for contract in FEEDS.values():
        channel = ET.parse(ROOT / contract["rss"]).getroot().find("channel")
        assert channel is not None
        rss_items = channel.findall("item")
        rss_guids = [item.findtext("guid", "") for item in rss_items]
        assert all(rss_guids)
        assert len(rss_guids) == len(set(rss_guids))
        assert all(
            _starts_with_one(item.findtext("title", ""), contract["prefixes"])
            for item in rss_items
        )

        document = json.loads((ROOT / contract["json"]).read_text(encoding="utf-8"))
        items = document["items"]
        item_ids = [item["id"] for item in items]
        assert len(item_ids) == len(set(item_ids))
        assert all(_starts_with_one(item["title"], contract["prefixes"]) for item in items)
        assert all(item["_palimpsest"]["kind"] in contract["kinds"] for item in items)


def test_instrument_feed_is_not_misidentified_as_the_mixed_feed() -> None:
    instruments = json.loads(
        (ROOT / "news/instruments/feed.json").read_text(encoding="utf-8")
    )
    mixed = json.loads((ROOT / "news/feed.json").read_text(encoding="utf-8"))

    assert instruments["feed_url"].endswith("/news/instruments/feed.json")
    assert mixed["feed_url"].endswith("/news/feed.json")
    assert instruments["feed_url"] != mixed["feed_url"]
    assert {item["_palimpsest"]["kind"] for item in instruments["items"]} == {
        "instrument_measurement"
    }
    assert mixed["items"][0]["_palimpsest"]["kind"] == "instrument_measurement"


def test_publisher_records_keep_the_original_and_verification_boundary_visible() -> None:
    mixed = json.loads((ROOT / "news/feed.json").read_text(encoding="utf-8"))
    wire = json.loads(
        (ROOT / "readings/newswire-latest.json").read_text(encoding="utf-8")
    )
    events = {event["event_id"]: event for event in wire["events"]}
    source_items = [
        item
        for item in mixed["items"]
        if item["_palimpsest"]["kind"] == "publisher_source_record"
    ]
    assert source_items
    for item in source_items:
        assert "Verification status:" in item["content_text"]
        assert "Read the original:" in item["content_text"]
        assert item["external_url"].startswith("https://")
        assert item["url"].startswith(f"{SITE}/news/wire/")
        assert item["external_url"] != item["url"]
        assert events[item["id"]]["dek"] not in item["content_text"]

    china = json.loads((ROOT / "news/china/feed.json").read_text(encoding="utf-8"))
    assert china["items"]
    for item in china["items"]:
        assert item["_palimpsest"]["kind"] == "publisher_source_record_with_analysis"
        assert item["external_url"].startswith("https://")
        assert item["external_url"] != item["url"]
        assert "Read the original:" in item["content_text"]


def test_feed_directory_contract_and_service_worker_cover_every_endpoint() -> None:
    directory = (ROOT / "feeds/index.html").read_text(encoding="utf-8")
    contract = (ROOT / "docs/FEED-QUALITY.md").read_text(encoding="utf-8")
    worker = (ROOT / "sw.js").read_text(encoding="utf-8")

    assert "7 RSS + 7 JSON Feed" in directory
    assert "Four kinds of item" in directory
    for item in FEEDS.values():
        for endpoint in (item["rss"], item["json"]):
            relative = f"/{endpoint}"
            assert relative in directory
            assert relative in contract
            assert f'"{relative}"' in worker


def test_progress_note_answers_the_criticism_without_hiding_ai_assistance() -> None:
    update = (
        ROOT / "updates/2026-08-17-listening-pass/index.html"
    ).read_text(encoding="utf-8")

    for phrase in (
        "Thanks. I worked on these.",
        "I still use AI assistance.",
        "More criticism is welcome.",
        "14 public feed endpoints",
        "not a replacement newspaper",
    ):
        assert phrase in update
