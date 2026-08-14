"""Fail-closed admission tests for the NarcoScope aggregate handoff."""
from __future__ import annotations

import copy
import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from core.narcoscope_bridge import (
    DEFAULT_ARTIFACT_PATH,
    DEFAULT_RECEIPT_PATH,
    NarcoScopeBridgeError,
    admission_receipt,
    artifact_sha256,
    canonical_json_bytes,
    load_artifact,
    load_receipt,
    strict_json_loads,
    validate_artifact,
    validate_receipt,
)
from scripts import sync_narcoscope


def _current():
    return load_artifact()[0]


def _receipt_instant(receipt: dict) -> datetime:
    return datetime.fromisoformat(
        receipt["current"]["admitted_at"].replace("Z", "+00:00")
    )


def _final_candidate_from_local_bytes():
    """Build the final producer shape without admitting or rewriting local bytes."""

    document = strict_json_loads(DEFAULT_ARTIFACT_PATH.read_bytes())
    precursors = document["datasets"]["precursorCorridorIncidents"]
    precursors["provenance"]["input"]["sha256"] = (
        "00799d3f4c7756a7790ccea8e1d0b8c4ac5fa97f7b7df26fe972d074864bc4dc"
    )
    aggregation = precursors["data"]["quantityAggregation"]
    aggregation.update({
        "eligibleRecordCount": 0,
        "excludedRecordCount": 1,
        "aggregationGroup": None,
    })
    corridor = precursors["data"]["corridors"][0]
    corridor.update({
        "seizureLocation": None,
        "quantityRelation": "less_than",
        "quantityBasis": (
            "Combined substance mass across nine PICS incidents; the source reports "
            "nearly 5 tons, retained as a less-than 5,000 kg bound."
        ),
        "aggregationEligibility": "ineligible_non_exact",
        "aggregationGroup": "meth_pre_precursor_substance_mass",
    })
    return document


def test_current_pin_is_fresh_hash_bound_and_preserves_supersession() -> None:
    document, raw = load_artifact()
    receipt = load_receipt(artifact=raw)

    assert document["dataAsOf"] == "2026-08-14"
    assert artifact_sha256(raw) == (
        "211bd6f1cafbbdff64c5ad8562484b5b8d65f2cf8fc22b9838dad7f16142ba62"
    )
    assert receipt["current"]["sha256"] == artifact_sha256(raw)
    assert receipt["current"]["data_as_of"] == document["dataAsOf"]
    assert receipt["superseded"][-1] == {
        "admitted_at": "2026-08-12T15:27:25Z",
        "data_as_of": "2026-08-12",
        "sha256": "2e8be3a3657fd339d78836cb1cef7a2e6a057e28a3238122072d0051a982dbbd",
        "superseded_at": receipt["current"]["admitted_at"],
    }


def test_schema_is_the_exact_final_producer_contract() -> None:
    schema_path = DEFAULT_ARTIFACT_PATH.with_name(
        "narcoscope-palimpsest-v1.schema.json"
    )
    assert hashlib.sha256(schema_path.read_bytes()).hexdigest() == (
        "e24b6426e3c253da8acee4e4c2c43ad88391b45e3db1edbe402fde9702c69fc8"
    )


def test_topic_payloads_reconcile_and_qualified_quantities_remain_unsummed() -> None:
    document = _current()
    precursors = document["datasets"]["precursorCorridorIncidents"]["data"]
    china_only = [
        row for row in precursors["corridors"]
        if row["originAttribution"] == "china_only"
    ]
    assert sum(row["quantityKg"] for row in china_only) == 5000
    assert len(precursors["corridors"]) == 1
    assert precursors["corridors"][0]["quantityRelation"] == "less_than"
    assert precursors["corridors"][0]["aggregationEligibility"] == (
        "ineligible_non_exact"
    )
    assert precursors["corridors"][0]["aggregationGroup"] == (
        "meth_pre_precursor_substance_mass"
    )
    assert precursors["corridors"][0]["seizureLocation"] is None
    assert precursors["corridors"][0]["incidentCount"] == 9
    assert precursors["quantityAggregation"] == {
        "status": "not_computed_non_exact_inputs",
        "exactRecordCount": 0,
        "nonExactRecordCount": 1,
        "eligibleRecordCount": 0,
        "excludedRecordCount": 1,
        "aggregationGroup": None,
        "summedQuantityKg": None,
    }
    assert len(precursors["contextRecords"]) == 1
    assert "quantityKg" not in precursors["contextRecords"][0]
    validate_artifact(document)


