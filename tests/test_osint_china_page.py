"""Public-contract checks for the OSINT China command surface.

The page is a shell over a generated, same-origin bundle. These tests pin the
failure semantics and integration points that are easy to break with a visual
edit: the page must start in a non-quotable pending state, must never inject
upstream strings as HTML, and must remain discoverable everywhere the site maps
its public surface.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "osint-china.html"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_page_starts_fail_closed_and_names_the_raw_fallback():
    page = PAGE.read_text(encoding="utf-8")
    assert 'data-oc="pending"' in page
    assert 'id="oc-result-status" aria-live="polite" aria-atomic="true"' in page
    assert "/readings/osint-china-latest.json" in page
    assert "no cached placeholder" in page.lower()
    assert '<main id="main" class="ps-wrap ps-wrap--wide" data-oc="pending">' in page


def test_untrusted_feed_strings_are_inserted_as_text_not_markup():
    page = PAGE.read_text(encoding="utf-8")
    assert ".textContent" in page
    assert ".innerHTML" not in page
    assert "safeHref" in page
    assert '/^\\/(?![\\/\\\\])/' in page
    assert '["palimpsest.info", "www.palimpsest.info"]' in page
    assert 'parsed.protocol === "https:"' in page
    assert 'function finite(value) { return typeof value === "number"' in page
    assert 'Number(value)' not in page
    assert 'text(d.n_signals_total' not in page
    assert 'reportingNow + "/" + currentSignals.length' in page


def test_freshness_is_recomputed_in_the_browser():
    page = PAGE.read_text(encoding="utf-8")
    assert "freshness_deadline" in page
    assert "Date.now() > deadline" in page
    assert "source_timestamp" in page
    assert "no measurement timestamp" in page
    assert "window.setInterval(refreshView, 60 * 1000)" in page
    assert "window.setInterval(start, 15 * 60 * 1000)" in page
    assert "statusSignature() !== lastStatusSignature" in page
    assert 'id="oc-layers" aria-live=' not in page


def test_expired_sources_cannot_remain_in_headline_kpis_or_active_findings():
    page = PAGE.read_text(encoding="utf-8")
    assert "function withholdKpi" in page
    for field in ("oc-erasure", "oc-target", "oc-network", "oc-darkness"):
        assert 'withholdKpi(' in page and f'"{field}"' in page
    assert 'if (statusFor(board) === "fresh")' in page
    assert 'if (!source || statusFor(source) !== "fresh") return' in page
    assert "command.headline || command.summary || d.headline" not in page


def test_refresh_failure_keeps_last_verified_bundle_but_marks_it_degraded():
    page = PAGE.read_text(encoding="utf-8")
    assert "if (currentDocument)" in page
    assert 'root.setAttribute("data-oc", lastFetchFailed ? "degraded" : "ok")' in page
    assert "The last verified bundle remains below" in page
    assert "it is not relabelled current" in page


def test_corrupt_is_not_reporting_and_uses_critical_tone():
    page = PAGE.read_text(encoding="utf-8")
    assert 'if (/corrupt/.test(raw)) return "corrupt"' in page
    assert 'status === "corrupt" || status === "error"' in page
    assert 'state !== "error" && state !== "corrupt"' in page


def test_disabled_optional_collector_is_not_relabelled_as_a_dead_scheduler():
    page = PAGE.read_text(encoding="utf-8")
    disabled = page.index('if (/disabled/.test(raw)')
    deadline = page.index("if (isFinite(deadline) && Date.now() > deadline)")
    assert disabled < deadline


def test_status_is_written_in_words_and_not_only_colour():
    page = PAGE.read_text(encoding="utf-8")
    assert 'id="oc-status"' in page
    assert 'id="oc-command-chip"' in page
    assert 'id="oc-runtime-mode">public roll-up' in page
    assert 'runtimeStatus === "fresh" ? "runtime bridge active"' in page
    assert "palimpsest-nemesis" not in page.lower()
    assert 'node("span", "oc-chip", status)' in page
    assert 'aria-hidden="true"' in page  # the coloured dot is explicitly decorative


def test_public_copy_is_honest_before_the_optional_runtime_is_live():
    page = PAGE.read_text(encoding="utf-8")
    assert "palimpsest-nemesis" not in page.lower()
    assert "Nemesis command surface" not in _text("llms.txt")
    assert "hours to daily" not in page
    assert "hourly to daily, weekly and monthly" in page
    assert "Every number keeps" not in page
    assert "denominator appears" in page
    assert "\u2014" not in page and "\u2013" not in page


def test_service_worker_caches_only_successful_expected_content_types():
    worker = _text("sw.js")
    assert "response.ok" in worker
    assert 'response.type === "opaque"' in worker
    assert 'type === "application/json"' in worker
    assert 'type === "text/html"' in worker
    assert "if (expectedContentType(url, res, req))" in worker
    assert "c.addAll(SHELL)" not in worker
    shell_start = worker.index("const SHELL = [")
    shell = worker[shell_start:worker.index("];", shell_start)]
    assert '"/readings/osint-china-latest.json"' not in shell
    assert '"/readings/china-index-latest.json"' not in shell
    assert '"/readings/china-econ-observations.jsonl"' not in shell
    assert '"/china/"' in shell
    assert '"/assets/china.css"' in shell
    assert '"/assets/china.js"' in shell
    assert 'if (url.pathname === LIVE_ROLLUP)' in worker
    assert '"/readings/china-econ-observations.jsonl"' in worker
    assert '"/readings/china-econ-forecast-latest.json"' in worker
    assert 'e.respondWith(fetch(req, { cache: "no-store" }))' in worker


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_live_rollup_network_failure_cannot_fall_back_to_service_worker_cache():
    harness = r'''
const fs = require("fs");
const vm = require("vm");
const listeners = {};
global.self = {
  location: { origin: "https://palimpsest.info" },
  addEventListener: (kind, callback) => { listeners[kind] = callback; },
  skipWaiting: () => {},
  clients: { claim: () => {} }
};
global.location = self.location;
let cacheMatches = 0;
global.caches = {
  match: async () => { cacheMatches += 1; return { stale: true }; },
  open: async () => ({ put: async () => {} }),
  keys: async () => []
};
const fetchCalls = [];
global.fetch = (request, options) => {
  fetchCalls.push({ request, options });
  return Promise.reject(new Error("network unavailable"));
};
vm.runInThisContext(fs.readFileSync("sw.js", "utf8"), { filename: "sw.js" });
let response;
listeners.fetch({
  request: {
    method: "GET",
    url: "https://palimpsest.info/readings/osint-china-latest.json?refresh=1"
  },
  respondWith: (promise) => { response = promise; }
});
(async () => {
  let failedClosed = false;
  try {
    await response;
  } catch (error) {
    failedClosed = error.message === "network unavailable";
  }
  if (!failedClosed) throw new Error("network failure was hidden");
  if (cacheMatches !== 0) throw new Error("live roll-up consulted the cache");
  if (fetchCalls.length !== 1) throw new Error("unexpected live roll-up fetch count");
  if (!fetchCalls[0].options || fetchCalls[0].options.cache !== "no-store") {
    throw new Error("live roll-up fetch was not no-store");
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
'''
    result = subprocess.run(
        [shutil.which("node"), "-e", harness],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_route_is_present_on_every_discovery_surface():
    assert '"osint-china.html": "/osint-china.html"' in _text("scripts/sync_nav.py")
    assert '("/osint-china.html", "Signal board"' in _text("scripts/site_nav.py")
    assert "https://palimpsest.info/osint-china.html" in _text("sitemap.xml")
    assert "https://palimpsest.info/osint-china.html" in _text("llms.txt")
    assert '"/osint-china.html"' in _text("sw.js")


def test_filter_controls_have_keyboard_native_semantics_and_touch_size():
    page = PAGE.read_text(encoding="utf-8")
    assert '<button class="oc-filter" type="button"' in page
    assert 'aria-pressed="true"' in page
    assert "min-height:44px" in page
    assert 'role="group" aria-label="Filter signals by intelligence layer"' in page


def test_complete_records_are_structured_lazily_and_never_injected_as_markup():
    page = PAGE.read_text(encoding="utf-8")
    assert 'makeRecordDetails("inspect complete record", signal, signalFacts(signal))' in page
    assert 'function structuredNode(label, value, path, depth)' in page
    assert 'branch.addEventListener("toggle"' in page
    assert 'details.addEventListener("toggle"' in page
    assert 'getAttribute("data-rendered") === "true"' in page
    assert 'depth >= 32' in page
    assert 'Complete structured record' in page
    assert '.textContent' in page
    assert '.innerHTML' not in page


def test_coverage_ledger_searches_the_retained_payload_not_only_card_copy():
    page = PAGE.read_text(encoding="utf-8")
    assert 'id="oc-search-input" type="search"' in page
    assert 'id="oc-search-clear" type="button"' in page
    assert 'JSON.stringify(signal).toLocaleLowerCase()' in page
    assert 'currentSignals.filter(matchesQuery)' in page
    assert 'No retained records match this search and layer.' in page


def test_intelligence_commons_keeps_project_links_and_claim_boundaries_explicit():
    page = PAGE.read_text(encoding="utf-8")
    assert 'id="commons" hidden' in page
    assert 'var COMMONS_FEED = "/integrations/intelligence-commons/manifest-v1.json"' in page
    assert 'var SCAMSHIELD_PACK = "/integrations/scamshield/intelligence-pack-v1.json"' in page
    assert 'var NARCOSCOPE_SNAPSHOT = "/integrations/intelligence-commons/narcoscope-palimpsest-v1.json"' in page
    assert 'does not establish causation, criminal origin, or guilt' in page
    assert 'No typology match presented as proof' in page
    assert '"api.seiche.info", "narcoscope.com", "drug-price-observatory.vercel.app", "github.com"' in page
    assert 'optionalJson(COMMONS_FEED)' in page
    assert 'optionalJson(SCAMSHIELD_PACK)' in page
    assert 'optionalJson(NARCOSCOPE_SNAPSHOT)' in page
    assert 'contract + " · " + boundary' in page
    assert 'aggregate only; no designation subjects or exact identifiers' in page
