#!/usr/bin/env python3
"""Build the evidence-bound Palimpsest AI Eval Journal.

This renderer has no network dependency.  It validates the authored source files,
binds their local citations to current SHA-256 receipts, and publishes the index,
article pages, per-article JSON, JSON Feed, RSS, sitemap, and one machine-readable
journal edition.  Outputs are assembled before any file is replaced.

    PYTHONPATH=. python -m scripts.build_eval_journal
    PYTHONPATH=. python -m scripts.build_eval_journal --check
"""
from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from xml.sax.saxutils import escape as xml_escape

from core import eval_journal
from scripts import site_nav


ROOT = Path(__file__).resolve().parent.parent
SITE = eval_journal.SITE
PUBLISHER = "Palimpsest Eval Lab"
OG_IMAGE = f"{SITE}/brand/palimpsest-og2.png"


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace(
        "<", "\\u003c"
    )


def _human_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.strftime("%d %B %Y · %H:%M UTC")


def _rfc2822(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return email.utils.format_datetime(parsed)


def _organization() -> dict[str, Any]:
    return {
        "@type": "Organization",
        "@id": f"{SITE}/#organization",
        "name": "Palimpsest",
        "url": f"{SITE}/",
        "logo": {
            "@type": "ImageObject",
            "url": f"{SITE}/brand/palimpsest-icon-512.png",
            "width": 512,
            "height": 512,
        },
    }


def _head(
    *,
    title: str,
    description: str,
    canonical: str,
    page_type: str,
    json_ld: Mapping[str, Any],
    published_at: str | None = None,
    modified_at: str | None = None,
) -> str:
    article_meta = ""
    if published_at:
        article_meta += f'<meta property="article:published_time" content="{_h(published_at)}">\n'
    if modified_at:
        article_meta += f'<meta property="article:modified_time" content="{_h(modified_at)}">\n'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)}</title>
<meta name="description" content="{_h(description)}">
<meta name="author" content="{_h(PUBLISHER)}">
<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">
<link rel="canonical" href="{_h(canonical)}">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<link rel="alternate" type="application/feed+json" title="Palimpsest AI Eval Journal JSON Feed" href="/evals/feed.json">
<link rel="alternate" type="application/rss+xml" title="Palimpsest AI Eval Journal RSS" href="/evals/feed.xml">
<meta name="theme-color" content="#0a1018">
<meta property="og:type" content="{_h(page_type)}">
<meta property="og:site_name" content="Palimpsest AI Eval Journal">
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
<link rel="stylesheet" href="/assets/eval-journal.css">
</head>"""


def _context(context: Mapping[str, Any], *, compact: bool = False) -> str:
    compact_class = " ej-context--compact" if compact else ""
    return f"""<aside class="ej-context{compact_class}" aria-label="Current evidence state">
  <p>{_h(context['label'])}</p>
  <strong>{_h(context['value'])}</strong>
  <span>{_h(context['detail'])}</span>
  <a href="{_h(context['url'])}">Inspect the live artifact →</a>
