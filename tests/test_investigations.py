"""Offline safety and determinism contracts for Palimpsest Research Leads."""
from __future__ import annotations

import copy
import json
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.investigations import (
    DEFAULT_CONFIG_PATH,
    InvestigationError,
    build_investigations,
    canonical_json_bytes,
    validate_investigations,
)
import scripts.build_investigations as cli
import scripts.build_osint_china as osint_china


ROOT = Path(__file__).resolve().parents[1]
READINGS = ROOT / "readings"


def _build(**kwargs):
    return build_investigations(
        readings_dir=READINGS,
        config_path=DEFAULT_CONFIG_PATH,
        **kwargs,
    )


def _case(document, slug):
    return next(row for row in document["cases"] if row["slug"] == slug)


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _copy_inputs(destination: Path) -> None:
    config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    for spec in config["artifacts"]:
        shutil.copy2(READINGS / spec["filename"], destination / spec["filename"])
    osint = json.loads((READINGS / "osint-china-latest.json").read_text(encoding="utf-8"))
    for signal in osint["signals"]:
        source = READINGS / signal["input"]["filename"]
        if source.exists():
            shutil.copy2(source, destination / source.name)


def _mutated_config(tmp_path: Path, mutate):
    config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(config)
    path = tmp_path / "investigations.json"
    _write_json(path, config)
    return path


def _artifact_clocks(directory: Path) -> list[datetime]:
    clocks: list[datetime] = []
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        raw = payload.get("generated_at")
        if not raw:
            continue
        try:
            clocks.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            continue
    return clocks


def _isoformat_z(clock: datetime) -> str:
    aligned = clock.astimezone(timezone.utc).replace(microsecond=0)
    return aligned.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rebuild_osint_after_source_mutation(tmp_path: Path, filename: str, mutate):
    _copy_inputs(tmp_path)
    source_path = tmp_path / filename
    source = json.loads(source_path.read_text(encoding="utf-8"))
    mutate(source)

    previous = json.loads(
        (tmp_path / "osint-china-latest.json").read_text(encoding="utf-8")
    )
    source_clock = datetime.fromisoformat(
        str(source.get("generated_at") or previous["generated_at"]).replace("Z", "+00:00")
    )
    # Collectors on main can be newer than the mutated source. Investigations
    # refuse future-dated artifacts, so the mutated row is restamped to the
    # newest copied clock and the decision sits five minutes later. The test
    # is about a null metric, not about that source being old.
    aligned = max([source_clock, *_artifact_clocks(tmp_path)])
    source["generated_at"] = _isoformat_z(aligned)
    _write_json(source_path, source)
    decision_clock = aligned + timedelta(minutes=5)
    refreshed = osint_china.build_document(
        tmp_path,
        now=decision_clock,
        input_commit=previous["input_commit"],
    )
    osint_china.write_atomic(refreshed, tmp_path / "osint-china-latest.json")
    desk = build_investigations(
        readings_dir=tmp_path,
        config_path=DEFAULT_CONFIG_PATH,
        as_of=decision_clock,
    )
    return refreshed, desk


def test_real_desk_has_two_bounded_research_leads_and_network_is_first():
    document = _build()

    assert document["schema_version"] == "palimpsest-investigations.v1"
    assert document["desk_id"] == "palimpsest-investigations"
    assert document["n_cases"] == len(document["cases"]) == 2
    assert document["cases"][0]["slug"] == "china-network-filtering-no-single-rate"
    assert {row["status"] for row in document["cases"]} <= {
        "evidence_gathering", "abstained"
    }
    for case in document["cases"]:
        if any(evidence["freshness"] == "stale" for evidence in case["evidence"]):
            assert case["status"] == "abstained"
    assert all(row["published_at"] is None for row in document["cases"])
    assert all(not row["publication_gate"]["publishable"] for row in document["cases"])
    assert all(row["version_id"].startswith("investigationv-") for row in document["cases"])


