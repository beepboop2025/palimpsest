"""Contracts for the revision-safe China economic pulse.

All tests are offline.  The checked-in readings exercise the real adapters;
small temporary ledgers exercise point-in-time behavior and hostile inputs.
"""
from __future__ import annotations

import copy
import calendar
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from core.econ_observation import EconomicObservation, sha256_bytes
from core.economic_pulse import (
    DESK_IDS,
    EconomicPulseError,
    build_economic_pulse,
    canonical_json_bytes,
    validate_economic_pulse,
)
import scripts.build_economic_pulse as cli
import scripts.build_osint_china as osint


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "china_econ_sources.json"
UTC = timezone.utc


def _build(**kwargs):
    return build_economic_pulse(
        readings_dir=ROOT / "readings",
        registry_path=REGISTRY,
        **kwargs,
    )


def _records(pulse):
    return [
        metric for desk in pulse["desks"] for metric in desk["metrics"]
    ] + pulse["release_calendar"]["entries"]


def _desk(pulse, desk_id):
    return next(desk for desk in pulse["desks"] if desk["id"] == desk_id)


def test_real_pulse_is_structured_abstaining_and_evidence_rich():
    pulse = _build()

    assert pulse["schema_version"] == "palimpsest-economic-pulse.v1"
    assert [desk["id"] for desk in pulse["desks"]] == list(DESK_IDS)
    assert pulse["economic_state"] == {
        "status": "warming_up",
        "direction": None,
        "composite": None,
        "claim": pulse["economic_state"]["claim"],
        "prohibited_interpretations": pulse["economic_state"]["prohibited_interpretations"],
    }
    assert "true GDP" in " ".join(pulse["economic_state"]["prohibited_interpretations"])
    assert pulse["readiness"]["status"] == "warming_up"
    assert "property-labor-demand" in {
        desk["id"] for desk in pulse["desks"] if desk["status"] == "not_collected"
    }
    assert pulse["n_metrics"] == len(_records(pulse))
    assert pulse["n_metrics"] >= 40

    for metric in _records(pulse):
        assert metric["unit"]
        assert metric["period_start"] <= metric["period_end"]
        assert metric["source_id"] and metric["independence_group"]
        assert metric["source_class"] in {"official", "market", "physical", "news"}
        assert metric["freshness"]["status"] in {"current", "stale"}
        assert metric["comparability"]["concept_id"]
        assert metric["revision"]["status"] in {"original", "revised", "not_available"}
        assert metric["limitation"]
        assert metric["evidence"]["url"] or metric["evidence"]["sha256"]


def test_pulse_is_deterministic_for_fixed_inputs_and_derived_clock():
    first = _build()
    second = _build()
    assert first == second
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["generated_at"] == first["as_of"]


def test_published_schema_and_semantic_validator_cover_the_exact_top_level():
    schema = json.loads(
        (ROOT / "protocol" / "economic-pulse-v1.schema.json").read_text(encoding="utf-8")
    )
    pulse = _build()

    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["schema_version"]["const"] == pulse["schema_version"]
    assert set(schema["required"]) == set(pulse)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["economicState"]["properties"]["direction"] == {"type": "null"}
    validate_economic_pulse(pulse)


def test_every_source_family_keeps_independence_and_class_instead_of_vote_counting():
    pulse = _build()
    money = _desk(pulse, "money-credit-fx")
    cfets = [row for row in money["metrics"] if row["source_id"] == "cfets_benchmarks"]

    assert len(cfets) >= 15
    assert {row["independence_group"] for row in cfets} == {"cfets_benchmarks"}
    assert money["independent_group_ids"].count("cfets_benchmarks") == 1
    assert {"official", "market", "physical"} <= {
        row["source_class"] for row in _records(pulse)
    }


