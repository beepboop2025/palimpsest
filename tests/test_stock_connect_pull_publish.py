"""Stock Connect telemetry — publication timing.

The fleet-wide fix that added last_changed_at to the other pulls does not apply
to this one, and these tests are the record of why. The other drivers gated the
reading itself on change, so a finding that held still stopped refreshing
generated_at and the observatory labelled its own healthy signal stale. This
driver has always written the reading unconditionally
(scripts/stock_connect_pull.py line 110, no gate), so "when did we last look" is
already answered honestly every round. Its change-gate sits on the history file
and is keyed by DATA date — the trading day HKEX printed — which is the half of
the reference pattern that was already correct.

That property is load-bearing and easy to undo by accident, so it is pinned here
rather than left as a reading of the source. "When did the answer move" is
already carried by asof, the last trading day HKEX actually printed; deriving a
separate last_changed_at would add a second answer to a question the reading
already answers, and would drift from the china-econ twin this driver mirrors.

Offline: the HKEX path is stubbed out entirely and only the writer runs. The
clock is stubbed too, because the reading stamps whole seconds and two rounds in
the same test second would otherwise be indistinguishable.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.stock_connect_pull as pull


DAY = "2026-07-16"
NEXT_DAY = "2026-07-17"
# One parsed trading day as collect_range hands it over: southbound keeps its
# buy/sell split, northbound is turnover-only since the Aug-2024 narrowing.
ROW = {
    "date": DAY,
    "sb_buy_b": 72.324,
    "sb_sell_b": 67.284,
    "southbound_net_b": 5.04,
    "nb_sse_turnover_b": 162.733,
    "nb_szse_turnover_b": 193.137,
    "nb_turnover_b": 355.87,
}


class _Clock:
    """Advances a day per round, matching the weekday cron behind this signal."""

    def __init__(self) -> None:
        self.t = datetime(2026, 7, 15, 13, 23, 0, tzinfo=timezone.utc)

    def now(self, tz=None) -> datetime:
        self.t += timedelta(days=1)
        return self.t


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the HKEX path stubbed."""
    monkeypatch.delenv("STOCK_CONNECT_BACKFILL_DAYS", raising=False)
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "stock-connect-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "stock-connect-history.jsonl"))
    monkeypatch.setattr(pull, "datetime", _Clock())

    def run(rows):
        # The whole input the writer needs is a date -> flow-row mapping, so the
        # per-day spaced fetches against hkex.com.hk never happen.
        monkeypatch.setattr(pull, "collect_range", lambda dates: rows)
        pull.main()
        return _reading(tmp_path)

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "stock-connect-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "stock-connect-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_an_unchanged_round_still_refreshes_the_observation_time(publish):
    """The bug this guards against being introduced here: a flat flow print, or a
    run over a holiday week where HKEX has nothing new to say, would stop
    rewriting the file and the site would call a working feed stale. This driver
    must keep publishing every round's own look-time."""
    run, _ = publish
    first = run({DAY: dict(ROW)})
    second = run({DAY: dict(ROW)})

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")


def test_an_unchanged_round_appends_no_history(publish):
    """History is the movement record, keyed by trading day. Re-reading a day
    already on file is not movement, so it must not add a row."""
    run, tmp_path = publish
    run({DAY: dict(ROW)})
    run({DAY: dict(ROW)})
    run({DAY: dict(ROW)})

    assert len(_history(tmp_path)) == 1


def test_asof_is_what_carries_movement(publish):
    """asof answers "when did the answer move" for this signal, which is why no
    separate last_changed_at is derived: a flow series moves when HKEX prints a
    new trading day, and that date is already published alongside the row it
    labels."""
    run, tmp_path = publish
    first = run({DAY: dict(ROW)})
    held = run({DAY: dict(ROW)})
    moved = run({DAY: dict(ROW), NEXT_DAY: {**ROW, "date": NEXT_DAY,
                                           "southbound_net_b": -8.572}})

    assert held["asof"] == first["asof"] == DAY
    assert held["reading"] == first["reading"]
    assert moved["asof"] == NEXT_DAY
    assert moved["reading"]["southbound_net_b"] == -8.572
    assert len(_history(tmp_path)) == 2


def test_a_revisit_completes_a_partial_day_without_shrinking_it(publish):
    """A day can arrive with only one leg parseable — a suspended session, or a
    round that caught the file mid-publication. The next round has to fill the
    gap and rewrite the completed row, never overwrite it with less."""
    run, tmp_path = publish
    run({DAY: {"date": DAY, "southbound_net_b": 5.04}})
    run({DAY: dict(ROW)})

    rows = _history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["southbound_net_b"] == 5.04
    assert rows[0]["nb_turnover_b"] == 355.87


def test_northbound_net_is_never_invented_by_the_publisher(publish):
    """The Aug-2024 narrowing survives the writer as well as the parser: the
    published reading carries turnover-only northbound and no derived net."""
    run, _ = publish
    latest = run({DAY: dict(ROW)})

    assert not any("northbound_net" in k for k in latest["reading"])
    assert "discontinued" in latest["note"]


def test_an_abstaining_round_publishes_nothing_at_all(publish):
    """Nothing parsed — HKEX down, a format change, or an all-holiday window —
    so there is no reading. The heartbeat is for rounds that produced something
    publishable, and a silent source is not one of them: the file must keep the
    last honest observation time rather than be restamped as freshly seen."""
    run, tmp_path = publish
    first = run({DAY: dict(ROW)})
    run({})

    after = _reading(tmp_path)
    assert after["generated_at"] == first["generated_at"], (
        "an abstaining round must not restamp the reading as freshly observed")
    assert after["asof"] == DAY
    assert len(_history(tmp_path)) == 1


def test_a_first_ever_round_writes_both_the_reading_and_the_history(publish):
    run, tmp_path = publish
    assert _reading(tmp_path) is None

    first = run({DAY: dict(ROW)})

    assert first["generated_at"] == "2026-07-16T13:23:00Z"
    assert first["method_version"] == pull.METHOD_VERSION
    assert first["history_days"] == 1
    assert _history(tmp_path) == [ROW]
