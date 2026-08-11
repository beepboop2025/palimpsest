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
import hashlib
import html
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

from core import economic_pulse as economic_pulse_model
from core import investigations as investigations_model
from core import newsroom
from core import newswire as newswire_model
from scripts import site_nav


ROOT = Path(__file__).resolve().parent.parent
NEWS = ROOT / "news"
READING = ROOT / "readings" / "newsroom-latest.json"
NEWSWIRE_READING = ROOT / "readings" / "newswire-latest.json"
ECONOMIC_READING = ROOT / "readings" / "china-economic-pulse-latest.json"
INVESTIGATIONS_READING = ROOT / "readings" / "investigations-latest.json"
SITE = "https://palimpsest.info"
PUBLISHER = "Palimpsest Observatory"
DESCRIPTION = (
    "Evidence-linked dispatches from Palimpsest's China censorship, network, "
    "erasure, state-telemetry and model measurements."
)
OG_IMAGE = f"{SITE}/brand/palimpsest-og2.png"

EVENT_DESKS = {
    "economy": "Economy",
    "politics": "Politics & law",
    "rights": "Rights",
    "security": "Security",
    "censorship": "Censorship",
    "connectivity": "Connectivity & networks",
    "technology": "Technology",
}

EVIDENCE_LABELS = {
    "measurement-corroborated": "Measurement + independent source groups",
    "primary-corroborated": "Primary record + independent source groups",
    "multi-source": "Multiple independent source groups",
    "single-measurement-source": "Single measurement source",
    "single-primary-source": "Single primary source",
    "single-source": "Single attributed source",
}

HOME_EVENTS_PER_DESK = 5
WIRE_PAGE_SIZE = 60

_SOURCE_LANGUAGES = {
    "bbc-chinese": "zh-Hant",
    "rfa-mandarin": "zh-Hans",
    "voa-chinese": "zh-Hans",
}

_LEAD_STRENGTH_RANK = {
    "measurement-corroborated": 5,
    "primary-corroborated": 4,
    "multi-source": 3,
    "single-measurement-source": 2,
    "single-primary-source": 1,
    "single-source": 0,
}
_DATA_RELEASE_TERMS = (
    "tender results",
    "survey on business",
    "exchange rate index",
    "consumer price",
    "producer price",
    "factory-gate prices",
    "gross domestic product",
    "gdp",
    "inflation",
    "unemployment",
    "retail sales",
    "industrial production",
    "trade balance",
    "money supply",
)


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _contains_han(value: object) -> bool:
    """Return whether text contains a Han ideograph used by the Chinese feeds."""

    return any(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
        for character in str(value)
    )


def _text_language(value: object, *, source_id: str | None = None) -> str:
    """Infer rendered text language without treating a source as the text itself.

    A Chinese-language desk can publish an English translation, so the Han-script
    check is the gate. The source identity then supplies the script variant that
    cannot be inferred reliably from a short headline alone.
    """

    if not _contains_han(value):
        return "en"
    return _SOURCE_LANGUAGES.get(source_id or "", "zh")


def _event_language(event: Mapping[str, Any]) -> str:
    """Infer the headline language, preferring the receipt that supplied it."""

    headline = str(event["headline"])
    refs = event.get("evidence_refs", [])
    matching_ref = next(
        (ref for ref in refs if str(ref.get("title", "")).strip() == headline.strip()),
        refs[0] if refs else None,
    )
    source_id = str(matching_ref["source_id"]) if matching_ref else None
    return _text_language(headline, source_id=source_id)


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


