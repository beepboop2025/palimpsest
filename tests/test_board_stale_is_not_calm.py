"""A dead collector must not read as a calm one.

    PYTHONPATH=. python3 -m pytest tests/test_board_stale_is_not_calm.py -q

processors/conformal_events._load_series returned bare floats, discarding every timestamp.
So a history file that stopped being appended to weeks ago was indistinguishable from one
that is genuinely flat — and a flat series scores as CALM. The board's headline would read
"all signals within their own history" over a collector that had been dead for a fortnight.

That is the fabricated-measurement bug in its most flattering form: not a wrong number, but a
reassuring one, produced by a stage that had no input and never checked. The board is the
surface a journalist or a grant reviewer looks at FIRST, so it is the worst place on the
project for it.

Measured live on 2026-07-31 while writing this: vantage-fusion-history.jsonl was 365 hours
old and cross-layer-history.jsonl 78 hours. Neither is in SIGNALS, but circumvention-demand
was 93 hours old and IS — inside its 120-hour bound, but only just.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from processors.conformal_events import MAX_AGE_HOURS, SIGNALS, build_reading


def _hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def _write_series(d, filename, key, values, age_h):
    """A flat, healthy-looking series whose newest row is `age_h` hours old."""
    with open(d / filename, "w", encoding="utf-8") as f:
        for i, v in enumerate(values):
            stamp = _hours_ago(age_h + (len(values) - 1 - i) * 6)
            f.write(json.dumps({"generated_at": stamp, key: v}) + "\n")


def test_every_registered_signal_declares_a_bound():
    """The registry assertion. A signal added to SIGNALS without a bound silently reverts to
    the old behaviour — read as calm forever — which is exactly the failure this prevents."""
    missing = sorted(set(SIGNALS) - set(MAX_AGE_HOURS))
    assert not missing, f"signals with no staleness bound: {missing}"
    assert all(v > 0 for v in MAX_AGE_HOURS.values())


def test_a_flat_but_fresh_series_is_still_calm(tmp_path):
    """The guard must not turn every quiet signal into an outage."""
    _write_series(tmp_path, "ooni-gfw-history.jsonl", "gfw_index", [50.0] * 30, age_h=1)

    r = build_reading(tmp_path)

    assert r["signals"]["ooni_gfw"]["state"] == "calm"
    assert "ooni_gfw" not in r["stale"]


def test_the_same_series_gone_stale_is_not_calm(tmp_path):
    """Identical values, only older. That is the whole point: the numbers cannot tell you,
    the timestamps can."""
    _write_series(tmp_path, "ooni-gfw-history.jsonl", "gfw_index", [50.0] * 30, age_h=300)

    r = build_reading(tmp_path)
    sig = r["signals"]["ooni_gfw"]

    assert sig["state"] == "stale"
    assert sig["age_hours"] > MAX_AGE_HOURS["ooni_gfw"]
    assert "past the" in sig["reason"]
    assert "ooni_gfw" in r["stale"]


def test_a_stale_signal_is_never_counted_as_elevated(tmp_path):
    """It must leave `active` in both directions: not elevated, and not calm either."""
    _write_series(tmp_path, "ooni-gfw-history.jsonl", "gfw_index",
                  [50.0] * 25 + [99.0], age_h=400)

    r = build_reading(tmp_path)

    assert "ooni_gfw" not in r["active"]
    assert r["signals"]["ooni_gfw"]["state"] == "stale"


def test_undated_rows_cannot_be_shown_to_be_current(tmp_path):
    """A row with no timestamp cannot be dated, and an undatable reading is not a current
    one — the same rule the erasure observatory applies to its layers."""
    with open(tmp_path / "ooni-gfw-history.jsonl", "w", encoding="utf-8") as f:
        for _ in range(30):
            f.write(json.dumps({"gfw_index": 50.0}) + "\n")

    sig = build_reading(tmp_path)["signals"]["ooni_gfw"]

    assert sig["state"] == "stale"
    assert sig["age_hours"] is None
    assert "no usable timestamp" in sig["reason"]


def test_the_headline_says_how_much_of_the_board_was_reporting(tmp_path):
    """'Nothing is elevated' over two live signals is a very different statement from the
    same words over twelve, and the headline used to be identical in both cases."""
    _write_series(tmp_path, "ooni-gfw-history.jsonl", "gfw_index", [50.0] * 30, age_h=1)

    r = build_reading(tmp_path)

    assert r["active"] == []
    assert r["n_reporting"] == 1
    assert r["n_signals"] == len(SIGNALS)
    assert "1 of 12 signals are reporting" in r["headline"]
    assert r["headline"] != "all signals within their own history"


def test_an_empty_board_never_reports_calm(tmp_path):
    """The degenerate case, and the one most likely to be hit by a fresh deployment or a
    broken readings path: nothing to read at all must not produce the all-clear."""
    r = build_reading(tmp_path)

    assert r["n_reporting"] == 0
    assert len(r["no_data"]) == len(SIGNALS)
    assert r["headline"] != "all signals within their own history"
    assert "0 of 12 signals are reporting" in r["headline"]
