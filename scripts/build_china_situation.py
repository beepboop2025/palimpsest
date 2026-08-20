#!/usr/bin/env python3
"""Build the China situation desk from validated public records.

The builder performs no collection. It combines the current Evidence Wire,
deterministic event analyses, and an optional sanitized social-observation
document, plus the existing human-reviewed Dragon Whispers artifact. All outputs
are assembled and validated before atomic replacement.
"""

from __future__ import annotations

import argparse
import email.utils
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

from core import china_situation as situation_model
from core import event_analysis
from core import newswire as newswire_model
from scripts import build_newsroom as newsroom_builder
from scripts import site_nav


ROOT = Path(__file__).resolve().parent.parent
WIRE_PATH = ROOT / "readings" / "newswire-latest.json"
NEWSROOM_PATH = ROOT / "readings" / "newsroom-latest.json"
SOCIAL_PATH = ROOT / "readings" / "social-observations-latest.json"
DRAGON_WHISPERS_PATH = ROOT / "readings" / "dragon-whispers-latest.json"
OUTPUT_PATH = ROOT / "readings" / "china-situation-latest.json"
OSINT_INPUTS = (
    ROOT / "readings" / "ddti-latest.json",
    ROOT / "readings" / "undertext-latest.json",
    ROOT / "readings" / "wayback-latest.json",
    ROOT / "readings" / "weibo-hotsearch-latest.json",
    ROOT / "readings" / "public-deletion-ledgers-latest.json",
    ROOT / "readings" / "github-refuge-latest.json",
)
PAGE_PATH = ROOT / "news" / "china" / "situation" / "index.html"
JSON_FEED_PATH = ROOT / "news" / "china" / "situation" / "feed.json"
RSS_FEED_PATH = ROOT / "news" / "china" / "situation" / "feed.xml"
SITE = "https://palimpsest.info"
FEED_LIMIT = 200
PAGE_SIZE = 48


class ChinaSituationBuildError(ValueError):
    """The checked-in inputs cannot produce a safe situation publication."""


def _strict_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ChinaSituationBuildError(f"required input is missing: {path}")
    value = newswire_model.strict_json_loads(path.read_bytes(), label=str(path))
    if type(value) is not dict:
        raise ChinaSituationBuildError(f"input root must be an object: {path}")
    return value


