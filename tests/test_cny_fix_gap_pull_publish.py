"""CNY fix-gap driver — the no-fix-no-gap rule and publication timing.

The one manufactured number this driver could produce: a "gap" on a Chinese
holiday, when the ECB prints a CNY reference but the PBOC never set a fix.
That gap would be pure calendar artefact, so the driver abstains — and the
abstention must leave the previous reading untouched, not restamp it.

As everywhere in the fleet: the reading is rewritten unconditionally on
every round that survives the gates (no write-if-changed on OUT — adding
one would look like a tidy-up and must not be done); the history is the
movement record, keyed by DATA date, replace-if-changed.

Offline: both legs are stubbed; the clock is stubbed at the daily cadence.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.cny_fix_gap_pull as pull

DAY = "2026-07-31"
SPOT = {"date": DAY, "eur_usd": 1.1485, "eur_cny": 7.7539,
        "usdcny": 6.751415, "usdcny_boc": 6.751203, "cross_check_diff": 0.000212}
FIX = 6.7894


class _Clock:
    """Advances a day per round, matching the daily cron."""

    def __init__(self) -> None:
        self.t = datetime(2026, 7, 31, 17, 3, 0, tzinfo=timezone.utc)

    def now(self, tz=None) -> datetime:
        self.t += timedelta(hours=24)
        return self.t


@pytest.fixture
def publish(tmp_path, monkeypatch):
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "cny-fix-gap-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "cny-fix-gap-history.jsonl"))
    monkeypatch.setattr(pull, "CHINA_ECON_HIST", str(tmp_path / "china-econ-history.jsonl"))
    monkeypatch.setattr(pull, "datetime", _Clock())

    def run(spot, parity_rows):
        with open(pull.CHINA_ECON_HIST, "w", encoding="utf-8") as f:
            for date, v in parity_rows.items():
                f.write(json.dumps({"date": date, "usdcny_parity": v}) + "\n")
        monkeypatch.setattr(pull, "fetch_spot", lambda: spot)
        pull.main()
        return _reading(tmp_path)

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "cny-fix-gap-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "cny-fix-gap-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_the_gap_is_signed_and_scaled_to_the_fix(publish):
    run, _ = publish
    got = run(dict(SPOT), {DAY: FIX})

    assert got["method_version"] == pull.METHOD_VERSION
    assert got["gap"] == pytest.approx(6.751415 - FIX, abs=1e-6)
    assert got["gap_pct"] == pytest.approx(100 * (6.751415 - FIX) / FIX, abs=1e-3)
    assert got["gap_pct"] < 0, "spot below fix must read negative, not absolute"
    assert got["cross_check_diff"] == pytest.approx(0.000212, abs=1e-6)


def test_an_unchanged_round_still_refreshes_the_observation_time(publish):
    run, _ = publish
    first = run(dict(SPOT), {DAY: FIX})
    second = run(dict(SPOT), {DAY: FIX})

    assert second["generated_at"] > first["generated_at"]


def test_a_missing_cross_check_is_an_explicit_null_not_a_vanished_key(publish):
    """The cross-check exists to make a silent break in either source
    visible; its own absence must be visible too."""
    run, _ = publish
    solo = {k: v for k, v in SPOT.items()
            if k not in ("usdcny_boc", "cross_check_diff")}
    got = run(solo, {DAY: FIX})

    assert "cross_check_diff" in got
    assert got["cross_check_diff"] is None


def test_no_fix_means_no_gap_and_no_restamp(publish):
    """The Chinese-holiday case: ECB prints CNY, the PBOC set no fix."""
    run, tmp_path = publish
    first = run(dict(SPOT), {DAY: FIX})
    run({**SPOT, "date": "2026-10-01"}, {DAY: FIX})   # National Day, no parity

    after = _reading(tmp_path)
    assert after["generated_at"] == first["generated_at"]
    assert len(_history(tmp_path)) == 1


def test_a_dead_spot_leg_abstains(publish):
    run, tmp_path = publish
    first = run(dict(SPOT), {DAY: FIX})
    run(None, {DAY: FIX})

    assert _reading(tmp_path)["generated_at"] == first["generated_at"]
    assert len(_history(tmp_path)) == 1


def test_history_is_keyed_by_data_date_and_corrects_in_place(publish):
    run, tmp_path = publish
    run(dict(SPOT), {DAY: FIX})
    run({**SPOT, "usdcny": 6.7601}, {DAY: FIX})   # revised leg, same date

    rows = _history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["usdcny_spot_ecb"] == 6.7601
