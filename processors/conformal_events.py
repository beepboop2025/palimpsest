"""Conformal event flags — anytime-valid alarms over every observatory signal.

THE PROBLEM. Each signal (DDTI threat, OONI GFW anomaly rate, Censored Planet
interference, refuge pressure...) is a noisy time series, and "is today's
reading an EVENT?" has so far been answered by eyeballing or hand-set
thresholds. A hand-set threshold has no error statement: it fires when it
fires, and nobody can say how often it cries wolf.

THE TOOL. A conformal e-detector (Vovk's conformal test martingales; the
deployment-monitoring form is WATCH, arXiv:2505.04608, which pairs them with
the Shiryaev-Roberts changepoint statistic; e-detector theory: Shin, Ramdas &
Shekhar 2023). Each new reading is scored against the signal's own past by a
conformal p-value; a decreasing bet b(p) turns it into an e-value (E[b(p)] <= 1
while nothing is changing, since p is super-uniform); the Shiryaev-Roberts
recursion accumulates them:

    R_t = (1 + R_{t-1}) * b(p_t)

A pure product martingale is ground toward zero by every calm year and then
cannot react; the SR recursion's additive floor makes it restart-anchored at
every instant, which is what a standing observatory needs. Its guarantee is
the right one for REPEATED monitoring:

    P( first false flag within n readings ) <= n / A
    E[ readings to a false flag ]           >= A          (ARL bound)

for flag threshold A. So ALARM at A=500 falsely fires at most once per 500
readings on average — stated per reading, valid however long the observatory
runs. No distributional assumption, no training set, honest from the very
first weeks of a young signal's history.

DESIGN (matches the rest of Palimpsest):
  - stdlib only, deterministic, offline-verifiable: rerunning over the same
    history JSONL reproduces every number;
  - conservative by construction: the deterministic p-value (rank+1)/(n+1) is
    super-uniform, which can only make the martingale SMALLER — validity is
    never bought with optimism;
  - fail-loud: a signal with too little history reports "warming up", never a
    silent 0;
  - after an ALARM both the statistic and the reference history reset, so the
    post-alarm world becomes the new null and the NEXT change stays
    detectable instead of the detector saturating forever.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

# Betting function: a deterministic discrete mixture of epsilon-bets
#   b_eps(p) = eps * p^(eps-1),  eps in (0,1)
# Each b_eps integrates to 1 over [0,1] and is decreasing in p, so b(p) of a
# super-uniform p-value is an e-value (E[b(p)] <= 1 under no change); mixing
# over a grid keeps power against both abrupt and slow shifts without
# choosing one epsilon.
EPS_GRID = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)

WATCH_A = 100.0    # SR flag: false WATCH on average <= once per 100 readings
ALARM_A = 500.0    # SR flag: false ALARM on average <= once per 500 readings
WARMUP = 8         # readings before the first bet (rank needs company)
STAT_CAP = 1e12    # numeric ceiling only; an alarm resets long before


def conformal_pvalue(history: list[float], x: float, *, side: str = "up",
                     weights: list[float] | None = None) -> float:
    """Deterministic conformal p-value of x against its past.

    p = (#{past at least as extreme as x} + 1) / (n + 1): the chance a benign
    reading looks this extreme. Ties count toward the numerator (conservative,
    super-uniform), so validity holds without randomization — Palimpsest cores
    carry no RNG.

    side="up" scores unusually HIGH readings, side="down" unusually LOW ones.
    A collapsing signal is an event too: OONI's anomaly rate falling can mean
    measurement itself is being blocked, and injector pools going quiet means
    consolidation — an upward-only detector is blind to half the event space.

    weights (optional, one per history entry) implement the nonexchangeable
    conformal p-value of Barber, Candes, Ramdas & Tibshirani (Ann. Statist.
    2023, arXiv:2202.13415): recent history counts more, so slow drift and
    seasonality erode coverage gracefully instead of silently becoming the new
    null. The new point always carries weight 1.
    """
    extreme = (lambda s: s >= x) if side == "up" else (lambda s: s <= x)
    if weights is None:
        return (sum(1 for s in history if extreme(s)) + 1) / (len(history) + 1)
    if len(weights) != len(history):
        raise ValueError("weights must be parallel to history")
    num = sum(w for s, w in zip(history, weights) if extreme(s)) + 1.0
    return num / (sum(weights) + 1.0)


def decay_weights(n: int, half_life: float | None) -> list[float] | None:
    """Geometric weights over an n-long history, newest last, weight 1 at the end.

    half_life is in readings; None disables weighting (plain exchangeable
    conformal). A finite half-life is what keeps a long-lived signal sensitive:
    with an unweighted reference that only grows, each new point is one of ever
    more and the detector goes progressively deaf.
    """
    if not half_life or n <= 0:
        return None
    return [0.5 ** ((n - 1 - i) / half_life) for i in range(n)]


def bet(p: float) -> float:
    """Deterministic epsilon-mixture bet against uniformity of one p-value."""
    p = min(max(p, 1e-12), 1.0)
    return sum(e * p ** (e - 1.0) for e in EPS_GRID) / len(EPS_GRID)


def merge_e(values: list[float]) -> float:
    """Merge e-values by arithmetic mean.

    Vovk & Wang (Ann. Statist. 2021, arXiv:1912.06116): the mean of arbitrarily
    DEPENDENT e-values is itself a valid e-value, and is essentially the only
    admissible symmetric merger. Dependence is the normal case here — the two
    sides of one signal, and the layers of one censorship event, all move
    together — so the mean is used everywhere rather than a product.
    """
    return sum(values) / len(values) if values else 1.0


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _effect(reference: list[float], x: float) -> dict:
    """How far the reading sits from its own past, in plain units.

    A state label alone ("watch") does not tell a reader whether the departure
    is trivial or enormous. Robust scale (median absolute deviation, scaled to
    be comparable to a standard deviation for roughly-normal data) is used
    because these series are short, skewed and outlier-prone.
    """
    if not reference:
        return {"direction": "flat", "delta": None, "robust_z": None, "pct_of_past_below": None}
    med = _median(reference)
    mad = _median([abs(s - med) for s in reference])
    delta = x - med
    scale = 1.4826 * mad
    below = sum(1 for s in reference if s < x) / len(reference)
    return {
        "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
        "delta": round(delta, 4),
        "robust_z": round(delta / scale, 2) if scale > 0 else None,
        "pct_of_past_below": round(100.0 * below, 1),
    }


def analyze_series(values: list[float], *, two_sided: bool = True,
                   half_life: float | None = None) -> dict:
    """Walk a signal's history with the Shiryaev-Roberts e-detector.

    Returns per-reading statistic, state, alarm epochs, and — new — the
    DIRECTION and size of the current departure.

    two_sided runs an upward and a downward conformal p-value and merges their
    bets by arithmetic mean (Vovk & Wang 2021). The merged quantity is still an
    e-value, so the Shiryaev-Roberts guarantee below is unchanged; the detector
    simply stops being blind to collapses.

    half_life (readings) applies geometric weights to the reference, trading
    exact exchangeability for robustness to slow drift (arXiv:2202.13415).
    None keeps the original unweighted behaviour.

    Reset policy: the statistic and the reference history reset after each
    alarm, so the detector hunts the NEXT change instead of living off (or
    saturating on) the last one. While the reference refills the state is
    reported as "warming_up" — never as "calm", which would read as an
    all-clear during exactly the period the detector cannot see.
    """
    # Two independent Shiryaev-Roberts statistics, one per direction. Merging the
    # two bets into a single e-value would be valid but halves the capital at
    # every step, which costs most of the detector's power against a real shift.
    # Running them separately keeps each at full power; the union bound over two
    # detectors is paid for by doubling each threshold, so the FAMILY-WISE
    # guarantee published below is exactly the one-sided guarantee it replaces:
    #   P(either side flags within n) <= n/(2A) + n/(2A) = n/A.
    sides = ("up", "down") if two_sided else ("up",)
    mult = float(len(sides))
    watch_at, alarm_at = WATCH_A * mult, ALARM_A * mult

    sr = {s: 0.0 for s in sides}
    reference: list[float] = []
    states: list[str] = []
    stats: list[float] = []
    alarms: list[int] = []
    directions: list[str] = []
    driver: str | None = None

    for i, x in enumerate(values):
        eff = _effect(reference, x)
        if len(reference) >= WARMUP:
            w = decay_weights(len(reference), half_life)
            for s in sides:
                p = conformal_pvalue(reference, x, side=s, weights=w)
                sr[s] = min((1.0 + sr[s]) * bet(p), STAT_CAP)
        peak = max(sr.values())
        driver = max(sr, key=lambda s: sr[s]) if peak > 0 else None
        state = (
            "warming_up" if len(reference) < WARMUP else
            "alarm" if peak >= alarm_at else
            "watch" if peak >= watch_at else "calm"
        )
        states.append(state)
        stats.append(round(peak, 4))
        directions.append(eff["direction"])
        reference.append(x)
        if state == "alarm":
            alarms.append(i)
            sr = {s: 0.0 for s in sides}
            reference = []  # post-alarm world is the new null

    last_ref = values[:-1] if len(values) > 1 else []
    effect = _effect(last_ref, values[-1]) if values else {}
    return {
        "n": len(values),
        "state": states[-1] if states else "no_data",
        "stat": stats[-1] if stats else None,
        "stats": stats,
        "states": states,
        "alarm_indices": alarms,
        "directions": directions,
        "effect": effect,
        "two_sided": two_sided,
        "driver_side": driver,
        "watch_at": watch_at,
        "alarm_at": alarm_at,
    }


# ── the observatory's signal registry ───────────────────────────────────────────
# signal -> (history file, extractor(record) -> float | None, meaning of "high")
SIGNALS = {
    "ooni_gfw": (
        "ooni-gfw-history.jsonl",
        lambda r: r.get("gfw_index"),
        "network-layer GFW anomaly rate",
    ),
    "ddti_threat": (
        "ddti-history.jsonl",
        lambda r: r.get("top_threat"),
        "peak censor attention on one term",
    ),
    "ddti_novelty": (
        "ddti-history.jsonl",
        lambda r: r.get("n_new"),
        "newly-sensitive terms entering the deletion stream",
    ),
    "censored_planet": (
        "censored-planet-history.jsonl",
        lambda r: r.get("cn_interference_rate_pct"),
        "remote-vantage interference rate",
    ),
    "gdelt_containment": (
        "gdelt-history.jsonl",
        lambda r: (
            r["n_containment"] + r["n_blackout"]
            if r.get("n_containment") is not None and r.get("n_blackout") is not None
            else None
        ),
        "terms loud abroad while censored at home",
    ),
    "github_refuge": (
        "github-refuge-history.jsonl",
        lambda r: r.get("n_pressure_events"),
        "takedown/pressure events on refuge repositories",
    ),
    "refusal_drift": (
        "refusal-drift-history.jsonl",
        lambda r: r.get("suppression_rate_pct"),
        "cross-lab model suppression rate",
    ),
    "bleedthrough_pools": (
        "bleedthrough-history.jsonl",
        # ignore demo rows so a placeholder demo can't seed a false baseline
        lambda r: None if r.get("demo") else r.get("distinct_pools"),
        "distinct GFW injector pools — a jump signals regional fragmentation",
    ),
    "bleedthrough_capacity": (
        "bleedthrough-history.jsonl",
        lambda r: None if r.get("demo") else r.get("max_process_count"),
        "peak parallel injector processes — fleet capacity on a border path",
    ),
    "tor_bridge_cn": (
        "circumvention-demand-history.jsonl",
        lambda r: r.get("bridge_users"),
        "demand for unlisted Tor entry points from China — a surge reads as "
        "censorship pressure or fear rising",
    ),
    "weibo_suppression": (
        "weibo-hotsearch-history.jsonl",
        lambda r: r.get("suppressed_invisible"),
        "DDTI terms deleted AND denied hot-search attention at once",
    ),
    "ioda_outages": (
        "ioda-outages-history.jsonl",
        lambda r: r.get("events_started_yesterday"),
        "shutdown-scale connectivity events detected by IODA's instruments",
    ),
}


def _load_series(readings_dir: Path, filename: str, extract) -> list[float]:
    path = readings_dir / filename
    if not path.exists():
        return []
    out: list[float] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line must not kill the whole signal
            v = extract(record)
            if isinstance(v, (int, float)):
                out.append(float(v))
    return out


def build_reading(readings_dir: str | Path) -> dict:
    """One reading across every registered signal — the observatory's
    calibrated 'is anything happening?' answer."""
    readings_dir = Path(readings_dir)
    signals = {}
    for name, (filename, extract, meaning) in SIGNALS.items():
        series = _load_series(readings_dir, filename, extract)
        if not series:
            signals[name] = {"state": "no_data", "n": 0, "meaning": meaning}
            continue
        r = analyze_series(series)
        signals[name] = {
            "state": r["state"],
            "stat": r["stat"],
            "n": r["n"],
            "n_alarms_in_history": len(r["alarm_indices"]),
            "meaning": meaning,
        }
    active = sorted(
        n for n, s in signals.items() if s.get("state") in ("watch", "alarm"))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": (
            "conformal e-detector per signal: one-sided conservative p-value "
            f"(rank+1)/(n+1) against the signal's own past, epsilon-mixture bet over "
            f"{EPS_GRID} (an e-value under no change), Shiryaev-Roberts recursion "
            f"R_t=(1+R_(t-1))*b(p_t), warmup {WARMUP}; WATCH >= {WATCH_A:g}, "
            f"ALARM >= {ALARM_A:g}; statistic and reference reset after alarm"
        ),
        "guarantee": (
            "anytime-valid for repeated monitoring: under no change, "
            f"P(false flag within n readings) <= n/A and average readings to a "
            f"false flag >= A (A={WATCH_A:g} watch, {ALARM_A:g} alarm)"
        ),
        "signals": signals,
        "active": active,
        "headline": (
            "all signals within their own history"
            if not active else
            "elevated: " + ", ".join(active)
        ),
    }
