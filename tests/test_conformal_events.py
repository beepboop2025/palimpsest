"""The conformal event layer's honesty is testable: validity (quiet data must
rarely flag, within the stated ARL bound), power (a real shift must flag
promptly), determinism, and the conservative direction of every approximation."""
import json
import random

from processors.conformal_events import (
    ALARM_A,
    WARMUP,
    WATCH_A,
    analyze_series,
    bet,
    build_reading,
    conformal_pvalue,
)


# ── p-value: conservative and rank-exact ────────────────────────────────────────

def test_pvalue_is_super_uniform_under_exchangeability():
    """For an i.i.d. stream, P(p <= a) must be <= a (conservative)."""
    rng = random.Random(5)
    hits = {0.1: 0, 0.25: 0}
    trials = 2000
    for _ in range(trials):
        hist = [rng.random() for _ in range(30)]
        p = conformal_pvalue(hist, rng.random())
        for a in hits:
            hits[a] += p <= a
    for a, k in hits.items():
        assert k / trials <= a + 0.02  # never anti-conservative (small MC slack)


def test_pvalue_extreme_reading_is_smallest_rank():
    assert conformal_pvalue([1.0, 2.0, 3.0], 99.0) == 1 / 4
    assert conformal_pvalue([1.0, 2.0, 3.0], 0.0) == 1.0  # ties/low: maximal p


def test_bet_is_a_decreasing_e_value_shape():
    """b must be decreasing in p (so super-uniform p keeps E[b] <= 1) and each
    mixture component integrates to 1 by the closed form [p^eps]_0^1."""
    ps = [0.001, 0.01, 0.1, 0.3, 0.5, 0.8, 1.0]
    bets = [bet(p) for p in ps]
    assert all(a >= b for a, b in zip(bets, bets[1:]))
    assert bets[-1] < 1.0 < bets[0]


# ── SR e-detector: validity and power ───────────────────────────────────────────

def test_quiet_series_false_alarm_rate_within_arl_bound():
    """Under no change, P(false alarm within n readings) <= n/ALARM_A.
    n=80 -> bound 16%; empirically Gaussian quiet data sits far below it."""
    rng = random.Random(11)
    alarms = 0
    trials = 300
    for _ in range(trials):
        series = [rng.gauss(50.0, 5.0) for _ in range(80)]
        alarms += bool(analyze_series(series)["alarm_indices"])
    assert alarms / trials <= 80 / ALARM_A  # the stated guarantee, exactly


def test_level_shift_is_alarmed_promptly():
    rng = random.Random(7)
    series = [rng.gauss(50.0, 3.0) for _ in range(40)] + \
             [rng.gauss(75.0, 3.0) for _ in range(15)]
    r = analyze_series(series)
    assert r["alarm_indices"], "a clean 8-sigma level shift must alarm"
    assert min(r["alarm_indices"]) >= 40   # never before the shift
    assert min(r["alarm_indices"]) <= 52   # and within ~12 readings of it


def test_calm_years_do_not_blind_the_detector():
    """The SR additive floor is the point: a LONG quiet stretch must not slow
    detection of the eventual shift (a pure product martingale fails this)."""
    rng = random.Random(13)
    long_calm = [rng.gauss(50.0, 3.0) for _ in range(400)] + \
                [rng.gauss(75.0, 3.0) for _ in range(15)]
    r = analyze_series(long_calm)
    post = [i for i in r["alarm_indices"] if i >= 400]
    assert post and min(post) <= 412


def test_warmup_never_flags():
    r = analyze_series([1.0, 2.0, 3.0])
    assert set(r["states"]) == {"warming_up"}
    assert not r["alarm_indices"]


def test_reset_rearms_after_alarm():
    """After an alarm the post-alarm level becomes the new null: given a
    decent post-alarm reference, a SECOND shift must be alarmed again.
    (A thin reference floors the p-value at 1/(n+1), so the detector honestly
    caps out at 'watch' — hence the longer middle segment here.)"""
    rng = random.Random(3)
    series = (
        [rng.gauss(10, 1) for _ in range(60)]
        + [rng.gauss(30, 1) for _ in range(80)]   # first shift
        + [rng.gauss(60, 1) for _ in range(25)]   # second shift
    )
    r = analyze_series(series)
    assert len(r["alarm_indices"]) >= 2
    assert any(i >= 140 for i in r["alarm_indices"])


def test_deterministic():
    series = [float(i % 7) for i in range(60)]
    assert analyze_series(series) == analyze_series(series)


def test_thresholds_sane():
    assert WATCH_A < ALARM_A
    assert WARMUP >= 5


# ── reading over real repo files ────────────────────────────────────────────────

def test_build_reading_over_fixture_dir(tmp_path):
    # Rows carry timestamps because the board now bounds recency: an undated history
    # cannot be shown to be current, so it reads "stale" rather than "calm". The fixture
    # was always implicitly claiming these rows were recent; now it says so.
    from datetime import datetime, timedelta, timezone
    _now = datetime.now(timezone.utc)
    quiet = [{"generated_at": (_now - timedelta(hours=(29 - i) * 6)).isoformat(),
              "gfw_index": 50 + (i % 3)} for i in range(30)]
    with open(tmp_path / "ooni-gfw-history.jsonl", "w") as fh:
        for r in quiet:
            fh.write(json.dumps(r) + "\n")
    reading = build_reading(tmp_path)
    s = reading["signals"]["ooni_gfw"]
    assert s["n"] == 30
    assert s["state"] in ("calm", "watch")  # quiet fixture must not alarm
    assert reading["signals"]["ddti_threat"]["state"] == "no_data"
    assert "average readings to a false flag" in reading["guarantee"]


