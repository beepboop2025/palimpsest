"""Believability math — the composite is canonical, the drift needs a baseline.

The two failure modes this file exists to prevent:

  1. A partial composite. If electricity is missing and the composite quietly
     renormalises over loans + freight, the published number is no longer the
     Li Keqiang index — it is an editorial index wearing its name.
  2. A drift claim without a baseline. headline-minus-composite is large and
     POSITIVE in any services-heavy economy; scoring that level as suspicion
     would flag every month forever. Only a break from the gap's own history
     may say "diverging", and until enough history exists the reading says
     "warming_up" and claims nothing.

Offline, stdlib-only, pure arithmetic.
"""
from __future__ import annotations

import pytest

from processors.believability import (
    LKQ_WEIGHTS,
    MIN_HISTORY,
    divergence_reading,
    lkq_composite,
)

FULL = {"loans_yoy": 9.0, "electricity_yoy": 5.0, "rail_freight_yoy": 2.0}
# 0.4*9 + 0.4*5 + 0.2*2 = 6.0
STEADY_GAPS = [1.0, 1.2, 0.9, 1.1, 1.0, 0.8, 1.1, 1.0]


def test_the_canonical_weights_are_the_economists_40_40_20():
    assert LKQ_WEIGHTS == {"loans_yoy": 0.40, "electricity_yoy": 0.40,
                           "rail_freight_yoy": 0.20}
    assert sum(LKQ_WEIGHTS.values()) == pytest.approx(1.0)


def test_composite_is_the_weighted_sum():
    assert lkq_composite(FULL) == pytest.approx(6.0)


def test_a_missing_component_kills_the_composite_not_reweights_it():
    partial = {k: v for k, v in FULL.items() if k != "electricity_yoy"}
    assert lkq_composite(partial) is None
    assert lkq_composite({**FULL, "electricity_yoy": None}) is None


def test_extra_telemetry_never_leaks_into_the_composite():
    assert lkq_composite({**FULL, "coal_yoy": -40.0, "steel_yoy": -40.0}) \
        == pytest.approx(6.0)


def test_no_headline_means_abstain():
    got = divergence_reading(None, FULL, STEADY_GAPS)
    assert got["abstained"] and got["label"] == "abstain"
    assert got["gap"] is None


def test_short_history_publishes_the_gap_but_claims_no_drift():
    got = divergence_reading(7.0, FULL, STEADY_GAPS[: MIN_HISTORY - 1])
    assert got["label"] == "warming_up" and not got["abstained"]
    assert got["gap"] == pytest.approx(1.0)
    assert got["drift"] is None and got["outside_band"] is None


def test_a_gap_matching_its_own_history_is_within_band():
    """The services-share critique, executable: a persistent +1.0 gap is the
    baseline, not the story."""
    got = divergence_reading(7.0, FULL, STEADY_GAPS)
    assert got["label"] == "within_band"
    assert got["outside_band"] is False
    assert got["expected_gap"] == pytest.approx(1.0)
    assert got["band_low"] <= got["gap"] <= got["band_high"]


def test_a_break_from_the_established_relationship_is_diverging():
    """Composite collapses to 6.0 while the headline claims 9.5: a +3.5 gap
    against a +1.0 +/- band history must register."""
    got = divergence_reading(9.5, FULL, STEADY_GAPS)
    assert got["label"] == "diverging" and got["outside_band"] is True
    assert got["drift"] == pytest.approx(2.5)


def test_a_flat_history_cannot_make_every_future_gap_infinite_surprise():
    """MAD of an identical-gap history is 0; the floor keeps the band real."""
    got = divergence_reading(7.05, FULL, [1.0] * MIN_HISTORY)
    assert got["label"] == "within_band", (
        "a 0.05-point wobble over a floor-width band must not read as a break")


def test_negative_divergence_registers_too():
    """The headline UNDERSHOOTING the physical economy is also a break in the
    relationship — the detector is two-sided by construction."""
    got = divergence_reading(4.0, FULL, STEADY_GAPS)
    assert got["label"] == "diverging"
    assert got["drift"] == pytest.approx(-3.0)