def test_changing_quantity_qualification_requires_a_new_explicit_pin() -> None:
    document = _current()
    corridor = document["datasets"]["precursorCorridorIncidents"]["data"]["corridors"][0]
    corridor["quantityRelation"] = "greater_than"

    # A bare shape checker would accept this.  The runtime intentionally cannot
    # know the source paragraph, so it pins the reviewed attribution tuples and
    # the exact source bytes; a relabel changes the hash and requires admission.
    validate_artifact(document)
    raw = canonical_json_bytes(document)
    current_receipt = load_receipt()
    changed = admission_receipt(
        document,
        raw,
        admitted_at=_receipt_instant(current_receipt) + timedelta(seconds=1),
        previous_receipt=current_receipt,
    )
    assert changed["current"]["sha256"] != current_receipt["current"]["sha256"]
    assert changed["superseded"][-1]["sha256"] == current_receipt["current"]["sha256"]


def test_arithmetic_tampering_and_unknown_payload_fields_fail_closed() -> None:
    wrong_total = _current()
    wrong_total["datasets"]["drugSeizures"]["data"]["quantityKg"] += 1
    with pytest.raises(NarcoScopeBridgeError, match="headline totals"):
        validate_artifact(wrong_total)

    wrong_incidents = _current()
    wrong_incidents["datasets"]["precursorCorridorIncidents"]["data"][
        "includedQuantitativeRecordCount"
    ] += 1
    with pytest.raises(NarcoScopeBridgeError, match="quantitative count"):
        validate_artifact(wrong_incidents)

    false_total = _current()
    false_total["datasets"]["precursorCorridorIncidents"]["data"][
        "quantityAggregation"
    ]["summedQuantityKg"] = 5000
    with pytest.raises(NarcoScopeBridgeError, match="may not enter"):
        validate_artifact(false_total)

    summable_context = _current()
    summable_context["datasets"]["precursorCorridorIncidents"]["data"][
        "contextRecords"
    ][0]["quantityKg"] = 168
    with pytest.raises(NarcoScopeBridgeError, match="fields differ"):
        validate_artifact(summable_context)

    unknown = _current()
    unknown["datasets"]["ofacDesignations"]["data"]["riskScore"] = 99
    with pytest.raises(NarcoScopeBridgeError, match="fields differ"):
        validate_artifact(unknown)


def test_final_producer_shape_is_accepted_before_pin_admission() -> None:
    validate_artifact(_final_candidate_from_local_bytes())


def test_non_exact_bound_cannot_be_relabelled_or_summed() -> None:
    eligible_bound = _final_candidate_from_local_bytes()
    corridor = eligible_bound["datasets"]["precursorCorridorIncidents"]["data"][
        "corridors"
    ][0]
    corridor["aggregationEligibility"] = "eligible"
    with pytest.raises(NarcoScopeBridgeError, match="non-exact.*ineligible"):
        validate_artifact(eligible_bound)

    summed_bound = _final_candidate_from_local_bytes()
    aggregation = summed_bound["datasets"]["precursorCorridorIncidents"]["data"][
        "quantityAggregation"
    ]
    aggregation.update({
        "status": "computed_exact_only",
        "eligibleRecordCount": 1,
        "excludedRecordCount": 0,
        "aggregationGroup": "meth_pre_precursor_substance_mass",
        "summedQuantityKg": 5000,
    })
    with pytest.raises(NarcoScopeBridgeError, match="counts|status|may not enter"):
        validate_artifact(summed_bound)


