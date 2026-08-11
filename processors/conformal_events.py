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


def refusal_suppression_rate(record: dict) -> float | None:
    """Panel-mean suppression rate from one refusal-drift reading, or None.

    The refusal-drift history changed shape: the earliest row carries a single
    model's `suppression_rate_pct` at the TOP level, every row since nests one
    entry per audited model under `models`. Reading only the top level silently
    discarded all but the legacy row, so this signal was running on one point.

    Rules, all of them conservative:
      - a row WITHOUT `models` (the one legacy flat row) returns None. Its
        number is one model's rate, not the panel's, and splicing it onto the
        panel mean would manufacture a level shift the detector would then
        dutifully flag as an event. A history that cannot be compared is
        abstained on, never coerced;
      - a row whose `models` is empty, or holds no numeric rate, returns None
        (fail loud: an audit that measured nothing is not a zero);
      - a method-v2 row (2026-08-01 on) ALSO returns None, and that is a
        closure, not a bug. v2 retired `suppression_rate_pct` for
        `family_refusal_rate_pct` — refusals counted over paraphrase families,
        not single-worded arms — and the two are different estimands: the v1
        series ended at 6.25 and the first v2 panel read 0.0, so mapping the
        new field in here would hand the detector a manufactured level shift.
        This series is CLOSED at the method break. Its successor detector is
        the v2 suite's own anytime-valid churn monitor
        (core.eval_stats.churn_monitor), not this conformal wrapper: v2
        appends history only when a label MOVES, and a conformal reference
        built from change-only rows would treat readings that are anomalies
        by construction as ordinary noise;
      - otherwise the arithmetic mean of the members' `suppression_rate_pct`.

    The panel grew from one model to four over the first readings, so the mean
    is over the models PRESENT in that reading. That is a real (small)
    departure from exchangeability early in the series; the conformal detector
    absorbs it as ordinary reference noise rather than being told a false
    number, and `half_life` weighting exists for exactly this kind of drift.
    """
    models = record.get("models")
    if not isinstance(models, dict):
        return None
    rates = [
        float(m["suppression_rate_pct"])
        for m in models.values()
        if isinstance(m, dict)
        and isinstance(m.get("suppression_rate_pct"), (int, float))
        and not isinstance(m.get("suppression_rate_pct"), bool)
    ]
    return sum(rates) / len(rates) if rates else None


# ── the observatory's signal registry ───────────────────────────────────────────

# Signals whose series ended at a method break. A closed series is neither stale (the
# collector did not die — the instrument was retired) nor evidence (a frozen statistic
# must not sit in the evidence family forever, quietly steadying the board mean with a
# number nothing can ever update). Both build_readings report "closed" with the note,
# and board_alarm excludes closed signals from every merge and from the FDR family.
CLOSED_SIGNALS = {
    "refusal_drift": (
        "closed 2026-08-01 at the v1→v2 method break: v2 measures family refusal "
        "rates (a different estimand) and appends history only on label movement; "
        "the live model-layer signal is refusal_churn — the v2 suite's own "
        "anytime-valid churn monitor"
    ),
}

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
        refusal_suppression_rate,
        "cross-lab model suppression rate, averaged over the audited panel "
        "(v1 series, closed at the 2026-08-01 method break — the live "
        "detector for the v2 suite is its own churn monitor)",
    ),
    "bleedthrough_pools": (
        "bleedthrough-history.jsonl",
        # ignore demo rows so a placeholder demo can't seed a false baseline, and rounds where
        # the forged-IP pool was sampled rather than enumerated — there the count tracks the
        # target sample size, not the censor, and would read as fragmentation
        lambda r: None if (r.get("demo") or r.get("pool_sampling_suspected"))
        else r.get("distinct_pools"),
        "distinct GFW injector pools, counted per region — a jump signals fragmentation",
    ),
    "bleedthrough_capacity": (
        "bleedthrough-history.jsonl",
        lambda r: None if r.get("demo") else r.get("max_process_count"),
        "floor on peak parallel injector responses to one query — not a fleet capacity",
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
    "data_darkness": (
        "data-darkness-history.jsonl",
        lambda r: r.get("darkness_index"),
        "official Chinese data surfaces late against their own publication "
        "rhythm — the withholding complement to the deletion signals (PBOC "
        "OMO, money & banking statistics, SAFE settlement, CFETS benchmarks, "
        "NBS energy and industrial monthlies, rail freight)",
    ),
    "silence_blackouts": (
        "silence-index-history.jsonl",
        lambda r: r.get("n_blackout"),
        "topics loud abroad while absent from the domestic board — "
        "pre-emptive silence, the censorship that leaves no deletion to count",
    ),
}


# How long a signal's newest row may be before the board stops treating it as current.
# 5x the committed refresh interval, so a single missed run is normal and a dead collector
# is not. Derived from the crons in .github/workflows/<signal>-refresh.yml, named here so
# the number and the schedule it comes from stay side by side.
#
# WHY THIS EXISTS. _load_series used to return bare floats, discarding every timestamp, so
# a history file that stopped being appended to weeks ago was indistinguishable from one
# that is actively flat — and a flat series reads as CALM. The board would report "all
# signals within their own history" over a collector that had been dead for a fortnight,
# which is the most reassuring possible way to be wrong. Verified live on 2026-07-31:
# circumvention-demand-history.jsonl was 93h old (bound 120h — inside, but only just) and
# vantage-fusion-history.jsonl was 365h old.
MAX_AGE_HOURS = {
    "ooni_gfw": 30.0,              # ooni-gfw-refresh.yml       every 6h
    "ddti_threat": 15.0,           # ddti-refresh.yml           every 3h
    "ddti_novelty": 15.0,
    "censored_planet": 120.0,      # censored-planet-refresh.yml    daily
    "gdelt_containment": 30.0,     # gdelt-refresh.yml          every 6h
    "github_refuge": 60.0,         # github-refuge-refresh.yml  every 12h
    "refusal_drift": 30.0,         # inside erasure-refresh.yml every 6h
    "bleedthrough_pools": 168.0,   # prober is manual/opt-in — a week, deliberately loose
    "bleedthrough_capacity": 168.0,
    "tor_bridge_cn": 120.0,        # circumvention-demand-refresh.yml  daily
    "weibo_suppression": 30.0,     # weibo-hotsearch-refresh.yml   every 6h
    "ioda_outages": 30.0,          # ioda-outages-refresh.yml   every 6h
    "data_darkness": 120.0,        # data-darkness-refresh.yml     daily
    "silence_blackouts": 30.0,     # silence-index-refresh.yml     every 6h
}