def _load_extension_documents(
    *,
    newswire_path: Path = NEWSWIRE_READING,
    economic_path: Path = ECONOMIC_READING,
    investigations_path: Path = INVESTIGATIONS_READING,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Load optional publication planes through their strict runtime validators.

    The instrument newsroom remains independently buildable for recovery and
    focused tests. Once an extension file exists, however, corruption is fatal:
    silently falling back would make a broken intake look like an empty news day.
    """

    wire = pulse = investigations = None
    if newswire_path.exists():
        wire = newswire_model.strict_json_loads(
            newswire_path.read_bytes(), label=str(newswire_path)
        )
        newswire_model.validate_newswire_document(wire)
    if economic_path.exists():
        pulse = newswire_model.strict_json_loads(
            economic_path.read_bytes(), label=str(economic_path)
        )
        economic_pulse_model.validate_economic_pulse(pulse)
    if investigations_path.exists():
        investigations = newswire_model.strict_json_loads(
            investigations_path.read_bytes(), label=str(investigations_path)
        )
        investigations_model.validate_investigations(
            investigations,
            readings_dir=ROOT / "readings",
        )
    return wire, pulse, investigations


def _revision_id(value: Mapping[str, Any], prefix: str = "revision") -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _site_path(url: str) -> str:
    prefix = SITE + "/"
    if not url.startswith(prefix):
        raise newsroom.NewsroomError(f"public story URL is outside {SITE}: {url!r}")
    return "/" + url.removeprefix(SITE).lstrip("/")


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


def _select_instrument_lead(
    stories: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    lead = next(
        (
            story
            for story in stories
            if story["priority"] == "lead" and story["status"] == "live"
        ),
        None,
    )
    if lead is None:
        lead = next((story for story in stories if story["status"] == "live"), None)
    if lead is None:
        lead = stories[0]
    return lead


def _wire_index_json_ld(
    feed: Mapping[str, Any], wire: Mapping[str, Any]
) -> dict[str, Any]:
    entries = [
        {"url": event["url"], "name": event["headline"]}
        for event in wire["events"]
    ] + [
        {"url": story["url"], "name": story["headline"]}
        for story in feed["stories"]
    ]
    return {
        "@context": "https://schema.org",
        "@graph": [
            _organization(),
            {
                "@type": "CollectionPage",
                "@id": feed["url"],
                "url": feed["url"],
                "name": "Palimpsest Wire",
                "description": wire["scope"],
                "dateModified": max(feed["generated_at"], wire["generated_at"]),
                "publisher": {"@id": f"{SITE}/#organization"},
                "mainEntity": {
                    "@type": "ItemList",
                    "numberOfItems": len(entries),
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": index,
                            "url": item["url"],
                            "name": item["name"],
                        }
                        for index, item in enumerate(entries, 1)
                    ],
                },
            },
        ],
    }


def _wire_items(wire: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {item["item_id"]: item for item in wire["items"]}


def _event_braid(
    event: Mapping[str, Any], wire: Mapping[str, Any], *, compact: bool = False
) -> str:
    items = _wire_items(wire)
    refs = event["evidence_refs"][:3] if compact else event["evidence_refs"]
    rows = []
    for ref in refs:
        item = items[ref["item_id"]]
        digest = item["feed_sha256"][:12]
        title_language = _text_language(ref["title"], source_id=ref["source_id"])
        rows.append(f"""<li class="nw-braid__node" data-role="{_h(ref['role'])}">
  <p class="nw-braid__role">{_h(ref['role'])} · {_h(ref['independence_group'])}</p>
  <p class="nw-braid__source"><a href="{_h(ref['url'])}">{_h(ref['source_name'])}</a></p>
  <p class="nw-braid__title" lang="{_h(title_language)}">{_h(ref['title'])}</p>
  <p class="nw-braid__time"><time datetime="{_h(ref['published_at'])}">{_h(_human_time(ref['published_at']))}</time> · feed sha {_h(digest)}</p>
</li>""")
    scan_ids = event["declared_links"]["scan_signal_ids"]
    economic_ids = event["declared_links"]["economic_signal_ids"]
    if scan_ids or economic_ids:
        linked = [f"scan:{value}" for value in scan_ids] + [
            f"economic:{value}" for value in economic_ids
        ]
        rows.append(f"""<li class="nw-braid__node nw-braid__node--link" data-role="topic-link">
  <p class="nw-braid__role">Declared topic surfaces · not a causal match</p>
  <p class="nw-braid__title">{_h(' · '.join(linked))}</p>
  <p class="nw-braid__time">A timed measurement join has not been asserted by this dossier.</p>
</li>""")
    return '<ol class="nw-braid" aria-label="Evidence braid">' + "".join(rows) + "</ol>"


def _event_lead(event: Mapping[str, Any], wire: Mapping[str, Any]) -> str:
    groups = len(event["evidence_groups"])
    coverage = wire["coverage"]
    coverage_class = "" if coverage["status"] == "healthy" else " nw-receipt__state--warning"
    language = _event_language(event)
    dek_language = _text_language(
        event["dek"], source_id=event["evidence_refs"][0]["source_id"]
    )
    return f"""<section class="nw-wire-lead" id="lead-dossier" aria-labelledby="lead-headline">
  <div class="nw-wire-lead__copy">
    <p class="nw-kicker">{_h(EVENT_DESKS[event['desk']])} · {_h(EVIDENCE_LABELS[event['evidence_strength']])}</p>
    <h1 id="lead-headline" lang="{_h(language)}">{_h(event['headline'])}</h1>
    <p class="nw-lead__dek" lang="{_h(dek_language)}">{_h(event['dek'])}</p>
    <p class="nw-lead__qualifier"><strong>Evidence boundary:</strong> {_h(event['lead_reason'])} {_h(event['limitations'][1])}</p>
    <div class="nw-actions">
      <a class="nw-actions__primary" href="{_h(_site_path(event['url']))}">Open the evidence dossier</a>
      <a href="/readings/newswire-latest.json">Structured wire</a>
      <a href="/news/economy/">Economic state</a>
    </div>
  </div>
  <aside class="nw-wire-lead__rail">
    <div class="nw-receipt" aria-label="Dossier receipt">
      <p class="nw-receipt__label">Dossier receipt</p>
      <dl>
        <dt>Strength</dt><dd>{_h(EVIDENCE_LABELS[event['evidence_strength']])}</dd>
        <dt>Groups</dt><dd>{groups} independent evidence group{'s' if groups != 1 else ''}</dd>
        <dt>Published</dt><dd>{_h(_human_time(event['published_at']))}</dd>
        <dt>Version</dt><dd><code>{_h(event['version_id'])}</code></dd>
        <dt>Intake</dt><dd><span class="nw-receipt__state{coverage_class}"><span class="nw-dot" aria-hidden="true"></span>{_h(coverage['status'])}</span></dd>
      </dl>
    </div>
  </aside>
  <div class="nw-wire-lead__braid">{_event_braid(event, wire, compact=True)}</div>
</section>"""


def _event_card(event: Mapping[str, Any]) -> str:
    group_count = len(event["evidence_groups"])
    state = "lead" if event["lead"] else "attributed"
    language = _event_language(event)
    dek_language = _text_language(
        event["dek"], source_id=event["evidence_refs"][0]["source_id"]
    )
    return f"""<article class="nw-event-card" data-strength="{_h(event['evidence_strength'])}" data-lead="{_h(str(event['lead']).lower())}">
  <p class="nw-card__kicker">{_h(EVIDENCE_LABELS[event['evidence_strength']])}</p>
  <h3 lang="{_h(language)}"><a class="nw-card__link" href="{_h(_site_path(event['url']))}">{_h(event['headline'])}</a></h3>
  <p class="nw-card__dek" lang="{_h(dek_language)}">{_h(event['dek'])}</p>
  <div class="nw-event-card__facts">
    <span>{len(event['evidence_refs'])} source receipt{'s' if len(event['evidence_refs']) != 1 else ''}</span>
    <span>{group_count} independent group{'s' if group_count != 1 else ''}</span>
    <span>{_h(state)}</span>
  </div>
  <p class="nw-card__meta"><time datetime="{_h(event['updated_at'])}">{_h(_human_time(event['updated_at']))}</time><span class="nw-card__hash">{_h(event['version_id'])}</span></p>
</article>"""


def _select_event_lead(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Choose a deterministic evidence-first lead from eligible current events.

    ``lead`` is the intake eligibility gate. This second ordering favours source
    structure, then an explicit release title, then recency; it does not invent a
    subjective truth or importance score.
    """

    eligible = [event for event in events if event["lead"]] or list(events)

    def key(event: Mapping[str, Any]) -> tuple[int, int, int, str, str]:
        headline = event["headline"].casefold()
        explicit_release = int(any(term in headline for term in _DATA_RELEASE_TERMS))
        return (
            _LEAD_STRENGTH_RANK[event["evidence_strength"]],
            explicit_release,
            len(event["evidence_groups"]),
            event["updated_at"],
            event["event_id"],
        )

    return max(eligible, key=key)


def _select_lead(entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Dispatch lead selection without conflating two publication contracts."""

    if entries and "event_id" in entries[0]:
        return _select_event_lead(entries)
    return _select_instrument_lead(entries)


def _event_sections(
    wire: Mapping[str, Any], *, lead_event_id: str
) -> tuple[str, str]:
    navigation = []
    blocks = []
    for order, (desk_id, title) in enumerate(EVENT_DESKS.items(), 1):
        events = [
            event for event in wire["events"]
            if event["desk"] == desk_id and event["event_id"] != lead_event_id
        ]
        if not events:
            continue
        navigation.append(f'<li><a href="#wire-{_h(desk_id)}">{_h(title)}</a></li>')
        visible_events = events[:HOME_EVENTS_PER_DESK]
        cards = "".join(_event_card(event) for event in visible_events)
        archive_link = ""
        if len(events) > len(visible_events):
            archive_link = (
                '<p class="nw-section__more"><a href="/news/wire/">'
                f'View all {len(events)} { _h(title).lower() } dossiers →</a></p>'
            )
        blocks.append(f"""<section class="nw-section nw-section--events" id="wire-{_h(desk_id)}">
  <div class="nw-section__head">
    <div><p class="nw-section__label">{order:02d} / Evidence desk</p><h2>{_h(title)}</h2></div>
    <p class="nw-section__dek">Every accepted item is accounted for. Single-source items remain attributed; corroboration counts independent evidence groups, not mirrors.</p>
  </div>
  <div class="nw-event-grid">{cards}</div>{archive_link}
</section>""")
    return "".join(navigation), "".join(blocks)


def _economic_panel(pulse: Mapping[str, Any] | None) -> str:
    if pulse is None:
        return """<section class="nw-econ" id="economy"><div><p class="nw-kicker nw-kicker--warning">Economic state unavailable</p><h2>No validated economic pulse was published</h2></div><p>The instrument newsroom remains available, but no state-of-economy synthesis is shown without its structured evidence contract.</p></section>"""
    gates = "".join(
        f"""<li data-passed="{_h(str(gate['passed']).lower())}"><span>{_h(gate['label'])}</span><strong>{gate['observed']} / {gate['minimum']}</strong></li>"""
        for gate in pulse["readiness"]["gates"]
    )
    desks = "".join(
        f"""<div class="nw-econ__desk"><span>{_h(desk['title'])}</span><strong>{desk['n_metrics']}</strong><small>{len(desk['independent_group_ids'])} groups · {_h(desk['status'])}</small></div>"""
        for desk in pulse["desks"]
    )
    coverage = pulse["coverage"]
    return f"""<section class="nw-econ" id="economy" aria-labelledby="economy-title">
  <div class="nw-econ__statement">
    <p class="nw-kicker nw-kicker--economic">China economic state · {_h(pulse['economic_state']['status'])}</p>
    <h2 id="economy-title">The evidence is broadening. The composite still abstains.</h2>
    <p>{_h(pulse['economic_state']['claim'])}</p>
    <a class="nw-text-link" href="/news/economy/">Open all metrics, releases and revision receipts →</a>
  </div>
  <div class="nw-econ__readiness">
    <p class="nw-receipt__label">Composite readiness gates</p>
    <ul>{gates}</ul>
    <p>{len(coverage['observed_independent_group_ids'])} observed independent groups · {coverage['registered_sources']} registered sources · {_h(pulse['readiness']['abstention_reason'])}</p>
  </div>
  <div class="nw-econ__desks">{desks}</div>
</section>"""


def _case_public_url(case: Mapping[str, Any]) -> str:
    """Return the absolute form of a validator-owned investigation route."""

    path = str(case["url"])
    if not path.startswith("/news/investigations/"):
        raise newsroom.NewsroomError(f"invalid investigation URL: {path!r}")
    return SITE + path


def _investigation_href(value: object) -> str:
    """Allow only explicit web URLs and root-relative public artifacts."""

    href = str(value)
    if href.startswith("https://"):
        return href
    if href.startswith("/") and not href.startswith("//"):
        return href
    return "#"


def _case_publication_state(case: Mapping[str, Any]) -> str:
    status = case["status"]
    if status == "published":
        return "published"
    if status == "abstained":
        return "abstained"
    return "open"


def _case_status_label(case: Mapping[str, Any]) -> tuple[str, str]:
    """Keep an open automated lead visually distinct from reviewed reporting."""

    status = case["status"]
    if status == "published":
        if case["correction"]["status"] == "corrected":
            return "Investigation", "CORRECTED"
        if case["published_at"] != case["updated_at"]:
            return "Investigation", "UPDATED"
        return "Investigation", "PUBLISHED"
    return {
        "evidence_gathering": ("Research lead", "OPEN INVESTIGATION"),
        "review_ready": ("Research lead", "REVIEW READY"),
        "abstained": ("Research lead", "ABSTAINED"),
    }[status]


def _case_language(case: Mapping[str, Any]) -> str:
    return _text_language(case["title"])


def _investigation_citations(case: Mapping[str, Any]) -> list[str]:
    citations = []
    for evidence in case["evidence"]:
        for candidate in (evidence["source_url"], evidence["artifact_url"]):
            href = _investigation_href(candidate)
            if href != "#" and href not in citations:
                citations.append(href)
    return citations


def _investigations_index_json_ld(
    investigations: Mapping[str, Any],
) -> dict[str, Any]:
    url = f"{SITE}/news/investigations/"
    return {
        "@context": "https://schema.org",
        "@graph": [
            _organization(),
            {
                "@type": "CollectionPage",
                "@id": url,
                "url": url,
                "name": "Palimpsest Investigations",
                "description": investigations["scope"],
                "dateModified": investigations["generated_at"],
                "publisher": {"@id": f"{SITE}/#organization"},
                "mainEntity": {
                    "@type": "ItemList",
                    "numberOfItems": investigations["n_cases"],
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": position,
                            "url": _case_public_url(case),
                            "name": case["title"],
                        }
                        for position, case in enumerate(investigations["cases"], 1)
                    ],
                },
            },
        ],
    }


def _investigation_case_json_ld(case: Mapping[str, Any]) -> dict[str, Any]:
    public_url = _case_public_url(case)
    common = {
        "@id": public_url,
        "url": public_url,
        "name": case["title"],
        "description": case["dek"],
        "dateModified": case["updated_at"],
        "inLanguage": _case_language(case),
        "isAccessibleForFree": True,
        "publisher": _organization(),
        "about": case["testable_question"],
        "citation": _investigation_citations(case),
    }
    if case["status"] == "published":
        return {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            **common,
            "headline": case["title"],
            "datePublished": case["published_at"],
            "articleSection": "Investigations",
            "mainEntityOfPage": {"@type": "WebPage", "@id": public_url},
            "author": _organization(),
            "image": [OG_IMAGE],
        }
    return {
        "@context": "https://schema.org",
        "@type": "Report",
        **common,
        "creativeWorkStatus": f"Research lead — {case['status'].replace('_', ' ')}",
    }


def _investigation_card(case: Mapping[str, Any]) -> str:
    kind, status = _case_status_label(case)
    state = _case_publication_state(case)
    language = _case_language(case)
    question_language = _text_language(case["testable_question"])
    n_groups = len({evidence["independence_group"] for evidence in case["evidence"]})
    return f"""<article class="nw-investigation-card" data-publication-state="{_h(state)}">
  <p class="nw-investigation-card__status"><span class="nw-dot" aria-hidden="true"></span>{_h(kind)} · {_h(status)}</p>
  <h3 lang="{_h(language)}"><a href="{_h(case['url'])}">{_h(case['title'])}</a></h3>
  <p class="nw-investigation-card__question" lang="{_h(question_language)}"><strong>Question under test:</strong> {_h(case['testable_question'])}</p>
  <p>{_h(case['status_reason'])}</p>
  <p class="nw-investigation-card__meta">{len(case['claims'])} claim record{'s' if len(case['claims']) != 1 else ''} · {len(case['evidence'])} evidence receipt{'s' if len(case['evidence']) != 1 else ''} · {n_groups} upstream group{'s' if n_groups != 1 else ''}<br>Updated <time datetime="{_h(case['updated_at'])}">{_h(_human_time(case['updated_at']))}</time></p>
</article>"""


def _investigation_register(
    *,
    title: str,
    label: str,
    description: str,
    cases: Sequence[Mapping[str, Any]],
    section_id: str,
) -> str:
    cards = "".join(_investigation_card(case) for case in cases)
    if not cards:
        cards = (
            '<div class="nw-empty-register"><strong>No case currently carries '
            f"this status.</strong><p>{_h(description)}</p></div>"
        )
    return f"""<section class="nw-investigation-register" aria-labelledby="{_h(section_id)}">
  <header><div><p class="nw-section__label">{_h(label)}</p><h2 id="{_h(section_id)}">{_h(title)}</h2></div><p>{_h(description)}</p></header>
  <div class="nw-investigation-grid">{cards}</div>
</section>"""


def _investigations_feature(
    investigations: Mapping[str, Any] | None,
) -> str:
    if investigations is None:
        return ""
    cases = investigations["cases"]
    published = [case for case in cases if case["status"] == "published"]
    open_cases = [
        case for case in cases
        if case["status"] in {"evidence_gathering", "review_ready"}
    ]
    abstained = [case for case in cases if case["status"] == "abstained"]
    featured = next(iter(published), cases[0] if cases else None)
    featured_case = ""
    if featured is not None:
        kind, status = _case_status_label(featured)
        featured_case = f"""<p class="nw-case-status" data-publication-state="{_h(_case_publication_state(featured))}">{_h(kind)} · {_h(status)}</p>
    <h3 lang="{_h(_case_language(featured))}">{_h(featured['title'])}</h3>
    <p><strong>Question under test:</strong> {_h(featured['testable_question'])}</p>"""
    return f"""<section class="nw-investigations-feature" id="investigations" aria-labelledby="investigations-feature-title" data-file-code="INV / {investigations['n_cases']:03d}">
  <div class="nw-investigations-feature__rail"><p class="nw-section__label">Investigations desk</p><strong>{investigations['n_cases']}</strong><span>{len(published)} published · {len(open_cases)} open · {len(abstained)} abstained</span></div>
  <div><h2 id="investigations-feature-title">The evidence threshold is part of the story</h2>
    <p>An investigation is a reviewed evidence synthesis, not a truth score. Open automated work remains a research lead and cannot borrow the authority of a published investigation.</p>
    {featured_case}
    <div class="nw-actions"><a class="nw-actions__primary" href="/news/investigations/">Open the investigations register</a><a href="/readings/investigations-latest.json">Structured desk</a><a href="/docs/INVESTIGATIONS.md">Publication method</a></div>
  </div>
</section>"""


def _accountability_tape(wire: Mapping[str, Any]) -> str:
    coverage = wire["coverage"]
    counts = coverage["counts"]
    source_rows = "".join(
        f"""<li data-status="{_h(source['status'])}"><strong>{_h(source['source_name'])}</strong><span>{_h(source['status'])}</span><small>{source['accepted_items']} accepted · {source['rejected_items']} rejected</small></li>"""
        for source in coverage["sources"]
    )
    return f"""<aside class="nw-tape" aria-labelledby="tape-title">
  <div class="nw-tape__head"><div><p class="nw-kicker">Accountability tape</p><h2 id="tape-title">Every feed answered for</h2></div>
  <p>{coverage['accepted_items']} accepted items · {coverage['rejected_items']} rejected or out-of-window · {counts['fetch_error']} fetch failures · {counts['parse_error']} malformed feeds · {counts['stale']} stale feeds.</p></div>
  <ul>{source_rows}</ul>
</aside>"""


def _instrument_sections(feed: Mapping[str, Any]) -> str:
    blocks = []
    for section in feed["sections"]:
        stories = [story for story in feed["stories"] if story["section"] == section["id"]]
        cards = "".join(_story_card(story, section["title"]) for story in stories)
        blocks.append(f"""<section class="nw-section nw-section--instruments" id="instrument-{_h(section['id'])}">
  <div class="nw-section__head">
    <div><p class="nw-section__label">Instrument desk</p><h2>{_h(section['title'])}</h2></div>
    <p class="nw-section__dek">{_h(section['dek'])}</p>
  </div>
  <div class="nw-grid">{cards}</div>
</section>""")
    return "".join(blocks)


def render_evidence_index(
    feed: Mapping[str, Any],
    wire: Mapping[str, Any],
    pulse: Mapping[str, Any] | None,
    investigations: Mapping[str, Any] | None = None,
) -> str:
    events = wire["events"]
    if not events:
        return render_index(feed)
    lead = _select_lead(events)
    event_navigation, event_blocks = _event_sections(wire, lead_event_id=lead["event_id"])
    coverage = wire["coverage"]
    instrument_coverage = feed["coverage"]
    investigations_nav = (
        '<li><a href="#investigations">Investigations</a></li>'
        if investigations is not None else ""
    )
    investigations_count = (
        f" · {investigations['n_cases']} investigation case files"
        if investigations is not None else ""
    )
    body = f"""<body class="ps newsroom-page newsroom-page--evidence-wire">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-masthead">
    <div class="nw-masthead__top">
      <p class="nw-wordmark">Palimpsest <span>Wire</span></p>
      <p class="nw-edition"><strong>Evidence edition</strong>{_h(_human_time(wire['generated_at']))}<br>{wire['n_events']} event dossiers · {feed['n_stories']} instruments{investigations_count}</p>
    </div>
    <p class="nw-masthead__dek">China intelligence that keeps reported facts, measured facts, corroboration, revisions and unknowns structurally separate.</p>
  </header>
  <div class="nw-meta-line"><span>China · economy · politics · censorship · networks</span><span>Window {_h(_human_time(wire['window']['from']))} → {_h(_human_time(wire['window']['to']))}</span><a href="/news/feed.xml">RSS</a><a href="/news/feed.json">JSON Feed</a><a href="/readings/newswire-latest.json">Structured wire</a></div>
  <div class="nw-status-strip" role="status" aria-label="Edition coverage">
    <span><i class="nw-dot nw-dot--live" aria-hidden="true"></i><strong>{wire['n_events']}</strong> dossiers</span>
    <span><i class="nw-dot nw-dot--warning" aria-hidden="true"></i><strong>{coverage['successful_sources']}/{coverage['registry_sources']}</strong> feeds answered</span>
    <span><i class="nw-dot nw-dot--missing" aria-hidden="true"></i><strong>{coverage['rejected_items']}</strong> rejected / out-of-window</span>
    <span><strong>{instrument_coverage['live']}/{instrument_coverage['total']}</strong> live instruments</span>
  </div>
  <nav aria-label="News desks"><ul class="nw-section-nav"><li><a href="#lead-dossier">Lead dossier</a></li><li><a href="#economy">Economic state</a></li>{investigations_nav}{event_navigation}<li><a href="#instruments">Instruments</a></li><li><a href="#tape-title">Coverage tape</a></li></ul></nav>
  {_event_lead(lead, wire)}
  {_economic_panel(pulse)}
  {_investigations_feature(investigations)}
  {event_blocks}
  <div id="instruments" class="nw-instrument-heading"><p class="nw-kicker">Measurement layer</p><h2>Current Palimpsest instruments</h2><p>These are mutable latest-state briefs. Event dossiers above preserve the news and revision timeline.</p></div>
  {_instrument_sections(feed)}
  {_accountability_tape(wire)}
</main>
<footer class="nw-footer"><div class="nw-shell">Palimpsest Wire publishes metadata-only event dossiers from a closed source registry and presents declared measurement surfaces as topical pointers, never causal joins. <a href="/news/investigations/">Investigations register</a> · <a href="/docs/EVIDENCE-WIRE.md">Method and architecture</a> · <a href="/readings/newsroom-latest.json">Instrument feed</a> · <a href="https://github.com/beepboop2025/palimpsest">Source code</a>.</div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title="Palimpsest Wire · hard-facts China intelligence",
        description="Evidence dossiers across China's economy, politics, censorship, networks and technology, with source independence, measurements, revisions and unknowns visible.",
        canonical=feed["url"],
        page_type="website",
        modified_at=max(feed["generated_at"], wire["generated_at"]),
        json_ld=_wire_index_json_ld(feed, wire),
    ) + "\n" + body


def render_investigations_index(investigations: Mapping[str, Any]) -> str:
    cases = investigations["cases"]
    published = [case for case in cases if case["status"] == "published"]
    open_cases = [
        case for case in cases
        if case["status"] in {"evidence_gathering", "review_ready"}
    ]
    abstained = [case for case in cases if case["status"] == "abstained"]
    body = f"""<body class="ps newsroom-page newsroom-page--investigations">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-investigations-head">
    <p class="nw-section__label">Public case register</p>
    <h1>Investigations and research leads</h1>
    <p class="nw-investigations-head__dek">Reviewed reporting, open evidence gathering and editorial abstention remain separate public states. Every case shows the question, receipts, counterevidence, falsifiers and unresolved collection targets.</p>
  </header>
  <div class="nw-meta-line"><span>Aggregate public evidence · no person-level records</span><span>Updated <time datetime="{_h(investigations['generated_at'])}">{_h(_human_time(investigations['generated_at']))}</time></span><a href="/readings/investigations-latest.json">Structured desk</a><a href="/docs/INVESTIGATIONS.md">Publication method</a></div>
  <div class="nw-status-strip" role="status" aria-label="Investigation publication states">
    <span><i class="nw-dot nw-dot--live" aria-hidden="true"></i><strong>{len(published)}</strong> published</span>
    <span><i class="nw-dot nw-dot--warning" aria-hidden="true"></i><strong>{len(open_cases)}</strong> open research leads</span>
    <span><i class="nw-dot nw-dot--missing" aria-hidden="true"></i><strong>{len(abstained)}</strong> abstained</span>
    <span><strong>{investigations['n_cases']}</strong> total case files</span>
  </div>
  <nav aria-label="Investigation registers"><ul class="nw-section-nav"><li><a href="#published-investigations">Published</a></li><li><a href="#open-research">Open research</a></li><li><a href="#editorial-abstentions">Abstentions</a></li></ul></nav>
  <p class="nw-investigation-notice"><strong>Publication boundary.</strong> An investigation is a reviewed evidence synthesis, not a truth score. Each finding shows supporting evidence, disconfirming evidence, a falsification test and limits. Automated work is labelled <strong>RESEARCH LEAD</strong>, never presented as a completed investigation.</p>
  {_investigation_register(title='Published investigations', label='Reviewed publication', description='Only cases that passed the structured publication gate and editorial review appear here.', cases=published, section_id='published-investigations')}
  {_investigation_register(title='Open research leads', label='Evidence gathering', description='Questions and draft claims remain under test. These cases are not published findings.', cases=open_cases, section_id='open-research')}
  {_investigation_register(title='Editorial abstentions', label='Threshold not met', description='The desk records why available evidence cannot support publication and what would be needed to revisit the question.', cases=abstained, section_id='editorial-abstentions')}
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Palimpsest Wire</a> · <a href="/readings/investigations-latest.json">Structured investigations desk</a> · <a href="/docs/INVESTIGATIONS.md">Method and safety boundary</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title="Palimpsest Investigations · public evidence case files",
        description=(
            "Reviewed investigations and open research leads with claims, "
            "counterevidence, falsification tests, limitations and revision receipts."
        ),
        canonical=f"{SITE}/news/investigations/",
        page_type="website",
        modified_at=investigations["generated_at"],
        json_ld=_investigations_index_json_ld(investigations),
    ) + "\n" + body


def _investigation_value(evidence: Mapping[str, Any]) -> str:
    if evidence["value_type"] == "null":
        return "No scalar value asserted"
    value = evidence["value"]
    if evidence["value_type"] == "boolean":
        return "true" if value else "false"
    if evidence["value_type"] in {"integer", "number"}:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def _investigation_evidence_table(case: Mapping[str, Any]) -> str:
    rows = []
    for evidence in case["evidence"]:
        source_link = (
            f'<a href="{_h(_investigation_href(evidence["source_url"]))}">Source record</a>'
            if evidence["source_url"]
            else "No source URL recorded"
        )
        rows.append(f"""<tr>
  <td><span class="nw-evidence-relation" data-relation="{_h(evidence['role'])}">{_h(evidence['role'])}</span><small>{_h(evidence['source_class'])}</small></td>
  <td><strong lang="{_h(_text_language(evidence['label']))}">{_h(evidence['label'])}</strong><small><code>{_h(evidence['evidence_id'])}</code> · {_h(evidence['independence_group'])}</small></td>
  <td><strong>{_h(_investigation_value(evidence))}</strong><small>Selector <code>{_h(evidence['selector'])}</code></small></td>
  <td>{_h(evidence['interpretation_limit'])}</td>
  <td>{source_link}<small><a href="{_h(_investigation_href(evidence['artifact_url']))}">Artifact</a> · <time datetime="{_h(evidence['source_timestamp'])}">{_h(_human_time(evidence['source_timestamp']))}</time> · sha {_h(evidence['artifact_sha256'][:12])} · {_h(evidence['freshness'])}</small></td>
</tr>""")
    if not rows:
        return '<div class="nw-empty-register"><strong>No evidence receipt is recorded.</strong></div>'
    return f"""<p class="nw-table-cue" id="investigation-evidence-cue">Scroll horizontally to inspect every evidence field.</p>
<div class="nw-table-wrap" role="region" tabindex="0" aria-labelledby="case-evidence-title" aria-describedby="investigation-evidence-cue"><table class="nw-evidence-table"><caption>Evidence receipts for this case file</caption><thead><tr><th scope="col">Relation / class</th><th scope="col">Receipt / upstream group</th><th scope="col">Recorded value</th><th scope="col">Interpretation limit</th><th scope="col">Provenance / integrity</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""


def _investigation_claims(case: Mapping[str, Any]) -> str:
    evidence_by_id = {
        evidence["evidence_id"]: evidence for evidence in case["evidence"]
    }
    counter_by_id = {
        item["counterevidence_id"]: item for item in case["counterevidence"]
    }
    limitation_by_id = {
        item["limitation_id"]: item for item in case["limitations"]
    }
    rows = []
    for claim in case["claims"]:
        linked_evidence = "".join(
            f"<li><code>{_h(evidence_id)}</code> · {_h(evidence_by_id[evidence_id]['label'])} · {_h(evidence_by_id[evidence_id]['role'])}</li>"
            for evidence_id in claim["evidence_ids"]
        ) or "<li>No evidence receipt is linked.</li>"
        linked_counter = "".join(
            f"<li><code>{_h(counter_id)}</code> · {_h(counter_by_id[counter_id]['statement'])} · {_h(counter_by_id[counter_id]['disposition'])}</li>"
            for counter_id in claim["counterevidence_ids"]
        ) or "<li>No counterevidence record is linked.</li>"
        linked_limits = "".join(
            f"<li><strong>{_h(limitation_by_id[limit_id]['statement'])}</strong> {_h(limitation_by_id[limit_id]['consequence'])}</li>"
            for limit_id in claim["limitation_ids"]
        ) or "<li>No claim-specific limitation is linked.</li>"
        noun = "Finding" if case["status"] == "published" else "Claim under test"
        rows.append(f"""<li class="nw-finding" data-confidence="{_h(claim['confidence'])}">
  <p class="nw-finding__label">{_h(noun)} · {_h(claim['type'].replace('_', ' '))} · {_h(claim['confidence'])} · {_h(claim['publication_state'])}</p>
  <h3 lang="{_h(_text_language(claim['statement']))}">{_h(claim['statement'])}</h3>
  <div class="nw-case-columns"><div class="nw-case-panel"><h4>Linked evidence receipts</h4><ul>{linked_evidence}</ul></div><div class="nw-case-panel nw-case-panel--counter"><h4>Linked counterevidence</h4><ul>{linked_counter}</ul></div></div>
  <div class="nw-finding__boundary"><strong>Claim limits.</strong><ul>{linked_limits}</ul></div>
</li>""")
    return "".join(rows) or (
        '<li class="nw-empty-register"><strong>No claim has been recorded. '
        "The case therefore makes no finding.</strong></li>"
    )


def _hypotheses_panel(case: Mapping[str, Any]) -> str:
    rows = "".join(
        f"""<li><strong lang="{_h(_text_language(item['statement']))}">{_h(item['statement'])}</strong><span>{_h(item['status'])} · <code>{_h(item['hypothesis_id'])}</code></span><p><strong>Linked falsification tests:</strong> {_h(', '.join(item['falsification_condition_ids']) or 'none linked')}</p></li>"""
        for item in case["hypotheses"]
    ) or "<li>No hypothesis is recorded. The case therefore cannot advance beyond evidence gathering.</li>"
    return f"""<div class="nw-case-panel"><h3>Hypotheses under test</h3><ul class="nw-case-record-list">{rows}</ul></div>"""


def _counterevidence_panel(case: Mapping[str, Any]) -> str:
    rows = "".join(
        f"""<li><strong lang="{_h(_text_language(item['statement']))}">{_h(item['statement'])}</strong><span>{_h(item['review_status'])} · {_h(item['disposition'])} · evidence {_h(', '.join(item['evidence_ids']) or 'none linked')}</span></li>"""
        for item in case["counterevidence"]
    ) or "<li>No counterevidence record is currently available.</li>"
    return f"""<div class="nw-case-panel nw-case-panel--counter"><h3>Counterevidence and competing records</h3><ul class="nw-case-record-list">{rows}</ul></div>"""


def _falsification_panel(case: Mapping[str, Any]) -> str:
    rows = "".join(
        f"""<li><strong lang="{_h(_text_language(item['statement']))}">{_h(item['statement'])}</strong><span>Status: {_h(item['status'])}</span><p><strong>Evidence needed:</strong> {_h(item['evidence_needed'])}</p></li>"""
        for item in case["falsification_conditions"]
    ) or "<li>No falsification condition is recorded; the publication gate must remain blocked.</li>"
    return f"""<div class="nw-case-panel nw-case-panel--target"><h3>Falsification tests</h3><ul class="nw-case-record-list">{rows}</ul></div>"""


def _publication_gate(case: Mapping[str, Any]) -> str:
    gate = case["publication_gate"]
    rows = "".join(
        f"""<tr><td><strong>{_h(check['label'])}</strong><small><code>{_h(check['check_id'])}</code></small></td><td>{_h(check['minimum'])}</td><td>{_h(check['observed'])}</td><td>{'Passed' if check['passed'] else 'Not passed'}</td><td>{_h(check['detail'])}</td></tr>"""
        for check in gate["checks"]
    )
    return f"""<p class="nw-table-cue" id="publication-gate-cue">Scroll horizontally to inspect every publication check.</p>
<div class="nw-table-wrap" role="region" tabindex="0" aria-labelledby="publication-gate-title" aria-describedby="publication-gate-cue"><table class="nw-evidence-table"><caption>Structured publication-gate checks</caption><thead><tr><th scope="col">Check</th><th scope="col">Minimum</th><th scope="col">Observed</th><th scope="col">Result</th><th scope="col">Detail</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="nw-method-note"><strong>Gate {_h(gate['status'])}.</strong> Publishable: {_h(str(gate['publishable']).lower())}. Failed checks: {_h(', '.join(gate['failed_check_ids']) or 'none')}.</p>"""


def _collection_targets(case: Mapping[str, Any]) -> str:
    rows = []
    for target in case["collection_targets"]:
        evidence_link = (
            f'<a href="{_h(_investigation_href(target["evidence_url"]))}">Collected evidence</a>'
            if target["evidence_url"]
            else "No evidence URL recorded"
        )
        blocker = target["blocker"] or "No blocker recorded"
        rows.append(f"""<div class="nw-case-panel nw-case-panel--target"><p class="nw-finding__label">{_h(target['status'])} · {_h(target['data_level'])}</p><h3>{_h(target['source_id'])}</h3><p>{_h(target['question_answered'])}</p><p><strong>Blocker:</strong> {_h(blocker)}</p><p>{evidence_link}</p></div>""")
    return "".join(rows) or (
        '<div class="nw-empty-register"><strong>No collection target is recorded.</strong></div>'
    )


def _methodology_steps(case: Mapping[str, Any]) -> str:
    return "".join(
        f"""<li><strong>{_h(step['step_id'])}</strong><span>{_h(step['description'])}</span><small>Reproducible: {_h(str(step['reproducible']).lower())}</small></li>"""
        for step in case["methodology"]
    ) or "<li>No methodology step is recorded.</li>"


def _safety_lists(case: Mapping[str, Any]) -> str:
    safety = case["safety"]
    prohibited = "".join(
        f"<li>{_h(value)}</li>" for value in safety["prohibited_interpretations"]
    ) or "<li>No prohibited interpretation is recorded.</li>"
    allegations = "".join(
        f"<li>{_h(value)}</li>" for value in safety["allegations"]
    ) or "<li>No allegation is made.</li>"
    motives = "".join(
        f"<li>{_h(value)}</li>" for value in safety["inferred_motives"]
    ) or "<li>No motive is inferred.</li>"
    return f"""<div class="nw-case-columns"><div class="nw-case-panel nw-case-panel--safety"><h3>Prohibited interpretations</h3><ul>{prohibited}</ul></div><div class="nw-case-panel"><h3>Allegations and motives</h3><ul>{allegations}{motives}</ul></div></div>"""


def render_investigation_case(case: Mapping[str, Any]) -> str:
    kind, status = _case_status_label(case)
    state = _case_publication_state(case)
    published = case["published_at"]
    published_display = _human_time(published) if published else "Not published"
    correction = case["correction"]
    reply = case["right_to_reply"]
    safety = case["safety"]
    correction_time = (
        _human_time(correction["last_corrected_at"])
        if correction["last_corrected_at"] else "No correction timestamp"
    )
    reply_parties = "".join(
        f"<li><strong>{_h(party['display_name'])}</strong> · {_h(party['party_type'])} · {_h(party['disposition'])}</li>"
        for party in reply["parties"]
    ) or "<li>No institution is recorded for reply.</li>"
    limits = "".join(
        f"<li><strong>{_h(item['statement'])}</strong><span>{_h(item['consequence'])}</span></li>"
        for item in case["limitations"]
    ) or "<li>No case-level limitation is recorded.</li>"
    open_notice = ""
    if case["status"] != "published":
        open_notice = (
            '<p class="nw-investigation-notice"><strong>RESEARCH LEAD · NOT A '
            "PUBLISHED INVESTIGATION.</strong> Draft claims remain under test and "
            "must not be read as findings.</p>"
        )
    body = f"""<body class="ps newsroom-page newsroom-page--investigation-case">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <article class="nw-case-file" data-publication-state="{_h(state)}">
    <header class="nw-case-file__header">
      <p class="nw-case-status" data-publication-state="{_h(state)}"><span class="nw-dot" aria-hidden="true"></span>{_h(kind)} · {_h(status)}</p>
      <h1 lang="{_h(_case_language(case))}">{_h(case['title'])}</h1>
      <p class="nw-case-file__question" lang="{_h(_text_language(case['testable_question']))}"><strong>Testable question:</strong> {_h(case['testable_question'])}</p>
      <p>{_h(case['dek'])}</p>
      <p><strong>Current status:</strong> {_h(case['status_reason'])}</p>
    </header>
    {open_notice}
    <div class="nw-case-file__meta">
      <div><dl><dt>Opened</dt><dd><time datetime="{_h(case['opened_at'])}">{_h(_human_time(case['opened_at']))}</time></dd></dl></div>
      <div><dl><dt>Updated</dt><dd><time datetime="{_h(case['updated_at'])}">{_h(_human_time(case['updated_at']))}</time></dd></dl></div>
      <div><dl><dt>Published</dt><dd>{_h(published_display)}</dd></dl></div>
      <div><dl><dt>Version receipt</dt><dd><code>{_h(case['version_id'])}</code><br><a href="revisions/{_h(case['version_id'])}.json">Immutable revision JSON</a></dd></dl></div>
    </div>
    <section class="nw-case-section" aria-labelledby="case-findings-title">
      <header><p class="nw-section__label">Claims and challenges</p><h2 id="case-findings-title">What is asserted—and what could overturn it</h2><p>Claim wording is reproduced from the structured record. Confidence and review state are not probability scores.</p></header>
      {_hypotheses_panel(case)}
      <ol class="nw-finding-list">{_investigation_claims(case)}</ol>
      <div class="nw-case-columns">{_counterevidence_panel(case)}{_falsification_panel(case)}</div>
    </section>
    <section class="nw-case-section" aria-labelledby="case-evidence-title">
      <header><p class="nw-section__label">Evidence ledger</p><h2 id="case-evidence-title">Inspect every receipt and interpretation limit</h2></header>
      {_investigation_evidence_table(case)}
    </section>
    <section class="nw-case-section" aria-labelledby="publication-gate-title">
      <header><p class="nw-section__label">Editorial threshold</p><h2 id="publication-gate-title">Publication gate</h2><p>A blocked gate keeps this work in the research-lead register regardless of how striking an individual measurement appears.</p></header>
      {_publication_gate(case)}
    </section>
    <section class="nw-case-section" aria-labelledby="limitations-title">
      <header><p class="nw-section__label">Epistemic boundary</p><h2 id="limitations-title">Limitations and consequences</h2></header>
      <ul class="nw-case-record-list">{limits}</ul>
    </section>
    <section class="nw-case-section" aria-labelledby="collection-targets-title">
      <header><p class="nw-section__label">Open collection</p><h2 id="collection-targets-title">Evidence still needed</h2><p>Targets name aggregate public evidence to collect. They never identify people to target.</p></header>
      <div class="nw-case-columns">{_collection_targets(case)}</div>
    </section>
    <section class="nw-case-section" aria-labelledby="methodology-title">
      <header><p class="nw-section__label">Reproducibility</p><h2 id="methodology-title">Methodology steps</h2></header>
      <ol class="nw-case-record-list">{_methodology_steps(case)}</ol>
    </section>
    <section class="nw-case-section" aria-labelledby="editorial-state-title">
      <header><p class="nw-section__label">Accountability</p><h2 id="editorial-state-title">Correction, reply and safety state</h2></header>
      <div class="nw-case-state-grid">
        <div><dl><dt>Correction</dt><dd>{_h(correction['status'])}<br>{_h(correction['note'])}<br>{_h(correction_time)}<br><a href="{_h(_investigation_href(correction['policy_url']))}">Correction policy</a></dd></dl></div>
        <div><dl><dt>Right to reply</dt><dd>{_h(reply['status'])}<br>{_h(reply['applicability_reason'])}<br>{len(reply['parties'])} institution{'s' if len(reply['parties']) != 1 else ''} recorded</dd></dl></div>
        <div><dl><dt>Safety</dt><dd>{_h(safety['data_level'])}<br>Person-level data: {_h(str(safety['person_level_data']).lower())}</dd></dl></div>
        <div><dl><dt>Current structured record</dt><dd><a href="case.json">case.json</a><br><code>{_h(case['case_id'])}</code></dd></dl></div>
      </div>
      <div class="nw-case-columns"><div class="nw-case-panel"><h3>Right-to-reply boundary</h3><p>No response is not evidence that a finding is true.</p></div><div class="nw-case-panel"><h3>Correction boundary</h3><p>Corrections append an immutable revision and preserve the previous public version.</p></div></div>
      <div class="nw-case-panel"><h3>Institutional reply register</h3><ul>{reply_parties}</ul></div>
      <p class="nw-investigation-notice"><strong>Safety boundary.</strong> Public, aggregate evidence only; private contact details, volunteer identifiers and person-level records are excluded.</p>
      {_safety_lists(case)}
    </section>
  </article>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/investigations/">← Investigations register</a> · <a href="case.json">Current case JSON</a> · <a href="/readings/investigations-latest.json">Structured desk</a> · <a href="/docs/INVESTIGATIONS.md">Method</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    is_published = case["status"] == "published"
    return _head(
        title=f"{case['title']} · Palimpsest Investigations",
        description=case["dek"],
        canonical=_case_public_url(case),
        page_type="article" if is_published else "website",
        published_at=case["published_at"] if is_published else None,
        modified_at=case["updated_at"],
        json_ld=_investigation_case_json_ld(case),
    ) + "\n" + body


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
{metric}        <h2>What the record says</h2>
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


def _event_json_ld(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "@id": event["url"],
        "mainEntityOfPage": {"@type": "WebPage", "@id": event["url"]},
        "headline": event["headline"],
        "description": event["dek"],
        "datePublished": event["published_at"],
        "dateModified": event["updated_at"],
        "articleSection": EVENT_DESKS[event["desk"]],
        "inLanguage": _event_language(event),
        "isAccessibleForFree": True,
        "author": _organization(),
        "publisher": _organization(),
        "image": [OG_IMAGE],
        "citation": [ref["url"] for ref in event["evidence_refs"]],
        "keywords": [*event["topics"], "China", "open source intelligence"],
    }


def render_event(
    event: Mapping[str, Any],
    *,
    wire: Mapping[str, Any],
    feed: Mapping[str, Any],
) -> str:
    items = _wire_items(wire)
    stories = {story["signal_id"]: story for story in feed["stories"]}
    facts = "".join(
        f"""<li><strong>{_h(fact['attribution'])}.</strong> <span lang="{_h(_text_language(fact['statement'], source_id=event['evidence_refs'][0]['source_id']))}">{_h(fact['statement'])}</span> <time datetime="{_h(fact['published_at'])}">{_h(_human_time(fact['published_at']))}</time></li>"""
        for fact in event["reported_facts"]
    )
    evidence_rows = []
    for ref in event["evidence_refs"]:
        item = items[ref["item_id"]]
        title_language = _text_language(ref["title"], source_id=ref["source_id"])
        excerpt_language = _text_language(item["excerpt"], source_id=ref["source_id"])
        evidence_rows.append(f"""<tr>
  <td><span class="nw-role" data-role="{_h(ref['role'])}">{_h(ref['role'])}</span></td>
  <td><a href="{_h(ref['url'])}">{_h(ref['source_name'])}</a><small>{_h(ref['independence_group'])}</small></td>
  <td><span lang="{_h(title_language)}">{_h(ref['title'])}</span><small lang="{_h(excerpt_language)}">{_h(item['excerpt'] or 'No feed excerpt supplied.')}</small></td>
  <td><time datetime="{_h(ref['published_at'])}">{_h(_human_time(ref['published_at']))}</time><small>feed sha {_h(item['feed_sha256'][:12])}</small></td>
</tr>""")
    limitations = "".join(f"<li>{_h(value)}</li>" for value in event["limitations"])
    scan_links = "".join(
        f"""<a href="{_h(_site_path(stories[signal_id]['url']))}"><strong>{_h(stories[signal_id]['headline'])}</strong><span>Current instrument · topical pointer only</span></a>"""
        for signal_id in event["declared_links"]["scan_signal_ids"]
        if signal_id in stories
    )
    economic_links = "".join(
        f"""<a href="/news/economy/"><strong>{_h(signal_id)}</strong><span>Economic surface · topical pointer only</span></a>"""
        for signal_id in event["declared_links"]["economic_signal_ids"]
    )
    declared_links = scan_links + economic_links or (
        "<p>No Palimpsest measurement surface is declared for this event. "
        "That absence is not evidence that no measurable change occurred.</p>"
    )
    mutation = event["mutation"]
    previous = mutation["previous_version_id"] or "none — first retained version"
    language = _event_language(event)
    dek_language = _text_language(
        event["dek"], source_id=event["evidence_refs"][0]["source_id"]
    )
    body = f"""<body class="ps newsroom-page newsroom-page--dossier">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <article class="nw-article nw-dossier">
    <header class="nw-article__header">
      <p class="nw-article__kicker">{_h(EVENT_DESKS[event['desk']])} · {_h(EVIDENCE_LABELS[event['evidence_strength']])}</p>
      <h1 lang="{_h(language)}">{_h(event['headline'])}</h1>
      <p class="nw-article__dek" lang="{_h(dek_language)}">{_h(event['dek'])}</p>
      <p class="nw-article__meta"><span>By {PUBLISHER}</span><time datetime="{_h(event['published_at'])}">{_h(_human_time(event['published_at']))}</time><span>{_h(mutation['kind'])} dossier version</span></p>
    </header>
    <div class="nw-dossier__summary">
      <div><p class="nw-receipt__label">Editorial disposition</p><p>{_h(event['lead_reason'])}</p></div>
      <div><p class="nw-receipt__label">Evidence strength</p><strong>{_h(EVIDENCE_LABELS[event['evidence_strength']])}</strong><p>{len(event['evidence_groups'])} independent group{'s' if len(event['evidence_groups']) != 1 else ''}; this is source structure, not a truth probability.</p></div>
      <div><p class="nw-receipt__label">Revision receipt</p><code>{_h(event['version_id'])}</code><p>Previous: <code>{_h(previous)}</code></p><a href="revisions/{_h(event['version_id'])}.json">Immutable revision JSON</a></div>
    </div>
    <section class="nw-dossier__section" aria-labelledby="reported-title">
      <p class="nw-section__label">Reported facts</p><h2 id="reported-title">What the registered sources published</h2>
      <ol class="nw-fact-list">{facts}</ol>
    </section>
    <section class="nw-dossier__section" aria-labelledby="braid-title">
      <p class="nw-section__label">Evidence braid</p><h2 id="braid-title">Order, provenance and declared surfaces</h2>
      {_event_braid(event, wire)}
      <p class="nw-method-note">The braid reports source ordering. A declared topic surface is not a timed statistical match and cannot establish cause, coordination, censorship, or economic impact.</p>
    </section>
    <section class="nw-dossier__section" aria-labelledby="matrix-title">
      <p class="nw-section__label">Evidence matrix</p><h2 id="matrix-title">Inspect every receipt</h2>
      <p class="nw-table-cue" id="evidence-matrix-cue">Scroll horizontally to inspect every column.</p>
      <div class="nw-table-wrap" role="region" tabindex="0" aria-labelledby="matrix-title" aria-describedby="evidence-matrix-cue"><table class="nw-evidence-table"><caption>Evidence receipts for this dossier</caption><thead><tr><th scope="col">Role</th><th scope="col">Source / group</th><th scope="col">Feed record</th><th scope="col">Published / hash</th></tr></thead><tbody>{''.join(evidence_rows)}</tbody></table></div>
    </section>
    <section class="nw-dossier__section" aria-labelledby="surfaces-title">
      <p class="nw-section__label">Measurement surfaces</p><h2 id="surfaces-title">Where Palimpsest can test the topic</h2>
      <div class="nw-surface-links">{declared_links}</div>
    </section>
    <section class="nw-dossier__section" aria-labelledby="limits-title">
      <p class="nw-section__label">Epistemic boundary</p><h2 id="limits-title">What this dossier cannot establish</h2>
      <ul class="nw-limitations">{limitations}</ul>
    </section>
  </article>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Latest evidence wire</a> · <a href="/readings/newswire-latest.json">Structured wire</a> · <a href="story.json">Current dossier JSON</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title=f"{event['headline']} · Palimpsest Wire",
        description=event["dek"],
        canonical=event["url"],
        page_type="article",
        published_at=event["published_at"],
        modified_at=event["updated_at"],
        json_ld=_event_json_ld(event),
    ) + "\n" + body


