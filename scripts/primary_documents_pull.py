#!/usr/bin/env python3
"""Capture the closed primary-source registry into immutable private storage."""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from core.evidence_documents import EvidenceDocumentStore
from core.primary_documents import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_PATH,
    PrimaryDocumentError,
    canonical_json_bytes,
    collect_primary_documents,
    load_primary_source_registry,
    strict_json_loads,
    validate_primary_document_index,
)
from core.safe_fetch import safe_fetch_bytes


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE_PATH = ROOT / "data" / "evidence-documents"


def _load_previous(path: Path) -> dict | None:
    if not path.exists():
        return None
    value = strict_json_loads(path.read_bytes(), label="prior primary-document index")
    if type(value) is not dict:
        raise PrimaryDocumentError("prior primary-document index must be an object")
    validate_primary_document_index(value)
    return value


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
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--store",
        type=Path,
        default=Path(
            os.getenv("PALIMPSEST_EVIDENCE_DOCUMENT_STORE", str(DEFAULT_STORE_PATH))
        ),
        help="absolute private EvidenceDocument store (default: env or data/evidence-documents)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the current public index without fetching or writing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    registry = load_primary_source_registry(args.config)
    if args.check:
        previous = _load_previous(args.output)
        if previous is None:
            raise PrimaryDocumentError(f"missing primary-document index: {args.output}")
        validate_primary_document_index(previous, registry=registry)
        print(
            "primary-documents: valid "
            f"({previous['n_documents']} documents, {previous['n_vintages']} vintages)"
        )
        return 0

    store_path = args.store.expanduser().resolve()
    previous = _load_previous(args.output)
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None

    def fetch(url: str, **kwargs) -> bytes:
        return safe_fetch_bytes(url, proxy=proxy, **kwargs)

    result = collect_primary_documents(
        registry,
        fetch,
        EvidenceDocumentStore(
            store_path,
            max_document_bytes=registry.max_document_bytes,
        ),
        now=datetime.now(timezone.utc),
        previous=previous,
    )
    _atomic_write(args.output, canonical_json_bytes(result))
    coverage = result["coverage"]
    print(
        "primary-documents: "
        f"{coverage['successful_sources']}/{coverage['registered_sources']} sources, "
        f"{result['n_new_vintages']} new vintages, coverage={coverage['status']}"
    )
    return 0 if coverage["successful_sources"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
