"""Data darkness — publication timing and the vantage/withholding split.

Two properties are load-bearing and pinned here.

First, the reading is rewritten unconditionally every round that survives the
abstain gate (scripts/data_darkness_pull.py, no gate on the OUT write). The
fleet relearned this the hard way: a write-if-changed gate on the reading
looks like a tidy-up and must not be added — it would stop refreshing
generated_at whenever the index held still, and the observatory would label
its own healthy signal stale.

Second, the vantage split: a surface that ANSWERED with an old date is scored
as staleness, but a surface that could not be FETCHED is published as
unreachable and excluded from darkness_index. This driver watches a state
suspected of hiding data — the one unforgivable bug would be reporting our
own network trouble as Beijing going dark.

Offline: collect() is stubbed entirely; the clock is stubbed at the daily
cron cadence.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.data_darkness_pull as pull


# 2026-07-31 is a Friday; the first stubbed round runs Saturday 2026-08-01.
FRIDAY = "2026-07-31"

# The real 2026 NBS promise map (February is a '……' month).
CAL_2026 = {1: 19, 3: 16, 4: 16, 5: 18, 6: 16, 7: 15, 8: 17, 9: 15,
            10: 19, 11: 16, 12: 15}

FULL = {
    "pboc_omo": {"latest_publication": FRIDAY, "latest_announcement_no": 147,
                 "n_listed": 20, "first_listed": "2026-07-03",
                 "numbers_listed_min": 128, "numbers_listed_max": 147},
    "pboc_mb_stats": {"latest_publication": FRIDAY, "latest_period": "2026-06",
                      "months_listed": 6},
    "safe_settlement": {"latest_publication": "2026-07-17", "n_files": 4},
    "cfets_benchmarks": {"latest_publication": FRIDAY,
                         "families": {"shibor": FRIDAY, "repo_fixing": FRIDAY,
                                      "central_parity": FRIDAY},
                         "our_last_look": "2026-08-01T04:04:31Z"},
    "nbs_energy": {"latest_publication": "2026-07-15", "latest_period": "2026-06",
                   "n_listed": 2, "promised_day": 17,
                   "promise_calendar": CAL_2026},
    "nbs_industrial": {"latest_publication": "2026-07-15",
                       "latest_period": "2026-06", "n_listed": 1,
                       "promised_day": 17, "promise_calendar": CAL_2026},
    "nra_rail": {"latest_publication": "2026-07-14", "latest_period": "2026-06"},
}
N_WATCHED = 7


class _Clock:
    """Advances a day per round by default, matching the daily cron."""

    def __init__(self, step_hours: float = 24.0) -> None:
        self.step = timedelta(hours=step_hours)
        self.t = datetime(2026, 7, 31, 10, 7, 0, tzinfo=timezone.utc)

    def now(self, tz=None) -> datetime:
        self.t += self.step
        return self.t


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the surfaces stubbed."""
    def make(step_hours: float = 24.0):
        monkeypatch.setattr(pull, "READINGS", str(tmp_path))
        monkeypatch.setattr(pull, "OUT", str(tmp_path / "data-darkness-latest.json"))
        monkeypatch.setattr(pull, "HIST", str(tmp_path / "data-darkness-history.jsonl"))
        monkeypatch.setattr(pull, "datetime", _Clock(step_hours))
        # The cfets reader-staleness gate is exercised by its own test; the
        # multi-round tests use a static fixture timestamp that would go
        # stale as the clock advances, so the bound is parked out of the way.
        monkeypatch.setattr(pull, "CFETS_READER_MAX_AGE_H", 10**6)

        def run(surfaces):
            monkeypatch.setattr(pull, "collect", lambda *a, **k: surfaces)
            pull.main()
            return _reading(tmp_path)

        return run

    return make, tmp_path


def _reading(tmp_path):
    path = tmp_path / "data-darkness-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "data-darkness-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_an_unchanged_round_still_refreshes_the_observation_time(publish):
    """Adding a write-if-changed gate here would look like a tidy-up and must
    not be done — see the module docstring."""
    make, _ = publish
    run = make()
    first = run(dict(FULL))
    second = run(dict(FULL))

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")


def test_a_first_round_publishes_the_contract_fields(publish):
    make, _ = publish
    run = make()
    got = run(dict(FULL))

    assert got["generated_at"] == "2026-08-01T10:07:00Z"
    assert got["method_version"] == pull.METHOD_VERSION
    assert got["n_series_watched"] == N_WATCHED
    assert got["n_series_reporting"] == N_WATCHED
    assert got["unreachable"] == []


def test_weekends_are_not_darkness_for_business_basis_surfaces(publish):
    """Friday's OMO announcement read on Saturday must score zero days late;
    the monthly surfaces run on calendar days."""
    make, _ = publish
    run = make()
    got = run(dict(FULL))   # Saturday 2026-08-01

    omo = got["series"]["pboc_omo"]
    assert omo["days_since"] == 0 and omo["staleness_ratio"] == 0
    mb = got["series"]["pboc_mb_stats"]
    assert mb["days_since"] == 1
    safe = got["series"]["safe_settlement"]
    assert safe["days_since"] == 15


