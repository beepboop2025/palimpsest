"""Publication contracts for the Chinese-to-English evidence desk."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

from scripts.build_chinese_translation_pages import (
    DEFAULT_OUTPUT_ROOT,
    PAGE_SIZE,
    build_outputs,
    load_translations,
    translation_public_paths,
)
from scripts.build_chinese_translations import is_chinese_dominant


ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / "readings" / "newswire-latest.json"


def test_sidecar_covers_every_current_chinese_dominant_wire_item() -> None:
    document = load_translations()
    wire = json.loads(WIRE.read_text(encoding="utf-8"))
    expected = {
        item["version_id"]
        for item in wire["items"]
        if is_chinese_dominant(f'{item["title"]}\n{item["excerpt"]}')
    }
    translated = {
        row["identity"]["item_version_id"]
        for row in document["translations"]
        if row["record_kind"] == "current_wire_item"
    }

    assert translated == expected
    assert document["coverage"]["eligible_current_items"] == len(expected)
    assert document["coverage"]["translated_current_items"] == len(expected)
    assert document["coverage"]["candidate_records"] == document["coverage"][
        "translated_records"
    ]
    assert document["coverage"]["missing_records"] == 0
    assert document["rights"]["article_bodies_submitted"] is False
    assert document["rights"]["originals_preserved"] is True
    assert document["source_snapshot"]["newswire_sha256"] == hashlib.sha256(
        WIRE.read_bytes()
    ).hexdigest()


def test_translation_pages_publish_every_receipt_once_with_context_separate() -> None:
    document = load_translations()
    outputs = build_outputs(document)
    paths = translation_public_paths(document)
    html_outputs = {
        path: payload for path, payload in outputs.items() if path.suffix == ".html"
    }

    assert len(html_outputs) == (
        len(document["translations"]) + PAGE_SIZE - 1
    ) // PAGE_SIZE
    for translation_id, public_path in paths.items():
        route, anchor = public_path.split("#", 1)
        relative = route.removeprefix("/") + "index.html"
        page_path = ROOT / relative
        assert page_path in html_outputs
        assert html_outputs[page_path].count(
            f'id="{anchor}"'.encode("utf-8")
        ) == 1

    first_page = html_outputs[DEFAULT_OUTPUT_ROOT / "index.html"]
    assert b"English translation of captured publisher metadata" in first_page
    assert b"Background / why this matters" in first_page
    assert b"not part of the publisher's Chinese text" in first_page
    assert b'<div lang="zh">' in first_page
    assert b"Machine-reviewed does not mean human-certified perfection" in first_page
    assert b"latest fully translated, admitted snapshot" in first_page
    assert b"Newer Evidence Wire records may await translation" in first_page


def test_translation_feeds_and_manifest_are_exact_public_outputs() -> None:
    document = load_translations()
    outputs = build_outputs(document)
    feed = json.loads(outputs[DEFAULT_OUTPUT_ROOT / "feed.json"])
    ElementTree.fromstring(outputs[DEFAULT_OUTPUT_ROOT / "feed.xml"])
    manifest = json.loads(outputs[DEFAULT_OUTPUT_ROOT / "generated-manifest.json"])

    assert feed["language"] == "en"
    assert feed["items"]
    assert "latest fully translated, admitted snapshot" in feed["description"].lower()
    assert "newer Evidence Wire records may await translation" in feed["description"]
    assert all(
        item["url"].startswith("https://palimpsest.info/news/china/english/")
        for item in feed["items"]
    )
    assert all(
        "Background / why this matters (Palimpsest synthesis, not part of the translation)"
        in item["content_text"]
        for item in feed["items"]
    )
    assert manifest["record_count"] == len(document["translations"])
    assert manifest["source_path"] == "readings/chinese-translations-latest.json"
    assert manifest["source_sha256"] == hashlib.sha256(
        (ROOT / manifest["source_path"]).read_bytes()
    ).hexdigest()
    assert manifest["page_count"] == (
        len(document["translations"]) + PAGE_SIZE - 1
    ) // PAGE_SIZE
    assert len(manifest["files"]) == len(outputs) - 1


def test_checked_in_translation_pages_are_byte_exact() -> None:
    document = load_translations()
    outputs = build_outputs(document)
    assert outputs
    for path, payload in outputs.items():
        assert path.read_bytes() == payload


def test_translation_feeds_are_visible_in_the_public_feed_directory() -> None:
    directory = (ROOT / "feeds" / "index.html").read_text(encoding="utf-8")
    assert "Chinese news in English" in directory
    assert 'href="/news/china/english/feed.xml"' in directory
    assert 'href="/news/china/english/feed.json"' in directory
    assert 'href="/news/china/english/"' in directory
