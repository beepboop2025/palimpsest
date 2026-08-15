#!/usr/bin/env python3
"""Build Live Eval Findings from verified, sealed evaluation artifacts.

This renderer owns only `/journal/` and `readings/eval-articles-latest.json`.
It writes every current article and a content-addressed immutable revision. Old
revision files remain available, while a changed article head points back to
its prior revision through the structured document.
"""
from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

from core import eval_articles
from scripts import site_nav


ROOT = Path(__file__).resolve().parents[1]
SITE = "https://palimpsest.info"
JOURNAL = ROOT / "journal"
READING = ROOT / "readings" / "eval-articles-latest.json"
MANIFEST = Path("journal/generated-manifest.json")
_REVISION_FILE = re.compile(r"^evalarticlev-[0-9a-f]{24}\.json$")


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json_script(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _absolute(path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise eval_articles.EvalArticleError("journal route is not site-absolute")
    return SITE + path


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname:
        return value
    if value.startswith("/") and not value.startswith("//"):
        return value
    return None


def _time_label(value: str) -> str:
    return eval_articles._timestamp(value, "timestamp").strftime("%-d %b %Y, %H:%M UTC")


def _head(
    *,
    title: str,
    description: str,
    canonical: str,
    structured: Mapping[str, Any],
    article: Mapping[str, Any] | None = None,
) -> str:
    meta = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_h(title)}</title>",
        f'<meta name="description" content="{_h(description)}">',
        f'<link rel="canonical" href="{_h(canonical)}">',
        '<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">',
        '<meta name="theme-color" content="#08110f">',
        '<meta property="og:site_name" content="Palimpsest">',
        f'<meta property="og:title" content="{_h(title)}">',
        f'<meta property="og:description" content="{_h(description)}">',
        f'<meta property="og:url" content="{_h(canonical)}">',
        f'<meta property="og:type" content="{"article" if article else "website"}">',
        '<meta property="og:image" content="https://palimpsest.info/brand/palimpsest-og2.png">',
        '<meta name="twitter:card" content="summary_large_image">',
        site_nav.HEAD,
        '<link rel="stylesheet" href="/assets/journal.css">',
        f'<script type="application/ld+json">{_json_script(structured)}</script>',
    ]
    if article is not None:
        meta.extend(
            [
                f'<meta property="article:published_time" content="{_h(article["published_at"])}">',
                f'<meta property="article:modified_time" content="{_h(article["updated_at"])}">',
                '<meta property="article:section" content="AI evaluation">',
            ]
        )
    return "\n".join(meta)


def _index_json_ld(collection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "Live Eval Findings",
        "description": (
            "Dated, evidence-bound interpretation of Palimpsest's sealed AI evaluations."
        ),
        "url": f"{SITE}/journal/",
        "dateModified": collection["generated_at"],
        "publisher": {
            "@type": "Organization",
            "name": "Palimpsest Observatory",
            "url": SITE,
        },
        "hasPart": [
            {
                "@type": "Article",
                "headline": article["title"],
                "url": _absolute(article["url"]),
                "datePublished": article["published_at"],
                "dateModified": article["updated_at"],
            }
            for article in collection["articles"]
        ],
    }


def _article_json_ld(article: Mapping[str, Any]) -> dict[str, Any]:
    source_urls = sorted(
        {
            row["source_url"]
            for row in article["evidence"]
            if _safe_url(row["source_url"])
        }
    )
    return {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": article["title"],
        "description": article["dek"],
        "url": _absolute(article["url"]),
        "mainEntityOfPage": _absolute(article["url"]),
        "datePublished": article["published_at"],
        "dateModified": article["updated_at"],
        "articleSection": "AI evaluation",
        "author": {
            "@type": "Organization",
            "name": article["authorship"]["byline"],
            "url": f"{SITE}/journal/",
        },
        "publisher": {
            "@type": "Organization",
            "name": "Palimpsest Observatory",
            "url": SITE,
            "logo": {
                "@type": "ImageObject",
                "url": f"{SITE}/brand/palimpsest-icon-512.png",
            },
        },
        "isBasedOn": source_urls,
        "about": ["AI evaluation", "model refusal", "evaluation integrity"],
        "image": f"{SITE}/brand/palimpsest-og2.png",
    }


