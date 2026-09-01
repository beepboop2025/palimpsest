"""Test-only bridge from the pinned 60-source wire to the active 58-source registry.

The two retired feeds accepted no current-window items, and neither identity
appears in an event.  This projection lets BRI tests exercise unrelated build
logic while production continues to refuse a registry-mismatched wire.
"""

from __future__ import annotations

import json
from pathlib import Path

from core import newswire


LEGACY_REGISTRY_SHA256 = (
    "7738ab6e4e275f9eb515593a2a2962de5ec8271c7c1b9cb287eb616932accd14"
)


def write_active_registry_wire(destination: Path, *, root: Path) -> Path:
    document = json.loads(
        (root / "readings" / "newswire-latest.json").read_text(encoding="utf-8")
    )
    registry = newswire.load_source_registry(root / "config" / "news_sources.json")
    retired = newswire._RETIRED_SOURCE_TOMBSTONES

    assert document["source_registry_sha256"] == LEGACY_REGISTRY_SHA256
    assert all(
        source_id not in json.dumps(
            {"items": document["items"], "events": document["events"]},
            ensure_ascii=False,
        )
        for source_id in retired
    )
    removed = [
        row
        for row in document["coverage"]["sources"]
        if row["source_id"] in retired
    ]
    assert {row["source_id"] for row in removed} == set(retired)
    for row in removed:
        assert row["feed_url"] == retired[row["source_id"]][0]
        assert row["accepted_items"] == 0
        assert row["status"] == "stale"

    coverage = document["coverage"]
    coverage["sources"] = [
        row for row in coverage["sources"] if row["source_id"] not in retired
    ]
    coverage["registry_sources"] = len(coverage["sources"])
    coverage["rejected_items"] -= sum(row["rejected_items"] for row in removed)
    for row in removed:
        coverage["counts"][row["status"]] -= 1
    coverage["successful_sources"] = (
        coverage["counts"]["success"] + coverage["counts"]["stale"]
    )
    coverage["status"] = (
        "healthy"
        if coverage["counts"]["empty"]
        == coverage["counts"]["fetch_error"]
        == coverage["counts"]["parse_error"]
        == 0
        else "degraded"
    )
    document["source_registry_sha256"] = registry.sha256

    assert coverage["registry_sources"] == len(registry.sources) == 58
    newswire.validate_newswire_document(document)
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination
