"""In-Path Interference — publication timing.

The collector answers "what did OONI's CN aggregate say". These tests cover the
separate question the publisher has to answer honestly: when did we last look, as
against when did the answer last move. A middlebox rate that sits at the same
number for weeks is what a stably deployed device looks like — it is a finding,
not a fault, and it must not read on the site as a collector that died.

Offline: observe_all() is stubbed, so the OONI aggregation API is never called and
only the writer runs. The pure functions downstream of it (control_state,
family_index, execution_blackouts) run for real, because the point of these tests
is what the publisher does with a real-shaped round.
"""
from __future__ import annotations

import json

import pytest

import scripts.in_path_interference_pull as pull


def _obs(test, family, *, completed, anomalies, failures=0):
    """One OONI test in the shape collectors/in_path_interference.py emits.

    completed=0 with failures>0 is the execution-blackout shape: the test ran and
    never once finished, which carries no anomaly rate at all.
    """
    total = completed + failures
    return {
        "test": test,
        "family": family,
        "label": test,
        "available": completed >= 100,
        "why": None if completed >= 100 else "below the floor",
        "measurement_count": total,
        "completed_count": completed,
        "failure_count": failures,
        "anomaly_count": anomalies,
        "anomaly_rate": round(anomalies / completed, 4) if completed else None,
        "execution_failure_rate": round(failures / total, 4) if total else None,
        "never_completes": bool(total > 0 and completed == 0),
    }


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the OONI path stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "in-path-interference-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "in-path-interference-history.jsonl"))

    def run(middlebox_anomalies=40, blackouts=("dnscheck",), degraded=False):
        # A middlebox test whose anomaly count is the dial, one transport test to
        # keep the second family populated, and an optional blackout. Everything
        # the writer needs, so the aggregation API never runs.
        observations = [
            _obs("http_header_field_manipulation", "MIDDLEBOX",
                 completed=1000, anomalies=middlebox_anomalies),
            _obs("vanilla_tor", "TRANSPORT", completed=500, anomalies=250),
        ]
        for name in blackouts:
            observations.append(
                _obs(name, "EXECUTION", completed=0, anomalies=0, failures=5000))
        if degraded:
            # Nothing clears the measurement floor, which is OONI failing to
            # answer usefully rather than China going quiet.
            observations = [
                _obs("http_header_field_manipulation", "MIDDLEBOX",
                     completed=3, anomalies=0),
            ]

        monkeypatch.setattr(pull, "observe_all",
                            lambda since, until: observations)
        pull.main()
        return _latest(tmp_path)

    return run, tmp_path


def _latest(tmp_path):
    path = tmp_path / "in-path-interference-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "in-path-interference-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_repeated_finding_still_refreshes_the_observation_time(publish):
    """The bug this guards: a middlebox rate that holds steady never moved the
    index, so the file stopped being rewritten and the site called a working
    signal stale."""
    run, tmp_path = publish
    first = run()
    second = run()

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")
    assert second["middlebox_index"] == first["middlebox_index"]


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


def test_a_moved_index_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(middlebox_anomalies=40)
    moved = run(middlebox_anomalies=90)

    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_a_new_execution_blackout_counts_as_movement(publish):
    """A method that stops completing in China is a change even when both
    indices hold still, so it has to reach the movement record."""
    run, tmp_path = publish
    run(blackouts=("dnscheck",))
    moved = run(blackouts=("dnscheck", "echcheck"))

    assert moved["middlebox_index"] == 4.0, "the indices were meant to hold still"
    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the
    previous generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run()

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "in-path-interference-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run()
    assert second["last_changed_at"] == first["generated_at"]


def test_a_degraded_round_publishes_nothing_at_all(publish):
    """The heartbeat is only for a round that produced a publishable reading. A
    degraded control means OONI failed us, not that China went quiet, so the
    abstain must leave the previous reading exactly where it was."""
    run, tmp_path = publish
    good = run()
    after = run(degraded=True)

    assert after == good, "an abstaining round must not restamp the reading"
    assert len(_history(tmp_path)) == 1


def test_a_degraded_first_round_writes_no_reading(publish):
    """With no file on disk there is nothing to hold still, and a fabricated
    reading is the failure mode to avoid."""
    run, tmp_path = publish
    assert run(degraded=True) is None
    assert _history(tmp_path) == []