def load_inputs(
    *,
    wire_path: Path = WIRE_PATH,
    newsroom_path: Path = NEWSROOM_PATH,
    social_path: Path = SOCIAL_PATH,
    dragon_whispers_path: Path = DRAGON_WHISPERS_PATH,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    wire = _strict_document(wire_path)
    feed = _strict_document(newsroom_path)
    analyses = event_analysis.build_event_analyses(wire, feed)
    social = _strict_document(social_path) if social_path.is_file() else None
    reviewed_telegram = (
        _strict_document(dragon_whispers_path) if dragon_whispers_path.is_file() else None
    )
    return wire, analyses, social, reviewed_telegram


def load_osint_observations(paths: Sequence[Path] = OSINT_INPUTS) -> list[dict[str, Any]]:
    """Collect already-published observation records. Missing files stay missing."""

    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            doc = newswire_model.strict_json_loads(path.read_bytes(), label=str(path))
        except Exception:
            continue
        if type(doc) is not dict:
            continue
        for key in ("observation_records", "observations", "ddti_observations"):
            block = doc.get(key)
            if type(block) is list:
                rows.extend(item for item in block if type(item) is dict)
    return rows


def _metric_text(metric: Mapping[str, Any]) -> str:
    value = metric["value"]
    if value is None:
        return "No current numeric value"
    rendered = f"{value:,}" if isinstance(value, int) else f"{value:,.4f}".rstrip("0").rstrip(".")
    unit = metric["unit"] or ""
    label = metric["label"] or "Current value"
    return f"{label}: {rendered} {unit}".strip()


def _status_panel(document: Mapping[str, Any]) -> str:
    inputs = document["inputs"]
    coverage = document["coverage"]
    configured = inputs["social_status"] in {"active", "degraded"}
    state_class = "is-live" if configured else "is-pending"
    heading = (
        "Social observations are connected."
        if configured
        else "Social connectors are ready; credentials and reviewed sources are not active yet."
    )
    detail = (
        f"{coverage['social_observations']} sanitized observations are present; "
        f"{coverage['social_observations_linked']} join an exact publisher article URL."
        if configured
        else (
            "The public desk is already combining publisher reports with Observatory "
            "measurements. Telegram and Instagram will appear only after their "
            "allowlists and official-API permissions are configured."
        )
    )
    return f"""<aside class="situation-social-state {state_class}" aria-labelledby="social-state-title">
  <div><p class="situation-kicker">Social intake · {_h(inputs['social_status'])}</p><h2 id="social-state-title">{_h(heading)}</h2></div>
  <p>{_h(detail)}</p>
  <a href="/docs/SOCIAL-OBSERVATION-PIPELINE.md">Inspect the collection and evidence boundary</a>
</aside>"""


def _telegram_briefing(document: Mapping[str, Any]) -> str:
    rows = document["reviewed_telegram"]
    if not rows:
        body = """<div class="situation-empty"><strong>The human-review queue has no published China signal yet.</strong><p>Raw Telegram forwards remain in Dragon Den. Nothing enters this website until a reviewer removes source identifiers and records uncertainty.</p></div>"""
    else:
        body = "<div class=\"situation-telegram-grid\">" + "".join(
            f"""<article><p class="situation-kicker">{_h(item['tier'])} · {_h(_human(item['published_at']))}</p><h3>{_h(item['headline'])}</h3><p>{_h(item['summary'])}</p><details><summary>Why it matters and what remains unknown</summary><p>{_h(item['why_it_matters'])}</p><p><strong>Uncertainty:</strong> {_h(item['uncertainty'])}</p></details><small>{_h(item['relation'])}</small></article>"""
            for item in rows
        ) + "</div>"
    return f"""<section class="situation-telegram" aria-labelledby="telegram-briefing-title">
  <div><p class="situation-kicker">Reviewed Telegram briefing · {_h(document['inputs']['telegram_status'])}</p><h2 id="telegram-briefing-title">Source-free signals stay separate from attributed reporting.</h2><p>These are human-reviewed Dragon Whispers, not publisher reports and not corroboration. They are displayed beside the event index without being guessed into an event.</p></div>
  {body}
  <p><a href="/news/china/whispers/">Open Dragon Whispers and its review receipts</a></p>
</section>"""


def _reporting_layer(row: Mapping[str, Any]) -> str:
    reporting = row["reporting"]
    sources = "".join(
        f"""<li><a href="{_h(source['url'])}" rel="external"><strong>{_h(source['source_name'])}</strong><span>{_h(source['title'])}</span></a><small>{_h(source['role'])} · {_h(source['independence_group'])} · {_h(_human(source['published_at']))}</small></li>"""
        for source in reporting["sources"]
    )
    return f"""<section class="situation-layer situation-layer--reports" aria-labelledby="reports-{_h(row['situation_id'])}">
  <header><span>01</span><div><p>Publisher wire</p><h3 id="reports-{_h(row['situation_id'])}">{reporting['source_count']} report{'s' if reporting['source_count'] != 1 else ''} · {reporting['independent_groups']} independent group{'s' if reporting['independent_groups'] != 1 else ''}</h3></div></header>
  <ul>{sources}</ul>
  <p class="situation-relation">{_h(reporting['relation'])} · {_h(reporting['evidence_strength'])}</p>
</section>"""


def _social_layer(row: Mapping[str, Any]) -> str:
    observations = row["social_context"]
    if not observations:
        body = """<div class="situation-empty"><strong>No exact-link social observation.</strong><p>This can mean the connector is inactive, the publisher did not post a link, or the post is outside the bounded API window. It is not evidence of silence.</p></div>"""
    else:
        body = "<ul>" + "".join(
            f"""<li><a href="{_h(item['permalink'])}" rel="external"><strong>{_h(item['source_name'])} · {_h(item['platform'])}</strong><span>{_h(item['title'])}</span></a><small>{_h(_human(item['published_at']))} · {_h(item['state'])} · {'same publisher lineage' if item['same_publisher_lineage'] else 'separate attributed source'}</small></li>"""
            for item in observations
        ) + "</ul>"
    return f"""<section class="situation-layer situation-layer--social" aria-labelledby="social-{_h(row['situation_id'])}">
  <header><span>02</span><div><p>Social observation</p><h3 id="social-{_h(row['situation_id'])}">{len(observations)} exact publisher-link observation{'s' if len(observations) != 1 else ''}</h3></div></header>
  {body}
  <p class="situation-relation">publisher-link-context-not-corroboration</p>
</section>"""


def _osint_evidence(item: Mapping[str, Any]) -> str:
    """Show the citeable trail already on the OSINT row. Never invent a snapshot."""

    parts: list[str] = []
    url = item.get("url") or ""
    if isinstance(url, str) and url.startswith("https://"):
        parts.append(f'<a href="{_h(url)}">source URL</a>')
    digest = item.get("content_sha256")
    if isinstance(digest, str) and digest:
        parts.append(f"SHA-256 {_h(digest)}")
    archive = item.get("archive") if isinstance(item.get("archive"), dict) else {}
    snapshot = archive.get("wayback_snapshot")
    lookup = archive.get("wayback_lookup")
    if isinstance(snapshot, str) and snapshot.startswith("https://"):
        parts.append(f'<a href="{_h(snapshot)}">Wayback snapshot</a>')
    elif isinstance(lookup, str) and lookup.startswith("https://"):
        parts.append(f'<a href="{_h(lookup)}">Wayback lookup</a>')
    return " · ".join(parts) if parts else "no public URL or snapshot on this row"


def _osint_layer(row: Mapping[str, Any]) -> str:
    observations = row.get("osint_context") or []
    if not observations:
        body = """<div class="situation-empty"><strong>No linked public OSINT observation.</strong><p>Join is exact publisher URL or an exact gazetteer/topic term. Absence is a coverage gap, not a finding.</p></div>"""
    else:
        body = "<ul>" + "".join(
            f"""<li><strong>{_h(item['source'])}</strong><span>{_h(item['title'])}</span><small>first {_h(item.get('first_seen') or 'unknown')} · last {_h(item.get('last_seen') or 'unknown')} · {_osint_evidence(item)} · {_h(item['relation'])}</small></li>"""
            for item in observations
        ) + "</ul>"
    return f"""<section class="situation-layer situation-layer--osint" aria-labelledby="osint-{_h(row['situation_id'])}">
  <header><span>04</span><div><p>Public OSINT context</p><h3 id="osint-{_h(row['situation_id'])}">{len(observations)} linked observation{'s' if len(observations) != 1 else ''}</h3></div></header>
  {body}
  <p class="situation-relation">topic-or-url-context-not-corroboration</p>
</section>"""


def _measurement_layer(row: Mapping[str, Any]) -> str:
    measurements = row["measurement_context"]
    if not measurements:
        body = """<div class="situation-empty"><strong>No declared Observatory surface.</strong><p>The synthesis stops at publisher structure and social coverage. It does not invent a measurement match.</p></div>"""
    else:
        body = "<ul>" + "".join(
            f"""<li><a href="{_h(item['story_url'])}"><strong>{_h(item['headline'])}</strong><span>{_h(item['finding'])}</span></a><small>{_h(item['status'])} · {_h(_metric_text(item['metric']))} · source {_h(_human(item['source_timestamp']))}</small></li>"""
            for item in measurements
        ) + "</ul>"
    return f"""<section class="situation-layer situation-layer--measurements" aria-labelledby="measurements-{_h(row['situation_id'])}">
  <header><span>03</span><div><p>Observatory measurement</p><h3 id="measurements-{_h(row['situation_id'])}">{len(measurements)} declared surface{'s' if len(measurements) != 1 else ''} · {_h(row['measurement_state'])}</h3></div></header>
  {body}
  <p class="situation-relation">topic-surface-only · not article verification</p>
</section>"""


def _situation_card(row: Mapping[str, Any], *, expanded: bool) -> str:
    topics = "".join(f"<span>{_h(topic)}</span>" for topic in row["topics"])
    checks = "".join(f"<li>{_h(item)}</li>" for item in row["synthesis"]["next_checks"])
    unknowns = "".join(
        f"<li>{_h(item)}</li>" for item in row["synthesis"]["known_unknowns"]
    )
    search = " ".join(
        [row["headline"], row["dek"], row["desk"], *row["topics"]]
        + [source["source_name"] for source in row["reporting"]["sources"]]
    ).casefold()
    return f"""<article class="situation-card" id="{_h(row['situation_id'])}" data-desk="{_h(row['desk'])}" data-posture="{_h(row['posture'])}" data-search="{_h(search)}">
  <header class="situation-card__head"><div><p class="situation-kicker">{_h(row['desk'])} · {_h(row['posture'].replace('-', ' '))}</p><h2>{_h(row['headline'])}</h2><p>{_h(row['dek'])}</p></div><div class="situation-card__meta"><time datetime="{_h(row['updated_at'])}">{_h(_human(row['updated_at']))}</time><span>{topics}</span><a href="/news/wire/{_h(row['event_id'])}/">Open dossier</a></div></header>
  <details{' open' if expanded else ''}><summary><span>Open the complete three-layer view</span><strong>{_h(row['reporting']['evidence_strength'])}</strong></summary>
    <div class="situation-layers">{_reporting_layer(row)}{_social_layer(row)}{_measurement_layer(row)}{_osint_layer(row)}</div>
    <section class="situation-synthesis"><div><p class="situation-kicker">Bounded synthesis</p><h3>What the combined view says</h3><p>{_h(row['synthesis']['summary'])}</p></div><div><h4>Next checks</h4><ol>{checks}</ol></div><details><summary>Known unknowns</summary><ul>{unknowns}</ul></details></section>
    <p class="situation-receipt">{_h(row['situation_id'])} · {_h(row['version_id'])} · {_h(row['analysis_id'])}</p>
  </details>
</article>"""


def _page_path(page: int) -> Path:
    if page < 1:
        raise ChinaSituationBuildError("situation page numbers start at one")
    return PAGE_PATH if page == 1 else PAGE_PATH.parent / "page" / str(page) / "index.html"


def _page_url(page: int) -> str:
    return (
        "/news/china/situation/"
        if page == 1
        else f"/news/china/situation/page/{page}/"
    )


def _pagination(*, page: int, total_pages: int, total_rows: int) -> str:
    if total_pages <= 1:
        return ""
    links: list[str] = []
    if page > 1:
        links.append(f'<a rel="prev" href="{_page_url(page - 1)}">← Newer</a>')
    for number in range(1, total_pages + 1):
        current = ' aria-current="page"' if number == page else ""
        links.append(f'<a{current} href="{_page_url(number)}">{number}</a>')
    if page < total_pages:
        links.append(f'<a rel="next" href="{_page_url(page + 1)}">Older →</a>')
    return (
        '<nav class="situation-pagination" aria-label="Situation archive pages">'
        f'<span>Page {page} of {total_pages} · {total_rows} total situations</span>'
        f'<div>{"".join(links)}</div></nav>'
    )


def render_page(document: Mapping[str, Any], *, page: int = 1) -> str:
    situation_model.validate_china_situation(document)
    coverage = document["coverage"]
    all_rows = document["situations"]
    total_pages = max(1, (len(all_rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        raise ChinaSituationBuildError("situation page exceeds the archive")
    first = (page - 1) * PAGE_SIZE
    page_rows = all_rows[first : first + PAGE_SIZE]
    cards = "".join(
        _situation_card(row, expanded=index == 0)
        for index, row in enumerate(page_rows)
    )
    desk_values = sorted({row["desk"] for row in page_rows})
    desks = "".join(
        f'<button type="button" data-situation-desk="{_h(desk)}">{_h(desk.title())}</button>'
        for desk in desk_values
    )
    pager = _pagination(page=page, total_pages=total_pages, total_rows=len(all_rows))
    range_start = first + 1 if page_rows else 0
    range_end = first + len(page_rows)
    body = f"""<body class="ps newsroom-page situation-page">
{site_nav.render('/news/')}
<main id="main">
  <header class="situation-hero"><div><p class="situation-kicker">Palimpsest / China situation desk{' / archive ' + str(page) if page > 1 else ''}</p><h1>Reports.<br><em>Social context.</em><br>Measurements.</h1></div><div><p class="situation-hero__dek">One evidence-bound view of what publishers report, how reviewed social sources carry it, and what Palimpsest can independently measure. The layers meet here; their limits do not disappear. This desk captures public posts, deletions, archives and GFW injector telemetry. It does not capture private WeChat, classified systems, or in-country accounts.</p><nav><a href="/news/china/erasure/">Find a deleted post</a><a href="/news/china/">Publisher stream</a><a href="/news/">Observatory desk</a><a href="/readings/china-situation-latest.json">Structured situation index</a><a href="/news/china/situation/feed.xml">RSS</a><a href="/news/china/situation/feed.json">JSON Feed</a></nav></div></header>
  <section class="situation-stats" aria-label="Current situation coverage"><span><strong>{coverage['in_scope_events']}</strong> situations</span><span><strong>{coverage['publisher_reports']}</strong> publisher reports</span><span><strong>{coverage['measurement_context_rows']}</strong> measurement links</span><span><strong>{coverage['social_observations_linked']}</strong> exact-link social observations</span><span><strong>{coverage.get('osint_context_rows', 0)}</strong> public OSINT links</span><span><strong>{coverage['reviewed_telegram_signals']}</strong> reviewed Telegram signals</span><span><strong>{_h(_human(document['generated_at']))}</strong> rebuilt</span></section>
  <div class="situation-shell">
    {_status_panel(document)}
    {_telegram_briefing(document)}
    <section class="situation-method"><p class="situation-kicker">How to read the synthesis</p><h2>More context does not automatically mean more proof.</h2><p>{_h(document['relation_policy'])}</p><div><span>Publisher wire → attributed reporting</span><span>Social → circulation and revision context</span><span>Observatory → topic-level measured context</span></div></section>
    {pager}
    <section class="situation-controls" aria-label="Filter situations"><label><span>Search this archive page</span><input id="situation-search" type="search" placeholder="headline, publisher, topic…" autocomplete="off"></label><div><button class="is-active" type="button" data-situation-desk="all">All desks</button>{desks}</div><label><span>Layer coverage</span><select id="situation-posture"><option value="all">All layer combinations</option><option value="three-layer-context">All three layers</option><option value="report-plus-measurement-context">Reports + measurements</option><option value="report-plus-social-context">Reports + social</option><option value="report-only">Reports only</option></select></label><p id="situation-count" role="status" aria-live="polite">Showing {range_start}–{range_end} of {len(all_rows)} situations</p></section>
    <section class="situation-list" aria-label="Current China situations">{cards}<p id="situation-empty" class="situation-empty-results" hidden>No current situation matches those filters.</p></section>
    {pager}
  </div>
</main>
<footer class="nw-footer"><div class="nw-shell">This is a deterministic synthesis of attributed reports and checked-in measurements. It performs no generative summarization and makes no whole-internet coverage claim. <a href="/docs/SOCIAL-OBSERVATION-PIPELINE.md">Method</a> · <a href="/news/standards/">Standards</a> · <a href="/readings/china-situation-latest.json">JSON contract</a>.</div></footer>
<script src="/assets/china-situation.js" defer></script>
{site_nav.FOOT}
</body></html>"""
    return newsroom_builder._head(
        title=f"China situation desk{' · page ' + str(page) if page > 1 else ''} · Palimpsest",
        description=(
            "Publisher reporting, reviewed social observations, and Palimpsest "
            "Observatory measurements combined without blurring evidence boundaries."
        ),
        canonical=f"{SITE}{_page_url(page)}",
        page_type="website",
        modified_at=document["generated_at"],
        feed_base="/news/china/situation",
        extra_styles=("/assets/china-situation.css",),
        json_ld={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "url": f"{SITE}{_page_url(page)}",
            "name": "Palimpsest China situation desk",
            "dateModified": document["generated_at"],
            "numberOfItems": len(page_rows),
            "isAccessibleForFree": True,
        },
    ) + body


def build_json_feed(document: Mapping[str, Any]) -> dict[str, Any]:
    situation_model.validate_china_situation(document)
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Palimpsest China situation desk",
        "home_page_url": f"{SITE}/news/china/situation/",
        "feed_url": f"{SITE}/news/china/situation/feed.json",
        "description": (
            "Evidence-bound combinations of publisher reports, social context, and "
            "Palimpsest Observatory measurements."
        ),
        "items": [
            {
                "id": row["version_id"],
                "url": row["url"],
                "title": f"[Situation synthesis] {row['headline']}",
                "summary": row["synthesis"]["summary"],
                "date_published": row["published_at"],
                "date_modified": row["updated_at"],
                "tags": [row["desk"], row["posture"], *row["topics"]],
                "_palimpsest": {
                    "kind": "china_situation_synthesis",
                    "situation_id": row["situation_id"],
                    "event_id": row["event_id"],
                    "publisher_reports": row["reporting"]["source_count"],
                    "independent_groups": row["reporting"]["independent_groups"],
                    "social_observations": len(row["social_context"]),
                    "measurement_surfaces": len(row["measurement_context"]),
                    "relation_policy": situation_model.RELATION_POLICY,
                },
            }
            for row in document["situations"][:FEED_LIMIT]
        ],
    }


def _rfc2822(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return email.utils.format_datetime(parsed, usegmt=True)


def build_rss(document: Mapping[str, Any]) -> bytes:
    situation_model.validate_china_situation(document)
    items = "".join(
        f"""<item><title>{xml_escape('[Situation synthesis] ' + row['headline'])}</title><link>{xml_escape(row['url'])}</link><guid isPermaLink="false">{xml_escape(row['version_id'])}</guid><pubDate>{_rfc2822(row['updated_at'])}</pubDate><description>{xml_escape(row['synthesis']['summary'])}</description><category>{xml_escape(row['desk'])}</category><category>{xml_escape(row['posture'])}</category></item>"""
        for row in document["situations"][:FEED_LIMIT]
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Palimpsest China situation desk</title>
  <link>{SITE}/news/china/situation/</link>
  <description>Publisher reports, social context, and Observatory measurements with evidence boundaries attached.</description>
  <language>en</language>
  <lastBuildDate>{_rfc2822(document['generated_at'])}</lastBuildDate>
  <atom:link href="{SITE}/news/china/situation/feed.xml" rel="self" type="application/rss+xml" />
  {items}
</channel>
</rss>
""".encode("utf-8")


def _h(value: object) -> str:
    return newsroom_builder._h(value)


def _human(value: str) -> str:
    return newsroom_builder._human_time(value)


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def build_outputs(
    *,
    wire_path: Path = WIRE_PATH,
    newsroom_path: Path = NEWSROOM_PATH,
    social_path: Path = SOCIAL_PATH,
    dragon_whispers_path: Path = DRAGON_WHISPERS_PATH,
) -> tuple[dict[Path, bytes], dict[str, Any]]:
    wire, analyses, social, reviewed_telegram = load_inputs(
        wire_path=wire_path,
        newsroom_path=newsroom_path,
        social_path=social_path,
        dragon_whispers_path=dragon_whispers_path,
    )
    document = situation_model.build_china_situation(
        wire,
        analyses,
        social=social,
        reviewed_telegram=reviewed_telegram,
        osint_observations=load_osint_observations(),
    )
    document = situation_model.bind_situation_page_urls(document, page_size=PAGE_SIZE)
    outputs: dict[Path, bytes] = {
        OUTPUT_PATH: _pretty_json(document),
        JSON_FEED_PATH: _pretty_json(build_json_feed(document)),
        RSS_FEED_PATH: build_rss(document),
    }
    total_pages = max(1, (len(document["situations"]) + PAGE_SIZE - 1) // PAGE_SIZE)
    for page in range(1, total_pages + 1):
        outputs[_page_path(page)] = render_page(document, page=page).encode("utf-8")
    return outputs, document


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _archive_page_paths() -> set[Path]:
    archive_root = PAGE_PATH.parent / "page"
    if not archive_root.is_dir():
        return set()
    return {
        path
        for path in archive_root.glob("*/index.html")
        if path.parent.name.isdigit()
    }


def _remove_stale_archive_pages(expected: set[Path]) -> None:
    for path in sorted(_archive_page_paths() - expected):
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass


def run(*, check: bool = False) -> int:
    outputs, document = build_outputs()
    changed = [
        path for path, payload in outputs.items() if not path.is_file() or path.read_bytes() != payload
    ]
    expected_archive_pages = {
        path for path in outputs if path != PAGE_PATH and PAGE_PATH.parent in path.parents
    }
    stale_archive_pages = _archive_page_paths() - expected_archive_pages
    if check:
        if changed or stale_archive_pages:
            for path in changed:
                print(f"stale: {path.relative_to(ROOT)}")
            for path in sorted(stale_archive_pages):
                print(f"stale archive page: {path.relative_to(ROOT)}")
            return 1
        print(
            f"China situation is current: {document['coverage']['in_scope_events']} situations"
        )
        return 0
    for path, payload in outputs.items():
        _atomic_write(path, payload)
    _remove_stale_archive_pages(expected_archive_pages)
    print(
        "Built China situation: "
        f"{document['coverage']['in_scope_events']} situations, "
        f"{document['coverage']['publisher_reports']} publisher reports, "
        f"{document['coverage']['measurement_context_rows']} measurement links, "
        f"{document['coverage']['social_observations_linked']} linked social observations"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated outputs drift")
    arguments = parser.parse_args(argv)
    return run(check=arguments.check)


if __name__ == "__main__":
    raise SystemExit(main())