def test_network_lead_preserves_denominators_independence_and_counterevidence():
    case = _case(_build(), "china-network-filtering-no-single-rate")
    evidence = {row["evidence_id"]: row for row in case["evidence"]}

    assert evidence["ooni-anomaly-rate"]["independence_group"] == evidence["in-path-rate"]["independence_group"]
    assert evidence["censored-planet-rate"]["independence_group"] != evidence["ooni-anomaly-rate"]["independence_group"]
    assert evidence["inside-view-panel"]["independence_group"] not in {
        evidence["ooni-anomaly-rate"]["independence_group"],
        evidence["censored-planet-rate"]["independence_group"],
    }
    assert evidence["vantage-fusion-rate"]["source_class"] == "derived"
    finding = next(row for row in case["claims"] if row["claim_id"] == "current-round-diverges")
    assert finding["confidence"] == "corroborated"
    assert finding["publication_state"] == "draft"
    assert case["counterevidence"][0]["evidence_ids"] == ["ioda-instruments-firing"]
    assert {row["status"] for row in case["falsification_conditions"]} == {"untested"}


def test_economy_lead_abstains_from_true_gdp_causality_and_missing_as_zero():
    case = _case(_build(), "china-economy-evidence-gap")
    evidence = {row["evidence_id"]: row for row in case["evidence"]}

    assert evidence["economic-state-status"]["value"] == "warming_up"
    assert evidence["economic-state-direction"]["value"] is None
    months = evidence["baseline-months-observed"]["value"]
    assert isinstance(months, int) and months >= 0
    assert months < 8
    assert evidence["substantive-desks-observed"]["value"] < 5
    assert {row["source_id"] for row in case["collection_targets"]} >= {
        "nbs-70-city-housing", "nbs-labor-force", "mot-transport", "spb-parcels"
    }
    prohibited = " ".join(case["safety"]["prohibited_interpretations"]).lower()
    assert "true gdp" in prohibited
    assert "not collected" in prohibited
    assert "intent" in prohibited and "motive" in prohibited
    assert case["safety"]["person_level_data"] is False
    assert case["safety"]["allegations"] == case["safety"]["inferred_motives"] == []


def test_every_evidence_reference_is_hashed_timestamped_allowlisted_and_scalar():
    document = _build()
    receipts = {row["artifact_id"]: row for row in document["input_integrity"]}

    for case in document["cases"]:
        for row in case["evidence"]:
            receipt = receipts[row["artifact_id"]]
            assert row["artifact_sha256"] == receipt["sha256"]
            assert row["artifact_generated_at"] == receipt["generated_at"]
            assert row["artifact_url"].startswith("https://palimpsest.info/readings/")
            assert row["source_url"].startswith("https://palimpsest.info/readings/")
            assert row["integrity"] == "verified"
            assert row["freshness"] in {"current", "stale"}
            if receipt["freshness"] == "stale":
                assert row["freshness"] == "stale"
            assert row["value_type"] in {"text", "integer", "number", "boolean", "null"}
            assert not isinstance(row["value"], (dict, list))


def test_fixed_inputs_are_byte_deterministic_and_version_ids_are_content_addressed():
    first = _build()
    second = _build()

    assert first == second
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert len(canonical_json_bytes(first)) < 2 * 1024 * 1024
    assert [row["version_id"] for row in first["cases"]] == [
        row["version_id"] for row in second["cases"]
    ]


def test_runtime_and_published_schema_cover_exact_fields():
    document = _build()
    schema = json.loads((ROOT / "protocol" / "investigations-v1.schema.json").read_text(encoding="utf-8"))

    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(document)
    assert schema["properties"]["schema_version"]["const"] == document["schema_version"]
    assert schema["$defs"]["case"]["additionalProperties"] is False
    validate_investigations(document, readings_dir=READINGS)
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(schema).validate(document)


