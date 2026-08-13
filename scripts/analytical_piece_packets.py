#!/usr/bin/env python3
"""Prepare private evidence packets or validate deterministic draft templates."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any

from core.analytical_pieces import (
    AnalyticalPieceError,
    build_packet_set,
    build_template_draft_set,
    canonical_json_bytes,
    validate_draft_set,
    validate_packet_set,
)
from core.investigative_candidates import atomic_write, build_candidates


MAX_INPUT_BYTES = 16 * 1024 * 1024


def _reject_constant(value: str) -> None:
    raise AnalyticalPieceError(f"non-finite JSON number is prohibited: {value}")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AnalyticalPieceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read(path: Path) -> dict[str, Any]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INPUT_BYTES:
            raise AnalyticalPieceError(f"JSON input is not a bounded regular file: {path}")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or os.read(descriptor, 1)
            or (before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise AnalyticalPieceError(f"JSON input changed while read: {path}")
        value = json.loads(
            raw,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnalyticalPieceError(f"cannot read strict JSON: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise AnalyticalPieceError(f"JSON root must be an object: {path}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--readings-dir", type=Path, required=True)
    prepare.add_argument("--candidates", type=Path, required=True)
    prepare.add_argument("--packets-output", type=Path, required=True)
    prepare.add_argument("--template-drafts-output", type=Path)

    validate = subparsers.add_parser("validate-drafts")
    validate.add_argument("--packets", type=Path, required=True)
    validate.add_argument("--drafts", type=Path, required=True)
    validate.add_argument("--canonical-output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "prepare":
        candidate = _read(args.candidates)
        rebuilt = build_candidates(args.readings_dir)
        if canonical_json_bytes(candidate) != canonical_json_bytes(rebuilt):
            raise AnalyticalPieceError(
                "candidate input does not derive from the supplied readings"
            )
        packets = build_packet_set(rebuilt)
        atomic_write(args.packets_output, canonical_json_bytes(packets), mode=0o600)
        if args.template_drafts_output:
            drafts = build_template_draft_set(packets)
            atomic_write(
                args.template_drafts_output, canonical_json_bytes(drafts), mode=0o600
            )
        print(
            f"analytical packets -> {packets['edition_id']} · "
            f"{packets['n_packets']} private packets"
        )
        return 0

    packets = _read(args.packets)
    drafts = _read(args.drafts)
    validate_packet_set(packets)
    validate_draft_set(packets, drafts)
    if args.canonical_output:
        atomic_write(args.canonical_output, canonical_json_bytes(drafts), mode=0o600)
    print(f"analytical drafts valid -> {drafts['n_drafts']} private drafts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
