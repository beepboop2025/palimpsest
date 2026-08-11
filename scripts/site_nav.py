#!/usr/bin/env python3
"""The site's navigation, defined once.

Palimpsest is a publication plus three evidence surfaces, and the navigation
has to say so on every page rather than making readers reverse-engineer it:

  1. an evidence-linked newsroom          (readers, journalists, researchers)
  2. a China censorship observatory       (researchers, journalists, OSINT)
  3. a verifiable AI evaluation registry  (labs, eval and safety researchers)
  4. a funded public good                 (grantmakers and individual donors)

Before this module there were five different hand-maintained navs across twelve
pages and four pages with no nav at all, because every page carried its own copy.
Now the structure lives here, `sync_nav.py` stamps it between the PS_NAV markers
on every page, and CI fails if any page has drifted.

The markup is real `<a href>` — never JS-injected. Palimpsest's whole reach
strategy is being cited by crawlers and AI agents, plenty of which do not run
JavaScript, and a JS-built nav would hide twenty signal pages from exactly the
readers the project wants.

Import `render(current)` from a page generator, or run sync_nav.py over static
files. Both paths use this one definition.
"""
from __future__ import annotations

import html as _html

BEGIN = "<!--PS_NAV-->"
END = "<!--/PS_NAV-->"

# `new` marks a signal that shipped recently enough to be worth pointing at.
# It is a hand-maintained editorial claim, not a timestamp — clear it when it
# stops being true rather than letting it rot into furniture.
NAV = [
    {"label": "News", "href": "/news/"},
    {
        "label": "Censorship",
        "lede": "What the apparatus removed, measured at the network, content and model layers.",
        "columns": [
            {
                "head": "Boards",
                "links": [
                    ("/osint-china.html", "OSINT China",
                     "Every signal, source and freshness state in one public roll-up", "new"),
                    ("/china-brief.html", "China Brief",
                     "The six-hourly read: what moved and what it means"),
                    ("/dashboards/ddti_observatory.html", "DDTI Observatory",
                     "Full command surface: fear index, topic network, ranked targets"),
                    ("/dashboards/ddti_dashboard.html", "Live Monitor",
                     "The deletion tracker on its own"),
                    ("/readings/erasure-observatory.html", "Erasure Observatory",
                     "Composite index across all three layers, sealed each run"),
                ],
            },
            {
                "head": "Signals",
                "links": [
                    ("/readings/inside-view.html", "Inside View",
                     "DNS injection seen from probes inside China", "new"),
                    ("/readings/in-path-interference.html", "In-Path Interference",
                     "Middlebox rewriting and transport health", "new"),
                    ("/readings/app-store.html", "App Store Layer",
                     "What the Chinese storefront will not offer you", "new"),
                    ("/readings/blocklist.html", "Blocklist Archaeology",
                     "The censor's own keyword list, version by version", "new"),
                    ("/readings/ooni-gfw.html", "Great Firewall",
                     "Outside-in blocking rates, OONI and Censored Planet"),
                    ("/readings/bleedthrough.html", "Bleedthrough",
                     "Injector-fleet tomography", "soon"),
                ],
            },
        ],
    },
    {
        "label": "AI Evals",
        "lede": "Evaluations sealed the moment they publish, so no result can be quietly revised.",
        "columns": [
            {
                "head": "The record",
                "links": [
                    ("/readings/eval-registry.html", "Verifiable Eval Registry",
                     "Pre-registered, hash-chained, independently recomputable"),
                    ("/readings/generative-firewall-index.html", "Generative Firewall Index",
                     "What state-aligned models refuse or rewrite"),
                    ("/readings/eval-registry.html#drift", "Refusal Drift",
                     "Frontier models watched for what they quietly stop answering"),
                    ("/readings/eval-registry.html#anchors", "External Anchors",
                     "Chain roots deposited outside our own infrastructure"),
                ],
            },
        ],
    },
    {
        "label": "Method",
        "lede": "Every number on this site is meant to be attacked. Here is what it would take.",
        "columns": [
            {
                "head": "How it works",
                "links": [
                    ("https://github.com/beepboop2025/palimpsest/blob/main/docs/NEW-METHODS.md",
                     "How it is measured", "The estimators, and what each one refuses to claim"),
                    ("https://github.com/beepboop2025/palimpsest/blob/main/docs/INTEGRITY.md",
                     "Integrity model", "What sealing does and does not prove"),
                    ("https://github.com/beepboop2025/palimpsest/blob/main/docs/VALIDATION.md",
                     "Validation", "The DDTI scorer retrodicted against six documented events"),
                    ("https://github.com/beepboop2025/palimpsest/blob/main/docs/ETHICS.md",
                     "Ethics and safety", "Watch the censor, never the censored"),
                    ("/evidence-capsules.html", "Evidence Capsules",
                     "Portable claims bound to exact evidence bytes, verified offline"),
                    ("/for-researchers.html#forecast", "Our own misses",
                     "The forecast ledger, including where we were wrong"),
                ],
            },
        ],
    },
    {"label": "Data", "href": "/data.html"},
    {"label": "Developers", "href": "/developers.html"},
    {"label": "Fund", "href": "/fund.html"},
]


