"""Tests for processors.gazetteer_evolution — candidate discovery from deletions.

    PYTHONPATH=. python3 -m pytest tests/test_gazetteer_evolution.py -q

Pure/offline: the mining core is deterministic and takes its evidence as plain dicts.
"""

from processors.gazetteer_evolution import (
    Candidate,
    build_proposal_ledger,
    candidate_tokens,
    mine_candidates,
    propose_glosses,
)


def test_candidate_tokens_ngrams_recover_short_term():
    # 散步 ("taking a walk" = protest euphemism) buried in a longer CJK run.
    toks = candidate_tokens("今晚去散步声援")
    assert "散步" in toks            # recovered via 2-gram slicing
    assert "声援" in toks


def test_candidate_tokens_catches_numeric_and_roman_coinages():
    toks = candidate_tokens("纪念8964 VIIV forever")
    assert "8964" in toks
    assert "viiv" in toks            # alnum tokens are lowercased


def test_candidate_tokens_drops_generic():
    toks = candidate_tokens("中国 政府 网络")  # all in the generic stoplist
    assert toks == set()


def test_mine_surfaces_euphemism_with_association():
    known = {"白纸", "8964", "躺平"}
    obs = [
        {"title": "", "text": "今晚去散步声援白纸的朋友", "url": "u1"},   # sensitive (白纸)
        {"title": "", "text": "广场散步现场很多人白纸", "url": "u2"},     # sensitive (白纸)
        {"title": "", "text": "大家一起散步纪念8964", "url": "u3"},      # sensitive (8964)
        {"title": "", "text": "周末散步看花拍照", "url": "u4"},          # benign
    ]
    cands = mine_candidates(obs, known, min_evidence=2, promote_score=0.3)
    by_term = {c.term: c for c in cands}
    assert "散步" in by_term
    sanbu = by_term["散步"]
    assert sanbu.total_support == 4         # appears in all four
    assert sanbu.sens_support == 3          # three carried a known sensitive term
    assert sanbu.association == 0.75
    assert sanbu.state == "propose"         # clears evidence floor + score threshold


def test_known_terms_are_not_proposed():
    known = {"白纸"}
    obs = [{"title": "", "text": "白纸白纸白纸", "url": "u1"}] * 5
    cands = mine_candidates(obs, known, min_evidence=1, promote_score=0.1)
    assert all(c.term != "白纸" for c in cands)  # already known → never a candidate


def test_one_off_cooccurrence_stays_on_watch():
    known = {"白纸"}
    obs = [
        {"title": "", "text": "随便说说天气 白纸", "url": "u1"},  # 天气 co-occurs once
    ]
    cands = mine_candidates(obs, known, min_evidence=3, promote_score=0.55)
    assert all(c.state == "watch" for c in cands)  # nothing has enough evidence


def test_proposal_ledger_is_advisory_only():
    known = {"白纸", "8964"}
    obs = [
        {"title": "t1", "text": "今晚去散步声援白纸", "url": "u1"},
        {"title": "t2", "text": "广场散步白纸", "url": "u2"},
        {"title": "t3", "text": "散步纪念8964", "url": "u3"},
    ]
    ledger = build_proposal_ledger(mine_candidates(obs, known, min_evidence=2, promote_score=0.3))
    assert "advisory-only" in ledger["policy"]
    assert ledger["n_proposals"] >= 1
    assert any(p["term"] == "散步" for p in ledger["proposals"])


def test_propose_glosses_is_offline_by_default():
    cands = [Candidate(term="散步", state="propose", sens_support=3, total_support=4)]
    out = propose_glosses(cands)              # no llm_fn → deterministic, empty gloss
    assert out[0]["draft_gloss"] == ""
    assert out[0]["ratified"] is False


def test_phenomenon_taxonomy_and_slang_recall():
    """CSM-MTBench (Zhao et al. 2026): evasion-phenomenon split + validation seam."""
    from processors.gazetteer_evolution import classify_phenomenon, slang_recall
    assert classify_phenomenon("8964", "june4_tiananmen") == "numeronym"
    assert classify_phenomenon("维尼", "leadership_xi") == "homophone"
    assert classify_phenomenon("润", "emigration_run") == "lexical"
    assert classify_phenomenon("🙄") == "affective"
    r = slang_recall(["润", "白纸"], ["润", "白纸", "躺平"])
    assert r["recall"] == 0.667 and r["missed"] == ["躺平"]


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))


# ── word-formation + PMI + per-stage recall (arXiv:2606.08715) ─────────────────────────

def test_well_formed_rules_name_the_rule():
    from processors.gazetteer_evolution import well_formed
    assert well_formed("白纸") == (True, "")
    assert well_formed("的自由")[1] == "R1_initial_function"
    assert well_formed("抗议的")[1] == "R2_final_function"
    assert well_formed("不可以")[1] == "R3_initial_neg_adv_prep"
    assert well_formed("这时候")[1] == "R4_initial_determiner"
    assert well_formed("8964") == (True, "")   # non-CJK passes untouched


def test_fragment_shaped_candidate_demoted_never_deleted():
    from processors.gazetteer_evolution import mine_candidates
    obs = [{"title": f"已删除 {i}", "text": "白纸 的自由 六四", "url": "u",
            "detected_at": None} for i in range(5)]
    cands = {c.term: c for c in mine_candidates(obs, {"六四"})}
    assert cands["白纸"].state == "propose"
    frag = cands["的自由"]
    assert frag.state == "watch" and frag.formation_rule == "R1_initial_function"
    assert frag.sens_support == 5               # the evidence stays visible


def test_pmi_annotates_cohesion():
    from processors.gazetteer_evolution import mine_candidates
    # 白纸 always together (cohesive); 纸共 only across a random boundary
    obs = [{"title": "", "text": "白纸运动 共产 白纸 共识 白纸", "url": "u",
            "detected_at": None} for _ in range(4)]
    cands = {c.term: c for c in mine_candidates(obs, {"共产"})}
    assert cands["白纸"].pmi is not None and cands["白纸"].pmi > 0


def test_stage_recall_multiplicative_identity():
    from processors.gazetteer_evolution import stage_recall
    obs = [{"title": f"删帖 {i}", "text": "白纸 六四 的自由", "url": "u",
            "detected_at": None} for i in range(5)]
    r = stage_recall({"白纸", "的自由", "从未出现"}, obs, {"六四"})
    assert r["n_truth"] == 3
    prod = 1.0
    for s in r["stages"]:
        assert s["recall"] is not None
        prod *= s["recall"]
    # identity holds on raw counts; published values carry 4dp display rounding
    assert abs(prod - r["strict_recall"]) < 5e-4      # R1·R2·R3·R4 = strict
    # 从未出现 dies at stage 1, 的自由 at stage 2, 白纸 survives to proposal
    assert r["stages"][0]["surviving"] == 2
    assert r["stages"][1]["surviving"] == 1
    assert r["strict_recall"] == round(1 / 3, 4)
