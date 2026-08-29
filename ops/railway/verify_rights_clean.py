#!/usr/bin/env python3
"""Independently prove a Railway static bundle contains no denied China values.

This verifier deliberately does not import ``scripts.stage_pages_rights``.  The
staging gate decides what to replace; this program records exact identities
from the denied ledger before staging and then checks every served byte after
staging.  Agreement between two implementations is the publication boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


LEDGER_PATH = Path("readings/china-econ-observations.jsonl")
SENTINEL_FIELDS = ("observation_id", "raw_sha256")
SENTINEL_RE = re.compile(rb"[0-9a-f]{32,128}\Z")
HEX_RUN_RE = re.compile(rb"[0-9a-f]{32,}")
MAX_PUBLIC_FILE_BYTES = 64 * 1024 * 1024
MAX_FAILURES = 64
DIRECT_VALUE_RE = re.compile(
    rb"""["'](?:fdr001|fdr007|fdr014|fr001|fr007|fr014|"""
    rb"""shibor_(?:on|1w|2w|1m|3m|6m|9m|1y)|usdcny_parity)["']"""
    rb"""\s*(?::|=)\s*["']?[+-]?(?:\d+(?:\.\d*)?|\.\d+)""",
    re.IGNORECASE,
)
MAPPING_VALUE_RE = re.compile(
    rb"""["'](?:cfets_benchmarks|chinamoney)["']\s*:\s*"""
    rb"""["']?[+-]?(?:\d+(?:\.\d*)?|\.\d+)""",
    re.IGNORECASE,
)


class RightsScanError(ValueError):
    """The independent static-publication scan could not prove a clean tree."""


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RightsScanError(f"refusing unsafe public path: {path}")
    size = path.stat().st_size
    if size > MAX_PUBLIC_FILE_BYTES:
        raise RightsScanError(f"public file exceeds independent scan cap: {path}")
    return path.read_bytes()


def _public_files(root: Path) -> list[Path]:
    root = root.resolve(strict=True)
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RightsScanError(f"public tree contains a symbolic link: {path}")
        if path.is_file():
            files.append(path)
    if not files:
        raise RightsScanError("public tree is empty")
    return files


def capture_sentinels(root: Path, output: Path) -> dict[str, int]:
    root = root.resolve(strict=True)
    output = output.resolve(strict=False)
    if _inside(root, output):
        raise RightsScanError("denied sentinels must remain outside the public tree")
    if output.exists() or output.is_symlink():
        raise RightsScanError("refusing to overwrite denied sentinel file")
    ledger = root / LEDGER_PATH
    rows = 0
    sentinels: set[bytes] = set()
    for line_number, raw_line in enumerate(_regular_bytes(ledger).splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RightsScanError(
                f"denied ledger line {line_number} is not JSON"
            ) from exc
        if not isinstance(row, dict):
            raise RightsScanError(f"denied ledger line {line_number} is not an object")
        rows += 1
        for field in SENTINEL_FIELDS:
            value = row.get(field)
            if not isinstance(value, str):
                raise RightsScanError(f"denied ledger line {line_number} lacks {field}")
            encoded = value.encode("ascii", errors="strict")
            if SENTINEL_RE.fullmatch(encoded) is None:
                raise RightsScanError(
                    f"denied ledger line {line_number} has invalid {field}"
                )
            sentinels.add(encoded)
    if rows == 0 or not sentinels:
        raise RightsScanError("denied ledger sentinel set is empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"\n".join(sorted(sentinels)) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"ledger_rows": rows, "sentinels": len(sentinels)}


def _load_sentinels(path: Path) -> tuple[bytes, ...]:
    raw = _regular_bytes(path)
    values = tuple(line for line in raw.splitlines() if line)
    if not values or values != tuple(sorted(set(values))):
        raise RightsScanError("denied sentinel file is empty or non-canonical")
    if any(SENTINEL_RE.fullmatch(value) is None for value in values):
        raise RightsScanError("denied sentinel file contains an invalid identity")
    return values


def _sentinels_by_length(
    sentinels: tuple[bytes, ...],
) -> tuple[tuple[int, frozenset[bytes]], ...]:
    grouped: dict[int, set[bytes]] = {}
    for sentinel in sentinels:
        grouped.setdefault(len(sentinel), set()).add(sentinel)
    return tuple(
        (length, frozenset(values)) for length, values in sorted(grouped.items())
    )


def _contains_denied_sentinel(
    raw: bytes,
    grouped_sentinels: tuple[tuple[int, frozenset[bytes]], ...],
) -> bool:
    """Find exact sentinel bytes by scanning only candidate hexadecimal runs."""

    for match in HEX_RUN_RE.finditer(raw):
        candidate = match.group()
        for length, sentinels in grouped_sentinels:
            if len(candidate) < length:
                continue
            if len(candidate) == length:
                if candidate in sentinels:
                    return True
                continue
            for offset in range(len(candidate) - length + 1):
                if candidate[offset : offset + length] in sentinels:
                    return True
    return False


def verify_clean(root: Path, sentinel_path: Path) -> dict[str, int]:
    root = root.resolve(strict=True)
    sentinel_path = sentinel_path.resolve(strict=True)
    if _inside(root, sentinel_path):
        raise RightsScanError("denied sentinels must remain outside the public tree")
    sentinels = _load_sentinels(sentinel_path)
    grouped_sentinels = _sentinels_by_length(sentinels)
    failures: list[str] = []
    scanned_bytes = 0
    files = _public_files(root)
    for path in files:
        raw = _regular_bytes(path)
        scanned_bytes += len(raw)
        relative = path.relative_to(root).as_posix()
        if _contains_denied_sentinel(raw, grouped_sentinels):
            failures.append(f"sentinel:{relative}")
        if DIRECT_VALUE_RE.search(raw) or MAPPING_VALUE_RE.search(raw):
            failures.append(f"forbidden-value:{relative}")
        if len(failures) >= MAX_FAILURES:
            break
    if failures:
        raise RightsScanError(
            "Railway artifact retained denied China evidence: " + ", ".join(failures)
        )
    return {
        "files": len(files),
        "bytes": scanned_bytes,
        "sentinels": len(sentinels),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--root", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--sentinels", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            result = capture_sentinels(args.root, args.output)
        else:
            result = verify_clean(args.root, args.sentinels)
    except (OSError, UnicodeError, RightsScanError, ValueError) as exc:
        print(f"railway-rights-scan refused: {exc}")
        return 2
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
