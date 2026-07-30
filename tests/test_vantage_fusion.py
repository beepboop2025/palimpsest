"""Vantage fusion: the point is triangulation that MEASURES disagreement, not
an average that hides it. Tests pin corroboration vs contest, the routing-
divergence flag, single-vantage honesty, net4people-as-modifier-only, and the
live-readings smoke test."""
from datetime import datetime, timedelta, timezone

from processors.vantage_fusion import MAX_AGE_HOURS, fuse

# Every real reading carries generated_at (verified against all three live feeds), and a
# vantage that cannot be shown to be current is excluded — so the fixtures stamp one. Ages
# are expressed in hours before NOW so a test can make a single vantage go stale.
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _stamp(age_h):
    return (NOW - timedelta(hours=age_h)).isoformat()


def _r(ooni=None, cp=None, n4p=None, ooni_age=1.0, cp_age=1.0, n4p_age=1.0):
    d = {}
    if ooni is not None:
        d["ooni"] = {"gfw_index": ooni, "generated_at": _stamp(ooni_age)}
    if cp is not None:
        d["censored_planet"] = {"cn_interference_rate_pct": cp, "generated_at": _stamp(cp_age)}
    if n4p is not None:
        d["net4people"] = {**n4p, "generated_at": _stamp(n4p_age)}
    return d


# ── corroboration vs contest ────────────────────────────────────────────────────

def test_agreeing_methods_are_corroborated():
    r = fuse(_r(ooni=55, cp=50), now=NOW)
    assert r["ok"] and r["confidence"] == "CORROBORATED"
    assert r["agreement"] > 0.8
    assert 50 <= r["fused_index"] <= 55


def test_diverging_methods_are_contested_and_flagged():
    r = fuse(_r(ooni=70, cp=10), now=NOW)  # 60pp apart
    assert r["confidence"] == "CONTESTED"
    assert r["divergence_pp"] == 60.0
    assert r["agreement"] <= 0.05  # near-total disagreement
    # the verdict must refuse the single number outright, not merely label it
    assert "NO SINGLE RATE IS DEFENSIBLE" in r["verdict"]
    assert "vantage-dependent" in r["verdict"]


def test_disagreement_becomes_interval_width_not_a_label():
    """The failure this replaced: OONI 59.2 and Censored Planet 4.8 were published
    as a fused 29.3 with a CONTESTED tag, and a reader takes 29.3 as the estimate."""
    r = fuse(_r(ooni=59.2, cp=4.8), now=NOW)
    assert r["interval"] == [4.8, 59.2]
    assert r["interval_width_pp"] > 50
    assert r["single_rate_quotable"] is False


def test_agreeing_methods_yield_a_narrow_quotable_interval():
    r = fuse(_r(ooni=55, cp=50), now=NOW)
    assert r["interval"] == [50.0, 55.0]
    assert r["interval_width_pp"] == 5.0
    assert r["single_rate_quotable"] is True
    assert "NO SINGLE RATE" not in r["verdict"]


def test_single_vantage_forms_no_interval_and_is_not_quotable():
    r = fuse(_r(ooni=40), now=NOW)
    assert r["interval"] == [40.0, 40.0]
    assert r["single_rate_quotable"] is False
    assert "uncorroborated" in r["verdict"]


def test_unquotable_reading_says_so_in_its_caveats():
    r = fuse(_r(ooni=70, cp=10), now=NOW)
    assert any("not a defensible" in c.lower() for c in r["caveats"])


def test_single_vantage_is_uncorroborated():
    r = fuse(_r(ooni=40), now=NOW)
    assert r["confidence"] == "SINGLE"
    assert r["agreement"] is None
    assert r["fused_index"] == 40.0  # the lone vantage, renormalized


def test_no_quantitative_vantage_abstains():
    assert fuse(_r(n4p={"n_recent": 5, "n_blocking": 3}), now=NOW)["ok"] is False
    assert fuse({})["ok"] is False


# ── weighting ───────────────────────────────────────────────────────────────────

def test_censored_planet_weighted_above_ooni():
    # with CP high and OONI low, the fused number leans toward CP (higher weight)
    r = fuse(_r(ooni=0, cp=100), now=NOW)
    assert r["fused_index"] > 50  # CP's 0.55 weight pulls above the midpoint


def test_missing_vantage_renormalizes_not_zero():
    # OONI alone must read as itself, not be diluted by an absent CP scoring 0
    assert fuse(_r(ooni=80), now=NOW)["fused_index"] == 80.0


# ── net4people as a confidence modifier only ────────────────────────────────────

