"""Build the China economic desk from the exact collected table snapshot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
from pathlib import Path

from processors.china_economic_health import build_analysis
from scripts import site_nav

ROOT = Path(__file__).resolve().parents[1]


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def source_link(item: dict) -> str:
    return f'<a href="{esc(item["source_url"])}" target="_blank" rel="noreferrer">NBS source</a>'


def render(snapshot: dict, analysis: dict) -> str:
    coverage = snapshot["coverage"]
    statuses = {row["family"]: row for row in snapshot["family_status"]}
    findings = []
    for finding in analysis["findings"]:
        citation = finding["evidence"][0]
        findings.append(f'<article class="ed-finding"><h3>{esc(finding["title"])}</h3><p>{esc(finding["text"])}</p><p class="ed-note">{esc(finding["limit"])}</p><p>{source_link(citation)} · Released {esc(citation["released_at"][:10])} · {esc(statuses[finding["family"]]["status"])}</p></article>')
    housing = []
    for group in analysis["housing"]:
        tiles = []
        for point in group["points"]:
            change = point["value"] - 100
            tone = "down" if change < 0 else "up" if change > 0 else "flat"
            tiles.append(f'<span class="ed-city ed-city--{tone}" title="{esc(point["row_label"])}: {change:+.1f}% from previous month"><b>{esc(point["row_label"])}</b><span>{change:+.1f}%</span></span>')
        housing.append(f'<section class="ed-housing"><h3>{esc(group["label"])}</h3><p>{group["falling"]} falling · {group["rising"]} rising · {group["unchanged"]} unchanged. Month-on-month changes from source indices (100 = previous month).</p><div class="ed-cities">{"".join(tiles)}</div></section>')
    profit_series = next((s for s in analysis["series"] if s["id"] == "industry-profit-growth"), None)
    bars = []
    if profit_series:
        maximum = max(abs(p["value"]) for p in profit_series["points"]) or 1
        for point in profit_series["points"]:
            width = abs(point["value"]) / maximum * 50
            left = 50 - width if point["value"] < 0 else 50
            tone = "down" if point["value"] < 0 else "up"
            bars.append(f'<li><span>{esc(point["row_label"])}</span><span class="ed-track"><i class="ed-bar ed-bar--{tone}" style="left:{left:.3f}%;width:{width:.3f}%"></i></span><b>{point["value"]:+g}%</b></li>')
    tables, options = [], []
    for release in snapshot["releases"]:
        family = release["family"]
        label = statuses[family]["label"]
        options.append(f'<option value="{esc(family)}">{esc(label)}</option>')
        rendered = []
        for table in release["tables"]:
            header = ''.join(f'<th scope="col">{esc(c)}</th>' for c in table["columns"])
            rows = ''.join('<tr>' + ''.join(f'<{"th scope=" + chr(34) + "row" + chr(34) if i == 0 else "td"}>{esc(v)}</{"th" if i == 0 else "td"}>' for i, v in enumerate(row["values"])) + '</tr>' for row in table["rows"])
            rendered.append(f'<details><summary>{esc(table["context"] or table["table_id"])} <small>({len(table["rows"])} rows)</small></summary><div class="ed-table-scroll" tabindex="0" role="region" aria-label="{esc(label)} source table"><table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></div></details>')
        size_metrics = release.get("narrative_metrics", [])
        sizes = '<p><b>Manufacturing PMI by firm size:</b> ' + ' · '.join(f'{esc(m["firm_size"])}: {m["value"]:g}' for m in size_metrics) + ' (NBS enterprise-size paragraph; diffusion index).</p>' if size_metrics else ''
        tables.append(f'<section class="ed-release" data-family="{esc(family)}"><h3>{esc(label)}</h3><p>{esc(release["title"])}</p><p class="ed-note">Released {esc(release["released_at"])} · First captured {esc(release["collected_at"])} · {esc(statuses[family]["status"])} · {release["numeric_cells"]} numeric cells</p><p>{source_link(release)} · {esc(release["rights"]["attribution"])}</p>{sizes}{"".join(rendered)}</section>')
    comparison = ''.join(f'<tr><th scope="row">{esc(r["dimension"])}</th><td>{esc(r["available"])}</td><td>{esc(r["gap"])}</td></tr>' for r in analysis["coverage_comparison"])
    missing = ', '.join(row["label"] + ' (' + row["status"] + ')' for row in snapshot["family_status"] if row["status"] != "current")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>China economic health | Palimpsest</title><meta name="description" content="Explore China's industrial profits, working capital, production, demand, investment, inflation and 70-city housing with original NBS tables and dated evidence.">
<link rel="canonical" href="https://www.palimpsest.info/china/economy/"><link rel="stylesheet" href="/dashboards/assets/tikto.css"><link rel="stylesheet" href="/assets/shell.css"><link rel="stylesheet" href="/assets/economic-desk.css"><script src="/assets/shell.js" defer></script><script src="/assets/economic-desk.js" defer></script></head>
<body class="ps economic-desk">{site_nav.render('/china/economy/')}
<main id="main" class="ed-main"><header class="ed-intro"><div><p class="ed-breadcrumb"><a href="/china/">China Observatory</a> / Economic health</p><h1>Inside China’s economy.</h1><p class="ed-deck">Follow the detail behind growth: industrial cash flow, household demand, investment, prices and the housing market in 70 cities.</p><p>Read the findings, compare the cross-sections, then inspect the source tables. Every value keeps its original heading and release date.</p></div><aside class="ed-receipt"><h2>Current evidence</h2><dl><dt>Statistical families</dt><dd>{coverage['families_available']} of {coverage['families_expected']}</dd><dt>Current numeric cells</dt><dd>{coverage['latest_numeric_cells']:,}</dd><dt>Retained release vintages</dt><dd>{coverage['retained_vintages']:,}</dd><dt>Independent source groups</dt><dd>{coverage['independent_source_groups']}</dd><dt>Collection checked</dt><dd>{esc(snapshot['generated_at'])}</dd></dl><p>Source: National Bureau of Statistics of China. These are official aggregates.</p></aside></header>
<nav class="ed-sections" aria-label="Economic desk sections"><a href="/china/evidence/">New: evidence observatory</a><a href="#findings">Findings</a><a href="#cross-sections">Cross-sections</a><a href="#source-tables">Source tables</a><a href="#coverage">Coverage and gaps</a><a href="/research/connected/">China, CPEC & Balochistan</a><a href="https://narcoscope.com/#bri">NarcoScope research</a></nav>
<p class="ed-status">Collection status: <b>{esc(snapshot['status'])}</b>. {esc('Needs attention: ' + missing if missing else 'All reviewed release families were collected within their freshness windows.')}</p>
<section id="findings"><h2>What the evidence says</h2><div class="ed-findings">{''.join(findings) or '<p>Analytical cuts are unavailable until compatible source tables are captured.</p>'}</div></section>
<section id="cross-sections"><h2>Look beneath the aggregate</h2>{''.join(housing)}<details class="ed-profit-chart"><summary>Compare industrial profit growth by industry</summary><p>Year-on-year change over the source’s cumulative period. This is a cross-section of reported growth rates; industry sizes differ.</p><ul class="ed-bars">{''.join(bars)}</ul></details></section>
<section id="source-tables"><h2>The source tables</h2><p>Monthly, year-to-date, year-on-year and index-base headings are preserved. A dash or source footnote remains unavailable, never zero.</p><div class="ed-controls"><label>Economic area <select id="ed-family"><option value="all">All areas</option>{''.join(options)}</select></label><label>Find a city, industry or measure <input type="search" id="ed-search" placeholder="For example: receivable, Beijing, steel"></label><span id="ed-result-count" aria-live="polite"></span></div><p class="ed-downloads"><a href="/readings/china-economic-health-latest.json" download>Download source-table JSON</a><a href="/china/economy/data.csv" download>Download table cells as CSV</a><a href="/readings/china-economic-history.csv" download>Download all retained release vintages (CSV)</a><a href="/readings/china-economic-analysis-latest.json" download>Download analysis and evidence</a></p>{''.join(tables)}</section>
<section id="coverage"><h2>Depth, provenance and remaining gaps</h2><p>Palimpsest aims to make China’s economic evidence useful at a granular level. China Beige Book’s private respondent network measures information that these public tables cannot supply. More table cells do not mean more independent evidence.</p><div class="ed-table-scroll" tabindex="0" role="region" aria-label="Economic coverage comparison"><table><thead><tr><th>Dimension</th><th>Public evidence available here</th><th>Remaining gap</th></tr></thead><tbody>{comparison}</tbody></table></div><p><a href="https://www.chinabeigebook.com/analytics-platform/">China Beige Book’s published platform description</a> · <a href="https://www.stats.gov.cn/english/nbs/200701/t20070104_59236.html">NBS statistical-data terms</a></p><p>Economic evidence can inform questions about trade, debt and project demand. A shared country or date does not establish a link to narcotics, an armed group, or an individual.</p></section></main><footer class="ed-footer">Palimpsest · Public economic research with inspectable evidence. NBS data retain their source terms; no downstream sublicense is granted.</footer></body></html>'''


def csv_bytes(snapshot: dict) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["family", "release_title", "released_at", "collected_at", "table", "table_context", "source_row", "source_column", "row_label", "column_label", "raw_value", "value", "status", "source_url", "raw_sha256"])
    def safe(value):
        return "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
    for release in snapshot["releases"]:
        for table in release["tables"]:
            for cell in table["cells"]:
                writer.writerow([safe(v) for v in [release["family"], release["title"], release["released_at"], release["collected_at"], table["table_id"], table["context"], cell["source_row"], cell["source_column"], cell["row_label"], cell["column_label"], cell["raw_value"], cell["value"], cell["status"], release["source_url"], release["raw_sha256"]]])
    return out.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "readings/china-economic-health-latest.json")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    raw = args.input.read_bytes()
    snapshot = json.loads(raw)
    if "history_export" in snapshot:
        history = snapshot["history_export"]
        if history["path"] != "/readings/china-economic-history.csv" or hashlib.sha256((args.root / "readings/china-economic-history.csv").read_bytes()).hexdigest() != history["sha256"]:
            raise ValueError("historical CSV does not match the economic snapshot")
    analysis = build_analysis(snapshot)
    analysis["input_sha256"] = hashlib.sha256(raw).hexdigest()
    outputs = {args.root / "china/economy/index.html": render(snapshot, analysis),
               args.root / "china/economy/data.csv": csv_bytes(snapshot),
               args.root / "readings/china-economic-analysis-latest.json": json.dumps(analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"}
    for path, text in outputs.items():
        if args.check:
            if not path.exists() or path.read_text() != text:
                raise ValueError(f"economic desk output drift: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    print(f"China economic desk: {len(analysis['findings'])} findings, {snapshot['coverage']['latest_numeric_cells']} current numeric cells")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
