"""Compare private UCDP reacquisition with the reviewed annual aggregates offline."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from collectors import ucdp_bulk as bulk
from core.ucdp_aggregate import (
    UCDPAggregateError,
    canonical_json_bytes,
    sha256_bytes,
    validate_public_bytes,
)
from scripts.ucdp_bulk_pull import (
    DEFAULT_REGISTRY,
    DEFAULT_REVIEW_LOCK,
    MAX_DERIVED_BYTES,
    ROOT,
    _offline_evidence,
    _read_bounded,
)


def _compare_rows(current, previous, identity_fields, previous_digest):
    old_digest = sha256_bytes(canonical_json_bytes(previous))
    if previous_digest != old_digest or len(current) != len(previous):
        raise bulk.UCDPBulkError("reviewed aggregate digest or row count differs")
    fresh = [row.to_dict() for row in current]
    for original, candidate in zip(previous, fresh, strict=True):
        if original.keys() != candidate.keys() or any(
            original[key] != candidate[key] for key in original.keys() - identity_fields
        ):
            raise bulk.UCDPBulkError(
                "annual observations differ from the reviewed aggregate"
            )
    return sha256_bytes(canonical_json_bytes(fresh))


def compare(directory: Path) -> dict:
    registry = bulk.load_registry(DEFAULT_REGISTRY)
    lock = bulk.load_review_lock(DEFAULT_REVIEW_LOCK)
    decision = lock.rights_decision
    expected = lock.expected_public_aggregate
    if decision is None or expected is None:
        raise bulk.UCDPBulkError("comparison requires an existing approved baseline")
    old_bytes = _read_bounded(
        ROOT / "readings/ucdp-aggregate-latest.json",
        maximum_bytes=MAX_DERIVED_BYTES,
        label="reviewed aggregate",
    )
    old = validate_public_bytes(old_bytes, schema_path=bulk.DEFAULT_PUBLIC_SCHEMA)
    if (
        old["review_lock_sha256"] != lock.raw_sha256
        or old["registry_sha256"] != registry.raw_sha256
    ):
        raise bulk.UCDPBulkError("public aggregate, registry and baseline lock differ")

    archives, receipts, snapshot, rights = _offline_evidence(registry, directory)
    if (sha256_bytes(snapshot), len(snapshot), rights.rights_page_url) != (
        decision.rights_page_snapshot_sha256,
        decision.rights_page_snapshot_bytes,
        decision.rights_page_url,
    ):
        raise bulk.UCDPBulkError(
            "rights snapshot changed and requires a new source assessment"
        )
    retrievals = [r.retrieved_at for r in receipts.values()]
    if max(retrievals) - min(retrievals) > timedelta(
        seconds=lock.policy.maximum_cross_input_retrieval_skew_seconds
    ):
        raise bulk.UCDPBulkError("acquisition clocks exceed the reviewed skew")
    if rights.observed_at < max(retrievals):
        raise bulk.UCDPBulkError("rights snapshot predates the final acquisition")
    rows = {}
    pins = []
    for previous in lock.inputs:
        key = previous.input_id
        receipt = receipts[key]
        member = bulk.extract_member(archives[key], registry.inputs[key])
        if (
            receipt.archive_sha256,
            receipt.archive_bytes,
            member.sha256,
            len(member.raw),
            bulk._transport_policy_sha256(receipt),
        ) != (
            previous.archive_sha256,
            previous.archive_bytes,
            previous.member_sha256,
            previous.member_bytes,
            previous.transport_policy_sha256,
        ):
            raise bulk.UCDPBulkError(f"{key} content or acquisition policy differs")
        rows[key] = bulk._csv_rows(member.raw, spec=registry.inputs[key])
        pins.append(
            {
                **previous.to_dict(),
                "receipt_sha256": sha256_bytes(canonical_json_bytes(receipt.to_dict())),
            }
        )

    ids, ids_digest = bulk._parse_actor_registry(rows["actor_registry"])
    if (len(ids), ids_digest) != (
        expected.actor_registry_id_count,
        expected.actor_registry_ids_sha256,
    ):
        raise bulk.UCDPBulkError("actor registry coverage differs")
    conflicts = bulk._parse_conflicts(
        rows["armed_conflict"],
        registry=registry,
        actor_ids=ids,
        armed_receipt=receipts["armed_conflict"],
        actor_receipt=receipts["actor_registry"],
    )
    countries = bulk._parse_country_years(
        rows["organized_country_year"],
        registry=registry,
        receipt=receipts["organized_country_year"],
    )
    conflict_digest = _compare_rows(
        conflicts,
        old["conflict_years"],
        {"record_id", "armed_conflict_acquisition_id", "actor_registry_acquisition_id"},
        expected.conflict_years_sha256,
    )
    country_digest = _compare_rows(
        countries,
        old["country_years"],
        {"record_id", "country_year_acquisition_id"},
        expected.country_years_sha256,
    )
    rights_metadata = rights.to_dict()
    return {
        "status": "comparison_only",
        "dataset_version": lock.dataset_version,
        "baseline_lock_sha256": lock.raw_sha256,
        "baseline_aggregate_sha256": sha256_bytes(old_bytes),
        "inputs": pins,
        "rights_snapshot": {
            "observed_at": rights_metadata["observed_at"],
            "rights_page_url": rights.rights_page_url,
            "snapshot_bytes": len(snapshot),
            "snapshot_sha256": rights.snapshot_sha256,
            "receipt_sha256": sha256_bytes(canonical_json_bytes(rights_metadata)),
        },
        "retrieved_at": {k: v.to_dict()["retrieved_at"] for k, v in receipts.items()},
        "counts": {"conflict_years": len(conflicts), "country_years": len(countries)},
        "expected_public_aggregate": bulk.UCDPExpectedPublicAggregate(
            len(ids),
            ids_digest,
            conflict_digest,
            country_digest,
        ).to_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="canonical private acquisition directory",
    )
    args = parser.parse_args()
    try:
        report = compare(args.input_dir)
    except (OSError, UCDPAggregateError, bulk.UCDPBulkError) as exc:
        print(f"UCDP comparison failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
