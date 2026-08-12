"""Runtime conformance tests for Lab Evidence Envelope v1."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from core import lab_evidence
from core.lab_evidence import (
    LabEvidenceError,
    canonical_json_bytes,
    compute_record_sha256,
    compute_source_set_sha256,
    load_envelope,
    load_envelope_set,
    seal_envelope_hashes,
    strict_json_loads,
    validate_envelope,
    validate_envelope_set,
)


ZERO_HASH = "0" * 64


def _source(
    ref_id: str = "pboc-release",
    group_id: str = "pboc",
    *,
    uri: str = "https://www.pbc.gov.cn/releases/2026-08.html",
) -> dict:
    return {
        "id": ref_id,
        "group_id": group_id,
        "publisher": "People's Bank of China",
        "uri": uri,
        "retrieved_at": "2026-08-12T09:30:00Z",
        "evidence_class": "OFFICIAL_STATISTIC",
        "content_sha256": hashlib.sha256(ref_id.encode()).hexdigest(),
    }


def _candidate(*, record_id: str = "cn.cny.loan-growth.2026-07") -> dict:
    return {
        "schema": "lab-evidence-envelope/v1",
        "record_id": record_id,
        "signal_id": "cn.cny.loan-growth",
        "event_time": "2026-07-31T23:59:59Z",
        "knowledge_time": "2026-08-12T09:00:00+00:00",
        "publication_time": "2026-08-12T10:00:00Z",
        "jurisdiction": {
            "scheme": "ISO-3166-1-alpha-2",
            "code": "CN",
            "label": "China",
        },
        "dimensions": {
            "substance_ids": [],
            "typology_ids": ["monetary-plumbing"],
        },
        "measure": {
            "type": "year-on-year-change",
            "value": "8.7",
            "unit": "percent",
        },
        "evidence_status": "OBSERVED",
        "measured_fraction": "1",
        "support_level": "DIRECT_OBSERVATION",
        "source_groups": ["pboc"],
        "source_refs": [_source()],
        "hashes": {
            "algorithm": "sha256",
            "record_sha256": ZERO_HASH,
            "source_set_sha256": ZERO_HASH,
        },
        "redistribution_status": "OPEN",
        "public_value_allowed": True,
        "license": "Public statistical release; attribution requested",
        "privacy_tier": "PUBLIC_AGGREGATE",
        "review_status": "MACHINE_VALIDATED",
        "contains_exact_iocs": False,
        "contains_raw_messages": False,
        "limitations": [
            "The observation is a source-reported national aggregate and does not identify causation."
        ],
        "supersedes": [],
    }


def _valid(**changes) -> dict:
    candidate = _candidate()
    candidate.update(changes)
    return seal_envelope_hashes(candidate)


def _reseal(candidate: dict) -> dict:
    return seal_envelope_hashes(candidate)


def test_valid_observation_round_trips_without_aliasing() -> None:
    original = _valid()
    validated = validate_envelope(original)

    assert validated == original
    assert validated is not original
    validated["source_refs"][0]["publisher"] = "mutated"
    assert original["source_refs"][0]["publisher"] == "People's Bank of China"


def test_valid_derived_interval_and_scenario_records() -> None:
    derived = _candidate(record_id="cn.cny.loan-growth-derived.2026-07")
    derived.update({
        "measure": {
            "type": "model-range",
            "interval": {
                "lower": "7.9",
                "upper": "9.1",
                "kind": "CONFIDENCE",
                "level": "0.95",
            },
            "unit": "percent",
        },
        "evidence_status": "DERIVED",
        "measured_fraction": "0.75",
        "support_level": "DERIVED_ESTIMATE",
        "method": {
            "id": "robust-trend",
            "version": "2.1.0",
            "input_record_ids": ["cn.cny.loan-growth.2026-06"],
            "assumptions": ["The source series is comparable across both releases."],
        },
    })
    assert validate_envelope(_reseal(derived))["measure"]["interval"]["level"] == "0.95"

    scenario = _candidate(record_id="cn.cny.loan-growth-scenario.2026-08")
    scenario.update({
        "measure": {
            "type": "stress-range",
            "interval": {"lower": "5", "upper": "7", "kind": "SCENARIO"},
            "unit": "percent",
        },
        "evidence_status": "SCENARIO",
        "measured_fraction": "0",
        "support_level": "SCENARIO_ONLY",
        "method": {
            "id": "stress-case",
            "version": "1",
            "input_record_ids": [],
            "assumptions": ["Policy rates remain unchanged."],
        },
    })
    assert validate_envelope(_reseal(scenario))["evidence_status"] == "SCENARIO"


@pytest.mark.parametrize(
    "payload, message",
    [
        ('{"schema":"a","schema":"b"}', "duplicate JSON key"),
        ('{"value":NaN}', "non-finite JSON number"),
        ('{"value":Infinity}', "non-finite JSON number"),
        ('{"value":1e9999}', "non-finite"),
    ],
)
def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(
    payload: str, message: str
) -> None:
    with pytest.raises(LabEvidenceError, match=message):
        strict_json_loads(payload)


def test_strict_json_and_canonicalization_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(LabEvidenceError, match="input limit"):
        strict_json_loads('"' + "x" * 50 + '"', maximum_bytes=16)

    monkeypatch.setattr(lab_evidence, "MAX_ENVELOPE_BYTES", 16)
    with pytest.raises(LabEvidenceError, match="canonical value exceeds"):
        canonical_json_bytes({"value": "x" * 20})


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda row: row.__setitem__("unexpected", True), "unknown"),
        (lambda row: row.pop("signal_id"), "missing"),
        (lambda row: row["jurisdiction"].__setitem__("extra", 1), "unknown"),
        (lambda row: row["measure"].__setitem__("extra", 1), "unknown"),
        (lambda row: row["source_refs"][0].__setitem__("extra", 1), "unknown"),
        (lambda row: row["hashes"].__setitem__("extra", 1), "unknown"),
        (lambda row: row.__setitem__(1, "not-json"), "keys must be strings"),
    ],
)
def test_every_object_uses_an_exact_field_set(mutate, message: str) -> None:
    candidate = _candidate()
    mutate(candidate)
    with pytest.raises(LabEvidenceError, match=message):
        _reseal(candidate)


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.org:8443/source",
        "https://user@example.org/source",
        "https://example.org/source#fragment",
        "https://Example.org/source",
        "https://example.org/a/../source",
        "https://example.org/source%0Ahidden",
        "https://example.org/source%2fchild",
        "http://example.org/source",
        "https://example.org\\source",
    ],
)
def test_source_https_urls_fail_closed(uri: str) -> None:
    candidate = _candidate()
    candidate["source_refs"][0]["uri"] = uri
    with pytest.raises(LabEvidenceError):
        _reseal(candidate)


def test_https_default_port_and_inert_urn_are_supported() -> None:
    https = _candidate()
    https["source_refs"][0]["uri"] = "https://example.org:443/source?a=1"
    assert validate_envelope(_reseal(https))["source_refs"][0]["uri"].endswith("?a=1")

    urn = _candidate()
    urn["source_refs"][0]["uri"] = "urn:sha256:" + "a" * 64
    assert validate_envelope(_reseal(urn))["source_refs"][0]["uri"].startswith("urn:")


@pytest.mark.parametrize(
    "field, timestamp",
    [
        ("event_time", "2026-08-01T00:00:00+05:30"),
        ("knowledge_time", "2026-13-01T00:00:00Z"),
        ("publication_time", "2026-08-12 10:00:00Z"),
    ],
)
def test_timestamps_are_valid_explicit_utc(field: str, timestamp: str) -> None:
    candidate = _candidate()
    candidate[field] = timestamp
    with pytest.raises(LabEvidenceError, match="timestamp|calendar|UTC"):
        _reseal(candidate)


def test_timestamp_order_compares_fractional_instants_exactly() -> None:
    candidate = _candidate()
    candidate["event_time"] = "2026-08-12T09:00:00.0000000002Z"
    candidate["knowledge_time"] = "2026-08-12T09:00:00.0000000001Z"
    with pytest.raises(LabEvidenceError, match="event_time <= knowledge_time"):
        _reseal(candidate)


@pytest.mark.parametrize("value", ["01", "+1", "1e3", ".5", "NaN", "1."])
def test_measure_decimals_use_exact_bounded_strings(value: str) -> None:
    candidate = _candidate()
    candidate["measure"]["value"] = value
    with pytest.raises(LabEvidenceError, match="decimal"):
        _reseal(candidate)


def test_interval_order_and_level_method_rule_are_enforced() -> None:
    candidate = _candidate()
    candidate["measure"] = {
        "type": "range",
        "interval": {"lower": "2.000000000000000001", "upper": "2", "kind": "RANGE"},
        "unit": "count",
    }
    with pytest.raises(LabEvidenceError, match="lower exceeds upper"):
        _reseal(candidate)

    candidate["measure"]["interval"] = {
        "lower": "1", "upper": "2", "kind": "CONFIDENCE", "level": "0.9"
    }
    with pytest.raises(LabEvidenceError, match="requires a versioned method"):
        _reseal(candidate)


@pytest.mark.parametrize(
    "status, fraction, support, method, message",
    [
        ("OBSERVED", "1.0", "DIRECT_OBSERVATION", None, "measured_fraction '1'"),
        ("OBSERVED", "1", "DERIVED_ESTIMATE", None, "incompatible"),
        ("DERIVED", "0.5", "DERIVED_ESTIMATE", None, "require a method"),
        ("SCENARIO", "0.1", "SCENARIO_ONLY", {}, "measured_fraction '0'"),
        ("SCENARIO", "0", "NOT_ASSESSED", {}, "SCENARIO_ONLY"),
    ],
)
def test_evidence_status_fraction_and_support_are_cross_checked(
    status: str, fraction: str, support: str, method: dict | None, message: str
) -> None:
    candidate = _candidate()
    candidate.update({
        "evidence_status": status,
        "measured_fraction": fraction,
        "support_level": support,
    })
    if method is not None:
        candidate["method"] = {
            "id": "scenario-method",
            "version": "1",
            "input_record_ids": [],
            "assumptions": ["Assumption"],
        }
    with pytest.raises(LabEvidenceError, match=message):
        _reseal(candidate)


def test_scenario_requires_a_nonempty_assumption() -> None:
    candidate = _candidate()
    candidate.update({
        "evidence_status": "SCENARIO",
        "measured_fraction": "0",
        "support_level": "SCENARIO_ONLY",
        "method": {
            "id": "scenario-method",
            "version": "1",
            "input_record_ids": [],
            "assumptions": [],
        },
    })
    with pytest.raises(LabEvidenceError, match="at least one assumption"):
        _reseal(candidate)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (
            lambda row: row["source_refs"].append(copy.deepcopy(row["source_refs"][0])),
            "duplicate id",
        ),
        (
            lambda row: row["source_refs"][0].__setitem__("group_id", "undeclared"),
            "undeclared",
        ),
        (lambda row: row["source_groups"].append("unused"), "not backed"),
    ],
)
def test_source_group_and_reference_consistency(mutation, message: str) -> None:
    candidate = _candidate()
    mutation(candidate)
    with pytest.raises(LabEvidenceError, match=message):
        _reseal(candidate)


def test_corroboration_requires_two_declared_and_used_independence_groups() -> None:
    candidate = _candidate()
    candidate["support_level"] = "CORROBORATED_OBSERVATION"
    with pytest.raises(LabEvidenceError, match="two independent"):
        _reseal(candidate)

    candidate["source_groups"].append("nbs")
    candidate["source_refs"].append(
        _source("nbs-release", "nbs", uri="https://data.stats.gov.cn/release/2026-08")
    )
    assert validate_envelope(_reseal(candidate))["support_level"] == (
        "CORROBORATED_OBSERVATION"
    )


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("redistribution_status", "LINK_ONLY", "redistribution"),
        ("privacy_tier", "CONTROLLED_AGGREGATE", "PUBLIC_AGGREGATE"),
        ("review_status", "UNREVIEWED", "MACHINE_VALIDATED"),
        ("review_status", "REJECTED", "MACHINE_VALIDATED"),
    ],
)
def test_public_value_gate_fails_closed(field: str, value: str, message: str) -> None:
    candidate = _candidate()
    candidate[field] = value
    with pytest.raises(LabEvidenceError, match=message):
        _reseal(candidate)


def test_nonpublic_controlled_and_link_only_record_is_admissible() -> None:
    candidate = _candidate()
    candidate.update({
        "redistribution_status": "LINK_ONLY",
        "public_value_allowed": False,
        "privacy_tier": "CONTROLLED_AGGREGATE",
        "review_status": "HUMAN_REVIEW_REQUIRED",
    })
    assert validate_envelope(_reseal(candidate))["public_value_allowed"] is False


def test_human_review_and_aggregate_privacy_fields_are_literal() -> None:
    candidate = _candidate()
    candidate["review_status"] = "HUMAN_REVIEWED"
    with pytest.raises(LabEvidenceError, match="reviewed_at"):
        _reseal(candidate)

    candidate["reviewed_at"] = "2026-08-12T09:59:00Z"
    candidate["contains_exact_iocs"] = 0
    with pytest.raises(LabEvidenceError, match="literal false"):
        _reseal(candidate)


def test_source_set_hash_sorts_refs_but_record_hash_binds_original_order() -> None:
    first = _candidate()
    first["source_groups"] = ["pboc", "nbs"]
    first["source_refs"].append(
        _source("nbs-release", "nbs", uri="https://data.stats.gov.cn/release")
    )
    second = copy.deepcopy(first)
    second["source_refs"].reverse()

    assert compute_source_set_sha256(first["source_refs"]) == (
        compute_source_set_sha256(second["source_refs"])
    )
    sealed_first = _reseal(first)
    sealed_second = _reseal(second)
    assert sealed_first["hashes"]["record_sha256"] != (
        sealed_second["hashes"]["record_sha256"]
    )


def test_record_hash_removes_only_its_self_referential_field() -> None:
    envelope = _valid()
    projection = copy.deepcopy(envelope)
    del projection["hashes"]["record_sha256"]
    assert compute_record_sha256(envelope) == hashlib.sha256(
        canonical_json_bytes(projection)
    ).hexdigest()

    envelope["hashes"]["source_set_sha256"] = "f" * 64
    assert compute_record_sha256(envelope) != hashlib.sha256(
        canonical_json_bytes(projection)
    ).hexdigest()


def test_hash_tampering_is_rejected() -> None:
    envelope = _valid()
    envelope["measure"]["value"] = "8.8"
    with pytest.raises(LabEvidenceError, match="record_sha256"):
        validate_envelope(envelope)

    envelope = _valid()
    envelope["hashes"]["source_set_sha256"] = "f" * 64
    envelope["hashes"]["record_sha256"] = compute_record_sha256(envelope)
    with pytest.raises(LabEvidenceError, match="source_set_sha256"):
        validate_envelope(envelope)


def test_sealing_replaces_stale_or_missing_envelope_hashes() -> None:
    candidate = _candidate()
    candidate["hashes"]["record_sha256"] = "not-a-digest"
    del candidate["hashes"]["source_set_sha256"]

    sealed = seal_envelope_hashes(candidate)
    assert validate_envelope(sealed) == sealed


def _revision(record_id: str, supersedes: list[str]) -> dict:
    candidate = _candidate(record_id=record_id)
    candidate["supersedes"] = supersedes
    return _reseal(candidate)


def test_valid_supersession_chain_is_admitted() -> None:
    old = _revision("series.release.v1", [])
    middle = _revision("series.release.v2", ["series.release.v1"])
    latest = _revision("series.release.v3", ["series.release.v2"])

    validated = validate_envelope_set([latest, old, middle])
    assert [record["record_id"] for record in validated] == [
        "series.release.v3", "series.release.v1", "series.release.v2",
    ]


def test_supersession_set_rejects_missing_duplicate_self_and_cycles() -> None:
    with pytest.raises(LabEvidenceError, match="absent"):
        validate_envelope_set([_revision("series.release.v2", ["series.release.v1"])])

    duplicate = _revision("series.release.v1", [])
    with pytest.raises(LabEvidenceError, match="duplicate record_id"):
        validate_envelope_set([duplicate, duplicate])

    candidate = _candidate(record_id="series.release.v1")
    candidate["supersedes"] = ["series.release.v1"]
    with pytest.raises(LabEvidenceError, match="itself"):
        _reseal(candidate)

    first = _revision("series.release.v1", ["series.release.v2"])
    second = _revision("series.release.v2", ["series.release.v1"])
    with pytest.raises(LabEvidenceError, match="cycle"):
        validate_envelope_set([first, second])


def test_json_and_file_loaders_validate_hashes_and_set_graph(tmp_path: Path) -> None:
    old = _revision("series.release.v1", [])
    latest = _revision("series.release.v2", ["series.release.v1"])
    envelope_path = tmp_path / "envelope.json"
    set_path = tmp_path / "set.json"
    envelope_path.write_bytes(canonical_json_bytes(old))
    set_path.write_text(json.dumps([old, latest]), encoding="utf-8")

    assert load_envelope(envelope_path)["record_id"] == "series.release.v1"
    assert len(load_envelope_set(set_path)) == 2


def test_file_loader_checks_size_before_json_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b"{" + b"x" * 100)
    monkeypatch.setattr(lab_evidence, "MAX_ENVELOPE_BYTES", 16)
    with pytest.raises(LabEvidenceError, match="exceeds the 16-byte limit"):
        load_envelope(path)
