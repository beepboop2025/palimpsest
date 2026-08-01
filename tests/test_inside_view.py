"""Inside View — classification and control-gate tests.

All of these run offline: `create` and `collect` are injected, so the whole
forged/clean decision path and the control gate are exercised with no network
and no Globalping credits.
"""
from __future__ import annotations

import pytest

from collectors.inside_view import (CLOUD_ASNS, CONTROL_COUNTRIES, RateLimited,
                                    control_state, observe_domain,
                                    observe_panel, regional_divergence)

TRUTH = ["95.216.163.36", "116.202.120.166"]


def _stub(control_answers, cn_probe_answers):
    """Build (create, collect) stubs. `cn_probe_answers` is a list of answer
    lists, one per simulated in-China probe; [] means the probe stayed silent."""
    def create(domain, locations, limit):
        # The in-China arm is identified by its magic ASN filter, not by a
        # country key — CN requests carry {"magic": "CN+<asn>"} and no "country".
        return "cn" if any("magic" in l for l in locations) else "ctl"

    def collect(mid):
        def probe(city, asn, answers):
            return {"probe": {"city": city, "asn": asn, "network": "n"},
                    "result": {"answers": [{"type": "A", "value": a} for a in answers]}}
        if mid == "ctl":
            return [probe("Frankfurt", 24940, control_answers)]
        return [probe(f"city{i}", 45090 + i, a)
                for i, a in enumerate(cn_probe_answers)]
    return create, collect


def _fake_owner(ips):
    """Offline stand-in for the Cymru lookup: the first octet is the owner, so
    1.1.1.1 and 1.0.0.1 are the same network and 9.9.9.9 is a different one."""
    out = {}
    for ip in ips:
        octet = str(ip).split(".")[0]
        if not octet.isdigit():
            continue
        out[str(ip)] = {"asn": int(octet) * 1000,
                        "name": f"NET{octet} - Test Network {octet}, XX",
                        "prefix": f"{octet}.0.0.0/8"}
    return out


def _observe(entry, control_answers, cn_probe_answers):
    create, collect = _stub(control_answers, cn_probe_answers)
    return observe_domain(entry, create=create, collect=collect,
                          resolve=_fake_owner)


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


# ── attribution + consent guards ──────────────────────────────────────────────

def _stub_asn(control_answers, cn_probes):
    """cn_probes is a list of (asn, answers) so ASN diversity can be controlled."""
    def create(domain, locations, limit):
        return "cn" if any("magic" in l for l in locations) else "ctl"

    def collect(mid):
        if mid == "ctl":
            return [{"probe": {"city": "Frankfurt", "asn": 24940, "network": "n"},
                     "result": {"answers": [{"type": "A", "value": a}
                                            for a in control_answers]}}]
        return [{"probe": {"city": f"c{i}", "asn": asn, "network": "n"},
                 "result": {"answers": [{"type": "A", "value": a} for a in ans]}}
                for i, (asn, ans) in enumerate(cn_probes)]
    return create, collect


def test_forgery_inside_one_asn_is_not_published_as_blocked():
    """Tencent and Alibaba each run resolver interception. Forgery seen only
    within one operator cannot be distinguished from that operator's own
    meddling, so it must not be reported as national filtering."""
    create, collect = _stub_asn(TRUTH, [(45090, ["4.36.66.178"]),
                                        (45090, ["64.33.88.161"]),
                                        (45090, ["203.161.230.171"])])
    o = observe_domain(CENSORED, create=create, collect=collect)
    v = regional_divergence(o)
    assert v["verdict"] == "SINGLE_OPERATOR"
    assert "45090" in v["detail"]


def test_forgery_across_two_asns_is_uniform_blocked():
    create, collect = _stub_asn(TRUTH, [(45090, ["4.36.66.178"]),
                                        (37963, ["64.33.88.161"])])
    o = observe_domain(CENSORED, create=create, collect=collect)
    assert regional_divergence(o)["verdict"] == "UNIFORM_BLOCKED"


def test_a_regional_split_still_reports_regional_within_one_asn():
    """The single-operator guard applies to unanimous forgery. A split is
    informative regardless of ASN width and must not be suppressed."""
    create, collect = _stub_asn(TRUTH, [(45090, ["4.36.66.178"]), (45090, TRUTH)])
    o = observe_domain(CENSORED, create=create, collect=collect)
    assert regional_divergence(o)["verdict"] == "REGIONAL"


def test_censored_queries_are_pinned_to_datacenter_asns():
    """The consent guard: a volunteer hosting a probe did not agree to have
    their household connection query wikileaks.org from inside China. Every CN
    request must carry a cloud-ASN magic filter, never a bare country filter."""
    asked = []

    def create(domain, locations, limit):
        asked.append(locations)
        return None

    observe_domain(CENSORED, create=create, collect=lambda mid: [])
    cn_calls = [loc for loc in asked if any("magic" in l for l in loc)]
    assert cn_calls, "the in-China call must use magic ASN filters"
    for loc in cn_calls:
        for entry in loc:
            assert entry["magic"].startswith("CN+")
            assert int(entry["magic"].split("+")[1]) in CLOUD_ASNS
    # and no call may target CN by country alone
    assert not any(l.get("country") == "CN" for loc in asked for l in loc)
