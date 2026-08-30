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

import html
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
from core.china_event_lens import (  # noqa: E402
    SOURCE_URL as WEIBO_TERMS_URL,
    build_declared_event_lenses,
)
from core.china_trending_event_lens import (  # noqa: E402
    SCHEMA_VERSION as TREND_LENS_SCHEMA,
    build_trending_event_lenses,
)
from core.safe_fetch import FetchError, safe_fetch_bytes  # noqa: E402
from core.weibo_hotsearch_terms import (  # noqa: E402
    validate_weibo_hotsearch_terms,
)

OUT = os.path.join(ROOT, "china-brief.html")
META = os.path.join(ROOT, "readings", "china-brief.json")
CANONICAL = "https://palimpsest.info/china-brief.html"

TITLE = "China brief — live censor attention · Palimpsest"
DESCRIPTION = (
    "A live read of what the Chinese censor is working on right now: ranked censor attention, "
    "newly sensitive terms, current event-level permitted attention, and the share of censor "
    "attention which is economic. Built from public China Digital Times and Weibo-board archives."
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
<meta property="og:image" content="https://palimpsest.info/brand/og-site.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{TITLE}">
<meta name="twitter:description" content="{DESCRIPTION}">
<meta name="twitter:image" content="https://palimpsest.info/brand/og-site.png">"""

PROVENANCE = """<div class="prov ps-p1" role="note">
  <p><strong>What this is.</strong> Live public-source measurement, built from the
  <a href="https://chinadigitaltimes.net/">China Digital Times</a> feed of documented
  censorship directives and scrubbed material. Terms are ranked by censor attention and by
  novelty — how recently a term became sensitive — against a persisted history, so a term
  marked new really is new to this instrument. The event lenses separately read the public
  Weibo hot-search archive as permitted attention, group revised headlines before testing
  continuity, and keep state-pinned framing, withdrawal watch, DDTI overlap, and optional
  independent-news context distinct.</p>
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
.event-lens{margin:20px clamp(12px,3vw,34px);padding:22px;max-width:1120px;border:1px solid #4dd0e155}
.event-lens__head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px}
.event-lens__eyebrow{margin:0 0 7px;color:#8a8472;font-size:11px;letter-spacing:.14em;text-transform:uppercase}
.event-lens h2{margin:0;color:#f4efe3;font-size:clamp(20px,3vw,31px);line-height:1.15}
.event-lens__state{display:inline-flex;align-items:center;white-space:nowrap;padding:6px 9px;border:1px solid #4dd0e177;color:#4dd0e1;font-size:11px;letter-spacing:.08em}
.event-lens__headline{margin:18px 0 5px;color:#f0cb74;font-size:14px;font-weight:700}
.event-lens__reading{max-width:980px;margin:0;color:#d6d9da;font-size:15px;line-height:1.65}
.event-lens__metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:19px 0}
.event-lens__metric{padding:12px;border:1px solid #ffffff18;background:#05080d66}
.event-lens__metric b{display:block;color:#f4efe3;font-size:20px;line-height:1.2}
.event-lens__metric span{display:block;margin-top:5px;color:#8f9ba7;font-size:11px;line-height:1.35}
.event-lens__evidence{margin:12px 0 0;padding-left:20px;color:#aeb8c2;font-size:12px;line-height:1.55}
.event-lens__evidence li{margin:5px 0}
.event-lens__foot{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:15px;color:#7f8a95;font-size:11px}
.event-lens__foot a{color:#4dd0e1}
.event-lens details{margin-top:13px;color:#8f9ba7;font-size:12px}
.event-lens details p{max-width:960px;line-height:1.55}
.trend-lenses{margin:20px clamp(12px,3vw,34px);padding:22px;max-width:1120px;border:1px solid #ffffff20}
.trend-lenses__head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px}
.trend-lenses__eyebrow{margin:0 0 7px;color:#8a8472;font-size:11px;letter-spacing:.14em;text-transform:uppercase}
.trend-lenses h2{margin:0;color:#f4efe3;font-size:clamp(20px,3vw,29px);line-height:1.15}
.trend-lenses__summary{max-width:960px;margin:12px 0 17px;color:#aeb8c2;font-size:13px;line-height:1.6}
.trend-lenses__state{display:inline-flex;align-items:center;white-space:nowrap;padding:6px 9px;border:1px solid #4dd0e155;color:#4dd0e1;font-size:10px;letter-spacing:.08em}
.trend-lenses__grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.trend-card{min-width:0;padding:16px;border:1px solid #ffffff1c;background:#05080d73}
.trend-card__top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.trend-card__rank{color:#f0cb74;font-size:12px;font-weight:700}
.trend-card__label{color:#4dd0e1;font-size:9px;line-height:1.35;letter-spacing:.06em;text-align:right}
.trend-card h3{margin:9px 0 7px;color:#f4efe3;font-size:17px;line-height:1.35;overflow-wrap:anywhere}
.trend-card__headline{margin:0 0 7px;color:#f0cb74;font-size:12px;font-weight:700}
.trend-card__reading{margin:0;color:#b9c2cd;font-size:12px;line-height:1.55}
.trend-card__metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin:13px 0 0}
.trend-card__metric{padding:8px;border:1px solid #ffffff12}
.trend-card__metric b{display:block;color:#f4efe3;font-size:14px}
.trend-card__metric span{display:block;margin-top:3px;color:#7f8a95;font-size:9px;line-height:1.3}
.trend-card details{margin-top:11px;color:#8f9ba7;font-size:11px}
.trend-card ol{margin:7px 0 0;padding-left:18px;line-height:1.45}
.trend-lenses__foot{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:15px;color:#7f8a95;font-size:11px}
.trend-lenses__foot a{color:#4dd0e1}
table{max-width:100%}
.panel{overflow-x:auto}
.ps-p1>h2,.ps-p2>h2,.ps-p3>h2{background:transparent}
/* The demo's own header/tabnav/pane padding is tuned for a desk. On a phone
   the 34px gutters eat a tenth of the screen, so they tighten here — and the
   ranked rows get room to breathe rather than a horizontal pan. */
@media(max-width:640px){
  header,.tabnav,.pane,footer{padding-left:16px;padding-right:16px}
  .rank{gap:10px}
  .event-lens{box-sizing:border-box;width:calc(100vw - 24px);max-width:calc(100vw - 24px);margin-left:12px;margin-right:12px;padding:17px;overflow-wrap:anywhere}
  .event-lens__head{display:block}
  .event-lens__state{margin-top:12px;white-space:normal}
  .event-lens__metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
  .trend-lenses{box-sizing:border-box;width:calc(100vw - 24px);max-width:calc(100vw - 24px);margin-left:12px;margin-right:12px;padding:17px;overflow-wrap:anywhere}
  .trend-lenses__head{display:block}
  .trend-lenses__state{margin-top:12px;white-space:normal}
  .trend-lenses__grid{grid-template-columns:1fr}
}
"""

EVENT_LENS_JS = r"""<script>
(() => {
  const EVENT_ID = "nepal-flood-tibet-jilong-2026-08";
  const TREND_SCHEMA = "palimpsest.china-trending-event-lenses.v1";
  const safe = (value, limit = 1200) =>
    typeof value === "string" ? value.slice(0, limit) : "";
  const set = (id, value) => {
    const node = document.getElementById(id);
    if (node && (typeof value === "string" || typeof value === "number")) {
      node.textContent = String(value).slice(0, 1200);
    }
  };
  const number = value => Number.isInteger(value) && value >= 0 ? value : "—";
  const element = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (typeof value === "string" || typeof value === "number") {
      node.textContent = String(value).slice(0, 1800);
    }
    return node;
  };
  const metric = (value, label) => {
    const box = element("div", "trend-card__metric");
    box.append(element("b", "", value), element("span", "", label));
    return box;
  };
  const renderDeclared = event => {
    if (!event || typeof event !== "object" || event.event_id !== EVENT_ID) return;
    const assessment = event.assessment || {};
    const attention = event.attention || {};
    const cross = attention.cross_border || {};
    const pins = attention.state_pins || {};
    const watch = event.withdrawal_watch || {};
    const clocks = event.clocks || {};
    set("event-lens-state", assessment.label || "EVENT LENS · UNAVAILABLE");
    set("event-lens-headline", assessment.headline || "No current censorship inference");
    set("event-lens-reading", assessment.reading || "Current event evidence is unavailable.");
    set("event-lens-headlines", number(cross.distinct_headlines));
    set("event-lens-rank", Number.isInteger(cross.best_rank) ? `#${cross.best_rank}` : "—");
    set("event-lens-pins", number(Array.isArray(pins.days) ? pins.days.length : 0));
    const resolved = number(watch.resolved_by_later_attention);
    const rejected = number(watch.ordinary_sense_rejections);
    const unresolved = number(watch.unresolved);
    set("event-lens-withdrawal", `${resolved} resolved · ${rejected} rejected · ${unresolved} open`);
    set("event-lens-clock", clocks.source_generated_at || "unknown source clock");
    const list = document.getElementById("event-lens-evidence");
    if (list && Array.isArray(event.evidence)) {
      const rows = event.evidence.slice(0, 5).filter(row => row && typeof row.title === "string");
      if (rows.length) {
        list.replaceChildren(...rows.map(row => {
          const item = document.createElement("li");
          const rank = Number.isInteger(row.best_rank) ? ` · best rank #${row.best_rank}` : "";
          item.textContent = `${String(row.date || "unknown date").slice(0, 10)} · ${row.title.slice(0, 180)}${rank}`;
          return item;
        }));
      }
    }
  };
  const renderTrendCard = (event, index) => {
    const assessment = event && typeof event.assessment === "object" ? event.assessment : {};
    const attention = event && typeof event.attention === "object" ? event.attention : {};
    const pins = attention && typeof attention.state_pins === "object" ? attention.state_pins : {};
    const watch = event && typeof event.withdrawal_watch === "object" ? event.withdrawal_watch : {};
    const ddti = event && typeof event.ddti_corroboration === "object" ? event.ddti_corroboration : {};
    const card = element("article", "trend-card");
    const top = element("div", "trend-card__top");
    const rank = Number.isInteger(attention.best_rank) ? `#${attention.best_rank}` : `trend ${index + 1}`;
    top.append(
      element("span", "trend-card__rank", rank),
      element("span", "trend-card__label", safe(assessment.label, 80) || "VISIBLE · PERMITTED ATTENTION")
    );
    const title = element("h3", "", safe(event && event.canonical_headline, 180) || "Current headline cluster");
    title.lang = "zh";
    card.append(
      top,
      title,
      element("p", "trend-card__headline", safe(assessment.headline, 220)),
      element("p", "trend-card__reading", safe(assessment.reading, 1600))
    );
    const metrics = element("div", "trend-card__metrics");
    metrics.append(
      metric(number(attention.distinct_headlines), "headline variants"),
      metric(number(Array.isArray(pins.days) ? pins.days.length : 0), "pin days"),
      metric(number(ddti.current_matches), "fresh DDTI overlaps"),
      metric(number(watch.resolved_by_later_attention), "exit flags resolved"),
      metric(number(watch.unresolved), "exit flags open"),
      metric(number(event && event.newswire_context && event.newswire_context.independent_publisher_groups), "wire publisher groups")
    );
    card.append(metrics);
    const evidence = Array.isArray(event && event.evidence)
      ? event.evidence.filter(row => row && typeof row.title === "string").slice(0, 5)
      : [];
    if (evidence.length) {
      const details = document.createElement("details");
      details.append(element("summary", "", "Evidence rows"));
      const list = document.createElement("ol");
      list.append(...evidence.map(row => {
        const item = document.createElement("li");
        const date = safe(row.date, 10) || "undated";
        const kind = safe(row.kind, 40) || "evidence";
        item.textContent = `${date} · ${kind} · ${safe(row.title, 180)}`;
        return item;
      }));
      details.append(list);
      card.append(details);
    }
    return card;
  };
  const renderTrending = source => {
    const lenses = source && source.trending_event_lenses;
    if (!lenses || lenses.schema_version !== TREND_SCHEMA || lenses.status !== "live") return;
    const events = Array.isArray(lenses.events) ? lenses.events.slice(0, 12) : [];
    if (!events.length) return;
    const selection = lenses.selection || {};
    set("trend-lenses-state", (lenses.assessment || {}).label || "TREND LENSES · CURRENT");
    set(
      "trend-lenses-summary",
      `Showing ${events.length} leading clusters from ${number(selection.current_clusters)} current clusters. Each signal remains evidence-bounded; visibility is not uncensored discussion.`
    );
    const grid = document.getElementById("trend-lenses-grid");
    if (grid) grid.replaceChildren(...events.map(renderTrendCard));
    set("trend-lenses-clock", (lenses.clocks || {}).source_generated_at || "unknown");
  };
  fetch("/readings/weibo-hotsearch-latest.json", {cache: "no-store", credentials: "omit"})
    .then(response => response.ok ? response.json() : Promise.reject(new Error("event source unavailable")))
    .then(source => {
      const events = source && source.event_lenses && source.event_lenses.events;
      if (Array.isArray(events)) {
        renderDeclared(events.find(event => event && event.event_id === EVENT_ID));
      }
      renderTrending(source);
    })
    .catch(() => {}); // Preserve the sealed static reading; never replace it with a false zero.
})();
</script>"""


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _load_board_document() -> tuple[dict | None, str]:
    """Prefer the live fixed route and preserve the retrieval state."""

    document = None
    retrieval_state = "unavailable"

    def exact_route(candidate: str) -> None:
        if candidate != WEIBO_TERMS_URL:
            raise FetchError("Weibo event-lens route changed")

    try:
        payload = safe_fetch_bytes(
            WEIBO_TERMS_URL,
            timeout=20,
            max_bytes=2 * 1024 * 1024,
            max_redirects=0,
            headers={
                "Accept": "application/json",
                "User-Agent": "palimpsest.info China event lens (desk@palimpsest.info)",
            },
            url_policy=exact_route,
        )
        document = json.loads(payload.decode("utf-8"))
        validate_weibo_hotsearch_terms(document)
        retrieval_state = "live_fixed_route"
    except (FetchError, UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError):
        local = os.path.join(ROOT, "readings", "weibo-hotsearch-terms-latest.json")
        try:
            with open(local, encoding="utf-8") as fh:
                document = json.load(fh)
            validate_weibo_hotsearch_terms(document)
            retrieval_state = "local_fallback"
        except (OSError, json.JSONDecodeError, ValueError):
            document = None
    return document, retrieval_state


def _load_newswire_document() -> tuple[dict | None, str]:
    """Load optional local wire context without weakening the board gate."""

    path = os.path.join(ROOT, "readings", "newswire-latest.json")
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None, "unavailable"
    return (document, "local_file") if isinstance(document, dict) else (None, "unavailable")


def _build_lens_documents(now: datetime) -> tuple[dict, dict]:
    document, retrieval_state = _load_board_document()
    newswire, newswire_retrieval_state = _load_newswire_document()

    declared = build_declared_event_lenses(document, evaluated_at=now)
    declared["retrieval_state"] = retrieval_state
    trending = build_trending_event_lenses(
        document,
        newswire,
        evaluated_at=now,
    )
    trending["retrieval_state"] = retrieval_state
    trending["newswire_retrieval_state"] = newswire_retrieval_state
    return declared, trending


def _load_event_lenses(now: datetime) -> dict:
    """Compatibility wrapper for the declared case-study lens."""

    declared, _trending = _build_lens_documents(now)
    return declared


def _event_panel(document: dict) -> str:
    events = document.get("events") if isinstance(document, dict) else []
    event = next(
        (
            item
            for item in (events or [])
            if isinstance(item, dict)
            and item.get("event_id") == "nepal-flood-tibet-jilong-2026-08"
        ),
        None,
    )
    if not event:
        event = build_declared_event_lenses(None)["events"][0]

    assessment = event.get("assessment") or {}
    attention = event.get("attention") or {}
    cross = attention.get("cross_border") or {}
    pins = attention.get("state_pins") or {}
    watch = event.get("withdrawal_watch") or {}
    clocks = event.get("clocks") or {}
    evidence = [row for row in (event.get("evidence") or []) if isinstance(row, dict)][:5]

    def count(value: object) -> str:
        return str(value) if type(value) is int and value >= 0 else "—"

    best_rank = cross.get("best_rank")
    rank_text = f"#{best_rank}" if type(best_rank) is int else "—"
    pin_days = pins.get("days") if isinstance(pins.get("days"), list) else []
    withdrawal_text = (
        f"{count(watch.get('resolved_by_later_attention'))} resolved · "
        f"{count(watch.get('ordinary_sense_rejections'))} rejected · "
        f"{count(watch.get('unresolved'))} open"
    )
    evidence_html = "".join(
        "<li>"
        f"{_h(row.get('date') or 'unknown date')} · "
        f"<span lang=zh>{_h(row.get('title') or '')}</span>"
        + (
            f" · best rank #{_h(row['best_rank'])}"
            if type(row.get("best_rank")) is int
            else ""
        )
        + "</li>"
        for row in evidence
    ) or "<li>No current event evidence is publishable.</li>"

    return f"""<section class="event-lens ps-p3" id="event-lens" aria-labelledby="event-lens-title">
  <div class="event-lens__head"><div>
    <p class="event-lens__eyebrow">Declared event lens · Nepal flood / Tibet–Jilong · public board</p>
    <h2 id="event-lens-title">What the censorship instruments indicate</h2>
  </div><span class="event-lens__state" id="event-lens-state">{_h(assessment.get("label") or "EVENT LENS · UNAVAILABLE")}</span></div>
  <p class="event-lens__headline" id="event-lens-headline">{_h(assessment.get("headline") or "No current censorship inference")}</p>
  <p class="event-lens__reading" id="event-lens-reading">{_h(assessment.get("reading") or "Current event evidence is unavailable.")}</p>
  <div class="event-lens__metrics" aria-label="Event-lens evidence summary">
    <div class="event-lens__metric"><b id="event-lens-headlines">{count(cross.get("distinct_headlines"))}</b><span>Nepal-linked distinct headlines in the bounded window</span></div>
    <div class="event-lens__metric"><b id="event-lens-rank">{_h(rank_text)}</b><span>best observed permitted-board rank</span></div>
    <div class="event-lens__metric"><b id="event-lens-pins">{len(pin_days)}</b><span>days with a linked Tibet–Jilong state-pinned headline</span></div>
    <div class="event-lens__metric"><b id="event-lens-withdrawal">{_h(withdrawal_text)}</b><span>exact-headline exit flags: continuity-resolved · sense-rejected · open</span></div>
  </div>
  <ol class="event-lens__evidence" id="event-lens-evidence">{evidence_html}</ol>
  <details><summary>How to read this</summary><p>The hot-search board is a curated permitted-attention surface. High rank and continued presence can contradict a topic-blackout reading, while pinned headlines reveal selected framing. Neither proves free discussion. Headline casualty figures are displayed only as unverified source text.</p></details>
  <div class="event-lens__foot"><span>source clock <b id="event-lens-clock">{_h(clocks.get("source_generated_at") or "unknown")}</b></span><span>retrieval {_h(document.get("retrieval_state") or "embedded")}</span><a href="{_h(WEIBO_TERMS_URL)}">Open every board title and rank</a></div>
</section>"""


def _trending_event_panel(document: dict) -> str:
    """Render the leading current clusters; the JSON retains the wider set."""

    payload = document if isinstance(document, dict) else {}
    assessment = payload.get("assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    selection = payload.get("selection")
    selection = selection if isinstance(selection, dict) else {}
    clocks = payload.get("clocks")
    clocks = clocks if isinstance(clocks, dict) else {}
    events = [
        row
        for row in (payload.get("events") or [])
        if isinstance(row, dict)
    ][:12]

    def count(value: object) -> str:
        return str(value) if type(value) is int and value >= 0 else "—"

    def card(event: dict, index: int) -> str:
        reading = event.get("assessment")
        reading = reading if isinstance(reading, dict) else {}
        attention = event.get("attention")
        attention = attention if isinstance(attention, dict) else {}
        pins = attention.get("state_pins")
        pins = pins if isinstance(pins, dict) else {}
        watch = event.get("withdrawal_watch")
        watch = watch if isinstance(watch, dict) else {}
        ddti = event.get("ddti_corroboration")
        ddti = ddti if isinstance(ddti, dict) else {}
        wire = event.get("newswire_context")
        wire = wire if isinstance(wire, dict) else {}
        best_rank = attention.get("best_rank")
        rank = f"#{best_rank}" if type(best_rank) is int else f"trend {index + 1}"
        evidence = [
            row
            for row in (event.get("evidence") or [])
            if isinstance(row, dict) and isinstance(row.get("title"), str)
        ][:5]
        evidence_html = "".join(
            "<li>"
            f"{_h(row.get('date') or 'undated')} · "
            f"{_h(row.get('kind') or 'evidence')} · "
            f"<span lang=zh>{_h(row.get('title') or '')}</span>"
            "</li>"
            for row in evidence
        )
        details = (
            f"<details><summary>Evidence rows</summary><ol>{evidence_html}</ol></details>"
            if evidence_html
            else ""
        )
        pin_days = pins.get("days") if isinstance(pins.get("days"), list) else []
        return f"""<article class="trend-card">
  <div class="trend-card__top"><span class="trend-card__rank">{_h(rank)}</span><span class="trend-card__label">{_h(reading.get("label") or "VISIBLE · PERMITTED ATTENTION")}</span></div>
  <h3 lang=zh>{_h(event.get("canonical_headline") or "Current headline cluster")}</h3>
  <p class="trend-card__headline">{_h(reading.get("headline") or "")}</p>
  <p class="trend-card__reading">{_h(reading.get("reading") or "")}</p>
  <div class="trend-card__metrics">
    <div class="trend-card__metric"><b>{count(attention.get("distinct_headlines"))}</b><span>headline variants</span></div>
    <div class="trend-card__metric"><b>{len(pin_days)}</b><span>pin days</span></div>
    <div class="trend-card__metric"><b>{count(ddti.get("current_matches"))}</b><span>fresh DDTI overlaps</span></div>
    <div class="trend-card__metric"><b>{count(watch.get("resolved_by_later_attention"))}</b><span>exit flags resolved</span></div>
    <div class="trend-card__metric"><b>{count(watch.get("unresolved"))}</b><span>exit flags open</span></div>
    <div class="trend-card__metric"><b>{count(wire.get("independent_publisher_groups"))}</b><span>wire publisher groups</span></div>
  </div>{details}
</article>"""

    valid = (
        payload.get("schema_version") == TREND_LENS_SCHEMA
        and payload.get("status") == "live"
        and bool(events)
    )
    if valid:
        cards = "".join(card(event, index) for index, event in enumerate(events))
        current_clusters = count(selection.get("current_clusters"))
        summary = (
            f"Showing {len(events)} leading clusters from {current_clusters} current "
            "clusters. Every card separates permitted attention, state pinning, "
            "withdrawal watch, DDTI overlap, and optional independent-news context. "
            "Visibility is not uncensored discussion."
        )
    else:
        cards = (
            '<article class="trend-card">'
            '<div class="trend-card__top"><span class="trend-card__rank">—</span>'
            f'<span class="trend-card__label">{_h(assessment.get("label") or "TREND LENSES · UNAVAILABLE")}</span></div>'
            '<h3>No current trend-level censorship inference</h3>'
            f'<p class="trend-card__reading">{_h(assessment.get("reading") or "Current permitted-board evidence is unavailable.")}</p>'
            "</article>"
        )
        summary = (
            "The current trend artifact did not clear its freshness or integrity gate. "
            "Palimpsest preserves the unavailable state instead of publishing a false calm."
        )

    return f"""<section class="trend-lenses ps-p2" id="trending-event-lenses" aria-labelledby="trend-lenses-title">
  <div class="trend-lenses__head"><div>
    <p class="trend-lenses__eyebrow">Automatic event lenses · current China trends · public board</p>
    <h2 id="trend-lenses-title">What each trending story indicates</h2>
  </div><span class="trend-lenses__state" id="trend-lenses-state">{_h(assessment.get("label") or "TREND LENSES · UNAVAILABLE")}</span></div>
  <p class="trend-lenses__summary" id="trend-lenses-summary">{_h(summary)}</p>
  <div class="trend-lenses__grid" id="trend-lenses-grid">{cards}</div>
  <div class="trend-lenses__foot"><span>source clock <b id="trend-lenses-clock">{_h(clocks.get("source_generated_at") or "unknown")}</b></span><span>retrieval {_h(payload.get("retrieval_state") or "embedded")}</span><a href="/readings/weibo-hotsearch-latest.json">Open the machine-readable lenses</a></div>
</section>"""


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
    built_at = datetime.now(timezone.utc)
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
    event_lenses, trending_event_lenses = _build_lens_documents(built_at)

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
    page = _swap(
        page,
        "<footer>",
        (
            _event_panel(event_lenses)
            + _trending_event_panel(trending_event_lenses)
            + PROVENANCE
            + EVENT_LENS_JS
            + "</main>\n<footer>"
        ),
        what="footer",
    )
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
        "generated_at": built_at.isoformat(),
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
        "event_lenses": event_lenses,
        "trending_event_lenses": trending_event_lenses,
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