def test_net4people_never_moves_the_rate():
    without = fuse(_r(ooni=50, cp=50), now=NOW)["fused_index"]
    with_reports = fuse(_r(ooni=50, cp=50,
                           n4p={"n_recent": 10, "n_blocking": 8}), now=NOW)["fused_index"]
    assert without == with_reports  # qualitative signal does not move the number


def test_blocking_reports_while_calm_is_flagged():
    r = fuse(_r(ooni=5, cp=5, n4p={"n_recent": 6, "n_blocking": 5}), now=NOW)
    assert r["qualitative_flag"] and "under-count" in r["qualitative_flag"]


def test_blocking_reports_while_elevated_corroborates():
    r = fuse(_r(ooni=60, cp=55, n4p={"n_recent": 6, "n_blocking": 5}), now=NOW)
    assert r["qualitative_flag"] and "corroborate" in r["qualitative_flag"]


# ── freshness: a stale vantage is excluded, never fused in as though it were today ──

def test_stale_vantage_is_excluded_and_reported():
    """The failure this prevents: a feed dies quietly, its last reading keeps fusing in
    at full coverage weight, and the board publishes a 'corroborated' number built partly
    from a week-old measurement."""
    r = fuse(_r(ooni=59.2, cp=4.8, cp_age=MAX_AGE_HOURS["censored_planet"] + 1), now=NOW)
    assert r["ok"]
    assert "censored_planet" not in r["vantages"]      # dropped, not silently averaged
    assert r["fused_index"] == 59.2                    # the fresh vantage, renormalized
    assert r["confidence"] == "SINGLE"                 # honestly uncorroborated now
    ex = r["excluded_vantages"]["censored_planet"]
    assert ex["reason"] == "stale"
    assert ex["age_hours"] > ex["bound_hours"]
    assert ex["as_of"]                                 # says exactly how old it was


def test_a_vantage_just_inside_its_bound_still_counts():
    """The bound must tolerate a single missed refresh, or a healthy feed flaps."""
    r = fuse(_r(ooni=59.2, cp=4.8, cp_age=MAX_AGE_HOURS["censored_planet"] - 1), now=NOW)
    assert set(r["vantages"]) == {"ooni", "censored_planet"}
    assert r["excluded_vantages"] == {}


def test_undated_vantage_cannot_be_shown_current_and_is_excluded():
    r = fuse({"ooni": {"gfw_index": 40.0}}, now=NOW)
    assert r["ok"] is False
    assert r["excluded_vantages"]["ooni"]["reason"] == "undated"


def test_all_vantages_stale_abstains_rather_than_publishing():
    r = fuse(_r(ooni=59.2, cp=4.8, ooni_age=500, cp_age=500), now=NOW)
    assert r["ok"] is False
    assert "current enough" in r["reason"]
    assert set(r["excluded_vantages"]) == {"ooni", "censored_planet"}


def test_stale_net4people_does_not_flag_the_qualitative_modifier():
    """net4people only modifies confidence, but a dead forum feed must not keep
    asserting 'blocking reports while calm' forever."""
    r = fuse(_r(ooni=5, cp=5, n4p={"n_recent": 6, "n_blocking": 5},
                n4p_age=MAX_AGE_HOURS["net4people"] + 1), now=NOW)
    assert r["net4people"]["present"] is False
    assert r["excluded_vantages"]["net4people"]["reason"] == "stale"


# ── live readings smoke test ────────────────────────────────────────────────────

def test_fuse_on_live_repo_readings():
    import json
    import os
    readings = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "readings")

    def load(name):
        p = os.path.join(readings, name)
        return json.load(open(p)) if os.path.exists(p) else {}

    live = {"ooni": load("ooni-gfw-latest.json"),
            "censored_planet": load("censored-planet-latest.json"),
            "net4people": load("net4people-latest.json")}

    # Anchor "now" to the newest committed reading rather than the wall clock. The
    # committed readings age every day this repo is not refreshed, so using the real
    # clock would turn this smoke test into a time bomb that starts failing once they
    # pass their freshness bounds — a fact about the checkout's age, not about fuse().
    stamps = [datetime.fromisoformat(v["generated_at"]) for v in live.values()
              if isinstance(v.get("generated_at"), str)]
    assert stamps, "every committed vantage reading should carry generated_at"
    now = max(stamps)

    r = fuse(live, now=now)
    # the repo ships all three, so fusion should produce a corroborated/contested
    # number with an agreement score
    assert r["ok"]
    assert r["confidence"] in ("CORROBORATED", "CONTESTED", "SINGLE")
    assert isinstance(r["verdict"], str)
