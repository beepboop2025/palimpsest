#!/usr/bin/env python3
"""Publish one bounded, metadata-only snapshot of the research-corpus allowlist."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ``python -m scripts.research_corpus_ingest`` already has the repository on sys.path.
# Keep the equally natural direct form working without requiring a caller-set PYTHONPATH.
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from collectors.research_corpus import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_READINGS,
    ResearchCorpusError,
    run_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="reviewed source allowlist and hard-limit config",
    )
    parser.add_argument(
        "--readings",
        type=Path,
        default=DEFAULT_READINGS,
        help="directory for research-corpus latest/history outputs",
    )
    return parser


def _summary(result: dict) -> dict:
    keys = (
        "collector",
        "status",
        "generated_at",
        "last_changed_at",
        "n_sources",
        "n_changed",
        "n_unchanged",
        "n_initial",
        "sources_expected",
        "sources_completed",
        "requests_made",
        "bytes_received",
        "snapshot_sha256",
        "publication",
        "error",
    )
    return {key: result[key] for key in keys if key in result}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_snapshot(config_path=args.config, readings=args.readings)
    except ResearchCorpusError as exc:
        # Errors are already source-id scoped and URL-free.  Do not dump a traceback or any
        # transport object that could carry request metadata into shared job logs.
        print(
            json.dumps(
                {
                    "collector": "research-corpus",
                    "status": "failed",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(_summary(result), ensure_ascii=False, sort_keys=True))
    # A Git transport outage is an explicit abstention: no latest/history file moved, and
    # the fleet observes that unchanged commit point as ``abstained``. Configuration,
    # validation, and limit failures still return non-zero through the exception path.
    return 0 if result.get("status") in {"success", "halted", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