def test_coverage_publishes_registered_live_observed_and_adapter_ready_separately():
    coverage = _build()["coverage"]

    assert coverage["registered_sources"] >= 30
    assert coverage["registered_independent_groups"] == len(
        coverage["registered_independent_group_ids"]
    )
    assert coverage["live_source_ids"] == [
        "cfets_benchmarks", "external_cny_reference", "hkex_stock_connect"
    ]
    assert len(coverage["live_independent_group_ids"]) == 3
    assert len(coverage["observed_independent_group_ids"]) >= 8
    assert coverage["adapter_ready_sources"]
    assert "nbs_70_city_housing" in coverage["missing_source_ids"]
    property_row = next(row for row in coverage["matrix"] if row["domain"] == "property")
    assert property_row["observed_groups"] == []
    assert property_row["adapter_ready_groups"]


def test_release_calendar_keeps_unknown_intraday_release_clocks_null():
    calendar_rows = _build()["release_calendar"]["entries"]

    assert len(calendar_rows) == 7
    assert all(row["released_at"] is None for row in calendar_rows)
    assert all(row["latest_publication_date"] for row in calendar_rows)
    assert all(row["unit"] == "days" for row in calendar_rows)
    assert all(row["revision"]["status"] == "not_available" for row in calendar_rows)


def test_stock_connect_currency_is_hkd_and_never_cross_currency_aggregated():
    pulse = _build()
    market = _desk(pulse, "markets-capital")
    southbound = [row for row in market["metrics"] if "southbound" in row["metric_id"]]
    northbound = [row for row in market["metrics"] if "Northbound" in row["label"]]

    assert southbound and {row["unit"] for row in southbound} == {"HKD billion"}
    assert northbound and {row["unit"] for row in northbound} == {"CNY billion"}
    check = next(row for row in pulse["input_integrity"] if row["check_id"] == "stock-connect-currency")
    assert check["status"] == "pass"

    spec = next(signal for signal in osint.SIGNALS if signal.id == "stock-connect")
    assert spec.metric_unit == "HKD billions"
    config = json.loads((ROOT / "config" / "newsroom.json").read_text(encoding="utf-8"))
    story = next(signal for signal in config["signals"] if signal["id"] == "stock-connect")
    assert "Hong Kong dollars" in story["headline_template"]
    assert "Hong Kong dollars" in story["claim_template"]
    assert "yuan" not in story["headline_template"].lower()


def test_cfets_wide_compatibility_view_matches_the_revision_ledger():
    pulse = _build()
    check = next(
        row for row in pulse["input_integrity"]
        if row["check_id"] == "cfets-wide-ledger-alignment"
    )
    assert check["status"] == "pass"
    assert "15" in check["detail"]


def test_rail_freight_cumulative_and_monthly_observation_periods_are_distinct():
    pulse = _build()
    period = json.loads(
        (ROOT / "readings" / "believability-latest.json").read_text(encoding="utf-8")
    )["asof"]
    year, month = map(int, period.split("-"))
    month_end = calendar.monthrange(year, month)[1]
    physical = _desk(pulse, "trade-logistics-physical")["metrics"]
    cumulative = next(
        row for row in physical
        if row["metric_id"] == "cn-activity-rail-freight-yoy"
    )
    monthly = next(
        row for row in physical
        if row["metric_id"] == "cn-activity-rail-freight-month-yoy"
    )

    assert (
        cumulative["period_start"], cumulative["period_end"], cumulative["frequency"]
    ) == (f"{year:04d}-01-01", f"{period}-{month_end:02d}", "M")
    assert "cumulative" in cumulative["comparability"]["basis"]
    assert (
        monthly["period_start"], monthly["period_end"], monthly["frequency"]
    ) == (f"{period}-01", f"{period}-{month_end:02d}", "M")
    assert "month only" in monthly["comparability"]["basis"]


def test_as_of_excludes_future_wide_inputs_and_future_observations():
    cutoff = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    pulse = _build(as_of=cutoff)
    receipts = {row["input_id"]: row for row in pulse["inputs"]}

    assert receipts["china-econ-wide"]["status"] == "future_excluded"
    assert receipts["data-darkness"]["status"] == "future_excluded"
    assert receipts["china-econ-ledger"]["status"] == "used"
    for metric in _records(pulse):
        assert datetime.fromisoformat(metric["collected_at"].replace("Z", "+00:00")) <= cutoff
        if metric["released_at"]:
            assert datetime.fromisoformat(metric["released_at"].replace("Z", "+00:00")) <= cutoff


