"""Censored Planet — publication timing.

The collector answers "what does Censored Planet's side-channel measurement say
about China". These tests cover the separate question the publisher has to answer
honestly: when did we last look, as against when did the answer last move. China's
interference rate is a persistent baseline rather than a series of episodes — the
reading's own persistence_note says so — which means a healthy round of this
signal usually reports exactly what the last one did. That steadiness is the
finding, and it must not read on the site as a collector that died.

Offline: the GraphQL layer is stubbed out entirely and only the writer runs.
"""
from __future__ import annotations

import json
import os

import pytest

import scripts.censored_planet_pull as pull


EVENT = {"startDate": "2026-06-01", "endDate": "2026-06-03",
         "cause": "dns", "impact": "high", "peak": 0.4}


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the API path stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "censored-planet-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "censored-planet-history.jsonl"))

    def run(rate=13.5, events=(EVENT,), series=({"date": "2026-06-01", "value": 1},)):
        # The three API calls are the entire input the writer needs, so no
        # request is ever built and nothing leaves the machine.
        monkeypatch.setattr(pull, "cn_interference_rate", lambda s, u: rate)
        monkeypatch.setattr(pull, "cn_events", lambda s, u: list(events))
        monkeypatch.setattr(pull, "cn_timeseries", lambda s, u: list(series))
        pull.main()
        path = tmp_path / "censored-planet-latest.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "censored-planet-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_repeated_finding_still_refreshes_the_observation_time(publish):
    """The bug this guards: a persistent interference rate never moved, so the
    file stopped being rewritten and the site called a working signal stale."""
    run, tmp_path = publish
    first = run()
    second = run()

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")


def test_a_repeated_finding_does_not_move_last_changed_at(publish):
    run, tmp_path = publish
    first = run()
    second = run()

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_repeated_finding_appends_no_history(publish):
    """History is the movement record. Heartbeats belong in the reading, not
    here, or the record of what changed fills up with what did not."""
    run, tmp_path = publish
    run()
    run()
    run()

    assert len(_history(tmp_path)) == 1


def test_a_moved_answer_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(rate=13.5)
    moved = run(rate=21.0)

    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2
    assert _history(tmp_path)[-1]["cn_interference_rate_pct"] == 21.0


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the
    previous generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run()

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "censored-planet-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run()
    assert second["last_changed_at"] == first["generated_at"]


def test_an_empty_api_round_still_abstains(publish):
    """The heartbeat covers rounds that produced a reading. A round where the
    API gave us nothing is not a quiet finding, it is no finding, and it must
    not leave a timestamped empty board behind."""
    run, tmp_path = publish
    assert run(rate=None, events=(), series=()) is None
    assert _history(tmp_path) == []


def test_an_empty_api_round_does_not_disturb_an_existing_reading(publish):
    """An unreachable source must not overwrite the last honest reading with a
    fresher-looking one, because that would date evidence we never re-observed."""
    run, tmp_path = publish
    first = run()
    # run() returns whatever is on disk, so an abstaining round hands back the
    # earlier reading untouched — which is precisely the assertion here.
    after = run(rate=None, events=(), series=())

    assert after["generated_at"] == first["generated_at"]
    assert len(_history(tmp_path)) == 1


def test_a_method_bump_republishes_and_moves_last_changed_at(publish, monkeypatch):
    """A methodology correction is a change a reader must see even when every
    number is identical, so it moves the movement record too."""
    run, tmp_path = publish
    first = run()

    monkeypatch.setattr(pull, "METHOD_VERSION", pull.METHOD_VERSION + 1)
    bumped = run()

    assert bumped["last_changed_at"] == bumped["generated_at"]
    assert bumped["last_changed_at"] != first["last_changed_at"]
    assert len(_history(tmp_path)) == 2


def test_series_tail_is_deduplicated_sorted_and_latest(publish):
    run, _tmp_path = publish
    rows = [
        {"date": f"2026-07-{day:02d}", "value": day}
        for day in range(1, 32)
    ]
    rows = [
        rows[-1],
        {"date": "not-a-date", "value": 999},
        *rows[:-1],
        {"date": "2026-07-15", "value": 1500},
    ]

    reading = run(series=rows)

    assert reading["series_points"] == 31
    assert [row["date"] for row in reading["series_tail"]] == [
        f"2026-07-{day:02d}" for day in range(2, 32)
    ]
    assert next(row for row in reading["series_tail"] if row["date"] == "2026-07-15")["value"] == 1500
