"""Deterministic, dependency-free baselines and forecast scoring.

The functions in this module intentionally know nothing about China, release
calendars, or publication.  They operate on already-selected numeric histories.
The processor is responsible for enforcing the bitemporal and target-period
cutoffs before values cross this boundary.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from numbers import Integral, Real
from statistics import NormalDist
from typing import Any, Mapping, Sequence


ENGINE_VERSION = "palimpsest-economic-forecast-engine.v1"
MODEL_KINDS = frozenset({
    "random_walk",
    "seasonal_naive",
    "mean_delta",
    "equal_delta_bridge",
})


class ForecastModelError(ValueError):
    """A model cannot produce an honest forecast from the supplied history."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json_bytes(value: object, *, pretty: bool = False) -> bytes:
    """Stable JSON encoding used for model, fold, and artifact identities."""

    options: dict[str, Any] = {
        "allow_nan": False,
        "ensure_ascii": False,
        "sort_keys": True,
    }
    if pretty:
        options["indent"] = 1
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + ("\n" if pretty else "")).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ForecastModelError("invalid_numeric_input", f"{name} must be a real number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ForecastModelError("invalid_numeric_input", f"{name} must be finite")
    return normalized


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) <= 0:
        raise ForecastModelError("invalid_model_spec", f"{name} must be a positive integer")
    return int(value)


def _history(values: Sequence[Real]) -> list[float]:
    if isinstance(values, (str, bytes)):
        raise ForecastModelError("invalid_history", "history must be a numeric sequence")
    return [_finite(value, f"history[{index}]") for index, value in enumerate(values)]


def _interval(
    point: float,
    residuals: Sequence[float],
    coverage: float | None,
    minimum_residuals: int,
) -> tuple[float | None, float | None, float | None, str | None]:
    if coverage is None:
        return None, None, None, None
    coverage = _finite(coverage, "interval_coverage")
    if not 0.0 < coverage < 1.0:
        raise ForecastModelError(
            "invalid_model_spec", "interval_coverage must lie strictly between zero and one"
        )
    if len(residuals) < minimum_residuals:
        return None, None, coverage, "insufficient_training_residuals"
    # A zero residual scale is meaningful for deterministic fixtures and is
    # represented as a zero-width interval rather than artificial uncertainty.
    scale = statistics.stdev(residuals) if len(residuals) > 1 else 0.0
    z_value = NormalDist().inv_cdf((1.0 + coverage) / 2.0)
    half_width = z_value * scale
    return (
        point - half_width,
        point + half_width,
        coverage,
        "normal_interval_from_training_residual_standard_deviation",
    )


def forecast_values(
    spec: Mapping[str, object],
    history: Sequence[Real],
    *,
    contributor_histories: Mapping[str, Sequence[Real]] | None = None,
) -> dict[str, object]:
    """Fit one transparent one-step baseline and return its prediction.

    ``history`` and each contributor history must already be sorted by economic
    period and point-in-time selected.  The function never fills gaps or reaches
    back into the observation ledger.
    """

    if not isinstance(spec, Mapping):
        raise ForecastModelError("invalid_model_spec", "model spec must be an object")
    kind = spec.get("kind")
    if kind not in MODEL_KINDS:
        raise ForecastModelError("invalid_model_spec", f"unsupported model kind {kind!r}")
    minimum = _positive_int(spec.get("min_train_observations"), "min_train_observations")
    values = _history(history)
    if len(values) < minimum:
        raise ForecastModelError(
            "insufficient_training_history",
            f"requires {minimum} target observations; found {len(values)}",
        )

    interval_coverage = spec.get("interval_coverage")
    if interval_coverage is not None:
        interval_coverage = _finite(interval_coverage, "interval_coverage")
    minimum_residuals = _positive_int(
        spec.get("min_interval_residuals"), "min_interval_residuals"
    )
    origin = values[-1]
    residuals: list[float] = []
    contributors_used: list[str] = []

    if kind == "random_walk":
        point = origin
        residuals = [current - previous for previous, current in zip(values, values[1:])]
        method = "last knowable target value"
    elif kind == "seasonal_naive":
        lag = _positive_int(spec.get("seasonal_lag"), "seasonal_lag")
        if len(values) <= lag:
            raise ForecastModelError(
                "insufficient_seasonal_history",
                f"requires more than {lag} target observations; found {len(values)}",
            )
        point = values[-lag]
        residuals = [values[index] - values[index - lag] for index in range(lag, len(values))]
        method = f"target value {lag} observed periods earlier"
    elif kind == "mean_delta":
        lookback = _positive_int(spec.get("delta_lookback"), "delta_lookback")
        if len(values) < 2:
            raise ForecastModelError(
                "insufficient_delta_history", "mean-delta requires at least two target values"
            )
        deltas = [current - previous for previous, current in zip(values, values[1:])]
        point = origin + statistics.fmean(deltas[-lookback:])
        for index in range(2, len(values)):
            prior_deltas = [
                values[position] - values[position - 1]
                for position in range(1, index)
            ]
            fitted = values[index - 1] + statistics.fmean(prior_deltas[-lookback:])
            residuals.append(values[index] - fitted)
        method = f"last target value plus mean of up to {lookback} prior target changes"
    else:
        histories = contributor_histories or {}
        minimum_contributors = _positive_int(
            spec.get("min_bridge_contributors"), "min_bridge_contributors"
        )
        deltas = []
        for series_id in sorted(histories):
            contributor_values = _history(histories[series_id])
            if len(contributor_values) < 2:
                continue
            deltas.append(contributor_values[-1] - contributor_values[-2])
            contributors_used.append(series_id)
        if len(deltas) < minimum_contributors:
            raise ForecastModelError(
                "insufficient_bridge_contributors",
                f"requires {minimum_contributors} contributors with two observations; "
                f"found {len(deltas)}",
            )
        point = origin + statistics.fmean(deltas)
        # A bridge interval needs historical, release-aligned contributor panels.
        # Until those are explicitly fit, publishing no interval is safer than
        # reusing the target-only residual scale under a different model.
        interval_coverage = None
        method = "last target value plus equal-weight mean latest contributor change"

    lower, upper, nominal, interval_method = _interval(
        point, residuals, interval_coverage, minimum_residuals
    )
    return {
        # CPython and libm can differ below the meaningful precision of these
        # baseline calculations. Quantize computed predictions at the public
        # model boundary so Linux CI, macOS publication, and offline rebuilds
        # produce the same JSON bytes and hashes.
        "point": _rounded(point),
        "lower": _rounded(lower) if lower is not None else None,
        "upper": _rounded(upper) if upper is not None else None,
        "interval_coverage": nominal,
        "interval_method": interval_method,
        "origin_value": origin,
        "training_observations": len(values),
        "training_residuals": len(residuals),
        "contributors_used": contributors_used,
        "method": method,
    }