def test_unknown_fields_nonfinite_values_and_duplicate_ids_fail_closed():
    document = _build()

    unknown = copy.deepcopy(document)
    unknown["truth_score"] = 1
    with pytest.raises(InvestigationError):
        validate_investigations(unknown)

    nonfinite = copy.deepcopy(document)
    nonfinite["input_integrity"][0]["age_hours"] = math.nan
    with pytest.raises(InvestigationError):
        validate_investigations(nonfinite)
    with pytest.raises(InvestigationError):
        canonical_json_bytes({"value": math.inf})

    duplicate = copy.deepcopy(document)
    duplicate["cases"][0]["claims"][1]["claim_id"] = duplicate["cases"][0]["claims"][0]["claim_id"]
    with pytest.raises(InvestigationError, match="duplicate claim_id"):
        validate_investigations(duplicate)

    duplicate_counter = copy.deepcopy(document)
    duplicate_counter["cases"][0]["counterevidence"].append(copy.deepcopy(duplicate_counter["cases"][0]["counterevidence"][0]))
    with pytest.raises(InvestigationError, match="duplicate counterevidence_id"):
        validate_investigations(duplicate_counter)


def test_missing_or_tampered_parent_artifact_fails_closed(tmp_path):
    _copy_inputs(tmp_path)
    document = build_investigations(readings_dir=tmp_path, config_path=DEFAULT_CONFIG_PATH)

    (tmp_path / "newswire-latest.json").unlink()
    with pytest.raises(InvestigationError, match="missing evidence artifact"):
        build_investigations(readings_dir=tmp_path, config_path=DEFAULT_CONFIG_PATH)
    with pytest.raises(InvestigationError, match="disappeared"):
        validate_investigations(document, readings_dir=tmp_path)


def test_osint_child_receipt_bytes_are_verified_not_only_parent_hash(tmp_path):
    _copy_inputs(tmp_path)
    osint = json.loads((tmp_path / "osint-china-latest.json").read_text(encoding="utf-8"))
    signal = next(row for row in osint["signals"] if row["id"] == "ooni-gfw")
    child = tmp_path / signal["input"]["filename"]
    child.write_bytes(child.read_bytes() + b" ")

    with pytest.raises(InvestigationError, match="input receipt does not match bytes"):
        build_investigations(readings_dir=tmp_path, config_path=DEFAULT_CONFIG_PATH)


def test_degraded_selected_signal_forces_abstention_without_relabeling_it_current(tmp_path):
    _copy_inputs(tmp_path)
    osint_path = tmp_path / "osint-china-latest.json"
    osint = json.loads(osint_path.read_text(encoding="utf-8"))
    signal = next(row for row in osint["signals"] if row["id"] == "ooni-gfw")
    signal["status"] = "degraded"
    signal["live"] = False
    signal["health"]["ok"] = False
    _write_json(osint_path, osint)

    document = build_investigations(readings_dir=tmp_path, config_path=DEFAULT_CONFIG_PATH)
    network = _case(document, "china-network-filtering-no-single-rate")
    row = next(item for item in network["evidence"] if item["evidence_id"] == "ooni-anomaly-rate")
    assert row["freshness"] == "stale"
    assert network["status"] == "abstained"
    assert not network["publication_gate"]["publishable"]


def test_null_ooni_denominator_is_retained_as_stale_abstention(tmp_path):
    osint, document = _rebuild_osint_after_source_mutation(
        tmp_path,
        "ooni-gfw-latest.json",
        lambda payload: payload.update({"n_completed_measurements": None}),
    )

    signal = next(row for row in osint["signals"] if row["id"] == "ooni-gfw")
    network = _case(document, "china-network-filtering-no-single-rate")
    evidence = {row["evidence_id"]: row for row in network["evidence"]}

    assert signal["status"] == "degraded" and signal["live"] is False
    assert evidence["ooni-anomaly-rate"]["value"] == signal["payload"]["gfw_index"]
    assert evidence["ooni-measurement-count"]["value"] is None
    assert evidence["ooni-measurement-count"]["value_type"] == "null"
    assert evidence["ooni-measurement-count"]["freshness"] == "stale"
    assert network["status"] == "abstained"