def _is_current(href: str, current: str) -> bool:
    """A link is aria-current when it *is* the page being rendered.

    Deliberately excludes anchored links: on the eval registry page, both
    "Verifiable Eval Registry" and "Refusal Drift" resolve to the same document,
    but marking all three aria-current="page" would have a screen reader
    announce three current pages in one menu. The anchors are destinations
    within the page, not the page itself.
    """
    if not current or href.startswith("http") or "#" in href:
        return False
    return href.rstrip("/") == current.split("#", 1)[0].rstrip("/")


def _esc(s: str) -> str:
    return _html.escape(s, quote=True)


def _link(entry: tuple, current: str) -> str:
    href, title, blurb = entry[0], entry[1], entry[2]
    tag = entry[3] if len(entry) > 3 else None
    cur = ' aria-current="page"' if _is_current(href, current) else ""
    ext = ' target="_blank" rel="noopener"' if href.startswith("http") else ""
    chip = ""
    if tag == "new":
        chip = '<span class="ps-tag">new</span>'
    elif tag == "soon":
        chip = '<span class="ps-tag ps-tag--soon">soon</span>'
    return (
        f'<a href="{_esc(href)}"{cur}{ext}>'
        f"<b>{_esc(title)}{chip}</b>"
        f"<span>{_esc(blurb)}</span></a>"
    )


def _within(item: dict, current: str) -> bool:
    """True when the page being rendered lives inside this flyout, so the
    top-level trigger can show as active even though it is not itself a link.

    Unlike _is_current this DOES count anchored links, because an anchor into
    the current page still means the reader is inside this pillar."""
    if not current:
        return False
    here = current.split("#", 1)[0].rstrip("/")
    for col in item.get("columns", []):
        for entry in col["links"]:
            href = entry[0]
            if href.startswith("http"):
                continue
            if href.split("#", 1)[0].rstrip("/") == here:
                return True
    return False


def render(current: str = "") -> str:
    """Return the full <nav> element for a page at path `current`.

    `current` is a site-absolute path, e.g. "/readings/eval-registry.html".
    """
    out = [
        f"{BEGIN}",
        '<a class="ps-skip" href="#main">Skip to content</a>',
        '<nav class="ps-nav" aria-label="Primary">',
        '  <a class="ps-nav__brand" href="/">'
        '<img src="/brand/palimpsest-icon.svg" width="20" height="20" alt="">PALIMPSEST</a>',
        '  <div class="ps-nav__spacer"></div>',
        '  <div class="ps-nav__items">',
    ]

    for i, item in enumerate(NAV):
        if "href" in item:
            cur = ' aria-current="page"' if _is_current(item["href"], current) else ""
            out.append(
                f'    <div class="ps-nav__item">'
                f'<a class="ps-nav__link" href="{_esc(item["href"])}"{cur}>'
                f'{_esc(item["label"])}</a></div>'
            )
            continue

        pid = f"ps-fly-{i}"
        within = ' data-within=""' if _within(item, current) else ""
        wide = " ps-flyout--wide" if len(item["columns"]) > 1 else ""
        out.append('    <div class="ps-nav__item">')
        out.append(
            f'      <button class="ps-nav__link" type="button" aria-expanded="false" '
            f'aria-controls="{pid}"{within}>{_esc(item["label"])}'
            f'<i class="ps-nav__chev" aria-hidden="true"></i></button>'
        )
        out.append(f'      <div class="ps-flyout{wide}" id="{pid}">')
        if item.get("lede"):
            out.append(f'        <p class="ps-flyout__lede">{_esc(item["lede"])}</p>')
        out.append('        <div class="ps-flyout__cols">')
        for col in item["columns"]:
            out.append('          <div class="ps-flyout__col">')
            if col.get("head"):
                out.append(f'            <p class="ps-flyout__head">{_esc(col["head"])}</p>')
            for entry in col["links"]:
                out.append("            " + _link(entry, current))
            out.append("          </div>")
        out.append("        </div>")
        out.append("      </div>")
        out.append("    </div>")

    out += [
        "  </div>",
        '  <button class="ps-nav__burger" type="button" aria-expanded="false" aria-label="Menu">'
        "<i></i><i></i><i></i></button>",
        '  <div class="ps-nav__scrim" aria-hidden="true"></div>',
        "</nav>",
        f"{END}",
    ]
    return "\n".join(out)


# The <head> block every page needs for the shell to work. Kept here so a page
# generator cannot ship the nav markup without the stylesheet that styles it.
HEAD = (
    '<link rel="stylesheet" href="/dashboards/assets/tikto.css">\n'
    '<link rel="stylesheet" href="/assets/shell.css">'
)
FOOT = '<script src="/assets/shell.js" defer></script>'


if __name__ == "__main__":  # pragma: no cover - manual inspection aid
    import sys
    print(render(sys.argv[1] if len(sys.argv) > 1 else ""))