</aside>"""


def _article_text(article: Mapping[str, Any]) -> str:
    chunks = [article["title"], article["dek"], f"Claim boundary: {article['claim']}"]
    for section in article["sections"]:
        chunks.append(section["heading"])
        chunks.extend(section["paragraphs"])
        chunks.extend(f"- {point}" for point in section["points"])
    chunks.append("Limitations")
    chunks.extend(f"- {item}" for item in article["limitations"])
    chunks.extend(["What would change the claim", article["falsifier"]])
    return "\n\n".join(chunks)


def _index_json_ld(journal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@graph": [
            _organization(),
            {
                "@type": "CollectionPage",
                "@id": journal["home_page_url"],
                "url": journal["home_page_url"],
                "name": journal["title"],
                "description": journal["description"],
                "dateModified": journal["generated_at"],
                "publisher": {"@id": f"{SITE}/#organization"},
                "mainEntity": {
                    "@type": "ItemList",
                    "numberOfItems": journal["n_articles"],
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": index,
                            "url": article["url"],
                            "name": article["title"],
                        }
                        for index, article in enumerate(journal["articles"], 1)
                    ],
                },
            },
        ],
    }


def _article_json_ld(article: Mapping[str, Any]) -> dict[str, Any]:
    author_type = "Person" if article["author"] == "Palimpsest's founder" else "Organization"
    citations = [f"{SITE}{item['url']}" for item in article["evidence"]]
    citations.extend(item["url"] for item in article["external_sources"])
    return {
        "@context": "https://schema.org",
        "@graph": [
            _organization(),
            {
                "@type": "Article",
                "@id": article["url"],
                "url": article["url"],
                "headline": article["title"],
                "description": article["dek"],
                "articleSection": "AI evaluation",
                "datePublished": article["published_at"],
                "dateModified": article["modified_at"],
                "author": {"@type": author_type, "name": article["author"]},
                "publisher": {"@id": f"{SITE}/#organization"},
                "isPartOf": {"@id": f"{SITE}/evals/"},
                "mainEntityOfPage": article["url"],
                "citation": citations,
                "keywords": [
                    "AI evaluations",
                    "LLM censorship",
                    "China censorship",
                    "evaluation integrity",
                    "reproducibility",
                ],
            },
        ],
    }


def _card(article: Mapping[str, Any]) -> str:
    return f"""<article class="ej-card">
  <p class="ej-card__meta">{_h(article['kind'])} <span>·</span> {_h(article['status'])}</p>
  <h2><a href="/evals/{_h(article['slug'])}/">{_h(article['title'])}</a></h2>
  <p class="ej-card__dek">{_h(article['dek'])}</p>
  <div class="ej-card__foot">
    <span>{len(article['evidence'])} evidence receipts</span>
    <code>sha256:{_h(article['content_sha256'][:12])}</code>
  </div>
</article>"""


def render_index(journal: Mapping[str, Any]) -> str:
    articles = list(journal["articles"])
    lead = next(
        article for article in articles if article["slug"] == "a-censored-answer-is-not-evidence"
    )
    cards = "\n".join(_card(article) for article in articles if article is not lead)
    body = f"""<body class="ps eval-journal-page">
{site_nav.render('/evals/')}
<main id="main">
  <header class="ej-mast ej-shell">
    <div class="ej-mast__line">
      <p class="ej-mark">Palimpsest <span>/ AI Eval Journal</span></p>
      <p class="ej-edition">Edition 001<br>{journal['n_articles']} evidence-bound essays<br>Updated {_h(_human_time(journal['generated_at']))}</p>
    </div>
    <p class="ej-mast__dek">Methods, failures, and findings from the lab measuring how language models refuse, erase, or reframe contested information.</p>
  </header>

  <aside class="ej-live-desk" aria-labelledby="live-desk-title">
    <div class="ej-shell ej-live-desk__grid">
      <div>
        <p class="ej-kicker ej-kicker--light">Continuously verified edition</p>
        <h2 id="live-desk-title">Read what the eval registry can prove today</h2>
      </div>
      <div>
        <p>The live findings desk rebuilds after verified panel publications. It separates control health, uncertainty, model-panel changes, and registry provenance into evidence-linked analyses.</p>
        <div class="ej-live-desk__actions">
          <a href="/journal/">Open live findings <span aria-hidden="true">→</span></a>
          <a href="/readings/eval-articles-latest.json">Structured current edition</a>
          <a href="/readings/eval-registry.html">Inspect the registry</a>
        </div>
      </div>
    </div>
  </aside>

  <section class="ej-trace" aria-label="Palimpsest evaluation trace">
    <div class="ej-shell ej-trace__grid">
      <div><span>01</span><strong>Prompt</strong><p>Freeze the exact question and comparison before the answer exists.</p></div>
      <div><span>02</span><strong>Discrepancy</strong><p>Measure refusal, substitution, asymmetry, controls, and uncertainty.</p></div>
      <div><span>03</span><strong>Proof</strong><p>Publish the response bytes, seals, limits, and the test that could fail.</p></div>
    </div>
  </section>

  <section class="ej-lead ej-shell" aria-labelledby="lead-title">
    <div class="ej-lead__copy">
      <p class="ej-kicker">{_h(lead['kind'])} · {_h(lead['status'])}</p>
      <h1 id="lead-title"><a href="/evals/{_h(lead['slug'])}/">{_h(lead['title'])}</a></h1>
      <p class="ej-lead__dek">{_h(lead['dek'])}</p>
      <p class="ej-lead__claim"><strong>Claim boundary</strong>{_h(lead['claim'])}</p>
      <a class="ej-open" href="/evals/{_h(lead['slug'])}/">Read the founder's note <span>→</span></a>
    </div>
    {_context(lead['live_context'])}
  </section>

  <section class="ej-articles ej-shell" aria-labelledby="articles-title">
    <div class="ej-section-head">
      <div><p class="ej-kicker">Launch file</p><h2 id="articles-title">What changed in the eval engine</h2></div>
      <p>Each article is built from a closed source record and refuses publication if a cited local artifact is missing. The receipts update when the evidence bytes change.</p>
    </div>
    <div class="ej-grid">{cards}</div>
  </section>

  <section class="ej-contract" aria-labelledby="contract-title">
    <div class="ej-shell ej-contract__grid">
      <div><p class="ej-kicker ej-kicker--light">Publication contract</p><h2 id="contract-title">No essay without an exit condition.</h2></div>
      <ol>
        <li><strong>Scoped claim</strong><span>What this article says—and the broader claim it refuses to make.</span></li>
        <li><strong>Exact evidence</strong><span>Current file path, byte count, and SHA-256 receipt for every local source.</span></li>
        <li><strong>Visible limits</strong><span>Known measurement and interpretation boundaries stay next to the conclusion.</span></li>
        <li><strong>Falsifier</strong><span>The condition that would lower, reverse, or retire the claim.</span></li>
      </ol>
    </div>
  </section>

  <div class="ej-feed ej-shell"><span>Follow the methods desk</span><a href="/evals/feed.xml">RSS</a><a href="/evals/feed.json">JSON Feed</a><a href="/readings/eval-journal-latest.json">Structured edition</a></div>
