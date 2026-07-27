"""Coverage guard: did the signal move, or did our ability to measure it move?

The guard exists because of a live case on 2026-07-27: OONI's index fell with a
robust z of -2.9 while the measurements behind it fell 41%. These tests pin the
distinction the guard has to draw, in both directions — it must not wave through
a coverage artifact as censorship, and it must not explain away a real move.
"""
import json

from processors.coverage_guard import (
    GOOD_FIT_R,
    MIN_PAIRS,
    MOVE_Z,
    assess,
    build_reading,
)


def test_short_history_abstains_rather_than_guessing():
    r = assess([1.0] * 5, [10.0] * 5)
    assert r["verdict"] == "INSUFFICIENT_HISTORY"
    assert r["n_pairs"] == 5


def test_flat_metric_is_no_move():
    metric = [50.0 + (i % 3) for i in range(30)]
    denom = [1000.0 + (i % 5) for i in range(30)]
    assert assess(metric, denom)["verdict"] == "NO_MOVE"


def test_metric_that_is_a_pure_function_of_coverage_is_confounded():
    """The canonical artifact: the metric only moves because the sample did.
    Calling this a censorship change is the exact failure the guard prevents."""
    denom = [1000.0 + 40.0 * ((i * 7) % 11) for i in range(40)]
    metric = [0.05 * d for d in denom]          # perfectly determined by coverage
    denom.append(300.0)                          # coverage collapses
    metric.append(0.05 * 300.0)                  # metric follows it down, exactly
    r = assess(metric, denom)
    assert r["verdict"] == "COVERAGE_CONFOUNDED", r
    assert abs(r["fit"]["correlation"]) > GOOD_FIT_R
    assert "NOT evidence of a censorship change" in r["note"]


def test_real_move_with_steady_coverage_is_confirmed():
    """A metric that jumps while its sample size holds steady is the clean case."""
    denom = [1000.0 + (i % 4) for i in range(40)]
    metric = [50.0 + (i % 3) * 0.1 for i in range(40)]
    denom.append(1001.0)
    metric.append(95.0)                          # a genuine, large departure
    r = assess(metric, denom)
    assert r["verdict"] == "CONFIRMED", r
    assert r["metric_robust_z"] > MOVE_Z


def test_move_survives_partial_coverage_explanation():
    """When coverage explains part but not the bulk of the move, the verdict is
    CONFIRMED and the attenuation is reported rather than hidden."""
    denom = [1000.0 + 30.0 * ((i * 5) % 9) for i in range(40)]
    metric = [0.02 * d + (i % 3) * 0.05 for i, d in enumerate(denom)]
    denom.append(900.0)
    metric.append(0.02 * 900.0 + 12.0)           # big residual on top of coverage
    r = assess(metric, denom)
    assert r["verdict"] == "CONFIRMED"
    assert r["residual_robust_z"] is not None
    assert "conditional z" in r["note"]


def test_uninformative_fit_does_not_license_explaining_the_move_away():
    """If coverage does not track the metric historically, it cannot be the
    explanation — even when the denominator happens to have moved too."""
    denom = [1000.0 + 200.0 * ((i * 3) % 7) for i in range(40)]
    metric = [50.0 + (i % 2) * 0.2 for i in range(40)]   # unrelated to denom
    denom.append(100.0)
    metric.append(90.0)
    r = assess(metric, denom)
    assert r["verdict"] == "CONFIRMED"
    assert abs(r["fit"]["correlation"]) < GOOD_FIT_R


def test_constant_denominator_is_inconclusive_not_confirmed():
    denom = [1000.0] * 40
    metric = [50.0 + (i % 3) * 0.1 for i in range(40)]
    denom.append(1000.0)
    metric.append(95.0)
    r = assess(metric, denom)
    # a constant denominator cannot explain anything, so the move stands
    assert r["verdict"] in ("CONFIRMED", "INCONCLUSIVE")


def test_verdicts_always_carry_a_note_and_pair_count():
    cases = [
        ([1.0] * 5, [1.0] * 5),
        ([50.0 + (i % 3) for i in range(30)], [1000.0] * 30),
    ]
    for m, d in cases:
        r = assess(m, d)
        assert r["n_pairs"] >= 0 and r["verdict"]
        assert "note" in r


def test_assess_is_deterministic():
    denom = [1000.0 + (i * 13) % 97 for i in range(40)]
    metric = [0.03 * d for d in denom]
    assert assess(metric, denom) == assess(metric, denom)


# ── whole reading ───────────────────────────────────────────────────────────────

def _write(tmp_path, filename, rows):
    with open(tmp_path / filename, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_build_reading_flags_a_confounded_signal(tmp_path):
    rows = []
    for i in range(30):
        n = 200000 + 5000 * ((i * 7) % 11)
        rows.append({"gfw_index": n / 4000.0, "n_measurements": n})
    rows.append({"gfw_index": 60000 / 4000.0, "n_measurements": 60000})
    _write(tmp_path, "ooni-gfw-history.jsonl", rows)
    r = build_reading(tmp_path)
    assert "ooni_gfw" in r["confounded"]
    assert "do not read as censorship change" in r["headline"]


def test_build_reading_survives_torn_lines(tmp_path):
    path = tmp_path / "ooni-gfw-history.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(20):
            fh.write(json.dumps({"gfw_index": 50.0 + i % 3,
                                 "n_measurements": 1000 + i}) + "\n")
        fh.write('{"gfw_index": 51.0, "n_meas\n')      # torn write
    r = build_reading(tmp_path)
    assert r["signals"]["ooni_gfw"]["n_pairs"] == 20


def test_signals_without_a_recorded_denominator_are_absent_not_assumed_clean(tmp_path):
    from processors.coverage_guard import GUARDED
    assert "censored_planet" not in GUARDED
    assert "tor_bridge_cn" not in GUARDED


def test_reading_carries_method_and_caveats(tmp_path):
    _write(tmp_path, "ooni-gfw-history.jsonl",
           [{"gfw_index": 50.0, "n_measurements": 1000} for _ in range(15)])
    r = build_reading(tmp_path)
    assert "conditioning" in r["method"] or "condition" in r["method"]
    assert any("not a causal claim" in c for c in r["caveats"])
    assert r["signals"]["ooni_gfw"]["denominator_is"]


def test_min_pairs_is_a_real_gate():
    assert MIN_PAIRS >= 10
