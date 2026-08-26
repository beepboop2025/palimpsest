#!/usr/bin/env python3
"""The site's navigation, defined once.

Palimpsest is an evidence workbench, not a menu of internal project names. The
navigation therefore starts with things a visitor can do: read a finding,
inspect a source index, open a measurement, use a tool, or review the method.

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
    {"label": "Findings", "href": "/journal/"},
    {"label": "Source index", "href": "/news/"},
    {
        "label": "Observatory",
        "lede": "Open a current measurement. Each page shows the result, source receipt, freshness and limit.",
        "match_prefixes": ["/china/", "/dashboards/"],
        "columns": [
            {
                "head": "Current results",
                "links": [
                    ("/china/", "China Observatory",
                     "See reporting status and current evidence", "new"),
                    ("/news/china/situation/", "China situation desk",
                     "Combine publisher reports, social context and measurements", "new"),
                    ("/news/china/rumour/", "Public vantages",
                     "Join public streams. Rumour stays context, never proof"),
                    ("/osint-china.html", "Signal board",
                     "Inspect every signal and its freshness"),
                    ("/china-brief.html", "China Brief",
                     "Read the current six-hour measurement brief"),
                    ("/weekly-situation.html", "Weekly situation report",
                     "Sealed multi-layer fusion of what the censor is working on", "new"),
                    ("/news/china/analysis/", "Censorship analysis",
                     "Open the latest cross-instrument result"),
                ],
            },
            {
                "head": "Inspect the record",
                "links": [
                    ("/china/sources/", "Source ledger",
                     "Check access, rights, readiness and limits"),
                    ("/china/releases/", "Release monitors",
                     "Check publication clocks and revisions"),
                    ("/news/china/erasure/", "Find a deleted post",
                     "Open the evidence trail, export and cite", "new"),
                    ("/status.html", "Collector health",
                     "Last successful seal, and every abstention"),
                    ("/dashboards/ddti_dashboard.html", "Live deletion monitor",
                     "Open the deletion tracker"),
                ],
            },
        ],
    },
    {
        "label": "BRI regions",
        "lede": "Move from corridor-wide evidence rules into the regional ledgers without treating source discovery as a verified project claim.",
        "match_prefixes": ["/belt-and-road/"],
        "columns": [
            {
                "head": "Belt and Road evidence",
                "links": [
                    ("/belt-and-road/#bri-corridors", "BRI & Corridors",
                     "Open lifecycle rules, economics and global source coverage", "new"),
                    ("/belt-and-road/#balochistan", "Balochistan",
                     "Keep civic, political, armed, legal and rights records separate", "new"),
                    ("/belt-and-road/#pakistan-gwadar", "Pakistan & Gwadar",
                     "Inspect CPEC, port, public-service and local-impact readiness", "new"),
                    ("/belt-and-road/#myanmar", "Myanmar",
                     "Inspect CMEC, Kyaukpyu, pipeline and rail readiness", "new"),
                ],
            },
        ],
    },
    {
        "label": "Tools + feeds",
        "lede": "Go directly to something usable. These links open a tool, data file, feed or verification surface.",
        "columns": [
            {
                "head": "Use a tool",
                "links": [
                    ("/guides/telegram-scam-message-checker/", "Check a scam message",
                     "Open the Telegram checker and privacy guide"),
                    ("/readings/eval-registry.html", "Verify an AI eval",
                     "Inspect preregistrations, run seals and chain roots"),
                    ("/evidence-capsules.html", "Verify a claim offline",
                     "Download a claim with its exact supporting bytes"),
                    ("/cite.html", "Cite a signal",
                     "Build a dataset or day citation from the atlas"),
                    ("/challenge.html", "Challenge a number",
                     "Reproduce the seal and file a method or source error"),
                    ("/forecast-scorecard.html", "Forecast scorecard",
                     "Weighted Interval Scores, with the misses kept"),
                ],
            },
            {
                "head": "Subscribe or build",
                "links": [
                    ("/feeds/", "Feeds directory",
                     "Choose the RSS or JSON feed for your task", "new"),
                    ("/evals/", "Eval methods journal",
                     "Read method changes, failures and falsifiers"),
                    ("/data.html", "Download data",
                     "Open every public dataset and licence"),
                    ("/developers.html", "API + MCP",
                     "Call the read-only tools and inspect the contracts"),
                ],
            },
        ],
    },
    {"label": "Method", "href": "/for-researchers.html"},
    {
        "label": "About",
        "lede": "See what changed after feedback, inspect the source, or support the public work.",
        "columns": [
            {
                "head": "Project",
                "links": [
                    ("/updates/2026-08-17-listening-pass/", "What changed after feedback",
                     "Read the criticism-to-change ledger", "new"),
                    ("/fund.html", "Fund the work",
                     "No paywall and no supporter-only evidence"),
                    ("https://github.com/beepboop2025/palimpsest", "Source repository",
                     "Clone, verify and inspect the complete record"),
                ],
            },
        ],
    },
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
    for prefix in item.get("match_prefixes", []):
        if here == prefix.rstrip("/") or here.startswith(prefix.rstrip("/") + "/"):
            return True
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
