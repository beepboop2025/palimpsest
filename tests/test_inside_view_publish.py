"""Inside View — publication timing.

The collector answers "what did probes inside China receive". These tests cover
the separate question the publisher has to answer honestly: when did we last
look, as against when did the answer last move. A censorship signal that holds
still is a finding, not a fault, and it must not read on the site as a collector
that died.

Offline: the network path is stubbed out entirely and only the writer runs.
"""
from __future__ import annotations

import importlib
import json
import os

import pytest

import scripts.inside_view_pull as pull


ROUND = {
    "observations": [
        {"domain": "torproject.org", "censored": True, "verdict": "forged"},
    ],
    "control": {"state": "SIGHTED"},
    "block_rate": 1.0,
}


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the probe path stubbed."""
    monkeypatch.setenv("PALIMPSEST_LIVE", "1")
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "inside-view-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "inside-view-history.jsonl"))

    def run(block_rate=1.0, control_state="SIGHTED"):
        # One censored domain per round; forged when the round is meant to read
        # as blocked, clean when it is not. That is the whole input the writer
        # needs, so the probe layer never runs.
        forged = 1 if block_rate else 0
        observations = [{
            "domain": "torproject.org",
            "expected_censored": True,
            "n_forged": forged,
            "n_clean": 1 - forged,
        }]

        monkeypatch.setattr(pull, "observe_panel", lambda: observations)
        monkeypatch.setattr(pull, "control_state",
                            lambda obs: {"state": control_state})
        monkeypatch.setattr(pull, "regional_divergence",
                            lambda o: (_ for _ in ()).throw(NotImplementedError))
        pull.main()
        with open(tmp_path / "inside-view-latest.json", encoding="utf-8") as f:
            return json.load(f)

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "inside-view-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_repeated_finding_still_refreshes_the_observation_time(publish):
    """The bug this guards: sustained blocking never moved block_rate, so the
    file stopped being rewritten and the site called a working signal stale."""
    run, tmp_path = publish
    first = run(block_rate=1.0)
    second = run(block_rate=1.0)

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")


def test_a_repeated_finding_does_not_move_last_changed_at(publish):
    run, tmp_path = publish
    first = run(block_rate=1.0)
    second = run(block_rate=1.0)

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_repeated_finding_appends_no_history(publish):
    """History is the movement record. Heartbeats belong in the reading, not
    here, or the record of what changed fills up with what did not."""
    run, tmp_path = publish
    run(block_rate=1.0)
    run(block_rate=1.0)
    run(block_rate=1.0)

    assert len(_history(tmp_path)) == 1


def test_a_moved_answer_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(block_rate=1.0)
    moved = run(block_rate=0.0)

    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the
    previous generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run(block_rate=1.0)

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "inside-view-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run(block_rate=1.0)
    assert second["last_changed_at"] == first["generated_at"]
