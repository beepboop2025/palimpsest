#!/usr/bin/env python3
"""Build the public newsroom-quality gate and standards page offline."""

from __future__ import annotations

import argparse
import html
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from core.corroboration import validate_corroboration
from core.editorial_readiness import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_PATH,
    build_editorial_readiness,
    canonical_json_bytes,
    load_editorial_packages,
)
from core.investigations import validate_investigations
from core.network_rounds import validate_network_rounds
from core.newswire import strict_json_loads, validate_prior_newswire_document
from core.primary_documents import (
    load_primary_source_registry,
    validate_primary_document_index,
)
from core.source_workflow import (
    summarize_source_workflow,
    validate_source_workflow_summary,
)
from scripts import site_nav


ROOT = Path(__file__).resolve().parents[1]
READINGS = ROOT / "readings"
DEFAULT_SOURCE_WORKFLOW = READINGS / "source-workflow-latest.json"
DEFAULT_PAGE = ROOT / "news" / "standards" / "index.html"
INPUT_PATHS = {
    "wire": READINGS / "newswire-latest.json",
    "primary": READINGS / "primary-documents-latest.json",
    "corroboration": READINGS / "corroboration-latest.json",
    "investigations": READINGS / "investigations-latest.json",
    "network_rounds": READINGS / "network-rounds-latest.json",
}


