"""Tests for the censorship forecaster (processors/forecaster.py)."""
from datetime import datetime, timezone

from processors.forecaster import (called_shot, derive_mechanical_variants,
                                    forecast_escalation, forecast_mutations)

DDTI = {"ranked": [
    {"term": "四通桥", "domain": "POLITICS", "threat": 9.0, "attention": 3.0, "novelty": 1.0, "is_new": True, "burst_ratio": None},
    {"term": "白纸", "domain": "POLITICS", "threat": 4.0, "attention": 2.0, "novelty": 0.8, "is_new": False, "burst_ratio": 5.0},
    {"term": "六四", "domain": "POLITICS", "threat": 1.7, "attention": 1.6, "novelty": 0.02, "is_new": False, "burst_ratio": 1.0},
]}


def test_escalation_ranks_new_and_bursting_above_chronic():
    e = forecast_escalation(DDTI)
    terms = [x["term"] for x in e]
    assert terms[0] == "四通桥"            # newly-sensitive => top
    assert terms.index("白纸") < terms.index("六四")  # bursting > chronic
    assert all(x["escalation"] > 0 for x in e)


def test_escalation_is_deterministic():
    assert forecast_escalation(DDTI) == forecast_escalation(DDTI)


def test_mechanical_variants():
    v = derive_mechanical_variants("六四")
    assert v["insertion"] == "六·四"
    v2 = derive_mechanical_variants("8964")
    assert "spacing" in v2 and "reversed" in v2 and v2["reversed"] == "4698"


def test_mutation_forecast_uses_phylogeny():
    # 六四 has children in the gazetteer (VIIV, 平反) => observed mechanisms + a prediction
    preds = forecast_mutations("cn", ["六四"])
    assert preds, "expected a mutation prediction for a pressured lineage root"
    p = next(p for p in preds if p["root"] == "六四")
    assert p["observed_mechanisms"]          # learned from the lineage
    assert p["predicted_next"]               # untried evasion classes
    assert p["mechanical_candidates"]["insertion"] == "六·四"


def test_called_shot_is_well_formed_and_deterministic():
    now = datetime(2026, 6, 30, tzinfo=timezone.utc)
    a = called_shot(DDTI, region="cn", now=now)
    b = called_shot(DDTI, region="cn", now=now)
    assert a == b
    for k in ("generated_at", "region", "horizon_days", "watch_terms", "watch_mutations", "falsifiable_by"):
        assert k in a
    assert a["watch_terms"] and a["region"] == "cn"