def test_null_in_path_primary_metric_is_retained_as_stale_abstention(tmp_path):
    osint, document = _rebuild_osint_after_source_mutation(
        tmp_path,
        "in-path-interference-latest.json",
        lambda payload: payload.update({"middlebox_index": None}),
    )

    signal = next(
        row for row in osint["signals"] if row["id"] == "in-path-interference"
    )
    network = _case(document, "china-network-filtering-no-single-rate")
    evidence = {row["evidence_id"]: row for row in network["evidence"]}

    assert signal["metric"] is None
    assert signal["status"] == "degraded" and signal["live"] is False
    assert evidence["in-path-rate"]["value"] is None
    assert evidence["in-path-rate"]["value_type"] == "null"
    assert evidence["in-path-rate"]["freshness"] == "stale"
    assert network["status"] == "abstained"


@pytest.mark.parametrize("malformed", [None, [], "not-an-object", {}])
def test_malformed_osint_payload_intermediates_still_fail_closed(tmp_path, malformed):
    _copy_inputs(tmp_path)
    osint_path = tmp_path / "osint-china-latest.json"
    osint = json.loads(osint_path.read_text(encoding="utf-8"))
    signal = next(row for row in osint["signals"] if row["id"] == "ooni-gfw")
    signal["payload"] = malformed
    _write_json(osint_path, osint)

    with pytest.raises(InvestigationError, match="evidence selector"):
        build_investigations(
            readings_dir=tmp_path,
            config_path=DEFAULT_CONFIG_PATH,
        )


def test_closed_provenance_mapping_prevents_manufactured_corroboration(tmp_path):
    config_path = _mutated_config(
        tmp_path,
        lambda config: next(
            row for row in config["cases"][0]["evidence"] if row["evidence_id"] == "in-path-rate"
        ).update({"independence_group": "invented-independent-group"}),
    )

    with pytest.raises(InvestigationError, match="trusted source mapping"):
        build_investigations(readings_dir=READINGS, config_path=config_path)

    document = _build()
    row = next(
        item for item in document["cases"][0]["evidence"]
        if item["evidence_id"] == "in-path-rate"
    )
    row["independence_group"] = "invented-independent-group"
    with pytest.raises(InvestigationError, match="trusted source mapping"):
        validate_investigations(document, readings_dir=READINGS)


def test_public_validator_resolves_evidence_values_against_verified_artifact():
    document = _build()
    row = next(
        item for item in document["cases"][0]["evidence"]
        if item["evidence_id"] == "ooni-anomaly-rate"
    )
    row["value"] = row["value"] + 1.0

    with pytest.raises(InvestigationError, match="verified artifact selector"):
        validate_investigations(document, readings_dir=READINGS)


def test_stale_clock_forces_abstention_and_explicit_as_of_is_supported():
    current = _build()
    latest = datetime.fromisoformat(current["generated_at"].replace("Z", "+00:00"))
    future = latest + timedelta(hours=48)
    stale = _build(as_of=future)

    assert stale["generated_at"] == future.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert {row["status"] for row in stale["cases"]} == {"abstained"}
    assert all(row["freshness"] == "stale" for row in stale["input_integrity"])
    validate_investigations(stale, readings_dir=READINGS)


def test_policy_floor_cannot_be_downgraded_by_the_public_document_or_config(tmp_path):
    document = _build()
    document["publication_policy"]["minimum_independent_groups_per_analytical_claim"] = 0
    with pytest.raises(InvestigationError, match="immutable v1 safety floor"):
        validate_investigations(document)

    config_path = _mutated_config(
        tmp_path,
        lambda config: config["publication_policy"].update(
            {"minimum_assessed_falsification_conditions": 0}
        ),
    )
    with pytest.raises(InvestigationError, match="immutable v1 safety floor"):
        build_investigations(readings_dir=READINGS, config_path=config_path)


