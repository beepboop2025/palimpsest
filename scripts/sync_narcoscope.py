#!/usr/bin/env python3
"""Admit a bounded NarcoScope public aggregate and preserve its revision chain."""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from core.narcoscope_bridge import (
    CANONICAL_URL,
    DEFAULT_ARTIFACT_PATH,
    DEFAULT_RECEIPT_PATH,
    MAX_BYTES,
    NarcoScopeBridgeError,
    admission_receipt,
    canonical_json_bytes,
    load_artifact,
    load_receipt,
    strict_json_loads,
    validate_artifact,
)
from core.safe_fetch import FetchError, safe_fetch_bytes


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


def _read_bounded_candidate(path: Path) -> bytes:
    """Read an offline candidate without following links or allocating unbounded input."""

    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NarcoScopeBridgeError("cannot safely open offline candidate") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise NarcoScopeBridgeError("offline candidate is not a regular file")
        if metadata.st_size > MAX_BYTES:
            raise NarcoScopeBridgeError(
                f"offline candidate exceeds {MAX_BYTES} bytes"
            )
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
            raise NarcoScopeBridgeError("offline candidate changed while being read")
        return raw
    finally:
        os.close(descriptor)


def _clock(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NarcoScopeBridgeError("--retrieved-at is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NarcoScopeBridgeError("--retrieved-at must include a timezone")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT_PATH)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument(
        "--source-file", type=Path,
        help="offline candidate bytes; omit to fetch the fixed canonical HTTPS URL",
    )
    parser.add_argument("--retrieved-at", help="timezone-aware admission clock")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--check", action="store_true",
        help="validate the checked-in artifact and receipt without fetching or writing",
    )
    action.add_argument(
        "--dry-run", action="store_true",
        help="fetch/read and validate a candidate but do not replace checked-in files",
    )
    action.add_argument(
        "--remote-check", action="store_true",
        help=(
            "fetch the canonical producer artifact and require exact byte identity "
            "with the checked-in admitted pin"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.remote_check and (
            args.source_file is not None or args.retrieved_at is not None
        ):
            raise NarcoScopeBridgeError(
                "--remote-check cannot be combined with candidate or clock overrides"
            )
        if args.remote_check:
            current, current_bytes = load_artifact(args.artifact)
            previous_receipt = load_receipt(args.receipt, artifact=current_bytes)
            candidate_bytes = safe_fetch_bytes(
                CANONICAL_URL,
                max_bytes=MAX_BYTES,
                max_redirects=2,
                proxy=os.getenv("PALIMPSEST_PROXY", "").strip() or None,
            )
            candidate = strict_json_loads(
                candidate_bytes, label="canonical NarcoScope artifact"
            )
            validate_artifact(candidate)
            if candidate_bytes != current_bytes:
                raise NarcoScopeBridgeError(
                    "canonical NarcoScope bytes differ from the admitted pin"
                )
            print(
                "narcoscope remote pin: exact · "
                f"dataAsOf={current['dataAsOf']} · "
                f"sha256={previous_receipt['current']['sha256']}"
            )
            return 0
        if args.check:
            current, current_bytes = load_artifact(args.artifact)
            previous_receipt = load_receipt(args.receipt, artifact=current_bytes)
            print(
                "narcoscope pin: valid · "
                f"dataAsOf={current['dataAsOf']} · "
                f"sha256={previous_receipt['current']['sha256']}"
            )
            return 0
        # A corrected producer contract can intentionally make the currently
        # pinned semantic shape obsolete. Preserve its byte-bound receipt for
        # supersession without requiring that old shape to pass the new
        # candidate validator. The replacement itself is always fully
        # validated below before either file is written.
        current_bytes = _read_bounded_candidate(args.artifact)
        current = strict_json_loads(current_bytes, label="current NarcoScope artifact")
        previous_receipt = load_receipt(args.receipt)
        if (
            previous_receipt["current"]["sha256"]
            != hashlib.sha256(current_bytes).hexdigest()
            or previous_receipt["current"]["data_as_of"] != current.get("dataAsOf")
        ):
            raise NarcoScopeBridgeError(
                "current NarcoScope bytes do not match the superseded pin"
            )
        if args.source_file is not None:
            candidate_bytes = _read_bounded_candidate(args.source_file)
        else:
            candidate_bytes = safe_fetch_bytes(
                CANONICAL_URL,
                max_bytes=MAX_BYTES,
                max_redirects=2,
                proxy=os.getenv("PALIMPSEST_PROXY", "").strip() or None,
            )
        candidate = strict_json_loads(candidate_bytes, label="NarcoScope candidate")
        validate_artifact(candidate)
        receipt = admission_receipt(
            candidate,
            candidate_bytes,
            admitted_at=_clock(args.retrieved_at),
            previous_receipt=previous_receipt,
        )
        changed = candidate_bytes != current_bytes
        if args.dry_run:
            verb = "would update" if changed else "already current"
            print(
                f"narcoscope pin: {verb} · dataAsOf={candidate['dataAsOf']} · "
                f"sha256={receipt['current']['sha256']}"
            )
            return 0
        if changed:
            # Either replacement can be interrupted, so every consumer validates
            # both files together and fails closed on a mismatch.  Artifact first
            # ensures stale metadata can never bless new bytes.
            _atomic_write(args.artifact, candidate_bytes)
            _atomic_write(args.receipt, canonical_json_bytes(receipt))
        print(
            f"narcoscope pin: {'updated' if changed else 'current'} · "
            f"dataAsOf={candidate['dataAsOf']} · sha256={receipt['current']['sha256']}"
        )
        return 0
    except (FetchError, NarcoScopeBridgeError, OSError) as exc:
        print(f"narcoscope pin: refused: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