def _sign(value: float, *, tolerance: float = 1e-12) -> int:
    if value > tolerance:
        return 1
    if value < -tolerance:
        return -1
    return 0


def _weighted_interval_score(
    actual: float,
    point: float,
    lower: float,
    upper: float,
    coverage: float,
) -> float:
    """WIS for a median and one central prediction interval."""

    alpha = 1.0 - coverage
    interval_score = upper - lower
    if actual < lower:
        interval_score += (2.0 / alpha) * (lower - actual)
    elif actual > upper:
        interval_score += (2.0 / alpha) * (actual - upper)
    return (
        0.5 * abs(actual - point) + (alpha / 2.0) * interval_score
    ) / (0.5 + alpha / 2.0)


def _rounded(value: float) -> float:
    # Twelve decimal places suppress platform-level floating noise without
    # implying that the underlying economic observations are this precise.
    return round(float(value), 12)


def score_predictions(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Score point/direction/interval accuracy for one outcome vintage."""

    errors: list[float] = []
    directional: list[bool] = []
    interval_hits: list[bool] = []
    interval_scores: list[float] = []
    nominal_coverages: list[float] = []
    for index, record in enumerate(records):
        actual = _finite(record.get("actual"), f"records[{index}].actual")
        point = _finite(record.get("point"), f"records[{index}].point")
        origin = _finite(record.get("origin"), f"records[{index}].origin")
        errors.append(actual - point)
        directional.append(_sign(point - origin) == _sign(actual - origin))
        lower, upper, coverage = (
            record.get("lower"),
            record.get("upper"),
            record.get("interval_coverage"),
        )
        if lower is None or upper is None or coverage is None:
            continue
        lower_value = _finite(lower, f"records[{index}].lower")
        upper_value = _finite(upper, f"records[{index}].upper")
        coverage_value = _finite(coverage, f"records[{index}].interval_coverage")
        if lower_value > upper_value or not 0.0 < coverage_value < 1.0:
            raise ForecastModelError("invalid_prediction_interval", "invalid interval bounds")
        interval_hits.append(lower_value <= actual <= upper_value)
        interval_scores.append(
            _weighted_interval_score(
                actual, point, lower_value, upper_value, coverage_value
            )
        )
        nominal_coverages.append(coverage_value)

    if not errors:
        return {
            "n_scored": 0,
            "mae": None,
            "median_absolute_error": None,
            "rmse": None,
            "directional_accuracy": None,
            "n_directional": 0,
            "intervals": {
                "n_scored": 0,
                "nominal_coverage": None,
                "empirical_coverage": None,
                "mean_wis": None,
            },
        }

    absolute = [abs(error) for error in errors]
    intervals = {
        "n_scored": len(interval_hits),
        "nominal_coverage": (
            _rounded(statistics.fmean(nominal_coverages)) if nominal_coverages else None
        ),
        "empirical_coverage": (
            _rounded(sum(interval_hits) / len(interval_hits)) if interval_hits else None
        ),
        "mean_wis": (
            _rounded(statistics.fmean(interval_scores)) if interval_scores else None
        ),
    }
    return {
        "n_scored": len(errors),
        "mae": _rounded(statistics.fmean(absolute)),
        "median_absolute_error": _rounded(statistics.median(absolute)),
        "rmse": _rounded(math.sqrt(statistics.fmean(error * error for error in errors))),
        "directional_accuracy": _rounded(sum(directional) / len(directional)),
        "n_directional": len(directional),
        "intervals": intervals,
    }


def failure_counts(failures: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Stable compact representation of fold-level failures."""

    counts = Counter(str(failure.get("code", "unknown")) for failure in failures)
    return [{"code": code, "count": count} for code, count in sorted(counts.items())]
