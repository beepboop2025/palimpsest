#!/usr/bin/env python3
"""Build the review-only, rights-gated China economic export for Seiche.

The default ledger and both outputs live under ``data/review``.  Supplying a
different output path is an explicit operator action; this command does not
publish, schedule, upload, or modify the public economic-observation ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from core.china_econ_export import ChinaEconExportError, build_export
from core.econ_ledger import LedgerIntegrityError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "data" / "review" / "china-econ-wdi-observations.jsonl"
DEFAULT_POLICY = ROOT / "config" / "china_econ_source_policy.json"
DEFAULT_SERIES_REGISTRY = ROOT / "config" / "china_econ_wdi_series.json"
DEFAULT_OUTPUT = ROOT / "data" / "review" / "palimpsest-china-economic-export-v1.jsonl"
DEFAULT_MANIFEST = (
    ROOT / "data" / "review" / "palimpsest-china-economic-export-v1-manifest.json"
)


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
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _resolved(path: Path, *, label: str) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ChinaEconExportError(f"cannot resolve {label} path: {exc}") from exc


def _same_existing_file(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _refuse_path_collisions(
    *,
    inputs: Mapping[str, Path],
    outputs: Mapping[str, Path],
) -> None:
    resolved_inputs = {
        label: _resolved(path, label=label) for label, path in inputs.items()
    }
    resolved_outputs = {
        label: _resolved(path, label=label) for label, path in outputs.items()
    }
    output_items = list(outputs.items())
    for position, (left_label, left_path) in enumerate(output_items):
        for right_label, right_path in output_items[position + 1 :]:
            if (
                resolved_outputs[left_label] == resolved_outputs[right_label]
                or _same_existing_file(left_path, right_path)
            ):
                raise ChinaEconExportError(
                    f"mutable outputs {left_label} and {right_label} resolve to the same file"
                )
        for input_label, input_path in inputs.items():
            if (
                resolved_outputs[left_label] == resolved_inputs[input_label]
                or _same_existing_file(left_path, input_path)
            ):
                raise ChinaEconExportError(
                    f"mutable output {left_label} collides with input {input_label}"
                )


def _parse_timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise argparse.ArgumentTypeError("generated-at must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("generated-at must be ISO-8601") from exc
    normalized = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if normalized != value:
        raise argparse.ArgumentTypeError("generated-at must be canonically encoded")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--series-registry", type=Path, default=DEFAULT_SERIES_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--generated-at",
        type=_parse_timestamp,
        help="deterministic UTC evaluation clock; defaults to current UTC",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _refuse_path_collisions(
            inputs={
                "ledger": args.ledger,
                "policy": args.policy,
                "series_registry": args.series_registry,
            },
            outputs={"output": args.output, "manifest": args.manifest},
        )
        generated_at = args.generated_at or datetime.now(UTC)
        bundle = build_export(
            ledger_path=args.ledger,
            policy_path=args.policy,
            series_registry_path=args.series_registry,
            generated_at=generated_at,
            artifact_name=args.output.name,
        )
        _atomic_write(args.output, bundle.artifact_bytes)
        _atomic_write(args.manifest, bundle.manifest_bytes)
    except (ChinaEconExportError, LedgerIntegrityError, OSError, TypeError, ValueError) as exc:
        print(f"china-economic-export refused: {exc}")
        return 2

    manifest = json.loads(bundle.manifest_bytes)
    print(
        "china-economic-export: review-only "
        f"records={manifest['artifact']['records']} "
        f"bytes={manifest['artifact']['bytes']} "
        f"sha256={manifest['artifact']['sha256'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_LEDGER",
    "DEFAULT_MANIFEST",
    "DEFAULT_OUTPUT",
    "DEFAULT_POLICY",
    "DEFAULT_SERIES_REGISTRY",
    "main",
]
