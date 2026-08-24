#!/usr/bin/env python3
"""Collect a broad China WDI history into the quarantined economic ledger.

The default target is ``data/review``.  Promotion into the public Palimpsest
ledger is intentionally an explicit operator choice after reviewing the exact
source catalogue, licence evidence, response shape and generated receipt.
"""
from __future__ import annotations

import argparse
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from collectors.world_bank_wdi import WDIError, collect, load_registry
from core.china_econ_export import load_availability_receipt, load_source_policy
from core.collector_artifact import build_artifact, canonical_json_bytes
from core.econ_ledger import LedgerIntegrityError, append_vintages, load_snapshot


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "china_econ_wdi_series.json"
DEFAULT_POLICY = ROOT / "config" / "china_econ_source_policy.json"
DEFAULT_LEDGER = ROOT / "data" / "review" / "china-econ-wdi-observations.jsonl"
DEFAULT_LATEST = ROOT / "data" / "review" / "china-econ-wdi-latest.json"
PUBLIC_LEDGER = ROOT / "readings" / "china-econ-wdi-observations.jsonl"
PUBLIC_LATEST = ROOT / "readings" / "china-econ-wdi-latest.json"
LATEST_SCHEMA = "palimpsest-china-econ-wdi-run.v3"
AVAILABILITY_SCHEMA = "palimpsest-china-econ-wdi-availability.v1"
INDICATOR_PROVENANCE_SCHEMA = "palimpsest-china-econ-wdi-indicator-provenance.v1"


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
        raise WDIError(f"cannot resolve {label} path: {exc}") from exc


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
                raise WDIError(
                    f"mutable outputs {left_label} and {right_label} resolve to the same file"
                )
        for input_label, input_path in inputs.items():
            if (
                resolved_outputs[left_label] == resolved_inputs[input_label]
                or _same_existing_file(left_path, input_path)
            ):
                raise WDIError(
                    f"mutable output {left_label} collides with input {input_label}"
                )


def _ledger_receipt(snapshot) -> dict[str, int | str]:
    return {
        "sha256": snapshot.byte_sha256,
        "bytes": snapshot.byte_size,
        "records": snapshot.records,
    }


