#!/usr/bin/env python3
"""Admit an offline NarcoScope v2 artifact and schema into Palimpsest."""
from __future__ import annotations

import argparse
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from core.narcoscope_corridor_bridge import (
    DEFAULT_ARTIFACT_PATH,
    DEFAULT_RECEIPT_PATH,
    DEFAULT_SCHEMA_PATH,
    MAX_BYTES,
    NarcoScopeCorridorError,
    admission_receipt,
    canonical_receipt_bytes,
    load_bundle,
    strict_json_loads,
    validate_artifact,
    validate_receipt,
    validate_schema,
)


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NarcoScopeCorridorError(f"cannot safely open {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BYTES:
            raise NarcoScopeCorridorError(f"{path} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size or os.read(descriptor, 1):
            raise NarcoScopeCorridorError(f"{path} changed while being read")
        return raw
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _clock(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NarcoScopeCorridorError("--admitted-at is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NarcoScopeCorridorError("--admitted-at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT_PATH)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--schema-source-file", type=Path)
    parser.add_argument("--admitted-at")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.check:
            artifact, _, receipt = load_bundle(args.artifact, args.schema, args.receipt)
            print(
                "narcoscope corridors pin: valid · "
                f"dataAsOf={artifact['dataAsOf']} · sha256={receipt['current']['sha256']}"
            )
            return 0
        if not args.source_file or not args.schema_source_file or not args.admitted_at:
            raise NarcoScopeCorridorError(
                "offline admission requires --source-file, --schema-source-file and --admitted-at"
            )
        artifact_raw = _read_regular(args.source_file)
        schema_raw = _read_regular(args.schema_source_file)
        artifact = strict_json_loads(artifact_raw, label="NarcoScope corridor candidate")
        schema = strict_json_loads(schema_raw, label="NarcoScope corridor schema candidate")
        validate_schema(schema)
        validate_artifact(artifact, schema)
        previous = None
        if args.receipt.is_file():
            previous_artifact_raw = _read_regular(args.artifact)
            previous_schema_raw = _read_regular(args.schema)
            previous_artifact = strict_json_loads(previous_artifact_raw, label="current corridor artifact")
            previous_receipt_raw = _read_regular(args.receipt)
            previous = strict_json_loads(previous_receipt_raw, label="current corridor receipt")
            if previous_receipt_raw != canonical_receipt_bytes(previous):
                raise NarcoScopeCorridorError("current corridor receipt is not canonical JSON")
            validate_receipt(
                previous,
                artifact_raw=previous_artifact_raw,
                schema_raw=previous_schema_raw,
                artifact=previous_artifact,
            )
        receipt = admission_receipt(
            artifact, artifact_raw, schema_raw,
            admitted_at=_clock(args.admitted_at), previous=previous,
        )
        if args.dry_run:
            print(
                "narcoscope corridors pin: candidate valid · "
                f"dataAsOf={artifact['dataAsOf']} · sha256={receipt['current']['sha256']}"
            )
            return 0
        _atomic_write(args.artifact, artifact_raw)
        _atomic_write(args.schema, schema_raw)
        _atomic_write(args.receipt, canonical_receipt_bytes(receipt))
        print(
            "narcoscope corridors pin: admitted · "
            f"dataAsOf={artifact['dataAsOf']} · sha256={receipt['current']['sha256']}"
        )
        return 0
    except (NarcoScopeCorridorError, OSError) as exc:
        print(f"narcoscope corridors pin: refused: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