</main>
<footer class="ej-footer"><div class="ej-shell">Palimpsest AI Eval Journal separates editorial explanation from measurement evidence. The live artifacts remain authoritative. <a href="/readings/eval-registry.html">Eval Registry</a> · <a href="/readings/eval-assurance-latest.json">Assurance JSON</a> · <a href="https://github.com/beepboop2025/palimpsest">Source code</a>.</div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title="Palimpsest AI Eval Journal · censorship evals with receipts",
        description=journal["description"],
        canonical=journal["home_page_url"],
        page_type="website",
        modified_at=journal["generated_at"],
        json_ld=_index_json_ld(journal),
    ) + "\n" + body


def _article_sections(article: Mapping[str, Any]) -> str:
    blocks = []
    for section in article["sections"]:
        paragraphs = "".join(f"<p>{_h(value)}</p>" for value in section["paragraphs"])
        points = ""
        if section["points"]:
            points = '<ul class="ej-points">' + "".join(
                f"<li>{_h(value)}</li>" for value in section["points"]
            ) + "</ul>"
        blocks.append(
            f'<section class="ej-prose__section"><h2>{_h(section["heading"])}</h2>{paragraphs}{points}</section>'
        )
    return "\n".join(blocks)


def _evidence_receipts(article: Mapping[str, Any]) -> str:
    return "\n".join(
        f"""<li>
  <a href="{_h(item['url'])}">{_h(item['label'])}</a>
  <span>{_h(item['role'])}</span>
  <code>{item['bytes']:,} bytes · sha256:{_h(item['sha256'][:16])}</code>
</li>"""
        for item in article["evidence"]
    )


