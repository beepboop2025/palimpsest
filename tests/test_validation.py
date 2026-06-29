"""Retrodiction backtest as a regression test.

Asserts the unmodified DDTI scorer catches documented censorship events
(selectivity + novelty + lead-time), and that the gazetteer covers them.
See scripts/validate_ddti.py and docs/VALIDATION.md.
"""
from processors.ddti_index import load_censorship_terms
from scripts.validate_ddti import run_backtest


def test_ddti_catches_known_events():
    rows = run_backtest()
    assert rows, "no validation events evaluated"
    for r in rows:
        # selectivity: the event's top-1 threat is one of its signature terms
        assert r["selectivity_hit"], f"{r['id']}: top-1 {r['top1']!r} is not a signature term"
        # novelty: terms born in the event are flagged is_new
        if r["born"]:
            assert r["novelty_hit"] is True, f"{r['id']}: born terms {r['born']} not flagged new"
        # lead time: the headline term surfaces from only 2 deletions
        assert r["lead_time_hit"], f"{r['id']}: no early (2-deletion) detection"
    detected = sum(r["detected"] for r in rows)
    assert detected == len(rows), f"only {detected}/{len(rows)} events detected"


def test_gazetteer_covers_validation_events():
    terms = set(load_censorship_terms())
    # representative signature terms across domains the backtest relies on
    for zh in ["李文亮", "彭帅", "铁链女", "四通桥", "白纸", "乌鲁木齐",
               "青年失业率", "法轮功", "维吾尔", "国安法", "蛤蟆"]:
        assert zh in terms, f"gazetteer is missing event term {zh!r}"
