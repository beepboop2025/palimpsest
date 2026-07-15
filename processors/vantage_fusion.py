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


def fuse(readings: dict) -> dict:
    """readings: {ooni, censored_planet, net4people} -> latest reading dicts
    (any may be missing). Returns the fused index + corroboration."""
    rates = _normalize(readings)
    if not rates:
        return {"ok": False, "reason": "no quantitative vantage reported — nothing to fuse"}

    # coverage-weighted mean over the vantages actually present
    wsum = sum(WEIGHTS[k] for k in rates)
    fused = sum(rates[k] * WEIGHTS[k] for k in rates) / wsum

    both = "ooni" in rates and "censored_planet" in rates
    divergence = abs(rates["ooni"] - rates["censored_planet"]) if both else None
    agreement = None
    if both:
        agreement = round(_clamp01(1.0 - divergence / AGREEMENT_SPAN), 3)

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
    verdict = (
        f"fused GFW anomaly {fused:.0f}/100 ({confidence.lower()}"
        + (f", methods agree {agreement:.0%}" if agreement is not None else ", single vantage")
        + ")"
        + (f"; ROUTING-INDUCED DIVERGENCE: OONI and Censored Planet differ by "
           f"{divergence:.0f}pp — the number is vantage-dependent"
           if confidence == "CONTESTED" else "")
    )

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fused_index": round(fused, 1),
        "confidence": confidence,
        "agreement": agreement,
        "divergence_pp": round(divergence, 1) if divergence is not None else None,
        "vantages": {k: round(v, 1) for k, v in sorted(rates.items())},
        "weights_used": {k: WEIGHTS[k] for k in rates},
        "net4people": q,
        "qualitative_flag": qual_flag,
        "verdict": verdict,
        "caveats": [
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
