"""Vantage fusion: the point is triangulation that MEASURES disagreement, not
an average that hides it. Tests pin corroboration vs contest, the routing-
divergence flag, single-vantage honesty, net4people-as-modifier-only, and the
live-readings smoke test."""
from processors.vantage_fusion import fuse


def _r(ooni=None, cp=None, n4p=None):
    d = {}
    if ooni is not None:
        d["ooni"] = {"gfw_index": ooni}
    if cp is not None:
        d["censored_planet"] = {"cn_interference_rate_pct": cp}
    if n4p is not None:
        d["net4people"] = n4p
    return d


# ── corroboration vs contest ────────────────────────────────────────────────────

def test_agreeing_methods_are_corroborated():
    r = fuse(_r(ooni=55, cp=50))
    assert r["ok"] and r["confidence"] == "CORROBORATED"
    assert r["agreement"] > 0.8
    assert 50 <= r["fused_index"] <= 55


def test_diverging_methods_are_contested_and_flagged():
    r = fuse(_r(ooni=70, cp=10))  # 60pp apart
    assert r["confidence"] == "CONTESTED"
    assert r["divergence_pp"] == 60.0
    assert r["agreement"] <= 0.05  # near-total disagreement
    # the verdict must refuse the single number outright, not merely label it
    assert "NO SINGLE RATE IS DEFENSIBLE" in r["verdict"]
    assert "vantage-dependent" in r["verdict"]


def test_disagreement_becomes_interval_width_not_a_label():
    """The failure this replaced: OONI 59.2 and Censored Planet 4.8 were published
    as a fused 29.3 with a CONTESTED tag, and a reader takes 29.3 as the estimate."""
    r = fuse(_r(ooni=59.2, cp=4.8))
    assert r["interval"] == [4.8, 59.2]
    assert r["interval_width_pp"] > 50
    assert r["single_rate_quotable"] is False


def test_agreeing_methods_yield_a_narrow_quotable_interval():
    r = fuse(_r(ooni=55, cp=50))
    assert r["interval"] == [50.0, 55.0]
    assert r["interval_width_pp"] == 5.0
    assert r["single_rate_quotable"] is True
    assert "NO SINGLE RATE" not in r["verdict"]


def test_single_vantage_forms_no_interval_and_is_not_quotable():
    r = fuse(_r(ooni=40))
    assert r["interval"] == [40.0, 40.0]
    assert r["single_rate_quotable"] is False
    assert "uncorroborated" in r["verdict"]


def test_unquotable_reading_says_so_in_its_caveats():
    r = fuse(_r(ooni=70, cp=10))
    assert any("not a defensible" in c.lower() for c in r["caveats"])


def test_single_vantage_is_uncorroborated():
    r = fuse(_r(ooni=40))
    assert r["confidence"] == "SINGLE"
    assert r["agreement"] is None
    assert r["fused_index"] == 40.0  # the lone vantage, renormalized


def test_no_quantitative_vantage_abstains():
    assert fuse(_r(n4p={"n_recent": 5, "n_blocking": 3}))["ok"] is False
    assert fuse({})["ok"] is False


# ── weighting ───────────────────────────────────────────────────────────────────

def test_censored_planet_weighted_above_ooni():
    # with CP high and OONI low, the fused number leans toward CP (higher weight)
    r = fuse(_r(ooni=0, cp=100))
    assert r["fused_index"] > 50  # CP's 0.55 weight pulls above the midpoint


def test_missing_vantage_renormalizes_not_zero():
    # OONI alone must read as itself, not be diluted by an absent CP scoring 0
    assert fuse(_r(ooni=80))["fused_index"] == 80.0


# ── net4people as a confidence modifier only ────────────────────────────────────

def test_net4people_never_moves_the_rate():
    without = fuse(_r(ooni=50, cp=50))["fused_index"]
    with_reports = fuse(_r(ooni=50, cp=50,
                           n4p={"n_recent": 10, "n_blocking": 8}))["fused_index"]
    assert without == with_reports  # qualitative signal does not move the number


def test_blocking_reports_while_calm_is_flagged():
    r = fuse(_r(ooni=5, cp=5, n4p={"n_recent": 6, "n_blocking": 5}))
    assert r["qualitative_flag"] and "under-count" in r["qualitative_flag"]


def test_blocking_reports_while_elevated_corroborates():
    r = fuse(_r(ooni=60, cp=55, n4p={"n_recent": 6, "n_blocking": 5}))
    assert r["qualitative_flag"] and "corroborate" in r["qualitative_flag"]


# ── live readings smoke test ────────────────────────────────────────────────────

def test_fuse_on_live_repo_readings():
    import json
    import os
    readings = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "readings")

    def load(name):
        p = os.path.join(readings, name)
        return json.load(open(p)) if os.path.exists(p) else {}

    r = fuse({"ooni": load("ooni-gfw-latest.json"),
              "censored_planet": load("censored-planet-latest.json"),
              "net4people": load("net4people-latest.json")})
    # the repo ships all three, so fusion should produce a corroborated/contested
    # number with an agreement score
    assert r["ok"]
    assert r["confidence"] in ("CORROBORATED", "CONTESTED", "SINGLE")
    assert isinstance(r["verdict"], str)