def _citation_links(citation_ids: Sequence[str]) -> str:
    return ", ".join(
        f'<a href="#evidence-{_h(citation_id)}">{_h(citation_id)}</a>'
        for citation_id in citation_ids
    )


def _article_card(article: Mapping[str, Any], *, featured: bool = False) -> str:
    state = str(article["finding_state"])
    class_name = "ej-card ej-card--feature" if featured else "ej-card"
    numbers = "".join(
        f'<li><strong>{_h(item["value"])}</strong><span>{_h(item["label"])}</span></li>'
        for item in article["key_numbers"][:3]
    )
    return f"""<article class="{class_name}" data-finding-state="{_h(state)}">
  <div class="ej-card__spine" aria-hidden="true"><span></span><span></span><span></span></div>
  <div class="ej-card__copy">
    <p class="ej-kicker">{_h(article['kicker'])}</p>
    <h2><a href="{_h(article['url'])}">{_h(article['title'])}</a></h2>
    <p class="ej-card__dek">{_h(article['dek'])}</p>
    <ul class="ej-card__numbers" aria-label="Key figures">{numbers}</ul>
    <p class="ej-card__meta"><span>{_h(article['authorship']['byline'])}</span><time datetime="{_h(article['updated_at'])}">Updated {_h(_time_label(article['updated_at']))}</time></p>
    <a class="ej-read" href="{_h(article['url'])}">Read the analysis <span aria-hidden="true">→</span></a>
  </div>
</article>"""


def render_index(collection: Mapping[str, Any]) -> str:
    eval_articles.validate_collection(collection)
    articles = list(collection["articles"])
    featured = articles[0]
    rest = articles[1:]
    cards = _article_card(featured, featured=True) + "".join(
        _article_card(article) for article in rest
    )
    head = _head(
        title="Live Eval Findings | Palimpsest",
        description=(
            "Dated analysis from sealed AI evaluations, with controls, uncertainty, citations, and revision receipts attached."
        ),
        canonical=f"{SITE}/journal/",
        structured=_index_json_ld(collection),
    )
    return f"""<!doctype html>
<html lang="en"><head>
{head}
</head><body class="ps ej">
{site_nav.render('/journal/')}
<main id="main">
  <header class="ej-masthead ej-wrap">
    <div class="ej-masthead__issue">
      <span>PALIMPSEST / LIVE EVAL FINDINGS</span>
      <span>Edition {_h(_time_label(collection['generated_at']))}</span>
    </div>
    <div class="ej-masthead__grid">
      <div>
        <p class="ej-eyebrow">Interpretation with receipts attached</p>
        <h1>The Eval<br><span>Journal</span></h1>
      </div>
      <div class="ej-masthead__statement">
        <p>AI evaluation scores are easy to publish and hard to read honestly. This desk starts with the controls, carries uncertainty into the headline, and binds every analytical sentence to a sealed run.</p>
        <div class="ej-undertext" aria-hidden="true">
          <span>one score, one ranking, one easy answer</span>
          <strong>dated finding, visible limits, reproducible record</strong>
        </div>
      </div>
    </div>
    <div class="ej-masthead__rule"><span></span><b>{len(articles):02d}</b><small>current analyses</small></div>
  </header>

  <section class="ej-ledger ej-wrap" aria-labelledby="latest-analysis">
    <div class="ej-section-head">
      <p>Latest from the desk</p>
      <h2 id="latest-analysis">Read the finding. Then inspect the instrument.</h2>
      <a href="/journal/feed.xml">Follow every verified revision</a>
    </div>
    <div class="ej-card-stack">{cards}</div>
  </section>

  <section class="ej-principles ej-wrap" aria-labelledby="journal-standard">
    <div class="ej-section-head">
      <p>Publication standard</p>
      <h2 id="journal-standard">The eval does not get the last word.</h2>
    </div>
    <ol>
      <li><span>01</span><div><h3>Seal first</h3><p>The prompt commitment and result are verified against the append-only registry before a sentence is written.</p></div></li>
      <li><span>02</span><div><h3>Controls before score</h3><p>A failed ordinary control turns the article into an instrument warning. It cannot become a suppression claim.</p></div></li>
      <li><span>03</span><div><h3>Uncertainty in the headline</h3><p>Rates keep their denominator, interval, model label, and timestamp. Nothing becomes a standing leaderboard.</p></div></li>
      <li><span>04</span><div><h3>Show the counterread</h3><p>Every piece carries the strongest alternative reading, limitations, and the command needed to verify the chain.</p></div></li>
    </ol>
  </section>

  <section class="ej-source-strip" aria-label="Journal source surfaces">
    <div class="ej-wrap">
      <p><span>Follow the record</span> The prose is a route into the evidence, never a replacement for it.</p>
      <nav aria-label="Eval source links">
        <a href="/readings/eval-registry.html">Eval Registry</a>
        <a href="/readings/refusal-drift-latest.json">Latest panel JSON</a>
        <a href="/readings/refusal-drift-history.jsonl">History JSONL</a>
        <a href="/journal/feed.xml">Findings RSS</a>
      </nav>
    </div>
  </section>
</main>
<footer class="ej-footer"><div class="ej-wrap"><span>Palimpsest Observatory</span><p>Public evidence. Visible limits. Offline verification.</p><a href="/fund.html">Fund the public good</a></div></footer>
{site_nav.FOOT}
</body></html>
"""


def _render_sections(article: Mapping[str, Any]) -> str:
    rendered = []
    for index, section in enumerate(article["sections"], 1):
        paragraphs = []
        for paragraph in section["paragraphs"]:
            prose = " ".join(_h(sentence["text"]) for sentence in paragraph["sentences"])
            receipts = sorted(
                {
                    citation
                    for sentence in paragraph["sentences"]
                    for citation in sentence["citation_ids"]
                }
            )
            paragraphs.append(
                f'<p>{prose}<span class="ej-citations"><b>Receipts</b> {_citation_links(receipts)}</span></p>'
            )
        rendered.append(
            f"""<section class="ej-article-section" id="{_h(section['section_id'])}">
  <header><span>{index:02d}</span><h2>{_h(section['heading'])}</h2></header>
  <div>{''.join(paragraphs)}</div>
</section>"""
        )
    return "".join(rendered)


def _render_records(title: str, records: Sequence[Mapping[str, Any]], class_name: str) -> str:
    items = "".join(
        f'<li><p>{_h(record["text"])}</p><span>{_citation_links(record["citation_ids"])}</span></li>'
        for record in records
    )
    return f'<section class="ej-record {class_name}"><h2>{_h(title)}</h2><ul>{items}</ul></section>'


def _revision_inventory(article: Mapping[str, Any], *, root: Path) -> list[str]:
    revision_dir = root / "journal" / article["slug"] / "revisions"
    current_id = article["revision_id"]
    links = {current_id: article["previous_revision_id"]}
    try:
        candidates = sorted(revision_dir.iterdir())
    except FileNotFoundError:
        candidates = []
    except OSError as exc:
        raise eval_articles.EvalArticleError("cannot inspect journal revisions") from exc
    if not candidates:
        return [current_id]
    for path in candidates:
        if not path.is_file() or _REVISION_FILE.fullmatch(path.name) is None:
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise eval_articles.EvalArticleError(
                f"cannot read journal revision {path.name}"
            ) from exc
        document = eval_articles._strict_json(raw, label=path.name)
        if not isinstance(document, dict) or document.get("revision_id") != path.stem:
            raise eval_articles.EvalArticleError(
                f"journal revision identity does not match {path.name}"
            )
        if (
            document.get("article_id") != article["article_id"]
            or document.get("slug") != article["slug"]
        ):
            raise eval_articles.EvalArticleError(
                f"journal revision belongs to another article: {path.name}"
            )
        previous_id = document.get("previous_revision_id")
        if previous_id is not None and (
            not isinstance(previous_id, str)
            or _REVISION_FILE.fullmatch(f"{previous_id}.json") is None
        ):
            raise eval_articles.EvalArticleError(
                f"journal revision has an invalid predecessor: {path.name}"
            )
        revision_id = document["revision_id"]
        try:
            computed_id = eval_articles._article_identity(document)
        except eval_articles.EvalArticleError as exc:
            raise eval_articles.EvalArticleError(
                f"journal revision content is malformed: {path.name}"
            ) from exc
        if computed_id != revision_id or computed_id != path.stem:
            raise eval_articles.EvalArticleError(
                f"journal revision content does not match its identity: {path.name}"
            )
        if revision_id in links and links[revision_id] != previous_id:
            raise eval_articles.EvalArticleError(
                f"journal revision link disagrees with current head: {path.name}"
            )
        links[revision_id] = previous_id

    ordered: list[str] = []
    revision_id: str | None = current_id
    while revision_id is not None:
        if revision_id in ordered:
            raise eval_articles.EvalArticleError("journal revision history contains a cycle")
        if revision_id not in links:
            raise eval_articles.EvalArticleError(
                f"journal revision history is missing {revision_id}"
            )
        ordered.append(revision_id)
        revision_id = links[revision_id]
    if set(ordered) != set(links):
        raise eval_articles.EvalArticleError(
            "journal revision history contains a disconnected revision"
        )
    return ordered


