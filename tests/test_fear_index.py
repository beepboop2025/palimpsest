"""Tests for the Censorship Fear Index (processors/fear_index.py)."""
from datetime import datetime, timezone

from processors.ddti_index import compute_selectivity_novelty
from processors.fear_index import compute_fear_index, fear_band
from scripts.validate_ddti import synth_stream

WIN = dict(current_window_days=2, history_window_days=30)


def _d(ranked):
    return {"ranked": ranked, "scope": "test"}


def test_empty_is_calm_zero():
    fi = compute_fear_index(_d([]))
    assert fi["index"] == 0.0
    assert fi["band"] == "CALM"
    assert fi["velocity_suppressed"] is True


def test_bands_are_ordered():
    assert fear_band(10)[0] == "CALM"
    assert fear_band(30)[0] == "ELEVATED"
    assert fear_band(60)[0] == "HIGH"
    assert fear_band(90)[0] == "SEVERE"


def test_index_and_components_are_bounded():
    fi = compute_fear_index(_d([
        {"term": "四通桥", "domain": "POLITICS", "threat": 9.0, "is_new": True},
        {"term": "白纸", "domain": "POLITICS", "threat": 4.0, "is_new": True},
        {"term": "核酸", "domain": "SAFETY", "threat": 2.0, "is_new": False},
    ]))
    assert 0.0 <= fi["index"] <= 100.0
    for v in fi["components"].values():
        assert 0.0 <= v <= 1.0


def test_calm_scores_below_acute():
    calm = _d([{"term": "腐败", "domain": "POLITICS", "threat": 1.0, "is_new": False},
               {"term": "拆迁", "domain": "SOCIETY", "threat": 0.9, "is_new": False}])
    acute = _d([{"term": "四通桥", "domain": "POLITICS", "threat": 9.0, "is_new": True},
                {"term": "彭载舟", "domain": "POLITICS", "threat": 4.0, "is_new": True}])
    assert compute_fear_index(calm)["index"] < compute_fear_index(acute)["index"]


def test_deterministic():
    d = _d([{"term": "白纸", "domain": "POLITICS", "threat": 5.0, "is_new": True}])
    now = datetime(2022, 11, 27, tzinfo=timezone.utc)
    assert compute_fear_index(d, now=now) == compute_fear_index(d, now=now)


def test_velocity_folds_in_and_clears_suppressed():
    d = _d([{"term": "四通桥", "domain": "POLITICS", "threat": 6.0, "is_new": True}])
    base = compute_fear_index(d)
    withv = compute_fear_index(d, velocity=0.9)
    assert withv["velocity_suppressed"] is False
    assert "velocity" in withv["components"]
    assert withv["index"] > base["index"]  # a fast-deletion vantage raises alarm


def test_real_event_far_exceeds_its_baseline():
    """The index spikes on a documented event vs. its own quiet baseline."""
    event = {"name": "Sitong Bridge",
             "signature_terms": ["四通桥", "彭载舟", "勇士"],
             "born_terms": ["四通桥", "彭载舟", "勇士"]}
    now = datetime(2022, 10, 13, 20, tzinfo=timezone.utc)
    full = synth_stream(event, now)
    base = [o for o in full if o["source"] == "synthetic-baseline"]
    di_event = compute_selectivity_novelty(full, now, **WIN)
    di_base = compute_selectivity_novelty(base, now, **WIN)
    fi_event = compute_fear_index(di_event)["index"]
    fi_base = compute_fear_index(di_base)["index"]
    assert fi_event > fi_base + 20, f"event {fi_event} should dwarf baseline {fi_base}"
