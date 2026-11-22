"""The redesigned front door remains accessible, live, and evidence bounded."""
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_home_has_one_heading_and_clear_publication_routes():
    page = (ROOT / "index.html").read_text(encoding="utf-8")

    assert len(re.findall(r"<h1(?:\s|>)", page)) == 1
    for route in (
        "/journal/",
        "/news/",
        "/china/",
        "/readings/eval-registry.html",
        "/developers.html",
        "/feeds/",
        "/updates/2026-08-17-listening-pass/",
        "/guides/telegram-scam-message-checker/",
        "/fund.html",
    ):
        assert f'href="{route}"' in page
    for marker in (
        "data-home-registry",
        "data-home-osint",
        "data-home-osint-summary",
        "data-home-wire",
        "data-home-wire-summary",
        "data-home-wire-source-state",
    ):
        assert marker in page
    assert "Follow the economy." in page
    assert "Read the record." in page
    for route in ("/china/evidence/", "/china/economy/", "/research/markets/", "/research/connected/"):
        assert f'href="{route}"' in page
    assert "retain their own evidence labels" in page
    assert "Evidence and review" in page
    assert "machine-authored text and human review" in page
    assert 'id="main"' in page
    assert 'class="ps home"' in page
    assert 'rel="icon" type="image/svg+xml"' in page


def test_home_initial_ledger_makes_no_unverified_live_count_claims():
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    ledger = page.split('<div class="hm-ledger">', 1)[1].split(
        '<section class="hm-boundaries"', 1
    )[0]

    for stale_claim in (
        '<strong data-home-registry-runs>423</strong>',
        '<span data-home-osint-live>32</span>',
        '<span data-home-osint-total>33</span>',
        '<strong data-home-wire-events>308</strong>',
        '<span data-home-wire-sources>30</span>',
        '<span data-home-wire-total-sources>31</span>',
        '<code data-home-osint-state>healthy</code>',
    ):
        assert stale_claim not in ledger
    for placeholder in (
        'data-home-registry-runs>checking current receipt</strong>',
        'data-home-registry-root>receipt unavailable</code>',
        'data-home-osint-live>checking</span>',
        'data-home-osint-total>checking</span>',
        'data-home-osint-state>checking current receipt</code>',
        'data-home-wire-events>checking current receipt</strong>',
        'data-home-wire-sources>checking</span>',
        'data-home-wire-total-sources>checking</span>',
    ):
        assert placeholder in ledger


def test_home_progressive_enhancement_never_injects_markup():
    script = (ROOT / "assets" / "home.js").read_text(encoding="utf-8")
    assert "textContent" in script
    assert "setAttribute" in script
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script


def test_home_rejects_restricted_success_responses_and_uses_fresh_public_feed():
    script = (ROOT / "assets" / "home.js").read_text(encoding="utf-8")

    assert 'document.status === "restricted"' in script
    assert 'document.availability === "unavailable"' in script
    assert "document.publication_allowed === false" in script
    for schema in (
        "palimpsest-collector-health.v1",
        "palimpsest.publication-freshness.v1",
    ):
        assert schema in script
    assert 'return read("/news/feed.json")' in script
    assert 'read("/freshness", "palimpsest.publication-freshness.v1", true)' in script
    assert 'item._palimpsest.kind === "publisher_source_record"' in script
    assert 'setText("[data-home-osint-state]", "Assessment unavailable")' in script
    assert 'setText("[data-home-wire-events]", "unavailable")' in script