def test_an_unfetchable_surface_is_unreachable_not_dark(publish):
    """The one unforgivable bug: our network trouble read as withholding."""
    make, _ = publish
    run = make()
    partial = {k: v for k, v in FULL.items() if k != "safe_settlement"}
    got = run(partial)

    assert got["unreachable"] == ["safe_settlement"]
    assert got["n_series_reporting"] == N_WATCHED - 1
    assert "safe_settlement" not in got["series"]
    reported = [got["series"][k]["staleness_ratio"] for k in got["series"]]
    assert got["darkness_index"] == round(sum(reported) / len(reported), 4)


def test_an_abstaining_round_publishes_nothing_at_all(publish):
    make, tmp_path = publish
    run = make()
    first = run(dict(FULL))
    run({})

    after = _reading(tmp_path)
    assert after["generated_at"] == first["generated_at"], (
        "an abstaining round must not restamp the reading as freshly observed")
    assert len(_history(tmp_path)) == 1


def test_history_keeps_one_row_per_day_and_lets_a_later_run_correct_it(publish):
    """Two runs on the same date are one observation, not two; a later run
    that sees different staleness replaces the day's row rather than
    appending a contradiction beside it."""
    make, tmp_path = publish
    run = make(step_hours=1.0)   # both rounds land on 2026-07-31

    run(dict(FULL))
    rows_first = _history(tmp_path)
    stale = dict(FULL)
    stale["safe_settlement"] = {"latest_publication": "2026-06-19", "n_files": 4}
    run(stale)
    rows_second = _history(tmp_path)

    assert len(rows_first) == 1 and len(rows_second) == 1
    assert rows_second[0]["date"] == rows_first[0]["date"]
    assert rows_second[0]["darkness_index"] > rows_first[0]["darkness_index"]


def test_promise_scoring_against_the_states_own_calendar(publish):
    """On Aug 1 the most recent promise already passed is July 15, covering
    June data — which is published, so 0 days past and 0 periods behind. The
    July data is not owed until Aug 17. A series with no calendar carries no
    days_past_promise at all — absence of a promise is not a promise kept."""
    make, _ = publish
    run = make()
    got = run(dict(FULL))    # Saturday 2026-08-01

    energy = got["series"]["nbs_energy"]
    assert energy["days_past_promise"] == 0
    assert energy["periods_behind"] == 0
    assert "days_past_promise" not in got["series"]["nra_rail"]


def test_a_broken_promise_counts_each_day(publish):
    """Clock at Aug 21: July data still absent and the calendar said the
    17th — four days past the state's own promise, one report cycle short."""
    make, _ = publish
    run = make()
    got = None
    for _ in range(21):      # advance the daily clock to 2026-08-21
        got = run(dict(FULL))

    energy = got["series"]["nbs_energy"]
    assert energy["days_past_promise"] == 4
    assert energy["periods_behind"] == 1
    assert energy["latest_period"] == "2026-06"


def test_a_late_previous_report_does_not_fulfil_the_next_promise(publish):
    """The reviewer-caught bug this pins: the June report (July's promise)
    arrives late on Aug 3. Publication month is August, but the AUGUST
    promise covers JULY data — still absent on Aug 21, so the series is 4
    days past promise, not 0. Fulfilment is judged by data period, never by
    publication month."""
    make, _ = publish
    run = make()
    fresh = dict(FULL)
    fresh["nbs_energy"] = {**FULL["nbs_energy"],
                           "latest_publication": "2026-08-03",
                           "latest_period": "2026-06"}
    got = None
    for _ in range(21):      # advance the daily clock to 2026-08-21
        got = run(fresh)

    energy = got["series"]["nbs_energy"]
    assert energy["days_past_promise"] == 4
    assert energy["periods_behind"] == 1


def test_a_release_covering_the_due_period_fulfils_the_promise(publish):
    make, _ = publish
    run = make()
    fresh = dict(FULL)
    fresh["nbs_energy"] = {**FULL["nbs_energy"],
                           "latest_publication": "2026-08-15",
                           "latest_period": "2026-07"}
    got = run(fresh)         # Saturday 2026-08-01
    assert got["series"]["nbs_energy"]["days_past_promise"] == 0


def test_a_stale_cfets_reader_is_unreachable_not_darkness(publish, monkeypatch):
    """The cfets surface is read through our own china-econ collector; if
    that reader has not looked recently, its silence is OUR outage. The one
    scenario this signal must never publish: our dead pipeline scored as
    Beijing going dark."""
    make, _ = publish
    run = make()
    monkeypatch.setattr(pull, "CFETS_READER_MAX_AGE_H", 30.0)
    fresh = dict(FULL)
    fresh["cfets_benchmarks"] = {**FULL["cfets_benchmarks"],
                                 "our_last_look": "2026-07-25T04:04:31Z"}
    got = run(fresh)         # Aug 1: the reader last looked a week ago

    assert "cfets_benchmarks" in got["unreachable"]
    assert "cfets_benchmarks" not in got["series"]
    assert got["n_series_reporting"] == N_WATCHED - 1


def test_darkness_rises_as_the_surfaces_stay_quiet(publish):
    """Hold every surface's last release fixed and let days pass: the index
    the conformal detector watches must be monotone in real silence."""
    make, tmp_path = publish
    run = make()
    first = run(dict(FULL))    # Saturday
    second = run(dict(FULL))   # Sunday
    third = run(dict(FULL))    # Monday — a missed business day now counts

    assert second["darkness_index"] >= first["darkness_index"]
    assert third["darkness_index"] > second["darkness_index"]
    assert third["series"]["pboc_omo"]["days_since"] == 1
    assert len(_history(tmp_path)) == 3
