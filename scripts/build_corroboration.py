#!/usr/bin/env python3
"""Build the offline, editor-reviewed event/primary-document join."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from core.corroboration import (
    DEFAULT_DECISIONS_PATH,
    DEFAULT_OUTPUT_PATH,
    build_corroboration,
    canonical_json_bytes,
    load_decisions,
)
from core.newswire import strict_json_loads
from core.primary_documents import (
    DEFAULT_OUTPUT_PATH as DEFAULT_PRIMARY_PATH,
    load_primary_source_registry,
    validate_primary_document_index,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NEWSWIRE = ROOT / "readings" / "newswire-latest.json"


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
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--newswire", type=Path, default=DEFAULT_NEWSWIRE)
    parser.add_argument("--primary", type=Path, default=DEFAULT_PRIMARY_PATH)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    wire = strict_json_loads(args.newswire.read_bytes(), label=str(args.newswire))
    primary = strict_json_loads(args.primary.read_bytes(), label=str(args.primary))
    validate_primary_document_index(
        primary, registry=load_primary_source_registry()
    )
    document = build_corroboration(
        wire,
        primary,
        decisions=load_decisions(args.decisions),
    )
    payload = canonical_json_bytes(document)
    if args.check:
        if not args.output.exists() or args.output.read_bytes() != payload:
            print(f"stale or missing {args.output}")
            return 1
        print(
            f"corroboration: current ({document['n_candidate_edges']} candidates, "
            f"{document['n_accepted_edges']} accepted)"
        )
        return 0
    _atomic_write(args.output, payload)
    print(
        f"corroboration: {document['n_candidate_edges']} candidates, "
        f"{document['n_accepted_edges']} accepted, "
        f"{document['n_corroborated_events']} corroborated events"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