def render_wire_archive(
    wire: Mapping[str, Any],
    *,
    events: Sequence[Mapping[str, Any]] | None = None,
    page: int = 1,
    n_pages: int = 1,
) -> str:
    page_events = list(events if events is not None else wire["events"])
    cards = "".join(_event_card(event) for event in page_events)
    page_suffix = f" · page {page} of {n_pages}" if n_pages > 1 else ""
    previous_href = (
        "/news/wire/" if page == 2
        else f"/news/wire/page/{page - 1}/" if page > 2
        else ""
    )
    next_href = f"/news/wire/page/{page + 1}/" if page < n_pages else ""
    pagination_links = []
    if previous_href:
        pagination_links.append(f'<a rel="prev" href="{previous_href}">← Newer dossiers</a>')
    pagination_links.append(f'<span>Page {page} of {n_pages}</span>')
    if next_href:
        pagination_links.append(f'<a rel="next" href="{next_href}">Older dossiers →</a>')
    pagination = (
        '<nav class="nw-pagination" aria-label="Dossier archive pages">'
        + "".join(pagination_links)
        + "</nav>"
    )
    canonical = (
        f"{SITE}/news/wire/" if page == 1
        else f"{SITE}/news/wire/page/{page}/"
    )
    body = f"""<body class="ps newsroom-page newsroom-page--archive">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-article__header nw-archive-head"><p class="nw-article__kicker">Receipt-complete event wire{_h(page_suffix)}</p><h1>China evidence dossiers</h1><p class="nw-article__dek">Every accepted current-window feed item is partitioned into exactly one dossier. Corroborated leads and single-source attributed records remain visibly different.</p></header>
  {pagination}
  <div class="nw-event-grid nw-event-grid--archive">{cards}</div>
  {pagination}
  {_accountability_tape(wire)}
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Palimpsest Wire</a> · <a href="/readings/newswire-latest.json">Structured wire</a></div></footer>
{site_nav.FOOT}
</body></html>"""
    return _head(
        title=f"China evidence dossiers{page_suffix} · Palimpsest Wire",
        description=wire["scope"],
        canonical=canonical,
        page_type="website",
        modified_at=wire["generated_at"],
        json_ld={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "url": canonical,
            "name": f"China evidence dossiers{page_suffix}",
            "dateModified": wire["generated_at"],
        },
    ) + "\n" + body