def test_only_exact_eligible_rows_in_one_group_can_be_summed() -> None:
    document = _final_candidate_from_local_bytes()
    data = document["datasets"]["precursorCorridorIncidents"]["data"]
    corridor = data["corridors"][0]
    corridor.update({
        "quantityRelation": "exact",
        "aggregationEligibility": "eligible",
    })
    data["quantityAggregation"] = {
        "status": "computed_exact_only",
        "exactRecordCount": 1,
        "nonExactRecordCount": 0,
        "eligibleRecordCount": 1,
        "excludedRecordCount": 0,
        "aggregationGroup": "meth_pre_precursor_substance_mass",
        "summedQuantityKg": 5000,
    }
    validate_artifact(document)

    wrong_group = copy.deepcopy(document)
    wrong_group["datasets"]["precursorCorridorIncidents"]["data"][
        "quantityAggregation"
    ]["aggregationGroup"] = "mdma_precursor_substance_mass"
    with pytest.raises(NarcoScopeBridgeError, match="group does not match"):
        validate_artifact(wrong_group)


def test_mixed_aggregation_groups_and_derived_rows_never_produce_a_total() -> None:
    mixed = _final_candidate_from_local_bytes()
    data = mixed["datasets"]["precursorCorridorIncidents"]["data"]
    first = data["corridors"][0]
    first.update({
        "quantityRelation": "exact",
        "aggregationEligibility": "eligible",
    })
    second = copy.deepcopy(first)
    second["precursor"] = "mdma_precursors"
    second["aggregationGroup"] = "mdma_precursor_substance_mass"
    second["sourceLocator"]["paragraph"] += 1
    data["corridors"].append(second)
    data["includedQuantitativeRecordCount"] = 2
    data["quantityAggregation"] = {
        "status": "not_computed_mixed_aggregation_groups",
        "exactRecordCount": 2,
        "nonExactRecordCount": 0,
        "eligibleRecordCount": 2,
        "excludedRecordCount": 0,
        "aggregationGroup": None,
        "summedQuantityKg": None,
    }
    validate_artifact(mixed)

    mixed["datasets"]["precursorCorridorIncidents"]["data"][
        "quantityAggregation"
    ].update({
        "status": "computed_exact_only",
        "aggregationGroup": "meth_pre_precursor_substance_mass",
        "summedQuantityKg": 10_000,
    })
    with pytest.raises(NarcoScopeBridgeError, match="status"):
        validate_artifact(mixed)

    derived = _final_candidate_from_local_bytes()
    derived_row = derived["datasets"]["precursorCorridorIncidents"]["data"][
        "corridors"
    ][0]
    derived_row.update({
        "quantityRelation": "exact",
        "recordKind": "derived_subtotal",
        "aggregationEligibility": "eligible",
    })
    with pytest.raises(NarcoScopeBridgeError, match="derived.*ineligible"):
        validate_artifact(derived)


@pytest.mark.parametrize("field", ("name", "alias", "address", "wallet", "message"))
def test_subject_and_indicator_fields_are_not_admissible(field: str) -> None:
    document = _current()
    document["exclusions"][0][field] = "must-never-cross"
    with pytest.raises(NarcoScopeBridgeError):
        validate_artifact(document)


def test_source_urls_and_input_paths_are_closed_not_configurable() -> None:
    hostile = _current()
    hostile["datasets"]["retailDrugPrices"]["provenance"]["url"] = (
        "https://127.0.0.1/private"
    )
    with pytest.raises(NarcoScopeBridgeError, match="allowlist"):
        validate_artifact(hostile)

    path_swap = _current()
    path_swap["datasets"]["ofacDesignations"]["provenance"]["input"]["path"] = (
        "src/data/people.json"
    )
    with pytest.raises(NarcoScopeBridgeError, match="input path changed"):
        validate_artifact(path_swap)


def test_duplicate_keys_nonfinite_and_oversize_inputs_are_rejected() -> None:
    with pytest.raises(NarcoScopeBridgeError, match="duplicate JSON key"):
        strict_json_loads(b'{"schema":1,"schema":2}')
    with pytest.raises(NarcoScopeBridgeError, match="non-finite"):
        strict_json_loads(b'{"value":NaN}')
    with pytest.raises(NarcoScopeBridgeError, match="exceeds"):
        strict_json_loads(b" " * (2 * 1024 * 1024 + 1))


