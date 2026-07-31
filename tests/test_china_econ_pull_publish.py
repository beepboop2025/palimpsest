"""China econ telemetry — publication timing.

The fleet-wide fix that added last_changed_at to the other pulls does not apply
to this one, and these tests are the record of why. The other drivers gated the
reading itself on change, so a finding that held still stopped refreshing
generated_at and the observatory labelled its own healthy signal stale. This
driver has always written the reading unconditionally (scripts/china_econ_pull.py
line 106, no gate), so "when did we last look" is already answered honestly every
round. Its change-gate sits on the history file and is keyed by DATA date, which
is the half of the reference pattern that was already correct.

That property is load-bearing and easy to undo by accident, so it is pinned here
rather than left as a reading of the source. "When did the answer move" is
already carried by asof, the last trading day the portal actually reported.

Offline: the portal path is stubbed out entirely and only the writer runs. The
clock is stubbed too, because the reading stamps whole seconds and two rounds in
the same test second would otherwise be indistinguishable.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.china_econ_pull as pull


DAY = "2026-07-17"
BENCHMARKS = {"shibor_on": 1.42, "fdr007": 1.55, "usdcny_parity": 7.1234}


class _Clock:
    """Advances six hours per round, matching the collector's cron cadence."""

    def __init__(self) -> None:
        self.t = datetime(2026, 7, 17, 3, 41, 0, tzinfo=timezone.utc)

    def now(self, tz=None) -> datetime:
        self.t += timedelta(hours=6)
        return self.t


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the portal path stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "china-econ-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "china-econ-history.jsonl"))
    monkeypatch.setattr(pull, "datetime", _Clock())

    def run(rows):
        # The whole input the writer needs is a date -> benchmarks mapping, so
        # the three throttle-spaced portal calls never happen.
        monkeypatch.setattr(pull, "collect", lambda start, end: rows)
        pull.main()
        return _reading(tmp_path)

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "china-econ-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "china-econ-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_an_unchanged_round_still_refreshes_the_observation_time(publish):
    """The bug this guards against being introduced here: benchmarks that do not
    move would stop rewriting the file, and the site would call a working feed
    stale. This driver must keep publishing every round's own look-time."""
    run, _ = publish
    first = run({DAY: dict(BENCHMARKS)})
    second = run({DAY: dict(BENCHMARKS)})

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")


def test_an_unchanged_round_appends_no_history(publish):
    """History is the movement record, keyed by trading day. Re-reading a day
    already on file is not movement, so it must not add a row."""
    run, tmp_path = publish
    run({DAY: dict(BENCHMARKS)})
    run({DAY: dict(BENCHMARKS)})
    run({DAY: dict(BENCHMARKS)})

    assert len(_history(tmp_path)) == 1


def test_asof_is_what_carries_movement(publish):
    """asof answers "when did the answer move" for this signal, which is why no
    separate last_changed_at is derived: a benchmark series moves when the portal
    reports a new trading day, and that date is already published."""
    run, tmp_path = publish
    first = run({DAY: dict(BENCHMARKS)})
    held = run({DAY: dict(BENCHMARKS)})
    moved = run({DAY: dict(BENCHMARKS), "2026-07-18": dict(BENCHMARKS)})

    assert held["asof"] == first["asof"]
    assert moved["asof"] == "2026-07-18"
    assert len(_history(tmp_path)) == 2


def test_a_revisit_completes_a_partial_day_without_shrinking_it(publish):
    """A throttled round can leave a day half-recorded. The next round has to
    fill the gap and append the completed row, never overwrite it with less."""
    run, tmp_path = publish
    run({DAY: {"shibor_on": 1.42}})
    run({DAY: dict(BENCHMARKS)})

    rows = _history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["fdr007"] == 1.55
    assert rows[0]["shibor_on"] == 1.42


def test_an_abstaining_round_publishes_nothing_at_all(publish):
    """No benchmark family answered, so there is no reading. The heartbeat is
    for rounds that produced something publishable, and a silent portal is not
    one of them — the file must keep the last honest observation time."""
    run, tmp_path = publish
    first = run({DAY: dict(BENCHMARKS)})
    run({})

    after = _reading(tmp_path)
    assert after["generated_at"] == first["generated_at"], (
        "an abstaining round must not restamp the reading as freshly observed")
    assert len(_history(tmp_path)) == 1


def test_a_first_ever_round_writes_both_the_reading_and_the_history(publish):
    run, tmp_path = publish
    assert _reading(tmp_path) is None

    first = run({DAY: dict(BENCHMARKS)})

    assert first["generated_at"] == "2026-07-17T09:41:00Z"
    assert first["method_version"] == pull.METHOD_VERSION
    assert _history(tmp_path) == [{"date": DAY, **BENCHMARKS}]
