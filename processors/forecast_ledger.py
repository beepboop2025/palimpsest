"""Forecast ledger — the observatory forecasts, then scores itself, and publishes the misses.

THE PROBLEM. `processors/forecaster.py` emits a "called shot": a timestamped,
falsifiable prediction whose own docstring calls a confirmed call "the strongest
possible demand-and-validity proof — the tool earns belief instead of asking for
it." No resolver was ever written. No past call has been graded. An unscored
called shot is not evidence; it is advertising, and the asymmetry is the whole
problem — a forecaster that never publishes its record is indistinguishable from
one that is always wrong.

Across the field this gap is universal. OONI, Censored Planet, IODA, Cloudflare
Radar and NetBlocks publish measurements or events; none publishes a forecast
with a scored track record, and the annual indices (Freedom on the Net, V-Dem,
RSF) are expert judgement with no measurement chain to score against at all.

THE TOOL. A strictly prequential loop (Dawid, JRSS-A 1984): at every reading,
forecast the next one from ONLY what was known then, then score against what
actually arrived. Nothing is refit with hindsight, so the resulting record is
what the observatory would really have said.

  point forecast   the last observed value. A random walk is the honest hard
                   baseline for these series; a fancier model has to beat it.
  interval         point +/- an ADAPTIVE conformal quantile of past absolute
                   one-step changes. Adaptive Conformal Inference (Gibbs &
                   Candes, NeurIPS 2021, arXiv:2106.00170) with decaying step
                   sizes (Angelopoulos, Barber & Bates, ICML 2024,
                   arXiv:2402.01139): after each miss the band widens, after
                   each hit it narrows, by
                       alpha_{t+1} = alpha_t + gamma_t * (alpha - err_t)
                   which holds long-run coverage under ARBITRARY distribution
                   shift — no stationarity assumption, which matters because a
                   censorship series is exactly what shifts when the censor acts.
  score            Weighted Interval Score (Bracher, Ray, Gneiting & Reich,
                   PLOS Comp Biol 2021, arXiv:2005.12881), a proper scoring rule
                   and a quantile approximation of CRPS, decomposable into
                   sharpness and over/under-prediction penalties. Lower is
                   better. Proper matters: an improper score can be gamed by
                   hedging, and a self-published scoreboard must not be gameable
                   by the person publishing it.
  baseline         the same forecast with a FIXED quantile instead of an
                   adaptive one. If adaptation does not earn its keep, the
                   ledger says so.

WHAT IS PUBLISHED. Empirical coverage against nominal, the WIS against the
baseline's WIS, and the worst misses by name and date. A forecast record with the
misses removed is worth nothing, so removing them is not an option the code
offers.

HONEST LIMITS:
  - one step ahead, on each signal's own cadence — a 3-hourly signal's "next
    reading" is three hours, a daily signal's is a day; horizons are not pooled;
  - a random-walk point forecast is deliberately unambitious. The claim here is
    calibration (the band covers what it says it covers), not sharpness;
  - coverage is a property of the series as recorded. A signal whose history logs
    state transitions rather than heartbeats is not a time series in this sense
    and is excluded rather than scored;
  - this scores the observatory's SIGNALS, not the term-level escalation calls in
    forecaster.py. Those need the full ranked list preserved per reading, which
    the history files do not currently carry — see NOT_YET_SCOREABLE below.

stdlib only, deterministic, offline-verifiable from committed histories.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from processors.conformal_events import SIGNALS, _load_series

# Central interval levels scored by the WIS, plus the median.
LEVELS = (0.50, 0.80, 0.95)
NOMINAL = 0.80          # the headline band whose coverage is reported
GAMMA0 = 0.05           # ACI base step size
MIN_HISTORY = 20        # readings before the ledger will score a signal
WARMUP = 10             # readings used to seed the residual pool, never scored

# Signals whose history is a STATE-TRANSITION log rather than a regular series.
# Forecasting "the next transition" is a different problem from forecasting the
# next reading, and pretending otherwise would manufacture a score out of an
# artifact of how the file is written.
NOT_A_TIME_SERIES = {"weibo_suppression", "ioda_outages"}

NOT_YET_SCOREABLE = (
    "term-level escalation calls from processors/forecaster.py are not scored here: "
    "resolving them needs the full ranked term list preserved at each reading, and "
    "readings/ddti-history.jsonl currently stores only the top term. Scoring them "
    "requires that list to start being written; it cannot be reconstructed."
)


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Type-7 empirical quantile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * min(max(q, 0.0), 1.0)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def interval_score(y: float, lo: float, hi: float, alpha: float) -> float:
    """Interval score for a central (1-alpha) interval. Lower is better.

    Width, plus a penalty proportional to how far outside the observation fell.
    """
    s = hi - lo
    if y < lo:
        s += (2.0 / alpha) * (lo - y)
    elif y > hi:
        s += (2.0 / alpha) * (y - hi)
    return s


def wis(y: float, median: float, intervals: dict[float, tuple[float, float]]) -> float:
    """Weighted Interval Score over several central levels plus the median.

    WIS = (0.5*|y-m| + sum_k (alpha_k/2) * IS_k) / (K + 0.5)
    """
    if not intervals:
        return abs(y - median)
    total = 0.5 * abs(y - median)
    for level, (lo, hi) in intervals.items():
        alpha = 1.0 - level
        total += (alpha / 2.0) * interval_score(y, lo, hi, alpha)
    return total / (len(intervals) + 0.5)


def run_prequential(values: list[float]) -> dict:
    """Forecast each reading from only its past, then score it. No hindsight."""
    n = len(values)
    if n < MIN_HISTORY:
        return {"scoreable": False,
                "reason": f"needs {MIN_HISTORY} readings, has {n}"}

    residuals: list[float] = []          # |one-step change| seen so far
    alpha = 1.0 - NOMINAL                # ACI-adapted miscoverage target
    alpha_fixed = 1.0 - NOMINAL          # the non-adaptive baseline

    hits = misses = 0
    wis_model: list[float] = []
    wis_base: list[float] = []
    records: list[dict] = []

    for t in range(1, n):
        point = values[t - 1]            # random-walk point forecast
        y = values[t]
        if len(residuals) >= WARMUP:
            pool = sorted(residuals)
            # adaptive band (clamped so a run of misses cannot demand a
            # quantile outside [0,1] or collapse the band to nothing)
            a = min(max(alpha, 0.01), 0.99)
            q_ad = _quantile(pool, 1.0 - a)
            q_fx = _quantile(pool, NOMINAL)

            lo, hi = point - q_ad, point + q_ad
            covered = lo <= y <= hi
            hits += covered
            misses += (not covered)

            ivals_ad, ivals_fx = {}, {}
            for lv in LEVELS:
                a_lv = min(max(alpha * (1.0 - lv) / (1.0 - NOMINAL), 0.01), 0.99)
                q = _quantile(pool, 1.0 - a_lv)
                ivals_ad[lv] = (point - q, point + q)
                qb = _quantile(pool, lv)
                ivals_fx[lv] = (point - qb, point + qb)

            s_model = wis(y, point, ivals_ad)
            s_base = wis(y, point, ivals_fx)
            wis_model.append(s_model)
            wis_base.append(s_base)
            records.append({"t": t, "forecast": round(point, 4),
                            "lo": round(lo, 4), "hi": round(hi, 4),
                            "actual": round(y, 4), "covered": bool(covered),
                            "wis": round(s_model, 4),
                            "miss_by": 0.0 if covered else round(
                                min(abs(y - lo), abs(y - hi)), 4)})

            # ACI update with a decaying step (arXiv:2402.01139)
            err = 0.0 if covered else 1.0
            gamma = GAMMA0 / (1.0 + 0.01 * len(wis_model)) ** 0.5
            alpha = alpha + gamma * ((1.0 - NOMINAL) - err)
            alpha = min(max(alpha, 0.01), 0.99)

        residuals.append(abs(values[t] - values[t - 1]))

    scored = hits + misses
    if scored == 0:
        return {"scoreable": False, "reason": "no reading survived warm-up"}

    worst = sorted((r for r in records if not r["covered"]),
                   key=lambda r: r["miss_by"], reverse=True)[:5]
    mean_model = sum(wis_model) / len(wis_model)
    mean_base = sum(wis_base) / len(wis_base)

    return {
        "scoreable": True,
        "n_forecasts": scored,
        "nominal_coverage": NOMINAL,
        "empirical_coverage": round(hits / scored, 3),
        "coverage_gap_pp": round(100.0 * (hits / scored - NOMINAL), 1),
        "wis": round(mean_model, 4),
        "wis_baseline_fixed_quantile": round(mean_base, 4),
        "beats_baseline": bool(mean_model < mean_base),
        "skill_vs_baseline": (round(1.0 - mean_model / mean_base, 3)
                              if mean_base > 0 else None),
        "n_misses": misses,
        "worst_misses": worst,
    }


def build_reading(readings_dir: str | Path) -> dict:
    readings_dir = Path(readings_dir)
    signals, excluded = {}, {}

    for name, (filename, extract, _meaning) in SIGNALS.items():
        if name in NOT_A_TIME_SERIES:
            excluded[name] = "history logs state transitions, not a regular series"
            continue
        series = _load_series(readings_dir, filename, extract)
        r = run_prequential(series)
        if r.get("scoreable"):
            signals[name] = r
        else:
            excluded[name] = r.get("reason", "not scoreable")

    scored = [s for s in signals.values()]
    n_forecasts = sum(s["n_forecasts"] for s in scored)
    n_beating = sum(1 for s in scored if s["beats_baseline"])
    # coverage averaged over signals, weighted by how many forecasts each made
    cov = (sum(s["empirical_coverage"] * s["n_forecasts"] for s in scored) / n_forecasts
           if n_forecasts else None)

    if not scored:
        headline = "no signal has enough regular history to score a forecast yet"
    else:
        headline = (
            f"{len(scored)} signals scored over {n_forecasts} one-step forecasts; "
            f"{cov:.0%} of readings fell inside the {NOMINAL:.0%} band "
            f"({'well calibrated' if abs(cov - NOMINAL) <= 0.05 else 'MISCALIBRATED'}); "
            f"adaptive bands beat the fixed-quantile baseline on {n_beating} of {len(scored)}"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "headline": headline,
        "n_signals_scored": len(scored),
        "n_forecasts": n_forecasts,
        "pooled_empirical_coverage": round(cov, 3) if cov is not None else None,
        "nominal_coverage": NOMINAL,
        "n_beating_baseline": n_beating,
        "signals": signals,
        "excluded": excluded,
        "not_yet_scoreable": NOT_YET_SCOREABLE,
        "method": (
            "strictly prequential (Dawid 1984): each reading is forecast from only its "
            "own past, never refit with hindsight. Point forecast = previous value "
            "(random walk). Band = adaptive conformal quantile of past absolute one-step "
            "changes, updated by alpha_{t+1} = alpha_t + gamma_t*(alpha - err_t) with "
            "decaying gamma (ACI, arXiv:2106.00170; decaying steps, arXiv:2402.01139), "
            "valid under arbitrary distribution shift. Scored by the Weighted Interval "
            "Score (arXiv:2005.12881), a proper rule, against the same forecast using a "
            "fixed quantile. Lower WIS is better."
        ),
        "caveats": [
            "one step ahead on each signal's OWN cadence; horizons are not pooled and "
            "a 3-hourly signal's forecast is not comparable to a daily signal's",
            "the point forecast is a random walk on purpose — the claim being tested is "
            "CALIBRATION (does the band cover what it says), not sharpness",
            "signals whose history logs state transitions rather than readings are "
            "excluded rather than scored; scoring them would manufacture a number out "
            "of how the file is written",
            "misses are published in full because a forecast record with the misses "
            "removed is worth nothing",
        ],
    }


if __name__ == "__main__":  # pragma: no cover - operator convenience
    print(json.dumps(build_reading(Path(__file__).resolve().parent.parent / "readings"), indent=2))
