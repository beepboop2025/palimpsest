"""Tests for collectors.gdelt_cross_signal — the scoring core (offline).

    PYTHONPATH=. python3 -m pytest tests/test_gdelt_cross_signal.py -q

Only fetch_global_volume() touches the network; it is not exercised here. The scoring
core (normalize_global, cross_signal, rank_cross_signals) is pure and deterministic.
"""

from collectors.gdelt_cross_signal import (
    cross_signal,
    normalize_global,
    rank_cross_signals,
)


def test_normalize_global_saturates():
    assert normalize_global(0.0) == 0.0
    assert normalize_global(None) == 0.0
    assert normalize_global(2.5, saturation=5.0) == 0.5
    assert normalize_global(10.0, saturation=5.0) == 1.0   # clamped


def test_containment_requires_both_sides():
    # Loud abroad AND censored at home → containment, positive score.
    c = cross_signal(domestic_attention=2.5, domestic_present=True, global_volume_intensity=4.0)
    assert c["label"] == "containment"
    assert c["cross_score"] > 0


def test_domestic_only_when_nothing_abroad():
    c = cross_signal(domestic_attention=2.5, domestic_present=True, global_volume_intensity=0.0)
    assert c["label"] == "domestic_only"
    assert c["cross_score"] == 0.0


def test_blackout_when_absent_at_home_but_loud_abroad():
    c = cross_signal(domestic_attention=0.0, domestic_present=False, global_volume_intensity=4.0)
    assert c["label"] == "blackout"
    # blackout scores on global salience alone (no domestic attention to multiply)
    assert c["cross_score"] == normalize_global(4.0)


def test_containment_scores_below_equivalent_blackout_global():
    # Same global salience: blackout uses raw global; containment discounts by the
    # bounded domestic factor (<1), so containment < blackout for equal global volume.
    contain = cross_signal(1.0, True, 4.0)["cross_score"]
    blackout = cross_signal(0.0, False, 4.0)["cross_score"]
    assert contain < blackout


def test_rank_orders_by_cross_score_and_abstains_on_none():
    ranked = rank_cross_signals([
        {"term": "loud+censored", "domestic_attention": 3.0, "domestic_present": True,
         "global_volume_intensity": 4.0},
        {"term": "blackout", "domestic_attention": 0.0, "domestic_present": False,
         "global_volume_intensity": 2.0},
        {"term": "no-gdelt", "domestic_attention": 0.5, "domestic_present": True,
         "global_volume_intensity": None},
    ])
    terms = [r["term"] for r in ranked]
    # the abstention (None global) sorts last with score 0 and an explicit flag
    assert ranked[-1]["term"] == "no-gdelt"
    assert ranked[-1]["label"] == "unknown"
    assert ranked[-1]["abstained"] is True
    assert "loud+censored" in terms and "blackout" in terms


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