def test_receipt_rejects_byte_mismatch_regression_and_duplicate_history() -> None:
    document, raw = load_artifact()
    receipt = load_receipt(artifact=raw)
    mismatch = copy.deepcopy(receipt)
    mismatch["current"]["sha256"] = "0" * 64
    with pytest.raises(NarcoScopeBridgeError, match="does not match"):
        validate_receipt(mismatch, artifact=raw)

    regressed = copy.deepcopy(document)
    regressed["dataAsOf"] = "2026-08-11"
    regressed["datasets"]["retailDrugPrices"]["provenance"]["localDataDate"] = (
        "2026-08-11"
    )
    regressed["datasets"]["precursorCorridorIncidents"]["provenance"][
        "localDataDate"
    ] = "2026-08-11"
    regressed["datasets"]["ofacDesignations"]["provenance"]["localDataDate"] = (
        "2026-08-11"
    )
    regressed["datasets"]["ofacDesignations"]["temporalCoverage"]["snapshotDate"] = (
        "2026-08-11"
    )
    regressed_raw = canonical_json_bytes(regressed)
    with pytest.raises(
        NarcoScopeBridgeError, match="candidate dataAsOf regresses"
    ):
        admission_receipt(
            regressed,
            regressed_raw,
            admitted_at=_receipt_instant(receipt) + timedelta(seconds=1),
            previous_receipt=receipt,
        )

    duplicate = copy.deepcopy(receipt)
    duplicate["superseded"].append({
        "admitted_at": "2026-08-11T00:00:00Z",
        "data_as_of": "2026-08-11",
        "sha256": receipt["current"]["sha256"],
        "superseded_at": "2026-08-12T13:15:56Z",
    })
    with pytest.raises(NarcoScopeBridgeError, match="duplicate"):
        validate_receipt(duplicate)


def test_idempotent_admission_preserves_clock_and_clock_regression_fails() -> None:
    document, raw = load_artifact()
    receipt = load_receipt(artifact=raw)

    same = admission_receipt(
        document,
        raw,
        admitted_at=_receipt_instant(receipt) + timedelta(days=1),
        previous_receipt=receipt,
    )
    assert same == receipt

    with pytest.raises(NarcoScopeBridgeError, match="admission clock regresses"):
        admission_receipt(
            document,
            raw,
            admitted_at=_receipt_instant(receipt) - timedelta(seconds=1),
            previous_receipt=receipt,
        )


def test_offline_cli_rejects_symlink_and_oversize_candidates(tmp_path: Path) -> None:
    target = tmp_path / "candidate.json"
    target.write_bytes(DEFAULT_ARTIFACT_PATH.read_bytes())
    symlink = tmp_path / "candidate-link.json"
    symlink.symlink_to(target)

    with pytest.raises(NarcoScopeBridgeError, match="safely open"):
        sync_narcoscope._read_bounded_candidate(symlink)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(NarcoScopeBridgeError, match="exceeds"):
        sync_narcoscope._read_bounded_candidate(oversized)


def test_offline_cli_check_and_monotonic_candidate_update(tmp_path: Path) -> None:
    artifact_path = tmp_path / "artifact.json"
    receipt_path = tmp_path / "receipt.json"
    artifact_path.write_bytes(DEFAULT_ARTIFACT_PATH.read_bytes())
    receipt_path.write_bytes(DEFAULT_RECEIPT_PATH.read_bytes())

    assert sync_narcoscope.main([
        "--artifact", str(artifact_path), "--receipt", str(receipt_path), "--check"
    ]) == 0

    candidate = _current()
    next_data_day = date.fromisoformat(candidate["dataAsOf"]) + timedelta(days=1)
    next_data_as_of = next_data_day.isoformat()
    candidate["dataAsOf"] = next_data_as_of
    candidate["datasets"]["retailDrugPrices"]["provenance"]["localDataDate"] = (
        next_data_as_of
    )
    current_receipt = load_receipt(receipt_path, artifact=artifact_path.read_bytes())
    next_admission = _receipt_instant(current_receipt) + timedelta(days=1)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_bytes(canonical_json_bytes(candidate))
    assert sync_narcoscope.main([
        "--artifact", str(artifact_path),
        "--receipt", str(receipt_path),
        "--source-file", str(candidate_path),
        "--retrieved-at", next_admission.isoformat(),
    ]) == 0
    updated_raw = artifact_path.read_bytes()
    updated = strict_json_loads(updated_raw)
    updated_receipt = load_receipt(receipt_path, artifact=updated_raw)
    assert updated["dataAsOf"] == next_data_as_of
    assert updated_receipt["current"]["sha256"] == hashlib.sha256(updated_raw).hexdigest()
    assert updated_receipt["superseded"][-1]["sha256"] == (
        current_receipt["current"]["sha256"]
    )


