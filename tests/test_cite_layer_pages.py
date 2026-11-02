"""New researcher UX pages stay dash-free and point at the sealed files."""
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    "cite.html",
    "challenge.html",
    "status.html",
    "forecast-scorecard.html",
    "weekly-situation.html",
)


def test_cite_layer_pages_exist_and_avoid_typographic_dashes() -> None:
    for relative in PAGES:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "\u2013" not in text, relative
        assert "\u2014" not in text, relative
        assert 'id="main"' in text


def test_status_and_scorecard_do_not_inject_markup() -> None:
    for relative in ("status.html", "forecast-scorecard.html", "cite.html"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "innerHTML" not in text, relative
        assert "document.write" not in text, relative


def test_challenge_and_cite_name_the_reproduce_command() -> None:
    cite = (ROOT / "cite.html").read_text(encoding="utf-8")
    challenge = (ROOT / "challenge.html").read_text(encoding="utf-8")
    assert "scripts.build_citation_pack" in cite
    assert "scripts/reproduce_all.py" in challenge
    assert "weekly-situation-latest.json" in cite


# A minimal DOM for running a page's inline script under node. It models only
# what these pages touch: ids, text, children, options, hidden and listeners.
_DOM_HARNESS = r"""
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
    this.value = "";
    this.href = "";
    this.attributes = {};
    this.style = {};
    this.listeners = {};
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name),
    };
  }
  get textContent() { return this.own + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this.own = String(value); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  get options() { return this.children.filter(child => child.tagName === "OPTION"); }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(item => item !== child); return child; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type) { (this.listeners[type] || []).forEach(fn => fn({ type, target: this })); }
}
function page(path, marker, tags, respond) {
  const html = fs.readFileSync(path, "utf8");
  const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).find(block => block.includes(marker));
  assert.ok(source, "inline script not found");
  const nodes = new Map();
  const requests = [];
  const context = vm.createContext({
    document: {
      readyState: "complete",
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, new El(tags[id] || "div", id));
        return nodes.get(id);
      },
      createElement: tag => new El(tag),
      createTextNode: text => ({ textContent: String(text) }),
      addEventListener() {},
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return respond(url);
    },
    Date, Promise,
  });
  context.window = context;
  vm.runInContext(source, context);
  return { nodes, requests, node: id => nodes.get(id) };
}
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise(setImmediate); };
const json = payload => ({ ok: true, status: 200, json: async () => payload });
const RESTRICTED = {
  schema_version: "palimpsest-restricted-publication-endpoint.v1",
  status: "restricted",
  availability: "unavailable",
  publication_allowed: false,
  reason: "Current source policy denies publication of one or more upstream value families; this endpoint therefore exposes metadata only.",
  limitations: [
    "No source value or derivative from a denied family is published.",
    "Unavailable or restricted evidence is not zero, calm, healthy, or a directional signal."
  ],
  rights_evaluated_at: "2026-11-02T09:28:44Z"
};
"""


def _run_page_harness(body: str, *args: str) -> None:
    node = shutil.which("node")
    assert node is not None
    script = _DOM_HARNESS + "(async () => {\n" + body + "\n})().catch(error => { console.error(error); process.exit(1); });"
    completed = subprocess.run(
        [node, "-e", script, *args], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_cite_builds_records_from_the_public_atlas_and_names_restrictions() -> None:
    _run_page_harness(
        r"""
const catalog = {
  schema: "palimpsest-data-catalog/v1",
  generated_at: "2026-11-02T09:23:42Z",
  datasets: [
    { id: "data-darkness", name: "Official-data darkness", latest: "readings/data-darkness-latest.json",
      publication_allowed: false, artifacts: { evidence_state: "gated", observed_at: null } },
    { id: "weekly-situation", name: "Weekly situation report", latest: "readings/weekly-situation-latest.json",
      artifacts: { evidence_state: "fresh", observed_at: "2026-11-02T09:21:47.971760Z" } }
  ]
};
const view = page(process.argv[1], "json-link", { dataset: "select", "json-link": "a" }, () => json(catalog));
await settle();
assert.deepEqual(view.requests.map(request => request.url), ["/readings/public-data-catalog-latest.json"]);
const select = view.node("dataset");
assert.deepEqual(select.options.map(option => option.textContent), [
  "data-darkness: Official-data darkness (gated)",
  "weekly-situation: Weekly situation report"
]);
assert.equal(select.value, "weekly-situation");
const year = view.node("accessed").value.slice(0, 4);
assert.match(view.node("apa").textContent,
  /^Palimpsest\. \(\d{4}\)\. Weekly situation report \[dataset\]\. Palimpsest Evidence Atlas\. https:\/\/palimpsest\.info\/readings\/weekly-situation-latest\.json \(accessed \d{4}-\d{2}-\d{2}\)\.$/);
assert.equal(view.node("bibtex").textContent.split("\n")[0], "@misc{palimpsest-weekly-situation,");
assert.match(view.node("bibtex").textContent, new RegExp("year         = \\{" + year + "\\}"));
assert.equal(view.node("json-link").href, "/readings/weekly-situation-latest.json");
assert.equal(view.node("status").textContent,
  "Built from the public atlas generated 2026-11-02T09:23:42Z. Evidence state: fresh; observed 2026-11-02T09:21:47.971760Z. If a field is missing, this page says so instead of guessing.");
select.value = "data-darkness";
select.dispatch("change");
assert.match(view.node("status").textContent, /values is restricted, so the file at this URL is a metadata-only restriction notice, not a reading/);
assert.match(view.node("apa").textContent, /Official-data darkness \[dataset\]/);

const restricted = page(process.argv[1], "json-link", { dataset: "select" }, () => json(RESTRICTED));
await settle();
assert.equal(restricted.node("status").textContent,
  "The atlas is restricted: " + RESTRICTED.reason.replace(/\.$/, "") + ".");
assert.equal(restricted.node("apa").textContent, "not reported");
assert.equal(restricted.node("dataset").options.length, 0);
""",
        str(ROOT / "cite.html"),
    )


def test_scorecard_shows_the_restriction_notice_instead_of_a_missing_headline() -> None:
    _run_page_harness(
        r"""
const restricted = page(process.argv[1], "forecast-ledger-latest.json", {}, () => json(RESTRICTED));
await settle();
assert.equal(restricted.node("headline").textContent, "Publication restricted. " + RESTRICTED.reason);
assert.equal(restricted.node("headline").className, "fc-lede");
assert.deepEqual(restricted.node("rows").children.map(row => row.textContent), [
  "Scores withheld under the current source policy.",
  ...RESTRICTED.limitations,
  "Rights evaluated 2026-11-02T09:28:44Z."
]);
assert.equal(restricted.node("rights").hidden, false);

const live = page(process.argv[1], "forecast-ledger-latest.json", {}, () => json({
  headline: "2 signals scored",
  signals: { ooni_gfw: { wis: 0.79, empirical_coverage: 0.8, beats_baseline: true, n_forecasts: 881, n_misses: 176 } }
}));
await settle();
assert.equal(live.node("headline").textContent, "2 signals scored");
assert.deepEqual(live.node("rows").children[0].children.map(cell => cell.textContent),
  ["ooni_gfw", "0.79", "0.8", "yes", "881", "176"]);
assert.equal(live.nodes.has("rights"), false);

const empty = page(process.argv[1], "forecast-ledger-latest.json", {}, () => json({ signals: {} }));
await settle();
assert.equal(empty.node("headline").textContent, "Forecast ledger has no headline.");
""",
        str(ROOT / "forecast-scorecard.html"),
    )
