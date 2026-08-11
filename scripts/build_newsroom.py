#!/usr/bin/env python3
"""Publish the Palimpsest Wire from the normalized China OSINT board.

This is a renderer, not a collector. ``core.newsroom`` owns the strict editorial
contract; this module turns that already-validated contract into static HTML,
per-story JSON, JSON Feed, RSS and a sitemap. Every output is built in memory
before any destination is replaced, so invalid source data cannot erase the
last known-good edition.

    PYTHONPATH=. python -m scripts.build_newsroom
    PYTHONPATH=. python -m scripts.build_newsroom --check
"""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from xml.sax.saxutils import escape as xml_escape

from core import newsroom
from scripts import site_nav


ROOT = Path(__file__).resolve().parent.parent
NEWS = ROOT / "news"
READING = ROOT / "readings" / "newsroom-latest.json"
SITE = "https://palimpsest.info"
PUBLISHER = "Palimpsest Observatory"
DESCRIPTION = (
    "Evidence-linked dispatches from Palimpsest's China censorship, network, "
    "erasure, state-telemetry and model measurements."
)
OG_IMAGE = f"{SITE}/brand/palimpsest-og2.png"


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json_script(value: object) -> str:
    """Serialize JSON safely inside a script element, including hostile text."""

    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _pretty_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def _parse_time(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise newsroom.NewsroomError(f"timestamp is timezone-free: {value!r}")
    return parsed.astimezone(timezone.utc)


def _human_time(value: str | None) -> str:
    if not value:
        return "not observed"
    return _parse_time(value).strftime("%d %b %Y · %H:%M UTC")


def _rfc2822(value: str) -> str:
    return email.utils.format_datetime(_parse_time(value), usegmt=True)


def _number(value: int | float | None) -> str:
    if value is None:
        return "not reported"
    if isinstance(value, int):
        return f"{value:,}"
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _metric_value(story: Mapping[str, Any]) -> str:
    metric = story["metric"]
    if metric["value"] is None:
        return "No current value"
    value = _number(metric["value"])
    unit = metric["unit"]
    if unit == "percent":
        return f"{value}%"
    if unit == "ratio":
        return f"{_number(metric['value'] * 100)}%"
    return f"{value} {unit}".strip()


def _metric_caption(story: Mapping[str, Any]) -> str:
    metric = story["metric"]
    if metric["label"] is None:
        return f"Source status: {story['status']}"
    text = metric["label"]
    denominator = metric["denominator"]
    if denominator["value"] is not None:
        text += f" · across {_number(denominator['value'])} {denominator['label']}"
    return text


def _status_label(status: str) -> str:
    return {
        "live": "Current evidence",
        "degraded": "Coverage degraded",
        "stale": "Evidence stale",
        "missing": "Source missing",
        "corrupt": "Source unreadable",
    }[status]


def _head(
    *,
    title: str,
    description: str,
    canonical: str,
    page_type: str,
    published_at: str | None = None,
    modified_at: str | None = None,
    json_ld: object,
) -> str:
    article_meta = ""
    if published_at:
        article_meta += (
            f'<meta property="article:published_time" content="{_h(published_at)}">\n'
        )
    if modified_at:
        article_meta += (
            f'<meta property="article:modified_time" content="{_h(modified_at)}">\n'
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)}</title>
<meta name="description" content="{_h(description)}">
<meta name="author" content="{PUBLISHER}">
<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">
<link rel="canonical" href="{_h(canonical)}">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<link rel="alternate" type="application/feed+json" title="Palimpsest Wire JSON Feed" href="/news/feed.json">
<link rel="alternate" type="application/rss+xml" title="Palimpsest Wire RSS" href="/news/feed.xml">
<meta name="theme-color" content="#0b131c">
<meta property="og:type" content="{_h(page_type)}">
<meta property="og:site_name" content="Palimpsest Wire">
<meta property="og:title" content="{_h(title)}">
<meta property="og:description" content="{_h(description)}">
<meta property="og:url" content="{_h(canonical)}">
<meta property="og:image" content="{OG_IMAGE}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_h(title)}">
<meta name="twitter:description" content="{_h(description)}">
<meta name="twitter:image" content="{OG_IMAGE}">
{article_meta}<script type="application/ld+json">{_json_script(json_ld)}</script>
{site_nav.HEAD}
<link rel="stylesheet" href="/assets/newsroom.css">
</head>"""


def _organization() -> dict[str, Any]:
    return {
        "@type": "NewsMediaOrganization",
        "@id": f"{SITE}/#organization",
        "name": PUBLISHER,
        "url": f"{SITE}/",
        "logo": {
            "@type": "ImageObject",
            "url": f"{SITE}/brand/palimpsest-icon-512.png",
            "width": 512,
            "height": 512,
        },
    }


def _receipt(story: Mapping[str, Any]) -> str:
    evidence = story["evidence"]
    source_time = evidence["source_timestamp"]
    digest = evidence["input"]["sha256"]
    filename = evidence["input"]["filename"]
    bytes_value = evidence["input"]["bytes"]
    status_class = "" if story["status"] == "live" else " nw-receipt__state--warning"
    sha = digest or "not available"
    size = f"{bytes_value:,} bytes" if bytes_value is not None else "not available"
    return f"""<aside class="nw-receipt" aria-label="Evidence receipt">
  <p class="nw-receipt__label">Evidence receipt</p>
  <dl>
    <dt>Status</dt>
    <dd><span class="nw-receipt__state{status_class}"><span class="nw-dot" aria-hidden="true"></span>{_h(_status_label(story['status']))}</span></dd>
    <dt>Observed</dt>
    <dd>{_h(_human_time(source_time))}</dd>
    <dt>Source file</dt>
    <dd><a href="{_h(evidence['url'])}">{_h(filename)}</a></dd>
    <dt>Source size</dt>
    <dd>{_h(size)}</dd>
    <dt>SHA-256</dt>
    <dd><code>{_h(sha)}</code></dd>
    <dt>Claim seal</dt>
    <dd><code>{_h(story['claim_fingerprint'])}</code></dd>
  </dl>
