"""Publish a usable dataset directory without advertising quarantined values.

The legacy catalog is a private build input and may be quarantined as a whole.
This projection keeps its editorial entries, checks each distribution against
the same publication policy, and exposes only permitted artifact metadata.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts import build_data_catalog as atlas
from scripts import stage_pages_rights as rights


OUTPUT = Path("readings/public-data-catalog-latest.json")


def build_public_catalog(
    catalog: Mapping[str, Any] | None = None, *, root: Path | None = None,
    now: datetime | None = None,
    _scan_cache: dict[tuple, bool] | None = None,
) -> dict[str, Any]:
    root = (root or atlas.ROOT).resolve()
    now = now or atlas._utc_now()
    if catalog is None:
        catalog, _, _ = atlas.build_catalog(now=now)
    result = copy.deepcopy(catalog)
    policy = rights.load_source_policy(root / rights.POLICY_RELATIVE_PATH)
    allowed = frozenset(
        key for key, value in policy.decisions.items()
        if rights._effective_decision(value, evaluated_at=now) == "allow"
    )
    denied = frozenset(set(policy.decisions) - allowed)
    pattern = rights._lineage_pattern(denied)
    decisions = {}
    scan_cache = _scan_cache if _scan_cache is not None else {}

    def permitted(relative: str | None) -> bool:
        if not relative:
            return False
        if relative in decisions:
            return decisions[relative]
        path = root / relative
        if (Path(relative).is_absolute() or ".." in Path(relative).parts
                or path.is_symlink() or root not in path.resolve().parents):
            raise ValueError("public catalog distribution escapes the release")
        if relative in rights.ALWAYS_RESTRICT or not path.is_file():
            decisions[relative] = False
            return False
        try:
            raw = rights._read_bounded(path)
            # Only reuse a verdict for identical bytes, path and effective
            # permissions. Never use mtime or a previous edition's cache.
            cache_key = (str(path), hashlib.sha256(raw).digest(), allowed, denied)
            if cache_key in scan_cache:
                decisions[relative] = scan_cache[cache_key]
                return decisions[relative]
            text = rights._decode_public_text(raw)
        except (OSError, ValueError):
            decisions[relative] = False
            return False
        if path.suffix == ".json":
            try:
                document = rights._strict_json_loads(text or "null")
            except ValueError:
                decisions[relative] = False
                return False
            if isinstance(document, dict) and (
                document.get("publication_allowed") is False
                or document.get("status") == "restricted"
            ):
                decisions[relative] = False
                return False
        try:
            decisions[relative] = not rights._contains_denied_value(
                root, path, raw, denied_source_ids=denied,
                allowed_source_ids=allowed, decoded_text=text,
                lineage_pattern=pattern,
            )
        except ValueError:
            decisions[relative] = False
        scan_cache[cache_key] = decisions[relative]
        return decisions[relative]

    for item in result["datasets"]:
        metadata = item["artifacts"]
        denied_latest = item.get("publication_allowed") is False or (
            metadata.get("latest_available") and not permitted(item.get("latest"))
        )
        if denied_latest:
            item["publication_allowed"] = False
            item["count_fields"] = []
            item["artifacts"] = {
                "evidence_state": "gated", "observed_at": None,
                "age_seconds": None, "counts": {}, "latest_bytes": None,
                "history_bytes": None, "history_rows": None,
                "latest_available": False, "history_available": False,
            }
            item["distributions"] = []
            continue
        if metadata.get("history_available") and not permitted(item.get("history")):
            metadata.update(history_available=False, history_bytes=None, history_rows=None)
        item["distributions"] = [
            entry for entry in item.get("distributions", [])
            if entry.get("available") and permitted(entry.get("path"))
        ]
        if metadata.get("latest_available"):
            document = json.loads((root / item["latest"]).read_text(encoding="utf-8"))
            source_status = document.get("status") if isinstance(document, dict) else None
            if source_status in {"abstain", "abstained", "unreachable", "source_refused"}:
                metadata["evidence_state"] = "abstained"
            elif source_status in {"permission_required", "gated", "AWAITING_REVIEW"}:
                metadata["evidence_state"] = "gated"
            elif source_status == "partial" and metadata["evidence_state"] == "fresh":
                metadata["evidence_state"] = "partial"

    summary = result["summary"]
    summary["states"] = dict(sorted(Counter(
        item["artifacts"]["evidence_state"] for item in result["datasets"]
    ).items()))
    summary["published_bytes"] = sum(
        (item["artifacts"].get("latest_bytes") or 0)
        + (item["artifacts"].get("history_bytes") or 0)
        + sum(entry.get("bytes") or 0 for entry in item.get("distributions", []))
        for item in result["datasets"]
    )
    summary["history_rows"] = sum(
        item["artifacts"].get("history_rows") or 0 for item in result["datasets"]
    )
    result["availability_semantics"] = (
        "Per-dataset publication checks. A current acquisition can be partial or "
        "abstained; a restricted dataset has no advertised value download."
    )
    raw = (json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if rights._contains_denied_value(
        root, root / OUTPUT, raw, denied_source_ids=denied,
        allowed_source_ids=allowed, decoded_text=raw.decode(), lineage_pattern=pattern,
    ):
        raise ValueError("public catalog still contains restricted values")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    scan_cache: dict[tuple, bool] = {}
    catalog = build_public_catalog(now=now, _scan_cache=scan_cache)
    from processors.collector_health import build_health
    health = build_health(catalog, root=atlas.ROOT, now=now)
    if not args.check:
        # Health is itself an atlas entry. Materialize this edition before the
        # final projection so it cannot inherit the committed report's old clock.
        atlas._atomic_json(atlas.ROOT / "readings/collector-health-latest.json", health)
        catalog = build_public_catalog(now=now, _scan_cache=scan_cache)
        health = build_health(catalog, root=atlas.ROOT, now=now)
        atlas._atomic_json(atlas.ROOT / "readings/collector-health-latest.json", health)
        # The final report has different bytes from the provisional report.
        # Re-inspect its size after its self-referential freshness has settled.
        catalog = build_public_catalog(now=now, _scan_cache=scan_cache)
        atlas._atomic_json(atlas.ROOT / OUTPUT, catalog)
    print(json.dumps({"states": catalog["summary"]["states"], "written": not args.check}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