def render_article(
    article: Mapping[str, Any], *, revisions: Sequence[str] | None = None
) -> str:
    revisions = list(revisions or [article["revision_id"]])
    head = _head(
        title=f"{article['title']} | Palimpsest Live Eval Findings",
        description=article["dek"],
        canonical=_absolute(article["url"]),
        structured=_article_json_ld(article),
        article=article,
    )
    numbers = "".join(
        f"""<li><strong>{_h(number['value'])}</strong><span>{_h(number['label'])}</span><small>{_h(number['note'])}</small><i>{_citation_links(number['citation_ids'])}</i></li>"""
        for number in article["key_numbers"]
    )
    evidence_rows = []
    for row in article["evidence"]:
        source = _safe_url(row["source_url"])
        source_link = (
            f'<a href="{_h(source)}">Open source artifact</a>' if source else "Source URL withheld"
        )
        value = json.dumps(row["value"], ensure_ascii=False, sort_keys=True, allow_nan=False)
        evidence_rows.append(
            f"""<tr id="evidence-{_h(row['evidence_id'])}">
  <td><code>{_h(row['evidence_id'])}</code><span>{_h(row['input_id'])}</span></td>
  <td><strong>{_h(row['label'])}</strong><code>{_h(row['selector'])}</code></td>
  <td><pre>{_h(value)}</pre></td>
  <td><p>{_h(row['interpretation_limit'])}</p>{source_link}</td>
</tr>"""
        )
    methods = "".join(
        f'<li><span>{index:02d}</span><div><h3>{_h(method["step"])}</h3><p>{_h(method["detail"])}</p><small>{_citation_links(method["citation_ids"])}</small></div></li>'
        for index, method in enumerate(article["methodology"], 1)
    )
    gates = "".join(
        f'<li><span aria-hidden="true">✓</span><div><strong>{_h(gate["label"])}</strong><p>{_h(gate["detail"])}</p><code>{_h(gate["gate_id"])}</code></div></li>'
        for gate in article["evaluation_receipt"]["gates"]
    )
    revision_links = "".join(
        f'<li><a href="revisions/{_h(revision)}.json">{_h(revision)}</a>{" <span>current</span>" if revision == article["revision_id"] else ""}</li>'
        for revision in revisions
    )
    return f"""<!doctype html>
<html lang="en"><head>
{head}
</head><body class="ps ej ej--article" data-finding-state="{_h(article['finding_state'])}">
{site_nav.render('/journal/')}
<main id="main">
  <article>
    <header class="ej-article-head ej-wrap">
      <nav aria-label="Breadcrumb"><a href="/journal/">Live Eval Findings</a><span>/</span><span>{_h(article['kicker'])}</span></nav>
      <div class="ej-article-head__grid">
        <div class="ej-article-head__copy">
          <p class="ej-eyebrow">{_h(article['kicker'])}</p>
          <h1>{_h(article['title'])}</h1>
          <p class="ej-article-head__dek">{_h(article['dek'])}</p>
          <p class="ej-byline"><strong>{_h(article['authorship']['byline'])}</strong><span>Published <time datetime="{_h(article['published_at'])}">{_h(_time_label(article['published_at']))}</time></span><span>Updated <time datetime="{_h(article['updated_at'])}">{_h(_time_label(article['updated_at']))}</time></span></p>
        </div>
        <aside class="ej-article-head__receipt" aria-label="Article receipt">
          <p>Publication receipt</p>
          <dl><dt>State</dt><dd>{_h(article['finding_state'])}</dd><dt>Citation coverage</dt><dd>{_h(article['evaluation_receipt']['citation_coverage'] * 100)}%</dd><dt>Sealed runs</dt><dd>{_h(article['evaluation_receipt']['sealed_run_count'])}</dd><dt>Revision</dt><dd><code>{_h(article['revision_id'])}</code></dd></dl>
          <a href="article.json">Current structured article</a>
        </aside>
      </div>
      <div class="ej-thesis"><span>Thesis</span><p>{_h(article['thesis'])}</p></div>
      <ul class="ej-key-numbers" aria-label="Key figures">{numbers}</ul>
    </header>

    <div class="ej-article-body ej-wrap">
      <aside class="ej-article-index">
        <p>In this analysis</p>
        <ol>{''.join(f'<li><a href="#{_h(section["section_id"])}">{index:02d} {_h(section["heading"])}</a></li>' for index, section in enumerate(article['sections'], 1))}</ol>
        <a href="#evidence">Evidence receipts</a>
      </aside>
      <div class="ej-article-prose">{_render_sections(article)}</div>
    </div>

    <section class="ej-adversarial ej-wrap" aria-label="Adversarial reading">
      {_render_records('The strongest counterread', article['counterreadings'], 'ej-record--counter')}
      {_render_records('What this cannot establish', article['limitations'], 'ej-record--limits')}
    </section>

    <section class="ej-method ej-wrap" aria-labelledby="method-heading">
      <div class="ej-section-head"><p>Reproduce it</p><h2 id="method-heading">From frozen prompts to a public claim</h2></div>
      <ol>{methods}</ol>
      <div class="ej-command"><span>Verify the chain offline</span><code>python3 scripts/verify_eval_registry.py</code></div>
    </section>

    <section class="ej-evidence ej-wrap" id="evidence" aria-labelledby="evidence-heading">
      <div class="ej-section-head"><p>Evidence ledger</p><h2 id="evidence-heading">Every cited value and its limit</h2><a href="article.json">Article JSON</a></div>
      <div class="ej-table-wrap" role="region" tabindex="0" aria-label="Article evidence receipts"><table><thead><tr><th>Receipt</th><th>Source selector</th><th>Exact value</th><th>Interpretation limit</th></tr></thead><tbody>{''.join(evidence_rows)}</tbody></table></div>
    </section>

    <section class="ej-gates ej-wrap" aria-labelledby="gates-heading">
      <div class="ej-section-head"><p>Quality gate</p><h2 id="gates-heading">Why this article was allowed to publish</h2></div>
      <ul>{gates}</ul>
    </section>

    <section class="ej-revisions ej-wrap" aria-labelledby="revisions-heading">
      <div><p class="ej-kicker">Correction record</p><h2 id="revisions-heading">The current head and every preserved revision</h2><p>A later eval may update this living analysis. Prior structured revisions remain addressable and the current head points back to the one it replaced.</p></div>
      <ul>{revision_links}</ul>
    </section>

    <footer class="ej-disclosure ej-wrap"><span>Machine authorship boundary</span><p>{_h(article['disclosure'])}</p></footer>
  </article>
</main>
<footer class="ej-footer"><div class="ej-wrap"><a href="/journal/">← Live Eval Findings</a><p>Public evidence. Visible limits. Offline verification.</p><a href="/readings/eval-registry.html">Open the Registry</a></div></footer>
{site_nav.FOOT}
</body></html>
"""


