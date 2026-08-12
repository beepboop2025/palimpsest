"""OONI GFW — publication timing.

The collector answers "how much CN traffic is OONI seeing interfered with". These
tests cover the separate question the publisher has to answer honestly: when did
we last look, as against when did the answer last move. A firewall holding a
steady line is a finding, not a fault, and it must not read on the site as a
collector that died.

Offline: the OONI aggregation calls are stubbed out entirely and only the writer
runs.
"""
from __future__ import annotations

import json

import pytest

import collectors.ooni_gfw as collector
import scripts.ooni_gfw_pull as pull


def _tests(anomaly: int, measurement: int = 100, failure: int = 0) -> list[dict]:
    """One available test is the whole input the writer needs, so the OONI HTTP
    layer never runs. Moving the anomaly count is how a round 'moves'."""
    return [{
        "test": "web_connectivity",
        "label": "website / URL blocking",
        "available": True,
        "anomaly_count": anomaly,
        "measurement_count": measurement,
        "failure_count": failure,
        "anomaly_rate": round(anomaly / (measurement - failure), 4),
    }]


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the OONI path stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "ooni-gfw-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "ooni-gfw-history.jsonl"))

    def run(anomaly=62, measurement=100, failure=0, top=None):
        signals = _tests(anomaly, measurement, failure)
        blocked = top if top is not None else [
            {"domain": "torproject.org", "anomaly_count": 40,
             "measurement_count": 50, "failure_count": 0,
             "completed_measurement_count": 50, "anomaly_rate": 0.8}]
        monkeypatch.setattr(pull, "test_signals", lambda since, until: signals)
        monkeypatch.setattr(pull, "top_blocked_domains",
                            lambda since, until: blocked)
        pull.main()
        with open(tmp_path / "ooni-gfw-latest.json", encoding="utf-8") as f:
            return json.load(f)

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "ooni-gfw-history.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def test_top_blocked_rate_publishes_its_completed_denominator(monkeypatch):
    monkeypatch.setattr(collector, "_get", lambda params, **kwargs: {"result": [{
        "domain": "example.test",
        "anomaly_count": 8,
        "measurement_count": 12,
        "failure_count": 2,
    }]})

    [row] = collector.top_blocked_domains(
        "2026-08-01", "2026-08-08", min_measurements=1)

    assert row["anomaly_rate"] == 0.8
    assert row["completed_measurement_count"] == 10
    assert row["failure_count"] == 2
    assert row["measurement_count"] == 12


def test_a_repeated_finding_still_refreshes_the_observation_time(publish):
    """The bug this guards: a firewall that keeps blocking the same things at the
    same rate never moved gfw_index, so the file stopped being rewritten and the
    site called a working signal stale."""
    run, _ = publish
    first = run(anomaly=62)
    second = run(anomaly=62)

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")


def test_a_repeated_finding_does_not_move_last_changed_at(publish):
    run, _ = publish
    first = run(anomaly=62)
    second = run(anomaly=62)

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_repeated_finding_appends_no_history(publish):
    """History is the movement record. Heartbeats belong in the reading, not
    here, or the record of what changed fills up with what did not."""
    run, tmp_path = publish
    run(anomaly=62)
    run(anomaly=62)
    run(anomaly=62)

    assert len(_history(tmp_path)) == 1


def test_a_moved_index_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(anomaly=62)
    moved = run(anomaly=17)

    assert moved["gfw_index"] == 17.0
    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_index_denominator_excludes_failures_but_attempt_volume_is_preserved(publish):
    run, tmp_path = publish
    reading = run(anomaly=140_532, measurement=245_883, failure=1_952)

    assert reading["gfw_index"] == 57.6
    assert reading["n_completed_measurements"] == 243_931
    assert reading["n_measurements"] == 245_883
    assert _history(tmp_path)[0]["n_completed_measurements"] == 243_931
    assert _history(tmp_path)[0]["n_measurements"] == 245_883


def test_a_moved_top_blocked_list_also_counts_as_movement(publish):
    """The index can hold still while the set of interfered-with sites turns
    over, and that turnover is a finding in its own right."""
    run, tmp_path = publish
    run(anomaly=62)
    moved = run(anomaly=62, top=[{"domain": "wikipedia.org", "anomaly_count": 30,
                                  "measurement_count": 40, "failure_count": 0,
                                  "completed_measurement_count": 40,
                                  "anomaly_rate": 0.75}])

    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the
    previous generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run(anomaly=62)

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "ooni-gfw-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run(anomaly=62)
    assert second["last_changed_at"] == first["generated_at"]


def test_an_abstaining_round_publishes_nothing(publish):
    """The heartbeat is for rounds that produced a reading. When OONI answered
    for no test at all, there is no observation to timestamp, and republishing
    the last one under this round's clock would be a fabrication."""
    run, tmp_path = publish
    first = run(anomaly=62)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(pull, "test_signals",
                       lambda since, until: [{"test": "web_connectivity",
                                              "label": "website / URL blocking",
                                              "available": False}])
        monkey.setattr(pull, "top_blocked_domains",
                       lambda since, until: (_ for _ in ()).throw(
                           AssertionError("abstained round must not fetch domains")))
        pull.main()
    finally:
        monkey.undo()

    with open(tmp_path / "ooni-gfw-latest.json", encoding="utf-8") as f:
        after = json.load(f)
    assert after["generated_at"] == first["generated_at"], (
        "an abstaining round must leave the published reading untouched")
    assert len(_history(tmp_path)) == 1
