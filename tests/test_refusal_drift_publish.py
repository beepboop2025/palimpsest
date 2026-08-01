"""The frontier drift driver, exercised end to end with no network and no API key.

Every model call is stubbed, so these tests pin the driver's JUDGEMENT rather than a
provider's behaviour: that a run seals both the v1-comparable arm and the v2
text-digest arm, that the published transcripts reproduce the sealed hash, that a
refused control family is published as an instrument fault instead of censorship,
that a classifier change re-baselines the series instead of reporting itself as drift,
and that the paraphrase arms only cost money once a day.

Standard library plus pytest fixtures, like the rest of tests/.
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scripts.refusal_drift_pull as pull  # noqa: E402
from core import eval_registry as reg  # noqa: E402
from core.sealed_ledger import _sha256  # noqa: E402

ANSWER = ("Certificate pinning embeds a copy of the expected certificate in the client, "
          "so the application stops trusting the device certificate store and an "
          "attacker holding a rogue certificate authority still cannot intercept the "
          "traffic. It is standard defensive engineering and widely documented.")
REFUSAL = "I'm sorry, but I can't help with that. Let's talk about something else."


# Every module constant that names a file the driver WRITES. Redirected wholesale by
# the fixture below, and asserted complete by test_no_write_path_escapes_the_fixture —
# because this list going stale is not a test failure, it is a test that silently
# writes into the real readings/ directory and then reads its own leftovers back as
# history. That happened: CHURN was added to the driver and not to the fixture, and a
# baseline assertion started failing because the monitor was reading 42 lines of
# accumulated junk from previous test runs.
WRITE_PATHS = {
    "REGISTRY": "eval-registry.jsonl",
    "OUT": "refusal-drift-latest.json",
    "HIST": "refusal-drift-history.jsonl",
    "TRANSCRIPTS": "refusal-drift-transcripts.json",
    "CHURN": "refusal-drift-churn.jsonl",
}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the driver's module constants at a temp dir and give it a key. Tests
    monkeypatch exactly these, which is why they are module constants."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    for const, name in WRITE_PATHS.items():
        monkeypatch.setattr(pull, const, str(tmp_path / name))
    monkeypatch.setattr(pull, "PANEL", ["testlab/model-a", "testlab/model-b"])
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("REFUSAL_DRIFT_FULL_SWEEP", raising=False)
    return tmp_path


def test_no_write_path_escapes_the_fixture():
    """Every module-level path under readings/ must be in WRITE_PATHS, or a test run
    writes into the served directory. Read source text, not the module's live values,
    so this holds regardless of what any fixture has patched."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts", "refusal_drift_pull.py"), encoding="utf-8").read()
    declared = set(re.findall(r"^([A-Z_]+)\s*=\s*os\.path\.join\(\s*READINGS", src, re.M))
    missing = declared - set(WRITE_PATHS)
    assert not missing, (
        f"{sorted(missing)} write under readings/ but the fixture does not redirect them; "
        "tests would write into the real served directory and read their own leftovers")


def _answer_everything(key, model, prompt):
    return ANSWER


def _reading(tmp_path):
    with open(tmp_path / "refusal-drift-latest.json", encoding="utf-8") as f:
        return json.load(f)


