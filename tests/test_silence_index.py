"""Tests for processors.silence_index — pre-emptive-censorship / blackout scoring.

    PYTHONPATH=. python3 -m pytest tests/test_silence_index.py -q

Pure/offline: the scoring core, the baseline-aware decoupling guard (the false-positive
killer), the abstain bands, the DDTI-schema emit, and the governance-gated processor (with an
injected GDELT enrich + injected domestic-volume table) are all exercised with no network.
"""

from datetime import datetime, timezone

from processors.silence_index import (
    ABSTAIN,
    BLACKOUT,
    CONTAINMENT,
    COUPLED,
    OUT_OF_SCOPE,
    SilenceIndexProcessor,
    china_nexus_and_lexicon,
    emit_observations,
    rank_silence,
    silence_score,
    silence_to_observation,
)
from core.governance import KillSwitch, RateCeiling

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


# ── Case 1: true blackout (Peng Shuai pattern) ─────────────────────────────────────────

def test_true_blackout_peng_shuai():
    r = silence_score("彭帅", global_norm=0.9, domestic_norm=0.02, china_nexus=True,
                      coupling_baseline=0.8, lexicon_hit=True)
    assert r["label"] == BLACKOUT
    assert r["abstained"] is False
    # baseline-aware decoupling: 0.8*0.9 - 0.02 = 0.70; *1.5 (lexicon) -> clamp 1.0
    assert r["silence_score"] is not None and r["silence_score"] > 0.9
    obs = silence_to_observation(r, NOW)
    assert obs["deletion_signal"] == BLACKOUT
    assert obs["terms"] == ["彭帅"]
    assert obs["severity"] == "high"
    assert obs["title"] == "[silence:blackout] 彭帅"


# ── Case 2: the negative control — normal local-interest variation MUST NOT flag ───────

def test_negative_control_no_china_nexus_is_out_of_scope():
    r = silence_score("premier league transfer", global_norm=0.9, domestic_norm=0.05,
                      china_nexus=False, coupling_baseline=None, lexicon_hit=False)
    assert r["label"] == OUT_OF_SCOPE
    assert r["silence_score"] == 0.0
    assert r["abstained"] is False


def test_negative_control_china_never_covers_scores_zero():
    """A China-nexus topic China simply never covers heavily: low coupling baseline => a low
    domestic volume is EXPECTED, so baseline-aware decoupling drives the score to ~0 (coupled),
    NOT a false blackout. This is the core false-positive guard."""
    r = silence_score("some niche china topic", global_norm=0.9, domestic_norm=0.04,
                      china_nexus=True, coupling_baseline=0.05, lexicon_hit=False)
    # expected_domestic = 0.05*0.9 = 0.045; decoupling = max(0, 0.045-0.04) = 0.005 -> ~0
    assert r["silence_score"] < 0.05
    assert r["label"] == COUPLED


# ── Bug B regression: no baseline AND no lexicon corroboration MUST abstain ─────────────

def test_no_baseline_no_lexicon_abstains_not_false_blackout():
    """The reviewer's exact repro: a bare china-nexus topic (nexus marker, no gazetteer hit)
    that is loud abroad and quiet on the injected domestic proxy, with NO coupling baseline,
    must ABSTAIN — not be fabricated as a blackout. Guard rail #2 cannot be bypassed."""
    r = silence_score("china trade deal", global_norm=0.9, domestic_norm=0.04, china_nexus=True,
                      coupling_baseline=None, lexicon_hit=False)
    assert r["label"] == ABSTAIN
    assert r["silence_score"] is None
    assert r["abstained"] is True
    assert emit_observations([r], NOW) == []               # shown suppressed, never emitted


def test_no_baseline_but_lexicon_hit_still_scores():
    """With no baseline but a transparent gazetteer/lexicon corroboration, scoring is allowed
    (naive decoupling backed by an auditable hit) — the guard requires EITHER baseline OR lexicon."""
    r = silence_score("peng shuai", global_norm=0.9, domestic_norm=0.02, china_nexus=True,
                      coupling_baseline=None, lexicon_hit=True)
    assert r["label"] in (BLACKOUT, CONTAINMENT)
    assert r["abstained"] is False
    assert r["silence_score"] is not None and r["silence_score"] > 0


def test_processor_missing_coupling_baseline_fn_abstains_bare_nexus():
    """End-to-end: a deployer wires domestic_volume_fn but FORGETS coupling_baseline_fn. A bare
    china-nexus topic (no gazetteer hit) must abstain, not emit a false blackout."""
    proc = SilenceIndexProcessor(
        domestic_volume_fn=lambda t: 0.03,
        enrich_fn=_fake_enrich({"中国 trade deal": 0.9}),
        # coupling_baseline_fn intentionally omitted
    )
    readings = proc.build_readings([{"term": "中国 trade deal", "attention": 1.0, "recent_count": 1}])
    assert readings[0]["label"] == ABSTAIN and readings[0]["abstained"] is True
    assert emit_observations(readings, NOW) == []