def build_json_feed(collection: Mapping[str, Any]) -> dict[str, Any]:
    items = []
    for article in collection["articles"]:
        text = [article["thesis"]]
        for section in article["sections"]:
            text.append(section["heading"])
            for paragraph in section["paragraphs"]:
                text.append(" ".join(sentence["text"] for sentence in paragraph["sentences"]))
        items.append(
            {
                # A feed item is a published evidence revision, while the URL and
                # article_id remain the stable identity of the recurring analysis.
                # This lets feed readers see every verified run instead of treating
                # a changing canonical page as the same old post forever.
                "id": article["revision_id"],
                "url": _absolute(article["url"]),
                "title": article["title"],
                "summary": article["dek"],
                "content_text": "\n\n".join(text),
                "date_published": article["updated_at"],
                "date_modified": article["updated_at"],
                "authors": [{"name": article["authorship"]["byline"], "url": f"{SITE}/journal/"}],
                "tags": ["AI evaluation", article["finding_state"]],
                "_palimpsest": {
                    "article_id": article["article_id"],
                    "revision_id": article["revision_id"],
                    "finding_state": article["finding_state"],
                    "citation_coverage": article["evaluation_receipt"]["citation_coverage"],
                },
            }
        )
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Palimpsest Live Eval Findings",
        "home_page_url": f"{SITE}/journal/",
        "feed_url": f"{SITE}/journal/feed.json",
        "description": "Dated analysis from sealed AI evaluations, with controls and uncertainty attached.",
        "items": items,
    }