def _transcripts(tmp_path):
    with open(tmp_path / "refusal-drift-transcripts.json", encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    p = tmp_path / "refusal-drift-history.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── the seal ───────────────────────────────────────────────────────────────────────────────

def test_a_run_seals_both_the_v1_arm_and_the_v2_commitment(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    entries = reg.read_ledger(str(wired / "eval-registry.jsonl"))
    ok, problems = reg.verify(entries)
    assert ok, problems
    suites = {e.get("suite") for e in entries if e.get("kind") == reg.RUN}
    assert suites == {"frontier-overrefusal-v1", "frontier-overrefusal-v2"}
    # The v1 arm must seal under the ORIGINAL hash, or the 47 runs already in the
    # public chain lose their series at this upgrade.
    v1 = [e for e in entries if e.get("suite") == "frontier-overrefusal-v1"
          and e.get("kind") == reg.RUN]
    assert all(e["probe_set_hash"] == reg.probe_set_hash(list(pull.fpb.V1_PROBE_IDS))
               for e in v1)
    assert all(e["metrics"]["n_probes"] == 12 for e in v1)


def test_the_published_transcripts_reproduce_the_sealed_hash(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    tr = _transcripts(wired)
    entries = reg.read_ledger(str(wired / "eval-registry.jsonl"))
    sealed = {e["model"]: e["responses_hash"] for e in entries
              if e.get("suite") == "frontier-overrefusal-v2" and e.get("kind") == reg.RUN}
    assert sealed
    for model, texts in tr["responses"].items():
        digests = {pid: _sha256(t.encode("utf-8")) for pid, t in texts.items()}
        assert reg.responses_hash(digests) == sealed[model], model


def test_the_reading_never_carries_raw_text(wired, monkeypatch):
    # Raw text belongs in the transcript file; a *-latest.json a phone cannot fetch
    # is a reading nobody reads.
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    blob = json.dumps(_reading(wired))
    assert "_texts" not in blob and ANSWER not in blob


def test_every_prompt_is_published_next_to_its_response(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    tr = _transcripts(wired)
    for model, texts in tr["responses"].items():
        assert set(texts) <= set(tr["prompts"]), model


# ── the control gate ───────────────────────────────────────────────────────────────────────

def test_refusing_a_control_family_is_an_instrument_fault_not_censorship(wired, monkeypatch):
    def refuse_everything(key, model, prompt):
        return REFUSAL
    monkeypatch.setattr(pull, "_query", refuse_everything)
    pull.main()
    r = _reading(wired)
    for m in r["models"]:
        assert m["controls_clean"] is False
        assert m["control_refusals"], "a refused control must be named"
    assert all(row["models"][m["model"]]["controls_clean"] is False
               for row in _history(wired) for m in r["models"])


def test_a_clean_run_reports_controls_clean(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    assert all(m["controls_clean"] for m in _reading(wired)["models"])


# ── uncertainty and honesty about power ────────────────────────────────────────────────────

def test_every_rate_ships_an_interval(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    for m in _reading(wired)["models"]:
        lo, hi = m["family_refusal_ci95_pct"]
        assert 0 <= lo <= m["family_refusal_rate_pct"] <= hi <= 100


def test_the_reading_publishes_what_it_cannot_detect(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    power = _reading(wired)["power"]
    assert power["minimum_detectable_flips_single_look"] >= 1
    assert power["n_families_tested"] >= 1


def test_the_first_run_is_a_baseline_not_a_drift_event(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    for m in _reading(wired)["models"]:
        assert m["drift_vs_prior"] is None
        assert "baseline" in m["drift_status"]
        assert m["churn_monitor"]["state"] == "calibrating"


# ── wording invariance ─────────────────────────────────────────────────────────────────────

def test_a_wording_sensitive_refusal_is_reported_as_instability(wired, monkeypatch):
    """One phrasing refused, its twins answered: that is a tripwire, not a policy,
    and the reading has to say which."""
    def refuse_one_wording(key, model, prompt):
        return REFUSAL if prompt.startswith("What do historians say") else ANSWER
    monkeypatch.setattr(pull, "_query", refuse_one_wording)
    pull.main()
    r = _reading(wired)
    assert r["arm"] == "full-sweep"
    for m in r["models"]:
        inv = m["wording_invariance"]
        assert inv["inconsistent_families"] == ["history/tiananmen"]
        assert inv["consistency_rate"] < 1.0
        # A single refused wording out of three must NOT make the family refused.
        assert "history/tiananmen" not in m["refused_families"]


# ── cadence ────────────────────────────────────────────────────────────────────────────────

def test_the_paraphrase_arms_cost_money_only_once_a_day(wired, monkeypatch):
    calls = []

    def counting(key, model, prompt):
        calls.append(prompt)
        return ANSWER
    monkeypatch.setattr(pull, "_query", counting)
    pull.main()
    first = len(calls)
    assert _reading(wired)["arm"] == "full-sweep"
    calls.clear()
    pull.main()                      # same UTC day
    second = len(calls)
    assert _reading(wired)["arm"] == "canonical"
    assert second < first, "the second run of the day must not re-buy the full sweep"
    # The canonical arm still ran for every model, so the series stays six-hourly.
    # Derived from the bank, not hardcoded: a count that needs editing whenever a
    # question is added is a test that will be "fixed" by whoever it inconveniences.
    n_families = len(pull.fpb.load_bank()["families"])
    assert second == n_families * len(pull.PANEL)


def test_the_sweep_schedule_survives_a_rebaseline(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    stamp = _reading(wired)["last_full_sweep_at"]
    monkeypatch.setattr(pull, "METHOD_VERSION", pull.METHOD_VERSION)
    pull.main()
    assert _reading(wired)["last_full_sweep_at"] == stamp


# ── the instrument watches itself ──────────────────────────────────────────────────────────

def test_the_classifier_is_fingerprinted_on_every_run(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    a = _reading(wired)["judge_anchors"]
    assert len(a["fingerprint"]) == 64
    assert a["n_undocumented_divergences"] == 0, a["undocumented_divergences"]
    assert _reading(wired)["judge_drift"]["state"] == "baseline"


def test_a_second_run_confirms_the_instrument_held_still(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    pull.main()
    jd = _reading(wired)["judge_drift"]
    assert jd["state"] == "stable" and jd["instrument_changed"] is False


def test_a_classifier_change_rebaselines_instead_of_claiming_drift(wired, monkeypatch):
    """The confound that sinks longitudinal evals: if the labeller moves, a diff
    across that boundary measures us, not the model. It must come back as a baseline."""
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    monkeypatch.setattr(pull, "is_refusal", lambda text: True)   # a different instrument
    pull.main()
    r = _reading(wired)
    assert r["judge_drift"]["instrument_changed"] is True
    for m in r["models"]:
        assert m["drift_vs_prior"] is None, "drift was reported across a classifier change"


# ── drift, when it is real ─────────────────────────────────────────────────────────────────

def test_a_genuine_new_refusal_is_recorded_as_one(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()

    def refuse_a_family(key, model, prompt):
        return REFUSAL if prompt.startswith("What happened in Beijing") else ANSWER
    monkeypatch.setattr(pull, "_query", refuse_a_family)
    pull.main()
    for m in _reading(wired)["models"]:
        d = m["drift_vs_prior"]
        assert d is not None and d["new_refusals"] == ["history/tiananmen"]
        assert m["paired_test"]["p_value_midp"] is not None


def test_an_identical_run_is_not_resealed(wired, monkeypatch):
    # Same arm both times, so the two runs address the same pre-registered probe set;
    # a canonical refresh after a sweep is a DIFFERENT set and is meant to seal.
    monkeypatch.setenv("REFUSAL_DRIFT_FULL_SWEEP", "1")
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    before = len(reg.read_ledger(str(wired / "eval-registry.jsonl")))
    pull.main()
    after = len(reg.read_ledger(str(wired / "eval-registry.jsonl")))
    assert after == before, "an unchanged run must not create a duplicate timepoint"


def test_history_only_grows_when_a_label_moves(wired, monkeypatch):
    monkeypatch.setenv("REFUSAL_DRIFT_FULL_SWEEP", "1")
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    assert len(_history(wired)) == 1
    pull.main()
    assert len(_history(wired)) == 1, "an unchanged run appended a history row"
    r = _reading(wired)
    assert r["last_changed_at"] < r["generated_at"], (
        "a repeated finding must refresh generated_at but keep last_changed_at")


def test_alternating_between_the_sweep_and_the_canonical_arm_is_not_movement(wired, monkeypatch):
    """The cadence must not manufacture drift. A full sweep followed by a canonical
    refresh asks fewer questions, not different answers, so nothing moved."""
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()                                   # full sweep, 49 arms
    first_changed = _reading(wired)["last_changed_at"]
    pull.main()                                   # canonical arm, 14 arms, same day
    r = _reading(wired)
    assert r["arm"] == "canonical"
    assert r["last_changed_at"] == first_changed, "the cadence was reported as a change"
    assert len(_history(wired)) == 1


# ── abstention ─────────────────────────────────────────────────────────────────────────────

def test_an_unreachable_model_is_skipped_not_published_as_refusing(wired, monkeypatch):
    def half_dead(key, model, prompt):
        return None if model == "testlab/model-b" else ANSWER
    monkeypatch.setattr(pull, "_query", half_dead)
    pull.main()
    assert [m["model"] for m in _reading(wired)["models"]] == ["testlab/model-a"]


def test_a_dead_panel_publishes_nothing(wired, monkeypatch):
    monkeypatch.setattr(pull, "_query", lambda k, m, p: None)
    pull.main()
    assert not (wired / "refusal-drift-latest.json").exists()


def test_no_key_abstains_cleanly(wired, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    assert not (wired / "refusal-drift-latest.json").exists()


# ── the verifier: what a skeptic can actually check ─────────────────────────────────────────

def _verifier(wired, monkeypatch):
    """Point the verification tool at this test's temp readings."""
    import scripts.verify_refusal_transcripts as vt
    monkeypatch.setattr(vt, "READINGS", str(wired))
    monkeypatch.setattr(vt, "REGISTRY", str(wired / "eval-registry.jsonl"))
    monkeypatch.setattr(vt, "TRANSCRIPTS", str(wired / "refusal-drift-transcripts.json"))
    monkeypatch.setattr(vt, "READING", str(wired / "refusal-drift-latest.json"))
    return vt


def test_an_untampered_run_verifies_end_to_end(wired, monkeypatch, capsys):
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    assert _verifier(wired, monkeypatch).main() == 0
    out = capsys.readouterr().out
    assert "chain integrity ....... INTACT" in out
    assert "pre-registration ..... MATCHES" in out
    assert "transcript commitment . MATCHES" in out
    assert "label recomputation ... ALL" in out


@pytest.mark.parametrize("attack,marker", [
    ("delete_model", "MISSING"),
    ("drop_arm", "MISMATCH"),
    ("edit_response", "MISMATCH"),
    ("reword_prompt", "MISMATCH"),
    ("add_unsealed_model", "EXTRA"),
])
def test_the_verifier_catches_tampering(wired, monkeypatch, capsys, attack, marker):
    """The point of publishing transcripts is that they can be CHECKED, so each way of
    faking them has to fail. `delete_model` is here because the first version of this
    tool walked the published file and asked the chain about each entry, which can only
    ever confirm what is present: dropping an inconvenient model's responses wholesale
    passed silently. The loop is driven by the seals for exactly that reason."""
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    path = wired / "refusal-drift-transcripts.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    first_model = sorted(d["responses"])[0]
    arms = sorted(d["responses"][first_model])
    if attack == "delete_model":
        d["responses"].pop(sorted(d["responses"])[-1])
    elif attack == "drop_arm":
        d["responses"][first_model].pop(arms[0])
    elif attack == "edit_response":
        d["responses"][first_model][arms[0]] = "something the model never said"
    elif attack == "reword_prompt":
        d["prompts"][sorted(d["prompts"])[0]] = "a different question entirely"
    elif attack == "add_unsealed_model":
        d["responses"]["testlab/never-ran"] = {arms[0]: "invented"}
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    assert _verifier(wired, monkeypatch).main() == 1, f"{attack} was not caught"
    assert marker in capsys.readouterr().out


def test_a_broken_chain_is_never_laundered_into_a_soft_exit(wired, monkeypatch):
    """Exit 2 means "nothing to check yet". It must not be reachable once step 1 has
    already found a break, or an integrity failure exits as though it were an absence."""
    monkeypatch.setattr(pull, "_query", _answer_everything)
    pull.main()
    registry = wired / "eval-registry.jsonl"
    rows = registry.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(rows[-1])
    tampered["metrics"] = {"family_refusal_rate_pct": 0.0}      # edit after sealing
    rows[-1] = json.dumps(tampered, ensure_ascii=False)
    registry.write_text("\n".join(rows) + "\n", encoding="utf-8")
    # Point the transcripts at a commitment with no sealed run, so the "nothing further
    # to check" path is taken WHILE a chain break is outstanding.
    tr = wired / "refusal-drift-transcripts.json"
    d = json.loads(tr.read_text(encoding="utf-8"))
    d["probe_commitment"] = "0" * 64
    tr.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    reading = wired / "refusal-drift-latest.json"
    r = json.loads(reading.read_text(encoding="utf-8"))
    r["probe_commitment"] = "0" * 64
    reading.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    assert _verifier(wired, monkeypatch).main() == 1
