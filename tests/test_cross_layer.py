"""Cross-layer lead/lag: the null has to survive autocorrelation.

The whole reason this module exists in this form is that cross-correlating two
autocorrelated series manufactures significance. The tests that matter most are
therefore the negative ones: two unrelated random walks must NOT be reported as
a timing relationship, however strongly they happen to correlate.
"""
import json
import random

from processors.cross_layer import (
    ALPHA,
    MAX_LAG_DAYS,
    MIN_OVERLAP,
    analyse_pair,
    build_reading,
    load_daily,
    required_overlap,
)


def _walk(n, seed, step=1.0):
    rng = random.Random(seed)
    v, out = 0.0, []
    for _ in range(n):
        v += rng.gauss(0, step)
        out.append(v)
    return out


# ── the null ────────────────────────────────────────────────────────────────────

def test_two_unrelated_random_walks_are_not_called_a_relationship():
    """The trap: independent random walks correlate strongly by construction.
    A naive test calls this significant; the circular-shift null must not."""
    a, b = _walk(120, seed=1), _walk(120, seed=2)
    r = analyse_pair(a, b)
    assert r["ok"]
    # the observed correlation is typically large in absolute terms...
    # ...but must not be significant against a null built from the same walks
    assert r["p_value"] > ALPHA, (r["correlation"], r["p_value"])


def test_the_null_shows_why_levels_cannot_be_used_directly():
    """On LEVELS, rotated random walks still correlate strongly — which is the
    autocorrelation trap, and why a naive test is confidently wrong. On
    DIFFERENCES that structure is gone, which is why differencing is the fix."""
    a, b = _walk(120, seed=7), _walk(120, seed=8)
    on_levels = analyse_pair(a, b, differenced=False)
    on_diffs = analyse_pair(a, b, differenced=True)
    assert on_levels["null_median_abs_r"] > 0.2
    assert on_diffs["null_median_abs_r"] < on_levels["null_median_abs_r"]


def test_a_genuine_lagged_relationship_is_recovered():
    """b is a is shifted by 3 days plus noise — the lag must be found."""
    rng = random.Random(4)
    base = _walk(200, seed=11)
    lag = 3
    a = base
    b = [0.0] * lag + [v + rng.gauss(0, 0.05) for v in base[:-lag]]
    r = analyse_pair(a, b)
    assert r["ok"]
    assert r["lag_days"] == lag, r
    assert abs(r["correlation"]) > 0.9
    assert r["p_value"] <= ALPHA


def test_lag_sign_convention_means_first_series_leads():
    rng = random.Random(6)
    base = _walk(200, seed=21)
    b = [0.0] * 2 + [v + rng.gauss(0, 0.05) for v in base[:-2]]
    r = analyse_pair(base, b)
    assert r["lag_days"] > 0, "positive lag must mean the FIRST series leads"


# ── gating and honesty ──────────────────────────────────────────────────────────

def test_short_overlap_is_refused():
    a, b = _walk(MIN_OVERLAP - 1, 3), _walk(MIN_OVERLAP - 1, 4)
    r = analyse_pair(a, b)
    assert r["ok"] is False and "need" in r["reason"]


def test_underpowered_pair_states_the_attainable_p_floor():
    """A pair that cannot reach alpha must say so numerically, not just fail."""
    a, b = _walk(22, 5), _walk(22, 6)
    r = analyse_pair(a, b)
    assert r["ok"] is False
    assert "smallest attainable p" in r["reason"]
    assert str(required_overlap()) in r["reason"]


def test_required_overlap_matches_the_shift_arithmetic():
    n = required_overlap()
    a, b = _walk(n, 31), _walk(n, 32)
    r = analyse_pair(a, b)
    assert r["ok"], f"{n}d should be enough to run the test: {r.get('reason')}"
    assert 1.0 / (r["n_shifts"] + 1) <= ALPHA


def test_constant_series_is_refused_not_scored():
    a = [5.0] * 60
    b = _walk(60, 9)
    r = analyse_pair(a, b)
    assert r["ok"] is False


def test_analysis_is_deterministic():
    a, b = _walk(60, 13), _walk(60, 14)
    assert analyse_pair(a, b) == analyse_pair(a, b)


def test_pvalue_is_the_conservative_conformal_form():
    """p = (#{null >= observed} + 1)/(n+1) can never be zero."""
    rng = random.Random(2)
    base = _walk(200, seed=41)
    b = [0.0] * 2 + [v + rng.gauss(0, 0.01) for v in base[:-2]]
    r = analyse_pair(base, b)
    # the reported p is rounded, so compare against the floor with that tolerance
    assert r["p_value"] >= 1.0 / (r["n_shifts"] + 1) - 5e-5
    assert r["p_value"] > 0


# ── daily alignment ─────────────────────────────────────────────────────────────