def test_build_reading_survives_torn_lines(tmp_path):
    with open(tmp_path / "ooni-gfw-history.jsonl", "w") as fh:
        fh.write('{"gfw_index": 50}\n{not json\n{"gfw_index": 51}\n')
    reading = build_reading(tmp_path)
    assert reading["signals"]["ooni_gfw"]["n"] == 2


def test_reading_on_live_repo_readings():
    """The real readings/ directory must produce a well-formed reading with at
    least one populated signal (the repo ships its own histories)."""
    import os
    readings = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "readings")
    reading = build_reading(readings)
    populated = [k for k, v in reading["signals"].items() if v.get("n", 0) > 0]
    assert populated, "no signal history found in readings/"
    assert isinstance(reading["headline"], str)


# ── two-sided detection, drift weighting, and reported effect ────────────────────
# Added with the two-sided upgrade: the detector used to score only unusually HIGH
# readings, so a collapsing signal was invisible. A collapse is an event — OONI's
# anomaly rate falling can mean measurement itself is being blocked.

def test_downward_collapse_is_detected():
    rng = random.Random(3)
    series = [rng.gauss(60.0, 3.0) for _ in range(40)] + \
             [rng.gauss(20.0, 3.0) for _ in range(15)]
    r = analyze_series(series)
    assert r["alarm_indices"], "a clean downward collapse must alarm"
    assert r["driver_side"] == "down"


def test_upward_only_detector_is_blind_to_a_collapse():
    """Pins the gap the upgrade closed, so it cannot silently reopen."""
    rng = random.Random(3)
    series = [rng.gauss(60.0, 3.0) for _ in range(40)] + \
             [rng.gauss(20.0, 3.0) for _ in range(15)]
    assert analyze_series(series, two_sided=False)["alarm_indices"] == []


def test_two_sided_still_catches_an_upward_shift():
    rng = random.Random(7)
    series = [rng.gauss(50.0, 3.0) for _ in range(40)] + \
             [rng.gauss(75.0, 3.0) for _ in range(15)]
    r = analyze_series(series)
    assert r["alarm_indices"] and r["driver_side"] == "up"


def test_two_sided_thresholds_pay_the_union_bound():
    """Two detectors are run, so each threshold doubles and the FAMILY-WISE
    false-alarm guarantee stays exactly the published one-sided guarantee."""
    r2 = analyze_series([float(i % 5) for i in range(30)])
    r1 = analyze_series([float(i % 5) for i in range(30)], two_sided=False)
    assert r2["alarm_at"] == ALARM_A * 2 and r2["watch_at"] == WATCH_A * 2
    assert r1["alarm_at"] == ALARM_A and r1["watch_at"] == WATCH_A


def test_two_sided_quiet_series_respects_the_arl_bound():
    """The union-bound correction has to hold empirically, not just on paper."""
    rng = random.Random(23)
    alarms = 0
    trials = 300
    for _ in range(trials):
        series = [rng.gauss(50.0, 5.0) for _ in range(80)]
        alarms += bool(analyze_series(series)["alarm_indices"])
    assert alarms / trials <= 80 / ALARM_A


def test_effect_reports_direction_and_size():
    series = [50.0 + (i % 3) * 0.1 for i in range(30)] + [95.0]
    eff = analyze_series(series)["effect"]
    assert eff["direction"] == "up"
    assert eff["delta"] > 40
    assert eff["robust_z"] > 3
    assert 0 <= eff["pct_of_past_below"] <= 100


def test_effect_on_a_flat_series_is_flat():
    assert analyze_series([50.0] * 30)["effect"]["direction"] == "flat"


def test_decay_weights_favour_recent_history():
    from processors.conformal_events import decay_weights
    w = decay_weights(5, half_life=2.0)
    assert w[-1] == 1.0 and w[0] < w[-1]
    assert decay_weights(5, None) is None
    assert decay_weights(0, 2.0) is None


def test_weighted_pvalue_discounts_stale_history():
    """With a decaying reference an old regime stops anchoring the null, so a
    reading typical of RECENT history is no longer scored as extreme."""
    from processors.conformal_events import conformal_pvalue, decay_weights
    hist = [10.0] * 20 + [90.0] * 10          # regime changed two-thirds in
    x = 90.0
    unweighted = conformal_pvalue(hist, x, side="up")
    weighted = conformal_pvalue(hist, x, side="up",
                                weights=decay_weights(len(hist), 3.0))
    assert weighted > unweighted


def test_downward_pvalue_is_symmetric_to_upward():
    from processors.conformal_events import conformal_pvalue
    hist = [float(i) for i in range(10)]
    assert conformal_pvalue(hist, -5.0, side="down") < 0.2
    assert conformal_pvalue(hist, -5.0, side="up") > 0.8


def test_weights_must_match_history_length():
    from processors.conformal_events import conformal_pvalue
    try:
        conformal_pvalue([1.0, 2.0], 3.0, weights=[1.0])
    except ValueError:
        return
    raise AssertionError("mismatched weights must fail loudly")


def test_two_sided_is_deterministic():
    series = [float((i * 7) % 13) for i in range(60)]
    assert analyze_series(series) == analyze_series(series)
