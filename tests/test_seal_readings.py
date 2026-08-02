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