</aside>"""


def _story_json_ld(story: Mapping[str, Any], section_title: str) -> dict[str, Any]:
    evidence = story["evidence"]
    return {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "@id": story["url"],
        "mainEntityOfPage": {"@type": "WebPage", "@id": story["url"]},
        "headline": story["headline"],
        "description": story["dek"],
        "datePublished": story["published_at"],
        "dateModified": story["modified_at"],
        "articleSection": section_title,
        "inLanguage": "en",
        "isAccessibleForFree": True,
        "author": _organization(),
        "publisher": _organization(),
        "image": [OG_IMAGE],
        "isBasedOn": evidence["url"],
        "citation": evidence["url"],
        "keywords": [story["section"], story["signal_id"], "China", "open source intelligence"],
    }


def _index_json_ld(feed: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@graph": [
            _organization(),
            {
                "@type": "CollectionPage",
                "@id": feed["url"],
                "url": feed["url"],
                "name": feed["title"],
                "description": feed["scope"],
                "dateModified": feed["generated_at"],
                "publisher": {"@id": f"{SITE}/#organization"},
                "mainEntity": {
                    "@type": "ItemList",
                    "numberOfItems": feed["n_stories"],
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": index,
                            "url": story["url"],
                            "name": story["headline"],
                        }
                        for index, story in enumerate(feed["stories"], 1)
                    ],
                },
            },
        ],
    }


def _story_card(story: Mapping[str, Any], section_title: str) -> str:
    evidence = story["evidence"]
    digest = evidence["input"]["sha256"]
    short_hash = digest[:12] if digest else "no-source-hash"
    status_class = "" if story["status"] == "live" else " nw-kicker--warning"
    return f"""<article class="nw-card" data-status="{_h(story['status'])}">
  <p class="nw-card__kicker{status_class}">{_h(section_title)} · {_h(_status_label(story['status']))}</p>
  <h3><a class="nw-card__link" href="/{_h(story['url'].removeprefix(SITE).lstrip('/'))}">{_h(story['headline'])}</a></h3>
  <p class="nw-card__dek">{_h(story['dek'])}</p>
  <p class="nw-card__metric"><strong>{_h(_metric_value(story))}</strong>{_h(_metric_caption(story))}</p>
  <p class="nw-card__meta"><time datetime="{_h(story['published_at'])}">{_h(_human_time(story['published_at']))}</time><span class="nw-card__hash">sha {short_hash}</span></p>
</article>"""


def _lead(story: Mapping[str, Any], section_title: str) -> str:
    qualifier = story["limitations"][0]
    status_class = "" if story["status"] == "live" else " nw-kicker--warning"
    return f"""<section class="nw-lead" aria-labelledby="lead-headline">
  <div>
    <p class="nw-kicker{status_class}">{_h(section_title)} · {_h(_status_label(story['status']))}</p>
    <h1 id="lead-headline">{_h(story['headline'])}</h1>
    <p class="nw-lead__dek">{_h(story['dek'])}</p>
    <p class="nw-lead__qualifier"><strong>Read with this qualifier:</strong> {_h(qualifier)}</p>
    <div class="nw-actions">
      <a class="nw-actions__primary" href="/{_h(story['url'].removeprefix(SITE).lstrip('/'))}">Read the evidence-linked report</a>
      <a href="/readings/newsroom-latest.json">Structured edition</a>
      <a href="/osint-china.html">Open evidence desk</a>
    </div>
  </div>
  {_receipt(story)}
