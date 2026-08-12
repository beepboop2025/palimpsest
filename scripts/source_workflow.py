#!/usr/bin/env python3
"""Commit encrypted reporting notes or export their aggregate readiness."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core.source_workflow import (
    SourceWorkflowError,
    SourceWorkflowStore,
    _canonical_bytes,
    summarize_source_workflow,
    validate_source_workflow_summary,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE = Path(
    os.getenv("PALIMPSEST_SOURCE_WORKFLOW_STORE", "/var/lib/palimpsest/source-workflow")
)
DEFAULT_OUTPUT = ROOT / "readings" / "source-workflow-latest.json"


def _atomic_public_write(path: Path, payload: bytes) -> None:
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
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    commit = subparsers.add_parser("commit")
    commit.add_argument("--encrypted-note", type=Path, required=True)
    commit.add_argument("--metadata", type=Path, required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--package", action="append", required=True)
    export.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    export.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    store = SourceWorkflowStore(args.store)
    try:
        if args.command == "commit":
            metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
            receipt = store.ingest(args.encrypted_note.read_bytes(), metadata)
            print(f"source-workflow: committed {receipt['record_id']}")
            return 0
        document = summarize_source_workflow(
            store.records(),
            package_ids=sorted(set(args.package)),
            generated_at=datetime.now(timezone.utc),
        )
        payload = _canonical_bytes(document)
        if args.check:
            current = json.loads(args.output.read_text(encoding="utf-8"))
            validate_source_workflow_summary(current)
            print(f"source-workflow: valid ({current['n_records']} private receipts)")
            return 0
        _atomic_public_write(args.output, payload)
        print(
            f"source-workflow: exported {document['n_packages']} packages, "
            f"{document['n_records']} private receipts"
        )
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, SourceWorkflowError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
