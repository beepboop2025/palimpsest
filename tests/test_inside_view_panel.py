"""Inside View — panel roles, the geo-split guard, and the probe budget.

Three things that only matter once the panel stops being five permanently
blocked domains measured from four coastal cities:

  * a domain the control arm cannot pin down must not be called blocked,
  * a boundary domain is an experiment and must not touch the headline rate or
    the control gate,
  * the round has to fit inside Globalping's hourly credit ceiling, because a
    round that runs out of credit mid-panel abstains whole.

Offline: the probe layer is stubbed, no network, no credits spent.
"""
from __future__ import annotations

import pytest

from collectors.inside_view import (CLOUD_ASNS, CN_PROBES, CONTROL_PROBES,
                                    PANEL, control_state, observe_domain,
                                    regional_divergence)

MEASUREMENT = {"domain": "torproject.org", "censored": True,
               "role": "measurement", "ddti": "INFORMATION"}
BOUNDARY = {"domain": "archive.org", "censored": True,
            "role": "boundary", "ddti": "INFORMATION"}
CONTROL = {"domain": "one.one.one.one", "censored": False,
           "role": "control", "ddti": None}


def _stub(control_by_country, cn_probe_answers):
    """control_by_country: {"DE": [...], "NL": [...]} so the two control probes
    can be made to agree or disagree. cn_probe_answers: one list per CN probe."""
    def create(domain, locations, limit):
        return "cn" if any("magic" in l for l in locations) else "ctl"

    def collect(mid):
        def probe(city, asn, answers, country=None):
            p = {"city": city, "asn": asn, "network": "n"}
            if country:
                p["country"] = country
            return {"probe": p,
                    "result": {"answers": [{"type": "A", "value": a} for a in answers]}}
        if mid == "ctl":
            return [probe("ctl", 24940, ans, country=c)
                    for c, ans in control_by_country.items()]
        return [probe(f"city{i}", 45090 + (i % 3), a)
                for i, a in enumerate(cn_probe_answers)]
    return create, collect


def _observe(entry, control_by_country, cn_probe_answers):
    create, collect = _stub(control_by_country, cn_probe_answers)
    return observe_domain(entry, create=create, collect=collect)


# ── the geo-split guard ───────────────────────────────────────────────────────

def test_a_domain_the_control_arm_disagrees_on_is_not_called_forged():
    """The failure this guard exists for: a domain that legitimately answers
    differently by region reads as forged, because forgery is defined as sharing
    no address with the control. www.baidu.com tripped exactly this on the first
    live round."""
    o = _observe(BOUNDARY,
                 {"DE": ["1.1.1.1"], "NL": ["2.2.2.2"]},   # controls disagree
                 [["9.9.9.9"], ["9.9.9.9"]])               # CN differs from both
    assert o["geo_variable"] is True
    assert o["n_forged"] == 0, "a geo-split domain must not be published as blocked"
    assert o["n_clean"] == 0, "nor as clean; the control arm cannot support either"
    assert o["n_geo_variable"] == 2
    assert {v["state"] for v in o["vantages"]} == {"geo_variable"}


def test_agreeing_controls_still_allow_a_forged_verdict():
    """The guard must not defang the instrument: when the control arm agrees,
    a divergent CN answer is still forgery."""
    o = _observe(MEASUREMENT,
                 {"DE": ["1.1.1.1"], "NL": ["1.1.1.1"]},
                 [["9.9.9.9"], ["9.9.9.9"]])
    assert o["geo_variable"] is False
    assert o["n_forged"] == 2


def test_partially_overlapping_controls_are_not_treated_as_a_geo_split():
    """Overlap is agreement enough. Two probes hitting different members of one
    rotation set should not disable the whole domain."""
    o = _observe(MEASUREMENT,
                 {"DE": ["1.1.1.1", "1.0.0.1"], "NL": ["1.0.0.1"]},
                 [["9.9.9.9"]])
    assert o["geo_variable"] is False
    assert o["n_forged"] == 1


def test_a_single_control_country_cannot_declare_a_geo_split():
    """One probe cannot disagree with itself, so the guard stays off rather than
    firing on thin evidence."""
    o = _observe(MEASUREMENT, {"DE": ["1.1.1.1"]}, [["9.9.9.9"]])
    assert o["geo_variable"] is False
    assert o["n_forged"] == 1


# ── roles ─────────────────────────────────────────────────────────────────────

def test_a_forged_boundary_domain_does_not_degrade_the_round():
    """A boundary domain is expected to be blocked sometimes. Treating it as a
    benign control would throw away every round in which it was."""
    obs = [
        _observe(MEASUREMENT, {"DE": ["1.1.1.1"]}, [["9.9.9.9"]]),
        _observe(BOUNDARY, {"DE": ["1.1.1.1"]}, [["9.9.9.9"]]),
        _observe(CONTROL, {"DE": ["1.1.1.1"]}, [["1.1.1.1"]]),
    ]
    assert control_state(obs)["state"] == "SIGHTED"