def test_remote_check_requires_exact_producer_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path = tmp_path / "artifact.json"
    receipt_path = tmp_path / "receipt.json"
    current_bytes = DEFAULT_ARTIFACT_PATH.read_bytes()
    artifact_path.write_bytes(current_bytes)
    receipt_path.write_bytes(DEFAULT_RECEIPT_PATH.read_bytes())

    monkeypatch.setattr(
        sync_narcoscope, "safe_fetch_bytes", lambda *_args, **_kwargs: current_bytes
    )
    assert sync_narcoscope.main([
        "--artifact", str(artifact_path),
        "--receipt", str(receipt_path),
        "--remote-check",
    ]) == 0

    # Whitespace-only producer drift still changes the admitted byte identity.
    monkeypatch.setattr(
        sync_narcoscope,
        "safe_fetch_bytes",
        lambda *_args, **_kwargs: current_bytes + b"\n",
    )
    assert sync_narcoscope.main([
        "--artifact", str(artifact_path),
        "--receipt", str(receipt_path),
        "--remote-check",
    ]) == 2


def test_cli_can_supersede_byte_bound_obsolete_shape_without_laundering_it(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "artifact.json"
    receipt_path = tmp_path / "receipt.json"
    candidate_path = tmp_path / "candidate.json"
    obsolete = b'{"dataAsOf":"2026-08-12","obsoleteShape":true}\n'
    obsolete_sha = hashlib.sha256(obsolete).hexdigest()
    artifact_path.write_bytes(obsolete)
    receipt_path.write_bytes(canonical_json_bytes({
        "schema": "palimpsest-partner-pin/v1",
        "producer": "narcoscope",
        "source_url": (
            "https://drug-price-observatory.vercel.app/data/"
            "narcoscope-palimpsest-v1.json"
        ),
        "artifact_id": "narcoscope.china.official-coverage",
        "current": {
            "data_as_of": "2026-08-12",
            "sha256": obsolete_sha,
            "admitted_at": "2026-08-12T13:00:00Z",
        },
        "superseded": [],
    }))
    candidate_path.write_bytes(DEFAULT_ARTIFACT_PATH.read_bytes())

    assert sync_narcoscope.main([
        "--artifact", str(artifact_path),
        "--receipt", str(receipt_path),
        "--source-file", str(candidate_path),
        "--retrieved-at", "2026-08-12T14:00:00Z",
    ]) == 0
    updated_raw = artifact_path.read_bytes()
    updated_receipt = load_receipt(receipt_path, artifact=updated_raw)
    assert updated_raw == candidate_path.read_bytes()
    assert updated_receipt["superseded"][-1]["sha256"] == obsolete_sha

    tampered = bytearray(obsolete)
    tampered[-2] = ord(" ")
    artifact_path.write_bytes(bytes(tampered))
    receipt_path.write_bytes(canonical_json_bytes({
        **updated_receipt,
        "current": {
            "data_as_of": "2026-08-12",
            "sha256": obsolete_sha,
            "admitted_at": "2026-08-12T13:00:00Z",
        },
        "superseded": [],
    }))
    assert sync_narcoscope.main([
        "--artifact", str(artifact_path),
        "--receipt", str(receipt_path),
        "--source-file", str(candidate_path),
        "--retrieved-at", "2026-08-12T14:00:00Z",
    ]) == 2
