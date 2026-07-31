"""Vantage fusion — publication timing.

processors/vantage_fusion.py answers "what do the three vantages jointly say".
These tests cover the separate question the publisher has to answer honestly:
when did we last look, as against when did the fused answer last move. This
driver has always rewritten its reading every cycle, so it never went stale on
the board — but a reader watching generated_at advance every six hours could not
tell that the fused index had not actually moved in weeks, and a refreshed
timestamp on a still number reads as news.

Offline: fuse() is stubbed out entirely and only the writer runs, so no vantage
reading is touched and nothing goes near the network.
"""
from __future__ import annotations

import json

import pytest

import scripts.vantage_fusion_pull as pull


def _reading(stamp: str, fused_index: float, confidence: str) -> dict:
    """The shape main() consumes: whatever fuse() returns on a good round."""
    return {
        "ok": True,
        "generated_at": stamp,
        "fused_index": fused_index,
        "interval": [fused_index, fused_index],
        "confidence": confidence,
        "agreement": 0.9,
        "vantages": {"censored_planet": fused_index, "ooni": fused_index},
        "verdict": f"GFW anomaly {fused_index:.0f}/100",
    }


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the fusion itself stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "vantage-fusion-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "vantage-fusion-history.jsonl"))

    # A hand-advanced clock rather than the wall clock: consecutive rounds must be
    # ordered unambiguously for the heartbeat assertions to mean anything.
    clock = {"minute": 0}

    def run(fused_index=30.0, confidence="CONTESTED", ok=True, reason=None):
        clock["minute"] += 10
        stamp = f"2026-07-31T{clock['minute'] // 60:02d}:{clock['minute'] % 60:02d}:00+00:00"
        if ok:
            fused = _reading(stamp, fused_index, confidence)
        else:
            fused = {"ok": False, "reason": reason or "nothing to fuse"}
        monkeypatch.setattr(pull, "fuse", lambda readings: dict(fused))
        pull.main()
        path = tmp_path / "vantage-fusion-latest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "vantage-fusion-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_still_fusion_still_refreshes_the_observation_time(publish):
    """Two vantages that keep disagreeing the same way are a finding, not a
    fault, and the file must keep saying when it was last computed."""
    run, tmp_path = publish
    first = run(fused_index=29.1)
    second = run(fused_index=29.1)

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged fusion must still publish this round's observation time")


def test_a_still_fusion_does_not_move_last_changed_at(publish):
    run, tmp_path = publish
    first = run(fused_index=29.1)
    second = run(fused_index=29.1)

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_still_fusion_appends_no_history(publish):
    """History is the movement record. Heartbeats belong in the reading, not
    here, or the record of what changed fills up with what did not."""
    run, tmp_path = publish
    run(fused_index=29.1)
    run(fused_index=29.1)
    run(fused_index=29.1)

    assert len(_history(tmp_path)) == 1


def test_a_moved_fusion_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(fused_index=29.1)
    moved = run(fused_index=45.0)

    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_a_changed_confidence_tier_counts_as_movement(publish):
    """The fused number can sit still while the corroboration behind it flips,
    and that is exactly the change a reader most needs dated."""
    run, tmp_path = publish
    run(fused_index=29.1, confidence="CONTESTED")
    moved = run(fused_index=29.1, confidence="CORROBORATED")

    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_an_immaterial_wobble_is_not_movement(publish):
    """Sub-threshold drift is noise in the underlying rates, and dating it as a
    change would manufacture a movement record out of jitter."""
    run, tmp_path = publish
    first = run(fused_index=29.1)
    wobble = run(fused_index=29.9)

    assert wobble["last_changed_at"] == first["generated_at"]
    assert wobble["generated_at"] > first["generated_at"]
    assert len(_history(tmp_path)) == 1


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the
    previous generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run(fused_index=29.1)

    legacy = dict(first)
    legacy.pop("last_changed_at")
    (tmp_path / "vantage-fusion-latest.json").write_text(
        json.dumps(legacy), encoding="utf-8")

    second = run(fused_index=29.1)
    assert second["last_changed_at"] == first["generated_at"]


def test_an_abstaining_round_publishes_nothing(publish):
    """No quantitative vantage is current enough to fuse. The heartbeat applies
    only to a round that produced a number, so this one must leave no reading."""
    run, tmp_path = publish
    assert run(ok=False, reason="no quantitative vantage is current enough to fuse") is None
    assert _history(tmp_path) == []


def test_an_abstaining_round_does_not_disturb_the_published_reading(publish):
    """A later abstention must not rewrite, restamp or erase the last good
    fusion — the file on disk stays the last thing actually measured."""
    run, tmp_path = publish
    good = run(fused_index=29.1)
    run(ok=False, reason="no quantitative vantage reported — nothing to fuse")

    on_disk = json.loads(
        (tmp_path / "vantage-fusion-latest.json").read_text(encoding="utf-8"))
    assert on_disk == good
    assert len(_history(tmp_path)) == 1