def test_home_public_document_and_feed_helpers_execute_fail_closed():
    node = shutil.which("node")
    assert node is not None, "Node is required to execute the homepage validator"
    harness = r"""
const helpers = require(process.argv[1]);
function check(condition, message) {
  if (!condition) throw new Error(message);
}
check(helpers.isPublicDocument({schema_version: "expected"}, "expected"), "valid document rejected");
check(!helpers.isPublicDocument({schema_version: "wrong"}, "expected"), "schema drift accepted");
check(!helpers.isPublicDocument({schema_version: "expected", status: "restricted"}, "expected"), "restricted status accepted");
check(!helpers.isPublicDocument({schema_version: "expected", availability: "unavailable"}, "expected"), "unavailable document accepted");
check(!helpers.isPublicDocument({schema_version: "expected", publication_allowed: false}, "expected"), "denied publication accepted");
const feed = {
  version: "https://jsonfeed.org/version/1.1",
  items: [
    {_palimpsest: {kind: "instrument_measurement"}},
    {_palimpsest: {kind: "publisher_source_record"}},
    {_palimpsest: {kind: "publisher_source_record"}}
  ]
};
check(helpers.countPublicReports(feed) === 2, "source report count is wrong");
let refused = false;
try {
  helpers.countPublicReports({version: "wrong", items: feed.items});
} catch (_error) {
  refused = true;
}
check(refused, "invalid feed version accepted");
"""
    subprocess.run(
        [node, "-e", harness, str(ROOT / "assets" / "home.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_home_visual_system_is_responsive_and_motion_safe():
    css = (ROOT / "assets" / "home.css").read_text(encoding="utf-8")
    assert "gradient(" not in css
    assert "@media (max-width: 860px)" in css
    assert "@media (max-width: 560px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css
    assert "--hm-blue: var(--research-blue)" in css
    assert "--hm-teal: var(--research-green)" in css
    assert "--hm-paper: var(--research-bg)" in css
    responsive = css.split("@media (max-width: 980px)", 1)[1].split(
        "@media (max-width: 860px)", 1
    )[0]
    assert responsive.index('"headline"') < responsive.index('"details"')
    assert responsive.index('"details"') < responsive.index('"proof"')


def test_new_home_copy_avoids_typographic_dashes():
    for relative in ("index.html", "assets/home.css", "assets/home.js"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "\u2013" not in text, relative
        assert "\u2014" not in text, relative


def test_home_leads_with_one_attributable_china_funnel():
    home = (ROOT / "index.html").read_text(encoding="utf-8")

    assert '<link rel="canonical" href="https://www.palimpsest.info/">' in home
    assert 'data-growth-page="home"' in home
    assert "China information-control evidence" in home
    assert "researchers, journalists and institutions" in home
    assert 'href="/news/china/situation/"' in home
    assert 'data-growth-event="brief_clicked"' in home
    assert 'data-growth-event="follow_clicked"' in home
    assert 'data-growth-location="home_primary"' in home
    assert 'data-growth-location="home_secondary"' in home
    assert "A click is not counted as a subscriber." in home
    assert 'href="/weekly-situation.html"' in home
    assert 'data-growth-event="weekly_brief_clicked"' in home
    assert 'data-growth-event="private_diligence_clicked"' in home
    assert "?start=china_diligence_home" in home
    assert "Starting the bot also subscribes you to daily updates" in home


def test_growth_measurement_is_bounded_and_respects_browser_opt_outs():
    shell = (ROOT / "assets" / "shell.js").read_text(encoding="utf-8")

    assert 'var GROWTH_SCHEMA = "palimpsest.growth-event.v1"' in shell
    assert 'var GROWTH_HOST = "www.palimpsest.info"' in shell
    assert 'navigator.doNotTrack === "1"' in shell
    assert "navigator.globalPrivacyControl === true" in shell
    assert 'localStorage.getItem("palimpsest_analytics_opt_out")' in shell
    assert "document.referrer" in shell
    assert 'source: growthSource()' in shell
    assert shell.index("chatgpt|openai|perplexity|gemini") < shell.index(
        "google|bing|duckduckgo"
    )
    assert "raw_referrer" not in shell
    assert "user_id" not in shell
    assert "client_timestamp" not in shell
    assert "depth >= 0.5" in shell
    assert "elapsed >= 20000" in shell
    assert 'trackGrowth("citation_copied", "page_share")' in shell
    assert 'trackGrowth("download_started", "page_share")' in shell


def test_service_worker_never_presents_an_old_journal_head_as_current():
    worker = (ROOT / "sw.js").read_text(encoding="utf-8")

    assert '"/readings/eval-articles-latest.json"' in worker
    assert '"/readings/eval-registry-latest.json"' in worker
    assert '"/journal/feed.json"' in worker
    assert '"/journal/feed.xml"' in worker
    assert "LIVE_JOURNAL_SYNDICATION.has(url.pathname)" in worker


def test_publication_receipt_preserves_dated_counts_without_claiming_freshness():
    node = shutil.which("node")
    assert node is not None
    harness = r"""
const assert = require("node:assert/strict");
const {publicationView, countPublicReports, collectorView} = require(process.argv[1]);
const now = Date.parse("2026-09-25T05:30:00Z");
const receipt = {
  schema_version: "palimpsest.publication-freshness.v1", status: "fresh",
  checked_at: "2026-09-25T05:30:00Z", source_commit: "a".repeat(40), tree_sha256: "b".repeat(64),
  clocks: {publication: {status: "fresh", generated_at: "2026-09-25T05:20:00Z"},
           wire: {status: "fresh", generated_at: "2026-09-25T05:15:00Z"}}
};
assert.equal(publicationView(receipt, now).state, "live");
const board = {schema_version: "palimpsest-collector-health.v1", generated_at: "2026-09-25T04:00:00Z",
  summary: {n_datasets: 98, by_state: {fresh:49, stale:5, gated:28, partial:2, warming_up:14}}};
assert.equal(collectorView(board, now).state, "delayed");
assert.equal(collectorView(board, now).fresh, 49);
board.generated_at = "2026-09-25T05:25:00Z";
assert.equal(collectorView(board, now).state, "live");
board.summary.by_state.fresh = 99;
assert.throws(() => collectorView(board, now));
const delayed = structuredClone(receipt);
delayed.status = "stale";
delayed.clocks.wire = {status: "stale", generated_at: "2026-09-25T04:05:02Z"};
delayed.rights = {publication_allowed: false, mode: "rights-suppressed"};
assert.equal(publicationView(delayed, now).state, "delayed");
assert.equal(publicationView(delayed, now).asOf, "2026-09-25 04:05 UTC");
const claimedFresh = structuredClone(delayed);
claimedFresh.status = "fresh"; claimedFresh.clocks.wire.status = "fresh";
assert.equal(publicationView(claimedFresh, now).state, "delayed");
for (const mutate of [
  r => {r.checked_at = "2026-09-25T04:00:00Z";},
  r => {r.checked_at = "2026-09-25T06:00:00Z";},
  r => {r.clocks.wire.generated_at = "2026-09-25T06:00:00Z";},
  r => {r.clocks.wire.generated_at = "bad";},
  r => {r.schema_version = "wrong";},
  r => {r.source_commit = "unknown";},
  r => {r.status = "restricted";}
]) {
  const invalid = structuredClone(receipt); mutate(invalid);
  assert.throws(() => publicationView(invalid, now));
}
assert.throws(() => countPublicReports({publication_allowed: false,
  version: "https://jsonfeed.org/version/1.1", items: [{_palimpsest: {kind: "publisher_source_record"}}]}));
"""
    subprocess.run([node, "-e", harness, str(ROOT / "assets/home.js")], check=True, capture_output=True, text=True)


def test_collector_tile_counts_every_state_that_is_not_fresh():
    node = shutil.which("node")
    assert node is not None
    harness = r"""
const assert = require("node:assert/strict");
const {collectorView} = require(process.argv[1]);
const now = Date.parse("2026-11-02T09:08:33Z");
// The live board on 2026-11-02: 44 of 98 datasets were not fresh.
const board = {schema_version: "palimpsest-collector-health.v1", generated_at: "2026-11-02T09:07:33Z",
  summary: {n_datasets: 98, by_state: {abstained: 2, disabled: 1, fresh: 54, gated: 28, partial: 1,
    "private-node": 1, stale: 1, warming: 10}}};
assert.equal(collectorView(board, now).coverage,
  "44 not fresh: 1 stale; 28 gated; 1 partial; 10 warming; 2 abstained; 1 disabled; 1 private-node");
// Counts come from the board: a new state is still shown and a zero is omitted.
board.summary = {n_datasets: 10, by_state: {fresh: 6, gated: 0, quarantined: 2, warming: 2}};
assert.equal(collectorView(board, now).coverage, "4 not fresh: 2 warming; 2 quarantined");
board.summary = {n_datasets: 3, by_state: {fresh: 3, stale: 0}};
assert.equal(collectorView(board, now).coverage, "0 not fresh");
"""
    subprocess.run([node, "-e", harness, str(ROOT / "assets/home.js")], check=True, capture_output=True, text=True)


def test_home_refresh_handles_503_network_loss_and_release_changes():
    node = shutil.which("node")
    assert node is not None
    harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const nodes = new Map();
const get = key => {if (!nodes.has(key)) nodes.set(key, {textContent: "", setAttribute(k,v){this[k]=v;}}); return nodes.get(key);};
const now = Date.now();
const receipt = {
  schema_version: "palimpsest.publication-freshness.v1", status: "stale",
  checked_at: new Date(now).toISOString(), source_commit: "a".repeat(40), tree_sha256: "b".repeat(64),
  clocks: {publication: {status: "stale", generated_at: new Date(now-7200000).toISOString()},
           wire: {status: "stale", generated_at: new Date(now-7300000).toISOString()}}
};
const feed = {version: "https://jsonfeed.org/version/1.1", items: [
  {_palimpsest: {kind: "publisher_source_record"}}, {_palimpsest: {kind: "publisher_source_record"}},
  {_palimpsest: {kind: "instrument_measurement"}}
]};
let tick, visibility, offline = false, feedReads = 0, switching = false, checks = 0;
const document = {hidden: false, querySelectorAll: key => [get(key)], addEventListener(event, fn){visibility=fn;}};
const context = {document, setInterval(fn, ms){assert.equal(ms, 60000); tick=fn;}, fetch: async url => {
  if (offline) throw Error("offline");
  if (url === "/freshness") {
    checks++;
    const data = structuredClone(receipt);
    if (switching) data.tree_sha256 = String(checks % 2 ? "c" : "d").repeat(64);
    return {ok: false, status: 503, json: async () => data};
  }
  if (url === "/news/feed.json") {feedReads++; return {ok:true,status:200,json:async()=>feed};}
  return {ok:false,status:503,json:async()=>({})};
}};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const settle = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await settle();
  assert.equal(get("[data-home-wire-events]").textContent, "2");
  assert.equal(get("[data-home-wire]")["data-feed-state"], "delayed");
  assert.match(get("[data-home-wire-source-state]").textContent, /As of .*UTC.*Update pending/);
  assert.equal(feedReads, 1);
  tick(); await settle(); assert.equal(feedReads, 1, "unchanged publication fetched again");
  offline=true; tick(); await settle();
  assert.equal(get("[data-home-wire-events]").textContent, "2");
  assert.match(get("[data-home-wire-source-state]").textContent, /Freshness check unavailable/);
  offline=false; switching=true; tick(); await settle();
  assert.equal(get("[data-home-wire-events]").textContent, "2");
  assert.equal(get("[data-home-wire]")["data-feed-state"], "delayed");
  document.hidden=true; const before=checks; tick(); await settle(); assert.equal(checks,before);
  document.hidden=false; switching=false; visibility(); await settle();
  assert.match(get("[data-home-wire-source-state]").textContent, /Update pending/);
})().catch(error => {console.error(error); process.exitCode=1;});
"""
    subprocess.run([node, "-e", harness, str(ROOT / "assets/home.js")], check=True, capture_output=True, text=True)


def _applications_section(page: str) -> str:
    return page.split('<section class="hm-apps"', 1)[1].split("</section>", 1)[0]


def test_home_states_three_applications_with_routes_and_limits():
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    apps = _applications_section(page)

    for anchor in ("censorship", "china-economy", "ai-evals"):
        assert f'id="{anchor}"' in apps
        assert f'href="#{anchor}"' in page
    for route in (
        "/news/",
        "/news/china/erasure/",
        "/china/evidence/",
        "/china/economy/",
        "/readings/china-publication-rights-latest.json",
        "/journal/",
        "/evals/",
        "/readings/eval-registry.html",
        "/readings/eval-assurance-latest.json",
    ):
        assert f'href="{route}"' in apps, route
    for route in ("/data.html", "/developers.html#first-result", "/feeds/", "/research/markets/"):
        assert f'href="{route}"' in page, route
    assert "never pooled" in apps
    assert "Integrity is not validity." in apps
    assert "CFETS and ChinaMoney values" in apps
    assert "never zero, calm or healthy" in apps
    assert page.count("<h1") == 1


def test_application_cards_carry_no_hand_typed_counts():
    apps = _applications_section((ROOT / "index.html").read_text(encoding="utf-8"))

    for placeholder in (
        "data-home-newsroom-stories>checking</strong>",
        "data-home-evals-runs>checking</strong>",
        "data-home-assurance-score>checking</strong>",
        "data-home-rights-label>checking</strong>",
        "data-home-newsroom-state>checking current edition</code>",
        "data-home-evals-state>checking current receipt</code>",
        "data-home-assurance-state>checking current claim ceiling</code>",
        "data-home-rights-state>checking current rights receipt</code>",
    ):
        assert placeholder in apps, placeholder
    numerals = re.findall(r">\s*([0-9][0-9,.%]*)\s*<", apps)
    assert numerals == ["01", "02", "03"], numerals
    for marker in ("data-home-newsroom", "data-home-evals", "data-home-assurance", "data-home-rights"):
        assert f'{marker} data-feed-state="loading"' in apps


def test_application_views_validate_receipts_and_fail_closed():
    node = shutil.which("node")
    assert node is not None
    harness = r"""
const assert = require("node:assert/strict");
const h = require(process.argv[1]);
const now = Date.parse("2026-11-22T11:00:00Z");
const newsroom = {schema_version: "palimpsest-news.v1", generated_at: "2026-11-22T10:52:03Z", n_stories: 3,
  stories: [{}, {}, {}], coverage: {total: 5, counts: {stale: 1, live: 3, degraded: 1, missing: 0}}};
let view = h.newsroomView(newsroom, now);
assert.equal(view.stories, 3);
assert.equal(view.state, "live");
assert.equal(view.asOf, "2026-11-22 10:52 UTC");
assert.equal(view.sources, "Sources: 3 live; 1 degraded; 1 stale");
assert.equal(h.newsroomView(newsroom, now + 3 * 3600000).state, "delayed");
for (const mutate of [
  d => {d.n_stories = 4;},
  d => {d.coverage.total = 6;},
  d => {d.coverage.counts.live = -1;},
  d => {d.status = "restricted";},
  d => {d.schema_version = "other";},
  d => {d.generated_at = "bad";},
]) {
  const copy = structuredClone(newsroom); mutate(copy);
  assert.throws(() => h.newsroomView(copy, now));
}
const registry = {runs: 12, preregistrations: 2, verified: true, merkle_root: "a".repeat(64),
  head_ts: "2026-11-22T08:16:54.326172+00:00"};
view = h.registryView(registry);
assert.deepEqual([view.runs, view.preregistrations, view.verified, view.root, view.asOf],
  [12, 2, true, "aaaaaaaaaaaa", "2026-11-22 08:16 UTC"]);
assert.equal(h.registryView({...registry, verified: false}).verified, false);
assert.throws(() => h.registryView({...registry, merkle_root: "short"}));
assert.throws(() => h.registryView({...registry, runs: 0}));
const assurance = {schema: "palimpsest.eval-assurance.v1", generated_at: "2026-11-22T08:16:54Z",
  summary: {checks: 6, pass: 2, fail: 1, open: 1, partial: 1, pending: 1},
  claim_ceiling: {level: "provisional-measurement", cannot_yet_claim: "No replication is on record."}};
view = h.assuranceView(assurance);
assert.equal(view.score, "2 of 6");
assert.equal(view.level, "provisional measurement");
assert.equal(view.rest, "Not passed: 1 partial; 1 pending; 1 open; 1 fail.");
assert.equal(view.ceiling, "No replication is on record.");
for (const mutate of [
  d => {d.summary.pass = 3;},
  d => {d.schema = "other";},
  d => {d.claim_ceiling.cannot_yet_claim = "";},
  d => {d.publication_allowed = false;},
]) {
  const copy = structuredClone(assurance); mutate(copy);
  assert.throws(() => h.assuranceView(copy));
}
const rights = {schema_version: "palimpsest-restricted-publication-endpoint.v1", status: "restricted",
  availability: "unavailable", publication_allowed: false, rights_evaluated_at: "2026-11-22T11:17:16Z",
  counts: {input_records: 9, published_records: 0, restricted_records: 9}, reason: "Policy denies values."};
view = h.rightsView(rights);
assert.equal(view.label, "Restricted");
assert.equal(view.asOf, "2026-11-22 11:17 UTC");
assert.ok(!/\d+ (records|values)|\b0\b/.test(JSON.stringify([view.label, view.reason])));
assert.ok(!("counts" in view));
for (const mutate of [
  d => {d.publication_allowed = true;},
  d => {d.schema_version = "palimpsest-news.v1";},
  d => {d.status = "fresh"; d.availability = "available";},
  d => {d.rights_evaluated_at = "bad";},
]) {
  const copy = structuredClone(rights); mutate(copy);
  assert.throws(() => h.rightsView(copy));
}
"""
    subprocess.run([node, "-e", harness, str(ROOT / "assets/home.js")], check=True, capture_output=True, text=True)


def test_application_tiles_render_text_only_and_mark_failures_unavailable():
    node = shutil.which("node")
    assert node is not None
    harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
function run(fail) {
  const nodes = new Map();
  const get = key => {if (!nodes.has(key)) nodes.set(key, {textContent: "", setAttribute(k, v) {this[k] = v;}}); return nodes.get(key);};
  const now = Date.now();
  const docs = {
    "/readings/newsroom-latest.json": {schema_version: "palimpsest-news.v1", generated_at: new Date(now - 60000).toISOString(),
      n_stories: 2, stories: [{}, {}], coverage: {total: 2, counts: {live: 1, degraded: 1}}},
    "/readings/eval-registry-latest.json": {runs: 5, preregistrations: 2, verified: true, merkle_root: "c".repeat(64),
      head_ts: new Date(now - 3600000).toISOString()},
    "/readings/eval-assurance-latest.json": {schema: "palimpsest.eval-assurance.v1", generated_at: new Date(now).toISOString(),
      summary: {checks: 2, pass: 1, pending: 1}, claim_ceiling: {level: "provisional-measurement", cannot_yet_claim: "Validation is pending."}},
    "/readings/china-econ-latest.json": {schema_version: "palimpsest-restricted-publication-endpoint.v1", status: "restricted",
      availability: "unavailable", publication_allowed: false, rights_evaluated_at: new Date(now).toISOString(),
      counts: {published_records: 0}, reason: "Policy denies values."}
  };
  const document = {hidden: false, querySelectorAll: key => [get(key)], addEventListener() {}};
  const context = {document, setInterval() {}, fetch: async url => {
    if (fail.has(url)) throw new Error("offline");
    if (url in docs) return {ok: true, status: 200, json: async () => structuredClone(docs[url])};
    return {ok: false, status: 503, json: async () => ({})};
  }};
  vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
  return new Promise(resolve => setTimeout(() => resolve(get), 20));
}
(async () => {
  let get = await run(new Set());
  assert.equal(get("[data-home-newsroom-stories]").textContent, "2");
  assert.equal(get("[data-home-newsroom-status]").textContent, "Current");
  assert.match(get("[data-home-newsroom-state]").textContent, /^Edition .* UTC\. Sources: 1 live; 1 degraded\.$/);
  assert.equal(get("[data-home-newsroom]")["data-feed-state"], "live");
  assert.equal(get("[data-home-evals-runs]").textContent, "5");
  assert.match(get("[data-home-evals-state]").textContent, /From 2 preregistrations\. Chain verified at build\. Head .* UTC\. Root cccccccccccc\./);
  assert.equal(get("[data-home-assurance-score]").textContent, "1 of 2");
  assert.equal(get("[data-home-assurance-status]").textContent, "Claim level: provisional measurement");
  assert.match(get("[data-home-assurance-state]").textContent, /Not passed: 1 pending\. Cannot yet claim: Validation is pending\./);
  assert.equal(get("[data-home-rights-status]").textContent, "Restricted");
  assert.equal(get("[data-home-rights-label]").textContent, "Metadata only");
  assert.equal(get("[data-home-rights]")["data-feed-state"], "restricted");
  assert.ok(!/\b0\b/.test(get("[data-home-rights-state]").textContent));
  get = await run(new Set(["/readings/newsroom-latest.json", "/readings/eval-registry-latest.json",
    "/readings/eval-assurance-latest.json", "/readings/china-econ-latest.json"]));
  for (const name of ["newsroom", "evals", "assurance", "rights"]) {
    assert.equal(get(`[data-home-${name}]`)["data-feed-state"], "unavailable", name);
    assert.equal(get(`[data-home-${name}-status]`).textContent, "Unavailable", name);
  }
  assert.equal(get("[data-home-newsroom-stories]").textContent, "unavailable");
  assert.equal(get("[data-home-evals-runs]").textContent, "unavailable");
  assert.equal(get("[data-home-assurance-score]").textContent, "unavailable");
  assert.equal(get("[data-home-rights-label]").textContent, "Unavailable");
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", harness, str(ROOT / "assets/home.js")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
