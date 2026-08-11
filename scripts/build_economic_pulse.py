#!/usr/bin/env python3
"""Build the revision-safe China economic pulse without network access.

    PYTHONPATH=. python -m scripts.build_economic_pulse
    PYTHONPATH=. python -m scripts.build_economic_pulse --check
    PYTHONPATH=. python -m scripts.build_economic_pulse --as-of 2026-08-11T14:04:16Z
"""
from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

from core.economic_pulse import (
    DEFAULT_READINGS_DIR,
    DEFAULT_REGISTRY_PATH,
    EconomicPulseError,
    build_economic_pulse,
    canonical_json_bytes,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "readings" / "china-economic-pulse-latest.json"


def _parse_as_of(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--as-of must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of must include a timezone")
    return parsed


def write_atomic(path: Path, payload: bytes) -> None:
    """Durably replace one public JSON file without exposing partial bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--readings-dir", type=Path, default=DEFAULT_READINGS_DIR)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of",
        help="timezone-aware decision clock; defaults to the newest input collection clock",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate inputs and require the existing output to match; write nothing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        decision_time = _parse_as_of(args.as_of)
        document = build_economic_pulse(
            readings_dir=args.readings_dir,
            registry_path=args.registry,
            as_of=decision_time,
        )
        payload = canonical_json_bytes(document)
    except (EconomicPulseError, argparse.ArgumentTypeError) as exc:
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
            f"economic pulse current · {document['n_metrics']} metrics · "
            f"{document['economic_state']['status']}"
        )
        return 0

    write_atomic(args.output, payload)
    print(
        f"economic pulse -> {args.output} · {document['n_metrics']} metrics · "
        f"{len(document['coverage']['observed_independent_group_ids'])} independent groups · "
        f"{document['economic_state']['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
