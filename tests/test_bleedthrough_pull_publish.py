"""BLEEDTHROUGH — publication timing.

tests/test_bleedthrough.py covers what the round measures. These tests cover the
separate question the publisher has to answer honestly: when did we last look, as
against when did the fleet last move. A stable injector deployment is the normal
state of the apparatus — pools rotate on the censor's schedule, not ours — so a
round that finds the same fleet is a finding, not a fault, and it must not read on
the site as a prober that died.

Offline: the UDP transport is replaced by a canned one, so the only thing that
runs is the fingerprinting and the writer. Nothing leaves the machine, which is
the same discipline the runner itself enforces with its three live gates.

    PYTHONPATH=. python3 -m pytest tests/test_bleedthrough_pull_publish.py -q
"""
from __future__ import annotations

import json

import pytest

from collectors.bleedthrough import RawInjection

import scripts.bleedthrough_pull as pull


# Three targets in one province, all drawing the same forged-IP pool. Three and not
# two because looks_sampled() calls one distinct pool across two targets a sample
# (1 >= 0.5 * 2) and would strip the regional layer; at three the round reads as an
# enumeration, which keeps these tests about publication timing and nothing else.
TARGET_IPS = ("203.0.113.10", "203.0.113.11", "203.0.113.12")

POOL_A = ("1.2.3.4", "5.6.7.8")
POOL_B = ("9.9.9.9", "8.8.8.8")     # a rotated pool: the movement this signal exists to see


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the probe path stubbed."""
    # The kill switch is file-gated and fails safe, so point it at a path that does
    # not exist rather than trusting the repo checkout to be free of a halt file.
    monkeypatch.delenv("PALIMPSEST_HALT", raising=False)
    monkeypatch.setenv("PALIMPSEST_KILLFILE", str(tmp_path / "no_such_halt_file"))
    monkeypatch.setenv("BLEEDTHROUGH_LIVE", "1")

    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({
        "probe": {"domain": "torproject.org", "qtype": 1, "ddti": "CIRCUMVENTION"},
        "targets": [{"ip": ip, "province": "CN-HA", "asn": "AS4134", "kind": "dark"}
                    for ip in TARGET_IPS],
    }), encoding="utf-8")

    monkeypatch.setattr(pull, "TARGETS", str(targets))
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "bleedthrough-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "bleedthrough-history.jsonl"))
    # The disk baseline has to survive across rounds inside one test — that is how a
    # rotation becomes an event — but must not leak into the repo's data dir.
    monkeypatch.setattr(pull, "STORE_DIR", str(tmp_path / "baselines"))
    # Two queries per target is enough to fingerprint a canned fleet, and it keeps the
    # rate ceiling from ever having to sleep.
    monkeypatch.setattr(pull, "BURST", 2)

    def run(pool=POOL_A):
        # Every injector answers every probe with the same forged pool, so the
        # fingerprint is a function of `pool` alone and an unchanged round is
        # genuinely unchanged rather than accidentally so.
        def transport(domain, target_ip):
            return [RawInjection(ip, rr_ttl=64) for ip in pool]

        monkeypatch.setattr(pull, "_udp_transport", transport)
        pull.main()
        out = tmp_path / "bleedthrough-latest.json"
        # None, not an exception: an abstaining round legitimately publishes nothing,
        # and the tests below need to say so out loud.
        if not out.exists():
            return None
        with open(out, encoding="utf-8") as f:
            return json.load(f)

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "bleedthrough-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_repeated_finding_still_refreshes_the_observation_time(publish):
    """The bug this guards: a fleet that holds still never moved the reading, so the
    file stopped being rewritten and the board called a working prober stale."""
    run, tmp_path = publish
    first = run(POOL_A)
    second = run(POOL_A)

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged fleet must still publish this round's observation time")


def test_a_repeated_finding_does_not_move_last_changed_at(publish):
    run, tmp_path = publish
    first = run(POOL_A)
    second = run(POOL_A)

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_repeated_finding_appends_no_history(publish):
    """History is the rotation record. Heartbeats belong in the reading, not here,
    or a record of what the censor changed fills up with what it did not."""
    run, tmp_path = publish
    run(POOL_A)
    run(POOL_A)
    run(POOL_A)

    assert len(_history(tmp_path)) == 1


def test_a_rotated_pool_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(POOL_A)
    moved = run(POOL_B)

    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2
    assert any(e["kind"] == "pool_rotation" for e in moved["events"]), (
        "the round that moved the reading should say what moved")


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the previous
    generated_at is the honest answer for when the fleet last moved."""
    run, tmp_path = publish
    first = run(POOL_A)

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "bleedthrough-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run(POOL_A)
    assert second["last_changed_at"] == first["generated_at"]


def test_a_silent_round_still_abstains(publish):
    """The heartbeat is for rounds that produced a reading. A round where no target
    injected has measured nothing — the channel may be down or the list stale — and
    it must leave no file behind rather than republish a hollow board."""
    run, tmp_path = publish

    assert run(()) is None           # no forged answers anywhere
    assert not (tmp_path / "bleedthrough-latest.json").exists()
    assert _history(tmp_path) == []


def test_a_silent_round_does_not_overwrite_a_good_reading(publish):
    """And when a reading is already published, an abstaining round must not touch
    it — neither to refresh its timestamp nor to blank it."""
    run, tmp_path = publish
    good = run(POOL_A)

    assert run(()) == good, "an abstaining round must leave the last reading exactly as it was"
    assert len(_history(tmp_path)) == 1
