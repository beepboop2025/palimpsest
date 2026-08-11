#!/usr/bin/env python3
"""Ingest one bounded OONI bulk hour into the private warehouse.

With no ``--hour`` this selects exactly the configured latest lagged UTC hour.
``--hour`` selects exactly one older hour for an operator-directed repair.  No
date range or implicit historical backfill is exposed by this CLI.

Run from the repository root as ``python3 -m scripts.ooni_bulk_ingest``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from collectors.ooni_bulk import (
    DEFAULT_CONFIG,
    DEFAULT_READINGS,
    ingest_hour,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hour",
        help="one UTC hour as YYYY-MM-DDTHH; default is the configured lagged hour",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="allowlist and hard-limit config",
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=None,
        help="private warehouse root (or PALIMPSEST_OONI_WAREHOUSE_DIR)",
    )
    parser.add_argument(
        "--readings",
        type=Path,
        default=DEFAULT_READINGS,
        help="directory for the bounded public latest/history rollups",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = ingest_hour(
        config_path=args.config,
        hour=args.hour,
        warehouse=args.warehouse,
        readings=args.readings,
    )
    # The result is deliberately aggregate-only: do not print request URLs,
    # S3 keys, measurement inputs, or local object paths into shared logs.
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"success", "halted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