</section>"""


def _select_lead(stories: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    lead = next((story for story in stories if story["priority"] == "lead" and story["status"] == "live"), None)
    if lead is None:
        lead = next((story for story in stories if story["status"] == "live"), None)
    if lead is None:
        lead = stories[0]
    return lead


def render_index(feed: Mapping[str, Any]) -> str:
    stories = feed["stories"]
    sections = {section["id"]: section for section in feed["sections"]}
    lead = _select_lead(stories)
    coverage = feed["coverage"]
    live = coverage["live"]
    reporting = coverage["reporting"]
    warnings = coverage["total"] - live
    navigation = "".join(
        f'<li><a href="#{_h(section["id"])}">{_h(section["title"])}</a></li>'
        for section in feed["sections"]
    )
    section_blocks = []
    for section in feed["sections"]:
        section_stories = [
            story for story in stories
            if story["section"] == section["id"] and story["id"] != lead["id"]
        ]
        if not section_stories:
            continue
        cards = "\n".join(_story_card(story, section["title"]) for story in section_stories)
        section_blocks.append(f"""<section class="nw-section" id="{_h(section['id'])}">
  <div class="nw-section__head">
    <div><p class="nw-section__label">{section['order']:02d} / Desk</p><h2>{_h(section['title'])}</h2></div>
    <p class="nw-section__dek">{_h(section['dek'])}</p>
  </div>
  <div class="nw-grid">{cards}</div>
</section>""")
    gaps = [story for story in stories if story["status"] != "live"]
    gap_items = "\n".join(
        f"""<div class="nw-coverage__item"><strong>{_h(story['headline'])}</strong><p>{_h(story['limitations'][0])}</p></div>"""
        for story in gaps
    ) or '<div class="nw-coverage__item"><strong>All sources are current</strong><p>No coverage qualifier is active in this edition.</p></div>'
    body = f"""<body class="ps newsroom-page">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-masthead">
    <div class="nw-masthead__top">
      <p class="nw-wordmark">Palimpsest <span>Wire</span></p>
      <p class="nw-edition"><strong>Verified edition</strong>{_h(_human_time(feed['generated_at']))}<br>{feed['n_stories']} evidence-linked dispatches</p>
    </div>
    <p class="nw-masthead__dek">Measurements become readable reports without losing their source, denominator, freshness or limits. Automated wording; no causal inference.</p>
  </header>
  <div class="nw-meta-line"><span>China · open-source evidence</span><span>Updated <time datetime="{_h(feed['generated_at'])}">{_h(_human_time(feed['generated_at']))}</time></span><a href="/news/feed.xml">RSS</a><a href="/news/feed.json">JSON Feed</a><a href="/readings/newsroom-latest.json">Full structured edition</a></div>
  <div class="nw-status-strip" role="status" aria-label="Edition coverage">
    <span><i class="nw-dot nw-dot--live" aria-hidden="true"></i><strong>{live}</strong> live</span>
    <span><i class="nw-dot nw-dot--warning" aria-hidden="true"></i><strong>{reporting}</strong> reporting</span>
    <span><i class="nw-dot nw-dot--missing" aria-hidden="true"></i><strong>{warnings}</strong> qualified</span>
    <span><strong>{coverage['total']}</strong> total instruments</span>
  </div>
  <nav aria-label="News desks"><ul class="nw-section-nav">{navigation}</ul></nav>
  {_lead(lead, sections[lead['section']]['title'])}
  {''.join(section_blocks)}
  <aside class="nw-coverage" aria-labelledby="coverage-title">
    <div><p class="nw-kicker nw-kicker--warning">Coverage desk</p><h2 id="coverage-title">What we cannot currently claim</h2></div>
    <div class="nw-coverage__items">{gap_items}</div>
  </aside>
</main>
<footer class="nw-footer"><div class="nw-shell">Palimpsest Wire is generated deterministically from the public <a href="/readings/osint-china-latest.json">OSINT China roll-up</a>. Every story links to its exact evidence bytes. <a href="/docs/NEWSROOM.md">Editorial rules</a> · <a href="https://github.com/beepboop2025/palimpsest">Source code</a>.</div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    title = "Palimpsest Wire · evidence-linked China intelligence"
    return _head(
        title=title,
        description=DESCRIPTION,
        canonical=feed["url"],
        page_type="website",
        modified_at=feed["generated_at"],
        json_ld=_index_json_ld(feed),
    ) + "\n" + body


