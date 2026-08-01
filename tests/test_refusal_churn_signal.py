"""The model layer must run 24/7 on the live instrument, honestly.

The v1 conformal refusal series closed at the 2026-08-01 method break, and the board's
model layer was keyed solely to it — so the layer was headed for a permanent "stale"
that would read as breakage over a deliberate retirement, while the v2 suite's own
anytime-valid churn monitor sat unread. These tests pin the replacement end to end:
processors/refusal_churn.py reads the per-run churn log, panel-merges per-model
e-values by arithmetic mean, and board_alarm feeds the merged value into e-BH and the
layer machinery — while the closed v1 series contributes nothing to any merge, ever.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.eval_stats import CHURN_BURN_IN, E_ALARM, E_WATCH
from processors import refusal_churn
from processors.board_alarm import build_reading
from processors.refusal_churn import MAX_AGE_HOURS, read_signal

NOW = datetime.now(timezone.utc)
FP = "sha256:judge-current"


def _row(i, model, flips, compared, *, fp=FP, method=2, age_h=None):
    ts = (NOW - timedelta(hours=age_h if age_h is not None else 0,
                          minutes=(200 - i))).isoformat()
    return {"ts": ts, "model": model, "method_version": method,
            "judge_fingerprint": fp, "flips": flips, "compared": compared}


def _write_log(tmp_path, rows):
    with open(tmp_path / refusal_churn.HISTORY_FILE, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _pairs(model, seq, **kw):
    return [_row(i, model, f, c, **kw) for i, (f, c) in enumerate(seq)]


QUIET = [(0, 14)] * (CHURN_BURN_IN + 10)          # calibrates, then stays at baseline
LOUD = [(0, 14)] * CHURN_BURN_IN + [(10, 14)] * 10   # calibrates quiet, then erupts


# ── the signal on its own ──────────────────────────────────────────────────────

def test_missing_log_is_no_data_with_the_reason_said(tmp_path):
    r = read_signal(tmp_path)
    assert r["state"] == "no_data" and r["e"] is None
    assert "two runs" in r["reason"]


def test_old_rows_are_stale_never_calm(tmp_path):
    _write_log(tmp_path, _pairs("m/a", QUIET, age_h=MAX_AGE_HOURS + 10))
    r = read_signal(tmp_path)
    assert r["state"] == "stale" and r["e"] is None
    assert "bound" in r["reason"]


def test_under_burn_in_is_calibrating_with_progress_not_a_number(tmp_path):
    _write_log(tmp_path, _pairs("m/a", [(0, 14)] * 5))
    r = read_signal(tmp_path)
    assert r["state"] == "calibrating" and r["e"] is None
    assert f"5 of {CHURN_BURN_IN + 1}" in r["reason"]


def test_baseline_churn_reads_quiet(tmp_path):
    _write_log(tmp_path, _pairs("m/a", QUIET))
    r = read_signal(tmp_path)
    assert r["state"] == "quiet"
    assert r["e"] is not None and r["e"] < E_WATCH


def test_a_real_shift_alarms_and_the_guarantee_travels_with_it(tmp_path):
    """Ten runs at ~70% churn against a ~1% calibrated null is not a borderline
    case; if this does not alarm, nothing ever will."""
    _write_log(tmp_path, _pairs("m/a", LOUD))
    r = read_signal(tmp_path)
    assert r["state"] == "alarm" and r["e"] >= E_ALARM
    assert "lifetime" in r["guarantee"] and "Ville" in r["guarantee"]


def test_panel_e_is_the_arithmetic_mean_and_calibrating_models_sit_out(tmp_path):
    """The merge must be the mean over MONITORED models only (Vovk & Wang — the
    admissible merge under dependence); a model mid-burn-in has no e-value and
    must be excluded rather than entered as anything."""
    rows = (_pairs("m/quiet", QUIET) + _pairs("m/loud", LOUD)
            + _pairs("m/young", [(0, 14)] * 3))
    _write_log(tmp_path, rows)
    r = read_signal(tmp_path)
    ms = r["models"]
    assert ms["m/young"]["state"] == "calibrating"
    monitored = [ms["m/quiet"]["evalue"], ms["m/loud"]["evalue"]]
    assert r["e"] == pytest.approx(sum(monitored) / 2, rel=1e-6)
    assert r["state"] == "alarm"   # the mean of one capped e-value and one tiny one


def test_rows_from_an_older_judge_are_not_this_instrument(tmp_path):
    """A flip measured by a different classifier is OUR change, not the model's.
    Only rows carrying the newest row's (method_version, judge_fingerprint) count,
    mirroring the puller's own filter — so a judge change re-baselines instead of
    manufacturing churn across the boundary."""
    old = _pairs("m/a", LOUD, fp="sha256:judge-retired")
    fresh = _pairs("m/a", [(0, 14)] * 4)
    _write_log(tmp_path, old + fresh)
    r = read_signal(tmp_path)
    assert r["instrument"]["judge_fingerprint"] == FP
    assert r["state"] == "calibrating"          # the loud old-judge rows are gone
    assert r["n_rows"] == 4


# ── the board wiring ───────────────────────────────────────────────────────────

def test_board_carries_the_churn_signal_and_its_alarm_elevates_the_model_layer(tmp_path):
    _write_log(tmp_path, _pairs("m/loud", LOUD))
    r = build_reading(tmp_path)
    assert r["signals"]["refusal_churn"]["state"] == "alarm"
    assert "refusal_churn" in r["recently_fired"]
    assert r["layers"]["model"]["state"] == "elevated"
    assert "refusal_churn" in r["readings_to_days"]


def test_calibrating_churn_contributes_no_evalue_and_the_layer_says_no_data(tmp_path):
    _write_log(tmp_path, _pairs("m/a", [(0, 14)] * 3))
    r = build_reading(tmp_path)
    assert r["signals"]["refusal_churn"]["state"] == "calibrating"
    assert "refusal_churn" not in r["layers"]["model"]["signals"]
    assert r["fdr_selection"]["n_tested"] == 0


def test_closed_v1_series_contributes_nothing_even_with_history_present(tmp_path):
    """The frozen v1 statistic must never sit in the FDR family or steady the
    board mean: closed is an exclusion, not a display state."""
    with open(tmp_path / "refusal-drift-history.jsonl", "w", encoding="utf-8") as fh:
        for i in range(40):
            fh.write(json.dumps({
                "generated_at": (NOW - timedelta(minutes=40 - i)).isoformat(),
                "models": {"m/a": {"suppression_rate_pct": 5.0 + (i % 3)}}}) + "\n")
    r = build_reading(tmp_path)
    assert r["signals"]["refusal_drift"]["state"] == "closed"
    assert "refusal_drift" not in r["layers"]["model"]["signals"]
    assert r["fdr_selection"]["n_tested"] == 0
    assert r["board_e_value"] is None


def test_board_treats_a_stale_signal_as_absent_evidence(tmp_path):
    """Board-level stale-before-calm: a conformal signal whose newest row is past
    its bound reports stale and is excluded from every merge — the aggregate is
    the surface a reader checks first, so it is the worst place to let a dead
    collector keep testifying."""
    with open(tmp_path / "ooni-gfw-history.jsonl", "w", encoding="utf-8") as fh:
        for i in range(30):
            fh.write(json.dumps({
                "generated_at": (NOW - timedelta(hours=100, minutes=30 - i)).isoformat(),
                "gfw_index": 50.0}) + "\n")
    r = build_reading(tmp_path)
    assert r["signals"]["ooni_gfw"]["state"] == "stale"
    assert "ooni_gfw" not in r["layers"]["network"]["signals"]
    assert r["fdr_selection"]["n_tested"] == 0