# ── Case 3: containment (loud abroad + present but decoupled) ───────────────────────────

def test_containment_present_but_decoupled():
    r = silence_score("乌鲁木齐", global_norm=0.9, domestic_norm=0.30, china_nexus=True,
                      coupling_baseline=0.8, lexicon_hit=True)
    # present (0.30 > PRESENCE_EPS) but well below expected 0.72 -> containment
    assert r["label"] == CONTAINMENT
    assert r["silence_score"] > 0
    obs = silence_to_observation(r, NOW)
    assert obs["deletion_signal"] == CONTAINMENT


# ── Case 4: withholding boundary (youth unemployment, Aug 2023) ─────────────────────────

def test_withholding_is_labelled_but_noted_not_a_deletion():
    r = silence_score("青年失业率", global_norm=0.8, domestic_norm=0.05, china_nexus=True,
                      coupling_baseline=0.6, lexicon_hit=True)
    assert r["label"] in (BLACKOUT, CONTAINMENT)  # it IS flagged
    obs = silence_to_observation(r, NOW)
    # ...but carried as a WITHHOLDING (data hole), never a plain post deletion
    assert "note" in obs and "withholding" in obs["note"].lower()
    assert obs["deletion_signal"] != "deletion"


# ── Case 5: abstain — domestic input unreachable (fail loud, not a false zero) ──────────

def test_abstain_when_domestic_unreachable():
    r = silence_score("彭帅", global_norm=0.9, domestic_norm=None, china_nexus=True,
                      coupling_baseline=0.8, lexicon_hit=True)
    assert r["label"] == ABSTAIN
    assert r["silence_score"] is None
    assert r["abstained"] is True
    # shown suppressed: NOT emitted as an observation
    assert emit_observations([r], NOW) == []


# ── Case 6: abstain — not loud anywhere (below floor) ──────────────────────────────────

def test_abstain_when_not_loud_anywhere():
    r = silence_score("彭帅", global_norm=0.03, domestic_norm=0.0, china_nexus=True,
                      coupling_baseline=0.8, lexicon_hit=True)
    assert r["label"] == ABSTAIN
    assert r["silence_score"] is None
    assert r["abstained"] is True


# ── Case 7: GDELT fail-soft passthrough (global_norm None -> abstain, sorts last) ──────

def test_gdelt_unknown_abstains_and_sorts_last():
    loud = silence_score("彭帅", global_norm=0.9, domestic_norm=0.02, china_nexus=True,
                         coupling_baseline=0.8, lexicon_hit=True)
    unknown = silence_score("李文亮", global_norm=None, domestic_norm=0.5, china_nexus=True,
                            coupling_baseline=0.8, lexicon_hit=True)
    assert unknown["label"] == ABSTAIN and unknown["abstained"] is True
    ranked = rank_silence([unknown, loud])
    assert ranked[0]["topic"] == "彭帅"          # real signal first
    assert ranked[-1]["abstained"] is True        # abstention flagged and last


# ── china-nexus + lexicon gazetteer matching (transparent, no model) ───────────────────

def test_china_nexus_and_lexicon_from_gazetteer():
    cn, lex = china_nexus_and_lexicon("彭帅")
    assert cn is True and lex is True             # sensitivity-gazetteer hit
    cn2, lex2 = china_nexus_and_lexicon("中国 economy")
    assert cn2 is True and lex2 is False          # bare nexus marker, no corroboration
    cn3, lex3 = china_nexus_and_lexicon("premier league transfer")
    assert cn3 is False and lex3 is False         # out of scope


# ── ranking: blackout above coupled above abstain ──────────────────────────────────────

def test_rank_orders_signal_then_coupled_then_abstain():
    black = silence_score("彭帅", global_norm=0.9, domestic_norm=0.02, china_nexus=True,
                          coupling_baseline=0.8, lexicon_hit=True)
    coupled = silence_score("中国 weather", global_norm=0.9, domestic_norm=0.85,
                            china_nexus=True, coupling_baseline=0.95, lexicon_hit=False)
    abst = silence_score("李文亮", global_norm=None, domestic_norm=0.5, china_nexus=True)
    ranked = rank_silence([abst, coupled, black])
    assert ranked[0]["label"] == BLACKOUT
    assert ranked[-1]["abstained"] is True


# ── the processor: injected GDELT enrich + injected domestic table, offline ────────────

