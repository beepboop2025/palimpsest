"""Vantage fusion — one calibrated GFW anomaly rate from independent methods,
with a corroboration measure instead of three signals shown side by side.

Palimpsest already carries three China network-censorship vantages that measure
the SAME thing by DIFFERENT methods:

  OONI            in-country OONI Probe measurements (gfw_index, 0-100)
  Censored Planet 95k+ REMOTE vantage points, Satellite + Hyperquack
                  (cn_interference_rate_pct, 0-100)
  net4people      community-logged blocking events (qualitative companion)

Showing them side by side leaves the reader to reconcile them. The censorship-
measurement literature (Routing-Induced Censorship Changes Globally,
arXiv:2406.19304) shows why that reconciliation is the whole point: censorship
is VANTAGE-DEPENDENT — the same resource can look blocked from one vantage and
open from another because of routing, partial deployment, or where the probe
sits. A single vantage over- or under-counts, and two independent methods can
legitimately disagree.

So fusion here is not an average that hides disagreement — it is a triangulation
that MEASURES it:

  fused_rate    coverage-weighted mean of the quantitative vantages (0-100).
  agreement     1 − normalized spread between the quantitative vantages: how
                much the independent methods corroborate each other.
  confidence    CORROBORATED  both methods present and agree → trust the number
                CONTESTED     methods present but diverge → a vantage artifact
                              or partial deployment; the number is soft
                SINGLE        only one quantitative method reporting → uncorroborated
  divergence    when OONI and Censored Planet disagree beyond a threshold, the
                gap is reported explicitly as routing-induced inconsistency, not
                smoothed away.

net4people is a QUALITATIVE corroborator: it never moves the fused rate (a
community event log is not a measured rate), but a spike in reported blocking
events while the rate is up RAISES confidence, and blocking reports while both
rates read calm is itself a flag (something is happening the aggregates miss).

Weights reflect method coverage, not preference, and are disclosed: Censored
Planet's 95k remote vantages get the most weight, OONI's in-country probes are
the ground-truth anchor, and both are normalized to [0,1] first. stdlib-only,
deterministic, offline-verifiable from the committed vantage readings.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Coverage weights for the quantitative vantages (sum need not be 1; normalized
# over whichever are present, so a missing vantage renormalizes rather than
# scoring as calm). Rationale is on each line — this is a disclosed choice.
WEIGHTS = {
    "censored_planet": 0.55,  # 95k+ remote vantages: widest coverage
    "ooni": 0.45,             # in-country probes: the ground-truth anchor
}
DIVERGENCE_PP = 20.0          # OONI vs CP gap (percentage points) that is "contested"
AGREEMENT_SPAN = 60.0         # spread at which agreement hits 0 (pp), for scaling

# A vantage that is PRESENT BUT OLD is more dangerous than one that is missing. A missing
# vantage is already handled honestly below (absent from the dict, renormalizing the weights
# rather than scoring as calm). A stale one parses, carries a plausible rate, and fuses at
# full coverage weight while describing a week that has already gone by — so a feed that
# died quietly keeps propping up a "corroborated" reading forever.
#
# Each bound is roughly four to six of that vantage's own refresh cycles: long enough that a
# single missed run is not an outage, short enough that a dead feed cannot pass as current.
MAX_AGE_HOURS = {
    "ooni": 36.0,             # ooni-gfw-refresh.yml: every 6h
    "censored_planet": 96.0,  # censored-planet-refresh.yml: once daily
    "net4people": 48.0,       # net4people-refresh.yml: twice daily
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _normalize(readings: dict) -> dict:
    """Pull each vantage's rate onto a common 0-100 scale. Missing/unusable
    vantages are simply absent from the returned dict (never coerced to 0)."""
    out: dict[str, float] = {}
    ooni = readings.get("ooni") or {}
    if isinstance(ooni.get("gfw_index"), (int, float)):
        out["ooni"] = float(ooni["gfw_index"])
    cp = readings.get("censored_planet") or {}
    if isinstance(cp.get("cn_interference_rate_pct"), (int, float)):
        out["censored_planet"] = float(cp["cn_interference_rate_pct"])
    return out


def _qualitative(readings: dict) -> dict:
    """net4people blocking-event share, as a confidence modifier only."""
    n4p = readings.get("net4people") or {}
    n_recent = n4p.get("n_recent")
    n_block = n4p.get("n_blocking")
    if not isinstance(n_recent, (int, float)) or not n_recent:
        return {"present": False}
    share = _clamp01(float(n_block or 0) / float(n_recent))
    return {"present": True, "n_blocking": int(n_block or 0),
            "n_recent": int(n_recent), "blocking_share": round(share, 3)}


def _age_hours(reading: dict, now: datetime):
    """Hours since the reading was generated, or None if it carries no usable date."""
    ts = (reading or {}).get("generated_at")
    if not isinstance(ts, str):
        return None
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 3600.0


def _drop_stale(readings: dict, now: datetime) -> tuple[dict, dict]:
    """Split the vantages into those current enough to fuse and those that are not.

    An excluded vantage is treated exactly like an absent one — the weights renormalize
    over whoever is left — but the exclusion is REPORTED rather than silent, so a reader
    can see the fused number rests on fewer methods than usual.
    """
    usable, excluded = {}, {}
    for name, reading in readings.items():
        if not reading:
            continue
        age = _age_hours(reading, now)
        bound = MAX_AGE_HOURS.get(name)
        if age is None:
            excluded[name] = {"reason": "undated",
                              "detail": "no usable generated_at — cannot be shown to be current"}
        elif bound is not None and age > bound:
            excluded[name] = {"reason": "stale", "age_hours": round(age, 1),
                              "bound_hours": bound, "as_of": reading.get("generated_at")}
        else:
            usable[name] = reading
    return usable, excluded


def fuse(readings: dict, *, now: datetime | None = None) -> dict:
    """readings: {ooni, censored_planet, net4people} -> latest reading dicts
    (any may be missing). Returns the fused index + corroboration.

    A vantage older than its MAX_AGE_HOURS bound is excluded and reported, never fused
    in as though it described today.
    """
    now = now or datetime.now(timezone.utc)
    readings, excluded = _drop_stale(readings, now)
    rates = _normalize(readings)
    if not rates:
        reason = "no quantitative vantage reported — nothing to fuse"
        if excluded:
            reason = ("no quantitative vantage is current enough to fuse — "
                      + ", ".join(f"{k} {v['reason']}" for k, v in sorted(excluded.items())))
        return {"ok": False, "reason": reason, "excluded_vantages": excluded}

    # coverage-weighted mean over the vantages actually present
    wsum = sum(WEIGHTS[k] for k in rates)
    fused = sum(rates[k] * WEIGHTS[k] for k in rates) / wsum

    both = "ooni" in rates and "censored_planet" in rates
    divergence = abs(rates["ooni"] - rates["censored_planet"]) if both else None
    agreement = None
    if both:
        agreement = round(_clamp01(1.0 - divergence / AGREEMENT_SPAN), 3)

    # Disagreement becomes INTERVAL WIDTH, not a label on a point estimate.
    #
    # This module's docstring promised never to smooth divergence away, but the
    # headline number did exactly that and then labelled it: on 2026-07-15 OONI
    # read 59.2 and Censored Planet 4.8, and the board published a fused 29.3 —
    # a midpoint of two numbers that share no support, carried unchanged for
    # twelve days under a CONTESTED tag. A reader takes 29.3 as the estimate.
    #
    # The honest interval when independent methods disagree is the span they
    # actually bracket. When they agree it collapses to a narrow band, so the
    # same construction serves both cases and the width itself carries the
    # message: wide means nobody should quote a single rate.
    if both:
        lo, hi = min(rates.values()), max(rates.values())
    else:
        lo = hi = next(iter(rates.values()))
    interval = [round(lo, 1), round(hi, 1)]
    interval_width = round(hi - lo, 1)
    # A single rate is only defensible when the methods substantially agree.
    quotable = bool(both) and divergence is not None and divergence <= DIVERGENCE_PP

    q = _qualitative(readings)

    if not both:
        confidence = "SINGLE"
    elif divergence > DIVERGENCE_PP:
        confidence = "CONTESTED"
    else:
        confidence = "CORROBORATED"

    # net4people cross-check: reported blocking while both rates read calm is a
    # flag the aggregates may be missing something; blocking alongside an
    # elevated rate reinforces it.
    qual_flag = None
    if q["present"]:
        elevated = fused >= 40.0
        active_reports = q["blocking_share"] >= 0.4 and q["n_blocking"] >= 2
        if active_reports and not elevated:
            qual_flag = ("community blocking reports while the measured rates read "
                         "calm — a possible under-count the aggregates miss")
        elif active_reports and elevated:
            qual_flag = "community blocking reports corroborate the elevated rate"

    parts = []
    for k, v in sorted(rates.items()):
        parts.append(f"{k.replace('_', ' ')} {v:.0f}")

    if quotable:
        verdict = (f"GFW anomaly {fused:.0f}/100, range {interval[0]:.0f}–{interval[1]:.0f} "
                   f"({confidence.lower()}, methods agree {agreement:.0%})")
    elif both:
        verdict = (f"NO SINGLE RATE IS DEFENSIBLE: OONI reads {rates['ooni']:.0f} and "
                   f"Censored Planet {rates['censored_planet']:.0f}, {divergence:.0f}pp apart. "
                   f"The honest answer is the range {interval[0]:.0f}–{interval[1]:.0f}, not its "
                   f"midpoint — censorship is vantage-dependent and these two methods are "
                   f"measuring different things about the same wall.")
    else:
        verdict = (f"GFW anomaly {fused:.0f}/100 from a SINGLE vantage — uncorroborated, "
                   f"no range can be formed")

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "fused_index": round(fused, 1),
        "interval": interval,
        "interval_width_pp": interval_width,
        "single_rate_quotable": quotable,
        "confidence": confidence,
        "agreement": agreement,
        "divergence_pp": round(divergence, 1) if divergence is not None else None,
        "vantages": {k: round(v, 1) for k, v in sorted(rates.items())},
        "weights_used": {k: WEIGHTS[k] for k in rates},
        "excluded_vantages": excluded,
        "net4people": q,
        "qualitative_flag": qual_flag,
        "verdict": verdict,
        "caveats": [
            "when single_rate_quotable is false the fused_index is NOT a defensible "
            "estimate — it is the weighted midpoint of methods that disagree, kept only "
            "for continuity of the series; quote the interval instead",
            "censorship is VANTAGE-DEPENDENT (arXiv:2406.19304): independent methods "
            "can legitimately disagree because of routing, partial deployment, or "
            "probe location — divergence is reported, never smoothed away",
            "CORROBORATED means two independent methods agree, not that either is "
            "ground truth; the GFW blocks without a block page, so every method is a "
            "side-channel estimate",
            "net4people is a QUALITATIVE cross-check — it modifies confidence, never "
            "the fused rate (an event log is not a measured rate)",
            "coverage weights (Censored Planet 0.55, OONI 0.45) are a disclosed choice "
            "reflecting vantage count, renormalized over whichever vantages report",
        ],
        "method": (
            "coverage-weighted mean of vantages normalized to 0-100 (Censored Planet "
            "remote interference rate, OONI in-country anomaly index); agreement = "
            "1 − |OONI − CP| / 60pp; CONTESTED when the two differ > 20pp; net4people "
            "blocking-event share adjusts confidence only."
        ),
    }
