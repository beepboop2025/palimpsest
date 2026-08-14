"""Pseudo-real-time, revision-aware backtests for named economic series.

This module is a publication engine, not a broad macro model.  Every fold is
anchored immediately before a target's first public release and selects feature
rows which pass both the release and Palimpsest collection clocks.  Target
outcomes are then scored twice: once against the first release and once against
the latest revision in the authenticated snapshot.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.econ_ledger import (
    LedgerIntegrityError,
    LedgerSnapshot,
    load_snapshot,
    observations_as_of,
    snapshot_digest,
)
from core.econ_observation import EconomicObservation
from core.economic_forecast import (
    ENGINE_VERSION,
    MODEL_KINDS,
    ForecastModelError,
    failure_counts,
    forecast_values,
    score_predictions,
    sha256_json,
)


SCHEMA_VERSION = "palimpsest-economic-forecast.v1"
CONFIG_SCHEMA_VERSION = "palimpsest-china-econ-targets.v1"
TARGET_HORIZON = "next_observed_period"


class ForecastBuildError(RuntimeError):
    """Inputs cannot support a trustworthy forecast publication."""


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _load_json(path: str | Path) -> tuple[dict[str, Any], bytes]:
    location = Path(path)
    try:
        payload = location.read_bytes()
        document = json.loads(
            payload,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ForecastBuildError(f"cannot load {location}: {exc}") from exc
    if not isinstance(document, dict):
        raise ForecastBuildError(f"{location}: top level must be an object")
    return document, payload


def _exact_keys(
    value: Mapping[str, object], required: set[str], path: str
) -> None:
    missing = required - set(value)
    extra = set(value) - required
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing {sorted(missing)}")
        if extra:
            detail.append(f"unexpected {sorted(extra)}")
        raise ForecastBuildError(f"{path}: {'; '.join(detail)}")


def _string(value: object, path: str) -> str:
    if type(value) is not str or not value.strip():
        raise ForecastBuildError(f"{path} must be a nonblank string")
    return value


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ForecastBuildError(f"{path} must be an integer")
    value = int(value)
    if value < minimum:
        raise ForecastBuildError(f"{path} must be at least {minimum}")
    return value


def _number(value: object, path: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ForecastBuildError(f"{path} must be a number")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ForecastBuildError(f"{path} must lie in [{minimum}, {maximum}]")
    return value


def validate_config(config: Mapping[str, object]) -> dict[str, Any]:
    """Validate and normalize the frozen target/model/promotion specification."""

    if not isinstance(config, Mapping):
        raise ForecastBuildError("forecast config must be an object")
    required = {
        "schema_version",
        "config_version",
        "scope",
        "engine_version",
        "baseline_model_id",
        "promotion_gates",
        "models",
        "targets",
    }
    _exact_keys(config, required, "config")
    if config["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise ForecastBuildError(f"config.schema_version must be {CONFIG_SCHEMA_VERSION!r}")
    _integer(config["config_version"], "config.config_version", minimum=1)
    _string(config["scope"], "config.scope")
    if config["engine_version"] != ENGINE_VERSION:
        raise ForecastBuildError(f"config.engine_version must be {ENGINE_VERSION!r}")
    baseline_id = _string(config["baseline_model_id"], "config.baseline_model_id")

    gate_keys = {
        "min_first_release_folds",
        "min_latest_revised_folds",
        "min_history_span_days",
        "min_source_groups",
        "min_independence_groups",
        "min_revised_outcomes",
        "min_challenger_relative_mae_improvement",
        "min_interval_folds",
        "min_interval_coverage",
        "max_interval_coverage",
    }
    gates = config["promotion_gates"]
    if not isinstance(gates, Mapping):
        raise ForecastBuildError("config.promotion_gates must be an object")
    _exact_keys(gates, gate_keys, "config.promotion_gates")
    for key in gate_keys - {
        "min_challenger_relative_mae_improvement",
        "min_interval_coverage",
        "max_interval_coverage",
    }:
        _integer(gates[key], f"config.promotion_gates.{key}")
    _number(
        gates["min_challenger_relative_mae_improvement"],
        "config.promotion_gates.min_challenger_relative_mae_improvement",
        minimum=0.0,
        maximum=1.0,
    )
    minimum_coverage = _number(
        gates["min_interval_coverage"],
        "config.promotion_gates.min_interval_coverage",
        minimum=0.0,
        maximum=1.0,
    )
    maximum_coverage = _number(
        gates["max_interval_coverage"],
        "config.promotion_gates.max_interval_coverage",
        minimum=0.0,
        maximum=1.0,
    )
    if minimum_coverage > maximum_coverage:
        raise ForecastBuildError("minimum interval coverage cannot exceed maximum")

    model_keys = {
        "model_id",
        "kind",
        "description",
        "min_train_observations",
        "min_scored_folds",
        "seasonal_lag",
        "delta_lookback",
        "interval_coverage",
        "min_interval_residuals",
        "min_bridge_contributors",
    }
    raw_models = config["models"]
    if not isinstance(raw_models, list) or not raw_models:
        raise ForecastBuildError("config.models must be a non-empty array")
    model_ids: set[str] = set()
    for index, model in enumerate(raw_models):
        path = f"config.models[{index}]"
        if not isinstance(model, Mapping):
            raise ForecastBuildError(f"{path} must be an object")
        _exact_keys(model, model_keys, path)
        model_id = _string(model["model_id"], f"{path}.model_id")
        if model_id in model_ids:
            raise ForecastBuildError(f"duplicate model_id {model_id!r}")
        model_ids.add(model_id)
        if model["kind"] not in MODEL_KINDS:
            raise ForecastBuildError(f"{path}.kind is unsupported")
        _string(model["description"], f"{path}.description")
        _integer(model["min_train_observations"], f"{path}.min_train_observations", minimum=1)
        _integer(model["min_scored_folds"], f"{path}.min_scored_folds", minimum=1)
        _integer(model["min_interval_residuals"], f"{path}.min_interval_residuals", minimum=1)
        _integer(model["min_bridge_contributors"], f"{path}.min_bridge_contributors", minimum=1)
        for field in ("seasonal_lag", "delta_lookback"):
            if model[field] is not None:
                _integer(model[field], f"{path}.{field}", minimum=1)
        if model["interval_coverage"] is not None:
            coverage = _number(
                model["interval_coverage"],
                f"{path}.interval_coverage",
                minimum=0.0,
                maximum=1.0,
            )
            if coverage in (0.0, 1.0):
                raise ForecastBuildError(f"{path}.interval_coverage must be strictly interior")
        kind = model["kind"]
        if kind == "seasonal_naive" and model["seasonal_lag"] is None:
            raise ForecastBuildError(f"{path}.seasonal_lag is required")
        if kind == "mean_delta" and model["delta_lookback"] is None:
            raise ForecastBuildError(f"{path}.delta_lookback is required")
    if baseline_id not in model_ids:
        raise ForecastBuildError("baseline_model_id does not name a configured model")

    target_keys = {
        "target_id",
        "label",
        "series_id",
        "source_id",
        "unit",
        "frequency",
        "geography",
        "sector",
        "firm_size",
        "ownership",
        "enabled",
        "horizon",
        "model_ids",
        "bridge_contributor_series_ids",
    }
    raw_targets = config["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ForecastBuildError("config.targets must be a non-empty array")
    target_ids: set[str] = set()
    for index, target in enumerate(raw_targets):
        path = f"config.targets[{index}]"
        if not isinstance(target, Mapping):
            raise ForecastBuildError(f"{path} must be an object")
        _exact_keys(target, target_keys, path)
        for field in (
            "target_id", "label", "series_id", "source_id", "unit", "frequency",
            "geography", "sector", "firm_size", "ownership",
        ):
            _string(target[field], f"{path}.{field}")
        if target["target_id"] in target_ids:
            raise ForecastBuildError(f"duplicate target_id {target['target_id']!r}")
        target_ids.add(str(target["target_id"]))
        if type(target["enabled"]) is not bool:
            raise ForecastBuildError(f"{path}.enabled must be boolean")
        if target["horizon"] != TARGET_HORIZON:
            raise ForecastBuildError(f"{path}.horizon must be {TARGET_HORIZON!r}")
        for field in ("model_ids", "bridge_contributor_series_ids"):
            values = target[field]
            if not isinstance(values, list) or any(
                type(value) is not str or not value.strip() for value in values
            ):
                raise ForecastBuildError(f"{path}.{field} must be a string array")
            if len(values) != len(set(values)):
                raise ForecastBuildError(f"{path}.{field} contains duplicates")
        if not target["model_ids"]:
            raise ForecastBuildError(f"{path}.model_ids cannot be empty")
        unknown = set(target["model_ids"]) - model_ids
        if unknown:
            raise ForecastBuildError(f"{path}.model_ids contains unknown ids {sorted(unknown)}")
        if baseline_id not in target["model_ids"]:
            raise ForecastBuildError(f"{path}.model_ids must include the baseline")
        if target["series_id"] in target["bridge_contributor_series_ids"]:
            raise ForecastBuildError(f"{path} cannot bridge the target series to itself")
    return json.loads(json.dumps(config, allow_nan=False))


def _registry_groups(registry: Mapping[str, object]) -> dict[str, str]:
    sources = registry.get("sources")
    if not isinstance(sources, list):
        raise ForecastBuildError("source registry must contain a sources array")
    groups: dict[str, str] = {}
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            raise ForecastBuildError(f"source registry sources[{index}] must be an object")
        source_id = _string(source.get("source_id"), f"registry.sources[{index}].source_id")
        group = _string(
            source.get("independence_group"),
            f"registry.sources[{index}].independence_group",
        )
        if source_id in groups:
            raise ForecastBuildError(f"duplicate registry source_id {source_id!r}")
        groups[source_id] = group
    return groups


def _target_match(row: EconomicObservation, target: Mapping[str, object]) -> bool:
    return (
        row.series_id == target["series_id"]
        and row.source_id == target["source_id"]
        and row.unit == target["unit"]
        and row.frequency == target["frequency"]
        and row.geography == target["geography"]
        and row.sector == target["sector"]
        and row.firm_size == target["firm_size"]
        and row.ownership == target["ownership"]
        and row.status == "observed"
    )


def _outcome(row: EconomicObservation) -> dict[str, object]:
    return {
        "value": row.value,
        "revision": row.revision,
        "released_at": _timestamp(row.released_at),
        "collected_at": _timestamp(row.collected_at),
        "observation_id": row.observation_id,
    }


def _prediction_failure(model_id: str, code: str, message: str) -> dict[str, object]:
    return {
        "model_id": model_id,
        "status": "failed",
        "point": None,
        "lower": None,
        "upper": None,
        "interval_coverage": None,
        "interval_method": None,
        "origin_value": None,
        "training_observations": 0,
        "training_residuals": 0,
        "method": None,
        "contributor_series_ids": [],
        "source_ids": [],
        "independence_groups": [],
        "failure": {"code": code, "message": message},
    }


def _model_prediction(
    model: Mapping[str, object],
    target: Mapping[str, object],
    knowable: Sequence[EconomicObservation],
    source_groups: Mapping[str, str],
) -> dict[str, object]:
    target_rows = sorted(
        (row for row in knowable if _target_match(row, target)),
        key=lambda row: (row.period_end, row.period_start, row.observation_id),
    )
    target_values = [row.value for row in target_rows]
    bridge_ids = list(target["bridge_contributor_series_ids"])
    contributor_rows: dict[str, list[EconomicObservation]] = {}
    contributor_values: dict[str, list[float]] = {}
    for series_id in bridge_ids:
        rows = sorted(
            (
                row for row in knowable
                if row.series_id == series_id
                and row.status == "observed"
                and row.unit == target["unit"]
                and row.geography == target["geography"]
            ),
            key=lambda row: (row.period_end, row.period_start, row.source_id),
        )
        contributor_rows[series_id] = rows
        contributor_values[series_id] = [row.value for row in rows]

    try:
        fitted = forecast_values(
            model,
            target_values,
            contributor_histories=contributor_values,
        )
    except ForecastModelError as exc:
        failed = _prediction_failure(str(model["model_id"]), exc.code, str(exc))
        relevant = target_rows
        if model["kind"] == "equal_delta_bridge":
            relevant = target_rows + [row for rows in contributor_rows.values() for row in rows]
        failed["source_ids"] = sorted({row.source_id for row in relevant})
        failed["independence_groups"] = sorted(
            {source_groups[row.source_id] for row in relevant if row.source_id in source_groups}
        )
        failed["training_observations"] = len(target_rows)
        return failed

    used_series = (
        [str(target["series_id"])]
        if model["kind"] != "equal_delta_bridge"
        else [str(target["series_id"]), *fitted["contributors_used"]]
    )
    relevant_rows = [row for row in target_rows if row.series_id in used_series]
    for series_id in fitted["contributors_used"]:
        relevant_rows.extend(contributor_rows[series_id])
    return {
        "model_id": model["model_id"],
        "status": "ok",
        "point": fitted["point"],
        "lower": fitted["lower"],
        "upper": fitted["upper"],
        "interval_coverage": fitted["interval_coverage"],
        "interval_method": fitted["interval_method"],
        "origin_value": fitted["origin_value"],
        "training_observations": fitted["training_observations"],
        "training_residuals": fitted["training_residuals"],
        "method": fitted["method"],
        "contributor_series_ids": used_series,
        "source_ids": sorted({row.source_id for row in relevant_rows}),
        "independence_groups": sorted(
            {source_groups[row.source_id] for row in relevant_rows if row.source_id in source_groups}
        ),
        "failure": None,
    }


def _folds(
    observations: Sequence[EconomicObservation],
    target: Mapping[str, object],
    models: Sequence[Mapping[str, object]],
    source_groups: Mapping[str, str],
) -> list[dict[str, object]]:
    target_rows = [row for row in observations if _target_match(row, target)]
    grouped: dict[tuple[object, object], list[EconomicObservation]] = defaultdict(list)
    for row in target_rows:
        grouped[(row.period_start, row.period_end)].append(row)

    folds: list[dict[str, object]] = []
    scope_series = {str(target["series_id"]), *target["bridge_contributor_series_ids"]}
    for (period_start, period_end), rows in sorted(grouped.items()):
        ordered = sorted(
            rows,
            key=lambda row: (
                row.released_at,
                row.collected_at,
                row.revision,
                row.observation_id,
            ),
        )
        first, latest = ordered[0], ordered[-1]
        decision_time = first.released_at - timedelta(microseconds=1)
        candidates = [
            row for row in observations
            if row.status == "observed"
            and row.series_id in scope_series
            and row.period_end < period_start
        ]
        knowable = observations_as_of(candidates, decision_time)
        release_excluded = sum(row.released_at > decision_time for row in candidates)
        collection_excluded = sum(
            row.released_at <= decision_time and row.collected_at > decision_time
            for row in candidates
        )
        predictions = [
            _model_prediction(model, target, knowable, source_groups)
            for model in models
        ]
        identity = {
            "target_id": target["target_id"],
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "decision_time": _timestamp(decision_time),
            "feature_snapshot_sha256": snapshot_digest(knowable),
        }
        folds.append({
            "fold_id": sha256_json(identity),
            "target_period_start": period_start.isoformat(),
            "target_period_end": period_end.isoformat(),
            "decision_time": _timestamp(decision_time),
            "first_release_outcome": _outcome(first),
            "latest_revised_outcome": _outcome(latest),
            "outcome_changed": (
                first.value != latest.value or first.revision != latest.revision
            ),
            "feature_snapshot_sha256": identity["feature_snapshot_sha256"],
            "selection": {
                "candidate_rows": len(candidates),
                "selected_rows": len(knowable),
                "excluded_by_release_clock": release_excluded,
                "excluded_by_collection_clock": collection_excluded,
                "same_or_future_period_rows_excluded": sum(
                    row.status == "observed"
                    and row.series_id in scope_series
                    and row.period_end >= period_start
                    for row in observations
                ),
            },
            "predictions": predictions,
        })
    return folds


def _model_summaries(
    target: Mapping[str, object],
    models: Sequence[Mapping[str, object]],
    folds: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    summaries = []
    for model in models:
        model_id = str(model["model_id"])
        first_records = []
        latest_records = []
        failures = []
        sources: set[str] = set()
        groups: set[str] = set()
        for fold in folds:
            prediction = next(
                row for row in fold["predictions"] if row["model_id"] == model_id
            )
            sources.update(prediction["source_ids"])
            groups.update(prediction["independence_groups"])
            if prediction["status"] != "ok":
                failures.append(prediction["failure"])
                continue
            common = {
                "point": prediction["point"],
                "origin": prediction["origin_value"],
                "lower": prediction["lower"],
                "upper": prediction["upper"],
                "interval_coverage": prediction["interval_coverage"],
            }
            first_records.append({
                **common,
                "actual": fold["first_release_outcome"]["value"],
            })
            latest_records.append({
                **common,
                "actual": fold["latest_revised_outcome"]["value"],
            })
        first_scores = score_predictions(first_records)
        latest_scores = score_predictions(latest_records)
        required = int(model["min_scored_folds"])
        scored = min(first_scores["n_scored"], latest_scores["n_scored"])
        if scored == 0:
            status = "failed"
            reason = "no fold produced a scoreable prediction"
        elif scored < required:
            status = "insufficient_history"
            reason = f"requires {required} scored folds; found {scored}"
        else:
            status = "eligible"
            reason = None
        configured_contributors = (
            [target["series_id"]]
            if model["kind"] != "equal_delta_bridge"
            else [target["series_id"], *target["bridge_contributor_series_ids"]]
        )
        summaries.append({
            "model_id": model_id,
            "model_hash": sha256_json({
                "engine_version": ENGINE_VERSION,
                "target": {
                    key: target[key]
                    for key in (
                        "series_id", "source_id", "unit", "frequency", "geography",
                        "sector", "firm_size", "ownership", "horizon",
                    )
                },
                "spec": model,
                "bridge_contributor_series_ids": target["bridge_contributor_series_ids"],
            }),
            "status": status,
            "status_reason": reason,
            "spec": dict(model),
            "configured_contributor_series_ids": configured_contributors,
            "observed_source_ids": sorted(sources),
            "observed_independence_groups": sorted(groups),
            "folds_attempted": len(folds),
            "folds_failed": len(failures),
            "failure_counts": failure_counts(failures),
            "first_release_scores": first_scores,
            "latest_revised_scores": latest_scores,
        })
    return summaries


def _relative_improvement(baseline: object, challenger: object) -> float | None:
    if baseline is None or challenger is None:
        return None
    baseline = float(baseline)
    challenger = float(challenger)
    if baseline <= 0.0:
        return None
    return round((baseline - challenger) / baseline, 12)


def _check(
    gate_id: str,
    *,
    required: object,
    observed: object,
    passed: bool,
    detail: str,
) -> dict[str, object]:
    return {
        "gate_id": gate_id,
        "required": required,
        "observed": observed,
        "passed": bool(passed),
        "detail": detail,
    }


def _promotion(
    target: Mapping[str, object],
    summaries: Sequence[Mapping[str, object]],
    folds: Sequence[Mapping[str, object]],
    gates: Mapping[str, object],
    baseline_model_id: str,
) -> tuple[dict[str, object], Mapping[str, object] | None]:
    eligible = [summary for summary in summaries if summary["status"] == "eligible"]
    baseline = next(
        (summary for summary in eligible if summary["model_id"] == baseline_model_id),
        None,
    )
    challengers = [
        summary for summary in eligible if summary["model_id"] != baseline_model_id
    ]
    candidate = min(
        challengers,
        key=lambda summary: (
            float(summary["first_release_scores"]["mae"])
            + float(summary["latest_revised_scores"]["mae"]),
            summary["model_id"],
        ),
        default=None,
    )
    first_count = max(
        (summary["first_release_scores"]["n_scored"] for summary in summaries),
        default=0,
    )
    latest_count = max(
        (summary["latest_revised_scores"]["n_scored"] for summary in summaries),
        default=0,
    )
    if folds:
        history_span = (
            datetime.fromisoformat(folds[-1]["target_period_end"]).date()
            - datetime.fromisoformat(folds[0]["target_period_start"]).date()
        ).days
    else:
        history_span = 0
    revised = sum(bool(fold["outcome_changed"]) for fold in folds)
    first_improvement = latest_improvement = joint_improvement = None
    if baseline is not None and candidate is not None:
        first_improvement = _relative_improvement(
            baseline["first_release_scores"]["mae"],
            candidate["first_release_scores"]["mae"],
        )
        latest_improvement = _relative_improvement(
            baseline["latest_revised_scores"]["mae"],
            candidate["latest_revised_scores"]["mae"],
        )
        if first_improvement is not None and latest_improvement is not None:
            joint_improvement = min(first_improvement, latest_improvement)

    source_ids = list(candidate["observed_source_ids"]) if candidate else []
    group_ids = list(candidate["observed_independence_groups"]) if candidate else []
    candidate_first_intervals = (
        candidate["first_release_scores"]["intervals"] if candidate else None
    )
    candidate_latest_intervals = (
        candidate["latest_revised_scores"]["intervals"] if candidate else None
    )
    interval_count = min(
        candidate_first_intervals["n_scored"] if candidate_first_intervals else 0,
        candidate_latest_intervals["n_scored"] if candidate_latest_intervals else 0,
    )
    coverages = []
    for interval_scores in (candidate_first_intervals, candidate_latest_intervals):
        if interval_scores and interval_scores["empirical_coverage"] is not None:
            coverages.append(float(interval_scores["empirical_coverage"]))

    checks = [
        _check(
            "first-release-folds",
            required=gates["min_first_release_folds"],
            observed=first_count,
            passed=first_count >= gates["min_first_release_folds"],
            detail="scoreable folds against first-published outcomes",
        ),
        _check(
            "latest-revised-folds",
            required=gates["min_latest_revised_folds"],
            observed=latest_count,
            passed=latest_count >= gates["min_latest_revised_folds"],
            detail="same predictions rescored against latest revised outcomes",
        ),
        _check(
            "history-span-days",
            required=gates["min_history_span_days"],
            observed=history_span,
            passed=history_span >= gates["min_history_span_days"],
            detail="calendar span of target outcomes in the frozen snapshot",
        ),
        _check(
            "candidate-source-groups",
            required=gates["min_source_groups"],
            observed=len(source_ids),
            passed=len(source_ids) >= gates["min_source_groups"],
            detail="source_ids actually used by the eligible challenger",
        ),
        _check(
            "candidate-independent-groups",
            required=gates["min_independence_groups"],
            observed=len(group_ids),
            passed=len(group_ids) >= gates["min_independence_groups"],
            detail="independence groups actually used by the eligible challenger",
        ),
        _check(
            "revised-outcomes",
            required=gates["min_revised_outcomes"],
            observed=revised,
            passed=revised >= gates["min_revised_outcomes"],
            detail="outcomes whose latest value or revision differs from first release",
        ),
        _check(
            "challenger-mae-improvement",
            required=gates["min_challenger_relative_mae_improvement"],
            observed=joint_improvement,
            passed=(
                joint_improvement is not None
                and joint_improvement >= gates["min_challenger_relative_mae_improvement"]
            ),
            detail="minimum relative MAE improvement across first and latest outcomes",
        ),
        _check(
            "interval-folds",
            required=gates["min_interval_folds"],
            observed=interval_count,
            passed=interval_count >= gates["min_interval_folds"],
            detail="candidate folds with central prediction intervals in both score views",
        ),
        _check(
            "interval-coverage",
            required={
                "minimum": gates["min_interval_coverage"],
                "maximum": gates["max_interval_coverage"],
            },
            observed=(
                {"first_release": coverages[0], "latest_revised": coverages[1]}
                if len(coverages) == 2 else None
            ),
            passed=(
                gates["min_interval_folds"] == 0
                or (
                    len(coverages) == 2
                    and all(
                        gates["min_interval_coverage"]
                        <= value
                        <= gates["max_interval_coverage"]
                        for value in coverages
                    )
                )
            ),
            detail=(
                "disabled because min_interval_folds is zero"
                if gates["min_interval_folds"] == 0
                else "empirical coverage in both outcome score views"
            ),
        ),
    ]
    passed = all(check["passed"] for check in checks)
    promotion = {
        "status": "passed" if passed else "failed",
        "baseline_model_id": baseline_model_id,
        "candidate_model_id": candidate["model_id"] if candidate else None,
        "champion_model_id": candidate["model_id"] if passed and candidate else None,
        "first_release_relative_mae_improvement": first_improvement,
        "latest_revised_relative_mae_improvement": latest_improvement,
        "checks": checks,
        "abstention_reasons": [
            check["detail"] for check in checks if not check["passed"]
        ],
    }
    return promotion, candidate if passed else None


def _production_forecast(
    observations: Sequence[EconomicObservation],
    as_of: datetime,
    target: Mapping[str, object],
    model: Mapping[str, object],
    source_groups: Mapping[str, str],
) -> dict[str, object]:
    scope_series = {str(target["series_id"]), *target["bridge_contributor_series_ids"]}
    selected = observations_as_of(
        [
            row for row in observations
            if row.status == "observed" and row.series_id in scope_series
        ],
        as_of,
    )
    prediction = _model_prediction(model, target, selected, source_groups)
    if prediction["status"] != "ok":
        raise ForecastBuildError(prediction["failure"]["message"])
    target_rows = [row for row in selected if _target_match(row, target)]
    if not target_rows:
        raise ForecastBuildError("no target observation is knowable at publication time")
    last_period = max(row.period_end for row in target_rows)
    return {
        "as_of": _timestamp(as_of),
        "target_period": {
            "kind": TARGET_HORIZON,
            "after": last_period.isoformat(),
        },
        "model_id": model["model_id"],
        "point": prediction["point"],
        "lower": prediction["lower"],
        "upper": prediction["upper"],
        "interval_coverage": prediction["interval_coverage"],
        "unit": target["unit"],
        "contributor_series_ids": prediction["contributor_series_ids"],
        "source_ids": prediction["source_ids"],
        "independence_groups": prediction["independence_groups"],
        "feature_snapshot_sha256": snapshot_digest(selected),
    }


def _build_target(
    observations: Sequence[EconomicObservation],
    as_of: datetime,
    target: Mapping[str, object],
    all_models: Sequence[Mapping[str, object]],
    source_groups: Mapping[str, str],
    gates: Mapping[str, object],
    baseline_model_id: str,
) -> dict[str, object]:
    model_by_id = {str(model["model_id"]): model for model in all_models}
    models = [model_by_id[model_id] for model_id in target["model_ids"]]
    folds = _folds(observations, target, models, source_groups)
    summaries = _model_summaries(target, models, folds)
    promotion, champion = _promotion(
        target, summaries, folds, gates, baseline_model_id
    )
    nowcast = None
    if champion is not None:
        champion_spec = model_by_id[str(champion["model_id"])]
        try:
            nowcast = _production_forecast(
                observations, as_of, target, champion_spec, source_groups
            )
        except ForecastBuildError as exc:
            promotion["checks"].append(_check(
                "production-fit",
                required=True,
                observed=False,
                passed=False,
                detail=f"champion could not fit the publication snapshot: {exc}",
            ))
            promotion["status"] = "failed"
            promotion["champion_model_id"] = None
            promotion["abstention_reasons"].append(
                "champion could not fit the publication snapshot"
            )

    target_observations = [row for row in observations if _target_match(row, target)]
    source_ids = sorted({row.source_id for row in target_observations})
    return {
        "target_id": target["target_id"],
        "label": target["label"],
        "series_id": target["series_id"],
        "source_id": target["source_id"],
        "unit": target["unit"],
        "frequency": target["frequency"],
        "geography": target["geography"],
        "sector": target["sector"],
        "firm_size": target["firm_size"],
        "ownership": target["ownership"],
        "horizon": target["horizon"],
        "status": "ready" if nowcast is not None else "warming_up",
        "outcome_periods": len(folds),
        "observed_source_ids": source_ids,
        "observed_independence_groups": sorted(
            {source_groups[source_id] for source_id in source_ids}
        ),
        "models": summaries,
        "promotion": promotion,
        "nowcast": nowcast,
        "folds": folds,
    }


def build_forecast_document(
    *,
    ledger_path: str | Path,
    config_path: str | Path,
    source_registry_path: str | Path,
    ledger_artifact_path: str = "readings/china-econ-observations.jsonl",
) -> dict[str, object]:
    """Build the complete deterministic publication from frozen local inputs."""

    try:
        snapshot: LedgerSnapshot = load_snapshot(ledger_path)
    except (LedgerIntegrityError, OSError, ValueError, TypeError) as exc:
        raise ForecastBuildError(f"cannot load observation ledger: {exc}") from exc
    if not snapshot.observations or snapshot.as_of is None:
        raise ForecastBuildError("observation ledger is empty")
    config_raw, config_bytes = _load_json(config_path)
    config = validate_config(config_raw)
    registry, registry_bytes = _load_json(source_registry_path)
    source_groups = _registry_groups(registry)
    unknown_sources = {
        str(target["source_id"])
        for target in config["targets"]
        if target["enabled"] and target["source_id"] not in source_groups
    }
    if unknown_sources:
        raise ForecastBuildError(
            f"target source_ids absent from source registry: {sorted(unknown_sources)}"
        )

    enabled_targets = [target for target in config["targets"] if target["enabled"]]
    if not enabled_targets:
        raise ForecastBuildError("forecast config has no enabled targets")
    targets = [
        _build_target(
            snapshot.observations,
            snapshot.as_of,
            target,
            config["models"],
            source_groups,
            config["promotion_gates"],
            config["baseline_model_id"],
        )
        for target in enabled_targets
    ]
    ready = sum(target["status"] == "ready" for target in targets)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _timestamp(snapshot.as_of),
        "as_of": _timestamp(snapshot.as_of),
        "source": "Palimpsest bitemporal China economic observation ledger",
        "method": (
            "Deterministic pseudo-real-time expanding-window backtest with both "
            "release and collection clocks and frozen promotion gates."
        ),
        "n_targets": len(targets),
        "status": "ready" if ready == len(targets) else "warming_up",
        "scope": config["scope"],
        "claim": (
            "Named-series one-step forecasts only after frozen pseudo-real-time "
            "promotion gates pass; otherwise this artifact publishes backtest evidence "
            "and abstains."
        ),
        "configuration": {
            "path": "config/china_econ_targets.json",
            "sha256": hashlib.sha256(config_bytes).hexdigest(),
            "config_version": config["config_version"],
            "engine_version": ENGINE_VERSION,
            "baseline_model_id": config["baseline_model_id"],
            "promotion_gates": config["promotion_gates"],
        },
        "snapshot": {
            "path": ledger_artifact_path,
            "records": snapshot.records,
            "bytes": snapshot.byte_size,
            "sha256": snapshot.byte_sha256,
            "as_of": _timestamp(snapshot.as_of),
        },
        "source_registry": {
            "path": "config/china_econ_sources.json",
            "sha256": hashlib.sha256(registry_bytes).hexdigest(),
        },
        "summary": {
            "targets": len(targets),
            "ready_targets": ready,
            "abstaining_targets": len(targets) - ready,
            "champion_target_ids": [
                target["target_id"] for target in targets if target["nowcast"] is not None
            ],
        },
        "integrity": {
            "aggregate_only": True,
            "release_clock_enforced": True,
            "collection_clock_enforced": True,
            "target_period_lookahead_blocked": True,
            "first_and_latest_outcomes_scored_separately": True,
            "failed_model_specs_retained": True,
        },
        "targets": targets,
        "limitations": [
            "A target is one named observed series, not an economy-wide activity measure.",
            "Backfilled rows are unavailable before their release and collection clocks even when their economic period is old.",
            "First-release and latest-revised scores answer different questions and must not be substituted for one another.",
            "The equal-delta bridge is a transparent baseline, not evidence of a causal relationship.",
        ],
    }
