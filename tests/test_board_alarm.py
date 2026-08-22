"""Board alarm: multiplicity control and layer coincidence.

The properties that matter are the ones a reader relies on when the board says
"something is happening": that the selection controls false discoveries across
all twelve signals rather than each one separately, that the merged e-value is a
legitimate e-value, and that a layer holding a single signal never claims to
corroborate itself.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from processors.board_alarm import (
    FDR_ALPHA,
    LAYERS,
    LAYER_ELEVATED_E,
    build_reading,
    ebh,
    layer_evalues,
)
from processors.conformal_events import merge_e


ROOT = Path(__file__).resolve().parents[1]


def test_refresh_installs_the_pinned_test_environment_before_validation():
    workflow = (
        ROOT / ".github" / "workflows" / "board-alarm-refresh.yml"
    ).read_text(encoding="utf-8")
    install = workflow.index("- name: Install the pinned offline test runner")
    build = workflow.index("- name: Recompute the board alarm")

    assert install < build
    assert "python -m pip install --quiet --require-hashes" in workflow[install:build]
    assert "-r .github/osint-china-ci-requirements.txt" in workflow[install:build]


# ── e-BH selection ──────────────────────────────────────────────────────────────

def test_ebh_selects_nothing_when_all_evalues_are_ordinary():
    # e-values near 1 are what "nothing is happening" looks like
    assert ebh({f"s{i}": 1.0 for i in range(12)})["selected"] == []


def test_ebh_threshold_matches_the_published_formula():
    """With K signals at alpha, selecting k requires e_(k) >= K/(alpha*k)."""
    K, alpha = 10, 0.1
    # one enormous e-value: needs to clear K/(alpha*1) = 100
    ev = {f"s{i}": 1.0 for i in range(K - 1)}
    ev["big"] = 101.0
    r = ebh(ev, alpha)
    assert r["selected"] == ["big"] and r["k"] == 1
    assert r["threshold"] == 100.0

    # just under the bar selects nothing — the boundary is not fudged
    ev["big"] = 99.0
    assert ebh(ev, alpha)["selected"] == []


def test_ebh_selects_more_when_many_signals_are_jointly_elevated():
    """Two signals each clearing K/(alpha*2) are selected together even though
    neither clears the k=1 bar — the step-up property."""
    K, alpha = 10, 0.1
    ev = {f"s{i}": 1.0 for i in range(K - 2)}
    ev["a"], ev["b"] = 60.0, 55.0          # K/(alpha*2) = 50, K/(alpha*1) = 100
    r = ebh(ev, alpha)
    assert r["selected"] == ["a", "b"] and r["k"] == 2


def test_ebh_is_more_conservative_than_reporting_each_signal_alone():
    """A signal at its own ALARM threshold need not survive the family-wise pass.
    This is the whole point: twelve private guarantees are not a board guarantee."""
    ev = {f"s{i}": 1.0 for i in range(11)}
    ev["loud"] = 500.0                      # would ALARM on its own
    ev_alone = {"loud": 500.0}
    assert ebh(ev_alone)["selected"] == ["loud"]
    # with eleven companions the bar is 12/(0.1*1) = 120; 500 still clears it,
    # so check the bar itself rose rather than asserting a flip
    assert ebh(ev)["threshold"] > ebh(ev_alone)["threshold"]


def test_ebh_empty_input_is_not_an_error():
    r = ebh({})
    assert r["selected"] == [] and r["n_tested"] == 0


# ── merging ─────────────────────────────────────────────────────────────────────

def test_merge_is_the_arithmetic_mean():
    """Vovk & Wang: the MEAN of dependent e-values is valid. A product is not,
    and would explode the moment two correlated signals both moved."""
    assert merge_e([1.0, 3.0]) == 2.0
    assert merge_e([]) == 1.0


def test_merged_evalue_of_calm_signals_stays_near_one():
    assert merge_e([1.0] * 12) == 1.0


def test_layer_with_one_signal_is_marked_uncorroborated():
    ev = {"refusal_drift": 40.0}
    layers = layer_evalues(ev)
    assert layers["model"]["n_signals"] == 1
    assert layers["model"]["corroborated"] is False
    # ...while a layer with several reporting signals is corroborated
    ev2 = {"ooni_gfw": 3.0, "censored_planet": 4.0}
    assert layer_evalues(ev2)["network"]["corroborated"] is True


def test_layer_is_elevated_only_above_the_published_threshold():
    below = layer_evalues({"ooni_gfw": LAYER_ELEVATED_E - 0.1})
    above = layer_evalues({"ooni_gfw": LAYER_ELEVATED_E + 0.1})
    assert below["network"]["state"] == "calm"
    assert above["network"]["state"] == "elevated"


def test_absent_layer_reports_no_data_not_calm():
    """A layer nobody is reporting must not read as an all-clear."""
    assert layer_evalues({"ooni_gfw": 1.0})["model"]["state"] == "no_data"


def test_every_registered_signal_belongs_to_exactly_one_layer():
    from processors.board_alarm import SIGNAL_FILES
    assigned = [s for members in LAYERS.values() for s in members]
    assert len(assigned) == len(set(assigned)), "a signal is in two layers"
    for name in SIGNAL_FILES:  # the conformal registry plus the churn signal
        assert name in assigned, f"{name} is monitored but assigned to no layer"
    for name in assigned:
        assert name in SIGNAL_FILES, f"{name} is in a layer but nothing produces it"


# ── whole reading ───────────────────────────────────────────────────────────────

def _write(tmp_path, filename, rows):
    """Rows stamped fresh: the board now refuses to treat a row it cannot date —
    or one past its signal's MAX_AGE_HOURS bound — as evidence, so an undated
    fixture would test the stale path instead of the one it means to."""
    now = datetime.now(timezone.utc)
    with open(tmp_path / filename, "w", encoding="utf-8") as fh:
        for i, r in enumerate(rows):
            stamped = {"generated_at": (now - timedelta(minutes=len(rows) - i)).isoformat(), **r}
            fh.write(json.dumps(stamped) + "\n")


def test_build_reading_over_calm_fixtures_reports_nothing_happening(tmp_path):
    rng = random.Random(5)
    _write(tmp_path, "ooni-gfw-history.jsonl",
           [{"gfw_index": rng.gauss(50, 2)} for _ in range(40)])
    _write(tmp_path, "ddti-history.jsonl",
           [{"top_threat": rng.gauss(2, 0.2), "n_new": 3} for _ in range(40)])
    r = build_reading(tmp_path)
    assert r["layer_coincidence"] == 0
    assert r["fdr_selection"]["selected"] == []
    assert "no signal exceeds" in r["headline"]


def test_multi_layer_comovement_is_the_headline(tmp_path):
    """A network signal and a content signal shifting together must surface as
    co-movement — the event a per-signal board is built to miss."""
    rng = random.Random(9)
    _write(tmp_path, "ooni-gfw-history.jsonl",
           [{"gfw_index": rng.gauss(50, 2)} for _ in range(40)] +
           [{"gfw_index": rng.gauss(80, 2)} for _ in range(12)])
    _write(tmp_path, "ddti-history.jsonl",
           [{"top_threat": rng.gauss(2, 0.2), "n_new": 3} for _ in range(40)] +
           [{"top_threat": rng.gauss(6, 0.2), "n_new": 3} for _ in range(12)])
    r = build_reading(tmp_path)
    assert r["layer_coincidence"] >= 2, r["layers"]
    assert "MULTI-LAYER" in r["headline"]
    assert set(r["elevated_layers"]) >= {"network", "content"}


def test_warming_signals_contribute_no_evalue(tmp_path):
    """Too-short history is neither evidence of calm nor of change."""
    _write(tmp_path, "ooni-gfw-history.jsonl", [{"gfw_index": 50.0} for _ in range(3)])
    r = build_reading(tmp_path)
    assert r["signals"]["ooni_gfw"]["state"] == "warming_up"
    assert r["fdr_selection"]["n_tested"] == 0
    assert r["board_e_value"] is None


def test_reading_carries_its_guarantee_and_caveats(tmp_path):
    _write(tmp_path, "ooni-gfw-history.jsonl", [{"gfw_index": 50.0} for _ in range(20)])
    r = build_reading(tmp_path)
    assert "arbitrary dependence" in r["board_guarantee"]
    assert any("common cause" in c for c in r["caveats"])
    assert r["method"] and r["readings_to_days"]["ooni_gfw"]


def test_reading_is_deterministic(tmp_path):
    _write(tmp_path, "ooni-gfw-history.jsonl",
           [{"gfw_index": float(50 + i % 5)} for i in range(30)])
    a, b = build_reading(tmp_path), build_reading(tmp_path)
    a.pop("generated_at"); b.pop("generated_at")
    assert a == b


def test_alpha_is_honoured(tmp_path):
    ev = {"a": 30.0, **{f"s{i}": 1.0 for i in range(9)}}
    # a looser alpha selects at least as much as a stricter one
    loose = ebh(ev, 0.5)["k"]
    strict = ebh(ev, 0.01)["k"]
    assert loose >= strict
    assert FDR_ALPHA == 0.10
