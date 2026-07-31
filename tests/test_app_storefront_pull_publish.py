"""App Storefront — publication timing.

The collector answers "which apps does Apple still offer in the Chinese
storefront". These tests cover the separate question the publisher has to answer
honestly: when did we last look, as against when did the catalogue last move.

Stillness is the normal state of this signal — a delisting is a discrete event
and most rounds find nothing new — so a catalogue that holds still must not read
on the site as a collector that died. The abstain paths are tested alongside,
because the heartbeat is only ever owed to a round that actually cleared the
control gate.

Offline: observe_panel() is the only thing that touches Apple, and it is stubbed
out entirely. control_state() and delisting_rate() are pure functions over the
observation list, so the real ones run.
"""
from __future__ import annotations

import json

import pytest

import scripts.app_storefront_pull as pull


def _app(name, track_id, state, *, category="SECURE_MESSAGING", control=False):
    """One panel row shaped the way collectors.app_storefront.observe_app returns
    it. storefront_cn is derived from the state so the row stays self-consistent
    if a reader checks the numbers against the verdict."""
    return {
        "name": name,
        "track_id": track_id,
        "category": category,
        "control": control,
        "storefront_control": 1,
        "storefront_cn": 0 if state == "DELISTED" else 1,
        "state": state,
    }


def _latest(tmp_path):
    path = tmp_path / "app-storefront-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "app-storefront-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the Apple lookup stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "app-storefront-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "app-storefront-history.jsonl"))

    def run(signal="DELISTED", controls_ok=True):
        # Two controls and two tracked apps is the smallest panel that exercises
        # the real control gate: the controls decide whether the round is
        # trustworthy, the tracked pair gives delisting_rate a denominator.
        observations = [
            _app("WeChat", 414478124,
                 "AVAILABLE" if controls_ok else "DELISTED",
                 category="CONTROL", control=True),
            _app("Alipay", 333206289,
                 "AVAILABLE" if controls_ok else "DELISTED",
                 category="CONTROL", control=True),
            _app("Signal", 874139669, signal),
            _app("Facebook", 284882215, "AVAILABLE", category="SOCIAL"),
        ]
        monkeypatch.setattr(pull, "observe_panel", lambda: observations)
        pull.main()
        return _latest(tmp_path)

    return run, tmp_path


def test_a_still_catalogue_still_refreshes_the_observation_time(publish):
    """The bug this guards: a catalogue that does not move never moved
    delisting_rate either, so the file stopped being rewritten and the site
    called a working signal stale."""
    run, tmp_path = publish
    first = run(signal="DELISTED")
    second = run(signal="DELISTED")

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged catalogue must still publish this round's observation time")


def test_a_still_catalogue_does_not_move_last_changed_at(publish):
    run, tmp_path = publish
    first = run(signal="DELISTED")
    second = run(signal="DELISTED")

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_still_catalogue_appends_no_history(publish):
    """The history is the record of when each door closed. A heartbeat is not a
    closing, so it has no business in that file."""
    run, tmp_path = publish
    run(signal="DELISTED")
    run(signal="DELISTED")
    run(signal="DELISTED")

    assert len(_history(tmp_path)) == 1


def test_a_relisting_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(signal="DELISTED")
    moved = run(signal="AVAILABLE")

    assert moved["last_changed_at"] == moved["generated_at"]
    assert moved["delisting_rate"] == 0.0
    assert [e["direction"] for e in moved["changes_since_last_reading"]] == ["relisted"]

    rows = _history(tmp_path)
    assert len(rows) == 2
    assert rows[-1]["delisting_rate"] == 0.0


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the
    previous generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run(signal="DELISTED")

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "app-storefront-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run(signal="DELISTED")
    assert second["last_changed_at"] == first["generated_at"]


def test_a_degraded_round_publishes_nothing_at_all(publish):
    """The heartbeat is owed only to a round that cleared the control gate. When
    a control app reads delisted the lookup itself is suspect, so republishing
    the old reading with a fresh timestamp would be asserting an observation we
    did not make."""
    run, tmp_path = publish
    first = run(signal="DELISTED")
    run(controls_ok=False)

    after = _latest(tmp_path)
    assert after["generated_at"] == first["generated_at"]
    assert len(_history(tmp_path)) == 1


def test_a_degraded_first_round_writes_no_file(publish):
    run, tmp_path = publish
    run(controls_ok=False)

    assert _latest(tmp_path) is None
    assert _history(tmp_path) == []


def test_no_comparable_app_still_abstains(publish):
    """The other abstain: the control gate passed but no rate could be derived.
    Nothing is written, so an empty round cannot masquerade as a measured one."""
    run, tmp_path = publish
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(pull, "delisting_rate", lambda obs: None)
        run(signal="DELISTED")
    finally:
        monkeypatch.undo()

    assert _latest(tmp_path) is None
    assert _history(tmp_path) == []
