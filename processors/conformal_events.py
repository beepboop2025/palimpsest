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


def conformal_pvalue(history: list[float], x: float) -> float:
    """One-sided deterministic conformal p-value of x against its past.

    p = (#{past >= x} + 1) / (n + 1): the chance a benign reading looks this
    extreme. Ties count toward the numerator (conservative, super-uniform),
    so validity holds without randomization — Palimpsest cores carry no RNG.
    """
    n = len(history)
    ge = sum(1 for s in history if s >= x)
    return (ge + 1) / (n + 1)


def bet(p: float) -> float:
    """Deterministic epsilon-mixture bet against uniformity of one p-value."""
    p = min(max(p, 1e-12), 1.0)
    return sum(e * p ** (e - 1.0) for e in EPS_GRID) / len(EPS_GRID)


def analyze_series(values: list[float]) -> dict:
    """Walk a signal's history with the Shiryaev-Roberts e-detector.

    Returns per-reading statistic, state (calm/watch/alarm), and alarm epochs.
    Reset policy: the statistic and the reference history reset after each
    alarm, so the detector hunts the NEXT change instead of living off (or
    saturating on) the last one.
    """
    sr = 0.0
    reference: list[float] = []
    states: list[str] = []
    stats: list[float] = []
    alarms: list[int] = []

    for i, x in enumerate(values):
        if len(reference) >= WARMUP:
            sr = min((1.0 + sr) * bet(conformal_pvalue(reference, x)), STAT_CAP)
        state = (
            "warming_up" if len(reference) < WARMUP else
            "alarm" if sr >= ALARM_A else
            "watch" if sr >= WATCH_A else "calm"
        )
        states.append(state)
        stats.append(round(sr, 4))
        reference.append(x)
        if state == "alarm":
            alarms.append(i)
            sr = 0.0
            reference = []  # post-alarm world is the new null

    return {
        "n": len(values),
        "state": states[-1] if states else "no_data",
        "stat": stats[-1] if stats else None,
        "stats": stats,
        "states": states,
        "alarm_indices": alarms,
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