def test_counter_and_context_receipts_never_inflate_claim_corroboration(tmp_path):
    def mutate(config):
        for row in config["cases"][0]["evidence"]:
            if row["evidence_id"] in {"censored-planet-rate", "inside-view-panel"}:
                row["role"] = "context"

    config_path = _mutated_config(tmp_path, mutate)
    document = build_investigations(readings_dir=READINGS, config_path=config_path)
    network = _case(document, "china-network-filtering-no-single-rate")
    claim = next(row for row in network["claims"] if row["claim_id"] == "current-round-diverges")
    group_check = next(row for row in network["publication_gate"]["checks"] if row["check_id"] == "independent-groups")

    assert claim["confidence"] == "single_group"
    assert group_check["observed"] == 1
    assert not group_check["passed"]


def test_failed_or_inconclusive_falsification_does_not_open_gate(tmp_path):
    def mutate(config):
        case = config["cases"][0]
        for claim in case["claims"]:
            claim["publication_state"] = "reviewed"
        case["falsification_conditions"][1]["status"] = "failed"
        case["falsification_conditions"][2]["status"] = "inconclusive"

    config_path = _mutated_config(tmp_path, mutate)
    document = build_investigations(readings_dir=READINGS, config_path=config_path)
    network = _case(document, "china-network-filtering-no-single-rate")
    check = next(row for row in network["publication_gate"]["checks"] if row["check_id"] == "falsification-assessed")

    assert check["observed"] == 0
    assert not check["passed"]
    assert not network["publication_gate"]["publishable"]


def test_one_passed_condition_cannot_cover_an_unresolved_linked_condition(tmp_path):
    def mutate(config):
        case = config["cases"][0]
        for claim in case["claims"]:
            claim["publication_state"] = "reviewed"
        next(
            row for row in case["falsification_conditions"]
            if row["condition_id"] == "three-round-convergence"
        )["status"] = "passed"

    config_path = _mutated_config(tmp_path, mutate)
    document = build_investigations(readings_dir=READINGS, config_path=config_path)
    network = _case(document, "china-network-filtering-no-single-rate")
    checks = {row["check_id"]: row for row in network["publication_gate"]["checks"]}

    assert checks["falsification-per-claim"]["passed"]
    assert checks["falsification-assessed"]["observed"] == 1
    assert checks["falsification-assessed"]["minimum"] == 2
    assert not checks["falsification-assessed"]["passed"]
    assert not network["publication_gate"]["publishable"]


def test_editorial_clock_advances_updated_at_and_content_version(tmp_path):
    current = _build()
    evidence_clock = datetime.fromisoformat(
        current["generated_at"].replace("Z", "+00:00")
    )
    decision_clock = evidence_clock + timedelta(hours=1)
    editorial_clock = evidence_clock + timedelta(minutes=30)
    baseline = _build(as_of=decision_clock)
    before = _case(baseline, "china-network-filtering-no-single-rate")
    config_path = _mutated_config(
        tmp_path,
        lambda config: config["cases"][0].update(
            {
                "editorial_updated_at": editorial_clock.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            }
        ),
    )
    changed = build_investigations(
        readings_dir=READINGS,
        config_path=config_path,
        as_of=decision_clock,
    )
    after = _case(changed, "china-network-filtering-no-single-rate")

    assert after["updated_at"] > before["updated_at"]
    assert after["version_id"] != before["version_id"]


def test_runtime_enforces_nested_schema_parity_without_jsonschema():
    document = _build()
    document["cases"][0]["claims"][0]["publication_state"] = "approved_by_magic"
    with pytest.raises(InvestigationError, match="publication_state"):
        validate_investigations(document)

    document = _build()
    document["cases"][0]["methodology"][0]["description"] = ""
    with pytest.raises(InvestigationError, match="bounded text"):
        validate_investigations(document)

    document = _build()
    prohibited = document["cases"][0]["safety"]["prohibited_interpretations"]
    prohibited.append(prohibited[0])
    with pytest.raises(InvestigationError, match="bounded unique array"):
        validate_investigations(document)