def render_article(article: Mapping[str, Any]) -> str:
    limitations = "".join(f"<li>{_h(item)}</li>" for item in article["limitations"])
    externals = ""
    if article["external_sources"]:
        items = "".join(
            f'<li><a href="{_h(item["url"])}" rel="external noopener">{_h(item["title"])}</a><span>{_h(item["relationship"])}</span></li>'
            for item in article["external_sources"]
        )
        externals = f"""<section class="ej-sources" aria-labelledby="external-title">
  <p class="ej-kicker">Related research</p><h2 id="external-title">Context, not borrowed proof</h2><ul>{items}</ul>
</section>"""
    externals_markup = f"        {externals}" if externals else ""
    commands = "".join(f"<li><code>{_h(command)}</code></li>" for command in article["verification"])
    body = f"""<body class="ps eval-journal-page eval-article-page">
{site_nav.render('/evals/')}
<main id="main" class="ej-shell">
  <article class="ej-article">
    <header class="ej-article__header">
      <p class="ej-breadcrumb"><a href="/evals/">AI Eval Journal</a> / {_h(article['kind'])}</p>
      <p class="ej-kicker">{_h(article['status'])}</p>
      <h1>{_h(article['title'])}</h1>
      <p class="ej-article__dek">{_h(article['dek'])}</p>
      <div class="ej-article__meta"><span>By {_h(article['author'])}</span><time datetime="{_h(article['published_at'])}">{_h(_human_time(article['published_at']))}</time><span>{len(article['evidence'])} evidence receipts</span><a href="article.json">Article JSON</a></div>
    </header>

    <aside class="ej-claim" aria-label="Claim boundary"><span>Claim boundary</span><p>{_h(article['claim'])}</p></aside>

    <div class="ej-article__layout">
      <div class="ej-prose">
        {_article_sections(article)}
        <section class="ej-limits" aria-labelledby="limits-title">
          <p class="ej-kicker">Limits carried with the claim</p><h2 id="limits-title">What this does not establish</h2><ul>{limitations}</ul>
        </section>
        <section class="ej-falsifier" aria-labelledby="falsifier-title">
          <p class="ej-kicker ej-kicker--light">Exit condition</p><h2 id="falsifier-title">What would change the claim</h2><p>{_h(article['falsifier'])}</p>
        </section>
{externals_markup}
        <section class="ej-verify" aria-labelledby="verify-title"><p class="ej-kicker">Reproduce it locally</p><h2 id="verify-title">Verification commands</h2><ol>{commands}</ol></section>
      </div>
      <aside class="ej-rail" aria-label="Evidence receipts">
        {_context(article['live_context'], compact=True)}
        <div class="ej-receipts"><p class="ej-kicker">Bound evidence</p><ol>{_evidence_receipts(article)}</ol><p class="ej-receipts__digest">Article record<br><code>sha256:{_h(article['content_sha256'])}</code></p></div>
      </aside>
    </div>
  </article>
</main>
<footer class="ej-footer"><div class="ej-shell"><a href="/evals/">← AI Eval Journal</a> · <a href="/readings/eval-registry.html">Eval Registry</a> · <a href="/readings/eval-assurance-latest.json">Live assurance</a> · <a href="/fund.html">Fund the next evidence gate</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title=f"{article['title']} · Palimpsest AI Eval Journal",
        description=article["dek"],
        canonical=article["url"],
        page_type="article",
        published_at=article["published_at"],
        modified_at=article["modified_at"],
        json_ld=_article_json_ld(article),
    ) + "\n" + body


def build_json_feed(journal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": journal["title"],
        "home_page_url": journal["home_page_url"],
        "feed_url": journal["feed_url"],
        "description": journal["description"],
        "language": "en",
        "authors": [{"name": PUBLISHER, "url": f"{SITE}/evals/"}],
        "items": [
            {
                "id": article["content_sha256"],
                "url": article["url"],
                "title": "[Palimpsest method article] " + article["title"],
                "summary": "Palimpsest evaluation method article. " + article["dek"],
                "content_text": _article_text(article),
                "date_published": article["published_at"],
                "date_modified": article["modified_at"],
                "authors": [{"name": article["author"]}],
                "tags": ["AI evaluations", article["kind"], article["status"]],
                "attachments": [
                    {
                        "url": f"{SITE}{receipt['url']}",
                        "mime_type": "application/json" if receipt["path"].endswith(".json") else "text/plain",
                        "title": receipt["label"],
                        "size_in_bytes": receipt["bytes"],
                    }
                    for receipt in article["evidence"]
                ],
                "_palimpsest": {
                    "kind": "eval_method_article",
                    "schema": article["schema"],
                    "claim": article["claim"],
                    "falsifier": article["falsifier"],
                    "content_sha256": article["content_sha256"],
                    "article_json": article["json_url"],
                },
            }
            for article in journal["articles"]
        ],
    }


def build_rss(journal: Mapping[str, Any]) -> bytes:
    items = []
    for article in journal["articles"]:
        text = _article_text(article)
        items.append(
            f"""  <item>
    <guid isPermaLink="false">urn:sha256:{article['content_sha256']}</guid>
    <title>{xml_escape('[Palimpsest method article] ' + article['title'])}</title>
    <link>{xml_escape(article['url'])}</link>
    <pubDate>{_rfc2822(article['published_at'])}</pubDate>
    <description>{xml_escape('Palimpsest evaluation method article. ' + article['dek'])}</description>
    <category>palimpsest-eval-method</category>
    <content:encoded><![CDATA[{text.replace(']]>', ']]]]><![CDATA[>')}]]></content:encoded>
  </item>"""
        )
    value = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>{xml_escape(journal['title'])}</title>
  <link>{xml_escape(journal['home_page_url'])}</link>
  <description>{xml_escape(journal['description'])}</description>
  <language>en</language>
  <lastBuildDate>{_rfc2822(journal['generated_at'])}</lastBuildDate>
  <atom:link href="{SITE}/evals/feed.xml" rel="self" type="application/rss+xml" />
{os.linesep.join(items)}
</channel>
</rss>
"""
    return value.encode("utf-8")


