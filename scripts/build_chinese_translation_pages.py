#!/usr/bin/env python3
"""Render the public English desk from the sealed Chinese translation sidecar."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from jsonschema import Draft202012Validator, FormatChecker

from core import newswire as newswire_model
from scripts import site_nav


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "readings" / "chinese-translations-latest.json"
DEFAULT_SCHEMA = ROOT / "protocol" / "chinese-translations-v1.schema.json"
DEFAULT_OUTPUT_ROOT = ROOT / "news" / "china" / "english"
PAGE_SIZE = 80
SITE = "https://palimpsest.info"
_PAGINATION_PATH = re.compile(
    r"^news/china/english/page/(?:[2-9]|[1-9][0-9]+)/index\.html$"
)
_REGIONAL_TERMS = (
    "belt and road",
    "cpec",
    "gwadar",
    "baloch",
    "balochistan",
    "myanmar",
    "burma",
    "rakhine",
    "rohingya",
    "kyaukpyu",
    "cmec",
    "shan state",
    "kachin",
    "一带一路",
    "一帶一路",
    "中巴经济走廊",
    "中巴經濟走廊",
    "瓜达尔",
    "瓜達爾",
    "俾路支",
    "俾路支斯坦",
    "巴洛奇",
    "缅甸",
    "緬甸",
    "中缅经济走廊",
    "中緬經濟走廊",
    "皎漂",
    "若开",
    "若開",
    "掸邦",
    "撣邦",
    "克钦",
    "克欽",
)


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sidecar_bytes(value: object) -> bytes:
    """Reproduce the translation builder's checked-in serialization exactly."""

    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_translations(
    path: Path = DEFAULT_INPUT,
    *,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict:
    raw = path.read_bytes()
    document = newswire_model.strict_json_loads(raw, label=str(path))
    schema = newswire_model.strict_json_loads(
        schema_path.read_bytes(), label=str(schema_path)
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    if document["schema_version"] != "chinese-translations-v1":
        raise ValueError("unsupported Chinese translation sidecar")
    translations = document["translations"]
    ids = [row["translation_id"] for row in translations]
    if len(ids) != len(set(ids)):
        raise ValueError("Chinese translation IDs must be unique")
    return document


def _published_at(row: Mapping[str, object]) -> str:
    clocks = row["source_clocks"]
    return str(
        clocks.get("published_at")
        or clocks.get("updated_at")
        or clocks.get("collected_at")
        or "1970-01-01T00:00:00Z"
    )


def _rss_date(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return format_datetime(parsed)


def _identity_key(row: Mapping[str, object]) -> str:
    identity = row["identity"]
    return str(
        identity.get("item_version_id")
        or identity.get("event_version_id")
        or row["translation_id"]
    )


def _is_regional(row: Mapping[str, object]) -> bool:
    original = row["original_zh"]
    translated = row["english"]
    haystack = " ".join(
        str(value)
        for value in (
            original.get("title"),
            original.get("context"),
            translated.get("title_en"),
            translated.get("context_en"),
            translated.get("background_en"),
        )
        if value
    ).casefold()
    return any(term.casefold() in haystack for term in _REGIONAL_TERMS)


def _section(row: Mapping[str, object]) -> str:
    if _is_regional(row):
        return "BRI and borderlands"
    topics = {str(topic).casefold() for topic in row.get("topics", [])}
    if topics.intersection({"economy", "trade", "technology"}):
        return "Economics and technology"
    if topics.intersection({"rights", "politics", "censorship", "security"}):
        return "Rights and politics"
    return "Other captured reporting"


def _publisher_links(row: Mapping[str, object]) -> str:
    seen = set()
    links = []
    for source in row["source_records"]:
        url = source.get("publisher_url")
        key = (source["source_id"], url)
        if key in seen:
            continue
        seen.add(key)
        label = _h(source["source_name"])
        if url:
            links.append(
                f'<a href="{_h(url)}" target="_blank" rel="noopener">{label}</a>'
            )
        else:
            links.append(label)
    return "; ".join(links) or "publisher link unavailable in the retained record"


def _background_basis(value: object) -> str:
    if type(value) is list:
        return "; ".join(_h(item) for item in value)
    if type(value) is dict:
        return "; ".join(f"{_h(key)}: {_h(item)}" for key, item in value.items())
    return _h(value or "retained publisher metadata only")


def _translation_card(row: Mapping[str, object]) -> str:
    original = row["original_zh"]
    translated = row["english"]
    title_en = translated.get("title_en") or "English translation unavailable"
    context_en = translated.get("context_en") or "No bounded excerpt was retained."
    original_context = original.get("context") or "No bounded excerpt was retained."
    background = translated.get("background_en") or (
        "No contextual note was produced from the retained metadata."
    )
    palimpsest_url = row.get("palimpsest_url")
    title_markup = _h(title_en)
    if palimpsest_url:
        title_markup = f'<a href="{_h(palimpsest_url)}">{title_markup}</a>'
    receipt = row["translation_provenance"]
    notes = translated.get("translation_notes_en")
    notes_markup = f'<p><b>Translation note:</b> {_h(notes)}</p>' if notes else ""
    return f'''<article class="ct-card" id="translation-{_h(row["translation_id"])}" data-ct-section="{_h(_section(row))}">
  <header><p class="ct-kicker">{_h(_section(row))} · <time datetime="{_h(_published_at(row))}">{_h(_published_at(row))}</time></p><h2>{title_markup}</h2></header>
  <section class="ct-translation" aria-label="English translation of publisher metadata"><h3>English translation of captured publisher metadata</h3><p>{_h(context_en)}</p>{notes_markup}</section>
  <aside class="ct-background" aria-label="Background and why this matters"><h3>Background / why this matters</h3><p>{_h(background)}</p><p><small><b>Basis:</b> {_background_basis(translated.get("background_basis"))}. This context is Palimpsest synthesis and is not part of the publisher's Chinese text.</small></p></aside>
  <details class="ct-original"><summary>Read the original captured Chinese metadata</summary><div lang="zh"><h3>{_h(original["title"])}</h3><p>{_h(original_context)}</p></div></details>
  <footer><p><b>Publisher record:</b> {_publisher_links(row)}.</p><p><small>Translation status: <code>{_h(translated["status"])}</code> · background status: <code>{_h(translated["background_status"])}</code> · method: <code>{_h(receipt["provider"])} / {_h(receipt["model_id"])}</code> · source kind: <code>{_h(row["record_kind"])}</code> · identity: <code>{_h(_identity_key(row))}</code> · receipt: <code>{_h(row["translation_id"])}</code>.</small></p></footer>
</article>'''


def _pagination_href(page_number: int) -> str:
    return "/news/china/english/" if page_number == 1 else f"/news/china/english/page/{page_number}/"


def _render_page(
    document: Mapping[str, object],
    rows: list[dict],
    *,
    page_number: int,
    page_count: int,
    total_count: int,
    section_counts: Counter[str],
) -> bytes:
    title = "Chinese news in English" + (
        "" if page_number == 1 else f" · archive page {page_number}"
    )
    canonical = _pagination_href(page_number)
    previous_link = (
        f'<a href="{_pagination_href(page_number - 1)}">← Newer translations</a>'
        if page_number > 1
        else ""
    )
    next_link = (
        f'<a href="{_pagination_href(page_number + 1)}">Older translations →</a>'
        if page_number < page_count
        else ""
    )
    section_summary = "".join(
        f'<li><strong>{count}</strong><span>{_h(section)}</span></li>'
        for section, count in section_counts.most_common()
    )
    grouped_cards = []
    for section in (
        "BRI and borderlands",
        "Economics and technology",
        "Rights and politics",
        "Other captured reporting",
    ):
        section_rows = [row for row in rows if _section(row) == section]
        if not section_rows:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", section.casefold()).strip("-")
        grouped_cards.append(
            f'<section class="ct-group" id="section-{_h(slug)}">'
            f'<header><p class="ct-kicker">English translation section</p>'
            f'<h2>{_h(section)}</h2><p>{len(section_rows)} records on this archive page.</p></header>'
            + "\n".join(_translation_card(row) for row in section_rows)
            + "</section>"
        )
    cards = "\n".join(grouped_cards)
    page_start = (page_number - 1) * PAGE_SIZE + 1 if rows else 0
    page_end = page_start + len(rows) - 1 if rows else 0
    return f'''<!doctype html>
<html lang="en" data-tk-theme="light">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_h(title)} · Palimpsest</title>
<meta name="description" content="The latest fully translated, admitted snapshot of Chinese-dominant publisher metadata retained by Palimpsest, with original Chinese, provenance, and separate contextual notes.">
<meta name="robots" content="index,follow,max-snippet:-1"><link rel="canonical" href="{SITE}{_h(canonical)}">
<link rel="alternate" type="application/feed+json" href="/news/china/english/feed.json" title="Palimpsest Chinese news in English JSON Feed">
<link rel="alternate" type="application/rss+xml" href="/news/china/english/feed.xml" title="Palimpsest Chinese news in English RSS">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
{site_nav.HEAD}<link rel="stylesheet" href="/assets/chinese-translations.css">
</head>
<body class="ps tk ct-page">
<!-- GENERATED BY scripts/build_chinese_translation_pages.py -->
{site_nav.render('/news/china/english/')}
<main id="main">
  <header class="ct-hero"><p class="ct-kicker">Chinese-language evidence desk · admitted snapshot {_h(document["generated_at"])}</p><h1>Chinese news, in English—with the original still attached.</h1><p class="ct-dek">This is Palimpsest’s latest fully translated, admitted snapshot of Chinese-dominant titles and bounded feed excerpts. Newer Evidence Wire records may await translation; the snapshot clock and source receipt below keep that lag visible. Palimpsest does not fetch or republish article bodies. Translation and added background are separate, and every record links back to the publisher where the captured metadata permits.</p><nav class="ct-tabs" aria-label="Translation desk sections"><a href="#translated-records">All translated records</a><a href="/belt-and-road/">BRI and borderlands</a><a href="/news/china/">Original China stream</a><a href="/news/china/english/feed.json">JSON Feed</a><a href="/readings/chinese-translations-latest.json">Complete data</a></nav><ul class="ct-stats"><li><strong>{total_count}</strong><span>translated retained records</span></li>{section_summary}</ul><p class="ct-boundary"><strong>Rights boundary.</strong> Publisher bodies remain on publisher sites. English fields translate only the title and short feed context Palimpsest captured under its metadata-link-only policy. Machine-reviewed does not mean human-certified perfection.</p></header>
  <section class="ct-list" id="translated-records"><header><p class="ct-kicker">Archive page {page_number} of {page_count}</p><h2>Records {page_start}–{page_end} of {total_count}</h2><p>English appears first for accessibility; open each original block to audit the Chinese captured text and translation receipt. Records on each archive page are separated into BRI and borderlands, economics and technology, rights and politics, and other captured reporting.</p></header>{cards}</section>
  <nav class="ct-pagination" aria-label="Translation archive pagination">{previous_link}<span>Page {page_number} / {page_count}</span>{next_link}</nav>
</main>
<footer class="ct-footer"><p><strong>Palimpsest Chinese news in English</strong> · translation is a separately versioned interpretation layer, never a silent mutation of the publisher record.</p><p><a href="/readings/chinese-translations-latest.json">Complete translation sidecar</a> · <a href="/protocol/chinese-translations-v1.schema.json">Schema</a> · <a href="/challenge.html">Challenge a translation or context note</a></p></footer>
{site_nav.FOOT}
</body></html>'''.encode("utf-8")


def _current_rows(rows: list[dict]) -> list[dict]:
    current = [
        row
        for row in rows
        if row["record_kind"] in {"current_wire_item", "current_wire_event", "retained_current_story"}
    ]
    return current or rows[: min(len(rows), 200)]


def translation_public_paths(document: Mapping[str, object]) -> dict[str, str]:
    """Map each immutable translation receipt to its exact paginated anchor."""

    rows = sorted(
        document["translations"],
        key=lambda row: (_published_at(row), row["translation_id"]),
        reverse=True,
    )
    paths = {}
    for position, row in enumerate(rows):
        page_number = position // PAGE_SIZE + 1
        paths[row["translation_id"]] = (
            _pagination_href(page_number)
            + f'#translation-{row["translation_id"]}'
        )
    return paths


def _json_feed(document: Mapping[str, object], rows: list[dict]) -> bytes:
    public_paths = translation_public_paths(document)
    items = []
    for row in _current_rows(rows):
        translated = row["english"]
        original = row["original_zh"]
        url = SITE + public_paths[row["translation_id"]]
        publisher_url = next(
            (
                source.get("publisher_url")
                for source in row["source_records"]
                if source.get("publisher_url")
            ),
            None,
        )
        content = (
            f"English translation of captured publisher metadata: {translated.get('context_en') or 'No bounded excerpt retained.'}\n\n"
            f"Background / why this matters (Palimpsest synthesis, not part of the translation): {translated.get('background_en') or 'Unavailable.'}\n\n"
            f"Original Chinese: {original['title']} — {original.get('context') or 'No bounded excerpt retained.'}"
        )
        item = {
            "id": row["translation_id"],
            "url": url,
            "title": translated.get("title_en") or "English translation unavailable",
            "content_text": content,
            "date_published": _published_at(row),
            "language": "en",
            "tags": row.get("topics", []),
            "_palimpsest": {
                "original_language": "zh",
                "translation_status": translated["status"],
                "translation_id": row["translation_id"],
            },
        }
        if publisher_url:
            item["external_url"] = publisher_url
        items.append(item)
    return _json_bytes(
        {
            "version": "https://jsonfeed.org/version/1.1",
            "title": "Palimpsest Chinese news in English",
            "home_page_url": f"{SITE}/news/china/english/",
            "feed_url": f"{SITE}/news/china/english/feed.json",
            "description": "Latest fully translated, admitted snapshot of retained Chinese publisher metadata; newer Evidence Wire records may await translation.",
            "language": "en",
            "items": items,
        }
    )


def _rss_feed(rows: list[dict]) -> bytes:
    document = {"translations": rows}
    public_paths = translation_public_paths(document)
    items = []
    for row in _current_rows(rows):
        translated = row["english"]
        original = row["original_zh"]
        link = SITE + public_paths[row["translation_id"]]
        description = (
            f"English translation of captured metadata: {translated.get('context_en') or 'No bounded excerpt retained.'} "
            f"Background (Palimpsest synthesis): {translated.get('background_en') or 'Unavailable.'} "
            f"Original Chinese title: {original['title']}"
        )
        items.append(
            "<item>"
            f"<guid isPermaLink=\"false\">{xml_escape(row['translation_id'])}</guid>"
            f"<title>{xml_escape(translated.get('title_en') or 'English translation unavailable')}</title>"
            f"<link>{xml_escape(str(link))}</link>"
            f"<description>{xml_escape(description)}</description>"
            f"<pubDate>{xml_escape(_rss_date(_published_at(row)))}</pubDate>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel><title>Palimpsest Chinese news in English</title>'
        f'<link>{SITE}/news/china/english/</link><description>Latest fully translated, admitted snapshot of retained Chinese publisher metadata; newer Evidence Wire records may await translation.</description><language>en</language>'
        + "".join(items)
        + "</channel></rss>\n"
    ).encode("utf-8")


def build_outputs(document: Mapping[str, object]) -> dict[Path, bytes]:
    rows = sorted(
        document["translations"],
        key=lambda row: (_published_at(row), row["translation_id"]),
        reverse=True,
    )
    if not rows:
        raise ValueError("translation sidecar contains no records")
    page_count = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    section_counts = Counter(_section(row) for row in rows)
    outputs = {}
    for page_number in range(1, page_count + 1):
        chunk = rows[(page_number - 1) * PAGE_SIZE : page_number * PAGE_SIZE]
        path = (
            DEFAULT_OUTPUT_ROOT / "index.html"
            if page_number == 1
            else DEFAULT_OUTPUT_ROOT / "page" / str(page_number) / "index.html"
        )
        outputs[path] = _render_page(
            document,
            chunk,
            page_number=page_number,
            page_count=page_count,
            total_count=len(rows),
            section_counts=section_counts,
        )
    outputs[DEFAULT_OUTPUT_ROOT / "feed.json"] = _json_feed(document, rows)
    outputs[DEFAULT_OUTPUT_ROOT / "feed.xml"] = _rss_feed(rows)
    manifest = {
        "schema_version": "palimpsest.chinese-translation-pages-manifest.v1",
        "generated_at": document["generated_at"],
        "source_path": "readings/chinese-translations-latest.json",
        "source_sha256": hashlib.sha256(_sidecar_bytes(document)).hexdigest(),
        "record_count": len(rows),
        "page_count": page_count,
        "files": [
            {
                "path": str(path.relative_to(ROOT)),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
            for path, payload in sorted(outputs.items(), key=lambda item: str(item[0]))
        ],
    }
    outputs[DEFAULT_OUTPUT_ROOT / "generated-manifest.json"] = _json_bytes(manifest)
    return outputs


def _stale_pagination_paths(expected: set[Path]) -> list[Path]:
    if not DEFAULT_OUTPUT_ROOT.is_dir():
        return []
    stale = []
    for path in DEFAULT_OUTPUT_ROOT.glob("page/*/index.html"):
        relative = str(path.relative_to(ROOT))
        if _PAGINATION_PATH.fullmatch(relative) and path not in expected:
            stale.append(path)
    return sorted(stale)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    document = load_translations(args.input, schema_path=args.schema)
    outputs = build_outputs(document)
    stale = _stale_pagination_paths(set(outputs))
    drift = [
        path
        for path, payload in outputs.items()
        if not path.is_file() or path.read_bytes() != payload
    ]
    if args.check:
        if drift or stale:
            print(
                "Chinese translation page drift: "
                + ", ".join(str(path.relative_to(ROOT)) for path in (*drift, *stale))
            )
            return 2
        print("Chinese translation pages: exact")
        return 0
    for path, payload in outputs.items():
        _atomic_write(path, payload)
        print(f"wrote {path.relative_to(ROOT)} ({len(payload)} bytes)")
    for path in stale:
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