@pytest.mark.parametrize("missing_field", ["counterevidence_ids", "limitation_ids"])
def test_advanced_analytical_claim_requires_counterevidence_and_limitations(missing_field):
    document = _build()
    case = document["cases"][0]
    case["status"] = "review_ready"
    analytical = next(row for row in case["claims"] if row["type"] == "analytical_finding")
    analytical[missing_field] = []

    with pytest.raises(InvestigationError, match="require counterevidence and limitations"):
        validate_investigations(document)


def test_counterevidence_must_bind_a_counter_role_receipt():
    document = _build()
    case = document["cases"][0]
    ioda = next(row for row in case["evidence"] if row["evidence_id"] == "ioda-instruments-firing")
    ioda["role"] = "support"

    with pytest.raises(InvestigationError, match="role=counter"):
        validate_investigations(document)


def test_assessed_falsification_cannot_be_orphaned_from_analytical_hypothesis():
    document = _build()
    case = document["cases"][0]
    orphan = next(
        row for row in case["falsification_conditions"]
        if row["condition_id"] == "country-scale-ioda-event"
    )
    orphan["status"] = "passed"

    with pytest.raises(InvestigationError, match="orphaned from analytical claims"):
        validate_investigations(document)


def _advance_network_case(config, status):
    case = config["cases"][0]
    case["status_intent"] = status
    case["published_at"] = "2026-08-11T14:00:00Z" if status == "published" else None
    for claim in case["claims"]:
        claim["publication_state"] = "reviewed"
    next(
        row for row in case["falsification_conditions"]
        if row["condition_id"] == "three-round-convergence"
    )["status"] = "passed"
    next(
        row for row in case["falsification_conditions"]
        if row["condition_id"] == "three-round-clean-panel"
    )["status"] = "passed"


def test_future_review_ready_and_published_paths_obey_gate_and_clock(tmp_path):
    review_config = _mutated_config(
        tmp_path, lambda config: _advance_network_case(config, "review_ready")
    )
    review_document = build_investigations(
        readings_dir=READINGS, config_path=review_config
    )
    review_case = _case(review_document, "china-network-filtering-no-single-rate")
    stale_evidence = any(row["freshness"] == "stale" for row in review_case["evidence"])
    if stale_evidence:
        assert review_case["status"] == "abstained"
        assert not review_case["publication_gate"]["publishable"]
    else:
        assert review_case["status"] == "review_ready"
        assert review_case["publication_gate"]["publishable"]

    publish_config = _mutated_config(
        tmp_path, lambda config: _advance_network_case(config, "published")
    )
    if stale_evidence:
        config = json.loads(publish_config.read_text(encoding="utf-8"))
        config["cases"][0]["published_at"] = None
        _write_json(publish_config, config)
    published_document = build_investigations(
        readings_dir=READINGS, config_path=publish_config
    )
    published_case = _case(
        published_document, "china-network-filtering-no-single-rate"
    )
    if stale_evidence:
        assert published_case["status"] == "abstained"
        return
    assert published_case["status"] == "published"
    assert published_case["opened_at"] <= published_case["published_at"] <= published_case["updated_at"]

    invalid_clock = _mutated_config(
        tmp_path,
        lambda config: (
            _advance_network_case(config, "published"),
            config["cases"][0].update({"published_at": "2026-08-10T23:59:59Z"}),
        ),
    )
    with pytest.raises(InvestigationError, match="published_at must fall between"):
        build_investigations(readings_dir=READINGS, config_path=invalid_clock)


