#!/usr/bin/env python3
"""Check or build a review-only BRI WDI national-context artifact.

Outbound access is never implicit.  Use ``--input`` with an exact saved response
and explicit ``--retrieved-at`` clock, or opt in with ``--fetch``.  A live fetch
samples its retrieval clock only after the bounded response has returned.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from collectors.bri_world_bank_wdi import (
    BRIWDIError,
    MAX_RESPONSE_BYTES,
    WDICollection,
    build_url,
    fetch_bytes,
    load_registry,
    parse_response,
)
from core.bri_observation import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "bri_wdi_series.json"


def _canonical_timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise argparse.ArgumentTypeError("retrieved-at must end in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("retrieved-at must be ISO-8601") from exc
    if parsed.astimezone(UTC).isoformat().replace("+00:00", "Z") != value:
        raise argparse.ArgumentTypeError("retrieved-at must be canonically encoded")
    return parsed


def _add_collection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--start-year", type=int, default=1960)
    parser.add_argument("--end-year", type=int, default=datetime.now(UTC).year)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--input",
        type=Path,
        help="parse exact saved World Bank response bytes without network access",
    )
    source.add_argument(
        "--fetch",
        action="store_true",
        help="explicitly permit one bounded request to the reviewed World Bank host",
    )
    parser.add_argument(
        "--retrieved-at",
        type=_canonical_timestamp,
        help="exact post-response UTC clock required for offline --input replay",
    )
    parser.add_argument(
        "--raw-output",
        type=Path,
        help=(
            "controlled path required with --fetch for the exact response bytes; "
            "never published automatically"
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check",
        help="validate the registry alone, or validate an explicit input/fetch",
    )
    _add_collection_arguments(check)
    build = commands.add_parser("build", help="build one deterministic review artifact")
    _add_collection_arguments(build)
    build.add_argument(
        "--output",
        type=Path,
        required=True,
        help="review output path; there is intentionally no public default",
    )
    return parser


def _read_bounded(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BRIWDIError(f"cannot inspect input: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BRIWDIError("input must be a regular non-symlink file")
    if metadata.st_size <= 0 or metadata.st_size > MAX_RESPONSE_BYTES:
        raise BRIWDIError(f"input is empty or exceeds {MAX_RESPONSE_BYTES} bytes")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BRIWDIError(f"cannot open input: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(MAX_RESPONSE_BYTES + 1)
    except OSError as exc:
        raise BRIWDIError(f"cannot read input: {exc}") from exc
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise BRIWDIError(f"input is empty or exceeds {MAX_RESPONSE_BYTES} bytes")
    return raw


def _resolve(path: Path, *, label: str) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise BRIWDIError(f"cannot resolve {label}: {exc}") from exc


def _write_atomic(path: Path, payload: bytes, *, inputs: Sequence[Path]) -> None:
    output = _resolve(path, label="output")
    for input_path in inputs:
        if output == _resolve(input_path, label="input"):
            raise BRIWDIError("output must not overwrite an input or registry")
        try:
            if path.exists() and input_path.exists() and path.samefile(input_path):
                raise BRIWDIError("output aliases an input or registry")
        except OSError as exc:
            raise BRIWDIError(f"cannot compare output and input paths: {exc}") from exc
    if path.is_symlink():
        raise BRIWDIError("output must not be a symlink")
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


def _preflight_source_outputs(args: argparse.Namespace) -> None:
    if args.fetch and args.raw_output is None:
        raise BRIWDIError("live --fetch requires --raw-output for exact response bytes")
    if not args.fetch and args.raw_output is not None:
        raise BRIWDIError("raw-output is accepted only with --fetch")
    output = getattr(args, "output", None)
    if output is not None and args.raw_output is not None:
        if _resolve(output, label="output") == _resolve(
            args.raw_output, label="raw output"
        ):
            raise BRIWDIError("artifact output and raw-output must be different files")


def _collection(args: argparse.Namespace) -> WDICollection | None:
    registry = load_registry(args.registry)
    if args.input is None and not args.fetch:
        if args.retrieved_at is not None:
            raise BRIWDIError("retrieved-at requires --input")
        return None
    if args.input is not None:
        if args.retrieved_at is None:
            raise BRIWDIError("offline --input requires --retrieved-at")
        raw = _read_bounded(args.input)
        return parse_response(
            raw,
            registry=registry,
            evidence_url=build_url(
                registry,
                start_year=args.start_year,
                end_year=args.end_year,
            ),
            start_year=args.start_year,
            end_year=args.end_year,
            retrieved_at=args.retrieved_at,
        )
    if args.retrieved_at is not None:
        raise BRIWDIError(
            "live --fetch samples retrieved-at after the response; do not predate it"
        )
    evidence_url = build_url(
        registry,
        start_year=args.start_year,
        end_year=args.end_year,
    )
    raw = fetch_bytes(evidence_url)
    retrieved_at = datetime.now(UTC)
    _write_atomic(
        args.raw_output,
        raw,
        inputs=[args.registry],
    )
    return parse_response(
        raw,
        registry=registry,
        evidence_url=evidence_url,
        start_year=args.start_year,
        end_year=args.end_year,
        retrieved_at=retrieved_at,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _preflight_source_outputs(args)
        collection = _collection(args)
        if args.command == "build":
            if collection is None:
                raise BRIWDIError("build requires exactly one of --input or --fetch")
            inputs = [args.registry]
            if args.input is not None:
                inputs.append(args.input)
            if args.raw_output is not None:
                inputs.append(args.raw_output)
            payload = canonical_json_bytes(collection.to_dict())
            _write_atomic(args.output, payload, inputs=inputs)
            print(
                "bri-wdi: built "
                f"collection={collection.collection_id} "
                f"rows={len(collection.observations)} output={args.output}"
            )
            return 0
        if collection is None:
            registry = load_registry(args.registry)
            print(
                "bri-wdi: registry valid "
                f"countries={len(registry.countries)} "
                f"indicators={len(registry.bindings)}"
            )
        else:
            receipt = collection.request_receipt
            raw_note = (
                f" raw_output={args.raw_output}" if args.raw_output is not None else ""
            )
            print(
                "bri-wdi: response valid "
                f"rows={receipt.source_rows} observed={receipt.observed_rows} "
                f"unavailable={receipt.unavailable_rows}{raw_note}"
            )
        return 0
    except (BRIWDIError, OSError, TypeError, ValueError) as exc:
        print(f"bri-wdi: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
