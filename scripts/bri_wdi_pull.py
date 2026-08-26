#!/usr/bin/env python3
"""Check or build a review-only BRI WDI national-context artifact.

Outbound access is never implicit. Use ``--input`` with exact saved response
bytes and their canonical acquisition receipt, or opt in with ``--fetch``. A
live fetch samples its retrieval clock only after the bounded response returns.
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
    MAX_RECEIPT_BYTES,
    MAX_RESPONSE_BYTES,
    WDICollection,
    acquisition_receipt_for,
    build_url,
    fetch_bytes,
    load_registry,
    parse_response,
    verify_acquisition_receipt,
)
from core.bri_observation import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "bri_wdi_series.json"
MAX_DERIVED_BYTES = 128 * 1024 * 1024


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
        "--receipt-input",
        type=Path,
        help="canonical acquisition sidecar required with offline --input",
    )
    parser.add_argument(
        "--raw-output",
        type=Path,
        help=(
            "controlled path required with --fetch for the exact response bytes; "
            "never published automatically"
        ),
    )
    parser.add_argument(
        "--receipt-output",
        type=Path,
        help=(
            "controlled path required with --fetch for the canonical immutable "
            "acquisition sidecar"
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
    build.add_argument(
        "--replace-derived",
        action="store_true",
        help="explicitly replace a differing derived review artifact",
    )
    return parser


def _absolute_without_symlinks(path: Path, *, label: str) -> Path:
    try:
        absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise BRIWDIError(f"cannot resolve {label}: {exc}") from exc
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise BRIWDIError(f"cannot inspect {label} path: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise BRIWDIError(f"{label} path must not contain symlink components")
    return absolute


def _read_bounded(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    candidate = _absolute_without_symlinks(path, label=label)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise BRIWDIError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BRIWDIError(f"{label} must be a regular non-symlink file")
    if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
        raise BRIWDIError(f"{label} is empty or exceeds {maximum_bytes} bytes")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise BRIWDIError(f"cannot open {label}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise BRIWDIError(f"{label} changed while opening")
            raw = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise BRIWDIError(f"cannot read {label}: {exc}") from exc
    if not raw or len(raw) > maximum_bytes:
        raise BRIWDIError(f"{label} is empty or exceeds {maximum_bytes} bytes")
    return raw


def _resolve(path: Path, *, label: str) -> Path:
    candidate = _absolute_without_symlinks(path, label=label)
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise BRIWDIError(f"cannot resolve {label}: {exc}") from exc


def _paths_alias(left: Path, right: Path, *, left_label: str, right_label: str) -> bool:
    left_resolved = _resolve(left, label=left_label)
    right_resolved = _resolve(right, label=right_label)
    if left_resolved == right_resolved:
        return True
    if left_resolved.exists() and right_resolved.exists():
        try:
            return left_resolved.samefile(right_resolved)
        except OSError as exc:
            raise BRIWDIError(
                f"cannot compare {left_label} and {right_label}: {exc}"
            ) from exc
    return False


def _require_distinct(paths: Sequence[tuple[str, Path]]) -> None:
    for position, (left_label, left) in enumerate(paths):
        for right_label, right in paths[position + 1 :]:
            if _paths_alias(
                left,
                right,
                left_label=left_label,
                right_label=right_label,
            ):
                raise BRIWDIError(f"{left_label} must not alias {right_label}")


def _prepare_parent(path: Path, *, label: str) -> Path:
    candidate = _absolute_without_symlinks(path, label=label)
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BRIWDIError(f"cannot create {label} parent: {exc}") from exc
    _absolute_without_symlinks(candidate.parent, label=f"{label} parent")
    return candidate


def _fsync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _preflight_immutable(path: Path, payload: bytes, *, label: str) -> bool:
    candidate = _absolute_without_symlinks(path, label=label)
    if not candidate.exists():
        return False
    existing = _read_bounded(
        candidate,
        maximum_bytes=max(len(payload), 1),
        label=label,
    )
    if existing != payload:
        raise BRIWDIError(
            f"{label} already exists with different bytes; immutable evidence "
            "cannot be replaced"
        )
    return True


def _write_immutable(path: Path, payload: bytes, *, label: str) -> str:
    if type(payload) is not bytes or not payload:
        raise BRIWDIError(f"{label} payload must be non-empty bytes")
    if _preflight_immutable(path, payload, label=label):
        return "identical"
    candidate = _prepare_parent(path, label=label)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags, 0o644)
    except FileExistsError:
        if _preflight_immutable(candidate, payload, label=label):
            return "identical"
        raise AssertionError("immutable preflight must return or raise")
    except OSError as exc:
        raise BRIWDIError(f"cannot create {label}: {exc}") from exc
    created = True
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(candidate.parent)
        created = False
    finally:
        if created:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
    return "created"


def _write_derived(
    path: Path,
    payload: bytes,
    *,
    replace_existing: bool,
) -> str:
    if type(payload) is not bytes or not payload or len(payload) > MAX_DERIVED_BYTES:
        raise BRIWDIError(
            f"derived output is empty or exceeds {MAX_DERIVED_BYTES} bytes"
        )
    candidate = _absolute_without_symlinks(path, label="derived output")
    existed = candidate.exists()
    if existed:
        existing = _read_bounded(
            candidate,
            maximum_bytes=MAX_DERIVED_BYTES,
            label="derived output",
        )
        if existing == payload:
            return "identical"
        if not replace_existing:
            raise BRIWDIError(
                "derived output already exists with different bytes; pass "
                "--replace-derived to authorize replacement"
            )
    candidate = _prepare_parent(candidate, label="derived output")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{candidate.name}.", suffix=".tmp", dir=candidate.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        _absolute_without_symlinks(candidate, label="derived output")
        os.replace(temporary, candidate)
        _fsync_directory(candidate.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return "replaced" if existed else "created"


def _preflight_source_outputs(args: argparse.Namespace) -> None:
    if args.input is not None and args.receipt_input is None:
        raise BRIWDIError(
            "offline --input requires --receipt-input for authenticated replay"
        )
    if args.input is None and args.receipt_input is not None:
        raise BRIWDIError("receipt-input is accepted only with --input")
    if args.fetch and (args.raw_output is None or args.receipt_output is None):
        raise BRIWDIError("live --fetch requires --raw-output and --receipt-output")
    if not args.fetch and (
        args.raw_output is not None or args.receipt_output is not None
    ):
        raise BRIWDIError(
            "raw-output and receipt-output are accepted only with --fetch"
        )
    output = getattr(args, "output", None)
    paths: list[tuple[str, Path]] = [("registry", args.registry)]
    for label, candidate in (
        ("raw input", args.input),
        ("receipt input", args.receipt_input),
        ("raw output", args.raw_output),
        ("receipt output", args.receipt_output),
        ("derived output", output),
    ):
        if candidate is not None:
            paths.append((label, candidate))
    for label, candidate in paths:
        _absolute_without_symlinks(candidate, label=label)
    _require_distinct(paths)


def _collection(args: argparse.Namespace) -> WDICollection | None:
    registry = load_registry(args.registry)
    if args.input is None and not args.fetch:
        return None
    evidence_url = build_url(
        registry,
        start_year=args.start_year,
        end_year=args.end_year,
    )
    if args.input is not None:
        raw = _read_bounded(
            args.input,
            maximum_bytes=MAX_RESPONSE_BYTES,
            label="raw input",
        )
        receipt_bytes = _read_bounded(
            args.receipt_input,
            maximum_bytes=MAX_RECEIPT_BYTES,
            label="receipt input",
        )
        acquisition = verify_acquisition_receipt(
            receipt_bytes,
            raw=raw,
            expected_url=evidence_url,
        )
        collection = parse_response(
            raw,
            registry=registry,
            evidence_url=evidence_url,
            start_year=args.start_year,
            end_year=args.end_year,
            retrieved_at=acquisition.retrieved_at,
        )
        if collection.request_receipt.acquisition_id != acquisition.acquisition_id:
            raise BRIWDIError("derived collection does not bind the verified receipt")
        return collection
    raw = fetch_bytes(evidence_url)
    retrieved_at = _post_response_clock()
    acquisition = acquisition_receipt_for(
        raw,
        evidence_url=evidence_url,
        retrieved_at=retrieved_at,
    )
    receipt_bytes = canonical_json_bytes(acquisition.to_dict())
    _preflight_immutable(args.raw_output, raw, label="raw output")
    _preflight_immutable(
        args.receipt_output,
        receipt_bytes,
        label="receipt output",
    )
    _write_immutable(args.raw_output, raw, label="raw output")
    _write_immutable(args.receipt_output, receipt_bytes, label="receipt output")
    collection = parse_response(
        raw,
        registry=registry,
        evidence_url=evidence_url,
        start_year=args.start_year,
        end_year=args.end_year,
        retrieved_at=retrieved_at,
    )
    if collection.request_receipt.acquisition_id != acquisition.acquisition_id:
        raise BRIWDIError("derived collection does not bind the persisted receipt")
    return collection


def _post_response_clock() -> datetime:
    """Sample Palimpsest knowledge time only after response bytes arrive."""

    return datetime.now(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _preflight_source_outputs(args)
        collection = _collection(args)
        if args.command == "build":
            if collection is None:
                raise BRIWDIError("build requires exactly one of --input or --fetch")
            payload = canonical_json_bytes(collection.to_dict())
            disposition = _write_derived(
                args.output,
                payload,
                replace_existing=args.replace_derived,
            )
            print(
                "bri-wdi: built "
                f"collection={collection.collection_id} "
                f"rows={len(collection.observations)} output={args.output} "
                f"disposition={disposition}"
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
                f" raw_output={args.raw_output} receipt_output={args.receipt_output}"
                if args.raw_output is not None
                else ""
            )
            print(
                "bri-wdi: response valid "
                f"rows={receipt.source_rows} observed={receipt.observed_rows} "
                f"forecast={receipt.forecast_rows} "
                f"unavailable={receipt.unavailable_rows}{raw_note}"
            )
        return 0
    except (BRIWDIError, OSError, TypeError, ValueError) as exc:
        print(f"bri-wdi: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
