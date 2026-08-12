#!/usr/bin/env python3
"""Normalize the consented DNS panel into the longitudinal round ledger."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from core.network_rounds import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_PATH,
    build_network_rounds,
    canonical_json_bytes,
    load_network_panel_config,
    validate_network_rounds,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSIDE_VIEW = ROOT / "readings" / "inside-view-latest.json"
DEFAULT_OUTAGE = ROOT / "readings" / "ioda-outages-latest.json"


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError(f"{path} must contain a JSON object")
    return value


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
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside-view", type=Path, default=DEFAULT_INSIDE_VIEW)
    parser.add_argument("--outage", type=Path, default=DEFAULT_OUTAGE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    config = load_network_panel_config(args.config)
    prior_rounds = []
    if args.output.exists():
        prior = _load(args.output)
        validate_network_rounds(prior, config=config)
        prior_rounds = prior["rounds"]
    document = build_network_rounds(
        _load(args.inside_view),
        outage=_load(args.outage) if args.outage.exists() else None,
        config=config,
        prior_rounds=prior_rounds,
    )
    payload = canonical_json_bytes(document)
    if args.check:
        if not args.output.exists() or args.output.read_bytes() != payload:
            print(f"stale or missing {args.output}")
            return 1
        print(
            f"network-rounds: current ({document['n_rounds']} rounds, "
            f"{document['n_comparable_rounds']} comparable)"
        )
        return 0
    _atomic_write(args.output, payload)
    print(
        f"network-rounds: {document['n_rounds']} rounds, "
        f"{document['n_comparable_rounds']} comparable, "
        f"status={document['longitudinal_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