def build_rss(collection: Mapping[str, Any]) -> bytes:
    items = []
    for article in collection["articles"]:
        published = eval_articles._timestamp(article["updated_at"], "updated_at")
        items.append(
            "<item>"
            f"<title>{xml_escape(article['title'])}</title>"
            f"<link>{xml_escape(_absolute(article['url']))}</link>"
            f"<guid isPermaLink=\"false\">{xml_escape(article['revision_id'])}</guid>"
            f"<pubDate>{email.utils.format_datetime(published)}</pubDate>"
            f"<description>{xml_escape(article['dek'])}</description>"
            "</item>"
        )
    raw = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Palimpsest Live Eval Findings</title>"
        f"<link>{SITE}/journal/</link>"
        "<description>Dated analysis from sealed AI evaluations.</description>"
        f"<lastBuildDate>{email.utils.format_datetime(eval_articles._timestamp(collection['generated_at'], 'generated_at'))}</lastBuildDate>"
        + "".join(items)
        + "</channel></rss>"
    )
    return (raw + "\n").encode("utf-8")


def build_sitemap(collection: Mapping[str, Any]) -> bytes:
    rows = [
        f"<url><loc>{SITE}/journal/</loc><lastmod>{xml_escape(collection['generated_at'])}</lastmod><changefreq>daily</changefreq><priority>0.9</priority></url>"
    ]
    rows.extend(
        f"<url><loc>{xml_escape(_absolute(article['url']))}</loc><lastmod>{xml_escape(article['updated_at'])}</lastmod><changefreq>daily</changefreq><priority>0.8</priority></url>"
        for article in collection["articles"]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(rows)
        + "</urlset>\n"
    ).encode("utf-8")


