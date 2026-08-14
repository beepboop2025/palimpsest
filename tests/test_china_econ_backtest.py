"""Unit contracts for deterministic, bitemporal economic backtesting."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from core.econ_ledger import append_vintages
from core.econ_observation import EconomicObservation
from core.economic_forecast import forecast_values, score_predictions
from processors.china_econ_backtest import (
    CONFIG_SCHEMA_VERSION,
    ForecastBuildError,
    build_forecast_document,
)


UTC = timezone.utc


def _model(
    model_id: str,
    kind: str,
    *,
    minimum: int = 1,
    scored: int = 1,
    lag: int | None = None,
    lookback: int | None = None,
    coverage: float | None = 0.8,
    bridge_minimum: int = 1,
) -> dict:
    return {
        "model_id": model_id,
        "kind": kind,
        "description": f"test {kind}",
        "min_train_observations": minimum,
        "min_scored_folds": scored,
        "seasonal_lag": lag,
        "delta_lookback": lookback,
        "interval_coverage": coverage,
        "min_interval_residuals": 2,
        "min_bridge_contributors": bridge_minimum,
    }


def _config(
    models: list[dict],
    *,
    bridge_series: list[str] | None = None,
    gates: dict | None = None,
) -> dict:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "config_version": 1,
        "scope": "Synthetic named-series test forecast.",
        "engine_version": "palimpsest-economic-forecast-engine.v1",
        "baseline_model_id": models[0]["model_id"],
        "promotion_gates": gates or {
            "min_first_release_folds": 20,
            "min_latest_revised_folds": 20,
            "min_history_span_days": 365,
            "min_source_groups": 2,
            "min_independence_groups": 2,
            "min_revised_outcomes": 2,
            "min_challenger_relative_mae_improvement": 0.1,
            "min_interval_folds": 10,
            "min_interval_coverage": 0.5,
            "max_interval_coverage": 1.0,
        },
        "models": models,
        "targets": [{
            "target_id": "test-next-observation",
            "label": "Test series — next observation",
            "series_id": "cn.test.target",
            "source_id": "source_a",
            "unit": "index",
            "frequency": "D",
            "geography": "CN",
            "sector": "all",
            "firm_size": "all",
            "ownership": "all",
            "enabled": True,
            "horizon": "next_observed_period",
            "model_ids": [model["model_id"] for model in models],
            "bridge_contributor_series_ids": bridge_series or [],
        }],
    }


def _registry(*, include_bridge: bool = False) -> dict:
    sources = [{
        "source_id": "source_a",
        "independence_group": "official_a",
    }]
    if include_bridge:
        sources.append({
            "source_id": "source_b",
            "independence_group": "market_b",
        })
    return {"sources": sources}


def _row(
    period: date,
    value: float,
    *,
    released: datetime,
    collected: datetime | None = None,
    revision: int = 0,
    series_id: str = "cn.test.target",
    source_id: str = "source_a",
) -> EconomicObservation:
    fingerprint = f"{series_id}|{period}|{value}|{revision}|{collected or released}"
    return EconomicObservation(
        series_id=series_id,
        value=value,
        unit="index",
        frequency="D",
        period_start=period,
        period_end=period,
        released_at=released,
        collected_at=collected or released,
        source_id=source_id,
        evidence_url="https://example.test/economic-release",
        revision=revision,
        raw_sha256=hashlib.sha256(fingerprint.encode()).hexdigest(),
        metadata={"method_version": 1},
    )


def _write_inputs(
    tmp_path: Path,
    rows: list[EconomicObservation],
    config: dict,
    registry: dict,
) -> tuple[Path, Path, Path]:
    ledger = tmp_path / "ledger.jsonl"
    append_vintages(ledger, sorted(rows, key=lambda row: row.collected_at))
    config_path = tmp_path / "targets.json"
    registry_path = tmp_path / "sources.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    return ledger, config_path, registry_path


def test_simple_baselines_and_scoring_are_transparent():
    random_walk = forecast_values(
        _model("rw", "random_walk"), [1.0, 2.0, 4.0]
    )
    seasonal = forecast_values(
        _model("seasonal", "seasonal_naive", lag=2), [1.0, 2.0, 4.0]
    )
    mean_delta = forecast_values(
        _model("delta", "mean_delta", lookback=2), [1.0, 2.0, 4.0]
    )

    assert random_walk["point"] == 4.0
    assert seasonal["point"] == 2.0
    assert mean_delta["point"] == 5.5
    for prediction in (random_walk, seasonal, mean_delta):
        for field in ("point", "lower", "upper"):
            value = prediction[field]
            if value is not None:
                assert value == round(value, 12)
    scores = score_predictions([{
        "actual": 6.0,
        "point": 5.0,
        "origin": 4.0,
        "lower": 4.5,
        "upper": 5.5,
        "interval_coverage": 0.8,
    }])
    assert scores["mae"] == scores["median_absolute_error"] == scores["rmse"] == 1.0
    assert scores["directional_accuracy"] == 1.0
    assert scores["intervals"]["empirical_coverage"] == 0.0
    assert scores["intervals"]["mean_wis"] > scores["mae"]


def test_fold_enforces_collection_clock_and_scores_outcome_vintages_separately(tmp_path):
    first_period = date(2026, 1, 1)
    target_period = date(2026, 1, 5)
    rows = [
        _row(
            first_period,
            1.0,
            released=datetime(2026, 1, 2, 9, tzinfo=UTC),
        ),
        _row(
            target_period,
            2.0,
            released=datetime(2026, 1, 6, 9, tzinfo=UTC),
        ),
        # Public before the second fold, but Palimpsest did not collect this
        # revision until later.  It must not replace the origin value in replay.
        _row(
            first_period,
            9.0,
            released=datetime(2026, 1, 3, 9, tzinfo=UTC),
            collected=datetime(2026, 1, 10, 9, tzinfo=UTC),
            revision=1,
        ),
        _row(
            target_period,
            4.0,
            released=datetime(2026, 1, 11, 9, tzinfo=UTC),
            collected=datetime(2026, 1, 12, 9, tzinfo=UTC),
            revision=1,
        ),
    ]
    models = [
        _model("random", "random_walk"),
        _model("seasonal", "seasonal_naive", lag=5, minimum=6),
    ]
    ledger, config_path, registry_path = _write_inputs(
        tmp_path, rows, _config(models), _registry()
    )
    document = build_forecast_document(
        ledger_path=ledger,
        config_path=config_path,
        source_registry_path=registry_path,
    )
    target = document["targets"][0]
    fold = next(
        fold for fold in target["folds"]
        if fold["target_period_start"] == target_period.isoformat()
    )
    prediction = next(
        row for row in fold["predictions"] if row["model_id"] == "random"
    )

    assert fold["selection"]["excluded_by_collection_clock"] == 1
    assert prediction["point"] == prediction["origin_value"] == 1.0
    random_summary = next(row for row in target["models"] if row["model_id"] == "random")
    assert random_summary["first_release_scores"]["mae"] == 1.0
    assert random_summary["latest_revised_scores"]["mae"] == 3.0
    seasonal_summary = next(
        row for row in target["models"] if row["model_id"] == "seasonal"
    )
    assert seasonal_summary["status"] == "failed"
    assert seasonal_summary["spec"]["seasonal_lag"] == 5
    assert seasonal_summary["failure_counts"] == [
        {"code": "insufficient_training_history", "count": 2}
    ]
    assert target["status"] == "warming_up"
    assert target["promotion"]["champion_model_id"] is None
    assert target["nowcast"] is None


def test_explicit_frozen_gates_can_promote_a_superior_independent_bridge(tmp_path):
    origin = date(2026, 1, 1)
    rows = []
    for offset in range(16):
        period = origin + timedelta(days=offset)
        bridge_release = datetime.combine(period, time(18), tzinfo=UTC)
        target_release = datetime.combine(period + timedelta(days=1), time(9), tzinfo=UTC)
        rows.extend([
            _row(
                period,
                float(offset),
                released=target_release,
            ),
            _row(
                period,
                100.0 + offset,
                released=bridge_release,
                series_id="cn.test.bridge",
                source_id="source_b",
            ),
        ])
    models = [
        _model("random", "random_walk", scored=5),
        _model(
            "bridge", "equal_delta_bridge", scored=5, coverage=None,
            bridge_minimum=1,
        ),
    ]
    gates = {
        "min_first_release_folds": 5,
        "min_latest_revised_folds": 5,
        "min_history_span_days": 5,
        "min_source_groups": 2,
        "min_independence_groups": 2,
        "min_revised_outcomes": 0,
        "min_challenger_relative_mae_improvement": 0.5,
        "min_interval_folds": 0,
        "min_interval_coverage": 0.0,
        "max_interval_coverage": 1.0,
    }
    ledger, config_path, registry_path = _write_inputs(
        tmp_path,
        rows,
        _config(models, bridge_series=["cn.test.bridge"], gates=gates),
        _registry(include_bridge=True),
    )
    first = build_forecast_document(
        ledger_path=ledger,
        config_path=config_path,
        source_registry_path=registry_path,
    )
    second = build_forecast_document(
        ledger_path=ledger,
        config_path=config_path,
        source_registry_path=registry_path,
    )

    assert first == second
    target = first["targets"][0]
    assert target["promotion"]["status"] == "passed"
    assert target["promotion"]["candidate_model_id"] == "bridge"
    assert target["promotion"]["champion_model_id"] == "bridge"
    assert all(check["passed"] for check in target["promotion"]["checks"])
    assert target["nowcast"]["point"] == 16.0
    assert target["nowcast"]["source_ids"] == ["source_a", "source_b"]
    assert target["nowcast"]["independence_groups"] == ["market_b", "official_a"]
    assert first["status"] == "ready"


def test_config_and_ledger_fail_closed(tmp_path):
    models = [_model("random", "random_walk")]
    config = _config(models)
    config["targets"][0]["source_id"] = "unregistered"
    ledger, config_path, registry_path = _write_inputs(
        tmp_path,
        [_row(
            date(2026, 1, 1),
            1.0,
            released=datetime(2026, 1, 2, tzinfo=UTC),
        )],
        config,
        _registry(),
    )
    with pytest.raises(ForecastBuildError, match="absent from source registry"):
        build_forecast_document(
            ledger_path=ledger,
            config_path=config_path,
            source_registry_path=registry_path,
        )

    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    with pytest.raises(ForecastBuildError, match="empty"):
        build_forecast_document(
            ledger_path=empty,
            config_path=config_path,
            source_registry_path=registry_path,
        )
