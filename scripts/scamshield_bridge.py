#!/usr/bin/env python3
"""Read one ScamShield assessment on stdin and emit an Evidence Capsule.

This is intentionally a local, one-shot bridge.  It has no listener, performs
no network access, and never receives the raw Telegram message by default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evidence.capsule import CapsuleError  # noqa: E402
from evidence.scamshield import MAX_ASSESSMENT_BYTES, capsule_from_assessment  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outbox", default="",
        help="optional path beneath the Palimpsest root for durable capsule intake",
    )
    return parser


def _store(capsule: dict, relative_outbox: str) -> Path:
    candidate = Path(relative_outbox)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("outbox must be a safe relative path beneath Palimpsest")
    root = ROOT.resolve()
    outbox = (root / candidate).resolve()
    if not outbox.is_relative_to(root):
        raise ValueError("outbox escapes the Palimpsest root")
    outbox.mkdir(parents=True, exist_ok=True)
    capsule_id = capsule["content_sha256"]
    target = outbox / f"{capsule_id}.json"
    encoded = (
        json.dumps(capsule, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    if target.exists():
        if target.read_bytes() != encoded:
            raise ValueError("existing capsule path has different bytes")
        return target

    fd, temporary_name = tempfile.mkstemp(prefix=".scamshield-", dir=outbox)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != encoded:
                raise ValueError("capsule write raced with different bytes")
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_ASSESSMENT_BYTES + 1)
    if len(raw) > MAX_ASSESSMENT_BYTES:
        print("scamshield bridge: assessment exceeds 1 MiB", file=sys.stderr)
        return 2
    try:
        capsule = capsule_from_assessment(raw)
        if args.outbox:
            _store(capsule, args.outbox)
    except (CapsuleError, TypeError, ValueError) as exc:
        print(f"scamshield bridge: {exc}", file=sys.stderr)
        return 2
    json.dump(capsule, sys.stdout, ensure_ascii=False, sort_keys=True,
              separators=(",", ":"), allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