def test_a_forged_control_still_degrades_the_round():
    """Regression: the gate that matters must keep working."""
    obs = [
        _observe(MEASUREMENT, {"DE": ["1.1.1.1"]}, [["9.9.9.9"]]),
        _observe(CONTROL, {"DE": ["1.1.1.1"]}, [["9.9.9.9"]]),
    ]
    assert control_state(obs)["state"] == "DEGRADED"


def test_a_reading_written_before_roles_existed_still_classifies():
    """Backfill: older observations carry expected_censored and no role."""
    o = _observe(MEASUREMENT, {"DE": ["1.1.1.1"]}, [["9.9.9.9"]])
    del o["role"]
    c = _observe(CONTROL, {"DE": ["1.1.1.1"]}, [["1.1.1.1"]])
    del c["role"]
    assert control_state([o, c])["state"] == "SIGHTED"


# ── the panel itself ──────────────────────────────────────────────────────────

def test_every_panel_entry_declares_a_role():
    assert all(e.get("role") in ("measurement", "boundary", "control") for e in PANEL)


def test_the_panel_still_has_both_a_measurement_set_and_negative_controls():
    roles = [e["role"] for e in PANEL]
    assert roles.count("measurement") >= 3
    assert roles.count("control") >= 2, "the classifier needs negative controls"


def test_boundary_domains_are_not_china_cdn_fronted():
    """A domain with a mainland PoP answers differently inside China for reasons
    that have nothing to do with a censor. The guard is the backstop, but the
    panel should not need it."""
    fronted = {"www.baidu.com", "www.qq.com", "www.taobao.com", "weibo.com"}
    assert not fronted & {e["domain"] for e in PANEL}


def test_a_round_fits_inside_the_globalping_hourly_budget():
    """Unauthenticated Globalping allows 250 probe-credits per rolling hour and
    one probe is one credit. A round that exhausts the budget mid-panel raises
    RateLimited and abstains whole, so the panel cannot outgrow the ceiling."""
    cost = len(PANEL) * (CN_PROBES + CONTROL_PROBES)
    assert cost <= 250, (
        f"a round costs {cost} credits against a 250/hour ceiling — shrink the "
        f"panel or CN_PROBES")


def test_the_cn_draw_is_wide_enough_to_span_more_than_one_operator():
    """A verdict needs two distinct ASNs, so a draw that cannot reach two
    operators can never publish a blocking verdict at all."""
    assert CN_PROBES >= 2 * len(CLOUD_ASNS), (
        "the draw should be able to land several probes on each cloud operator")


def test_sensitive_queries_stay_on_datacenter_networks():
    """The consent line. Globalping's CN pool includes household connections;
    hosting a probe is not consent to emit a censored query from a home."""
    household = {17962, 146817, 136188, 151185, 24445}   # Topway, Feixun, Ningbo…
    assert not household & set(CLOUD_ASNS)


# ── the layer this was all for ────────────────────────────────────────────────

def test_a_split_boundary_domain_produces_a_regional_verdict():
    """The point of the exercise: a domain treated differently across vantages
    is the only thing that can make REGIONAL fire, and the measurement set is
    saturated by construction."""
    o = _observe(BOUNDARY,
                 {"DE": ["1.1.1.1"], "NL": ["1.1.1.1"]},
                 [["9.9.9.9"], ["9.9.9.9"], ["1.1.1.1"], ["1.1.1.1"]])
    assert regional_divergence(o)["verdict"] == "REGIONAL"


def test_a_geo_split_domain_reaches_no_regional_verdict():
    """Nothing was classified, so there is nothing to disagree about."""
    o = _observe(BOUNDARY,
                 {"DE": ["1.1.1.1"], "NL": ["2.2.2.2"]},
                 [["9.9.9.9"], ["8.8.8.8"]])
    assert regional_divergence(o)["verdict"] == "INSUFFICIENT"


def test_no_cdn_geodns_domain_sits_in_the_panel_while_the_classifier_is_ip_based():
    """github.com read forged from every vantage in its one live round, which
    would have published "GitHub is blocked in China". It is not: China was
    answered 20.205.243.166 (AS8075 MICROSOFT), GitHub's own Asia-Pacific
    endpoint, against a control of 140.82.121.4 (AS36459 GITHUB). Same owner,
    different PoP, no censor.

    Until forgery is decided by origin-AS rather than by address, a domain that
    runs its own regional PoPs cannot be told apart from an injected one, so it
    must stay out of the panel. Re-add it with the AS comparison, not before.
    """
    geodns = {"github.com", "www.microsoft.com", "azure.com", "cloudflare.com"}
    assert not geodns & {e["domain"] for e in PANEL}
