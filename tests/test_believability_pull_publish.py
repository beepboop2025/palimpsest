"""Believability driver — the monthly writer's contract.

What is pinned here, beyond the fleet-standard unconditional-reading rule:

  - a missing canonical component is PUBLISHED as missing (named in
    components_missing, counted out of n_components_present) and abstains
    the composite — the reading never quietly reweights;
  - the history row carries NO run timestamp and is keyed by DATA month, so
    a monthly re-run with an unchanged answer leaves the committed file
    byte-identical (the history-side form of the write-if-changed lesson);
  - drift is scored only against PRIOR months' gaps — this month's own gap
    must never sit in its own reference.

Offline: the collector is stubbed; the clock is stubbed at a daily tick
(the workflow is monthly; the tick only needs to move time forward).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.believability_pull as pull

FULL = {"loans_yoy": 9.0, "electricity_yoy": 5.0, "rail_freight_yoy": 2.0}
# composite 6.0; headline 7.0 -> gap 1.0


class _Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 8, 17, 6, 43, 0, tzinfo=timezone.utc)

    def now(self, tz=None) -> datetime:
        self.t += timedelta(hours=24)
        return self.t


@pytest.fixture
def publish(tmp_path, monkeypatch):
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "believability-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "believability-history.jsonl"))
    monkeypatch.setattr(pull, "datetime", _Clock())
    monkeypatch.delenv("BELIEVABILITY_PERIOD", raising=False)

    def run(components, headline, telemetry=None, flags=None):
        monkeypatch.setattr(pull, "collect", lambda period: {
            "period": period,
            "components": dict(components),
            "headline_ip_yoy": headline,
            "telemetry": telemetry or {},
            "flags": flags or [],
        })
        pull.main()
        return _reading(tmp_path)

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "believability-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "believability-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _seed_history(tmp_path, months_gaps):
    with open(tmp_path / "believability-history.jsonl", "w", encoding="utf-8") as f:
        for month, gap in months_gaps:
            f.write(json.dumps({"month": month, "gap": gap, "label": "warming_up",
                                "components": {}, "headline_ip_yoy": None}) + "\n")


def test_first_round_warms_up_and_publishes_the_contract_fields(publish):
    run, _ = publish
    got = run(FULL, 7.0)

    assert got["method_version"] == pull.METHOD_VERSION
    assert got["asof"] == "2026-07"
    assert got["n_components_present"] == 3
    assert got["n_components_required"] == 3
    assert got["components_missing"] == []
    assert got["lkq_composite"] == pytest.approx(6.0)
    assert got["gap"] == pytest.approx(1.0)
    assert got["label"] == "warming_up"
    assert got["status"] == "not_ready"
    assert got["collector_status"] == "observed"
    assert got["analysis_status"] == "warming_up"
    assert got["analysis_ready"] is False
    assert got["n_history_required"] == pull.MIN_HISTORY


def test_a_missing_component_is_named_and_abstains_the_composite(publish):
    run, _ = publish
    partial = {k: v for k, v in FULL.items() if k != "rail_freight_yoy"}
    got = run(partial, 7.0)

    assert got["components_missing"] == ["rail_freight_yoy"]
    assert got["n_components_present"] == 2
    assert got["lkq_composite"] is None
    assert got["label"] == "abstain"
    assert got["status"] == "abstain"
    assert got["collector_status"] == "partial_observation"
    assert got["analysis_status"] == "abstain"
    assert got["analysis_ready"] is False


def test_nothing_collected_publishes_nothing(publish):
    run, tmp_path = publish
    run({}, None)
    assert _reading(tmp_path) is None
    assert _history(tmp_path) == []


def test_an_unchanged_month_rerun_leaves_the_history_byte_identical(publish):
    run, tmp_path = publish
    run(FULL, 7.0)
    before = (tmp_path / "believability-history.jsonl").read_bytes()
    second = run(FULL, 7.0)
    after = (tmp_path / "believability-history.jsonl").read_bytes()

    assert after == before, "same month, same answer — the committed history must not churn"
    assert second is not None, "the reading itself still refreshed"


def test_a_degraded_rerun_cannot_overwrite_a_complete_month(publish):
    """A transient source failure on a re-run must not delete a complete
    month from every future drift baseline: the history keeps the numeric
    gap, while the reading honestly shows this round's degraded look."""
    run, tmp_path = publish
    run(FULL, 7.0)
    partial = {k: v for k, v in FULL.items() if k != "rail_freight_yoy"}
    got = run(partial, 7.0)

    rows = _history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["gap"] == pytest.approx(1.0), (
        "the complete row survives the degraded re-run")
    assert got["label"] == "abstain", "the reading still reports this round's look"


def test_drift_scores_only_against_prior_months(publish):
    run, tmp_path = publish
    _seed_history(tmp_path, [(f"2026-{m:02d}", 1.0) for m in range(1, 7)]
                  + [("2025-11", 1.1), ("2025-12", 0.9)])
    got = run(FULL, 9.5)   # gap 3.5 against a +1.0-ish 8-month baseline

    assert got["n_history"] == 8
    assert got["label"] == "diverging"
    assert got["outside_band"] is True


def test_the_current_month_never_sits_in_its_own_reference(publish):
    """Seeding history that already contains THIS month must not let the
    fresh gap steady its own baseline."""
    run, tmp_path = publish
    _seed_history(tmp_path, [(f"2026-{m:02d}", 1.0) for m in range(1, 7)]
                  + [("2025-11", 1.0), ("2025-12", 1.0), ("2026-07", 3.5)])
    got = run(FULL, 9.5)

    assert got["n_history"] == 8, "the 2026-07 row must be excluded from its own reference"
    assert got["label"] == "diverging"


def test_explicit_completed_period_supports_a_reproducible_backfill(monkeypatch):
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    monkeypatch.setenv("BELIEVABILITY_PERIOD", "2026-06")
    assert pull._target_period(now) == "2026-06"


def test_older_backfill_updates_history_without_regressing_latest(publish,
                                                                  monkeypatch):
    run, tmp_path = publish
    current = run(FULL, 7.0)
    assert current["asof"] == "2026-07"

    monkeypatch.setenv("BELIEVABILITY_PERIOD", "2026-06")
    run(FULL, 7.0)

    assert {row["month"] for row in _history(tmp_path)} == {"2026-06", "2026-07"}
    assert _reading(tmp_path)["asof"] == "2026-07"


def test_period_override_rejects_unreleased_or_malformed_months(monkeypatch):
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    monkeypatch.setenv("BELIEVABILITY_PERIOD", "2026-08")
    with pytest.raises(SystemExit, match="newer than the latest eligible"):
        pull._target_period(now)
    monkeypatch.setenv("BELIEVABILITY_PERIOD", "June 2026")
    with pytest.raises(SystemExit, match="must be YYYY-MM"):
        pull._target_period(now)