def _format_economic_value(metric: Mapping[str, Any]) -> str:
    value = _number(metric["value"])
    if metric["unit"] == "percent":
        return f"{value}%"
    if metric["unit"] == "ratio":
        return f"{_number(metric['value'] * 100)}%"
    return f"{value} {metric['unit']}"


def render_economic_page(pulse: Mapping[str, Any]) -> str:
    gate_rows = "".join(
        f"""<li data-passed="{_h(str(gate['passed']).lower())}"><span>{_h(gate['label'])}</span><strong>{gate['observed']} / {gate['minimum']}</strong></li>"""
        for gate in pulse["readiness"]["gates"]
    )
    desk_blocks = []
    for desk in pulse["desks"]:
        cards = []
        for metric in desk["metrics"]:
            revision = metric["revision"]
            release = _human_time(metric["released_at"]) if metric["released_at"] else "source gives date/period only"
            cards.append(f"""<article class="nw-metric-card" id="{_h(metric['metric_id'])}" data-freshness="{_h(metric['freshness']['status'])}">
  <p class="nw-card__kicker">{_h(metric['source_class'])} · {_h(metric['freshness']['status'])}</p>
  <h3>{_h(metric['label'])}</h3>
  <p class="nw-metric-card__value">{_h(_format_economic_value(metric))}</p>
  <dl><dt>Period</dt><dd>{_h(metric['period_start'])} → {_h(metric['period_end'])}</dd><dt>Released</dt><dd>{_h(release)}</dd><dt>Collected</dt><dd>{_h(_human_time(metric['collected_at']))}</dd><dt>Source group</dt><dd>{_h(metric['independence_group'])}</dd><dt>Comparability</dt><dd>{_h(metric['comparability']['basis'])}</dd><dt>Revision</dt><dd>{_h(revision['status'])}</dd></dl>
  <p class="nw-metric-card__limit">{_h(metric['limitation'])}</p>
  <a href="{_h(metric['evidence']['url'])}">Open evidence receipt</a>
</article>""")
        if not cards:
            cards.append("<div class=\"nw-empty-desk\"><strong>No current metric</strong><p>Not collected is not zero. The source backlog remains visible in the coverage matrix.</p></div>")
        desk_blocks.append(f"""<section class="nw-section nw-econ-desk" id="desk-{_h(desk['id'])}"><div class="nw-section__head"><div><p class="nw-section__label">Economic evidence desk</p><h2>{_h(desk['title'])}</h2></div><p class="nw-section__dek">{_h(desk['limitations'][0])}</p></div><div class="nw-metric-grid">{''.join(cards)}</div></section>""")
    coverage_rows = "".join(
        f"""<tr><td>{_h(row['domain'])}</td><td>{_h(row['status'])}</td><td>{_h(', '.join(row['observed_groups']) or 'none')}</td><td>{_h(', '.join(row['adapter_ready_groups']) or 'none')}</td></tr>"""
        for row in pulse["coverage"]["matrix"]
    )
    body = f"""<body class="ps newsroom-page newsroom-page--economy">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-article__header nw-economy-head"><p class="nw-article__kicker">China economic evidence · {_h(pulse['economic_state']['status'])} · as known {_h(_human_time(pulse['as_of']))}</p><h1>The economic pulse abstains—and shows you exactly why.</h1><p class="nw-article__dek">{_h(pulse['economic_state']['claim'])}</p></header>
  <section class="nw-econ-gates"><div><p class="nw-kicker nw-kicker--economic">Readiness, not rhetoric</p><h2>Composite gates</h2><p>{_h(pulse['readiness']['abstention_reason'])}</p></div><ul>{gate_rows}</ul></section>
  {''.join(desk_blocks)}
  <section class="nw-dossier__section" aria-labelledby="coverage-matrix-title"><p class="nw-section__label">Coverage matrix</p><h2 id="coverage-matrix-title">Observed, adapter-ready and absent</h2><p class="nw-table-cue" id="coverage-matrix-cue">Scroll horizontally to inspect every column.</p><div class="nw-table-wrap" role="region" tabindex="0" aria-labelledby="coverage-matrix-title" aria-describedby="coverage-matrix-cue"><table class="nw-evidence-table"><caption>Economic evidence collection coverage</caption><thead><tr><th scope="col">Domain</th><th scope="col">Status</th><th scope="col">Observed groups</th><th scope="col">Adapter-ready groups</th></tr></thead><tbody>{coverage_rows}</tbody></table></div></section>
  <aside class="nw-coverage"><div><p class="nw-kicker nw-kicker--warning">Prohibited shortcuts</p><h2>What the pulse does not claim</h2></div><div class="nw-coverage__items">{''.join(f'<div class="nw-coverage__item"><p>{_h(value)}</p></div>' for value in pulse['economic_state']['prohibited_interpretations'])}</div></aside>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Palimpsest Wire</a> · <a href="/readings/china-economic-pulse-latest.json">Structured economic pulse</a> · <a href="/data.html">Evidence Atlas</a></div></footer>
{site_nav.FOOT}
</body></html>"""
    return _head(
        title="China economic state · Palimpsest Wire",
        description=pulse["scope"],
        canonical=f"{SITE}/news/economy/",
        page_type="website",
        modified_at=pulse["generated_at"],
        json_ld={
            "@context": "https://schema.org",
            "@type": "Dataset",
            "name": "Palimpsest China Economic Pulse",
            "description": pulse["scope"],
            "dateModified": pulse["generated_at"],
            "url": f"{SITE}/readings/china-economic-pulse-latest.json",
            "creator": _organization(),
        },
    ) + "\n" + body