def build_sitemap(journal: Mapping[str, Any]) -> bytes:
    urls = [
        f"  <url><loc>{SITE}/evals/</loc><lastmod>{journal['generated_at']}</lastmod><changefreq>weekly</changefreq><priority>1.0</priority></url>"
    ]
    urls.extend(
        f"  <url><loc>{article['url']}</loc><lastmod>{article['modified_at']}</lastmod><changefreq>monthly</changefreq><priority>0.9</priority></url>"
        for article in journal["articles"]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    ).encode("utf-8")


def build_outputs(journal: Mapping[str, Any]) -> dict[Path, bytes]:
    outputs: dict[Path, bytes] = {
        Path("readings/eval-journal-latest.json"): eval_journal.encode_json(journal),
        Path("evals/index.html"): render_index(journal).encode("utf-8"),
        Path("evals/feed.json"): eval_journal.encode_json(build_json_feed(journal)),
        Path("evals/feed.xml"): build_rss(journal),
        Path("evals/sitemap.xml"): build_sitemap(journal),
    }
    for article in journal["articles"]:
        base = Path("evals") / article["slug"]
        outputs[base / "index.html"] = render_article(article).encode("utf-8")
        outputs[base / "article.json"] = eval_journal.encode_json(article)
    return outputs


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def publish(outputs: Mapping[Path, bytes], *, root: Path = ROOT) -> int:
    changed = 0
    for relative, payload in outputs.items():
        destination = root / relative
        if destination.exists() and destination.read_bytes() == payload:
            continue
        _atomic_write(destination, payload)
        changed += 1
    return changed


def check(outputs: Mapping[Path, bytes], *, root: Path = ROOT) -> list[str]:
    stale = []
    for relative, payload in outputs.items():
        destination = root / relative
        if not destination.exists() or destination.read_bytes() != payload:
            stale.append(str(relative))
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    journal = eval_journal.build_journal(ROOT)
    outputs = build_outputs(journal)
    if args.check:
        stale = check(outputs)
        if stale:
            print("stale eval-journal outputs:\n  " + "\n  ".join(stale))
            return 1
        print(f"eval journal is current · {journal['n_articles']} articles")
        return 0
    changed = publish(outputs)
    print(
        f"eval journal -> evals/ · {journal['n_articles']} articles · "
        f"{changed} files changed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
