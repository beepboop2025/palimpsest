"""Build the China evidence observatory from validated, independently dated inputs."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path

from processors.china_economic_history import build_history
from processors.china_external_accounts import validate_public
from processors.china_mirror_trade import publication_source_group
from processors.china_publication_watch import validate_document
from scripts import site_nav

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "palimpsest.china-evidence-observatory.v1"
INPUTS = {"health": "china-economic-health-latest.json", "history": "china-economic-history.csv",
          "watch": "china-publication-watch-latest.json", "trade": "china-mirror-trade-latest.json",
          "external": "china-external-accounts-latest.json"}


def esc(value):
    return html.escape(str(value), quote=True)


def serial(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def build(root):
    raw = {key: (root / "readings" / name).read_bytes() for key,name in INPUTS.items()}
    health, watch, trade, external = (json.loads(raw[key]) for key in ("health", "watch", "trade", "external"))
    historical = build_history(health, raw["history"])
    validate_document(watch); publication_source_group(trade); validate_public(external)
    export = trade["history_export"]
    if export["path"] != "/readings/china-mirror-trade-history.csv" or hashlib.sha256((root / export["path"].lstrip("/")).read_bytes()).hexdigest() != export["sha256"]:
        raise ValueError("mirror trade historical bytes do not match the snapshot")
    datasets = [
        {"id": "nbs-history", "title": "Inside the official economic record", "status": health["status"], "visibility": "public",
         "clock": health["generated_at"], "source_group": "nbs_official_statistics", "path": "/readings/china-economic-history-analysis-latest.json",
         "download": "/readings/china-economic-history.csv", "coverage": historical["coverage"],
         "description": "Source-table archive, comparable monthly business indicators and 70-city housing histories. Repeated months are deduplicated; source vintages remain inspectable."},
        {"id": "mirror-trade", "title": "What Europe records about trade", "status": trade["status"], "visibility": "public",
         "clock": trade["generated_at"], "source_group": "eurostat_eu_reported_trade", "path": "/readings/china-mirror-trade-latest.json",
         "download": export["path"], "coverage": trade["coverage"],
         "description": "EU declarations for China, Pakistan and Myanmar: monthly product values, weights, trading partners and reporter-country detail. EU totals and member countries overlap."},
        {"id": "publication-watch", "title": "What changed in the public record", "status": watch["status"], "visibility": "metadata_public",
         "clock": watch["generated_at"], "source_group": "official_document_observation", "path": "/readings/china-publication-watch-latest.json",
         "download": "/readings/china-publication-watch-latest.json", "coverage": watch["coverage"],
         "description": "Document-level baselines, content changes, index changes and repeat URL-absence checks. Network failures and access limits remain separate."},
        {"id": "external-accounts", "title": "Regional cross-border payments", "status": external["status"], "visibility": "numeric_data_private",
         "clock": external["generated_at"], "source_group": "safe_official_statistics", "path": "/readings/china-external-accounts-latest.json",
         "download": "/readings/china-external-accounts-latest.json", "coverage": external["coverage"],
         "description": "SAFE workbooks are retained for private research with original hashes. Public output shows acquisition metadata; numeric tables and derived analysis require publication permission."},
    ]
    findings = [{"id": "nbs:" + f["id"], "title": f["title"], "text": f["text"], "interpretation": f["interpretation"],
                 "evidence_class": "official_statistical_comparison", "source_urls": sorted({e["source_url"] for e in f["evidence"]}),
                 "data_path": "/readings/china-economic-history-analysis-latest.json", "region": "china"} for f in historical["findings"]]
    findings.extend({"id": "trade:" + f["id"], "title": f["title"], "text": f["text"], "interpretation": f["limit"],
                     "evidence_class": "externally_reported_trade", "source_urls": [trade["source"]["dataset_url"]],
                     "data_path": "/readings/china-mirror-trade-latest.json", "region": "regional", "evidence_series": f["evidence_series"]} for f in trade["findings"])
    result = {"schema": SCHEMA, "generated_at": max(d["clock"] for d in datasets),
              "input_sha256": {key: hashlib.sha256(value).hexdigest() for key,value in raw.items()},
              "datasets": datasets, "findings": findings, "methodology_cases": watch["methodology_cases"],
              "publication_observations": watch["documents"],
              "use_policy": {"concealment_inference": "not_established_by_gaps_or_disagreement", "actor_inference": "prohibited",
                             "private_numeric_data": "excluded", "missing_values": "unavailable_not_zero"},
              "research_tests": [
                  {"title": "Test the demand story", "test": "Compare production, domestic orders, export orders and EU-reported demand by product and month.", "counterevidence": "Inventory rebuilding, seasonal patterns and goods sold outside Europe can explain divergence."},
                  {"title": "Test the cash-flow story", "test": "Follow receivable days, inventory turnover and margins; compare the same calendar period across years.", "counterevidence": "Industry composition and changes in the above-size enterprise sample can move aggregate ratios."},
                  {"title": "Test the property story", "test": "Track consecutive city price falls alongside official construction, sales and investment tables.", "counterevidence": "A price index does not measure transaction volume, developer liquidity or household equity losses."},
                  {"title": "Test a publication-change claim", "test": "Inspect the before/after capture receipts, official methodological explanations and repeated checks from the watch.", "counterevidence": "Routine revisions, site migrations and index rotation are alternative explanations; intent needs additional evidence."},
                  {"title": "Connect the regional record", "test": "Use Pakistan and Myanmar mirror trade with CPEC, Balochistan, Gwadar and BRI reporting to identify questions for project documents.", "counterevidence": "National trade does not identify a particular port, district, financing contract or illicit actor."},
              ]}
    return result, historical, trade


def render(data, historical, trade):
    rendered_findings = [f'<article class="eo-finding"><h3>{esc(f["title"])}</h3><p>{esc(f["text"])}</p><p class="eo-note">{esc(f["interpretation"])}</p><a href="{esc(f["source_urls"][0])}">Original source</a> · <a href="{esc(f["data_path"])}">Inspect the evidence</a></article>' for f in data["findings"]]
    findings = ''.join(rendered_findings[:6])
    if len(rendered_findings) > 6:
        findings += f'<details class="eo-more-findings"><summary>Explore {len(rendered_findings)-6} more trade comparisons</summary><div class="eo-findings">' + ''.join(rendered_findings[6:]) + '</div></details>'
    sources = []
    for d in data["datasets"]:
        coverage = ''.join(f'<li>{esc(key.replace("_", " "))}: <b>{esc(value if not isinstance(value,list) else ", ".join(map(str,value)))}</b></li>' for key,value in d["coverage"].items())
        sources.append(f'<article><h3>{esc(d["title"])}</h3><p>{esc(d["description"])}</p><p class="eo-note">{esc(d["status"])} · Checked {esc(d["clock"])}</p><details><summary>Measured collection coverage</summary><ul>{coverage}</ul></details><p><a href="{esc(d["path"])}">Dataset and provenance</a> · <a href="{esc(d["download"])}" download>Download {"metadata" if d["visibility"] == "numeric_data_private" else "data"}</a></p></article>')
    cases = ''.join(f'<article class="eo-case"><time>{esc(c["event_date"])}</time><h3>{esc(c["title"])}</h3><p>{esc(c["claim"])}</p><p>{esc(c["interpretation"])}</p><p class="eo-note">Evidence capture: {esc(c["evidence_status"])}</p>' + ' · '.join(f'<a href="{esc(url)}">Official record {i+1}</a>' for i,url in enumerate(c["source_urls"])) + '</article>' for c in data["methodology_cases"])
    watch_rows = ''.join(f'<tr><th scope="row"><a href="{esc(d["url"])}">{esc(d["title"])}</a><small>{esc(d["publisher"])}</small></th><td>{esc(d["availability"])}</td><td>{esc(d["event"])}</td><td>{esc(d["last_success_at"] or "Not captured")}</td><td><code>{esc((d["text_sha256"] or "")[:12])}</code></td></tr>' for d in data["publication_observations"])
    cities = ''.join(f'<tr data-city="{esc(s["city"].lower())}"><th scope="row">{esc(s["city"])}</th><td>{esc(s["kind"])}</td><td>{esc(s["points"][-1]["period"] if s["points"] else "Unavailable")}</td><td>{esc(s["points"][-1]["value"] if s["points"] else "Unavailable")}</td><td>{s["consecutive_observed_declines"]}</td><td>{len(s["points"])}</td></tr>' for s in historical["housing_history"])
    tests = ''.join(f'<details><summary>{esc(t["title"])}</summary><p>{esc(t["test"])}</p><p class="eo-note">Check alternatives: {esc(t["counterevidence"])}</p></details>' for t in data["research_tests"])
    options = ''.join(f'<option value="{esc(s["id"])}">{esc(s["label"])}</option>' for s in historical["series"])
    revisions = historical["coverage"]["observed_revision_pairs"]
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>China evidence observatory | Palimpsest</title><meta name="description" content="Investigate China's economic detail, EU mirror trade, housing persistence and changes in official publications. Inspect historical data, source captures and competing explanations."><link rel="canonical" href="https://www.palimpsest.info/china/evidence/"><link rel="stylesheet" href="/dashboards/assets/tikto.css"><link rel="stylesheet" href="/assets/shell.css"><link rel="stylesheet" href="/assets/evidence-observatory.css"><script src="/assets/shell.js" defer></script><script src="/assets/evidence-observatory.js" defer></script></head><body class="ps">{site_nav.render('/china/evidence/')}<main id="main" class="eo-main"><header class="eo-intro"><p><a href="/china/">China Observatory</a> / Evidence desk</p><h1>Read beneath the headline.</h1><p class="eo-deck">Compare the economic detail. Track what changes in the official record. Test the explanation against outside evidence.</p><p>China’s national figures tell part of the story. This desk opens the underlying industry, city and trade histories—and records what those sources can and cannot establish.</p></header><nav class="eo-nav" aria-label="Evidence desk"><a href="#findings">Findings</a><a href="#history">Economic histories</a><a href="#housing">City persistence</a><a href="#publication">Publication changes</a><a href="#datasets">Data library</a><a href="/research/connected/">CPEC, Balochistan & BRI</a></nav><section id="findings"><h2>Questions the data can answer</h2><div class="eo-findings">{findings}</div></section><section id="history"><h2>Follow the detail over time</h2><p>Each chart uses explicit source periods and the latest retained vintage. A repeated publication of a month counts once. Full table cells remain available in the archive.</p><label for="eo-series">Measure</label><select id="eo-series">{options}</select><div id="eo-chart" aria-live="polite"><p>Choose a measure to load its history. <a href="/readings/china-economic-history-analysis-latest.json">The full history is also available as JSON.</a></p></div><p class="eo-note">Observed same-URL numeric revision pairs: {revisions}. A zero means none in the captured comparisons; it does not prove the historical record never changed.</p><p><a href="/china/economy/">Open current source tables and industry comparisons</a> · <a href="/readings/china-economic-history.csv" download>Download every retained source-table vintage</a></p></section><section id="housing"><h2>Housing: breadth and persistence</h2><p>Monthly price indexes use previous month = 100. Streaks stop at a missing month. They describe observed prices, not the value of losses or sales.</p><label>Find a city <input type="search" id="eo-city" placeholder="Beijing, Chengdu, Urumqi…"></label><div class="eo-scroll" role="region" aria-label="Housing history by city" tabindex="0"><table id="eo-housing"><thead><tr><th>City</th><th>Market</th><th>Latest month</th><th>Price index</th><th>Consecutive observed declines</th><th>Captured months</th></tr></thead><tbody>{cities}</tbody></table></div></section><section id="publication"><h2>When the official record changes</h2><p>These cases cite historical official announcements. The live watch starts from its own captured baseline and keeps later document changes separate from those historical accounts.</p><div class="eo-cases">{cases}</div><details><summary>Inspect {len(data['publication_observations'])} watched documents</summary><p>Access limits and transport failures do not establish removal. An observed removal requires a previous successful capture and repeated 404/410 responses.</p><div class="eo-scroll" role="region" aria-label="Official publication observations" tabindex="0"><table><thead><tr><th>Document</th><th>Availability</th><th>Observed event</th><th>Last successful check</th><th>Text fingerprint</th></tr></thead><tbody>{watch_rows}</tbody></table></div></details></section><section id="datasets"><h2>The research library</h2><div class="eo-library">{''.join(sources)}</div></section><section class="eo-tests"><h2>Make the investigation falsifiable</h2><p>A gap, revision or disagreement can generate a research question. Establishing concealment requires evidence of what was withheld or changed, by whom, and why.</p>{tests}</section><aside class="eo-connected"><h2>Follow the regional consequences</h2><p>Bring these questions into CPEC project finance, Gwadar utilisation, Balochistan livelihoods, BRI debt and Myanmar border economies. NarcoScope’s separately attributed evidence is available in the same research workflow.</p><a href="/research/connected/">Open connected regional research</a> · <a href="https://www.narcoscope.com/#bri">Continue in NarcoScope</a></aside></main><footer class="eo-footer">Palimpsest · NBS statistical-data terms and Eurostat reuse conditions apply. SAFE quantities remain private. Public sources do not replicate China Beige Book’s private company panel.</footer></body></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--root", type=Path, default=ROOT); parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    data, historical, trade = build(args.root)
    outputs = {"readings/china-evidence-observatory-latest.json": serial(data),
               "readings/china-economic-history-analysis-latest.json": serial(historical),
               "china/evidence/index.html": render(data, historical, trade)}
    for name,value in outputs.items():
        path = args.root / name
        if args.check:
            if not path.exists() or path.read_text() != value: raise ValueError(f"evidence observatory output drift: {name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value)
    print(serial({"datasets":len(data["datasets"]), "findings":len(data["findings"]), "history":historical["coverage"]}).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
