#!/usr/bin/env python3
"""Build the deterministic China named-series forecast/backtest artifact.

Usage:
    PYTHONPATH=. python -m scripts.build_china_econ_forecast
    PYTHONPATH=. python -m scripts.build_china_econ_forecast --check
"""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Iterable

from core.economic_forecast import canonical_json_bytes
from processors.china_econ_backtest import ForecastBuildError, build_forecast_document


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "readings" / "china-econ-observations.jsonl"
DEFAULT_CONFIG = ROOT / "config" / "china_econ_targets.json"
DEFAULT_REGISTRY = ROOT / "config" / "china_econ_sources.json"
DEFAULT_OUTPUT = ROOT / "readings" / "china-econ-forecast-latest.json"
PUBLIC_LEDGER_PATH = "readings/china-econ-observations.jsonl"


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build(
    *,
    ledger: Path = DEFAULT_LEDGER,
    config: Path = DEFAULT_CONFIG,
    registry: Path = DEFAULT_REGISTRY,
    ledger_artifact_path: str = PUBLIC_LEDGER_PATH,
) -> dict[str, object]:
    return build_forecast_document(
        ledger_path=ledger,
        config_path=config,
        source_registry_path=registry,
        ledger_artifact_path=ledger_artifact_path,
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ledger-artifact-path", default=PUBLIC_LEDGER_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="build in memory and require exact current output; write nothing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        document = build(
            ledger=args.ledger,
            config=args.config,
            registry=args.registry,
            ledger_artifact_path=args.ledger_artifact_path,
        )
        payload = canonical_json_bytes(document, pretty=True)
    except (ForecastBuildError, OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    if args.check:
        try:
            current = args.output.read_bytes()
        except FileNotFoundError:
            print(f"missing {args.output}")
            return 1
        if current != payload:
            print(f"stale {args.output}")
            return 1
        print(
            f"china economic forecast current · {document['summary']['targets']} targets · "
            f"{document['status']}"
        )
        return 0

    _write_atomic(args.output, payload)
    print(
        f"china economic forecast -> {args.output} · "
        f"{document['summary']['targets']} targets · {document['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