def _load(path: Path) -> dict[str, Any]:
    value = strict_json_loads(path.read_bytes(), label=str(path))
    if type(value) is not dict:
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _empty_source_workflow(
    config: Mapping[str, Any], investigations: Mapping[str, Any]
) -> dict[str, Any]:
    clock = datetime.fromisoformat(
        investigations["generated_at"].replace("Z", "+00:00")
    )
    return summarize_source_workflow(
        [],
        package_ids=[row["package_id"] for row in config["packages"]],
        generated_at=clock,
    )


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_standards_page(document: Mapping[str, Any]) -> bytes:
    summary = document["summary"]
    package_cards = []
    for package in document["packages"]:
        checks = "".join(
            f"<li data-state=\"{'passed' if row['passed'] else 'blocked'}\"><strong>{_h(row['label'])}</strong><span>{'Passed' if row['passed'] else 'Blocked'} · {_h(row['detail'])}</span></li>"
            for row in package["checks"]
        )
        package_cards.append(
            f"""<article class="nw-case-card" data-publication-state="{_h(package['status'])}">
  <p class="nw-section__label">{_h(package['profile'])} · {_h(package['status'])}</p>
  <h2>{_h(package['title'])}</h2>
  <p><code>{_h(package['package_id'])}</code></p>
  <ul class="nw-case-record-list">{checks}</ul>
</article>"""
        )
    profiles = "".join(
        f"<section class=\"nw-case-panel\"><h2>{_h(row['profile'].title())}</h2><ol>{''.join(f'<li>{_h(requirement.replace('-', ' '))}</li>' for requirement in row['requirements'])}</ol></section>"
        for row in document["publication_profiles"]
    )
    body = f"""<body class="ps newsroom-page newsroom-page--investigations">
{site_nav.render('/news/standards/')}
<main id="main" class="nw-shell">
  <header class="nw-article__header"><p class="nw-article__kicker">Newsroom quality gate · {_h(document['generated_at'])}</p><h1>Evidence can nominate a story. Only reporting can finish it.</h1><p class="nw-article__dek">{_h(document['scope'])}</p></header>
  <section class="nw-investigations-feature"><div><p class="nw-section__label">Current release state</p><h2>{_h(summary['wire_eligible'])}/{_h(summary['wire_events'])} wire dossiers satisfy the wire floor</h2><p>{_h(summary['explainers_publishable'])}/{_h(summary['explainers'])} explainers and {_h(summary['investigations_publishable'])}/{_h(summary['investigations'])} investigations satisfy their deeper reporting gates. Passing never publishes automatically.</p></div></section>
  <section class="nw-case-section"><header><p class="nw-section__label">Profiles</p><h2>Different speed, different burden</h2></header><div class="nw-case-columns">{profiles}</div></section>
  <section class="nw-case-section"><header><p class="nw-section__label">Open packages</p><h2>Every failed check stays visible</h2><p>Human-source counts come from encrypted private receipts. No identity, contact detail, or note text appears here.</p></header><div class="nw-investigations-grid">{''.join(package_cards)}</div></section>
  <aside class="nw-coverage"><div><p class="nw-kicker nw-kicker--warning">Evidence boundary</p><h2>No silent promotion</h2></div><div class="nw-coverage__items"><div class="nw-coverage__item"><p>Primary catalog pages are discovery context, not structured observations.</p></div><div class="nw-coverage__item"><p>Candidate joins require explicit editorial acceptance before they add an independent group.</p></div><div class="nw-coverage__item"><p>Network results describe a target, method, protocol, round, and vantage—not a national percentage.</p></div></div></aside>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Palimpsest Wire</a> · <a href="/readings/editorial-readiness-latest.json">Structured gate</a> · <a href="/readings/primary-documents-latest.json">Primary documents</a> · <a href="/readings/network-rounds-latest.json">Network rounds</a> · <a href="/docs/REPORTING-NEWSROOM-V2.md">Design</a></div></footer>
{site_nav.FOOT}
</body></html>"""
    head = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Reporting standards · Palimpsest Wire</title><meta name="description" content="Machine-enforced evidence and human-reporting publication gates for Palimpsest Wire."><link rel="canonical" href="https://palimpsest.info/news/standards/"><link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg"><meta name="theme-color" content="#0b131c">{site_nav.HEAD}</head>"""
    return (head + "\n" + body + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes, mode: int = 0o644) -> None:
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
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    source_workflow_path: Path = DEFAULT_SOURCE_WORKFLOW,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_editorial_packages(config_path)
    wire = _load(INPUT_PATHS["wire"])
    primary = _load(INPUT_PATHS["primary"])
    corroboration = _load(INPUT_PATHS["corroboration"])
    investigations = _load(INPUT_PATHS["investigations"])
    network_rounds = _load(INPUT_PATHS["network_rounds"])
    validate_prior_newswire_document(wire)
    validate_primary_document_index(
        primary, registry=load_primary_source_registry()
    )
    validate_corroboration(corroboration)
    validate_investigations(investigations)
    validate_network_rounds(network_rounds)
    source_workflow = (
        _load(source_workflow_path)
        if source_workflow_path.exists()
        else _empty_source_workflow(config, investigations)
    )
    validate_source_workflow_summary(source_workflow)
    document = build_editorial_readiness(
        wire,
        primary,
        corroboration,
        investigations,
        network_rounds,
        source_workflow,
        config=config,
    )
    return document, source_workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--source-workflow", type=Path, default=DEFAULT_SOURCE_WORKFLOW)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--page", type=Path, default=DEFAULT_PAGE)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    document, source_workflow = build(
        config_path=args.config,
        source_workflow_path=args.source_workflow,
    )
    outputs = {
        args.output: canonical_json_bytes(document),
        args.page: render_standards_page(document),
        args.source_workflow: canonical_json_bytes(source_workflow),
    }
    if args.check:
        drift = [
            str(path)
            for path, payload in outputs.items()
            if not path.exists() or path.read_bytes() != payload
        ]
        for path in drift:
            print(f"stale or missing {path}")
        return 1 if drift else 0
    for path, payload in outputs.items():
        _atomic_write(path, payload)
    print(
        "editorial-readiness: "
        f"wire {document['summary']['wire_eligible']}/{document['summary']['wire_events']}; "
        f"explainers {document['summary']['explainers_publishable']}/{document['summary']['explainers']}; "
        f"investigations {document['summary']['investigations_publishable']}/{document['summary']['investigations']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
