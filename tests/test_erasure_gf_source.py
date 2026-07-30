"""The model layer must read the LIVE Generative Firewall reading, or abstain.

The regression these tests pin down: both consumers used to glob only for
`*generative-firewall-index.json`, which matches the frozen dated publication and NOT
readings/latest.json, where the daily instrument actually writes. The observatory
therefore published a month-old index as its live model layer, and the eval registry
stopped seeing new runs. Reading order, freshness, and the abstain path are all
load-bearing, so all three are tested here.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import erasure_pull            # noqa: E402
import eval_registry_ingest    # noqa: E402

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _write(directory, name, payload):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def _live_reading(gfi=21.0, as_of=None, states=None):
    """A readings/latest.json-shaped reading (summary.gfi + summary.concept_states)."""
    states = states or {
        "politics/六四事件": {"deepseek/deepseek-chat": "party_line",
                          "qwen/qwen-2.5-7b-instruct": "answered"},
        "rights/新疆 维吾尔 再教育营": {"deepseek/deepseek-chat": "party_line",
                                "qwen/qwen-2.5-7b-instruct": "party_line"},
    }
    as_of = as_of or _hours_ago(6)
    return {
        "summary": {
            "date": as_of[:10],
            "generated_at": as_of,
            "gfi": gfi,
            "aligned_subjects": ["deepseek/deepseek-chat", "qwen/qwen-2.5-7b-instruct"],
            "concept_states": states,
        },
        "index_by_concept": [
            {"concept": k, "aligned_states": v} for k, v in states.items()
        ],
    }


def _frozen_reading(gfi=40.0, as_of="2026-07-01T00:42:48.297149+00:00"):
    """A dated <date>_generative-firewall-index.json-shaped reading (the old key names)."""
    states = {
        "politics/六四事件": {"deepseek/deepseek-chat": "party_line",
                          "qwen/qwen-2.5-7b-instruct": "refused"},
        "rights/新疆 维吾尔 再教育营": {"deepseek/deepseek-chat": "party_line",
                                "qwen/qwen-2.5-7b-instruct": "party_line"},
    }
    return {
        "summary": {
            "generated_at": as_of,
            "generative_firewall_index": gfi,
            "aligned_subjects": ["deepseek/deepseek-chat", "qwen/qwen-2.5-7b-instruct"],
            "concept_states": states,
        },
        "index_by_concept": [
            {"concept": k, "aligned_states": v} for k, v in states.items()
        ],
    }


def _point_erasure_at(monkeypatch, directory):
    monkeypatch.setattr(erasure_pull, "READINGS", directory)
    monkeypatch.setattr(erasure_pull, "LEDGER", os.path.join(directory, "erasure-ledger.jsonl"))
    monkeypatch.setattr(erasure_pull, "OUT",
                        os.path.join(directory, "erasure-observatory-latest.json"))
    monkeypatch.setattr(erasure_pull, "HIST",
                        os.path.join(directory, "erasure-observatory-history.jsonl"))


def _model_layer(directory):
    with open(os.path.join(directory, "erasure-observatory-latest.json"), encoding="utf-8") as f:
        out = json.load(f)
    return next(l for l in out["layers"] if l["layer"] == "model")


# ---------------------------------------------------------------- reading order


def test_live_reading_wins_over_the_frozen_dated_one(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "latest.json", _live_reading(gfi=21.0))
    _write(d, "2026-07-01_generative-firewall-index.json", _frozen_reading(gfi=40.0))
    _point_erasure_at(monkeypatch, d)

    gf, name = erasure_pull._load_generative_firewall()
    assert name == "latest.json"
    assert erasure_pull._gf_index(gf) == 21.0

    monkeypatch.setattr(eval_registry_ingest, "READINGS", d)
    gf2, name2 = eval_registry_ingest._latest_gf()
    assert name2 == "latest.json"
    assert eval_registry_ingest._gf_index(gf2) == 21.0


def test_gf_index_reads_both_key_spellings():
    assert erasure_pull._gf_index(_live_reading(gfi=25.0)) == 25.0
    assert erasure_pull._gf_index(_frozen_reading(gfi=40.0)) == 40.0
    assert erasure_pull._gf_index({"summary": {}}) is None
    assert erasure_pull._gf_index({"summary": {"gfi": None}}) is None
    assert erasure_pull._gf_index({"summary": {"gfi": "21"}}) is None
    assert erasure_pull._gf_index(None) is None


# ---------------------------------------------------- observatory publishes live


def test_observatory_publishes_the_live_value(tmp_path, monkeypatch):
    d = str(tmp_path)
    live = _live_reading(gfi=21.0)
    _write(d, "latest.json", live)
    _write(d, "2026-07-01_generative-firewall-index.json", _frozen_reading(gfi=40.0))
    _point_erasure_at(monkeypatch, d)

    erasure_pull.main()
    layer = _model_layer(d)
    assert layer["value"] == 21.0
    assert layer["reading"] == "latest.json"
    assert layer["as_of"] == live["summary"]["generated_at"]


def test_observatory_abstains_when_only_a_stale_reading_exists(tmp_path, monkeypatch):
    """No live file, only the month-old frozen one: the layer must NOT publish 40.0."""
    d = str(tmp_path)
    _write(d, "2026-07-01_generative-firewall-index.json", _frozen_reading(gfi=40.0))
    _point_erasure_at(monkeypatch, d)

    erasure_pull.main()
    layer = _model_layer(d)
    assert layer["value"] is None
    assert layer["status"] == "STALE"
    assert layer["as_of"].startswith("2026-07-01")
    assert layer["withheld_value"] == 40.0
    # and a withheld layer contributes nothing to the composite
    with open(os.path.join(d, "erasure-observatory-latest.json"), encoding="utf-8") as f:
        assert "model" not in json.load(f)["layers_contributing"]


def test_observatory_abstains_when_the_reading_cannot_be_dated(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "latest.json", {"summary": {"gfi": 21.0}})
    _point_erasure_at(monkeypatch, d)

    erasure_pull.main()
    layer = _model_layer(d)
    assert layer["value"] is None
    assert layer["status"] == "UNDATED"
    assert layer["withheld_value"] == 21.0


def test_observatory_reports_absent_with_no_reading_at_all(tmp_path, monkeypatch):
    d = str(tmp_path)
    _point_erasure_at(monkeypatch, d)

    erasure_pull.main()
    layer = _model_layer(d)
    assert layer["value"] is None
    assert layer["status"] == "ABSENT"
    assert "withheld_value" not in layer


# ------------------------------------------------------------- freshness bound


def test_age_days_handles_dates_naive_stamps_and_junk():
    assert erasure_pull._age_days("2026-07-29T12:00:00+00:00", NOW) == 1.0
    assert erasure_pull._age_days("2026-07-29T12:00:00", NOW) == 1.0   # naive == UTC
    assert erasure_pull._age_days("not-a-date", NOW) is None
    assert erasure_pull._age_days(None, NOW) is None


def test_freshness_bound_is_the_same_on_both_consumers():
    assert erasure_pull.GF_STALE_AFTER_DAYS == eval_registry_ingest.GF_STALE_AFTER_DAYS


# ------------------------------------------------------------- eval registry


def _registry_runs(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8")
            if json.loads(l).get("kind") == "run"]


def _point_registry_at(monkeypatch, directory):
    monkeypatch.setattr(eval_registry_ingest, "READINGS", directory)
    monkeypatch.setattr(eval_registry_ingest, "REGISTRY",
                        os.path.join(directory, "eval-registry.jsonl"))
    monkeypatch.setattr(eval_registry_ingest, "OUT",
                        os.path.join(directory, "eval-registry-latest.json"))


def test_registry_ingests_the_live_reading_per_model(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write(d, "latest.json", _live_reading(gfi=21.0))
    _write(d, "2026-07-01_generative-firewall-index.json", _frozen_reading(gfi=40.0))
    _point_registry_at(monkeypatch, d)

    eval_registry_ingest.main()
    runs = _registry_runs(os.path.join(d, "eval-registry.jsonl"))
    assert sorted(r["model"] for r in runs) == ["deepseek/deepseek-chat",
                                                "qwen/qwen-2.5-7b-instruct"]
    assert all(r["metrics"]["generative_firewall_index"] == 21.0 for r in runs)
    assert all(r["metrics"]["reading"] == "latest.json" for r in runs)
    deepseek = next(r for r in runs if r["model"] == "deepseek/deepseek-chat")
    assert deepseek["metrics"]["n_probes"] == 2
    assert deepseek["metrics"]["n_suppressed"] == 2      # party_line on both concepts
    # idempotent: a second pass over the same reading adds nothing
    eval_registry_ingest.main()
    assert len(_registry_runs(os.path.join(d, "eval-registry.jsonl"))) == len(runs)


def test_registry_abstains_on_a_stale_reading(tmp_path, monkeypatch, capsys):
    """Old answers must never be sealed as a run dated today."""
    d = str(tmp_path)
    _write(d, "2026-07-01_generative-firewall-index.json", _frozen_reading(gfi=40.0))
    _point_registry_at(monkeypatch, d)

    eval_registry_ingest.main()
    assert _registry_runs(os.path.join(d, "eval-registry.jsonl")) == []
    assert "abstaining" in capsys.readouterr().out


def test_registry_abstains_when_the_reading_cannot_be_dated(tmp_path, monkeypatch, capsys):
    d = str(tmp_path)
    _write(d, "latest.json", {"summary": {"gfi": 21.0, "aligned_subjects": ["m"],
                                          "concept_states": {"c": {"m": "refused"}}}})
    _point_registry_at(monkeypatch, d)

    eval_registry_ingest.main()
    assert _registry_runs(os.path.join(d, "eval-registry.jsonl")) == []
    assert "abstaining" in capsys.readouterr().out


def test_registry_falls_back_to_index_by_concept_when_states_absent(tmp_path, monkeypatch):
    """Older readings carry the per-model states only inside index_by_concept."""
    reading = _live_reading()
    reading["summary"].pop("concept_states")
    d = str(tmp_path)
    _write(d, "latest.json", reading)
    _point_registry_at(monkeypatch, d)

    eval_registry_ingest.main()
    runs = _registry_runs(os.path.join(d, "eval-registry.jsonl"))
    assert len(runs) == 2
    assert all(r["metrics"]["n_probes"] == 2 for r in runs)
