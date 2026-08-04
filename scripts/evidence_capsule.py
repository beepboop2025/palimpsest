#!/usr/bin/env python3
"""Build or verify Palimpsest Evidence Capsule v1 files, entirely offline."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evidence.capsule import CapsuleError, load_capsule, verify_capsule  # noqa: E402
from evidence.palimpsest import capsule_from_reading  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="verify a capsule without network or execution")
    verify.add_argument("capsule")
    verify.add_argument("--artifact-root", help="root for path-backed artifacts")
    build = sub.add_parser(
        "palimpsest",
        help="build from an exact Palimpsest reading with entry-membership evidence",
    )
    build.add_argument("--reading", required=True)
    build.add_argument("--source", required=True)
    build.add_argument("--ledger", default=str(ROOT / "readings" / "erasure-ledger.jsonl"))
    build.add_argument("--anchors", default=str(ROOT / "readings" / "anchors.jsonl"))
    build.add_argument("--ledger-name", default="erasure")
    build.add_argument("--source-uri")
    build.add_argument("--created-at")
    build.add_argument("--output", "-o", help="write JSON here (default: stdout)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            capsule = load_capsule(args.capsule)
            report = verify_capsule(capsule, base_dir=args.artifact_root)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["ok"] else 1
        capsule = capsule_from_reading(
            args.reading, source=args.source, ledger_path=args.ledger,
            anchors_path=args.anchors, ledger_name=args.ledger_name,
            repository_root=ROOT, source_uri=args.source_uri,
            created_at=args.created_at,
        )
        rendered = json.dumps(capsule, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    except (CapsuleError, OSError, KeyError, ValueError) as exc:
        print(f"evidence capsule: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
