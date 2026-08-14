#!/usr/bin/env python3
"""Build a deterministic, sealable manifest for the China observation ledger.

Usage:
    PYTHONPATH=. python -m scripts.build_china_econ_manifest
    PYTHONPATH=. python -m scripts.build_china_econ_manifest --check
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from core.econ_ledger import LedgerIntegrityError, LedgerSnapshot, load_snapshot
from core.econ_observation import EconomicObservation


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "readings" / "china-econ-observations.jsonl"
DEFAULT_OUTPUT = ROOT / "readings" / "china-econ-observations-latest.json"
PUBLIC_ARTIFACT_PATH = "readings/china-econ-observations.jsonl"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _value_revision_counts(
    observations: Iterable[EconomicObservation],
) -> tuple[int, int]:
    grouped: dict[tuple[object, ...], list[EconomicObservation]] = defaultdict(list)
    for row in observations:
        grouped[row.vintage_key].append(row)
    value_changes = provenance_vintages = 0
    for rows in grouped.values():
        for prior, current in zip(rows, rows[1:]):
            if current.value != prior.value:
                value_changes += 1
            else:
                provenance_vintages += 1
    return value_changes, provenance_vintages


def build_manifest(
    ledger_path: str | os.PathLike[str] = DEFAULT_LEDGER,
    *,
    artifact_path: str = PUBLIC_ARTIFACT_PATH,
) -> dict:
    """Describe the exact ledger bytes and their validated aggregate coverage."""

    snapshot: LedgerSnapshot = load_snapshot(ledger_path)
    if not snapshot.observations or snapshot.as_of is None:
        raise LedgerIntegrityError(f"{os.fspath(ledger_path)}: ledger is empty")
    rows = snapshot.observations
    value_changes, provenance_vintages = _value_revision_counts(rows)

    series_ids = sorted({row.series_id for row in rows})
    source_ids = sorted({row.source_id for row in rows})
    geographies = sorted({row.geography for row in rows})
    sectors = sorted({row.sector for row in rows})
    firm_sizes = sorted({row.firm_size for row in rows})
    ownerships = sorted({row.ownership for row in rows})
    frequencies = sorted({row.frequency for row in rows})
    units = sorted({row.unit for row in rows})
    released = [row.released_at for row in rows]
    collected = [row.collected_at for row in rows]

    return {
        "schema_version": "palimpsest-economic-observation-manifest.v1",
        "generated_at": _timestamp(snapshot.as_of),
        "as_of": _timestamp(snapshot.as_of),
        "source": (
            "Palimpsest aggregate collectors; each observation retains its own "
            "source_id, evidence_url, and raw response digest"
        ),
        "method": (
            "Validate every aggregate observation and observation_id, enforce "
            "append order and source revision invariants, then hash the exact "
            "published JSONL bytes."
        ),
        "scope": (
            "Aggregate China economic observations actually collected by "
            "Palimpsest; this is a bitemporal evidence ledger, not a GDP estimate "
            "or a claim of complete source coverage."
        ),
        "n_observations": snapshot.records,
        "artifact": {
            "path": artifact_path,
            "url": f"https://palimpsest.info/{artifact_path}",
            "media_type": "application/x-ndjson",
            "bytes": snapshot.byte_size,
            "sha256": snapshot.byte_sha256,
            "records": snapshot.records,
        },
        "contract": {
            "manifest_schema": {
                "path": "protocol/economic-observation-manifest-v1.schema.json",
                "url": (
                    "https://palimpsest.info/protocol/"
                    "economic-observation-manifest-v1.schema.json"
                ),
            },
            "observation_schema": {
                "path": "protocol/economic-observation-v1.schema.json",
                "url": (
                    "https://palimpsest.info/protocol/"
                    "economic-observation-v1.schema.json"
                ),
            },
            "aggregate_only": True,
            "bitemporal": True,
        },
        "coverage": {
            "series_count": len(series_ids),
            "series_ids": series_ids,
            "source_count": len(source_ids),
            "source_ids": source_ids,
            "geography_count": len(geographies),
            "geographies": geographies,
            "sector_count": len(sectors),
            "sectors": sectors,
            "firm_size_count": len(firm_sizes),
            "firm_sizes": firm_sizes,
            "ownership_count": len(ownerships),
            "ownerships": ownerships,
            "frequencies": frequencies,
            "units": units,
            "period_start": min(row.period_start for row in rows).isoformat(),
            "period_end": max(row.period_end for row in rows).isoformat(),
            "first_released_at": _timestamp(min(released)),
            "last_released_at": _timestamp(max(released)),
            "first_collected_at": _timestamp(min(collected)),
            "last_collected_at": _timestamp(max(collected)),
            "value_revision_events": value_changes,
            "provenance_only_vintages": provenance_vintages,
        },
        "integrity": {
            "status": "verified",
            "exact_byte_digest": True,
            "observation_ids_verified": True,
            "unique_observation_ids": True,
            "monotonic_source_revisions": True,
        },
        "limitations": [
            "Coverage arrays describe rows present in this artifact, not the full source registry.",
            "A collected_at clock records when Palimpsest knew a vintage; it is not the economic period.",
            "Where a source omits a release timestamp, released_at may be a conservative first-observed upper bound declared in row metadata.",
        ],
    }


def canonical_manifest_bytes(document: dict) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def publish_manifest(
    ledger_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    artifact_path: str = PUBLIC_ARTIFACT_PATH,
) -> dict:
    """Build and atomically publish the deterministic manifest."""

    document = build_manifest(ledger_path, artifact_path=artifact_path)
    _write_atomic(Path(output_path), canonical_manifest_bytes(document))
    return document


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--artifact-path",
        default=PUBLIC_ARTIFACT_PATH,
        help="public path recorded in the manifest (independent of local input path)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the ledger and require an exact current manifest; write nothing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        document = build_manifest(args.ledger, artifact_path=args.artifact_path)
        payload = canonical_manifest_bytes(document)
    except (LedgerIntegrityError, OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))

    if args.check:
        try:
            current = args.output.read_bytes()
        except FileNotFoundError:
            print(f"missing {args.output}")
            return 1
        if current != payload:
            print(f"stale {args.output}")
            return 1
        print(
            f"china economic observation manifest current · "
            f"{document['n_observations']} records · "
            f"sha256 {document['artifact']['sha256'][:12]}"
        )
        return 0

    _write_atomic(args.output, payload)
    print(
        f"china economic observation manifest -> {args.output} · "
        f"{document['n_observations']} records · "
        f"sha256 {document['artifact']['sha256'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
