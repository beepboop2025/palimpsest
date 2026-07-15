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
    quiet = [{"gfw_index": 50 + (i % 3)} for i in range(30)]
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
