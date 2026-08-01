"""The refusal-drift signal must read the shape the puller actually writes.

`scripts/refusal_drift_pull.py` moved `suppression_rate_pct` out of the record's
top level and under a per-model `models` map. The event detector kept reading
the top level, so every panel reading extracted to None and the whole signal ran
off the single legacy flat row — a one-point history that can never flag
anything, presented on the board as a live monitored signal.

These tests pin the reader to both shapes: the panel mean for panel rows, and an
honest abstention (never a zero) for anything that cannot be compared.

Method v2 (2026-08-01) then CLOSED the series: the puller retired
`suppression_rate_pct` for `family_refusal_rate_pct`, a different estimand
(refusals over paraphrase families, not single-worded arms), and gates history
appends on label movement. v2 rows must abstain here — splicing them in would
manufacture a level shift (v1 ended at 6.25, the first v2 panel read 0.0) —
and the committed-history regression counts only v1-comparable panels.
"""
import json
from pathlib import Path

import pytest

from processors.conformal_events import (
    SIGNALS,
    _load_series,
    refusal_suppression_rate,
)

READINGS = Path(__file__).resolve().parent.parent / "readings"
HISTORY = READINGS / "refusal-drift-history.jsonl"


def _panel(**rates):
    return {"models": {name: {"suppression_rate_pct": v, "drift": 0.0}
                       for name, v in rates.items()}}


# ── the extractor ──────────────────────────────────────────────────────────────

def test_panel_row_returns_the_mean_across_models():
    r = _panel(a=0.0, b=10.0, c=20.0, d=30.0)
    assert refusal_suppression_rate(r) == pytest.approx(15.0)


def test_single_model_panel_returns_that_model():
    assert refusal_suppression_rate(_panel(a=7.5)) == pytest.approx(7.5)


def test_legacy_flat_row_abstains():
    """The pre-panel row is one model's rate, not the panel's. Splicing it in
    would manufacture a level shift the detector would flag as an event."""
    legacy = {"generated_at": "2026-07-11T00:00:00+00:00",
              "model": "openai/gpt-4o-mini",
              "suppression_rate_pct": 12.5, "drift": 0.0, "new_refusals": []}
    assert refusal_suppression_rate(legacy) is None


def test_empty_or_missing_panel_abstains():
    assert refusal_suppression_rate({"models": {}}) is None
    assert refusal_suppression_rate({"generated_at": "x"}) is None
    assert refusal_suppression_rate({"models": None}) is None


def test_unmeasurable_rates_abstain_rather_than_becoming_zero():
    """FAIL LOUD: an audit that measured nothing is not a suppression rate of 0."""
    assert refusal_suppression_rate({"models": {"a": {"drift": 0.0}}}) is None
    assert refusal_suppression_rate({"models": {"a": {"suppression_rate_pct": None}}}) is None
    assert refusal_suppression_rate({"models": {"a": {"suppression_rate_pct": "12.5"}}}) is None
    assert refusal_suppression_rate({"models": {"a": True}}) is None
    # a bool is not a measurement, even though bool is a subclass of int
    assert refusal_suppression_rate({"models": {"a": {"suppression_rate_pct": True}}}) is None


def test_method_v2_row_abstains_because_the_series_is_closed():
    """A v2 row measures family refusal rates, not arm suppression rates.
    Reading `family_refusal_rate_pct` into this series would splice two
    estimands and hand the detector a manufactured level shift; the v2 era
    is watched by the suite's own churn monitor instead."""
    v2 = {"generated_at": "2026-08-01T03:51:31+00:00",
          "method_version": 2,
          "probe_commitment": "sha256:deadbeef",
          "arm": "canonical",
          "judge_fingerprint": "sha256:cafef00d",
          "models": {"openai/gpt-4o-mini": {
              "family_refusal_rate_pct": 0.0, "ci95_pct": [0.0, 10.2],
              "arm_refusal_rate_pct": 0.7, "wording_consistency": 0.98,
              "controls_clean": True, "flips": None, "compared": None,
              "churn_state": "calibrating"}}}
    assert refusal_suppression_rate(v2) is None


def test_partial_panel_averages_only_the_members_that_reported():
    r = {"models": {"a": {"suppression_rate_pct": 4.0},
                    "b": {"drift": 0.1},
                    "c": {"suppression_rate_pct": 8.0}}}
    assert refusal_suppression_rate(r) == pytest.approx(6.0)


# ── wiring and the committed history ───────────────────────────────────────────

def test_registry_uses_the_panel_extractor():
    assert SIGNALS["refusal_drift"][1] is refusal_suppression_rate
    assert SIGNALS["refusal_drift"][0] == "refusal-drift-history.jsonl"


def _comparable_panel(record):
    """A panel this series can read: at least one member reporting a numeric
    v1 `suppression_rate_pct`. Mirrors the extractor's contract so the count
    below keeps meaning 'no comparable row was silently discarded' — v2 rows
    carry `family_refusal_rate_pct` and are excluded on purpose."""
    models = record.get("models")
    if not isinstance(models, dict):
        return False
    return any(isinstance(m, dict)
               and isinstance(m.get("suppression_rate_pct"), (int, float))
               and not isinstance(m.get("suppression_rate_pct"), bool)
               for m in models.values())


@pytest.mark.skipif(not HISTORY.exists(), reason="history not present")
def test_committed_history_yields_every_panel_reading():
    """Regression on the silent discard: the series must be every v1 panel row,
    not the one legacy row that used to be all this signal saw. v2 rows are not
    dropped readings — they are the closed series' successor era."""
    rows = [json.loads(line) for line in HISTORY.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected = [refusal_suppression_rate(r) for r in rows]
    expected = [v for v in expected if v is not None]
    series = _load_series(READINGS, *SIGNALS["refusal_drift"][:2])
    assert series == pytest.approx(expected)
    assert len(series) > 1, "the signal is back to running on a single reading"
    # every row that carries a v1-comparable panel must survive to the detector
    panels = sum(1 for r in rows if _comparable_panel(r))
    assert len(series) == panels