def test_load_daily_averages_within_a_day(tmp_path):
    path = tmp_path / "h.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for v in (10.0, 20.0):
            fh.write(json.dumps({"generated_at": "2026-07-01T03:00:00Z",
                                 "gfw_index": v}) + "\n")
        fh.write(json.dumps({"generated_at": "2026-07-02T03:00:00Z",
                             "gfw_index": 7.0}) + "\n")
    d = load_daily(tmp_path, "h.jsonl", lambda r: r.get("gfw_index"))
    assert d == {"2026-07-01": 15.0, "2026-07-02": 7.0}


def test_load_daily_prefers_the_data_date_over_generation_time(tmp_path):
    """A row stamped with the date its DATA refers to must align on that date."""
    path = tmp_path / "h.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": "2026-07-01",
                             "generated_at": "2026-07-05T00:00:00Z",
                             "bridge_users": 5.0}) + "\n")
    d = load_daily(tmp_path, "h.jsonl", lambda r: r.get("bridge_users"))
    assert list(d) == ["2026-07-01"]


def test_load_daily_survives_torn_lines(tmp_path):
    path = tmp_path / "h.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": "2026-07-01", "v": 1.0}) + "\n")
        fh.write('{"date": "2026-07-0\n')
        fh.write(json.dumps({"date": "2026-07-02", "v": 2.0}) + "\n")
    d = load_daily(tmp_path, "h.jsonl", lambda r: r.get("v"))
    assert len(d) == 2


# ── whole reading ───────────────────────────────────────────────────────────────

def test_empty_dir_explains_itself_rather_than_looking_broken(tmp_path):
    r = build_reading(tmp_path)
    assert r["n_pairs_tested"] == 0
    assert "not yet earned" in r["headline"]
    assert r["required_overlap_days"] == required_overlap()


def test_reading_marks_itself_preliminary_and_disclaims_cause(tmp_path):
    r = build_reading(tmp_path)
    assert r["preliminary"] is True
    assert any("TIMING, NOT CAUSE" in c for c in r["caveats"])
    assert any("autocorrelated" in c for c in r["caveats"])


def test_within_layer_pairs_are_never_tested(tmp_path):
    """Co-movement inside a layer is expected and uninformative here."""
    from processors.board_alarm import LAYERS
    rows_a = [{"date": f"2026-07-{d:02d}", "gfw_index": float(d)} for d in range(1, 29)]
    rows_b = [{"date": f"2026-07-{d:02d}", "cn_interference_rate_pct": float(d)}
              for d in range(1, 29)]
    for name, rows in (("ooni-gfw-history.jsonl", rows_a),
                       ("censored-planet-history.jsonl", rows_b)):
        with open(tmp_path / name, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    # both are network-layer signals
    assert "ooni_gfw" in LAYERS["network"] and "censored_planet" in LAYERS["network"]
    out = build_reading(tmp_path)
    for p in out["pairs"] + out["skipped"]:
        assert set(p["pair"]) != {"ooni_gfw", "censored_planet"}


def test_max_lag_window_is_respected():
    rng = random.Random(8)
    base = _walk(200, seed=51)
    r = analyse_pair(base, [v + rng.gauss(0, 0.1) for v in base])
    assert -MAX_LAG_DAYS <= r["lag_days"] <= MAX_LAG_DAYS


# ── calibration: the module's central claim ─────────────────────────────────────

def test_false_positive_rate_on_independent_walks_is_near_nominal():
    """The claim the whole design rests on. Independent random walks must be
    called significant at roughly alpha, not far above it.

    Measured while building this: the naive correlation test fires on 98% of such
    pairs, the shift null on LEVELS on 37%, and the shift null on DIFFERENCES —
    what ships — on 8%. This test guards the last number against regression.
    """
    trials, fp = 60, 0
    for t in range(trials):
        a, b = _walk(80, 1000 + t), _walk(80, 5000 + t)
        r = analyse_pair(a, b)
        if r["ok"] and r["p_value"] <= ALPHA:
            fp += 1
    assert fp / trials <= 0.20, f"false positive rate {fp}/{trials} — null is broken"


def test_levels_are_measurably_worse_calibrated_than_differences():
    """Pins WHY differencing is not optional, so it cannot be removed as noise."""
    trials, fp_levels, fp_diff = 40, 0, 0
    for t in range(trials):
        a, b = _walk(80, 2000 + t), _walk(80, 9000 + t)
        lv = analyse_pair(a, b, differenced=False)
        df = analyse_pair(a, b, differenced=True)
        fp_levels += bool(lv["ok"] and lv["p_value"] <= ALPHA)
        fp_diff += bool(df["ok"] and df["p_value"] <= ALPHA)
    assert fp_diff < fp_levels, (fp_diff, fp_levels)


def test_differencing_still_recovers_a_real_lag():
    rng = random.Random(77)
    base = _walk(200, seed=61)
    b = [0.0] * 4 + [v + rng.gauss(0, 0.05) for v in base[:-4]]
    r = analyse_pair(base, b)
    assert r["ok"] and r["lag_days"] == 4 and r["p_value"] <= ALPHA


def test_difference_is_the_plain_day_over_day_change():
    from processors.cross_layer import difference
    assert difference([1.0, 3.0, 6.0]) == [2.0, 3.0]
    assert difference([5.0]) == []
