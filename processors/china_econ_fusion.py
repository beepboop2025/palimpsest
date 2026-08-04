"""Small, auditable baselines for correlated-source fusion and ragged edges.

These are deliberately conservative reference engines.  Production challengers
(statsmodels DynamicFactorMQ, MIDAS, Bayesian MF-VAR) must beat them on frozen
historical vintages.  The key safety property here is structural: duplicated
official numbers and their multilateral mirrors cannot masquerade as independent
confirmations merely because they arrived through different URLs.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from numbers import Integral, Real
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SignalEstimate:
    name: str
    value: float
    standard_error: float
    independence_group: str
    quality: float = 1.0

    def __post_init__(self) -> None:
        if not self.name or not self.independence_group:
            raise ValueError("name and independence_group are required")
        if not math.isfinite(self.value):
            raise ValueError("value must be finite")
        if not math.isfinite(self.standard_error) or self.standard_error <= 0:
            raise ValueError("standard_error must be positive and finite")
        if not 0.0 < self.quality <= 1.0:
            raise ValueError("quality must lie in (0, 1]")


def fuse_independent_groups(estimates: Iterable[SignalEstimate]) -> dict:
    """Fuse one canonical estimate from each independent information family.

    The member with the greatest quality-adjusted precision represents its
    ``independence_group``.  Ties are resolved by name and then numeric fields,
    so input order and duplicate multiplicity cannot move the result.  Other
    transports are diagnostic-only: their names and disagreement from the
    canonical member are reported, but they add neither location nor precision.
    """
    rows = list(estimates)
    if not rows:
        raise ValueError("at least one estimate is required")
    grouped: dict[str, list[SignalEstimate]] = defaultdict(list)
    for row in rows:
        grouped[row.independence_group].append(row)

    group_rows = []
    for name, members in sorted(grouped.items()):
        ordered = sorted(
            members,
            key=lambda m: (
                -((m.quality / m.standard_error) ** 2),
                m.name,
                m.value,
                m.standard_error,
                m.quality,
            ),
        )
        canonical = ordered[0]
        precision = (canonical.quality / canonical.standard_error) ** 2
        canonical_se = canonical.standard_error / canonical.quality
        ignored = ordered[1:]
        within_group = []
        for member in ignored:
            member_se = member.standard_error / member.quality
            difference = member.value - canonical.value
            within_group.append({
                "name": member.name,
                "difference_from_canonical": difference,
                "disagreement_z": abs(difference) / math.hypot(canonical_se, member_se),
            })
        group_rows.append({
            "group": name,
            "mean": canonical.value,
            "precision": precision,
            "canonical_member": canonical.name,
            "members": [m.name for m in ordered],
            "ignored_duplicate_members": [m.name for m in ignored],
            "within_group_disagreement": within_group,
            "max_within_group_disagreement_z": max(
                (row["disagreement_z"] for row in within_group), default=0.0
            ),
        })

    total_precision = sum(g["precision"] for g in group_rows)
    weights = [g["precision"] / total_precision for g in group_rows]
    mean = sum(w * g["mean"] for w, g in zip(weights, group_rows))
    variance = 1.0 / total_precision
    disagreement = sum(
        g["precision"] * (g["mean"] - mean) ** 2 for g in group_rows
    )
    n_groups = len(group_rows)
    effective = 1.0 / sum(w * w for w in weights)
    return {
        "mean": mean,
        "standard_error": math.sqrt(variance),
        "n_inputs": len(rows),
        "n_ignored_duplicate_members": len(rows) - n_groups,
        "n_independent_groups": n_groups,
        "effective_independent_groups": effective,
        "disagreement_z": math.sqrt(disagreement / max(1, n_groups - 1)),
        "groups": [
            {**g, "weight": w, "standard_error": 1.0 / math.sqrt(g["precision"])}
            for g, w in zip(group_rows, weights)
        ],
    }


@dataclass(frozen=True, slots=True)
class RaggedRelease:
    """One standardized indicator release available to a latent-state filter.

    ``state_step`` is the target-period ordinal (for example ``year*12+month``),
    not the release order.  Missing months are therefore explicit state gaps.
    """

    name: str
    state_step: int
    released_at: datetime
    collected_at: datetime
    value: float
    measurement_variance: float
    loading: float = 1.0
    quality: float = 1.0
    revision: int = 0

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("name is required and must be a string")
        if isinstance(self.state_step, bool) or not isinstance(self.state_step, Integral):
            raise TypeError("state_step must be an integer (not bool)")
        if isinstance(self.revision, bool) or not isinstance(self.revision, Integral):
            raise TypeError("revision must be an integer (not bool)")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        for field_name in ("released_at", "collected_at"):
            timestamp = getattr(self, field_name)
            if type(timestamp) is not datetime:
                raise TypeError(f"{field_name} must be a datetime")
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.collected_at < self.released_at:
            raise ValueError("collected_at cannot precede released_at")
        for field_name in ("value", "measurement_variance", "loading", "quality"):
            field_value = getattr(self, field_name)
            if isinstance(field_value, bool) or not isinstance(field_value, Real):
                raise TypeError(f"{field_name} must be a real number (not bool)")
            if not math.isfinite(float(field_value)):
                raise ValueError(f"{field_name} must be finite")
            object.__setattr__(self, field_name, float(field_value))
        if self.measurement_variance <= 0:
            raise ValueError("measurement_variance must be positive and finite")
        if not 0.0 < self.quality <= 1.0:
            raise ValueError("quality must lie in (0, 1]")
        object.__setattr__(self, "state_step", int(self.state_step))
        object.__setattr__(self, "revision", int(self.revision))

    @property
    def vintage_key(self) -> tuple[str, int]:
        """Indicator/target-period key whose releases supersede each other."""
        return self.name, self.state_step


class RaggedEdgeKalman:
    """Scalar dynamic-factor baseline with native missing/release-lag support.

    x_t = phi*x_(t-1) + eta_t
    y_i,t = loading_i*x_t + epsilon_i,t

    The indicator values must be standardized on the TRAINING vintage only.
    The class does not estimate loadings or variances: those are model
    parameters, fit inside each rolling-origin fold.  Its job is the auditable
    fixed-as-of target-time filtering and update decomposition.
    """

    def __init__(self, *, phi: float = 0.85, process_variance: float = 0.15,
                 initial_mean: float = 0.0, initial_variance: float = 1.0) -> None:
        params = {
            "phi": phi,
            "process_variance": process_variance,
            "initial_mean": initial_mean,
            "initial_variance": initial_variance,
        }
        for name, value in params.items():
            if isinstance(value, bool) or not isinstance(value, Real):
                raise TypeError(f"{name} must be a real number (not bool)")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if not -0.999 <= float(phi) <= 0.999:
            raise ValueError("phi must lie in [-0.999, 0.999]")
        if float(process_variance) <= 0 or float(initial_variance) <= 0:
            raise ValueError("variances must be positive")
        self.phi = float(phi)
        self.q = float(process_variance)
        self.initial_mean = float(initial_mean)
        self.initial_variance = float(initial_variance)

    def _predict(self, mean: float, variance: float, steps: int) -> tuple[float, float]:
        for _ in range(max(0, steps)):
            mean = self.phi * mean
            variance = self.phi * self.phi * variance + self.q
        return mean, variance

    def run(self, releases: Iterable[RaggedRelease], *, as_of: datetime) -> dict:
        if type(as_of) is not datetime:
            raise TypeError("as_of must be a datetime")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        all_rows = list(releases)
        knowable = [
            row for row in all_rows
            if row.released_at <= as_of and row.collected_at <= as_of
        ]
        latest = {}
        for row in knowable:
            previous = latest.get(row.vintage_key)
            if previous is None or (
                row.released_at, row.collected_at, row.revision,
                row.value, row.measurement_variance, row.loading, row.quality,
            ) > (
                previous.released_at, previous.collected_at, previous.revision,
                previous.value, previous.measurement_variance,
                previous.loading, previous.quality,
            ):
                latest[row.vintage_key] = row
        rows = sorted(latest.values(), key=lambda r: (r.state_step, r.name))
        diagnostics = {
            "n_input_rows": len(all_rows),
            "n_knowable_rows": len(knowable),
            "n_selected_vintages": len(rows),
            "n_superseded_vintages": len(knowable) - len(rows),
            "n_excluded_release_after_as_of": sum(
                row.released_at > as_of for row in all_rows
            ),
            "n_excluded_collection_after_as_of": sum(
                row.collected_at > as_of for row in all_rows
            ),
            "selected_revisions": [
                {"name": row.name, "state_step": row.state_step, "revision": row.revision}
                for row in rows
            ],
        }
        if not rows:
            return {
                "status": "abstain",
                "reason": "no release knowable as of cutoff",
                "as_of": as_of.isoformat(),
                "selection_diagnostics": diagnostics,
            }

        mean, variance = self.initial_mean, self.initial_variance
        current_step = rows[0].state_step
        # One prediction into the first observed target period.  Callers wanting
        # a presample state can choose an earlier state_step in their panel.
        mean, variance = self._predict(mean, variance, 1)
        updates = []
        for row in rows:
            if row.state_step > current_step:
                mean, variance = self._predict(mean, variance, row.state_step - current_step)
                current_step = row.state_step
            elif row.state_step < current_step:  # guarded by sort; defensive only
                raise ValueError("state_step order moved backwards")

            # Lower-quality observations are retained but carry less precision.
            r_var = row.measurement_variance / (row.quality * row.quality)
            innovation = row.value - row.loading * mean
            innovation_variance = row.loading * row.loading * variance + r_var
            gain = variance * row.loading / innovation_variance
            posterior_mean_change = gain * innovation
            prior_mean, prior_variance = mean, variance
            mean = mean + posterior_mean_change
            # Joseph form for the scalar filter protects non-negativity.
            update = 1.0 - gain * row.loading
            variance = update * update * variance + gain * gain * r_var
            variance = max(variance, 1e-12)
            updates.append({
                "name": row.name,
                "state_step": row.state_step,
                "released_at": row.released_at.isoformat(),
                "collected_at": row.collected_at.isoformat(),
                "revision": row.revision,
                "update_kind": "point_in_time_filter_update",
                "prior_mean": prior_mean,
                "prior_variance": prior_variance,
                "innovation": innovation,
                "kalman_gain": gain,
                "posterior_mean_change": posterior_mean_change,
                "posterior_mean": mean,
                "posterior_variance": variance,
            })

        return {
            "status": "ok",
            "as_of": as_of.isoformat(),
            "state_step": current_step,
            "mean": mean,
            "standard_error": math.sqrt(variance),
            "n_releases": len(rows),
            "updates": updates,
            "selection_diagnostics": diagnostics,
            "method": (
                "scalar linear-Gaussian dynamic factor; latest vintages filtered by release "
                "and collection time, then assimilated in target-time order; updates are "
                "fixed-as-of filter updates, not chronological release-news events"
            ),
        }
