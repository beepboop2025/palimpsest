"""Forecast ledger: the observatory scores itself, and cannot quietly stop.

The properties worth pinning are the ones that make a self-published scoreboard
trustworthy: the loop must be strictly prequential (no hindsight), the score must
be proper (not gameable by hedging), the band must actually adapt, and misses
must survive into the output.
"""
import json
import random

from processors.forecast_ledger import (
    LEVELS,
    MIN_HISTORY,
    NOMINAL,
    build_reading,
    interval_score,
    run_prequential,
    wis,
)


# ── the scoring rule ────────────────────────────────────────────────────────────

def test_interval_score_rewards_covering_and_narrow():
    wide = interval_score(10.0, 0.0, 20.0, 0.2)
    narrow = interval_score(10.0, 9.0, 11.0, 0.2)
    assert narrow < wide, "among covering intervals, narrower must score better"


def test_interval_score_penalises_a_miss_by_distance():
    near = interval_score(12.0, 9.0, 11.0, 0.2)
    far = interval_score(30.0, 9.0, 11.0, 0.2)
    assert far > near > interval_score(10.0, 9.0, 11.0, 0.2)


def test_interval_score_penalty_scales_with_confidence():
    """Missing a 95% interval must hurt more than missing a 50% one."""
    tight = interval_score(20.0, 9.0, 11.0, 0.05)
    loose = interval_score(20.0, 9.0, 11.0, 0.50)
    assert tight > loose


def test_wis_is_minimised_by_an_honest_forecast():
    """A proper rule: the truthful forecast must not be beatable by hedging."""
    y = 10.0
    honest = wis(y, 10.0, {lv: (10.0 - 1, 10.0 + 1) for lv in LEVELS})
    biased = wis(y, 16.0, {lv: (16.0 - 1, 16.0 + 1) for lv in LEVELS})
    assert honest < biased


def test_wis_with_no_intervals_falls_back_to_absolute_error():
    assert wis(10.0, 7.0, {}) == 3.0


# ── the prequential loop ────────────────────────────────────────────────────────

def test_short_series_is_not_scored():
    r = run_prequential([1.0] * (MIN_HISTORY - 1))
    assert r["scoreable"] is False
    assert "needs" in r["reason"]


def test_calibration_on_a_stationary_series_is_near_nominal():
    rng = random.Random(11)
    series = [rng.gauss(50.0, 4.0) for _ in range(400)]
    r = run_prequential(series)
    assert r["scoreable"]
    # ACI targets NOMINAL long-run coverage; allow honest slack on 400 points
    assert abs(r["empirical_coverage"] - NOMINAL) < 0.12, r["empirical_coverage"]


def test_band_adapts_to_a_variance_explosion():
    """After the series gets wilder the band must widen, or coverage collapses.
    This is the property a fixed-width band cannot deliver."""
    rng = random.Random(5)
    calm = [rng.gauss(50.0, 1.0) for _ in range(150)]
    wild = [rng.gauss(50.0, 12.0) for _ in range(250)]
    r = run_prequential(calm + wild)
    assert r["scoreable"]
    assert r["empirical_coverage"] > 0.6, "band failed to adapt to the new regime"


def test_misses_are_recorded_and_ranked():
    rng = random.Random(3)
    series = [rng.gauss(50.0, 2.0) for _ in range(120)]
    series[100] = 500.0                       # one enormous outlier
    r = run_prequential(series)
    assert r["n_misses"] >= 1
    assert r["worst_misses"], "a large miss must appear in the published list"
    assert r["worst_misses"][0]["miss_by"] > 0
    assert r["worst_misses"][0]["covered"] is False
    # the list is ranked worst-first
    by = [m["miss_by"] for m in r["worst_misses"]]
    assert by == sorted(by, reverse=True)


def test_forecast_never_uses_the_value_it_predicts():
    """Prequential discipline: the point forecast for t is the value at t-1.
    If hindsight ever leaked in, this identity would break."""
    series = [float(i * 3 % 17) for i in range(60)]
    r = run_prequential(series)
    for rec in r["worst_misses"]:
        assert rec["forecast"] == round(series[rec["t"] - 1], 4)


def test_scoring_is_deterministic():
    series = [float((i * 13) % 29) for i in range(80)]
    assert run_prequential(series) == run_prequential(series)


def test_baseline_comparison_is_reported_both_ways():
    """The ledger must be able to say its own method LOST."""
    rng = random.Random(17)
    r = run_prequential([rng.gauss(10.0, 2.0) for _ in range(200)])
    assert "beats_baseline" in r and isinstance(r["beats_baseline"], bool)
    assert r["wis_baseline_fixed_quantile"] > 0
    assert r["skill_vs_baseline"] is not None


# ── whole reading ───────────────────────────────────────────────────────────────

def _write(tmp_path, filename, rows):
    with open(tmp_path / filename, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_build_reading_scores_a_real_looking_series(tmp_path):
    rng = random.Random(2)
    _write(tmp_path, "ooni-gfw-history.jsonl",
           [{"gfw_index": rng.gauss(55.0, 3.0)} for _ in range(120)])
    r = build_reading(tmp_path)
    assert r["n_signals_scored"] == 1
    assert r["n_forecasts"] > 50
    assert r["pooled_empirical_coverage"] is not None
    assert "band" in r["headline"]


def test_transition_logs_are_excluded_not_scored(tmp_path):
    _write(tmp_path, "weibo-hotsearch-history.jsonl",
           [{"suppressed_invisible": i % 3} for i in range(50)])
    r = build_reading(tmp_path)
    assert "weibo_suppression" in r["excluded"]
    assert "transitions" in r["excluded"]["weibo_suppression"]
    assert "weibo_suppression" not in r["signals"]


def test_thin_signals_are_named_as_excluded_not_silently_dropped(tmp_path):
    _write(tmp_path, "ooni-gfw-history.jsonl", [{"gfw_index": 50.0} for _ in range(5)])
    r = build_reading(tmp_path)
    assert "ooni_gfw" in r["excluded"]
    assert "needs" in r["excluded"]["ooni_gfw"]


def test_reading_declares_what_it_cannot_yet_score(tmp_path):
    r = build_reading(tmp_path)
    assert "forecaster.py" in r["not_yet_scoreable"]
    assert any("misses are published" in c for c in r["caveats"])
    assert "prequential" in r["method"]


def test_empty_dir_says_so_rather_than_claiming_success(tmp_path):
    r = build_reading(tmp_path)
    assert r["n_signals_scored"] == 0
    assert "no signal has enough" in r["headline"]
