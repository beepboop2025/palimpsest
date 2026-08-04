"""Privacy-bounded estimates from aggregate China business-survey cells.

This module deliberately has no respondent-level API.  Its only input row is
``StratumCounts``: three response-category counts and that stratum's share of
the target population.  Exact-key mappings are accepted for safe JSON
ingestion, but a mapping containing a name, respondent identifier, free text,
or any other field is rejected before estimation.

The estimator is a transparent reference implementation, not a substitute for
a survey organization's sample frame.  It post-stratifies the aggregate cells,
caps extreme adjustment factors, and reports the remaining coverage and weight
imbalance instead of pretending that missing strata were observed.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Final


_ROW_FIELDS: Final[frozenset[str]] = frozenset(
    {"up", "same", "down", "population_target_share"}
)
_Z_95: Final[float] = 1.959963984540054
_SHARE_TOLERANCE: Final[float] = 1e-12
_PRIVACY_FLOOR: Final[int] = 5


class SurveyInputError(ValueError):
    """Raised when data violate the aggregate-only survey contract."""


@dataclass(frozen=True, slots=True)
class StratumCounts:
    """One anonymous stratum represented only by aggregate response counts.

    ``population_target_share`` is an absolute share on the 0--1 scale.  The
    shares supplied across all rows may total less than one; that shortfall is
    treated as uncovered population and can cause the estimator to abstain.
    No stratum label is accepted so that a person or business identifier cannot
    be smuggled into downstream diagnostics.
    """

    up: int
    same: int
    down: int
    population_target_share: float

    def __post_init__(self) -> None:
        for field_name in ("up", "same", "down"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, Integral):
                raise SurveyInputError(f"{field_name} must be a non-negative integer count")
            if value < 0:
                raise SurveyInputError(f"{field_name} must be a non-negative integer count")
            # Normalize non-builtin Integral implementations without accepting
            # floating-point values such as 3.0 as counts.
            object.__setattr__(self, field_name, int(value))

        share = self.population_target_share
        if isinstance(share, bool) or not isinstance(share, Real):
            raise SurveyInputError("population_target_share must be a finite number")
        try:
            share = float(share)
        except (OverflowError, ValueError) as exc:
            raise SurveyInputError(
                "population_target_share must be a finite number"
            ) from exc
        if not math.isfinite(share) or not 0.0 <= share <= 1.0:
            raise SurveyInputError("population_target_share must lie in [0, 1]")
        object.__setattr__(self, "population_target_share", share)

    @property
    def sample_size(self) -> int:
        """Number of responses represented by this aggregate row."""

        return self.up + self.same + self.down

    @classmethod
    def from_mapping(cls, row: Mapping[str, object]) -> StratumCounts:
        """Validate and convert an exact four-field aggregate mapping.

        Exact-key validation is an intentional privacy boundary.  In
        particular, mappings with respondent IDs, names, raw response arrays,
        timestamps, or free text are not silently ignored.
        """

        keys = frozenset(row.keys())
        if keys != _ROW_FIELDS:
            missing = sorted(_ROW_FIELDS - keys)
            extra = sorted(str(key) for key in keys - _ROW_FIELDS)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("disallowed " + ", ".join(extra))
            raise SurveyInputError(
                "aggregate row must contain exactly up, same, down, and "
                "population_target_share (" + "; ".join(details) + ")"
            )
        return cls(
            up=row["up"],  # type: ignore[arg-type]
            same=row["same"],  # type: ignore[arg-type]
            down=row["down"],  # type: ignore[arg-type]
            population_target_share=row["population_target_share"],  # type: ignore[arg-type]
        )


def _validate_configuration(
    min_cell_count: int,
    max_poststrat_weight: float,
    min_population_coverage: float,
) -> tuple[int, float, float]:
    if isinstance(min_cell_count, bool) or not isinstance(min_cell_count, Integral):
        raise SurveyInputError(
            f"min_cell_count must be an integer of at least {_PRIVACY_FLOOR}"
        )
    min_cell_count = int(min_cell_count)
    if min_cell_count < _PRIVACY_FLOOR:
        raise SurveyInputError(
            f"min_cell_count must be an integer of at least {_PRIVACY_FLOOR}"
        )

    if isinstance(max_poststrat_weight, bool) or not isinstance(
        max_poststrat_weight, Real
    ):
        raise SurveyInputError("max_poststrat_weight must be finite and at least 1")
    try:
        max_poststrat_weight = float(max_poststrat_weight)
    except (OverflowError, ValueError) as exc:
        raise SurveyInputError(
            "max_poststrat_weight must be finite and at least 1"
        ) from exc
    if not math.isfinite(max_poststrat_weight) or max_poststrat_weight < 1.0:
        raise SurveyInputError("max_poststrat_weight must be finite and at least 1")

    if isinstance(min_population_coverage, bool) or not isinstance(
        min_population_coverage, Real
    ):
        raise SurveyInputError("min_population_coverage must lie in [0, 1]")
    try:
        min_population_coverage = float(min_population_coverage)
    except (OverflowError, ValueError) as exc:
        raise SurveyInputError(
            "min_population_coverage must lie in [0, 1]"
        ) from exc
    if (
        not math.isfinite(min_population_coverage)
        or not 0.0 <= min_population_coverage <= 1.0
    ):
        raise SurveyInputError("min_population_coverage must lie in [0, 1]")
    return min_cell_count, max_poststrat_weight, min_population_coverage


def _coerce_rows(
    strata: Iterable[StratumCounts | Mapping[str, object]],
) -> list[StratumCounts]:
    # A single mapping is a common accidental respondent-row call.  Reject it
    # instead of iterating over its field names.
    if isinstance(strata, Mapping) or isinstance(strata, (str, bytes)):
        raise SurveyInputError("strata must be an iterable of aggregate rows")
    try:
        raw_rows = list(strata)
    except TypeError as exc:
        raise SurveyInputError("strata must be an iterable of aggregate rows") from exc

    rows = []
    for index, row in enumerate(raw_rows):
        try:
            if isinstance(row, StratumCounts):
                rows.append(row)
            elif isinstance(row, Mapping):
                rows.append(StratumCounts.from_mapping(row))
            else:
                raise SurveyInputError(
                    "row must be StratumCounts or an exact aggregate mapping"
                )
        except SurveyInputError as exc:
            raise SurveyInputError(f"strata[{index}]: {exc}") from exc
    return rows


def _coverage_diagnostics(
    *,
    rows: list[StratumCounts],
    used: list[StratumCounts],
    suppressed: list[StratumCounts],
    zero_target: list[StratumCounts],
    min_cell_count: int,
    min_population_coverage: float,
) -> dict[str, object]:
    provided_share_raw = math.fsum(row.population_target_share for row in rows)
    used_share_raw = math.fsum(row.population_target_share for row in used)
    suppressed_share_raw = math.fsum(
        row.population_target_share for row in suppressed
    )
    # A tolerance-sized floating overshoot should not produce a coverage above
    # 100%.  It does not affect relative post-stratification weights.
    provided_share = min(1.0, provided_share_raw)
    used_share = min(1.0, used_share_raw)
    suppressed_share = min(1.0, suppressed_share_raw)
    return {
        "population_target_share_provided": provided_share,
        "population_target_share_used": used_share,
        "population_target_share_suppressed": suppressed_share,
        "population_target_share_unprovided": max(0.0, 1.0 - provided_share),
        "population_coverage": used_share,
        "minimum_population_coverage": min_population_coverage,
        "strata_received": len(rows),
        "strata_used": len(used),
        "strata_suppressed": len(suppressed),
        "strata_zero_target_excluded": len(zero_target),
        "eligible_sample_size": sum(row.sample_size for row in used),
        "minimum_cell_count": min_cell_count,
        "suppression_rule": (
            "suppress the whole stratum when it has no responses or any of "
            "up/same/down is below minimum_cell_count"
        ),
    }


def _abstain(reason: str, coverage: dict[str, object]) -> dict[str, object]:
    return {
        "status": "abstain",
        "reason": reason,
        "coverage": coverage,
        "method": "aggregate-only capped post-stratified diffusion index",
    }


def _capped_calibration(
    target_shares: list[float],
    sample_shares: list[float],
    cap: float,
) -> tuple[list[float], set[int]]:
    """Return achieved stratum shares with normalized weights bounded by cap.

    For stratum ``h``, the effective adjustment factor is
    ``achieved_share[h] / sample_share[h]``.  Starting from target shares, this
    water-filling algorithm fixes over-cap strata at ``cap * sample_share`` and
    redistributes their excess proportionally across the remaining targets.  It
    therefore preserves a weighted mean factor of one without the common error
    of normalizing winsorized weights back above their advertised cap.
    """

    achieved = [0.0] * len(target_shares)
    remaining = list(range(len(target_shares)))
    remaining_mass = 1.0
    trimmed: set[int] = set()

    while remaining:
        remaining_target = math.fsum(target_shares[index] for index in remaining)
        if remaining_target <= 0.0:  # defensive: public caller requires positive shares
            raise SurveyInputError("capped calibration has no positive target mass")
        scale = remaining_mass / remaining_target
        over_cap = [
            index
            for index in remaining
            if scale * target_shares[index] / sample_shares[index]
            > cap + _SHARE_TOLERANCE
        ]
        if not over_cap:
            for index in remaining:
                achieved[index] = scale * target_shares[index]
            break

        fixed_mass = math.fsum(
            cap * sample_shares[index] for index in over_cap
        )
        for index in over_cap:
            achieved[index] = cap * sample_shares[index]
            trimmed.add(index)
        remaining_mass = max(0.0, remaining_mass - fixed_mass)
        fixed = set(over_cap)
        remaining = [index for index in remaining if index not in fixed]

    # Repair only floating-point residue, never substantive infeasibility.  A
    # cap >= 1 guarantees total capacity because sample shares sum to one.
    for _ in range(2):
        residual = 1.0 - math.fsum(achieved)
        if residual == 0.0:
            break
        if residual > 0.0:
            candidates = [
                (cap * sample_shares[index] - achieved[index], index)
                for index in range(len(achieved))
            ]
            headroom, index = max(candidates)
            if headroom + _SHARE_TOLERANCE < residual:
                raise SurveyInputError("post-stratification cap is infeasible")
            achieved[index] += residual
        else:
            index = max(range(len(achieved)), key=achieved.__getitem__)
            if achieved[index] + residual < -_SHARE_TOLERANCE:
                raise SurveyInputError("post-stratification calibration failed")
            achieved[index] += residual

    maximum_factor = max(
        achieved_share / sample_share
        for achieved_share, sample_share in zip(achieved, sample_shares)
    )
    if maximum_factor > cap + _SHARE_TOLERANCE:
        raise SurveyInputError("post-stratification calibration exceeded its cap")
    return achieved, trimmed


def estimate_diffusion_index(
    strata: Iterable[StratumCounts | Mapping[str, object]],
    *,
    min_cell_count: int = 5,
    max_poststrat_weight: float = 4.0,
    min_population_coverage: float = 0.8,
) -> dict[str, object]:
    """Estimate an aggregate-only, post-stratified diffusion index.

    The index is ``50 + 50 * (p_up - p_down)``.  Each stratum receives its
    target-population share divided by its observed sample share.  If an
    adjustment exceeds ``max_poststrat_weight``, bounded calibration trims it
    and redistributes target mass across strata that still have headroom.  The
    final normalized adjustment factors therefore have sample-weighted mean one
    and none exceeds the stated cap.

    Standard error combines the plug-in multinomial variance of the
    up-minus-down contrast separately within each stratum, using the achieved
    stratum shares after capping.  This is an approximation: without respondent
    rows or design metadata it cannot estimate clustering, finite-population
    corrections, nonresponse bias, or replicate-weight variance.  The 95%
    normal interval is clipped to the index's 0--100 range.

    Invalid schemas and impossible values raise ``SurveyInputError``.  Valid
    aggregate data that are too sparse or cover too little of the target
    population return ``status='abstain'`` with diagnostics and no estimate.
    """

    (
        min_cell_count,
        max_poststrat_weight,
        min_population_coverage,
    ) = _validate_configuration(
        min_cell_count,
        max_poststrat_weight,
        min_population_coverage,
    )
    rows = _coerce_rows(strata)
    if not rows:
        coverage = _coverage_diagnostics(
            rows=[],
            used=[],
            suppressed=[],
            zero_target=[],
            min_cell_count=min_cell_count,
            min_population_coverage=min_population_coverage,
        )
        return _abstain("no aggregate strata supplied", coverage)

    provided_share = math.fsum(row.population_target_share for row in rows)
    if provided_share > 1.0 + _SHARE_TOLERANCE:
        raise SurveyInputError(
            "population_target_share values must sum to no more than 1"
        )

    used: list[StratumCounts] = []
    suppressed: list[StratumCounts] = []
    zero_target: list[StratumCounts] = []
    for row in rows:
        below_minimum = any(
            count < min_cell_count for count in (row.up, row.same, row.down)
        )
        if row.sample_size == 0 or below_minimum:
            suppressed.append(row)
        elif row.population_target_share == 0.0:
            zero_target.append(row)
        else:
            used.append(row)

    coverage = _coverage_diagnostics(
        rows=rows,
        used=used,
        suppressed=suppressed,
        zero_target=zero_target,
        min_cell_count=min_cell_count,
        min_population_coverage=min_population_coverage,
    )
    if not used:
        return _abstain(
            "no privacy-eligible stratum has positive population target share",
            coverage,
        )

    used_population_share = math.fsum(
        row.population_target_share for row in used
    )
    if used_population_share + _SHARE_TOLERANCE < min_population_coverage:
        return _abstain(
            "population coverage after suppression is below the configured minimum",
            coverage,
        )

    sample_size = sum(row.sample_size for row in used)
    try:
        sample_size_float = float(sample_size)
    except OverflowError as exc:
        raise SurveyInputError(
            "aggregate sample size is too large for the finite-precision estimator"
        ) from exc
    if not math.isfinite(sample_size_float):
        raise SurveyInputError(
            "aggregate sample size is too large for the finite-precision estimator"
        )
    target_shares = [
        row.population_target_share / used_population_share for row in used
    ]
    sample_shares = [row.sample_size / sample_size_float for row in used]
    raw_weights = [
        target_share / sample_share
        for target_share, sample_share in zip(target_shares, sample_shares)
    ]
    achieved_shares, trimmed_indices = _capped_calibration(
        target_shares,
        sample_shares,
        max_poststrat_weight,
    )
    effective_weights = [
        achieved_share / sample_share
        for achieved_share, sample_share in zip(achieved_shares, sample_shares)
    ]
    # Report an equality-to-cap result exactly at the configured cap rather
    # than a possible one-ulp division overshoot already accepted above.
    maximum_effective_weight = min(
        max_poststrat_weight,
        max(effective_weights),
    )

    # Each conceptual respondent in stratum h has normalized weight A_h / n_h.
    # Kish n can therefore be computed exactly from aggregates alone.
    sum_squared_normalized_weights = math.fsum(
        achieved_share * achieved_share / row.sample_size
        for row, achieved_share in zip(used, achieved_shares)
    )
    if sum_squared_normalized_weights <= 0.0:  # defensive only
        return _abstain("post-stratification produced no positive weight", coverage)
    kish_effective_n = 1.0 / sum_squared_normalized_weights
    # Round-off can put Kish n a few ulps above the number of positive-weight
    # observations even though the Cauchy-Schwarz bound says it cannot be.
    kish_effective_n = min(sample_size_float, kish_effective_n)
    if kish_effective_n <= 1.0 + _SHARE_TOLERANCE:
        return _abstain(
            "Kish effective sample size is not greater than one",
            coverage,
        )

    p_up = math.fsum(
        achieved_share * row.up / row.sample_size
        for row, achieved_share in zip(used, achieved_shares)
    )
    p_same = math.fsum(
        achieved_share * row.same / row.sample_size
        for row, achieved_share in zip(used, achieved_shares)
    )
    p_down = math.fsum(
        achieved_share * row.down / row.sample_size
        for row, achieved_share in zip(used, achieved_shares)
    )

    contrast = p_up - p_down
    diffusion_index = min(100.0, max(0.0, 50.0 + 50.0 * contrast))
    stratum_contrast_variance = []
    for row in used:
        stratum_p_up = row.up / row.sample_size
        stratum_p_down = row.down / row.sample_size
        stratum_contrast = stratum_p_up - stratum_p_down
        stratum_contrast_variance.append(
            max(
                0.0,
                stratum_p_up
                + stratum_p_down
                - stratum_contrast * stratum_contrast,
            )
        )
    mean_contrast_variance = math.fsum(
        achieved_share * achieved_share * within_variance / row.sample_size
        for row, achieved_share, within_variance in zip(
            used,
            achieved_shares,
            stratum_contrast_variance,
        )
    )
    standard_error = 50.0 * math.sqrt(mean_contrast_variance)
    ci_low = max(0.0, diffusion_index - _Z_95 * standard_error)
    ci_high = min(100.0, diffusion_index + _Z_95 * standard_error)

    total_variation_gap = 0.5 * math.fsum(
        abs(achieved - target)
        for achieved, target in zip(achieved_shares, target_shares)
    )
    trimmed_rows = [used[index] for index in sorted(trimmed_indices)]

    return {
        "status": "ok",
        "diffusion_index": diffusion_index,
        "weighted_proportions": {
            "up": p_up,
            "same": p_same,
            "down": p_down,
        },
        "standard_error": standard_error,
        "confidence_interval_95": {"low": ci_low, "high": ci_high},
        "kish_effective_sample_size": kish_effective_n,
        "weighting_design_effect": sample_size / kish_effective_n,
        "coverage": coverage,
        "weights": {
            "maximum_allowed": max_poststrat_weight,
            "maximum_raw": max(raw_weights),
            "maximum_used": maximum_effective_weight,
            "sample_weighted_mean_used": math.fsum(
                sample_share * weight
                for sample_share, weight in zip(sample_shares, effective_weights)
            ),
            "strata_capped": len(trimmed_rows),
            "population_target_share_capped": math.fsum(
                row.population_target_share for row in trimmed_rows
            ),
            "target_total_variation_gap_after_cap": total_variation_gap,
        },
        "method": {
            "estimator": "aggregate-only capped post-stratified diffusion index",
            "formula": "50 + 50 * (weighted_p_up - weighted_p_down)",
            "standard_error": (
                "sum over strata of achieved_share^2 times plug-in within-stratum "
                "up-minus-down variance divided by stratum sample size"
            ),
            "weight_cap": (
                "bounded proportional calibration; normalized adjustment factors "
                "have sample-weighted mean one and do not exceed the cap"
            ),
            "confidence_interval": "95% normal approximation clipped to [0, 100]",
            "limitations": (
                "aggregate counts cannot identify clustering, finite-population "
                "corrections, nonresponse bias, or replicate-weight variance"
            ),
        },
    }


__all__ = ["StratumCounts", "SurveyInputError", "estimate_diffusion_index"]