def test_complete_reply_cannot_retain_pending_party():
    document = _build()
    rtr = document["cases"][0]["right_to_reply"]
    rtr["status"] = "complete"
    rtr["parties"] = [{
        "party_id": "measurement-institution",
        "party_type": "institution",
        "display_name": "Measurement institution",
        "disposition": "pending",
    }]

    with pytest.raises(InvestigationError, match="cannot contain pending"):
        validate_investigations(document)

    document = _build()
    rtr = document["cases"][0]["right_to_reply"]
    rtr["status"] = "pending"
    rtr["parties"] = [{
        "party_id": "measurement-institution",
        "party_type": "institution",
        "display_name": "Measurement institution",
        "disposition": "responded",
    }]
    with pytest.raises(InvestigationError, match="requires a pending party"):
        validate_investigations(document)


def test_public_urls_reject_queries_fragments_and_contact_paths():
    for suffix in (
        "?token=secret", "#contact", "/reporter@example.org", "/reporter%40example.org"
    ):
        document = _build()
        document["cases"][0]["collection_targets"][0]["evidence_url"] = (
            "https://palimpsest.info/readings/bleedthrough-latest.json" + suffix
        )
        with pytest.raises(InvestigationError, match="allowlist"):
            validate_investigations(document)


def test_correction_state_requires_consistent_clock():
    document = _build()
    correction = document["cases"][0]["correction"]
    correction["status"] = "corrected"
    with pytest.raises(InvestigationError, match="requires a correction clock"):
        validate_investigations(document)

    document = _build()
    correction = document["cases"][0]["correction"]
    correction["last_corrected_at"] = "2026-08-11T13:00:00Z"
    with pytest.raises(InvestigationError, match="cannot carry a correction clock"):
        validate_investigations(document)


def test_json_input_cap_applies_before_parse_and_osint_arrays_are_bounded(tmp_path):
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(InvestigationError, match="exceeds"):
        build_investigations(readings_dir=READINGS, config_path=oversized)

    _copy_inputs(tmp_path)
    osint_path = tmp_path / "osint-china-latest.json"
    osint = json.loads(osint_path.read_text(encoding="utf-8"))
    template = copy.deepcopy(osint["signals"][0])
    while len(osint["signals"]) <= 128:
        row = copy.deepcopy(template)
        row["id"] = f"bounded-signal-{len(osint['signals'])}"
        osint["signals"].append(row)
    osint["n_signals_total"] = len(osint["signals"])
    _write_json(osint_path, osint)
    with pytest.raises(InvestigationError, match="signals are outside"):
        build_investigations(readings_dir=tmp_path, config_path=DEFAULT_CONFIG_PATH)


def test_source_url_allowlist_and_person_level_text_fail_closed(tmp_path):
    evil_url = _mutated_config(
        tmp_path,
        lambda config: config["cases"][0]["collection_targets"][0].update(
            {"evidence_url": "https://example.com/unreviewed"}
        ),
    )
    with pytest.raises(InvestigationError, match="allowlist"):
        build_investigations(readings_dir=READINGS, config_path=evil_url)

    pii_path = _mutated_config(
        tmp_path,
        lambda config: config["cases"][0]["claims"][0].update(
            {"statement": "Contact reporter@example.org for a person-level record."}
        ),
    )
    with pytest.raises(InvestigationError, match="person-level contact"):
        build_investigations(readings_dir=READINGS, config_path=pii_path)


def test_gate_selectors_are_identity_keyed_not_position_keyed():
    config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    economy = config["cases"][1]
    readiness_selectors = [
        row["selector"] for row in economy["evidence"]
        if row["artifact_id"] == "economic-pulse" and "/readiness/gates/" in row["selector"]
    ]

    assert readiness_selectors
    assert all("/@gate_id=" in selector for selector in readiness_selectors)
    assert not any("/gates/0/" in selector or "/gates/1/" in selector for selector in readiness_selectors)


def test_cli_atomic_write_and_check_mode(tmp_path):
    output = tmp_path / "investigations-latest.json"

    assert cli.main(["--output", str(output)]) == 0
    assert output.read_bytes() == canonical_json_bytes(_build())
    assert cli.main(["--output", str(output), "--check"]) == 0
    output.write_text("{}\n", encoding="utf-8")
    assert cli.main(["--output", str(output), "--check"]) == 1