def _fake_enrich(global_table):
    def enrich(term_dicts):
        return [{"term": t["term"], "global_norm": global_table.get(t["term"]),
                 "abstained": global_table.get(t["term"]) is None} for t in term_dicts]
    return enrich


def test_processor_build_readings_offline():
    global_table = {"彭帅": 0.92, "premier league transfer": 0.88, "李文亮": None}
    domestic = {"彭帅": 0.02, "premier league transfer": 0.05, "李文亮": 0.4}
    baselines = {"彭帅": 0.8, "premier league transfer": 0.9, "李文亮": 0.8}
    proc = SilenceIndexProcessor(
        domestic_volume_fn=lambda t: domestic.get(t),
        enrich_fn=_fake_enrich(global_table),
        coupling_baseline_fn=lambda t: baselines.get(t),
    )
    terms = [{"term": "彭帅", "attention": 1.0, "recent_count": 1},
             {"term": "premier league transfer", "attention": 1.0, "recent_count": 1},
             {"term": "李文亮", "attention": 1.0, "recent_count": 1}]
    readings = proc.build_readings(terms)
    by_topic = {r["topic"]: r for r in readings}
    assert by_topic["彭帅"]["label"] == BLACKOUT                       # gazetteer china-nexus + decoupled
    assert by_topic["premier league transfer"]["label"] == OUT_OF_SCOPE  # not in scope
    assert by_topic["李文亮"]["label"] == ABSTAIN                       # GDELT unknown -> abstain
    obs = emit_observations(readings, NOW)
    assert any(o["title"] == "[silence:blackout] 彭帅" for o in obs)
    assert all("league" not in o["title"] for o in obs)               # out-of-scope never emitted


def test_processor_inert_default_domestic_abstains_everything():
    """No domestic_volume_fn => every topic abstains (the correct fail-loud inert default)."""
    proc = SilenceIndexProcessor(enrich_fn=_fake_enrich({"彭帅": 0.92}))
    readings = proc.build_readings([{"term": "彭帅", "attention": 1.0, "recent_count": 1}])
    assert readings[0]["label"] == ABSTAIN and readings[0]["abstained"] is True
    assert emit_observations(readings, NOW) == []


def test_processor_governance_killswitch_halts_build(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_SILENCE_UNSET")
    ks.engage("test")
    proc = SilenceIndexProcessor(
        domestic_volume_fn=lambda t: 0.5,
        enrich_fn=lambda td: (_ for _ in ()).throw(AssertionError("enrich must not run")),
        kill_switch=ks,
    )
    try:
        proc.build_readings([{"term": "彭帅", "attention": 1.0, "recent_count": 1}])
        assert False, "halted processor must refuse to fetch"
    except RuntimeError:
        pass


def test_processor_rate_ceiling_consulted():
    rc = RateCeiling(rate=1000, capacity=10, clock=lambda: 0.0)
    proc = SilenceIndexProcessor(
        domestic_volume_fn=lambda t: 0.02,
        enrich_fn=_fake_enrich({"彭帅": 0.92}),
        coupling_baseline_fn=lambda t: 0.8,
        rate_ceiling=rc,
    )
    readings = proc.build_readings([{"term": "彭帅", "attention": 1.0, "recent_count": 1}])
    assert readings[0]["label"] == BLACKOUT


# ── Case 8: retrodiction — blackout events rank above non-events ────────────────────────

def test_retrodiction_blackout_events_rank_above_non_events():
    """Reuse documented blackout events (彭帅 / 白纸 / 李文亮 window) with a fixture domestic-volume
    table; the scorer must rank them above an in-scope-but-coupled non-event. Pattern after
    tests/test_validation.py (scoring method, not live collection)."""
    global_table = {"彭帅": 0.92, "白纸": 0.85, "李文亮": 0.80, "中国 GDP": 0.70}
    domestic = {"彭帅": 0.02, "白纸": 0.03, "李文亮": 0.05, "中国 GDP": 0.62}   # GDP tracks abroad
    baselines = {"彭帅": 0.8, "白纸": 0.8, "李文亮": 0.8, "中国 GDP": 0.9}
    proc = SilenceIndexProcessor(
        domestic_volume_fn=lambda t: domestic.get(t),
        enrich_fn=_fake_enrich(global_table),
        coupling_baseline_fn=lambda t: baselines.get(t),
    )
    terms = [{"term": k, "attention": 1.0, "recent_count": 1} for k in global_table]
    ranked = rank_silence(proc.build_readings(terms))
    top3 = {r["topic"] for r in ranked[:3]}
    assert {"彭帅", "白纸", "李文亮"} == top3            # blackout events on top
    gdp = next(r for r in ranked if r["topic"] == "中国 GDP")
    assert gdp["label"] == COUPLED                       # the coupled non-event is not silence


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
