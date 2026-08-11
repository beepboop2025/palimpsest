#!/usr/bin/env python3
"""Build the aggregate-only Palimpsest Investigations desk without network access."""
from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

from core.investigations import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_READINGS_DIR,
    InvestigationError,
    build_investigations,
    canonical_json_bytes,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "readings" / "investigations-latest.json"


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
    """Durably replace the public file without exposing partial JSON."""

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
        directory_fd = os.open(path.parent, os.O_RDONLY)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readings-dir", type=Path, default=DEFAULT_READINGS_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", help="timezone-aware decision clock")
    parser.add_argument(
        "--check", action="store_true",
        help="validate inputs and require byte-identical current output; write nothing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        document = build_investigations(
            readings_dir=args.readings_dir,
            config_path=args.config,
            as_of=_parse_as_of(args.as_of),
        )
        payload = canonical_json_bytes(document)
    except (InvestigationError, argparse.ArgumentTypeError) as exc:
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
        print(f"investigations current · {document['n_cases']} research leads")
        return 0
    write_atomic(args.output, payload)
    states = ", ".join(f"{case['slug']}={case['status']}" for case in document["cases"])
    print(f"investigations -> {args.output} · {document['n_cases']} leads · {states}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
