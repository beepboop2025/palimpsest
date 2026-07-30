"""Inside View — classification and control-gate tests.

All of these run offline: `create` and `collect` are injected, so the whole
forged/clean decision path and the control gate are exercised with no network
and no Globalping credits.
"""
from __future__ import annotations

import pytest

from collectors.inside_view import (RateLimited, control_state, observe_domain,
                                    observe_panel, regional_divergence)

TRUTH = ["95.216.163.36", "116.202.120.166"]


def _stub(control_answers, cn_probe_answers):
    """Build (create, collect) stubs. `cn_probe_answers` is a list of answer
    lists, one per simulated in-China probe; [] means the probe stayed silent."""
    def create(domain, locations, limit):
        return "ctl" if locations and locations[0].get("country") != "CN" else "cn"

    def collect(mid):
        def probe(city, asn, answers):
            return {"probe": {"city": city, "asn": asn, "network": "n"},
                    "result": {"answers": [{"type": "A", "value": a} for a in answers]}}
        if mid == "ctl":
            return [probe("Frankfurt", 24940, control_answers)]
        return [probe(f"city{i}", 45090 + i, a)
                for i, a in enumerate(cn_probe_answers)]
    return create, collect


def _observe(entry, control_answers, cn_probe_answers):
    create, collect = _stub(control_answers, cn_probe_answers)
    return observe_domain(entry, create=create, collect=collect)


CENSORED = {"domain": "torproject.org", "censored": True, "ddti": "CIRCUMVENTION"}
BENIGN = {"domain": "example.com", "censored": False, "ddti": None}


def test_answer_disjoint_from_control_is_forged():
    o = _observe(CENSORED, TRUTH, [["4.36.66.178"], ["64.33.88.161"]])
    assert o["n_forged"] == 2
    assert o["n_clean"] == 0
    assert o["forged_fraction"] == 1.0


def test_answer_overlapping_control_is_clean():
    o = _observe(BENIGN, TRUTH, [TRUTH, ["95.216.163.36"]])
    assert o["n_clean"] == 2
    assert o["n_forged"] == 0


def test_partial_overlap_counts_as_clean():
    """One shared address is enough: CDNs legitimately return rotating subsets,
    and treating that as forgery would fabricate censorship."""
    o = _observe(BENIGN, TRUTH, [["116.202.120.166", "203.0.113.9"]])
    assert o["n_clean"] == 1
    assert o["n_forged"] == 0


def test_silent_probe_is_neither_forged_nor_clean():
    o = _observe(CENSORED, TRUTH, [["4.36.66.178"], []])
    assert o["n_silent"] == 1
    assert o["n_forged"] == 1
    assert o["forged_fraction"] == 1.0     # silence excluded from the denominator


def test_no_control_truth_leaves_answers_unclassified():
    """Without a control arm we cannot know what is forged. The collector must
    refuse to guess rather than default to 'clean'."""
    o = _observe(CENSORED, [], [["4.36.66.178"]])
    assert o["n_forged"] == 0
    assert o["n_clean"] == 0
    assert o["vantages"][0]["state"] == "unclassified"


# ── control gate ──────────────────────────────────────────────────────────────

def test_gate_sighted_when_censored_forged_and_benign_clean():
    obs = [_observe(CENSORED, TRUTH, [["4.36.66.178"]]),
           _observe(BENIGN, TRUTH, [TRUTH])]
    assert control_state(obs)["state"] == "SIGHTED"


def test_gate_blind_when_nothing_forged_but_controls_behaved():
    """The load-bearing case: everything reads clean. That is NOT an all-clear,
    because a broken vantage looks identical to a quiet censor."""
    obs = [_observe(CENSORED, TRUTH, [TRUTH]),
           _observe(BENIGN, TRUTH, [TRUTH])]
    assert control_state(obs)["state"] == "BLIND"


def test_gate_degraded_when_a_benign_control_reads_forged():
    """If example.com looks forged, the classifier is wrong and nothing derived
    from this round can be trusted."""
    obs = [_observe(CENSORED, TRUTH, [["4.36.66.178"]]),
           _observe(BENIGN, TRUTH, [["203.0.113.9"]])]
    gate = control_state(obs)
    assert gate["state"] == "DEGRADED"
    assert "example.com" in gate["why"]


def test_gate_degraded_when_nothing_answered():
    obs = [_observe(CENSORED, TRUTH, [[]]), _observe(BENIGN, TRUTH, [[]])]
    assert control_state(obs)["state"] == "DEGRADED"


def test_rate_limit_propagates_instead_of_looking_like_silence():
    """A 429 must not be swallowed into empty results. 'we did not ask' and
    'we asked and every probe was silent' are different findings, and only one
    of them is a measurement."""
    def create(domain, locations, limit):
        raise RateLimited("429 on " + domain)

    with pytest.raises(RateLimited):
        observe_panel([CENSORED], create=create, collect=lambda mid: [])


def test_geo_split_domain_would_read_as_forged():
    """Documents why a China-hosted CDN domain cannot serve as a negative
    control: legitimate regional resolution shares no address with the control
    arm and is indistinguishable from injection. This is the bug that tripped
    the gate on the first live round."""
    o = _observe(BENIGN, ["203.0.113.1"], [["198.51.100.7"]])
    assert o["n_forged"] == 1
    assert control_state([o])["state"] == "DEGRADED"


# ── pending layer ─────────────────────────────────────────────────────────────

def test_split_vantages_report_regional_not_a_majority_verdict():
    """The whole reason to measure from inside: a domain blocked in one province
    and clean in another must surface as REGIONAL, not be voted away."""
    o = _observe(CENSORED, TRUTH, [["4.36.66.178"], TRUTH, TRUTH])
    v = regional_divergence(o)
    assert v["verdict"] == "REGIONAL"
    assert "1/3" in v["detail"]


def test_all_forged_is_uniform_blocked():
    o = _observe(CENSORED, TRUTH, [["4.36.66.178"], ["64.33.88.161"]])
    assert regional_divergence(o)["verdict"] == "UNIFORM_BLOCKED"


def test_rotating_forged_pool_is_not_mistaken_for_disagreement():
    """Every vantage blocked, every vantage handed a DIFFERENT forged address.
    Comparing answers would call this divergence; comparing state does not."""
    o = _observe(CENSORED, TRUTH,
                 [["4.36.66.178"], ["199.59.150.44"], ["64.33.88.161"], ["203.161.230.171"]])
    assert regional_divergence(o)["verdict"] == "UNIFORM_BLOCKED"


def test_all_clean_is_uniform_clean():
    o = _observe(BENIGN, TRUTH, [TRUTH, TRUTH])
    assert regional_divergence(o)["verdict"] == "UNIFORM_CLEAN"


def test_single_answering_vantage_is_insufficient_for_a_regional_claim():
    """One probe cannot speak to regional variation, and a lone forged answer
    must not become a blocking verdict on its own."""
    o = _observe(CENSORED, TRUTH, [["4.36.66.178"], []])
    assert regional_divergence(o)["verdict"] == "INSUFFICIENT"


def test_silent_vantages_do_not_dilute_the_split():
    o = _observe(CENSORED, TRUTH, [["4.36.66.178"], TRUTH, [], []])
    v = regional_divergence(o)
    assert v["verdict"] == "REGIONAL"
    assert "1/2" in v["detail"]
