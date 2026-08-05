"""Erasure Observatory — publication timing.

The runner answers "how much of the record was removed or rewritten". These tests
cover the separate question the publisher has to answer honestly: when did we last
look, as against when did the answer last move. A composite that holds still is a
finding about a settled regime, not a fault, and it must not read on the front page
as a runner that died — index.html paints the erasure hero stale past twelve hours,
against a six-hourly cron.

The other half of the guard is that the heartbeat invents nothing. A round where no
layer could be shown to be current still republishes a null index and its ABSENT
layers; it never fills them in to look alive.

Offline: no probe or feed layer runs. The runner reads other signals' published
readings off disk, so the inputs here are fixture files in a temp directory.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import erasure_pull as pull  # noqa: E402


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir holding one fresh network input.

    The OONI reading is rewritten byte-identically on an unchanged round, which is
    what a real quiet round looks like: append_seal is idempotent on the same
    payload, so the ledger head does not move either and the round is unchanged all
    the way through.
    """
    d = str(tmp_path)
    monkeypatch.setattr(pull, "READINGS", d)
    monkeypatch.setattr(pull, "LEDGER", os.path.join(d, "erasure-ledger.jsonl"))
    monkeypatch.setattr(pull, "OUT", os.path.join(d, "erasure-observatory-latest.json"))
    monkeypatch.setattr(pull, "HIST", os.path.join(d, "erasure-observatory-history.jsonl"))

    # One as-of for the whole test, so a repeated round is genuinely a repeat.
    as_of = _hours_ago(3)

    def run(gfw_index=57.9, network=True):
        if network:
            with open(os.path.join(d, "ooni-gfw-latest.json"), "w", encoding="utf-8") as f:
                json.dump({"source": "OONI", "scope": "CN", "generated_at": as_of,
                           "gfw_index": gfw_index, "n_measurements": 1234, "tests": {}},
                          f, ensure_ascii=False)
        pull.main()
        with open(os.path.join(d, "erasure-observatory-latest.json"), encoding="utf-8") as f:
            return json.load(f)

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "erasure-observatory-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_repeated_composite_still_refreshes_the_observation_time(publish):
    """The bug this guards: a settled index never moved, so the file stopped being
    rewritten and the front page called a working hero signal stale."""
    run, _ = publish
    first = run(gfw_index=57.9)
    second = run(gfw_index=57.9)

    assert second["erasure_index"] == first["erasure_index"]
    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")


def test_a_repeated_composite_does_not_move_last_changed_at(publish):
    run, _ = publish
    first = run(gfw_index=57.9)
    second = run(gfw_index=57.9)

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_repeated_composite_appends_no_history(publish):
    """History is the movement record. Heartbeats belong in the reading, not here,
    or the record of what changed fills up with what did not."""
    run, tmp_path = publish
    run(gfw_index=57.9)
    run(gfw_index=57.9)
    run(gfw_index=57.9)

    assert len(_history(tmp_path)) == 1


def test_a_moved_composite_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    first = run(gfw_index=57.9)
    moved = run(gfw_index=40.0)

    assert moved["erasure_index"] != first["erasure_index"]
    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the previous
    generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run(gfw_index=57.9)

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "erasure-observatory-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run(gfw_index=57.9)
    assert second["last_changed_at"] == first["generated_at"]


def test_a_method_bump_republishes_and_moves_last_changed_at(publish, monkeypatch):
    """A methodology correction that leaves every number identical is still movement
    a reader has to see, so it moves last_changed_at and appends to history."""
    run, tmp_path = publish
    first = run(gfw_index=57.9)

    monkeypatch.setattr(pull, "METHOD_VERSION", pull.METHOD_VERSION + 1)
    bumped = run(gfw_index=57.9)

    assert bumped["erasure_index"] == first["erasure_index"]
    assert bumped["last_changed_at"] == bumped["generated_at"]
    assert bumped["last_changed_at"] != first["last_changed_at"]
    assert len(_history(tmp_path)) == 2


def test_a_stale_layer_stays_withheld_across_a_heartbeat(publish, tmp_path):
    """The heartbeat is an observation time, not a licence to fill anything in. A
    network reading past its bound is withheld on the republished round exactly as
    it was on the first, with its true as-of date and the number being declined."""
    run, _ = publish
    with open(tmp_path / "ooni-gfw-latest.json", "w", encoding="utf-8") as f:
        json.dump({"source": "OONI", "generated_at": _hours_ago(24 * 9),
                   "gfw_index": 57.9}, f)

    first = run(network=False)
    second = run(network=False)

    for out in (first, second):
        net = next(l for l in out["layers"] if l["layer"] == "network")
        assert net["value"] is None
        assert net["status"] == "STALE"
        assert net["withheld_value"] == 57.9
        assert "network" not in out["layers_contributing"]
    assert second["generated_at"] > first["generated_at"]


def test_a_round_where_no_layer_reported_republishes_a_null_index(publish, tmp_path):
    """The abstain path, under the heartbeat. With nothing readable the composite is
    null and stays null on the republish — a fresh observation time must never be
    the moment a missing measurement acquires a number."""
    run, _ = publish
    first = run(network=False)
    second = run(network=False)

    assert first["erasure_index"] is None and second["erasure_index"] is None
    assert first["layers_contributing"] == [] and second["layers_contributing"] == []
    assert all(l["value"] is None for l in second["layers"])
    assert second["generated_at"] > first["generated_at"]
    assert second["last_changed_at"] == first["last_changed_at"]
    assert len(_history(tmp_path)) == 1


def test_narrative_operational_transition_moves_composite_history(publish, tmp_path):
    run, _ = publish
    generated = _hours_ago(24 * 30)
    baike_path = tmp_path / "baike-redaction-latest.json"
    base = {
        "generated_at": generated,
        "pipeline_checked_at": _hours_ago(2),
        "status": "disabled",
        "collector_status": "disabled_no_authorized_access",
        "collector_reason": "Baike collection is disabled pending authorized access",
        "rewrite_index": None,
        "valid_for_series": False,
    }
    baike_path.write_text(json.dumps(base), encoding="utf-8")
    first = run()
    before = len(_history(tmp_path))

    base.update({
        "pipeline_checked_at": _hours_ago(1),
        "status": "halted",
        "collector_status": "halted_by_governance",
        "collector_reason": "Baike collection halted by governance",
    })
    baike_path.write_text(json.dumps(base), encoding="utf-8")
    second = run()
    history = _history(tmp_path)

    first_narrative = next(l for l in first["layers"] if l["layer"] == "narrative")
    second_narrative = next(l for l in second["layers"] if l["layer"] == "narrative")
    assert first_narrative["collector_status"] == "disabled_no_authorized_access"
    assert second_narrative["collector_status"] == "halted_by_governance"
    assert len(history) == before + 1
    assert history[-1]["collector_status"]["narrative"] == "halted_by_governance"
    assert second["last_changed_at"] == second["generated_at"]
