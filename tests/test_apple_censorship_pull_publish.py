"""Apple Censorship census — publication timing.

The collector answers "how much of the App Store is gone in China, and what the
removals are for". These tests cover the separate question the publisher has to
answer honestly: when did we last look, as against when did the answer last move.

That distinction bites hardest here. GreatFire re-runs a 108,000-app census on
its own schedule, so the corpus is expected to read identically for days at a
time. A census that holds still is the finding, not a fault, and it must not read
on the site as a collector that died.

Offline: fetch_overview is stubbed with row literals, so nothing reaches
api2.applecensorship.com. The parsing and control-gate code underneath runs for
real, because the point is the writer's behaviour on genuine readings.
"""
from __future__ import annotations

import json

import pytest

import scripts.apple_censorship_pull as pull


def _row(code, country, tested, unavailable, *, tags=None, deletion=0):
    return {"code": code, "country": country, "totalTested": tested,
            "unavalibeApps": unavailable,
            "unavalibeAppsPercent": (100 * unavailable / tested) if tested else 0,
            "deletion": deletion, "tags": tags or {}}


def _corpus(unavailable=30314, tags=None):
    """A whole-looking corpus: the CN row plus enough filler to clear the
    control gate's country floor, which would otherwise call this truncated."""
    rows = [_row("CN", "China mainland", 107812, unavailable,
                 tags=tags or {"VPN": 113, "Tibet": 16, "Uyghur": 4},
                 deletion=4992)]
    rows += [_row(f"X{i}", f"Country {i}", 8000, 10 * i) for i in range(1, 25)]
    return rows


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the fetch path stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "apple-censorship-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "apple-censorship-history.jsonl"))

    def run(unavailable=30314, tags=None, rows=...):
        # `rows=None` stands for GreatFire not answering at all, which is how the
        # abstain path is reached without a network call.
        corpus = _corpus(unavailable, tags) if rows is ... else rows
        monkeypatch.setattr(pull, "fetch_overview", lambda: corpus)
        pull.main()
        path = tmp_path / "apple-censorship-latest.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "apple-censorship-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_repeated_census_still_refreshes_the_observation_time(publish):
    """The bug this guards: a corpus that GreatFire had not re-run never moved
    unavailable_pct, so the file stopped being rewritten and the site called a
    working signal stale."""
    run, _ = publish
    first = run()
    second = run()

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged census must still publish this round's observation time")


def test_a_repeated_census_does_not_move_last_changed_at(publish):
    run, _ = publish
    first = run()
    second = run()

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_repeated_census_appends_no_history(publish):
    """History is the movement record. Heartbeats belong in the reading, not
    here, or the record of what changed fills up with what did not."""
    run, tmp_path = publish
    run()
    run()
    run()

    assert len(_history(tmp_path)) == 1


def test_a_moved_count_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(unavailable=30314)
    moved = run(unavailable=30400)

    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_a_moved_tag_composition_counts_as_movement(publish):
    """Composition is the finding this signal exists for, so a reshuffle of what
    the removals are FOR must date the reading even when the headline percentage
    is untouched."""
    run, tmp_path = publish
    first = run(tags={"VPN": 113, "Tibet": 16})
    moved = run(tags={"VPN": 113, "Tibet": 16, "LGBTQ+": 11})

    assert moved["unavailable_pct"] == first["unavailable_pct"]
    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the
    previous generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run()

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "apple-censorship-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run()
    assert second["last_changed_at"] == first["generated_at"]


def test_a_degraded_round_publishes_nothing_at_all(publish):
    """The heartbeat is for rounds that produced a reading. A silent upstream is
    not an observation, and republishing the last census under this round's
    timestamp would be inventing a measurement nobody made."""
    run, tmp_path = publish
    first = run()
    after = run(rows=None)

    assert after["generated_at"] == first["generated_at"], (
        "an abstaining round must not restamp the previous census")
    assert len(_history(tmp_path)) == 1


def test_a_degraded_first_round_writes_no_reading(publish):
    """Nothing on disk yet and nothing measured: the abstain must leave the
    signal unpublished rather than manufacture an empty one."""
    run, tmp_path = publish
    assert run(rows=None) is None
    assert _history(tmp_path) == []