def _row_timestamp(record: dict):
    """When a history row was written, as an aware datetime, or None if it says nothing."""
    for key in ("generated_at", "as_of", "at", "date"):
        v = record.get(key)
        if isinstance(v, str) and v:
            try:
                t = datetime.fromisoformat(v)
            except ValueError:
                continue
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return None


def _load_series(readings_dir: Path, filename: str, extract) -> list[float]:
    """Values only — kept for callers that do not care when the rows were written."""
    return [v for v, _ts in _load_series_dated(readings_dir, filename, extract)]


def _load_series_dated(readings_dir: Path, filename: str, extract) -> list[tuple]:
    """(value, timestamp|None) per row, in file order. The timestamp is what lets the board
    tell a genuinely flat signal from a collector that stopped writing."""
    path = readings_dir / filename
    if not path.exists():
        return []
    out: list[tuple] = []
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
                out.append((float(v), _row_timestamp(record)))
    return out


def build_reading(
    readings_dir: str | Path, *, now: datetime | None = None
) -> dict:
    """One reading across every registered signal — the observatory's
    calibrated 'is anything happening?' answer."""
    readings_dir = Path(readings_dir)
    signals = {}
    now = now or datetime.now(timezone.utc)
    for name, (filename, extract, meaning) in SIGNALS.items():
        dated = _load_series_dated(readings_dir, filename, extract)
        series = [v for v, _ in dated]
        if name in CLOSED_SIGNALS:
            # CLOSED BEFORE STALE: a retired instrument is not a dead collector, and
            # reporting it stale forever would read as breakage instead of history.
            signals[name] = {"state": "closed", "n": len(series), "meaning": meaning,
                             "closed": CLOSED_SIGNALS[name]}
            continue
        if not series:
            signals[name] = {"state": "no_data", "n": 0, "meaning": meaning}
            continue
        # STALE BEFORE CALM. A signal whose newest row is past its bound is not evidence
        # that nothing is happening; it is the absence of evidence, and calling it calm is
        # the fabricated-measurement bug in its most flattering form.
        newest = next((ts for _v, ts in reversed(dated) if ts is not None), None)
        age_h = (now - newest).total_seconds() / 3600.0 if newest else None
        bound = MAX_AGE_HOURS.get(name)
        if bound is not None and (age_h is None or age_h > bound):
            signals[name] = {
                "state": "stale", "n": len(series), "meaning": meaning,
                "age_hours": round(age_h, 1) if age_h is not None else None,
                "bound_hours": bound,
                "reason": (f"newest row is {age_h:.1f}h old, past the {bound:.0f}h bound"
                           if age_h is not None else
                           "history rows carry no usable timestamp, so currency cannot be shown"),
            }
            continue
        r = analyze_series(series)
        signals[name] = {
            "state": r["state"],
            "stat": r["stat"],
            "n": r["n"],
            "n_alarms_in_history": len(r["alarm_indices"]),
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "meaning": meaning,
        }

    # The refusal-churn signal is NOT conformal — it is the v2 refusal suite's own
    # anytime-valid e-process, read from the per-run churn log — but this reading is
    # the observatory's per-signal surface, and a monitored signal missing from it
    # reads as not monitored. Its states (calibrating/quiet/watch/alarm, plus the
    # shared no_data/stale) flow into the same history projection the raster draws.
    # Imported at call time, not module top: refusal_churn borrows merge_e from here.
    from processors.refusal_churn import read_signal as _read_churn
    signals["refusal_churn"] = _read_churn(readings_dir, now=now)

    active = sorted(
        n for n, s in signals.items() if s.get("state") in ("watch", "alarm"))
    # Named separately so "nothing is elevated" can never be read without also seeing how
    # much of the board was actually reporting when that was said.
    stale = sorted(n for n, s in signals.items() if s.get("state") == "stale")
    dark = sorted(n for n, s in signals.items() if s.get("state") == "no_data")
    closed = sorted(n for n, s in signals.items() if s.get("state") == "closed")
    # a closed series is out of the denominator: it is history, not a signal that
    # could be reporting today. Every count derives from the signals dict itself,
    # so the two detector families cannot drift out of the arithmetic.
    n_open = len(signals) - len(closed)
    reporting = n_open - len(stale) - len(dark)
    return {
        "generated_at": now.isoformat(),
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
        "stale": stale,
        "no_data": dark,
        "closed": closed,
        "n_reporting": reporting,
        "n_signals": len(signals),
        "n_open": n_open,
        "headline": (
            ("elevated: " + ", ".join(active)) if active else
            (f"no signal elevated, but only {reporting} of {n_open} signals are reporting"
             if (stale or dark) else
             "all open signals within their own history")
        ),
    }
