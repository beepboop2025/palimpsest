"""Build the shared China, CPEC, Balochistan and BRI research desk offline."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from processors.connected_research import build_connected
from core.narcoscope_corridor_bridge import load_bundle
from scripts import site_nav
from scripts.build_china_economic_health import esc

ROOT = Path(__file__).resolve().parents[1]
INPUTS = {"wire": "readings/regional-research-wire-latest.json", "economy": "readings/regional-economic-context-latest.json",
          "china": "readings/china-economic-analysis-latest.json", "partner": "integrations/intelligence-commons/narcoscope-palimpsest-corridors-v2.json",
          "observatory": "readings/china-evidence-observatory-latest.json"}


def reporting(items: list[dict]) -> str:
    return '<ul class="cr-reporting">' + ''.join(f'<li><a href="{esc(row["url"])}" target="_blank" rel="noreferrer">{esc(row["title"])}</a><small>{esc(row["source_name"])} · {esc(row["published_at"][:10])}</small></li>' for row in items) + '</ul>'


def render(data: dict) -> str:
    sections = []
    if data.get("observatory"):
        findings = ''.join(f'<article class="ed-finding"><h3>{esc(f["title"])}</h3><p>{esc(f["text"])}</p><p class="ed-note">{esc(f["interpretation"])}</p><a href="{esc(f["data_path"])}">Inspect the evidence</a></article>' for f in data["observatory"]["findings"])
        sections.append(f'<section><h2>Investigate the economic and publication record</h2><p>Compare historical business conditions, city housing persistence, EU mirror trade and documented changes to official statistics.</p><p><a href="/china/evidence/">Open the China evidence observatory and full data library</a></p><div class="ed-findings">{findings}</div></section>')
    for region in data["regions"]:
        questions = []
        trends = ''.join(f'<article class="ed-finding"><h3>{esc(f["title"])}</h3><p>{esc(f["text"])}</p><p class="ed-note">{esc(f["interpretation"])}</p><a href="{esc(f["evidence"][0]["source_url"])}">World Bank national series</a></article>' for f in region["economic_findings"])
        for question in region["questions"]:
            gaps = ''.join(f'<li>{esc(gap)}</li>' for gap in question["missing_evidence"])
            questions.append(f'<article class="cr-question"><h3>{esc(question["title"])}</h3><p><b>{esc(question["question"])}</b></p><p>{esc(question["assessment"])}</p>{reporting(question["recent_reporting"])}<details><summary>Evidence needed to resolve this question</summary><ul>{gaps}</ul></details></article>')
        if trends:
            questions.insert(0, f'<section class="cr-economic-trends"><h3>What the annual economic record shows</h3><p>National observations, with reference years shown. These comparisons describe financing conditions and household price pressure; they do not establish project effects.</p><div class="ed-findings">{trends}</div></section>')
        indicators = []
        for row in region["national_indicators"]:
            value = row["latest_available"]
            display = f'{value["value"]:,.2f}' if value else 'Unavailable'
            period = value["period_end"][:4] if value else '—'
            indicators.append(f'<tr><th scope="row"><a href="{esc(row["source_url"])}">{esc(row["name"])}</a></th><td>{esc(row["country_code"])}</td><td>{display}</td><td>{esc(row["unit"])}</td><td>{period}</td><td>{esc(row["last_requested_period"]["evidence_state"])}</td></tr>')
        sections.append(f'<section class="cr-region" id="{esc(region["region"])}"><h2>{esc(region["title"])}</h2><p class="ed-deck">{esc(region["thesis"])}</p><p class="ed-note">Captured in the last 30 days: {region["last_30_days_items"]} items from {region["last_30_days_independence_groups"]} publisher groups. These counts describe collection coverage.</p><p class="ed-downloads"><a href="{esc(region["palimpsest_path"])}">Open the full Palimpsest dossier</a><a href="https://narcoscope.com/#{esc(region["narcoscope_tab"])}">Open related NarcoScope evidence</a></p><div class="cr-questions">{"".join(questions)}</div><details class="cr-national"><summary>National economic context: {len(indicators)} indicator series</summary><p>Latest available annual observations. The final column shows whether the requested final year has a value. Pakistan country data do not measure Balochistan districts or CPEC projects.</p><div class="ed-table-scroll" role="region" aria-label="National economic context" tabindex="0"><table><thead><tr><th>Indicator</th><th>Country</th><th>Latest available</th><th>Unit</th><th>Year</th><th>Final requested year</th></tr></thead><tbody>{"".join(indicators)}</tbody></table></div></details></section>')
    china = ''.join(f'<article class="ed-finding"><h3>{esc(f["title"])}</h3><p>{esc(f["text"])}</p><p class="ed-note">{esc(f["limit"])}</p><a href="{esc(f["evidence"][0]["source_url"])}">Inspect the official table</a></article>' for f in data["china_findings"])
    partner = ''.join(f'<li><b>{esc(row["topic"].replace("_", " "))}</b><p>{esc(row["measurement"]["method"])}</p></li>' for row in data["narcoscope"]["datasets"])
    sources = ''.join(f'<tr><th scope="row"><a href="{esc(s["feed_url"])}">{esc(s["name"])}</a></th><td>{esc(s["role"])}</td><td>{esc(s["status"])}</td><td>{esc(s["relevant_items"] if s["relevant_items"] is not None else "Unavailable")}</td></tr>' for s in data["source_status"])
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>China, CPEC, Balochistan & BRI research | Palimpsest × NarcoScope</title><meta name="description" content="Connected research on China's economy, CPEC project finance, Gwadar, Balochistan livelihoods and rights, BRI lending and Myanmar. Source-linked questions, national data and dated reporting."><link rel="canonical" href="https://www.palimpsest.info/research/connected/"><link rel="stylesheet" href="/dashboards/assets/tikto.css"><link rel="stylesheet" href="/assets/shell.css"><link rel="stylesheet" href="/assets/economic-desk.css"><link rel="stylesheet" href="/assets/connected-research.css"><script src="/assets/shell.js" defer></script></head><body class="ps economic-desk">{site_nav.render('/research/connected/')}<main id="main" class="ed-main"><header class="cr-intro"><p><a href="/china/economy/">China economic desk</a> / Connected regional research</p><h1>Follow the money.<br>Examine the local record.</h1><p class="ed-deck">China’s economy, CPEC and Gwadar, Balochistan, the Belt and Road, and Myanmar—organized around questions that evidence can answer.</p><p>Palimpsest brings economic tables, project scrutiny and regional reporting. NarcoScope contributes its separately attributed official illicit-economy datasets. Use the shared research paths to examine each record.</p></header><nav class="ed-sections" aria-label="Research regions">{''.join(f'<a href="#{esc(r["region"])}">{esc(r["region"].title())}</a>' for r in data['regions'])}<a href="#sources">Source coverage</a><a href="/readings/connected-research-latest.json" download>Research data</a></nav><p class="ed-note">Regional collection: {esc(data['source_clocks']['regional_collection'])}. Economic retrieval: {esc(data['source_clocks']['economic_retrieval'])}. Individual observations retain their own dates.</p><section><h2>China’s current statistical detail</h2><div class="ed-findings">{china}</div><a href="/china/economy/">Explore the full economic desk and source tables</a></section>{''.join(sections)}<section id="narcoscope"><h2>Research across Palimpsest and NarcoScope</h2><p>Open official drug-price, seizure, precursor, designation and wildlife coverage in the same research workflow. The partner dataset is dated {esc(data['narcoscope']['data_as_of'])}; it has its own scope and reporting periods.</p><ul class="cr-partner">{partner}</ul><p><a href="{esc(data['narcoscope']['source_url'])}">Download the NarcoScope aggregate</a> · <a href="https://narcoscope.com/#bri">Continue in NarcoScope</a></p><p>Shared geography, timing or keywords do not establish a criminal, political or causal relationship. Project-level claims still need direct project evidence.</p></section><section id="sources"><h2>Source coverage and collection state</h2><div class="ed-table-scroll" tabindex="0" role="region" aria-label="Source collection status"><table><thead><tr><th>Publisher feed</th><th>Source role</th><th>Collection</th><th>Relevant items in fetched feed</th></tr></thead><tbody>{sources}</tbody></table></div><p class="ed-downloads"><a href="/readings/regional-research-wire-latest.json">Reporting metadata</a><a href="/readings/regional-economic-context-latest.json">24 national economic indicators and histories</a><a href="/readings/connected-research-latest.json">Shared research snapshot</a></p></section></main><footer class="ed-footer">Palimpsest × NarcoScope · NBS statistical-data terms, World Bank CC BY 4.0, and attributed publisher metadata. Reporting is not exhaustive; unavailable values remain unavailable.</footer></body></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    raw = {key: (args.root / value).read_bytes() for key, value in INPUTS.items()}
    commons = args.root / "integrations/intelligence-commons"
    load_bundle(commons / "narcoscope-palimpsest-corridors-v2.json", commons / "narcoscope-palimpsest-corridors-v2.schema.json", commons / "narcoscope-corridors-pin-v2.json")
    data = build_connected(**{key: json.loads(value) for key, value in raw.items()}, input_hashes={key: hashlib.sha256(value).hexdigest() for key, value in raw.items()})
    outputs = {args.root / "readings/connected-research-latest.json": json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
               args.root / "research/connected/index.html": render(data)}
    for path, value in outputs.items():
        if args.check:
            if not path.exists() or path.read_text() != value:
                raise ValueError(f"connected research output drift: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
    print(f"Connected research: {len(data['regions'])} regions, {sum(len(r['questions']) for r in data['regions'])} research questions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
