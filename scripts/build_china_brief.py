"""Build /china-brief.html — the compact live China state read, published on the site.

A single-glance read of what the censor is working on right now: ranked censor attention,
what is newly sensitive, and how much of that attention is economic. It is the demo's live
view, promoted to a published page so a visitor sees a current reading without cloning
anything.

TWO RULES THIS FILE EXISTS TO HOLD.

1. NEVER FALL BACK TO SYNTHETIC. demo/palimpsest_demo.py drops to its seeded sample when the
   feeds are unreachable, which is right for a local demo and catastrophic for a published
   page: it would put invented censorship events on palimpsest.info under a live timestamp.
   Here an unreachable feed is a hard failure that leaves the previous page untouched and
   exits non-zero, so the workflow goes red and a human looks.

2. THE PAGE STATES ITS OWN AGE. Every reading carries generated_at, and china-brief.json is
   written beside it so freshness is machine-checkable rather than a claim in prose.

Novelty is scored against demo/data/cdt_history.json, which the refresh workflow commits —
without that persisted history every term reads as new on every run.

    PYTHONPATH=. python3 scripts/build_china_brief.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "demo"))

import palimpsest_demo as demo  # noqa: E402  (standalone by design; path set above)

OUT = os.path.join(ROOT, "china-brief.html")
META = os.path.join(ROOT, "readings", "china-brief.json")
CANONICAL = "https://palimpsest.info/china-brief.html"

TITLE = "China brief — live censor attention · Palimpsest"
DESCRIPTION = (
    "A live read of what the Chinese censor is working on right now: ranked censor attention, "
    "newly sensitive terms, and the share of that attention which is economic. Built from the "
    "public China Digital Times feed and refreshed automatically."
)

HEAD = f"""<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{DESCRIPTION}">
<link rel="canonical" href="{CANONICAL}">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Palimpsest">
<meta property="og:title" content="{TITLE}">
<meta property="og:description" content="{DESCRIPTION}">
<meta property="og:url" content="{CANONICAL}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{TITLE}">
<meta name="twitter:description" content="{DESCRIPTION}">"""

NAV = """<nav class="pnav" aria-label="Primary">
  <div class="pnav__links">
    <a href="/">Home</a>
    <a href="/china-brief.html" aria-current="page">Brief</a>
    <a href="/dashboards/ddti_observatory.html">Observatory</a>
    <a href="/dashboards/ddti_dashboard.html">Monitor</a>
    <a href="/readings/erasure-observatory.html">Erasure</a>
    <a href="/readings/eval-registry.html">Eval Registry</a>
    <a href="/readings/generative-firewall-index.html">Firewall</a>
    <a href="/for-researchers.html">Data</a>
    <a href="/support.html">Support</a>
  </div>
</nav>"""

PROVENANCE = """<div class="prov" role="note">
  <p><strong>What this is.</strong> Live public-source measurement, built from the
  <a href="https://chinadigitaltimes.net/">China Digital Times</a> feed of documented
  censorship directives and scrubbed material. Terms are ranked by censor attention and by
  novelty — how recently a term became sensitive — against a persisted history, so a term
  marked new really is new to this instrument.</p>
  <p><strong>What it is not.</strong> This page carries no deletion <em>velocity</em>: that
  component cannot be measured from outside the wall and is suppressed across the platform
  rather than estimated. Attention and novelty are reachable and are what you see here.
  Every figure traces to a public source, and the reading states the moment it was taken.</p>
  <p class="prov__links">Full method in
  <a href="https://github.com/beepboop2025/palimpsest/blob/main/docs/METHODOLOGY.md">METHODOLOGY.md</a>
  · the deeper board is the <a href="/dashboards/ddti_observatory.html">Observatory</a>
  · raw datasets on <a href="/for-researchers.html">the researcher page</a>.</p>
</div>"""

EXTRA_CSS = """
.pnav{position:sticky;top:0;z-index:50;background:rgba(8,8,11,.86);backdrop-filter:blur(9px);
  border-bottom:1px solid rgba(255,255,255,.08);padding:8px clamp(12px,3vw,20px)}
.pnav__links{display:flex;gap:2px;flex-wrap:wrap}
.pnav__links a{color:#8ca0b3;font-size:12px;letter-spacing:.03em;padding:6px 10px;border-radius:7px;
  text-decoration:none;transition:background .15s,color .15s;white-space:nowrap}
.pnav__links a:hover{color:#fff;background:rgba(255,255,255,.07)}
.pnav__links a[aria-current="page"]{color:#06d6e0;background:rgba(6,214,224,.11)}
.prov{margin:18px clamp(12px,3vw,20px);padding:15px 17px;border-radius:12px;
  border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.03);max-width:960px}
.prov p{margin:0 0 9px;font-size:13px;line-height:1.6;color:#b9c2cd}
.prov p:last-child{margin-bottom:0}
.prov strong{color:#e9e4d8}
.prov__links{color:#8a8472;font-size:12.5px}
.prov a{color:#4dd0e1}
table{max-width:100%}
.panel{overflow-x:auto}
@media(max-width:720px){.kpis{grid-template-columns:repeat(2,1fr)}}
"""


def build() -> dict:
    items, reachable = [], 0
    for url in demo.CDT_FEEDS:
        got = demo.fetch_feed(url)
        if got:
            reachable += 1
            items.extend(got)

    articles = demo.parse_articles(items)
    if not articles:
        # Rule 1. The local demo falls back to synthetic here; a published page must not.
        raise SystemExit(
            "FATAL: no China Digital Times articles reachable — refusing to publish. "
            "The previous china-brief.html is left untouched rather than replaced by a "
            "synthetic or empty reading."
        )

    history = demo.load_history()
    ranked = demo.score_terms(articles, history)
    demo.save_history(history, ranked)
    econ = demo.economic_stress(articles)

    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "report.html")
        demo.render_live(ranked, len(articles), reachable, tmp, econ)
        with open(tmp, encoding="utf-8") as fh:
            page = fh.read()

    # The demo owns the markup; we add only the site chrome and the provenance note, so the
    # published page cannot drift from the demo a reader runs locally.
    page = page.replace("<title>Palimpsest · CN</title>", f"<title>{TITLE}</title>{HEAD}", 1)
    page = page.replace("</style>", EXTRA_CSS + "</style>", 1)
    page = page.replace("<body>", "<body>" + NAV, 1)
    page = page.replace("</body>", PROVENANCE + "</body>", 1)

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(page)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "page": "china-brief.html",
        "source": "China Digital Times (public feed)",
        "feeds_reachable": reachable,
        "feeds_total": len(demo.CDT_FEEDS),
        "n_articles": len(articles),
        "n_terms": len(ranked),
        "n_new_terms": sum(1 for r in ranked if r["is_new"]),
        "economic_stress_pct": econ["pct"],
        "top_terms": [{"term": r["term"], "domain": r["domain"],
                       "threat": round(r["threat"], 2), "is_new": bool(r["is_new"])}
                      for r in ranked[:10]],
        "velocity": None,
        "velocity_note": ("not measurable from outside the wall — suppressed, never estimated"),
    }
    os.makedirs(os.path.dirname(META), exist_ok=True)
    with open(META, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return meta


if __name__ == "__main__":
    m = build()
    print(f"china brief -> {OUT}")
    print(f"  {m['n_articles']} articles · {m['n_terms']} terms · {m['n_new_terms']} new · "
          f"econ {m['economic_stress_pct']}% · feeds {m['feeds_reachable']}/{m['feeds_total']}")