def build_json_feed(
    feed: Mapping[str, Any], wire: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    sections = {section["id"]: section["title"] for section in feed["sections"]}
    event_items = []
    if wire is not None:
        event_items = [
            {
                "id": event["event_id"],
                "url": event["url"],
                "external_url": event["evidence_refs"][0]["url"],
                "title": event["headline"],
                "summary": event["dek"],
                "content_text": "\n\n".join(
                    [fact["statement"] for fact in event["reported_facts"]]
                    + ["Evidence boundary: " + " ".join(event["limitations"])]
                ),
                "date_published": event["published_at"],
                "date_modified": event["updated_at"],
                "tags": [
                    EVENT_DESKS[event["desk"]],
                    event["evidence_strength"],
                    *event["topics"],
                ],
                "attachments": [
                    {
                        "url": ref["url"],
                        "mime_type": "text/html",
                        "title": f"{ref['source_name']}: {ref['title']}",
                    }
                    for ref in event["evidence_refs"]
                ],
                "_palimpsest": {
                    "kind": "event_dossier",
                    "version_id": event["version_id"],
                    "evidence_strength": event["evidence_strength"],
                    "independent_groups": len(event["evidence_groups"]),
                },
            }
            for event in wire["events"]
        ]
    instrument_items = [
        {
            "id": story["id"] if wire is not None else story["id"] + ":" + story["claim_fingerprint"],
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
            **(
                {"_palimpsest": {
                    "kind": "instrument_brief",
                    "revision_id": _revision_id(story, "storyv"),
                }}
                if wire is not None else {}
            ),
        }
        for story in feed["stories"]
    ]
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Palimpsest Wire" if wire is not None else feed["title"],
        "home_page_url": feed["url"],
        "feed_url": f"{SITE}/news/feed.json",
        "description": wire["scope"] if wire is not None else feed["scope"],
        "language": "en",
        "authors": [{"name": PUBLISHER, "url": f"{SITE}/"}],
        "items": event_items + instrument_items,
    }