def _observation(
    *,
    value,
    revision,
    released,
    collected=None,
    series_id="cn.cfets.shibor_on",
    source_id="cfets_benchmarks",
    unit="%",
    frequency="D",
    period_start=date(2026, 1, 2),
    period_end=None,
):
    released_at = datetime.fromisoformat(released)
    return EconomicObservation(
        series_id=series_id,
        value=value,
        unit=unit,
        frequency=frequency,
        period_start=period_start,
        period_end=period_end or period_start,
        released_at=released_at,
        collected_at=datetime.fromisoformat(collected) if collected else released_at,
        source_id=source_id,
        evidence_url="https://www.chinamoney.com.cn/example",
        revision=revision,
        raw_sha256=sha256_bytes(f"revision-{revision}".encode()),
        metadata={"family": "shibor", "release_time_semantics": "test fixture"},
    )


def _write_ledger(path, rows):
    path.write_text(
        "".join(json.dumps(row.to_dict(), sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_revision_selection_and_ledger_are_point_in_time(tmp_path):
    initial = _observation(
        value=1.5, revision=0, released="2026-01-03T00:00:00+00:00"
    )
    revised = _observation(
        value=1.4, revision=1, released="2026-02-01T00:00:00+00:00"
    )
    _write_ledger(tmp_path / "china-econ-observations.jsonl", [initial, revised])

    january = build_economic_pulse(
        readings_dir=tmp_path,
        registry_path=REGISTRY,
        as_of=datetime(2026, 1, 15, tzinfo=UTC),
    )
    february = build_economic_pulse(
        readings_dir=tmp_path,
        registry_path=REGISTRY,
        as_of=datetime(2026, 2, 2, tzinfo=UTC),
    )

    jan_metric = _desk(january, "money-credit-fx")["metrics"][0]
    feb_metric = _desk(february, "money-credit-fx")["metrics"][0]
    assert jan_metric["value"] == 1.5
    assert jan_metric["revision"] == {
        "status": "original", "number": 0, "previous_value": None, "delta": None
    }
    assert january["revisions"] == []
    assert feb_metric["value"] == 1.4
    assert feb_metric["revision"]["previous_value"] == 1.5
    assert feb_metric["revision"]["delta"] == pytest.approx(-0.1)
    assert len(february["revisions"]) == 1
    assert february["revisions"][0]["delta"] == pytest.approx(-0.1)


def test_late_collection_does_not_leak_into_an_earlier_replay(tmp_path):
    late = _observation(
        value=1.5,
        revision=0,
        released="2026-01-03T00:00:00+00:00",
        collected="2026-03-01T00:00:00+00:00",
    )
    _write_ledger(tmp_path / "china-econ-observations.jsonl", [late])

    pulse = build_economic_pulse(
        readings_dir=tmp_path,
        registry_path=REGISTRY,
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert _desk(pulse, "money-credit-fx")["metrics"] == []
    assert next(row for row in pulse["inputs"] if row["input_id"] == "china-econ-ledger")["status"] == "future_excluded"


def test_reviewed_non_cfets_series_routes_to_its_explicit_desk_and_revises(tmp_path):
    common = {
        "series_id": "cn.mot.rail_freight_ytd_yoy",
        "source_id": "mot_transport",
        "unit": "percent",
        "frequency": "M",
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 6, 30),
    }
    initial = _observation(
        value=2.5,
        revision=0,
        released="2026-07-01T00:00:00+00:00",
        **common,
    )
    revised = _observation(
        value=2.7,
        revision=1,
        released="2026-07-15T00:00:00+00:00",
        **common,
    )
    _write_ledger(tmp_path / "china-econ-observations.jsonl", [initial, revised])

    pulse = build_economic_pulse(
        readings_dir=tmp_path,
        registry_path=REGISTRY,
        as_of=datetime(2026, 7, 16, tzinfo=UTC),
    )
    metric = _desk(pulse, "trade-logistics-physical")["metrics"][0]

    assert _desk(pulse, "money-credit-fx")["metrics"] == []
    assert metric["metric_id"] == "cn-mot-rail-freight-ytd-yoy"
    assert metric["label"] == "Rail freight cumulative year-on-year growth"
    assert metric["source_id"] == "mot_transport"
    assert metric["independence_group"] == "mot_transport_statistics"
    assert metric["source_class"] == "physical"
    assert metric["freshness"]["budget_hours"] == 1_200.0
    assert metric["comparability"]["concept_id"] == "year-on-year-growth-percent"
    assert metric["revision"] == {
        "status": "revised",
        "number": 1,
        "previous_value": 2.5,
        "delta": pytest.approx(0.2),
    }
    assert len(pulse["revisions"]) == 1
    assert pulse["revisions"][0]["series_id"] == common["series_id"]
    assert not any(
        row["check_id"] == "ledger-series-routing"
        for row in pulse["input_integrity"]
    )


def test_future_non_cfets_series_is_excluded_from_metrics_and_revisions(tmp_path):
    future = _observation(
        value=2.5,
        revision=0,
        released="2026-08-01T00:00:00+00:00",
        series_id="cn.mot.rail_freight_ytd_yoy",
        source_id="mot_transport",
        unit="percent",
        frequency="M",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 7, 31),
    )
    _write_ledger(tmp_path / "china-econ-observations.jsonl", [future])

    pulse = build_economic_pulse(
        readings_dir=tmp_path,
        registry_path=REGISTRY,
        as_of=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
    )

    assert pulse["n_metrics"] == 0
    assert pulse["revisions"] == []
    receipt = next(
        row for row in pulse["inputs"] if row["input_id"] == "china-econ-ledger"
    )
    assert receipt["status"] == "future_excluded"
    assert not any(
        row["check_id"] == "ledger-series-routing"
        for row in pulse["input_integrity"]
    )


def test_unknown_visible_ledger_series_is_excluded_with_integrity_receipt(tmp_path):
    unknown = _observation(
        value=99.0,
        revision=0,
        released="2026-07-01T00:00:00+00:00",
        series_id="cn.future.unreviewed_index",
        source_id="mot_transport",
        unit="points",
        frequency="M",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )
    _write_ledger(tmp_path / "china-econ-observations.jsonl", [unknown])

    pulse = build_economic_pulse(
        readings_dir=tmp_path,
        registry_path=REGISTRY,
        as_of=datetime(2026, 7, 2, tzinfo=UTC),
    )

    assert pulse["n_metrics"] == 0
    assert pulse["revisions"] == []
    receipt = next(
        row for row in pulse["input_integrity"]
        if row["check_id"] == "ledger-series-routing"
    )
    assert receipt["status"] == "warning"
    assert "cn.future.unreviewed_index" in receipt["detail"]
    assert "No desk, label, or comparability semantics were inferred" in receipt["detail"]


def test_reviewed_ledger_series_with_wrong_unit_fails_closed(tmp_path):
    wrong = _observation(
        value=2.5,
        revision=0,
        released="2026-07-01T00:00:00+00:00",
        series_id="cn.mot.rail_freight_ytd_yoy",
        source_id="mot_transport",
        unit="tonnes",
        frequency="M",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 6, 30),
    )
    _write_ledger(tmp_path / "china-econ-observations.jsonl", [wrong])

    with pytest.raises(EconomicPulseError, match="expected one of.*percent"):
        build_economic_pulse(
            readings_dir=tmp_path,
            registry_path=REGISTRY,
            as_of=datetime(2026, 7, 2, tzinfo=UTC),
        )


def test_not_collected_is_not_converted_to_zero(tmp_path):
    pulse = build_economic_pulse(
        readings_dir=tmp_path,
        registry_path=REGISTRY,
        as_of=datetime(2026, 8, 11, tzinfo=UTC),
    )
    assert pulse["n_metrics"] == 0
    assert all(desk["status"] == "not_collected" for desk in pulse["desks"])
    assert all(receipt["status"] == "missing" for receipt in pulse["inputs"])
    assert pulse["economic_state"]["composite"] is None


@pytest.mark.parametrize("constant", [float("nan"), float("inf"), float("-inf")])
def test_validator_rejects_nonfinite_numbers(constant):
    pulse = _build()
    pulse["desks"][0]["metrics"][0]["value"] = constant
    with pytest.raises(EconomicPulseError, match="finite"):
        validate_economic_pulse(pulse)


def test_validator_rejects_duplicate_metrics_even_when_counts_are_adjusted():
    pulse = _build()
    desk = pulse["desks"][0]
    desk["metrics"].append(copy.deepcopy(desk["metrics"][0]))
    desk["n_metrics"] += 1
    pulse["n_metrics"] += 1
    with pytest.raises(EconomicPulseError, match="duplicate metric_id"):
        validate_economic_pulse(pulse)


def test_validator_rejects_mixed_units_within_one_comparability_concept():
    pulse = _build()
    shibor = [
        metric for metric in _desk(pulse, "money-credit-fx")["metrics"]
        if metric["comparability"]["concept_id"] == "money-market-rate-percent"
    ]
    assert len(shibor) > 1
    shibor[-1]["unit"] = "basis points"
    with pytest.raises(EconomicPulseError, match="mixes units"):
        validate_economic_pulse(pulse)


def test_validator_rejects_person_level_fields_anywhere():
    pulse = _build()
    pulse["desks"][0]["metrics"][0]["respondent_id"] = "r-1"
    with pytest.raises(EconomicPulseError, match="aggregate-only"):
        validate_economic_pulse(pulse)


def test_duplicate_input_keys_and_nonfinite_constants_fail_closed(tmp_path):
    (tmp_path / "stock-connect-latest.json").write_text(
        '{"generated_at":"2026-01-01T00:00:00Z",'
        '"generated_at":"2026-01-02T00:00:00Z"}',
        encoding="utf-8",
    )
    with pytest.raises(EconomicPulseError, match="duplicate JSON key"):
        build_economic_pulse(
            readings_dir=tmp_path,
            registry_path=REGISTRY,
            as_of=datetime(2026, 2, 1, tzinfo=UTC),
        )

    (tmp_path / "stock-connect-latest.json").write_text(
        '{"generated_at":"2026-01-01T00:00:00Z","value":NaN}',
        encoding="utf-8",
    )
    with pytest.raises(EconomicPulseError, match="non-finite"):
        build_economic_pulse(
            readings_dir=tmp_path,
            registry_path=REGISTRY,
            as_of=datetime(2026, 2, 1, tzinfo=UTC),
        )


def test_wrong_southbound_currency_fails_closed(tmp_path):
    wrong = json.loads(
        (ROOT / "readings" / "stock-connect-latest.json").read_text(encoding="utf-8")
    )
    wrong["units"]["southbound_net_b"] = "CNY bn, wrong"
    (tmp_path / "stock-connect-latest.json").write_text(
        json.dumps(wrong), encoding="utf-8"
    )
    with pytest.raises(EconomicPulseError, match="southbound flow must be declared in HKD"):
        build_economic_pulse(
            readings_dir=tmp_path,
            registry_path=REGISTRY,
            as_of=datetime(2026, 8, 11, tzinfo=UTC),
        )


def test_cli_writes_atomically_checks_drift_and_uses_public_mode(tmp_path):
    output = tmp_path / "china-economic-pulse-latest.json"
    common = [
        "--readings-dir", str(ROOT / "readings"),
        "--registry", str(REGISTRY),
        "--output", str(output),
    ]
    assert cli.main(common) == 0
    first = output.read_bytes()
    assert first.endswith(b"\n")
    assert output.stat().st_mode & 0o777 == 0o644
    assert cli.main([*common, "--check"]) == 0

    output.write_text("{}\n", encoding="utf-8")
    assert cli.main([*common, "--check"]) == 1
    assert output.read_text(encoding="utf-8") == "{}\n"
