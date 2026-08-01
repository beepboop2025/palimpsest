"""Believability read — the state's headline growth story against its own
physical telemetry.

> "GDP figures are man-made and therefore unreliable" — Li Keqiang, 2007,
> to the US ambassador in Kunming; he watched electricity consumption, rail
> freight volume and bank lending instead. This module computes exactly that
> comparison, continuously, from the state's OWN published series.

WHAT THIS MEASURES AND WHAT IT DOES NOT. The canonical Li Keqiang composite
(40% new bank loans, 40% electricity, 20% rail freight — the weights The
Economist fixed in 2010) tracks the physical-industrial economy. A gap
between the headline and the composite is NOT, by itself, evidence of
fabrication: China's services share has grown for two decades, and services
legitimately use less electricity and ship less freight per yuan of output.
That critique is built in, not footnoted:

  - the published statistic is DIVERGENCE DRIFT — this month's
    headline-minus-composite gap relative to the gap's OWN rolling history
    (median and MAD band), so a structurally widening services economy reads
    as the slowly moving baseline it is, and only a BREAK from the
    established relationship registers;
  - the band is published with every reading (uncertainty first), and a
    reading inside the band says so in plain words;
  - with fewer than MIN_HISTORY months of gap history the reading ABSTAINS —
    a drift claim without a baseline would be exactly the false certainty
    this project exists to refuse.

Pure arithmetic, standard-library only, inputs injected. No network here:
the driver owns fetching and provenance, this module owns the math and the
abstention discipline. All component inputs are year-on-year growth rates in
percent, as published by the state (NBS releases, PBOC monthly financial
statistics) — we compare the state to the state.
"""
from __future__ import annotations

import statistics

# The canonical composite, and only the canonical composite. Extra physical
# series (coal, crude steel) are published beside it as telemetry rows but
# are never silently blended in: the moment the weights become editorial,
# the index stops being citable as "the Li Keqiang index, computed live".
LKQ_WEIGHTS = {
    "loans_yoy": 0.40,
    "electricity_yoy": 0.40,
    "rail_freight_yoy": 0.20,
}

MIN_HISTORY = 8      # months of gap history before drift may be scored
BAND_MADS = 2.0      # the published expected range is median ± BAND_MADS * MAD
_MAD_FLOOR = 0.25    # pct-points; a degenerate flat history must not make
                     # every future gap read as an infinite surprise


def lkq_composite(components: dict) -> float | None:
    """The canonical 40/40/20 composite, or None unless EVERY canonical
    component is present — a partial composite would silently reweight."""
    try:
        return round(sum(
            LKQ_WEIGHTS[k] * float(components[k]) for k in LKQ_WEIGHTS
        ), 4)
    except (KeyError, TypeError, ValueError):
        return None


def divergence_reading(headline_yoy, components: dict,
                       gap_history: list) -> dict:
    """One month's believability reading. NEVER raises.

    headline_yoy: the state's headline growth claim for the same period, in
        percent (industrial production YoY for monthly reads; the driver says
        which headline it wired). None => abstain.
    components: {"loans_yoy", "electricity_yoy", "rail_freight_yoy", ...}
        in percent. Any canonical component missing => abstain.
    gap_history: prior months' raw gaps (floats, percent points), oldest
        first, NOT including this month.

    Shape:
      {"lkq_composite", "headline_yoy", "gap",            # this month's facts
       "expected_gap", "band_low", "band_high",           # the baseline story
       "drift",                                           # gap - expected_gap
       "outside_band": bool | None,
       "label": "abstain" | "warming_up" | "within_band" | "diverging",
       "abstained": bool, "n_history"}
    """
    composite = lkq_composite(components)
    base = {
        "lkq_composite": composite,
        "headline_yoy": headline_yoy,
        "n_history": len(gap_history),
    }

    if composite is None or not isinstance(headline_yoy, (int, float)) \
            or isinstance(headline_yoy, bool):
        return {**base, "gap": None, "expected_gap": None, "band_low": None,
                "band_high": None, "drift": None, "outside_band": None,
                "label": "abstain", "abstained": True}

    gap = round(float(headline_yoy) - composite, 4)

    if len(gap_history) < MIN_HISTORY:
        # The gap itself is published (it is a fact); the drift claim is not.
        return {**base, "gap": gap, "expected_gap": None, "band_low": None,
                "band_high": None, "drift": None, "outside_band": None,
                "label": "warming_up", "abstained": False}

    hist = [float(g) for g in gap_history]
    expected = statistics.median(hist)
    mad = statistics.median([abs(g - expected) for g in hist])
    band = max(mad, _MAD_FLOOR) * BAND_MADS
    low, high = expected - band, expected + band
    drift = round(gap - expected, 4)
    outside = gap < low or gap > high

    return {
        **base,
        "gap": gap,
        "expected_gap": round(expected, 4),
        "band_low": round(low, 4),
        "band_high": round(high, 4),
        "drift": drift,
        "outside_band": outside,
        "label": "diverging" if outside else "within_band",
        "abstained": False,
    }
