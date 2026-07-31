"""Circumvention demand — publication timing.

tests/test_circumvention_demand.py covers what the collector reads out of Tor's
series. This file covers the separate question the publisher has to answer
honestly: when did we last look, as against when did the answer last move.

The two are not the same claim, and the board renders only the first. A reading
that stops being rewritten because its numbers held still gets labelled "stale ·
last measured …" on the public site, which accuses a working collector of being
dead. This driver already avoids that by rewriting the reading on every round
that produced one — see the note above METHOD_VERSION in the runner — so these
tests pin that behaviour down rather than change it. Movement lives in the
history file, which stays gated on change so the record of what moved never
fills up with rounds where nothing did.

The last test is the guard that matters most: an unreachable Tor Metrics must
still abstain. A heartbeat is only honest for a round that actually produced a
publishable reading, and going quiet when the source is gone is the correct
outcome — that is when the site SHOULD read stale.

Offline: the fetch path never runs, the collector is stubbed at the runner's
seam, and the clock is fake.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import scripts.circumvention_demand_pull as pull


DATE = "2026-07-28"


def _day(bridge_users=2811, snowflake=(1400, 1440)):
    return {DATE: {"date": DATE, "bridge_users": bridge_users,
                   "relay": {"users": 485, "lower": 290, "upper": 1245},
                   "transports": {"snowflake": {"low": snowflake[0],
                                                "high": snowflake[1]}}}}


class _Clock:
    """A fake wall clock. generated_at is written at second resolution, so two
    rounds in the same test tick would otherwise compare a timestamp against
    itself and the heartbeat assertion would pass or fail on machine speed."""

    def __init__(self):
        self._t = datetime(2026, 7, 31, 8, 0, 0, tzinfo=timezone.utc)

    def now(self, tz=None):
        self._t += timedelta(hours=6)
        return self._t


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with Tor Metrics stubbed out."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "circumvention-demand-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "circumvention-demand-history.jsonl"))
    monkeypatch.setattr(pull, "datetime", _Clock())

    def run(fresh=None):
        # The whole input the writer needs is the merged {date: record} the
        # collector would have returned, so no CSV is parsed and no socket opens.
        monkeypatch.setattr(pull, "collect",
                            lambda start, end: _day() if fresh is None else fresh)
        pull.main()

    return run, tmp_path


def _latest(tmp_path):
    path = tmp_path / "circumvention-demand-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "circumvention-demand-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_repeated_reading_still_refreshes_the_observation_time(publish):
    """The bug this guards against, which bit the write-if-changed drivers: a
    finding that holds still stops rewriting its file, and the board calls a
    healthy signal stale. Tor's series can sit flat for days, and flat demand is
    a finding about the wall, not a dead collector."""
    run, tmp_path = publish
    run()
    first = _latest(tmp_path)
    run()
    second = _latest(tmp_path)

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged reading must still publish this round's observation time")
    assert second["asof"] == first["asof"] == DATE


def test_a_repeated_reading_appends_no_history(publish):
    """History here is keyed by DATA date and holds Tor's best current estimate
    per day. Re-reading the same day with the same numbers is not a revision, so
    the record must not grow and revised_at must not move."""
    run, tmp_path = publish
    run()
    after_first = (tmp_path / "circumvention-demand-history.jsonl").read_text()
    run()
    run()
    after_third = (tmp_path / "circumvention-demand-history.jsonl").read_text()

    assert after_third == after_first
    assert len(_history(tmp_path)) == 1


def test_a_revised_day_is_rewritten_and_stamped(publish):
    """Tor revises recent days upward as reports arrive. A changed estimate
    replaces the day's record and records when the revision was seen, so a
    reader can tell a revised number from a first read."""
    run, tmp_path = publish
    run()
    first_row = _history(tmp_path)[0]
    run(_day(bridge_users=3402))
    revised = _history(tmp_path)

    assert len(revised) == 1, "a revision replaces the day, it does not duplicate it"
    assert revised[0]["bridge_users"] == 3402
    assert revised[0]["revised_at"] >= first_row["revised_at"]
    assert _latest(tmp_path)["reading"]["bridge_users"] == 3402


def test_an_unreachable_source_abstains_and_publishes_nothing(publish):
    """All three tables failing is not a measurement of anything. Nothing is
    written, so no fabricated reading and no heartbeat over an empty round."""
    run, tmp_path = publish
    run({})

    assert _latest(tmp_path) is None
    assert _history(tmp_path) == []


def test_an_abstaining_round_leaves_the_last_real_reading_untouched(publish):
    """And when the source goes away after a good round, the published file
    keeps the observation time of the last round that actually saw something.
    That is exactly when the board SHOULD read stale."""
    run, tmp_path = publish
    run()
    good = _latest(tmp_path)
    run({})
    after = _latest(tmp_path)

    assert after == good, ("an abstaining round must not restamp a reading it "
                           "did not make")
