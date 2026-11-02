"""Withheld readings render their restriction notice, never an empty claim.

The Pages rights gate replaces a withheld reading with one metadata-only
endpoint document. A page that reads it must say the values are restricted and
why, not that the ledger is empty, has no headline, or has no runs yet.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

_HARNESS = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
class El {
  constructor(tag, id) {
    this.tagName = String(tag || "div").toUpperCase();
    this.id = id || "";
    this.children = [];
    this.own = "";
    this.className = "";
    this.hidden = false;
    this.attributes = {};
    this.style = {};
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name),
    };
  }
  get textContent() { return this.own + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this.own = String(value); this.children = []; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(item => item !== child); return child; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelector(selector) {
    const name = selector.replace(/^\./, "");
    for (const child of this.children) {
      if (typeof child.className === "string" && child.className.split(/\s+/).includes(name)) return child;
      const found = child.querySelector ? child.querySelector(selector) : null;
      if (found) return found;
    }
    return null;
  }
}
function sandbox(respond) {
  const nodes = new Map();
  const context = vm.createContext({
    document: {
      readyState: "complete",
      documentElement: {},
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, new El("div", id));
        return nodes.get(id);
      },
      createElement: tag => new El(tag),
      createTextNode: text => ({ textContent: String(text) }),
      querySelectorAll: () => [],
      addEventListener() {},
    },
    matchMedia: () => ({ matches: false }),
    fetch: async url => respond(url),
    Date, Promise, Error, JSON,
  });
  context.window = context;
  return { context, node: id => nodes.get(id), El };
}
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise(setImmediate); };
const NOTICE = {
  schema_version: "palimpsest-restricted-publication-endpoint.v1",
  status: "restricted",
  availability: "unavailable",
  publication_allowed: false,
  reason: "Current source policy denies publication of one or more upstream value families; this endpoint therefore exposes metadata only.",
  limitations: ["Unavailable or restricted evidence is not zero, calm, healthy, or a directional signal."],
  rights_evaluated_at: new Date().toISOString()
};
"""


def _run(body: str, *args: str) -> None:
    node = shutil.which("node")
    assert node is not None
    script = _HARNESS + "(async () => {\n" + body + "\n})().catch(error => { console.error(error); process.exit(1); });"
    completed = subprocess.run(
        [node, "-e", script, *args], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_fund_page_names_the_forecast_restriction_and_its_reason() -> None:
    _run(
        r"""
const html = fs.readFileSync(process.argv[1], "utf8");
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1])
  .find(block => block.includes('var FL = "/readings/forecast-ledger-latest.json"'));
assert.ok(source);
const ledger = "/readings/forecast-ledger-latest.json";
function render(payload) {
  const page = sandbox(url => url === ledger
    ? { ok: true, status: 200, json: async () => payload }
    : { ok: false, status: 404, json: async () => ({}) });
  vm.runInContext(source, page.context);
  return page;
}

const page = render(NOTICE);
await settle();
const why = "publication restricted: " + NOTICE.reason;
for (const id of ["hero-forecasts", "fl-cov", "fl-n", "fl-sig"]) {
  assert.equal(page.node(id).textContent, "withheld", id);
  assert.ok(page.node(id).classList.contains("f-warn"), id);
}
assert.equal(page.node("hero-forecast-cov").textContent, why);
assert.equal(page.node("fl-headline").textContent, why);
assert.match(page.node("hero-forecast-fresh").textContent, /^rights evaluated /);
assert.match(page.node("fl-fresh").textContent, /^rights evaluated /);
for (const id of ["fl-rows", "fl-miss-rows"]) {
  const rows = page.node(id).children;
  assert.equal(rows.length, 1, id);
  assert.equal(rows[0].children[0].colSpan, 6);
  assert.equal(rows[0].textContent,
    "scores withheld under the current source policy; restricted evidence is not zero, calm, healthy, or a directional signal");
}
for (const id of ["fl-baseline", "fl-misstotal", "fl-excluded", "fl-caveats"]) {
  assert.equal(page.node(id).textContent,
    "withheld while publication is restricted; rights evidence: /readings/china-publication-rights-latest.json", id);
}
// A restriction is not a read failure: the error boxes are never written.
assert.equal(page.node("err-forecast"), undefined);
assert.equal(page.node("err-forecast-2"), undefined);

const live = render({
  generated_at: new Date().toISOString(), n_forecasts: 3744, n_signals_scored: 9,
  pooled_empirical_coverage: 0.824, nominal_coverage: 0.8, headline: "9 signals scored",
  n_beating_baseline: 2, signals: {}
});
await settle();
assert.equal(live.node("hero-forecasts").textContent, "3744");
assert.equal(live.node("fl-headline").textContent, "“9 signals scored”");
""",
        str(ROOT / "fund.html"),
    )


def test_charts_report_a_withheld_history_as_restricted_not_empty() -> None:
    _run(
        r"""
const source = fs.readFileSync(process.argv[1], "utf8");
function charts(respond) {
  const page = sandbox(respond);
  vm.runInContext(source, page.context);
  return { page, charts: page.context.window.PalimpsestCharts };
}
const text = body => ({ ok: true, status: 200, text: async () => body });

const withheld = charts(() => text(JSON.stringify(NOTICE) + "\n"));
await assert.rejects(withheld.charts.loadHistory("/readings/event-flags-history.jsonl"),
  error => error.restricted === true && error.message === NOTICE.reason);
for (const draw of ["raster", "calibration"]) {
  const host = new withheld.page.El("section");
  const fallback = host.appendChild(new withheld.page.El("p"));
  fallback.className = "pc__fallback";
  fallback.textContent = "Every flag state is committed to the raw history file.";
  withheld.charts[draw](host, { url: "/readings/" + draw + "-history.jsonl", groups: [] });
  await settle();
  assert.equal(fallback.textContent, "Publication restricted: " + NOTICE.reason + " · rights evidence", draw);
  const link = fallback.querySelector(".pc__err").children[0];
  assert.equal(link.href, "/readings/china-publication-rights-latest.json");
}

const rows = [
  { generated_at: "2026-11-02T09:21:46Z", states: { ooni_gfw: "calm" } },
  { generated_at: "2026-11-01T09:21:46Z", states: { ooni_gfw: "watch" } }
];
const history = charts(() => text(rows.map(row => JSON.stringify(row)).join("\n") + "\n"));
const loaded = await history.charts.loadHistory("/readings/ok-history.jsonl");
assert.deepEqual([...loaded].map(row => row.states.ooni_gfw), ["watch", "calm"]);
const single = charts(() => text(JSON.stringify({ generated_at: "2026-11-02T00:00:00Z", status: "restricted" }) + "\n"));
assert.equal((await single.charts.loadHistory("/readings/one-row-history.jsonl")).length, 1);

const empty = charts(() => text(""));
const host = new empty.page.El("section");
const fallback = host.appendChild(new empty.page.El("p"));
fallback.className = "pc__fallback";
empty.charts.raster(host, { url: "/readings/empty-history.jsonl", groups: [] });
await settle();
assert.match(fallback.textContent, /no runs on record yet/);
""",
        str(ROOT / "assets" / "charts.js"),
    )