def _period_coverage(observations) -> tuple[str | None, str | None]:
    rows = tuple(observations)
    if not rows:
        return None, None
    return (
        min(row.period_start for row in rows).isoformat(),
        max(row.period_end for row in rows).isoformat(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--latest", type=Path, default=DEFAULT_LATEST)
    parser.add_argument("--start-year", type=int, default=1960)
    parser.add_argument("--end-year", type=int, default=datetime.now(UTC).year)
    parser.add_argument(
        "--input",
        type=Path,
        help="read exact response bytes from disk instead of making an outbound request",
    )
    parser.add_argument(
        "--public-context-only",
        action="store_true",
        help=(
            "write only the reviewed, attributed readings paths after the exact "
            "source-policy gate; values remain context-only and non-scoring"
        ),
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="collect and validate only")
    modes.add_argument(
        "--validate-only",
        action="store_true",
        help="validate registry and existing ledger without collection",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = {"registry": args.registry}
        if args.public_context_only:
            inputs["policy"] = args.policy
        if args.input is not None:
            inputs["input"] = args.input
        _refuse_path_collisions(
            inputs=inputs,
            outputs={"ledger": args.ledger, "latest": args.latest},
        )
        registry = load_registry(args.registry)
        collected_at = datetime.now(UTC)
        if args.public_context_only:
            if args.input is not None:
                raise WDIError(
                    "public-context-only collection requires the reviewed HTTPS fetch path"
                )
            if (
                _resolved(args.registry, label="registry")
                != _resolved(DEFAULT_REGISTRY, label="reviewed registry")
                or _resolved(args.policy, label="policy")
                != _resolved(DEFAULT_POLICY, label="reviewed policy")
                or _resolved(args.ledger, label="ledger")
                != _resolved(PUBLIC_LEDGER, label="public ledger")
                or _resolved(args.latest, label="latest")
                != _resolved(PUBLIC_LATEST, label="public latest")
            ):
                raise WDIError(
                    "public-context-only writes require the exact reviewed inputs and readings paths"
                )
            policy = load_source_policy(args.policy)
            decision = policy.decisions.get("world_bank_wdi")
            if (
                decision is None
                or decision.decision != "allow"
                or not decision.values_allowed
                or not decision.seiche_export_allowed
                or decision.license != "CC-BY-4.0"
                or decision.reviewed_at_value > collected_at
                or decision.expires_at_value <= collected_at
            ):
                raise WDIError("world_bank_wdi is not currently allowed for publication")
        before = load_snapshot(args.ledger)
        if args.validate_only:
            if args.public_context_only:
                public_availability = load_availability_receipt(
                    args.latest,
                    series_registry_path=args.registry,
                )
                if public_availability.ledger_after != _ledger_receipt(before):
                    raise WDIError(
                        "public availability receipt is not bound to the exact ledger"
                    )
            print(
                "china-econ-wdi: valid "
                f"({len(registry.bindings)} configured series, "
                f"{before.records} quarantined observations)"
            )
            return 0
        response = collect(
            registry,
            start_year=args.start_year,
            end_year=args.end_year,
            collected_at=collected_at,
            fetch=(lambda _url: args.input.read_bytes()) if args.input else None,
        )
        if args.public_context_only and args.latest.exists():
            prior_availability = load_availability_receipt(
                args.latest,
                series_registry_path=args.registry,
            )
            if prior_availability.ledger_after != _ledger_receipt(before):
                raise WDIError(
                    "public availability receipt is not bound to the exact prior ledger"
                )
            current_identities = {
                (row.indicator_id, row.year)
                for row in response.availability
                if row.available
            }
            withdrawn = (
                prior_availability.current_numeric_identities - current_identities
            )
            if withdrawn:
                indicator_id, year = sorted(withdrawn)[0]
                raise WDIError(
                    "public-context-only refuses a missing/null previously numeric "
                    f"identity until the withdrawal contract is reviewed: "
                    f"{indicator_id}/{year}"
                )
        if args.dry_run:
            print(
                "china-econ-wdi: dry-run "
                f"observations={len(response.observations)} "
                f"series={len({row.series_id for row in response.observations})} "
                f"null_rows={response.null_rows} "
                f"raw_sha256={response.raw_sha256}"
            )
            return 0
        appended = append_vintages(args.ledger, response.observations)
        after = load_snapshot(args.ledger)
    except (
        LedgerIntegrityError,
        OSError,
        TypeError,
        ValueError,
        WDIError,
    ) as exc:
        print(f"china-econ-wdi refused: {exc}")
        return 2

    response_period_start, response_period_end = _period_coverage(response.observations)
    ledger_period_start, ledger_period_end = _period_coverage(after.observations)
    response_coverage = {
        "coverage_semantics": "exact_current_response",
        "requested_start_year": response.requested_start_year,
        "requested_end_year": response.requested_end_year,
        "configured_indicators": len(registry.bindings),
        "represented_indicators": len(response.represented_indicators),
        "populated_indicators": len(response.populated_indicators),
        "null_only_indicators": len(
            set(response.represented_indicators) - set(response.populated_indicators)
        ),
        "source_rows": response.source_rows,
        "populated_observations": len(response.observations),
        "null_rows": response.null_rows,
        "period_start": response_period_start,
        "period_end": response_period_end,
    }
    ledger_coverage = {
        "coverage_semantics": "accumulated_append_only_history_not_current_response",
        "records": after.records,
        "series_count": len({row.series_id for row in after.observations}),
        "period_start": ledger_period_start,
        "period_end": ledger_period_end,
    }
    availability = {
        "schema_version": AVAILABILITY_SCHEMA,
        "records": len(response.availability),
        "null_records": response.null_rows,
        "entries": [row.to_dict() for row in response.availability],
        "coverage_semantics": "exact_current_response",
        "withdrawal_state": "residual_gate_no_append_only_withdrawal_ledger",
        "withdrawal_limitation": (
            "An unavailable indicator/year in this exact response is not appended as a "
            "numeric observation. Any older value retained in the accumulated ledger must "
            "not be treated as present in current-response coverage."
        ),
    }
    indicator_provenance = {
        "schema_version": INDICATOR_PROVENANCE_SCHEMA,
        "records": len(response.indicator_provenance),
        "entries": [row.to_dict() for row in response.indicator_provenance],
        "upstream_attribution_state": registry.dataset[
            "per_indicator_upstream_metadata_status"
        ],
        "upstream_attribution_requirement": registry.dataset[
            "per_indicator_upstream_metadata_requirement"
        ],
    }
    payload = {
        "schema_version": LATEST_SCHEMA,
        "generated_at": _timestamp(collected_at),
        "source_id": registry.dataset["source_id"],
        "dataset": registry.dataset["name"],
        "dataset_last_updated": response.dataset_last_updated.isoformat(),
        "license": registry.dataset["license"],
        "license_url": registry.dataset["license_url"],
        "rights_evidence_url": registry.dataset["rights_evidence_url"],
        "redistribution_status": registry.dataset["redistribution_status"],
        "batch_raw_sha256": response.raw_sha256,
        "context_only": True,
        "scoring_allowed": False,
        "appended_observations": len(appended),
        "ledger_before": _ledger_receipt(before),
        "ledger_after": _ledger_receipt(after),
        "response_coverage": response_coverage,
        "ledger_coverage": ledger_coverage,
        "availability": availability,
        "indicator_provenance": indicator_provenance,
        "publication_state": (
            "public_context_only" if args.public_context_only else "review_only"
        ),
        "revision_lineage": {
            "mode": (
                "git_tracked_append_only"
                if args.public_context_only
                else "local_review_append_only"
            ),
            "durable_cross_run": args.public_context_only,
            "ledger_path": (
                "readings/china-econ-wdi-observations.jsonl"
                if args.public_context_only
                else args.ledger.name
            ),
        },
        "limitations": [
            "WDI is an annual structural transport, not a live China liquidity feed.",
            "Backfilled rows become knowable to Palimpsest at this capture, not in their historical reference years.",
            "WDI mirrors multiple upstream source families and is not independent confirmation of every official series.",
            "The append-only observation ledger has no withdrawal event contract yet; use the exact-response availability receipt for current-pull coverage.",
            registry.dataset["per_indicator_upstream_metadata_requirement"],
        ],
    }
    age_days = (collected_at.date() - response.dataset_last_updated).days
    artifact = build_artifact(
        collector_id="world-bank-wdi-china",
        source_receipt={
            "url": response.evidence_url,
            "raw_sha256": response.raw_sha256,
            "dataset_last_updated": response.dataset_last_updated.isoformat(),
            "license": registry.dataset["license"],
        },
        freshness={
            "evidence_state": "fresh" if age_days <= 120 else "stale",
            "observed_at": _timestamp(collected_at),
            "native_cadence": "annual",
            "dataset_age_days": max(age_days, 0),
        },
        coverage={
            **response_coverage,
        },
        abstention=None,
        payload=payload,
    )
    document = {**payload, "collector_artifact": artifact}
    _write_atomic(args.latest, canonical_json_bytes(document))
    print(
        "china-econ-wdi: "
        f"appended={len(appended)} total={after.records} "
        f"response_series={len(response.populated_indicators)} "
        f"ledger_series={ledger_coverage['series_count']} "
        f"raw_sha256={response.raw_sha256[:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