def build_outputs(
    collection: Mapping[str, Any], *, root: Path = ROOT
) -> dict[Path, bytes]:
    eval_articles.validate_collection(collection)
    outputs: dict[Path, bytes] = {
        Path("readings/eval-articles-latest.json"): eval_articles.pretty_json_bytes(collection),
        Path("journal/index.html"): render_index(collection).encode("utf-8"),
        Path("journal/feed.json"): eval_articles.pretty_json_bytes(build_json_feed(collection)),
        Path("journal/feed.xml"): build_rss(collection),
        Path("journal/sitemap.xml"): build_sitemap(collection),
    }
    immutable: list[str] = []
    for article in collection["articles"]:
        base = Path("journal") / article["slug"]
        revisions = _revision_inventory(article, root=root)
        outputs[base / "index.html"] = render_article(article, revisions=revisions).encode("utf-8")
        outputs[base / "article.json"] = eval_articles.pretty_json_bytes(article)
        revision_path = base / "revisions" / f"{article['revision_id']}.json"
        outputs[revision_path] = eval_articles.pretty_json_bytes(article)
        immutable.append(str(revision_path))
    paths = sorted(str(path) for path in outputs)
    paths.append(str(MANIFEST))
    manifest = {
        "schema_version": "palimpsest-eval-journal-manifest.v1",
        "generated_at": collection["generated_at"],
        "paths": sorted(paths),
        "immutable_revision_paths": sorted(immutable),
    }
    outputs[MANIFEST] = eval_articles.pretty_json_bytes(manifest)
    return outputs


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
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_revision(path: Path) -> bool:
    return len(path.parts) == 4 and path.parts[:1] == ("journal",) and path.parts[2] == "revisions" and bool(_REVISION_FILE.fullmatch(path.name))


def publish(outputs: Mapping[Path, bytes], *, root: Path = ROOT) -> tuple[int, int]:
    changed = unchanged = 0
    manifest = outputs.get(MANIFEST)
    if manifest is None:
        raise eval_articles.EvalArticleError("journal output set has no manifest")
    for relative, payload in sorted(outputs.items(), key=lambda item: str(item[0])):
        if relative == MANIFEST:
            continue
        destination = root / relative
        try:
            current = destination.read_bytes()
        except FileNotFoundError:
            current = None
        if current == payload:
            unchanged += 1
            continue
        if current is not None and _is_revision(relative):
            raise eval_articles.EvalArticleError(
                f"refusing to overwrite immutable journal revision: {relative}"
            )
        _atomic_write(destination, payload)
        changed += 1
    destination = root / MANIFEST
    try:
        current_manifest = destination.read_bytes()
    except FileNotFoundError:
        current_manifest = None
    if current_manifest == manifest:
        unchanged += 1
    else:
        _atomic_write(destination, manifest)
        changed += 1
    return changed, unchanged


def check(outputs: Mapping[Path, bytes], *, root: Path = ROOT) -> list[str]:
    drift: list[str] = []
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate and report generated drift")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        collection = eval_articles.build(root=ROOT)
        outputs = build_outputs(collection)
        if args.check:
            drift = check(outputs)
            for item in drift:
                print(item)
            if drift:
                print(f"eval findings drift: {len(drift)} file(s)")
                return 1
            print(f"eval findings current: {collection['n_articles']} articles")
            return 0
        changed, unchanged = publish(outputs)
    except eval_articles.EvalArticleError as exc:
        parser.error(str(exc))
    print(
        f"eval findings -> journal/ · {collection['n_articles']} articles · "
        f"{changed} updated · {unchanged} unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
