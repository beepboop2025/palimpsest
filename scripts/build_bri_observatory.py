#!/usr/bin/env python3
"""Build the public Belt and Road coverage contract and source-ledger page."""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from processors.bri_observatory import (
    PUBLIC_BUILD_STATES,
    WDI_ARTIFACT_PATH,
    WDI_OBSERVATION_SCHEMA_PATH,
    WDI_PUBLICATION_RECEIPT_PATH,
    WDI_SERIES_REGISTRY_PATH,
    build_public_artifact,
    build_wdi_observation_descriptor,
    load_registry,
)
from scripts import site_nav


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "bri_observatory.json"
DEFAULT_JSON = ROOT / "readings" / "belt-and-road-observatory-latest.json"
DEFAULT_HTML = ROOT / "belt-and-road" / "index.html"
DEFAULT_WDI_BUNDLE = ROOT / WDI_ARTIFACT_PATH
DEFAULT_WDI_SCHEMA = ROOT / WDI_OBSERVATION_SCHEMA_PATH
DEFAULT_WDI_SERIES_REGISTRY = ROOT / WDI_SERIES_REGISTRY_PATH
DEFAULT_WDI_PUBLICATION_RECEIPT = ROOT / WDI_PUBLICATION_RECEIPT_PATH
_AUTO_WDI_PUBLICATION_RECEIPT = object()
_PUBLICATION_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _resolve_wdi_publication_receipts(
    publication_receipt_path: Path | None | object,
    archived_size_receipt_path: Path | None,
    *,
    source_implementation: str,
) -> tuple[Path | None, Path | None]:
    """Resolve no-flag production proof without weakening explicit fixture mode."""

    if publication_receipt_path is not _AUTO_WDI_PUBLICATION_RECEIPT:
        return publication_receipt_path, archived_size_receipt_path
    if source_implementation != "live":
        return None, archived_size_receipt_path
    if not DEFAULT_WDI_PUBLICATION_RECEIPT.is_file():
        return None, archived_size_receipt_path
    if archived_size_receipt_path is not None:
        return DEFAULT_WDI_PUBLICATION_RECEIPT, archived_size_receipt_path
    try:
        document = json.loads(
            DEFAULT_WDI_PUBLICATION_RECEIPT.read_text(encoding="utf-8")
        )
        publication_sha = document["workflow"]["publication_sha"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(
            "cannot resolve the default WDI Pages publication receipt"
        ) from exc
    if type(publication_sha) is not str or not _PUBLICATION_SHA_RE.fullmatch(
        publication_sha
    ):
        raise ValueError(
            "default WDI Pages receipt publication_sha must be lowercase 40-hex"
        )
    size_receipt = ROOT / (
        ".well-known/receipts/pages-artifact-size-" + publication_sha + ".json"
    )
    return DEFAULT_WDI_PUBLICATION_RECEIPT, size_receipt


def _json_bytes(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _chips(values: list[str]) -> str:
    return "".join(f'<li><code>{_esc(value)}</code></li>' for value in values)


_IMPLEMENTATION_ORDER = (
    "live", "repository_ready", "adapter_ready", "link_only", "planned",
    "blocked", "out_of_scope",
)


def _state_summary(counts: Counter[str], *, preferred: tuple[str, ...] = ()) -> str:
    ordered = [key for key in preferred if counts.get(key)]
    ordered.extend(sorted(key for key in counts if key not in ordered))
    return " · ".join(
        f'{counts[key]} {_esc(key.replace("_", " "))}' for key in ordered
    )


def _target_projection(
    artifact: dict,
    *,
    target_ids: tuple[str, ...],
    card_class: str = "bri-card",
) -> dict:
    """Project exact target/source readiness into escaped human-readable cards."""

    targets_by_id = {row["target_id"]: row for row in artifact["watch_targets"]}
    sources_by_id = {row["source_id"]: row for row in artifact["sources"]}
    missing_targets = [
        target_id for target_id in target_ids if target_id not in targets_by_id
    ]
    if missing_targets:
        raise ValueError(f"regional projection is missing targets {missing_targets}")

    targets = [targets_by_id[target_id] for target_id in target_ids]
    target_source_ids = list(dict.fromkeys(
        source_id
        for target in targets
        for source_id in target["source_ids"]
    ))
    missing_sources = [
        source_id for source_id in target_source_ids if source_id not in sources_by_id
    ]
    if missing_sources:
        raise ValueError(f"regional projection is missing sources {missing_sources}")
    target_sources = [sources_by_id[source_id] for source_id in target_source_ids]
    implementation_counts = Counter(
        source["implementation"] for source in target_sources
    )
    rights_counts = Counter(source["rights_status"] for source in target_sources)
    target_status_counts = Counter(target["evidence_status"] for target in targets)
    build_ready_count = sum(
        count
        for state, count in implementation_counts.items()
        if state in PUBLIC_BUILD_STATES
    )

    cards = []
    for target in targets:
        target_sources_for_card = [
            sources_by_id[source_id] for source_id in target["source_ids"]
        ]
        card_states = Counter(
            source["implementation"] for source in target_sources_for_card
        )
        card_rights = Counter(
            source["rights_status"] for source in target_sources_for_card
        )
        source_routes = "; ".join(
            f'<a href="{_esc(source["url"])}" target="_blank" rel="noopener">'
            f'{_esc(source["name"])}</a>'
            for source in target_sources_for_card
        )
        cards.append(
            f'<article class="{_esc(card_class)}" '
            f'data-bri-region-target="{_esc(target["target_id"])}">'
            f'<p class="bri-eyebrow">{_esc(target["evidence_status"].replace("_", " "))}</p>'
            f'<h3>{_esc(target["label"])}</h3>'
            f'<p><b>Readiness:</b> {_state_summary(card_states, preferred=_IMPLEMENTATION_ORDER)}. '
            f'<b>Rights:</b> {_state_summary(card_rights)}.</p>'
            f'<p><b>Registered source routes:</b> {source_routes}.</p>'
            f'<ul class="bri-chips">{_chips(target["required_coverage"])}</ul>'
            '</article>'
        )

    return {
        "build_ready_count": build_ready_count,
        "cards": "".join(cards),
        "implementation_counts": implementation_counts,
        "rights_counts": rights_counts,
        "source_count": len(target_sources),
        "target_count": len(targets),
        "target_status_counts": target_status_counts,
    }


def _render_region_section(
    artifact: dict,
    *,
    anchor: str,
    eyebrow: str,
    title: str,
    introduction: str,
    geography_codes: tuple[str, ...],
    target_ids: tuple[str, ...],
) -> str:
    """Render a regional reading lane from the public artifact only.

    A missing target, source or geography is a build failure. Quietly dropping
    one would make the human page less complete than its machine contract.
    """

    geography_report = artifact["coverage_report"]["geographies"]
    missing_geographies = [
        code for code in geography_codes if code not in geography_report
    ]
    if missing_geographies:
        raise ValueError(
            f"regional section {anchor} is missing geographies {missing_geographies}"
        )
    projection = _target_projection(artifact, target_ids=target_ids)
    geography_source_ids = {
        source_id
        for code in geography_codes
        for source_id in geography_report[code]["source_ids"]
    }

    return f"""<section class="bri-section" id="{_esc(anchor)}" aria-labelledby="{_esc(anchor)}-title">
    <p class="bri-eyebrow">{_esc(eyebrow)}</p><h2 id="{_esc(anchor)}-title">{_esc(title)}</h2>
    <p>{_esc(introduction)} The broader registry associates {len(geography_source_ids)} source families with this geography; the {projection["target_count"]} target ledgers below name {projection["source_count"]} distinct source routes directly.</p>
    <dl class="bri-contract"><div><dt>Target status</dt><dd>{_state_summary(projection["target_status_counts"])}</dd></div><div><dt>Source readiness</dt><dd>{_state_summary(projection["implementation_counts"], preferred=_IMPLEMENTATION_ORDER)}</dd></div><div><dt>Public build-state gate</dt><dd>{projection["build_ready_count"]} of {projection["source_count"]} named routes</dd></div></dl>
    <p><strong>Rights states:</strong> {_state_summary(projection["rights_counts"])}. Build-ready describes a source route, not a verified project record. Link-only and planned routes are discovery, not ingestion; no status here proves construction, operation, attribution, local benefit or harm.</p>
    <div class="bri-grid">{projection["cards"]}</div>
  </section>"""


def _render_observation_datasets(artifact: dict) -> str:
    rows = artifact.get("observation_datasets")
    if not rows:
        return ""
    [dataset] = rows
    coverage = dataset["coverage"]
    rights = dataset["rights"]
    boundary = dataset["context_boundary"]
    receipt = dataset["publication_receipt"]
    if receipt is None:
        publication_proof = ""
    else:
        publication_proof = (
            '    <p><strong>Point-in-time publication proof:</strong> '
            f'<a href="{_esc(receipt["public_url"])}">Inspect the immutable receipt</a>. '
            f'Exact served bytes were verified at <code>{_esc(receipt["verified_at"])}</code>; '
            "the release-verification freshness window ends at "
            f'<code>{_esc(receipt["fresh_until"])}</code> and may already have ended. '
            "This is release-time proof, not continuous monitoring "
            f'(<code>{_esc(receipt["availability_semantics"])}</code>).</p>\n'
        )
    return f"""<section class="bri-section" id="economic-context" aria-labelledby="economic-context-title">
    <p class="bri-eyebrow">Normalized economic context · {_esc(dataset["publication_state"].replace("_", " "))}</p><h2 id="economic-context-title">Country-period context with forecasts, nulls and source clocks intact</h2>
    <p>This exact World Bank WDI bundle contains {_esc(coverage["source_rows"])} country-series-year rows across China, Myanmar and Pakistan. It keeps {_esc(coverage["forecast_rows"])} source-marked forecasts separate from {_esc(coverage["observed_rows"])} observed values and retains {_esc(coverage["unavailable_rows"])} unavailable rows as null—not zero.</p>
    <dl class="bri-contract"><div><dt>Coverage</dt><dd>{_esc(coverage["start_year"])}–{_esc(coverage["end_year"])} · {_esc(coverage["indicators"])} indicators</dd></div><div><dt>Knowledge time</dt><dd><code>{_esc(dataset["clocks"]["retrieved_at"])}</code></dd></div><div><dt>Role</dt><dd>{_esc(boundary["allowed_role"])} only · {_esc(boundary["join_scope"].replace("_", " "))}</dd></div><div><dt>Rights</dt><dd>{_esc(rights["license"])} · {_esc(rights["attribution"])}</dd></div></dl>
{publication_proof}    <p>No row may infer a project, actor, corridor or causal effect. <a href="/{_esc(dataset["artifact"]["path"])}">Inspect the normalized bundle</a> · <a href="/{_esc(dataset["observation_schema"]["path"])}">Validate its schema</a></p>
  </section>"""


def _render_html(artifact: dict) -> bytes:
    report = artifact["coverage_report"]
    states = report["implementation_states"]
    workstreams = "".join(
        (
            '<article class="bri-card">'
            f'<p class="bri-eyebrow">{_esc(item["status"].replace("_", " "))}</p>'
            f'<h3>{_esc(item["label"])}</h3>'
            f'<p>{len(item["source_ids"])} source families · '
            f'{len(item["required_coverage"])} required evidence dimensions</p>'
            f'<ul class="bri-chips">{_chips(item["required_coverage"])}</ul>'
            '</article>'
        )
        for item in artifact["workstreams"]
    )
    targets = "".join(
        (
            f'<li data-bri-record data-bri-text="{_esc((item["label"] + " " + item["target_type"]).lower())}">'
            f'<div><span>{_esc(item["target_type"].replace("_", " "))}</span>'
            f'<strong>{_esc(item["label"])}</strong></div>'
            f'<code>{_esc(item["evidence_status"])}</code>'
            f'<p>{_esc(", ".join(item["required_coverage"]))}</p></li>'
        )
        for item in artifact["watch_targets"]
    )
    lanes = "".join(
        (
            '<article class="bri-lane">'
            f'<p class="bri-eyebrow">{_esc(lane["lane_id"].replace("_", " "))}</p>'
            f'<h3>{_esc(lane["label"])}</h3>'
            f'<p><b>Entities:</b> {_esc(", ".join(lane["entity_types"]))}</p>'
            f'<p><b>Never merge with:</b> {_esc(", ".join(lane["prohibited_merges"]))}</p>'
            '</article>'
        )
        for lane in artifact["movement_taxonomy"]["lanes"]
    )
    sources = "".join(
        (
            f'<li class="bri-source" data-bri-source data-state="{_esc(source["implementation"])}" '
            f'data-class="{_esc(source["source_class"])}" '
            f'data-bri-text="{_esc((source["source_id"] + " " + source["name"] + " " + source["publisher"] + " " + " ".join(source["coverage"])).lower())}">'
            '<div class="bri-source__head">'
            f'<a href="{_esc(source["url"])}" target="_blank" rel="noopener">{_esc(source["name"])}</a>'
            f'<span class="bri-state bri-state--{_esc(source["implementation"].replace("_", "-"))}">{_esc(source["implementation"].replace("_", " "))}</span>'
            '</div>'
            f'<p>{_esc(source["publisher"])} · {_esc(source["authority_role"].replace("_", " "))}</p>'
            f'<p>{_esc(", ".join(source["coverage"]))}</p>'
            f'<small>Rights: {_esc(source["rights_status"].replace("_", " "))}. {_esc(source["notes"])}</small>'
            '</li>'
        )
        for source in artifact["sources"]
    )
    balochistan_targets = _target_projection(
        artifact,
        target_ids=(
            "balochistan_resources_revenue",
            "balochistan_movement_history",
        ),
        card_class="bri-lane",
    )
    pakistan_gwadar = _render_region_section(
        artifact,
        anchor="pakistan-gwadar",
        eyebrow="Pakistan and Gwadar · target-specific readiness",
        title="CPEC, port, connectivity and public-service records stay claim by claim.",
        introduction=(
            "This lane keeps national finance context, provincial records and "
            "Gwadar-specific project sources at their published geographic grain."
        ),
        geography_codes=("PAK", "PAK-BAL", "PAK-GWD"),
        target_ids=(
            "cpec_portfolio",
            "gwadar_port_free_zone",
            "gwadar_connectivity",
            "gwadar_public_services",
            "balochistan_resources_revenue",
        ),
    )
    myanmar = _render_region_section(
        artifact,
        anchor="myanmar",
        eyebrow="Myanmar · target-specific readiness",
        title="CMEC, Kyaukpyu, pipelines and rail remain separate evidence records.",
        introduction=(
            "This lane preserves the difference between official proposals, "
            "investment approvals, economic context and observed implementation."
        ),
        geography_codes=("MMR", "MMR-RKH"),
        target_ids=(
            "cmec_portfolio",
            "kyaukpyu_port_sez",
            "china_myanmar_pipelines",
            "mandalay_muse_rail",
        ),
    )
    schema_org_document = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "Palimpsest Belt and Road Observatory coverage contract",
        "description": artifact["scope"],
        "url": "https://palimpsest.info/belt-and-road/",
        "dateModified": artifact["as_of"],
        "license": "https://github.com/beepboop2025/palimpsest/blob/main/LICENSE",
        "distribution": {
            "@type": "DataDownload",
            "encodingFormat": "application/json",
            "contentUrl": "https://palimpsest.info/readings/belt-and-road-observatory-latest.json",
        },
    }
    observation_datasets = artifact.get("observation_datasets", [])
    if observation_datasets:
        [dataset] = observation_datasets
        schema_org_document["hasPart"] = {
            "@type": "Dataset",
            "name": "BRI-country World Development Indicators context",
            "description": (
                "Attributed national country-period context for China, Myanmar "
                "and Pakistan; never project, actor, corridor or causal evidence."
            ),
            "license": dataset["rights"]["license_url"],
            "creator": {
                "@type": "Organization",
                "@id": "https://www.worldbank.org/#organization",
                "name": "World Bank",
                "url": "https://www.worldbank.org/",
            },
            "distribution": {
                "@type": "DataDownload",
                "encodingFormat": dataset["artifact"]["media_type"],
                "contentUrl": dataset["artifact"]["url"],
            },
        }
    schema_org = json.dumps(
        schema_org_document,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    schema_path = artifact["$schema"]
    document = f"""<!doctype html>
<html lang="en" data-tk-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Belt and Road Observatory · Palimpsest</title>
<meta name="description" content="A provenance-first global Belt and Road infrastructure and economics source ledger, with deep Pakistan, Gwadar, Balochistan, Myanmar and Kyaukpyu coverage.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://palimpsest.info/belt-and-road/">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<meta name="theme-color" content="#edf2ec">
<script type="application/ld+json">{schema_org}</script>
{site_nav.HEAD}
<link rel="stylesheet" href="/assets/bri.css">
</head>
<body class="ps tk bri-page">
<!-- GENERATED BY scripts/build_bri_observatory.py -->
{site_nav.render('/belt-and-road/')}
<main id="main">
  <header class="bri-hero">
    <p class="bri-eyebrow">Global BRI evidence backbone · first deep dossiers: Pakistan and Myanmar</p>
    <h1>Every project claim should show what it knows—and what it does not.</h1>
    <p class="bri-dek">{_esc(artifact["scope"])}</p>
    <div class="bri-release"><strong>Evidence coverage contract</strong><span>Publication is not a claim that every registered source has been ingested.</span></div>
    <div class="bri-stats" aria-label="Current registry coverage">
      <div><strong>{report["source_count"]}</strong><span>source families</span></div>
      <div><strong>{report["build_ready_source_count"]}</strong><span>build-ready inputs</span></div>
      <div><strong>{len(artifact["watch_targets"])}</strong><span>priority target ledgers</span></div>
      <div><strong>{len(report["build_ready_gaps"])}</strong><span>build-ready gaps kept visible</span></div>
    </div>
    <p class="bri-actions"><a href="/readings/belt-and-road-observatory-latest.json">Download the machine contract</a><a href="#sources">Inspect the source ledger</a><a href="#balochistan">Read the Balochistan boundary</a></p>
  </header>

  <section class="bri-section" id="bri-corridors" aria-labelledby="meaning-title">
    <p class="bri-eyebrow">The counting rule</p><h2 id="meaning-title">An announcement is not a disbursement. Completion is not operation.</h2>
    <p>Palimpsest records announcement, approval, contract, commitment, disbursement, construction, completion and operation separately. It preserves currency, price basis, sovereign guarantees, revisions and source clocks before any total is calculated.</p>
    <div class="bri-columns"><div><h3>Economic depth</h3><ul class="bri-chips">{_chips(artifact["economic_metrics"])}</ul></div><div><h3>Ground-level outcomes</h3><ul class="bri-chips">{_chips(artifact["local_impact_fields"])}</ul></div></div>
  </section>

  <section class="bri-section" aria-labelledby="workstreams-title">
    <p class="bri-eyebrow">One backbone, several evidence lanes</p><h2 id="workstreams-title">Coverage that can grow without flattening unlike facts</h2>
    <div class="bri-grid">{workstreams}</div>
  </section>

  <section class="bri-section" aria-labelledby="targets-title">
    <p class="bri-eyebrow">Priority ledger</p><h2 id="targets-title">From the global BRI universe to Gwadar and Kyaukpyu</h2>
    <label class="bri-search">Filter targets<input type="search" data-bri-target-search placeholder="Try Gwadar, rail, resources, movement…"></label>
    <ul class="bri-targets" data-bri-targets>{targets}</ul>
  </section>

  <section class="bri-section bri-dark" id="balochistan" aria-labelledby="balochistan-title">
    <p class="bri-eyebrow">Balochistan: plural record, never one label</p><h2 id="balochistan-title">The umbrella term is a research concept, not a database actor.</h2>
    <p>{_esc(artifact["movement_taxonomy"]["identity_rule"])}</p>
    <div class="bri-lanes">{lanes}</div>
    <div class="bri-region-evidence" aria-labelledby="balochistan-targets-title">
      <p class="bri-eyebrow">Artifact targets · exact source readiness</p><h3 id="balochistan-targets-title">Resources and revenue are not movement affiliation; movement history is not one actor.</h3>
      <p>The machine contract exposes {balochistan_targets["target_count"]} separate Balochistan target ledgers backed by {balochistan_targets["source_count"]} distinct named source routes. These are scope and readiness records, not classifications of a person, community or political position.</p>
      <p><b>Target status:</b> {_state_summary(balochistan_targets["target_status_counts"])}. <b>Source readiness:</b> {_state_summary(balochistan_targets["implementation_counts"], preferred=_IMPLEMENTATION_ORDER)}. <b>Rights:</b> {_state_summary(balochistan_targets["rights_counts"])}. <b>Public build-state gate:</b> {balochistan_targets["build_ready_count"]} of {balochistan_targets["source_count"]} named routes.</p>
      <div class="bri-lanes">{balochistan_targets["cards"]}</div>
      <p>Link-only and planned routes are discovery, not ingestion. Build-ready describes a source route, not a verified project record or actor record. The resources/revenue target cannot infer affiliation; the plural movement-history target cannot merge electoral politics, peaceful civic advocacy, armed organizations, state actions, legal designations, rights reporting or political economy.</p>
    </div>
  </section>

  {pakistan_gwadar}

  {myanmar}

  <section class="bri-section" aria-labelledby="narco-title">
    <p class="bri-eyebrow">NarcoScope integration</p><h2 id="narco-title">Country context beside the dossier—not a label on it.</h2>
    <p>The additive v2 bridge covers official country aggregates for China, Pakistan and Myanmar. Its only permitted join is geography plus time. It cannot classify a political movement, armed organization, community, person, project, bilateral route or causal chain.</p>
    <dl class="bri-contract"><div><dt>Contract</dt><dd><code>{_esc(artifact["partner_bridges"][0]["contract"])}</code></dd></div><div><dt>Status</dt><dd>{_esc(artifact["partner_bridges"][0]["status"].replace("_", " "))}</dd></div><div><dt>Inference</dt><dd>{_esc(artifact["partner_bridges"][0]["actor_inference"])}</dd></div></dl>
  </section>

  <section class="bri-section" id="sources" aria-labelledby="sources-title">
    <p class="bri-eyebrow">Source and rights ledger</p><h2 id="sources-title">Open, pending, licensed and blocked routes all stay visible.</h2>
    <p>{states.get("live", 0)} sources are live, {states.get("repository_ready", 0)} input is repository ready, {states.get("adapter_ready", 0)} adapters are ready, and {states.get("blocked", 0)} licensed route is blocked from public redistribution. Link-only entries are discovery, not ingestion.</p>
    <div class="bri-controls"><label>Search<input type="search" data-bri-source-search placeholder="Source, publisher, evidence field…"></label><label>Implementation<select data-bri-state><option value="all">All states</option>{''.join(f'<option value="{_esc(state)}">{_esc(state.replace("_", " "))} ({count})</option>' for state, count in states.items())}</select></label><p data-bri-source-count aria-live="polite"></p></div>
    <ul class="bri-sources" data-bri-sources>{sources}</ul>
  </section>

  <section class="bri-section bri-limit" aria-labelledby="limits-title">
    <p class="bri-eyebrow">Publication boundary</p><h2 id="limits-title">Useful for accountability, deliberately useless for targeting.</h2>
    <p>Conflict events publish only after a delay and at administrative-area grain. The observatory carries no person-level dossiers, live tactical coordinates, vulnerability maps or operational guidance. Allegations, government positions, administrative designations, legal status, coded events and adjudicated findings remain distinct.</p>
  </section>
</main>
<footer class="bri-footer"><p><strong>Palimpsest Belt and Road Observatory</strong> · Source rights, lifecycle states, local impacts and corrections before conclusions.</p><p><a href="/readings/belt-and-road-observatory-latest.json">JSON</a> · <a href="{_esc(schema_path)}">Schema</a> · <a href="/challenge.html">Challenge a source or method</a></p></footer>
{site_nav.FOOT}
<script src="/assets/bri.js" defer></script>
</body>
</html>
"""
    economic_context = _render_observation_datasets(artifact)
    if economic_context:
        marker = '  <section class="bri-section" aria-labelledby="narco-title">'
        if document.count(marker) != 1:
            raise ValueError("BRI page economic-context insertion marker changed")
        document = document.replace(marker, f"  {economic_context}\n\n{marker}")
    return document.encode("utf-8")


def build(
    registry_path: Path = DEFAULT_REGISTRY,
    *,
    wdi_bundle_path: Path | None = DEFAULT_WDI_BUNDLE,
    wdi_artifact_path: str = WDI_ARTIFACT_PATH,
    wdi_observation_schema_path: Path = DEFAULT_WDI_SCHEMA,
    wdi_observation_schema_repository_path: str = WDI_OBSERVATION_SCHEMA_PATH,
    wdi_series_registry_path: Path = DEFAULT_WDI_SERIES_REGISTRY,
    wdi_series_registry_repository_path: str = WDI_SERIES_REGISTRY_PATH,
    wdi_publication_receipt_path: Path | None | object = _AUTO_WDI_PUBLICATION_RECEIPT,
    wdi_archived_size_receipt_path: Path | None = None,
) -> tuple[bytes, bytes]:
    registry = load_registry(registry_path)
    observation_datasets = None
    if wdi_bundle_path is not None:
        source_implementation = next(
            source["implementation"]
            for source in registry["sources"]
            if source["source_id"] == "world_bank_wdi"
        )
        (
            resolved_publication_receipt,
            resolved_archived_size_receipt,
        ) = _resolve_wdi_publication_receipts(
            wdi_publication_receipt_path,
            wdi_archived_size_receipt_path,
            source_implementation=source_implementation,
        )
        observation_datasets = [
            build_wdi_observation_descriptor(
                registry,
                bundle_path=wdi_bundle_path,
                artifact_path=wdi_artifact_path,
                observation_schema_path=wdi_observation_schema_path,
                observation_schema_repository_path=(
                    wdi_observation_schema_repository_path
                ),
                series_registry_path=wdi_series_registry_path,
                series_registry_repository_path=(
                    wdi_series_registry_repository_path
                ),
                publication_receipt_path=resolved_publication_receipt,
                archived_size_receipt_path=resolved_archived_size_receipt,
            )
        ]
    artifact = build_public_artifact(
        registry,
        observation_datasets=observation_datasets,
    )
    return _json_bytes(artifact), _render_html(artifact)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--html-output", type=Path, default=DEFAULT_HTML)
    parser.add_argument(
        "--wdi-bundle",
        type=Path,
        default=DEFAULT_WDI_BUNDLE,
        help="exact normalized WDI bundle to bind into a v2 observatory build",
    )
    parser.add_argument(
        "--wdi-artifact-path",
        default=WDI_ARTIFACT_PATH,
        help="repository-relative public path advertised for --wdi-bundle",
    )
    parser.add_argument(
        "--wdi-observation-schema",
        type=Path,
        default=DEFAULT_WDI_SCHEMA,
    )
    parser.add_argument(
        "--wdi-series-registry",
        type=Path,
        default=DEFAULT_WDI_SERIES_REGISTRY,
    )
    parser.add_argument(
        "--wdi-publication-receipt",
        type=Path,
        default=_AUTO_WDI_PUBLICATION_RECEIPT,
        help=(
            "canonical production receipt; defaults to the checked-in receipt "
            "when present"
        ),
    )
    parser.add_argument(
        "--wdi-archived-size-receipt",
        type=Path,
        help=(
            "canonical checked-in Pages size receipt; defaults to the exact "
            "publication-SHA path"
        ),
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    json_payload, html_payload = build(
        args.registry,
        wdi_bundle_path=args.wdi_bundle,
        wdi_artifact_path=args.wdi_artifact_path,
        wdi_observation_schema_path=args.wdi_observation_schema,
        wdi_series_registry_path=args.wdi_series_registry,
        wdi_publication_receipt_path=args.wdi_publication_receipt,
        wdi_archived_size_receipt_path=args.wdi_archived_size_receipt,
    )
    outputs = ((args.json_output, json_payload), (args.html_output, html_payload))
    if args.check:
        drift = [str(path.relative_to(ROOT)) for path, payload in outputs if not path.is_file() or path.read_bytes() != payload]
        if drift:
            print("belt-and-road observatory drift: " + ", ".join(drift))
            return 2
        print("belt-and-road observatory: exact")
        return 0
    for path, payload in outputs:
        _atomic_write(path, payload)
        print(f"wrote {path.relative_to(ROOT)} ({len(payload)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
