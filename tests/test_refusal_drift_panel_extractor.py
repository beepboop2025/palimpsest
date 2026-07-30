"""The refusal-drift signal must read the shape the puller actually writes.

`scripts/refusal_drift_pull.py` moved `suppression_rate_pct` out of the record's
top level and under a per-model `models` map. The event detector kept reading
the top level, so every panel reading extracted to None and the whole signal ran
off the single legacy flat row — a one-point history that can never flag
anything, presented on the board as a live monitored signal.

These tests pin the reader to both shapes: the panel mean for panel rows, and an
honest abstention (never a zero) for anything that cannot be compared.
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


def test_partial_panel_averages_only_the_members_that_reported():
    r = {"models": {"a": {"suppression_rate_pct": 4.0},
                    "b": {"drift": 0.1},
                    "c": {"suppression_rate_pct": 8.0}}}
    assert refusal_suppression_rate(r) == pytest.approx(6.0)


# ── wiring and the committed history ───────────────────────────────────────────

def test_registry_uses_the_panel_extractor():
    assert SIGNALS["refusal_drift"][1] is refusal_suppression_rate
    assert SIGNALS["refusal_drift"][0] == "refusal-drift-history.jsonl"


@pytest.mark.skipif(not HISTORY.exists(), reason="history not present")
def test_committed_history_yields_every_panel_reading():
    """Regression on the silent discard: the series must be the panel rows, not
    the one legacy row that used to be all this signal saw."""
    rows = [json.loads(line) for line in HISTORY.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected = [refusal_suppression_rate(r) for r in rows]
    expected = [v for v in expected if v is not None]
    series = _load_series(READINGS, *SIGNALS["refusal_drift"][:2])
    assert series == pytest.approx(expected)
    assert len(series) > 1, "the signal is back to running on a single reading"
    # every row that carries a populated panel must survive to the detector
    panels = sum(1 for r in rows if isinstance(r.get("models"), dict) and r["models"])
    assert len(series) == panels