def render_story(
    story: Mapping[str, Any],
    *,
    section: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    claim_items = "\n".join(
        f'<p><strong>{_h(claim["type"].replace("_", " ").title())}.</strong> {_h(claim["statement"])}</p>'
        for claim in story["claims"]
    )
    limitations = "\n".join(f"<li>{_h(item)}</li>" for item in story["limitations"])
    related = "\n".join(
        f'<a href="/{_h(by_id[signal_id]["url"].removeprefix(SITE).lstrip("/"))}">{_h(by_id[signal_id]["headline"])}</a>'
        for signal_id in story["related_signal_ids"]
        if signal_id in by_id
    ) or "<p>No related dispatch is declared for this instrument.</p>"
    metric = ""
    if story["metric"]["value"] is not None:
        metric = f"""<div class="nw-metric-block" aria-label="Headline metric"><strong>{_h(_metric_value(story))}</strong><span>{_h(_metric_caption(story))}</span></div>"""
    status_class = "" if story["status"] == "live" else " nw-kicker--warning"
    body = f"""<body class="ps newsroom-page">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <article class="nw-article">
    <header class="nw-article__header">
      <p class="nw-article__kicker{status_class}">{_h(section['title'])} · {_h(_status_label(story['status']))}</p>
      <h1>{_h(story['headline'])}</h1>
      <p class="nw-article__dek">{_h(story['dek'])}</p>
      <p class="nw-article__meta"><span>By {PUBLISHER}</span><time datetime="{_h(story['published_at'])}">{_h(_human_time(story['published_at']))}</time><span>Automated evidence brief</span></p>
    </header>
    <div class="nw-article__layout">
      <div class="nw-article__body">
        {metric}
        <h2>What the record says</h2>
        {claim_items}
        <p>This report is scoped to <strong>{_h(story['signal_id'])}</strong>. It does not merge unlike instruments or infer a cause from co-movement.</p>
        <h2>How it was measured</h2>
        <p>{_h(story['method']['summary'])}</p>
        <h2>What this cannot establish</h2>
        <ul class="nw-limitations">{limitations}</ul>
        <h2>Read the evidence</h2>
        <p>The exact source reading is <a href="{_h(story['evidence']['url'])}">{_h(story['evidence']['input']['filename'])}</a>. The structured version of this dispatch is <a href="story.json">published beside the article</a>.</p>
      </div>
      <aside class="nw-article__rail">
        {_receipt(story)}
        <div class="nw-related"><p class="nw-receipt__label">Related dispatches</p>{related}</div>
      </aside>
    </div>
  </article>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Latest edition</a> · <a href="/osint-china.html">Evidence desk</a> · <a href="/news/feed.xml">RSS</a> · <a href="/readings/newsroom-latest.json">Structured edition</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title=f"{story['headline']} · Palimpsest Wire",
        description=story["dek"],
        canonical=story["url"],
        page_type="article",
        published_at=story["published_at"],
        modified_at=story["modified_at"],
        json_ld=_story_json_ld(story, section["title"]),
    ) + "\n" + body


def build_json_feed(feed: Mapping[str, Any]) -> dict[str, Any]:
    sections = {section["id"]: section["title"] for section in feed["sections"]}
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": feed["title"],
        "home_page_url": feed["url"],
        "feed_url": f"{SITE}/news/feed.json",
        "description": feed["scope"],
        "language": "en",
        "authors": [{"name": PUBLISHER, "url": f"{SITE}/"}],
        "items": [
            {
                "id": story["id"] + ":" + story["claim_fingerprint"],
                "url": story["url"],
                "external_url": story["evidence"]["url"],
                "title": story["headline"],
                "summary": story["dek"],
                "content_text": "\n\n".join(
                    [claim["statement"] for claim in story["claims"]]
                    + ["Limitations: " + " ".join(story["limitations"])]
                ),
                "date_published": story["published_at"],
                "date_modified": story["modified_at"],
                "tags": [sections[story["section"]], story["signal_id"], story["status"]],
                "attachments": [{
                    "url": story["evidence"]["url"],
                    "mime_type": "application/json",
                    "title": story["evidence"]["input"]["filename"],
                    **(
                        {"size_in_bytes": story["evidence"]["input"]["bytes"]}
                        if story["evidence"]["input"]["bytes"] is not None
                        else {}
                    ),
                }],
            }
            for story in feed["stories"]
        ],
    }


def build_rss(feed: Mapping[str, Any]) -> bytes:
    items = []
    for story in feed["stories"]:
        description = story["dek"] + " Evidence: " + story["evidence"]["url"]
        items.append(f"""  <item>
    <title>{xml_escape(story['headline'])}</title>
    <link>{xml_escape(story['url'])}</link>
    <guid isPermaLink="false">{xml_escape(story['id'] + ':' + story['claim_fingerprint'])}</guid>
    <pubDate>{_rfc2822(story['published_at'])}</pubDate>
    <description>{xml_escape(description)}</description>
    <category>{xml_escape(story['section'])}</category>
    <source url="{xml_escape(story['evidence']['url'])}">{xml_escape(story['signal_id'])}</source>
  </item>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{xml_escape(feed['title'])}</title>
  <link>{xml_escape(feed['url'])}</link>
  <description>{xml_escape(feed['scope'])}</description>
  <language>en</language>
  <lastBuildDate>{_rfc2822(feed['generated_at'])}</lastBuildDate>
  <atom:link href="{SITE}/news/feed.xml" rel="self" type="application/rss+xml" />
{chr(10).join(items)}
</channel>
</rss>
"""
    return xml.encode("utf-8")


def build_sitemap(feed: Mapping[str, Any]) -> bytes:
    urls = [
        f"""  <url><loc>{SITE}/news/</loc><lastmod>{xml_escape(feed['generated_at'])}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>"""
    ]
    for story in feed["stories"]:
        news_markup = ""
        if story["status"] == "live":
            news_markup = f"""<news:news><news:publication><news:name>Palimpsest Wire</news:name><news:language>en</news:language></news:publication><news:publication_date>{xml_escape(story['published_at'])}</news:publication_date><news:title>{xml_escape(story['headline'])}</news:title></news:news>"""
        urls.append(
            f"  <url><loc>{xml_escape(story['url'])}</loc><lastmod>{xml_escape(story['modified_at'])}</lastmod>{news_markup}</url>"
        )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
{chr(10).join(urls)}
</urlset>
"""
    return xml.encode("utf-8")


def build_outputs(feed: Mapping[str, Any]) -> dict[Path, bytes]:
    """Return every public output without touching the filesystem."""

    sections = {section["id"]: section for section in feed["sections"]}
    stories = {story["signal_id"]: story for story in feed["stories"]}
    outputs: dict[Path, bytes] = {
        Path("readings/newsroom-latest.json"): _pretty_json(feed),
        Path("news/index.html"): render_index(feed).encode("utf-8"),
        Path("news/feed.json"): _pretty_json(build_json_feed(feed)),
        Path("news/feed.xml"): build_rss(feed),
        Path("news/sitemap.xml"): build_sitemap(feed),
    }
    for story in feed["stories"]:
        base = Path("news") / story["slug"]
        outputs[base / "index.html"] = render_story(
            story,
            section=sections[story["section"]],
            by_id=stories,
        ).encode("utf-8")
        outputs[base / "story.json"] = _pretty_json(story)
    return outputs


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def publish(outputs: Mapping[Path, bytes], *, root: Path = ROOT) -> tuple[int, int]:
    changed = unchanged = 0
    for relative, payload in sorted(outputs.items(), key=lambda item: str(item[0])):
        destination = root / relative
        try:
            current = destination.read_bytes()
        except FileNotFoundError:
            current = None
        if current == payload:
            unchanged += 1
            continue
        _atomic_write(destination, payload)
        changed += 1
    return changed, unchanged


def check(outputs: Mapping[Path, bytes], *, root: Path = ROOT) -> list[str]:
    drift = []
    for relative, payload in sorted(outputs.items(), key=lambda item: str(item[0])):
        destination = root / relative
        try:
            current = destination.read_bytes()
        except FileNotFoundError:
            drift.append(f"missing {relative}")
            continue
        if current != payload:
            drift.append(f"stale {relative}")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report generated-file drift without writing")
    args = parser.parse_args(argv)
    feed = newsroom.build_news_feed()
    outputs = build_outputs(feed)
    if args.check:
        drift = check(outputs)
        for item in drift:
            print(item)
        if drift:
            print(f"newsroom drift: {len(drift)} file(s)")
            return 1
        print(f"newsroom current: {len(outputs)} files")
        return 0
    changed, unchanged = publish(outputs)
    print(
        f"newsroom -> {READING.relative_to(ROOT)} · {feed['n_stories']} stories · "
        f"{changed} files updated · {unchanged} unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
