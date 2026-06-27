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


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
