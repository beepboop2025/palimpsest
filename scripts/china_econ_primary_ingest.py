#!/usr/bin/env python3
"""Parse reviewed primary-document vintages into the economic JSONL ledger.

This command is intentionally not scheduled.  It authenticates every selected
EvidenceDocument through the store API, parses all retained vintages, and only
then performs one locked ledger append transaction.  ``--check`` and
``--dry-run`` never touch the ledger.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from core.econ_ledger import (
    LedgerIntegrityError,
    append_vintages,
    load_snapshot,
    snapshot_digest,
)
from core.evidence_documents import EvidenceDocumentStore
from core.primary_documents import (
    PrimaryDocumentError,
    strict_json_loads,
    validate_primary_document_index,
)
from processors.china_econ_primary import (
    DEFAULT_ALIAS_PATH,
    DEFAULT_SERIES_PATH,
    PrimaryEconomicAdapterError,
    load_series_registry,
    load_source_aliases,
    observations_from_captured_document,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX_PATH = ROOT / "readings" / "primary-documents-latest.json"
# The adapter tranche is reviewed but deliberately not live.  A bare write
# therefore targets a private review ledger; an operator must explicitly pass
# the public ledger after source-by-source release-clock and shape review.
DEFAULT_LEDGER_PATH = ROOT / "data" / "review" / "china-econ-primary-observations.jsonl"
DEFAULT_STORE_PATH = ROOT / "data" / "evidence-documents"


def _load_index(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise PrimaryDocumentError(f"missing primary-document index: {path}") from exc
    value = strict_json_loads(raw, label="primary-document index")
    if type(value) is not dict:
        raise PrimaryDocumentError("primary-document index must be an object")
    validate_primary_document_index(value)
    return value


def build_candidates(
    *,
    index_path: Path,
    store_path: Path,
    alias_path: Path,
    series_path: Path,
) -> tuple:
    """Authenticate and parse the complete reviewed tranche without writing."""

    aliases = load_source_aliases(alias_path)
    series = load_series_registry(series_path, aliases=aliases)
    index = _load_index(index_path)
    documents = {row["source_id"]: row for row in index["documents"]}
    missing = set(aliases.aliases) - set(documents)
    if missing:
        raise PrimaryEconomicAdapterError(
            f"primary-document index lacks reviewed sources {sorted(missing)}"
        )
    resolved_store = store_path.expanduser().resolve()
    if not resolved_store.is_dir():
        raise PrimaryEconomicAdapterError(
            f"EvidenceDocument store does not exist: {resolved_store}"
        )
    store = EvidenceDocumentStore(resolved_store)
    candidates = []
    for primary in sorted(aliases.aliases):
        document = documents[primary]
        # Every retained content revision is replayed.  The economic ledger's
        # append transaction decides whether it is a new value revision or a
        # same-value provenance vintage.
        for vintage in document["vintages"]:
            manifest_id = vintage["manifest_sha256"]
            manifest = store.load_manifest(manifest_id, verify_content=True)
            raw = store.read_content(manifest_id)
            candidates.extend(
                observations_from_captured_document(
                    raw,
                    document=document,
                    vintage=vintage,
                    manifest=manifest,
                    aliases=aliases,
                    series_registry=series,
                )
            )
    if not candidates:
        raise PrimaryEconomicAdapterError("reviewed tranche produced zero observations")
    return tuple(candidates)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX_PATH)
    parser.add_argument(
        "--store",
        type=Path,
        default=Path(
            os.getenv("PALIMPSEST_EVIDENCE_DOCUMENT_STORE", str(DEFAULT_STORE_PATH))
        ),
        help="absolute private EvidenceDocument store",
    )
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIAS_PATH)
    parser.add_argument("--series", type=Path, default=DEFAULT_SERIES_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="authenticate and parse every retained vintage without writing",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="show the aggregate candidate summary without writing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        # A corrupt existing ledger is a stop condition even in review modes.
        snapshot = load_snapshot(args.ledger)
        candidates = build_candidates(
            index_path=args.index,
            store_path=args.store,
            alias_path=args.aliases,
            series_path=args.series,
        )
    except (
        LedgerIntegrityError,
        PrimaryDocumentError,
        PrimaryEconomicAdapterError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"china-econ-primary refused: {exc}", file=sys.stderr)
        return 2
    sources = sorted({row.source_id for row in candidates})
    series = sorted({row.series_id for row in candidates})
    if args.check:
        print(
            "china-econ-primary: valid "
            f"({len(candidates)} candidates, {len(series)} series, "
            f"{len(sources)} sources; ledger={snapshot.records} rows)"
        )
        return 0
    if args.dry_run:
        print(
            "china-econ-primary: dry-run "
            f"candidates={len(candidates)} series={len(series)} "
            f"sources={','.join(sources)} logical_sha256={snapshot_digest(candidates)}"
        )
        return 0
    try:
        appended = append_vintages(args.ledger, candidates)
    except (LedgerIntegrityError, OSError, TypeError, ValueError) as exc:
        print(f"china-econ-primary refused: {exc}", file=sys.stderr)
        return 2
    print(
        "china-econ-primary: "
        f"{len(appended)} observation vintages appended from "
        f"{len(candidates)} authenticated candidates"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_candidates", "main"]
