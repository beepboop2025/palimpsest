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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "demo"))

import palimpsest_demo as demo  # noqa: E402  (standalone by design; path set above)

# The site's navigation and shell tags, defined once in scripts/site_nav.py. This page is
# rewritten wholesale every six hours, so a nav hand-copied into this file would quietly
# revert the rest of the site's chrome on the next cron run. Import it instead.
import site_nav  # noqa: E402

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

PROVENANCE = """<div class="prov ps-p1" role="note">
  <p><strong>What this is.</strong> Live public-source measurement, built from the
  <a href="https://chinadigitaltimes.net/">China Digital Times</a> feed of documented
  censorship directives and scrubbed material. Terms are ranked by censor attention and by
  novelty — how recently a term became sensitive — against a persisted history, so a term
  marked new really is new to this instrument.</p>
  <p><strong>What it is not.</strong> This page carries no deletion <em>velocity</em>: that
  component cannot be measured from outside the wall and is suppressed across the platform
  rather than estimated. Attention and novelty are reachable and are what you see here.
  Every figure traces to a public source, and the reading states the moment it was taken.</p>
  <p><strong>Sibling project.</strong> <a href="https://seiche.info/">Seiche</a> is this page's
  mirror image: a free funding-stress terminal for US money markets, possible because the Fed
  publishes its plumbing every week. Palimpsest works the opposite case, a state that curates
  what may be known. Both read the same China money-market benchmarks, collected keyless by
  this project and consumed by Seiche's CHINA row.</p>
  <p class="prov__links">Full method in
  <a href="https://github.com/beepboop2025/palimpsest/blob/main/docs/METHODOLOGY.md">METHODOLOGY.md</a>
  · the deeper board is the <a href="/dashboards/ddti_observatory.html">Observatory</a>
  · raw datasets on <a href="/for-researchers.html">the researcher page</a>.</p>
</div>"""

# Page-specific only. Surfaces come from the shell's plane classes (ps-p3 reading,
# ps-p2 method, ps-p1 evidence), which are linked AFTER this block so they win the
# tie against the demo's own .panel background — see build().
EXTRA_CSS = """
.prov{margin:18px clamp(12px,3vw,20px) 26px;padding:15px 17px;max-width:960px}
.prov p{margin:0 0 9px;font-size:13px;line-height:1.6;color:#b9c2cd}
.prov p:last-child{margin-bottom:0}
.prov strong{color:#e9e4d8}
.prov__links{color:#8a8472;font-size:12.5px}
.prov a{color:#4dd0e1}
table{max-width:100%}
.panel{overflow-x:auto}
.ps-p1>h2,.ps-p2>h2,.ps-p3>h2{background:transparent}
@media(max-width:720px){.kpis{grid-template-columns:repeat(2,1fr)}}
"""


def _swap(page: str, old: str, new: str, *, what: str, required: bool = True) -> str:
    """Make one anchored edit to the demo's markup, and be loud when the anchor is gone.

    demo/palimpsest_demo.py owns this page's HTML, so every hook below is a literal string
    from that file. A silent no-op would publish a brief with no navigation, or with the
    depth grammar missing, and nothing would flag it. Structural hooks (nav, shell, main)
    are required and stop the run; the presentation-only ones warn and continue, because a
    brief that publishes one plane class short still carries a true reading.
    """
    if old not in page:
        msg = f"demo markup drifted — the {what} anchor is no longer in the rendered page"
        if required:
            raise SystemExit(f"FATAL: {msg}. Refusing to publish a page without its shell.")
        print(f"WARNING: {msg}; depth class not applied.", file=sys.stderr)
        return page
    return page.replace(old, new, 1)


def _canonicalise_history() -> bool:
    """Rewrite the novelty baseline to the only part of it that is load-bearing.

    demo.save_history() restamps `last_seen` on EVERY term on EVERY run. That is harmless for
    a local demo and pathological for a committed file: this baseline is tracked so novelty
    survives across runs, so a 6-hourly refresh would commit a 134-line pure-timestamp diff
    four times a day, forever, burying the one event that actually matters — a term appearing
    for the first time.

    Neither timestamp is ever read. demo.score_terms decides `is_new` from `term not in
    history`, i.e. from the presence of the KEY alone; `first_seen` and `last_seen` are
    write-only. So the committed form keeps sorted keys and `first_seen` (cheap, stable, and
    genuinely informative) and drops `last_seen`, which was pure churn. The file now changes
    only when the term set changes, which is exactly the signal.

    Returns True if the file changed on disk.
    """
    path = demo.HISTORY_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
    except OSError:
        before = ""
    try:
        history = json.loads(before) if before else {}
    except json.JSONDecodeError:
        history = {}

    canonical = {
        term: {"first_seen": meta.get("first_seen")}
        for term, meta in sorted(history.items())
        if isinstance(meta, dict)
    }
    text = json.dumps(canonical, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if text == before:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return True


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
    baseline_changed = _canonicalise_history()
    econ = demo.economic_stress(articles)

    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "report.html")
        demo.render_live(ranked, len(articles), reachable, tmp, econ)
        with open(tmp, encoding="utf-8") as fh:
            page = fh.read()

    # The demo owns the markup; we add only the site chrome, the depth classes and the
    # provenance note, so the published page cannot drift from the demo a reader runs locally.
    page = _swap(page, "<title>Palimpsest · CN</title>", f"<title>{TITLE}</title>{HEAD}",
                 what="title")
    page = _swap(page, "</style>", EXTRA_CSS + "</style>", what="page stylesheet")
    # tikto.css + shell.css are linked AFTER the demo's inline <style>, deliberately. The
    # demo gives .panel an opaque background at one class of specificity, the plane classes
    # are also one class, and the later sheet wins that tie — so this ordering is what lets
    # the depth grammar below actually reach the surface it names.
    page = _swap(page, "</head>", site_nav.HEAD + "\n</head>", what="shell stylesheet links")
    page = _swap(page, "<body>",
                 '<body class="ps">\n' + site_nav.render("/china-brief.html") + '\n<main id="main">',
                 what="body open")
    page = _swap(page, "<footer>", PROVENANCE + "</main>\n<footer>", what="footer")
    page = _swap(page, "</body>", site_nav.FOOT + "</body>", what="body close")

    # Depth encodes epistemic distance. The state read is the sentence a reader may quote,
    # so it sits nearest the eye (p3); the two overview panels are the components that
    # produced it (p2); the full ranked table and the signal bars are the receipts (p1).
    page = _swap(page, "<div class=stateread>", '<div class="stateread ps-p3">',
                 what="state read", required=False)
    for anchor, plane, what in (
        ("<div class=panel><h2><span class='dot r'></span>Censor attention",
         "ps-p2", "censor-attention panel"),
        ("<div class=panel><h2><span class='dot t'></span>Economic stress",
         "ps-p2", "economic-stress panel"),
        ("<div class=panel><h2><span class='dot r'></span>DDTI",
         "ps-p1", "DDTI table"),
        ("<div class=panel><h2><span class='dot t'></span>Surging topics",
         "ps-p1", "surging-topics panel"),
        ("<div class=panel><h2><span class='dot g'></span>Attention by domain",
         "ps-p1", "attention-by-domain panel"),
    ):
        page = _swap(page, anchor,
                     anchor.replace("<div class=panel>", f'<div class="panel {plane}">'),
                     what=what, required=False)

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
        "novelty_baseline_changed": baseline_changed,
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