def build_rss(
    feed: Mapping[str, Any], wire: Mapping[str, Any] | None = None
) -> bytes:
    items = []
    if wire is not None:
        for event in wire["events"]:
            description = (
                event["dek"]
                + " Evidence boundary: "
                + event["limitations"][1]
                + " Sources: "
                + ", ".join(ref["url"] for ref in event["evidence_refs"])
            )
            items.append(f"""  <item>
    <title>{xml_escape(event['headline'])}</title>
    <link>{xml_escape(event['url'])}</link>
    <guid isPermaLink="false">{xml_escape(event['event_id'])}</guid>
    <pubDate>{_rfc2822(event['published_at'])}</pubDate>
    <description>{xml_escape(description)}</description>
    <category>{xml_escape(event['desk'])}</category>
    <source url="{xml_escape(event['evidence_refs'][0]['url'])}">{xml_escape(event['evidence_refs'][0]['source_name'])}</source>
  </item>""")
    for story in feed["stories"]:
        description = story["dek"] + " Evidence: " + story["evidence"]["url"]
        guid = story["id"] if wire is not None else story["id"] + ":" + story["claim_fingerprint"]
        items.append(f"""  <item>
    <title>{xml_escape(story['headline'])}</title>
    <link>{xml_escape(story['url'])}</link>
    <guid isPermaLink="false">{xml_escape(guid)}</guid>
    <pubDate>{_rfc2822(story['published_at'])}</pubDate>
    <description>{xml_escape(description)}</description>
    <category>{xml_escape(story['section'])}</category>
    <source url="{xml_escape(story['evidence']['url'])}">{xml_escape(story['signal_id'])}</source>
  </item>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{xml_escape('Palimpsest Wire' if wire is not None else feed['title'])}</title>
  <link>{xml_escape(feed['url'])}</link>
  <description>{xml_escape(wire['scope'] if wire is not None else feed['scope'])}</description>
  <language>en</language>
  <lastBuildDate>{_rfc2822(max(feed['generated_at'], wire['generated_at']) if wire is not None else feed['generated_at'])}</lastBuildDate>
  <atom:link href="{SITE}/news/feed.xml" rel="self" type="application/rss+xml" />
{chr(10).join(items)}
</channel>
</rss>
"""
    return xml.encode("utf-8")


def build_sitemap(
    feed: Mapping[str, Any],
    wire: Mapping[str, Any] | None = None,
    investigations: Mapping[str, Any] | None = None,
) -> bytes:
    urls = [
        f"""  <url><loc>{SITE}/news/</loc><lastmod>{xml_escape(feed['generated_at'])}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>"""
    ]
    if wire is not None:
        archive_pages = max(1, (len(wire["events"]) + WIRE_PAGE_SIZE - 1) // WIRE_PAGE_SIZE)
        urls.append(
            f"  <url><loc>{SITE}/news/wire/</loc><lastmod>{xml_escape(wire['generated_at'])}</lastmod><changefreq>hourly</changefreq></url>"
        )
        urls.extend(
            f"  <url><loc>{SITE}/news/wire/page/{page}/</loc><lastmod>{xml_escape(wire['generated_at'])}</lastmod><changefreq>hourly</changefreq></url>"
            for page in range(2, archive_pages + 1)
        )
        urls.extend(
            f"  <url><loc>{xml_escape(event['url'])}</loc><lastmod>{xml_escape(event['updated_at'])}</lastmod><news:news><news:publication><news:name>Palimpsest Wire</news:name><news:language>en</news:language></news:publication><news:publication_date>{xml_escape(event['published_at'])}</news:publication_date><news:title>{xml_escape(event['headline'])}</news:title></news:news></url>"
            for event in wire["events"]
        )
        urls.append(
            f"  <url><loc>{SITE}/news/economy/</loc><lastmod>{xml_escape(wire['generated_at'])}</lastmod><changefreq>daily</changefreq></url>"
        )
    if investigations is not None:
        urls.append(
            f"  <url><loc>{SITE}/news/investigations/</loc><lastmod>{xml_escape(investigations['generated_at'])}</lastmod><changefreq>daily</changefreq><priority>0.9</priority></url>"
        )
        for case in investigations["cases"]:
            news_markup = ""
            if case["status"] == "published":
                news_markup = f"""<news:news><news:publication><news:name>Palimpsest Investigations</news:name><news:language>{xml_escape(_case_language(case))}</news:language></news:publication><news:publication_date>{xml_escape(case['published_at'])}</news:publication_date><news:title>{xml_escape(case['title'])}</news:title></news:news>"""
            urls.append(
                f"  <url><loc>{xml_escape(_case_public_url(case))}</loc><lastmod>{xml_escape(case['updated_at'])}</lastmod>{news_markup}</url>"
            )
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


def build_outputs(
    feed: Mapping[str, Any],
    *,
    wire: Mapping[str, Any] | None = None,
    pulse: Mapping[str, Any] | None = None,
    investigations: Mapping[str, Any] | None = None,
) -> dict[Path, bytes]:
    """Return every public output without touching the filesystem."""

    if wire is not None:
        newswire_model.validate_newswire_document(wire)
    if pulse is not None:
        economic_pulse_model.validate_economic_pulse(pulse)
    if investigations is not None:
        investigations_model.validate_investigations(investigations)
    sections = {section["id"]: section for section in feed["sections"]}
    stories = {story["signal_id"]: story for story in feed["stories"]}
    outputs: dict[Path, bytes] = {
        Path("readings/newsroom-latest.json"): _pretty_json(feed),
        Path("news/index.html"): (
            render_evidence_index(feed, wire, pulse, investigations)
            if wire is not None
            else render_index(feed)
        ).encode("utf-8"),
        Path("news/feed.json"): _pretty_json(build_json_feed(feed, wire)),
        Path("news/feed.xml"): build_rss(feed, wire),
        Path("news/sitemap.xml"): build_sitemap(feed, wire, investigations),
    }
    if wire is not None:
        outputs[Path("news/instruments/feed.json")] = _pretty_json(build_json_feed(feed))
        outputs[Path("news/instruments/feed.xml")] = build_rss(feed)
        event_pages = [
            wire["events"][offset:offset + WIRE_PAGE_SIZE]
            for offset in range(0, len(wire["events"]), WIRE_PAGE_SIZE)
        ] or [[]]
        for page_number, page_events in enumerate(event_pages, 1):
            archive_path = (
                Path("news/wire/index.html") if page_number == 1
                else Path("news/wire/page") / str(page_number) / "index.html"
            )
            outputs[archive_path] = render_wire_archive(
                wire,
                events=page_events,
                page=page_number,
                n_pages=len(event_pages),
            ).encode("utf-8")
        archive: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for event in wire["events"]:
            year, month = event["published_at"][:7].split("-")
            archive.setdefault((year, month), []).append(event)
            base = Path("news/wire") / event["event_id"]
            outputs[base / "index.html"] = render_event(
                event, wire=wire, feed=feed
            ).encode("utf-8")
            outputs[base / "story.json"] = _pretty_json(event)
            outputs[base / "revisions" / f"{event['version_id']}.json"] = _pretty_json(event)
        for (year, month), events in sorted(archive.items()):
            outputs[Path("news/archive") / year / month / "index.json"] = _pretty_json({
                "schema_version": "palimpsest-news-archive.v1",
                "year": int(year),
                "month": int(month),
                "generated_at": wire["generated_at"],
                "n_events": len(events),
                "events": events,
            })
    if pulse is not None:
        outputs[Path("news/economy/index.html")] = render_economic_page(pulse).encode("utf-8")
    if investigations is not None:
        outputs[Path("news/investigations/index.html")] = (
            render_investigations_index(investigations).encode("utf-8")
        )
        for case in investigations["cases"]:
            base = Path("news/investigations") / case["slug"]
            outputs[base / "index.html"] = render_investigation_case(case).encode(
                "utf-8"
            )
            outputs[base / "case.json"] = _pretty_json(case)
            outputs[base / "revisions" / f"{case['version_id']}.json"] = (
                _pretty_json(case)
            )
    for story in feed["stories"]:
        base = Path("news") / story["slug"]
        outputs[base / "index.html"] = render_story(
            story,
            section=sections[story["section"]],
            by_id=stories,
        ).encode("utf-8")
        outputs[base / "story.json"] = _pretty_json(story)
        if wire is not None:
            revision = _revision_id(story, "storyv")
            outputs[base / "revisions" / f"{revision}.json"] = _pretty_json(story)
    if wire is not None or investigations is not None:
        manifest_path = Path("news/generated-manifest.json")
        all_paths = sorted([str(path) for path in outputs] + [str(manifest_path)])
        immutable = [path for path in all_paths if "/revisions/" in path]
        generated_times = [feed["generated_at"]]
        if wire is not None:
            generated_times.append(wire["generated_at"])
        if investigations is not None:
            generated_times.append(investigations["generated_at"])
        outputs[manifest_path] = _pretty_json({
            "schema_version": "palimpsest-news-manifest.v1",
            "generated_at": max(generated_times),
            "n_paths": len(all_paths),
            "paths": all_paths,
            "immutable_revision_paths": immutable,
            "mutable_paths": [path for path in all_paths if path not in immutable],
        })
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
    wire, pulse, investigations = _load_extension_documents()
    outputs = build_outputs(
        feed,
        wire=wire,
        pulse=pulse,
        investigations=investigations,
    )
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
        f"newsroom -> {READING.relative_to(ROOT)} · {feed['n_stories']} instruments · "
        f"{wire['n_events'] if wire else 0} events · "
        f"{changed} files updated · {unchanged} unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
