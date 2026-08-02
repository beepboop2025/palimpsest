"""The readings record: every published reading is sealed, and a rewrite shows.

Palimpsest's readings are regenerated in place, so the files themselves are a
live view rather than a record. These tests pin the property that makes the
observatory's own claim checkable: what we published is committed to a chain,
re-sealing an unchanged reading is free, and quietly editing a past reading
fails verification instead of passing silently.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import sealed_ledger as led  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "seal_readings",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "seal_readings.py"))
seal_readings = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seal_readings)


def _reading(n: int) -> dict:
    return {"generated_at": f"2026-08-0{n}T00:00:00+00:00", "method_version": 1,
            "value": n}


def test_every_published_reading_is_sealed():
    """The coverage claim itself. If a new reading ships unsealed, this fails."""
    cov = seal_readings.coverage()
    assert cov["unsealed"] == [], f"unsealed readings: {cov['unsealed']}"
    assert cov["readings_sealed"] == cov["readings_published"]
    assert cov["readings_published"] >= 30
    assert cov["verified"] is True


def test_the_live_chain_verifies_and_is_linked():
    entries = led.read_ledger(seal_readings.LEDGER)
    ok, problems = led.verify(entries)
    assert ok, problems
    assert entries[0]["prev_hash"] == led.GENESIS_PREV
    for prev, cur in zip(entries, entries[1:]):
        assert cur["prev_hash"] == prev["entry_hash"]
        assert cur["seq"] == prev["seq"] + 1


def test_discovery_skips_the_chains_themselves():
    """A chain is not a reading. Sealing one into another would be circular.

    Note the discriminator is the exact filename, not the name shape:
    `forecast-ledger-latest.json` is a published reading that happens to be
    called a ledger, and it must be sealed like any other.
    """
    discovered = dict(seal_readings.discover())
    paths = {os.path.basename(p) for p in discovered.values()}
    assert not (paths & seal_readings._NOT_A_READING)
    assert all(p.endswith(".json") and not p.endswith(".jsonl") for p in paths)
    assert "forecast-ledger" in discovered, "a reading named -ledger is still a reading"
    assert "generative-firewall" in discovered, "readings/latest.json must be covered"


def _isolated(monkeypatch, tmp_path):
    """Point the sweep at a throwaway readings directory and chain."""
    readings = tmp_path / "readings"
    readings.mkdir()
    monkeypatch.setattr(seal_readings, "READINGS", str(readings))
    monkeypatch.setattr(seal_readings, "LEDGER", str(tmp_path / "chain.jsonl"))
    monkeypatch.setattr(seal_readings, "ROOT", str(tmp_path))
    return readings


def _publish(readings, name: str, payload: dict) -> None:
    (readings / f"{name}-latest.json").write_text(json.dumps(payload),
                                                  encoding="utf-8")


def test_a_sweep_leaves_every_reading_sealed_against_its_own_bytes(monkeypatch, tmp_path):
    """The property the chain's timestamps claim.

    A seal is a statement that these exact bytes were published at this exact
    moment, and append-only means a false one can never be withdrawn. So the
    sweep must never leave a source sealed against content other than what is
    on disk when it runs.
    """
    readings = _isolated(monkeypatch, tmp_path)
    _publish(readings, "alpha", _reading(1))
    _publish(readings, "beta", _reading(2))
    seal_readings.seal_all()
    cov = seal_readings.coverage()
    assert cov["drifted"] == []
    assert cov["unsealed"] == []
    assert cov["readings_sealed"] == 2


def test_a_reading_refreshed_after_its_seal_is_reported_as_drifted(monkeypatch, tmp_path):
    """Drift is expected between runs and must be visible, never assumed away.

    Every reading has its own refresh workflow, so between sweeps some file is
    always newer than its seal. That is the honest cost of a snapshot cadence.
    It is reported rather than fatal, because the failure worth catching is the
    reverse: a chain generated on a stale checkout, sealing bytes the site had
    already superseded.
    """
    readings = _isolated(monkeypatch, tmp_path)
    _publish(readings, "alpha", _reading(1))
    _publish(readings, "beta", _reading(2))
    seal_readings.seal_all()

    _publish(readings, "beta", _reading(3))          # its own workflow refreshes it
    assert seal_readings.coverage()["drifted"] == ["beta"]

    seal_readings.seal_all()                         # the next sweep catches up
    assert seal_readings.coverage()["drifted"] == []


def test_an_unreadable_reading_is_named_not_counted_as_sealed(monkeypatch, tmp_path):
    readings = _isolated(monkeypatch, tmp_path)
    _publish(readings, "alpha", _reading(1))
    seal_readings.seal_all()
    (readings / "alpha-latest.json").write_text("{ truncated", encoding="utf-8")
    cov = seal_readings.coverage()
    assert cov["unreadable"] == ["alpha"]
    assert cov["drifted"] == []


def test_an_unreadable_reading_exits_2_so_the_caller_can_carry_on(monkeypatch, tmp_path):
    """A truncated file owned by another workflow is not a tamper finding.

    In CI this step sits between the producing steps and the commit step, so a
    hard failure here discards the erasure-ledger appends, the sealed registry
    entries and the refusal transcripts, all still uncommitted. Exit 2 keeps
    the problem loud and the record intact.
    """
    readings = _isolated(monkeypatch, tmp_path)
    _publish(readings, "alpha", _reading(1))
    (readings / "beta-latest.json").write_text("{ truncated", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["seal_readings.py"])

    assert seal_readings.main() == 2
    cov = seal_readings.coverage()
    assert cov["readings_sealed"] == 1          # the readable one still sealed
    assert cov["unreadable"] == ["beta"]


def test_a_broken_chain_exits_1_and_appends_nothing(monkeypatch, tmp_path):
    """The one condition worth failing a job over, and it must be separable."""
    readings = _isolated(monkeypatch, tmp_path)
    _publish(readings, "alpha", _reading(1))
    ledger = seal_readings.LEDGER
    led.append_seal(ledger, "alpha", _reading(1))
    entries = led.read_ledger(ledger)
    entries[0]["payload_sha256"] = "0" * 64
    with open(ledger, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    monkeypatch.setattr(sys, "argv", ["seal_readings.py"])

    assert seal_readings.main() == 1
    assert len(led.read_ledger(ledger)) == 1


def test_the_anchor_log_summary_is_not_swept_as_a_reading(monkeypatch, tmp_path):
    """An anchor record is no more a reading than anchors.jsonl is.

    It also runs before the anchor step, and anchors-latest.json takes a fresh
    ts on every anchor, so sweeping it would make the chain grow on every run
    with nothing measured behind the growth.
    """
    readings = _isolated(monkeypatch, tmp_path)
    _publish(readings, "alpha", _reading(1))
    _publish(readings, "anchors", {"ts": "2026-08-02T00:00:00+00:00"})
    assert set(dict(seal_readings.discover())) == {"alpha"}
    assert "anchors-latest.json" in seal_readings._NOT_A_READING


def test_resealing_an_unchanged_reading_adds_nothing(tmp_path):
    ledger = str(tmp_path / "chain.jsonl")
    r = _reading(1)
    assert led.append_seal(ledger, "demo", r) is not None
    assert led.append_seal(ledger, "demo", r) is None
    assert len(led.read_ledger(ledger)) == 1


def test_a_changed_reading_seals_again(tmp_path):
    ledger = str(tmp_path / "chain.jsonl")
    led.append_seal(ledger, "demo", _reading(1))
    led.append_seal(ledger, "demo", _reading(2))
    entries = led.read_ledger(ledger)
    assert len(entries) == 2
    assert entries[0]["payload_sha256"] != entries[1]["payload_sha256"]
    assert led.verify(entries)[0]


def test_rewriting_a_sealed_reading_is_caught(tmp_path):
    """The point of the whole exercise: a past reading cannot be quietly edited.

    The digest of what we published is committed, so a later edit no longer
    matches its seal, and anyone can run this check against the published file.
    """
    ledger = str(tmp_path / "chain.jsonl")
    published = _reading(1)
    entry = led.append_seal(ledger, "demo", published)

    rewritten = dict(published, value=999)   # someone changes history
    assert led.payload_digest(rewritten) != entry["payload_sha256"]
    assert led.payload_digest(published) == entry["payload_sha256"]


def test_a_tampered_chain_refuses_to_be_extended(tmp_path, capsys):
    """Never append onto a broken history: new valid-looking links would
    launder the break."""
    ledger = str(tmp_path / "chain.jsonl")
    led.append_seal(ledger, "demo", _reading(1))
    led.append_seal(ledger, "demo", _reading(2))

    entries = led.read_ledger(ledger)
    entries[0]["payload_sha256"] = "0" * 64          # tamper
    with open(ledger, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    ok, problems = led.verify(led.read_ledger(ledger))
    assert not ok and problems
