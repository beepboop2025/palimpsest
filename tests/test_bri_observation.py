"""Unit contracts for BRI national economic observations."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from core.bri_observation import (
    BRIEconomicObservation,
    BRIObservationError,
    BRIRights,
    canonical_json_bytes,
    request_id_for,
    sha256_bytes,
)


RETRIEVED_AT = datetime(2026, 8, 26, 10, 30, tzinfo=UTC)
RELEASE_UPPER_BOUND = datetime(2026, 7, 13, 23, 59, 59, tzinfo=UTC)
EVIDENCE_URL = (
    "https://api.worldbank.org/v2/country/CHN;MMR;PAK/indicator/"
    "NY.GDP.MKTP.KD.ZG?source=2&date=2024%3A2024&format=json&"
    "per_page=20000&footnote=y"
)
RAW_HASH = sha256_bytes(b"exact World Bank response")
ROW_HASH = sha256_bytes(b"canonical source row")
ACQUISITION_ID = sha256_bytes(b"canonical acquisition receipt")


def _observation(**changes: object) -> BRIEconomicObservation:
    values: dict[str, object] = {
        "series_id": "bri.context.wdi.gdp_real_growth",
        "indicator_id": "NY.GDP.MKTP.KD.ZG",
        "country_code": "PAK",
        "value": 2.5,
        "unit": "annual percent",
        "evidence_state": "observed",
        "unavailability_reason": None,
        "obs_status": "",
        "footnote": "",
        "scale": "",
        "period_start": date(2024, 1, 1),
        "period_end": date(2024, 12, 31),
        "source_release_upper_bound": RELEASE_UPPER_BOUND,
        "retrieved_at": RETRIEVED_AT,
        "source_dataset_last_updated": date(2026, 7, 13),
        "evidence_url": EVIDENCE_URL,
        "raw_response_sha256": RAW_HASH,
        "source_row_sha256": ROW_HASH,
        "request_id": request_id_for(
            evidence_url=EVIDENCE_URL,
            raw_response_sha256=RAW_HASH,
        ),
        "acquisition_id": ACQUISITION_ID,
    }
    values.update(changes)
    return BRIEconomicObservation(**values)


def test_round_trip_preserves_strict_bitemporal_record_and_stable_id():
    observation = _observation()
    document = observation.to_dict()
    clone = BRIEconomicObservation.from_dict(document)

    assert clone == observation
    assert clone.observation_id == observation.observation_id
    assert document["source_release_upper_bound"] == "2026-07-13T23:59:59Z"
    assert document["retrieved_at"] == "2026-08-26T10:30:00Z"
    assert document["context_scope"] == "national_economic_context"
    assert document["causality_boundary"] == "not_evidence_of_bri_causality"
    assert document["aggregate_level"] == "country"
    assert document["obs_status"] == ""
    assert document["footnote"] == ""
    assert document["scale"] == ""
    assert document["acquisition_id"] == ACQUISITION_ID


def test_canonical_json_and_identity_are_order_independent_and_strict():
    left = {"country": "PAK", "value": 2.5}
    right = {"value": 2.5, "country": "PAK"}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_bytes(left).endswith(b"\n")
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})

    base = _observation()
    changed = replace(base, source_row_sha256=sha256_bytes(b"revised source row"))
    assert changed.observation_id != base.observation_id

    for field, value in (
        ("obs_status", "F"),
        ("footnote", "Source qualification"),
        ("scale", "millions"),
    ):
        changes = {field: value}
        if field == "obs_status":
            changes["evidence_state"] = "forecast"
        qualified = replace(base, **changes)
        assert qualified.observation_id != base.observation_id

    multiline = replace(base, footnote="Source qualification.\n")
    assert multiline.footnote == "Source qualification.\n"
    assert multiline.observation_id != base.observation_id
    with pytest.raises(BRIObservationError, match="control characters"):
        replace(base, footnote="unsafe\x00qualification")


def test_null_is_an_explicit_unavailable_state_never_numeric_zero():
    unavailable = _observation(
        value=None,
        evidence_state="unavailable",
        unavailability_reason="source_value_null",
    )
    assert unavailable.to_dict()["value"] is None

    with pytest.raises(BRIObservationError, match="null value"):
        _observation(
            value=0,
            evidence_state="unavailable",
            unavailability_reason="source_value_null",
        )
    with pytest.raises(BRIObservationError, match="real number"):
        _observation(value=None)
    with pytest.raises(BRIObservationError, match="unavailability_reason"):
        _observation(unavailability_reason="source_value_null")


def test_forecast_is_distinct_and_source_status_fails_closed():
    forecast = _observation(evidence_state="forecast", obs_status="F")
    assert forecast.to_dict()["evidence_state"] == "forecast"
    assert forecast.to_dict()["obs_status"] == "F"

    with pytest.raises(BRIObservationError, match="require obs_status"):
        _observation(evidence_state="forecast")
    with pytest.raises(BRIObservationError, match="require obs_status"):
        _observation(obs_status="F")
    with pytest.raises(BRIObservationError, match="empty or F"):
        _observation(obs_status="P")
    with pytest.raises(BRIObservationError, match="real number"):
        _observation(
            value=None,
            evidence_state="forecast",
            obs_status="F",
        )


def test_release_clock_is_exactly_the_lastupdated_upper_bound():
    early_retrieval = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
    row = _observation(
        retrieved_at=early_retrieval,
        source_release_upper_bound=early_retrieval,
    )
    assert row.source_release_upper_bound == early_retrieval

    with pytest.raises(BRIObservationError, match="conservatively bind"):
        _observation(
            source_release_upper_bound=datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
        )
    with pytest.raises(BRIObservationError, match="future"):
        _observation(source_dataset_last_updated=date(2026, 8, 27))
    with pytest.raises(BRIObservationError, match="timezone-aware"):
        _observation(retrieved_at=datetime(2026, 8, 26, 10, 30))


def test_contract_rejects_non_national_or_causal_reinterpretation():
    with pytest.raises(BRIObservationError, match="country_code"):
        _observation(country_code="PAK-BAL")
    with pytest.raises(BRIObservationError, match="context_scope"):
        _observation(context_scope="project_effect")
    with pytest.raises(BRIObservationError, match="causality_boundary"):
        _observation(causality_boundary="bri_caused")
    with pytest.raises(BRIObservationError, match="aggregate_level"):
        _observation(aggregate_level="project")


def test_rights_and_request_receipt_cannot_be_detached_or_rewritten():
    with pytest.raises(BRIObservationError, match="reviewed WDI attribution"):
        BRIRights(license="proprietary")
    with pytest.raises(BRIObservationError, match="request_id"):
        _observation(request_id="0" * 64)
    with pytest.raises(BRIObservationError, match="reviewed World Bank"):
        request_id_for(
            evidence_url="https://example.test/wdi",
            raw_response_sha256=RAW_HASH,
        )


def test_from_dict_fails_closed_on_field_drift_or_id_tampering():
    document = _observation().to_dict()
    with_extra = {**document, "actor": "not allowed"}
    with pytest.raises(BRIObservationError, match="fields changed"):
        BRIEconomicObservation.from_dict(with_extra)

    tampered = json.loads(json.dumps(document))
    tampered["value"] = 99.0
    with pytest.raises(BRIObservationError, match="does not authenticate"):
        BRIEconomicObservation.from_dict(tampered)

    noncanonical = json.loads(json.dumps(document))
    noncanonical["retrieved_at"] = "2026-08-26T10:30:00+00:00"
    with pytest.raises(BRIObservationError, match="canonical UTC"):
        BRIEconomicObservation.from_dict(noncanonical)
