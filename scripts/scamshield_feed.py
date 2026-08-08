#!/usr/bin/env python3
"""Emit privacy-minimized ScamShield review candidates as JSON Lines.

The command performs no network access and never publishes by itself.  Its
default excludes message-only provenance matches; pass
``--include-typology-matches`` only for a controlled analyst surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evidence.capsule import MAX_CAPSULE_BYTES, CapsuleError, strict_json_loads  # noqa: E402
from evidence.scamshield import public_record_from_capsule  # noqa: E402

MAX_FILES = 10_000
MAX_TOTAL_BYTES = 256 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", default="var/scamshield-inbox")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--include-typology-matches", action="store_true")
    return parser


def _safe_inbox(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("inbox must be a safe relative path beneath Palimpsest")
    root = ROOT.resolve()
    inbox = (root / candidate).resolve()
    if not inbox.is_relative_to(root):
        raise ValueError("inbox escapes the Palimpsest root")
    return inbox


def publication_candidate(capsule: dict, *, include_typology_matches: bool) -> dict:
    record = public_record_from_capsule(capsule)
    if not include_typology_matches:
        hypotheses = [
            item for item in record["hypotheses"]
            if item["support_level"] in {"CORROBORATED_LEAD", "DIRECT_LINK"}
        ]
        if len(hypotheses) != len(record["hypotheses"]):
            record["origin_answer"] = (
                "Message-only source attribution withheld pending independent corroboration."
            )
        record["hypotheses"] = hypotheses
    record["feed_policy"] = {
        "typology_match_visible": include_typology_matches,
        "automatic_publication": False,
    }
    return record


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.limit <= MAX_FILES:
        print(f"scamshield feed: --limit must be in [1, {MAX_FILES}]", file=sys.stderr)
        return 2
    try:
        inbox = _safe_inbox(args.inbox)
        if not inbox.is_dir():
            raise ValueError(f"inbox does not exist: {inbox}")
        paths = sorted(inbox.glob("*.json"), key=lambda path: path.name)[:args.limit]
        total = 0
        for path in paths:
            if path.is_symlink():
                raise ValueError(f"feed refuses symlinked capsule: {path.name}")
            resolved = path.resolve()
            if not resolved.is_relative_to(inbox) or not resolved.is_file():
                raise ValueError(f"capsule path escapes the inbox: {path.name}")
            path = resolved
            size = path.stat().st_size
            if size > MAX_CAPSULE_BYTES:
                raise ValueError(f"capsule exceeds limit: {path.name}")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("feed input exceeds the total byte limit")
            capsule = strict_json_loads(path.read_bytes())
            if not isinstance(capsule, dict):
                raise ValueError(f"capsule is not an object: {path.name}")
            record = publication_candidate(
                capsule,
                include_typology_matches=args.include_typology_matches,
            )
            print(json.dumps(
                record, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ))
    except (CapsuleError, OSError, TypeError, ValueError) as exc:
        print(f"scamshield feed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
