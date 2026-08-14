#!/usr/bin/env python3
"""Import one operator-reviewed, digest-only MyQuant evaluation receipt.

This command has no fetch mode and no remote credentials.  The positional input is a
local handoff file; its path is never written to the public artifacts.  Run it only in
the Palimpsest publisher checkout, then inspect, seal, verify, commit, and use the
existing ``push_data_commit.py`` reconciliation path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.myquant_model_evidence import (  # noqa: E402
    EvidenceImportError,
    import_envelope,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "envelope",
        type=Path,
        help="local sanitized envelope (the input path is never published)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = import_envelope(args.envelope)
    except (EvidenceImportError, OSError) as exc:
        print(f"myquant-evidence: REFUSING: {exc}", file=sys.stderr)
        return 2
    action = "imported" if result.changed else "already reconciled"
    print(
        f"myquant-evidence: {action} {result.kind} "
        f"receipt={result.receipt_sha256} registry_seq={result.registry_seq}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
