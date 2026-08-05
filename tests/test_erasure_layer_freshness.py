"""Every layer must be shown to be current, not just the one that got caught.

    PYTHONPATH=. python3 -m pytest tests/test_erasure_layer_freshness.py -q

The model layer earned its freshness bound the hard way: for 29 days the observatory
published a frozen 2026-07-01 number as today's model erasure, and the composite averaged
it in. That was fixed. The network layer, sitting beside it in the same function, still had
NO freshness check at all — it read ooni-gfw-latest.json and published whatever number was
in the file, whenever it had been written. Same bug, one layer over, waiting for OONI's
refresh to fail the way CDT's feeds did.

So these tests pin the rule at the level of the class rather than the instance:

  * a layer older than its bound is WITHHELD, with its true as-of date and the number being
    declined, never carried forward as today's;
  * a withheld layer does not enter the composite, and layers_contributing says so;
  * a fresh layer publishes its as_of and age_days so a reader can check the claim;
  * an undatable reading is treated as unusable, because a reading that cannot be dated
    cannot be shown to be current.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import erasure_pull  # noqa: E402


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _write(directory, name, payload):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _ooni(index=57.9, as_of=None, key="generated_at"):
    doc = {"source": "OONI", "scope": "CN", "gfw_index": index,
           "n_measurements": 1234, "tests": {}}
    if as_of is not None:
        doc[key] = as_of
    return doc


def _gf(gfi=25.0, as_of=None):
    return {"summary": {"generated_at": as_of or _hours_ago(6), "date": "2026-07-30",
                        "gfi": gfi, "concept_states": {}}}


def _point_at(monkeypatch, directory):
    monkeypatch.setattr(erasure_pull, "READINGS", directory)
    monkeypatch.setattr(erasure_pull, "LEDGER", os.path.join(directory, "erasure-ledger.jsonl"))
    monkeypatch.setattr(erasure_pull, "OUT",
                        os.path.join(directory, "erasure-observatory-latest.json"))
    monkeypatch.setattr(erasure_pull, "HIST",
                        os.path.join(directory, "erasure-observatory-history.jsonl"))


def _run(directory):
    erasure_pull.main()
    with open(os.path.join(directory, "erasure-observatory-latest.json"), encoding="utf-8") as f:
        return json.load(f)


def _layer(out, name):
    return next(l for l in out["layers"] if l["layer"] == name)


# ── the as-of reader ─────────────────────────────────────────────────────────

def test_reading_as_of_finds_both_shapes():
    """Readings in this repo put their timestamp top-level (ooni) or under summary (gf)."""
    assert erasure_pull._reading_as_of({"generated_at": "2026-07-30T14:00:00+00:00"}) \
        == "2026-07-30T14:00:00+00:00"
    assert erasure_pull._reading_as_of({"summary": {"generated_at": "2026-07-01T00:00:00+00:00"}}) \
        == "2026-07-01T00:00:00+00:00"
    assert erasure_pull._reading_as_of({"as_of": "2026-07-30T14:00:00+00:00"}) \
        == "2026-07-30T14:00:00+00:00"
    assert erasure_pull._reading_as_of({}) is None
    assert erasure_pull._reading_as_of(None) is None
    assert erasure_pull._reading_as_of({"generated_at": 12345}) is None


# ── the network layer, which had no bound at all ─────────────────────────────

def test_fresh_network_layer_publishes_and_dates_itself(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "ooni-gfw-latest.json", _ooni(57.9, as_of=_hours_ago(3)))
    _point_at(monkeypatch, d)

    net = _layer(_run(d), "network")

    assert net["value"] == 57.9
    assert net["as_of"] is not None
    assert net["age_days"] < 1
    assert "status" not in net


def test_stale_network_layer_is_withheld_not_republished_as_today(tmp_path, monkeypatch):
    """The exact failure the model layer had: a frozen number published under a fresh
    generated_at. Nine days is well past the two-day bound."""
    d = str(tmp_path)
    _write(d, "ooni-gfw-latest.json", _ooni(57.9, as_of=_hours_ago(24 * 9)))
    _point_at(monkeypatch, d)

    out = _run(d)
    net = _layer(out, "network")

    assert net["value"] is None
    assert net["status"] == "STALE"
    assert net["withheld_value"] == 57.9          # says what it declined to publish
    assert "9.0 days old" in net["reason"]
    assert "network" not in out["layers_contributing"]


def test_a_withheld_layer_never_enters_the_composite(tmp_path, monkeypatch):
    """A stale layer averaged into the index is the whole bug. With the network layer
    withheld and only the model layer fresh, the composite must BE the model layer."""
    d = str(tmp_path)
    _write(d, "ooni-gfw-latest.json", _ooni(57.9, as_of=_hours_ago(24 * 30)))
    _write(d, "latest.json", _gf(gfi=25.0))
    _point_at(monkeypatch, d)

    out = _run(d)

    assert out["layers_contributing"] == ["model"]
    assert out["erasure_index"] == 25.0            # not the 41.5 mean of 57.9 and 25.0


def test_undated_network_reading_is_unusable_not_assumed_current(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "ooni-gfw-latest.json", _ooni(57.9, as_of=None))
    _point_at(monkeypatch, d)

    net = _layer(_run(d), "network")

    assert net["value"] is None
    assert net["status"] == "UNDATED"
    assert net["withheld_value"] == 57.9


def test_a_stale_layer_is_still_sealed_into_the_ledger(tmp_path, monkeypatch):
    """Withholding a value from the published index is not a reason to leave the
    observation out of the tamper-evident record. We saw it; the chain should say so."""
    d = str(tmp_path)
    _write(d, "ooni-gfw-latest.json", _ooni(57.9, as_of=_hours_ago(24 * 9)))
    _point_at(monkeypatch, d)

    out = _run(d)

    assert any(s["source"] == "ooni-gfw" for s in out["integrity"]["sealed_this_run"])


def test_a_malformed_network_reading_is_absent_not_zero(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "ooni-gfw-latest.json", {"gfw_index": "fifty-seven"})
    _point_at(monkeypatch, d)

    net = _layer(_run(d), "network")

    assert net["value"] is None
    assert net["status"] == "ABSENT"
    assert "withheld_value" not in net             # nothing readable to withhold


def test_a_boolean_is_not_a_number(tmp_path, monkeypatch):
    """bool is a subclass of int in Python, so True would otherwise sail through
    isinstance(x, (int, float)) and be published as an index of 1.0."""
    d = str(tmp_path)
    _write(d, "ooni-gfw-latest.json", {"gfw_index": True, "generated_at": _hours_ago(1)})
    _point_at(monkeypatch, d)

    assert _layer(_run(d), "network")["status"] == "ABSENT"


def test_both_layers_stale_yields_no_index_rather_than_a_made_up_one(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "ooni-gfw-latest.json", _ooni(57.9, as_of=_hours_ago(24 * 30)))
    _write(d, "latest.json", _gf(gfi=25.0, as_of=_hours_ago(24 * 30)))
    _point_at(monkeypatch, d)

    out = _run(d)

    assert out["layers_contributing"] == []
    assert out["erasure_index"] is None


# ── the class, not the instances ─────────────────────────────────────────────
# The tests above were written for the model and network layers, and this file's own
# docstring claimed to "pin the rule at the level of the class". It did not: the narrative
# layer and BOTH cross-checks still had no bound and no as_of at all, in the same function,
# after the network layer was fixed. Same instance-not-class failure, two layers down.
# These tests are about the TABLE, so a fourth input cannot be added unbounded.

def test_every_input_this_runner_reads_is_bounded():
    """The registry assertion. _bounded_reading does a hard dict lookup, so an input added
    without an entry is a KeyError at run time rather than a silent unbounded publish — this
    pins that the known inputs are all present and that the table is not empty."""
    t = erasure_pull.INPUT_MAX_AGE_HOURS
    assert set(t) == {
        "ooni-gfw-latest.json", "baike-redaction-latest.json",
        "censored-planet-latest.json", "net4people-latest.json",
    }, "an input was added or removed without updating this guard"
    assert all(isinstance(v, float) and v > 0 for v in t.values())


def test_a_stale_narrative_layer_is_withheld_not_published_as_today(tmp_path, monkeypatch):
    """The branch that did not exist. A readable but old Baike index used to publish as the
    current narrative value — the exact bug the model layer was fixed for."""
    d = str(tmp_path)
    _write(d, "baike-redaction-latest.json",
           {"generated_at": _hours_ago(24 * 30), "rewrite_index": 71.0})
    _point_at(monkeypatch, d)

    out = _run(d)
    nar = _layer(out, "narrative")

    assert nar["value"] is None
    assert nar["status"] == "STALE"
    assert nar["withheld_value"] == 71.0
    assert "narrative" not in out["layers_contributing"]


def test_a_fresh_narrative_layer_publishes_and_dates_itself(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "baike-redaction-latest.json",
           {"generated_at": _hours_ago(2), "rewrite_index": 71.0})
    _point_at(monkeypatch, d)

    nar = _layer(_run(d), "narrative")

    assert nar["value"] == 71.0
    assert nar["as_of"] is not None and nar["age_hours"] < 3


def test_a_stale_null_narrative_reading_is_stale_with_age_and_path(tmp_path, monkeypatch):
    """A retained abstention ages too; it must not appear as an indefinitely armed layer."""
    d = str(tmp_path)
    _write(d, "baike-redaction-latest.json", {
        "generated_at": _hours_ago(24 * 30), "rewrite_index": None,
        "reason": "Baike collection disabled pending authorized access",
    })
    _point_at(monkeypatch, d)

    nar = _layer(_run(d), "narrative")

    assert nar["value"] is None
    assert nar["status"] == "STALE"
    assert nar["age_hours"] > 36
    assert nar["reading"] == "baike-redaction-latest.json"
    assert "readings/baike-redaction-latest.json" in nar["reason"]
    assert "30.0 days old" in nar["reason"]


def test_a_current_null_narrative_reading_is_unavailable_not_armed(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "baike-redaction-latest.json", {
        "generated_at": _hours_ago(2), "rewrite_index": None,
        "reason": "Baike collection disabled pending authorized access",
    })
    _point_at(monkeypatch, d)

    nar = _layer(_run(d), "narrative")

    assert nar["value"] is None
    assert nar["status"] == "UNAVAILABLE"
    assert nar["reading"] == "baike-redaction-latest.json"
    assert nar["age_hours"] < 3


def test_a_stale_cross_check_is_dropped_and_the_drop_is_stated(tmp_path, monkeypatch):
    """A cross-check is a corroborating claim a reader weighs against the layers, so a stale
    one is a stale claim. Silently omitting it would read as 'we never had that check'."""
    d = str(tmp_path)
    _write(d, "censored-planet-latest.json",
           {"generated_at": _hours_ago(24 * 20), "cn_interference_rate_pct": 4.45})
    _point_at(monkeypatch, d)

    out = _run(d)

    assert [c["name"] for c in out["cross_checks"]] == []
    withheld = out["cross_checks_withheld"]
    assert len(withheld) == 1
    assert withheld[0]["withheld_value"] == 4.45
    assert withheld[0]["status"] == "STALE"


def test_a_fresh_cross_check_carries_its_age(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "censored-planet-latest.json",
           {"generated_at": _hours_ago(3), "cn_interference_rate_pct": 4.45})
    _point_at(monkeypatch, d)

    out = _run(d)

    assert out["cross_checks_withheld"] == []
    cc = out["cross_checks"][0]
    assert cc["value"] == 4.45 and cc["as_of"] and cc["age_hours"] < 4
