#!/usr/bin/env python3
"""Collect bounded passive Cloudflare Radar TCP reset/timeout telemetry.

The country scope and aggregation are committed in config.  There are no CLI
endpoint, country, or backfill overrides. Set CLOUDFLARE_API_TOKEN for a
standalone run; production uses its collector-only Docker secret. Without
either, this command exits successfully in a neutral gated/skipped state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from collectors.cloudflare_radar_tcp import (
    DEFAULT_CONFIG,
    DEFAULT_READINGS,
    RadarError,
    collect_and_publish,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="committed geography allowlist, aggregation, provenance, and bounds",
    )
    parser.add_argument(
        "--readings",
        type=Path,
        default=DEFAULT_READINGS,
        help="directory for normalized latest and append-only history outputs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = collect_and_publish(config_path=args.config, readings=args.readings)
    except RadarError as exc:
        # RadarError messages are intentionally credential-free and never include
        # response bodies or Authorization headers.
        print(
            json.dumps(
                {"status": "error", "error": type(exc).__name__, "detail": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
