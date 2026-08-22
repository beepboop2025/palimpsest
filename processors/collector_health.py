"""Public collector-health board from the Evidence Atlas, not a silent stale page.

The catalog already records evidence_state per dataset. This module is the
reader-facing projection of that record: what is fresh, what is stale, what
abstained, and which latest files still lack the uniform artifact envelope.

It never imputes a missing collector into a live count.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.collector_artifact import project_reading


SCHEMA_VERSION = "palimpsest-collector-health.v1"
METHOD_VERSION = 1


def build_health(
    catalog: Mapping[str, Any],
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    datasets = catalog.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(now),
            "method_version": METHOD_VERSION,
            "headline": "collector health abstains: the evidence atlas has no datasets",
            "summary": {},
            "signals": [],
            "abstention": {
                "code": "missing-catalog",
                "reason": "catalog.datasets is missing or empty",
            },
            "limitations": [_limit_catalog()],
        }

    signals = []
    counts: dict[str, int] = {}
    for dataset in datasets:
        if not isinstance(dataset, Mapping):
            continue
        row = _row(dataset, root=root)
        state = row["evidence_state"]
        counts[state] = counts.get(state, 0) + 1
        signals.append(row)

    signals.sort(key=lambda item: (item["evidence_state"], item["id"]))
    n = len(signals)
    stale = counts.get("stale", 0)
    abstained = counts.get("abstained", 0) + counts.get("missing", 0)
    gated = counts.get("gated", 0)
    fresh = counts.get("fresh", 0)
    headline = (
        f"{fresh} of {n} atlas datasets are fresh; "
        f"{stale} stale, {gated} gated, {abstained} abstained or missing"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(now),
        "method_version": METHOD_VERSION,
        "headline": headline,
        "summary": {
            "n_datasets": n,
            "by_state": dict(sorted(counts.items())),
        },
        "signals": signals,
        "abstention": None,
        "method": (
            "Projection of config/public_data_catalog.json plus the catalog builder's "
            "artifact timestamps. Envelope compliance is a schema check on the latest "
            "file, not a second measurement."
        ),
        "limitations": [
            _limit_catalog(),
            "A stale file is still evidence of the last successful seal; it is not a zero.",
            "schema-gap means the latest file is readable but does not yet carry the "
            "uniform collector-artifact envelope. That is a contract debt, not a dead source.",
        ],
    }


def _row(dataset: Mapping[str, Any], *, root: Path | None) -> dict[str, Any]:
    artifacts = dataset.get("artifacts") if isinstance(dataset.get("artifacts"), Mapping) else {}
    evidence_state = str(artifacts.get("evidence_state") or dataset.get("status") or "missing")
    observed_at = artifacts.get("observed_at")
    envelope = None
    latest = dataset.get("latest")
    if root is not None and isinstance(latest, str) and latest:
        path = Path(latest)
        if not path.is_absolute():
            path = root / latest
        if path.is_file():
            try:
                envelope = project_reading(path, collector_id=str(dataset.get("id") or path.stem))
            except Exception as exc:  # noqa: BLE001 - health board must not crash a source
                envelope = {
                    "abstention": {
                        "code": "projection-failed",
                        "reason": str(exc),
                    }
                }
    return {
        "id": dataset.get("id"),
        "name": dataset.get("name"),
        "layer": dataset.get("layer"),
        "status": dataset.get("status"),
        "cadence": dataset.get("cadence"),
        "evidence_state": evidence_state,
        "observed_at": observed_at,
        "age_seconds": artifacts.get("age_seconds"),
        "latest": latest,
        "landing_page": dataset.get("landing_page"),
        "envelope_schema": None if envelope is None else envelope.get("schema_version"),
        "envelope_state": None
        if envelope is None
        else (envelope.get("freshness") or {}).get("evidence_state"),
        "abstention": None if envelope is None else envelope.get("abstention"),
    }


def _iso(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def _limit_catalog() -> str:
    return (
        "Health is the atlas evidence_state, which is last-seal age against the "
        "declared cadence. It is not a live probe of the upstream publisher."
    )
