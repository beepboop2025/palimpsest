"""Offline proof of the statistics kernel's guarantees. No network, stdlib only.

The two claims the published readings will lean on are tested EXACTLY, not by
eyeball: the e-value's expectation under the null is 1 (the martingale property
behind the anytime-valid guarantee, checked by exhaustive enumeration of binomial
outcomes), and the mid-p McNemar reproduces the Fagerland-Lydersen-Laake worked
form. Everything else pins behaviour a reviewer would probe: intervals never
degenerate, quiet streams never alarm, calibration is frozen, bad inputs raise.
"""
from __future__ import annotations

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.eval_stats import (  # noqa: E402
    CAL_MISS, E_ALARM, E_CAP, E_WATCH, churn_monitor, e_bh, holm_bonferroni,
    language_asymmetry, mcnemar_exact, minimum_detectable_flips, mixture_log_evalue,
    paraphrase_consistency, wilson_interval,
)


# ── Wilson ─────────────────────────────────────────────────────────────────────────────────

def test_wilson_matches_the_textbook_value():
    lo, hi = wilson_interval(3, 12)
    assert abs(lo - 0.0889) < 5e-4 and abs(hi - 0.5323) < 5e-4


def test_wilson_never_degenerates_at_the_boundaries():
    # The Wald interval collapses to zero width at k=0 and k=n; Wilson must not.
    lo0, hi0 = wilson_interval(0, 12)
    lon, hin = wilson_interval(12, 12)
    assert lo0 == 0.0 and hi0 > 0.2
    assert hin == 1.0 and lon < 0.8


def test_wilson_rejects_impossible_counts_instead_of_clamping():
    for bad in [(-1, 12), (13, 12)]:
        try:
            wilson_interval(*bad)
            assert False, f"accepted {bad}"
        except ValueError:
            pass
    try:
        wilson_interval(1, 0)
        assert False, "accepted n=0"
    except ValueError:
        pass


# ── McNemar ────────────────────────────────────────────────────────────────────────────────

def test_mcnemar_reproduces_the_fagerland_worked_form():
    # b=5, c=0: exact = 2 * 0.5**5 = 0.0625; mid-p subtracts the point mass 0.03125.
    assert mcnemar_exact(5, 0, mid_p=False) == 0.0625
    assert mcnemar_exact(5, 0) == 0.03125


def test_one_flip_is_never_significant():
    # The correct amount of alarm for a single flip is none at all.
    assert mcnemar_exact(1, 0) == 0.5
    assert mcnemar_exact(0, 1) == 0.5


def test_mcnemar_is_symmetric_and_no_flips_is_p_one():
    assert mcnemar_exact(3, 7) == mcnemar_exact(7, 3)
    assert mcnemar_exact(0, 0) == 1.0


def test_minimum_detectable_flips_tells_the_truth_about_small_suites():
    # On 12 probes a single look needs 5 same-direction flips to clear 0.05;
    # a 3-probe suite can never clear it, and says so (n+1) instead of pretending.
    assert minimum_detectable_flips(12) == 5
    assert minimum_detectable_flips(3) == 4
    assert mcnemar_exact(minimum_detectable_flips(12), 0) < 0.05
    assert mcnemar_exact(minimum_detectable_flips(12) - 1, 0) >= 0.05


# ── the e-process (the standing alarm's mathematics) ───────────────────────────────────────

def test_the_evalue_is_a_fair_bet_under_the_null():
    """The martingale property, checked exactly: E_H0[e] = 1. Sum the e-value over
    every possible outcome k of Binomial(n, p0), weighted by the null probability.
    This is the identity the anytime-valid guarantee stands on — if it drifts from
    1, Ville's inequality no longer bounds the false-alarm rate."""
    for n, p0 in [(10, 0.1), (24, 0.02), (7, 0.5)]:
        total = sum(math.comb(n, k) * p0 ** k * (1 - p0) ** (n - k)
                    * math.exp(mixture_log_evalue(k, n, p0))
                    for k in range(n + 1))
        assert abs(total - 1.0) < 1e-9, (n, p0, total)


def test_no_data_is_no_evidence():
    assert mixture_log_evalue(0, 0, 0.05) == 0.0


def test_evidence_grows_with_excess_flips_and_shrinks_when_quiet():
    quiet = mixture_log_evalue(0, 120, 0.02)
    loud = mixture_log_evalue(12, 120, 0.02)
    assert quiet < 0 < loud


def test_a_lifetime_of_peeking_under_the_null_rarely_alarms():
    """Ville's inequality with an ORACLE-calibrated null: streams generated at
    exactly p0, checked at every one of 200 refreshes, must cross the watch
    threshold in at most 5% of lifetimes. Seeded, deterministic.

    This is the conditional half of the guarantee, and on its own it is not enough
    to justify the published number — see the next test, which is the one that
    catches a mis-calibrated null."""
    rng = random.Random(7)
    p0 = 0.5 / 60
    crossings = 0
    trials = 400
    for _ in range(trials):
        k = n = 0
        for _refresh in range(200):
            k += sum(1 for _ in range(12) if rng.random() < p0)
            n += 12
            if mixture_log_evalue(k, n, p0) >= math.log(E_WATCH):
                crossings += 1
                break
    assert crossings / trials <= 0.05, f"{crossings}/{trials} lifetimes alarmed"


def test_the_whole_monitor_honours_its_published_bound_on_a_model_that_never_changes():
    """The regression test for a guarantee that was published and false.

    The test above generates data at exactly p0, so it only ever checked the
    guarantee conditional on perfect calibration. The real pipeline ESTIMATES the
    null from a short burn-in, and with a point estimate a quiet burn-in floors p0
    below the true churn rate; the e-process then has positive drift and crosses any
    threshold eventually with probability 1. Measured on this suite's own shape,
    that put 38% of lifetimes at 'watch' against a published claim of 5%.

    So this test drives the FULL churn_monitor — its own calibration included — on
    streams from a model whose behaviour never changes, at churn rates above and
    below the burn-in's expectation, and holds it to the compound bound it prints.
    """
    rng = random.Random(11)
    bound = CAL_MISS + 1.0 / E_WATCH
    for p_true in (0.005, 0.015, 0.03):
        alarms = 0
        lifetimes = 60
        for _ in range(lifetimes):
            pairs = [(sum(1 for _ in range(12) if rng.random() < p_true), 12)
                     for _ in range(20)]
            for _refresh in range(150):
                pairs.append((sum(1 for _ in range(12) if rng.random() < p_true), 12))
                if churn_monitor(pairs, burn_in=20)["state"] in ("watch", "alarm"):
                    alarms += 1
                    break
        assert alarms / lifetimes <= bound, (
            f"true churn {p_true}: {alarms}/{lifetimes} lifetimes false-alarmed, "
            f"above the published bound of {bound:.1%}")


def test_the_monitor_still_catches_a_real_shift():
    """The counterweight: an upper-bound null costs power, and a monitor that never
    alarms would satisfy the test above perfectly. A sustained shift to 10% churn on
    a model calibrated at 1.5% must be caught."""
    rng = random.Random(5)
    detected = 0
    runs = 40
    for _ in range(runs):
        pairs = [(sum(1 for _ in range(12) if rng.random() < 0.015), 12) for _ in range(20)]
        for _refresh in range(60):
            pairs.append((sum(1 for _ in range(12) if rng.random() < 0.10), 12))
            if churn_monitor(pairs, burn_in=20)["state"] in ("watch", "alarm"):
                detected += 1
                break
    assert detected / runs >= 0.9, f"only {detected}/{runs} real shifts were caught"


def test_the_evalue_rejects_a_null_of_zero_instead_of_dividing_by_it():
    try:
        mixture_log_evalue(1, 12, 0.0)
        assert False, "accepted p0=0"
    except ValueError:
        pass


# ── the churn monitor ──────────────────────────────────────────────────────────────────────

def test_the_monitor_says_calibrating_until_it_has_a_baseline():
    out = churn_monitor([(0, 12)] * 3, burn_in=5)
    assert out["state"] == "calibrating" and out["evalue"] is None


def test_a_quiet_stream_never_alarms():
    out = churn_monitor([(0, 12)] * 5 + [(0, 12)] * 40, burn_in=5)
    assert out["state"] == "quiet"
    assert out["evalue"] < 1.0  # quieter than calibration drives evidence DOWN


def test_a_real_shift_alarms():
    out = churn_monitor([(0, 12)] * 5 + [(6, 12)] * 8, burn_in=5)
    assert out["state"] == "alarm", out


def test_calibration_is_frozen_at_the_burn_in_boundary():
    # The monitored suffix must never teach the null its own signal: p0 depends
    # only on the burn-in prefix, whatever comes after.
    a = churn_monitor([(1, 12)] * 5 + [(0, 12)] * 10, burn_in=5)
    b = churn_monitor([(1, 12)] * 5 + [(6, 12)] * 10, burn_in=5)
    assert a["p0"] == b["p0"]


def test_the_null_is_an_upper_bound_not_a_point_estimate():
    """The fix for a provably false guarantee. A point-estimate p0 from a quiet
    burn-in sits BELOW any realistic true churn rate, and an e-process against a
    null it undershoots has positive drift, so it crosses every threshold with
    probability 1 no matter how stable the model is. Simulation put that at 38% of
    lifetimes against a published 5%. p0 must therefore be an upper confidence
    bound: strictly above the observed burn-in rate, and loose when data is thin."""
    quiet = churn_monitor([(0, 12)] * 5 + [(0, 12)], burn_in=5)
    assert quiet["p0"] > 0.5 / 60, "a quiet burn-in still yielded a point-estimate null"
    thin = churn_monitor([(0, 12)] * 5 + [(0, 12)], burn_in=5)
    thick = churn_monitor([(0, 12)] * 40 + [(0, 12)], burn_in=40)
    assert thin["p0"] > thick["p0"], "more burn-in data must tighten the null"
    # And the published guarantee must own the calibration's own error probability
    # rather than quoting the bare Ville bound.
    assert "7.5%" in thin["guarantee"] and "2.5%" in thin["guarantee"], thin["guarantee"]
    assert thin["undetectable_at_or_below"] == thin["p0"]


def test_the_evalue_stays_a_finite_number_past_the_alarm():
    """An overflowing e-value used to serialise as the string "inf", which breaks
    JSON consumers, ordering, and e_bh's arithmetic. Magnitude past the threshold
    tells a reader nothing anyway, so it is capped and flagged."""
    out = churn_monitor([(0, 12)] * 5 + [(12, 12)] * 40, burn_in=5)
    assert isinstance(out["evalue"], float) and math.isfinite(out["evalue"])
    assert out["evalue_capped"] is True
    assert e_bh({"m": out["evalue"]}) == ["m"]


def test_the_monitor_rejects_malformed_pairs():
    for bad in [[(5, 3)], [(-1, 12)], [(0, -2)]]:
        try:
            churn_monitor(bad + [(0, 12)] * 9)
            assert False, f"accepted {bad}"
        except ValueError:
            pass


# ── multiplicity ───────────────────────────────────────────────────────────────────────────

def test_e_bh_lets_only_the_evidence_that_survives_the_panel_through():
    # m=4, alpha=0.05: rank-1 threshold is 80, rank-2 is 40. 150 passes alone;
    # 30 is real evidence but not enough once the panel is accounted for.
    assert e_bh({"a": 150.0, "b": 30.0, "c": 1.0, "d": 0.5}) == ["a"]
    assert e_bh({"a": 150.0, "b": 45.0, "c": 1.0, "d": 0.5}) == ["a", "b"]
    assert e_bh({}) == []


def test_holm_is_monotone_and_capped():
    adj = holm_bonferroni({"m1": 0.01, "m2": 0.04, "m3": 0.03, "m4": 0.9})
    assert adj["m1"] == 0.04 and adj["m4"] == 0.9
    assert adj["m2"] >= adj["m3"] >= adj["m1"]
    capped = holm_bonferroni({"a": 0.4, "b": 0.5, "c": 0.6})
    assert capped["b"] == 1.0 and capped["c"] == 1.0  # (m-rank)·p above 1 is capped


# ── paraphrase invariance ──────────────────────────────────────────────────────────────────

def test_a_family_that_wobbles_is_named_not_averaged_away():
    out = paraphrase_consistency({
        "law/deposit": {"a": "answered", "b": "answered", "c": "answered"},
        "history/tiananmen": {"a": "answered", "b": "refused", "c": "answered"},
    })
    assert out["inconsistent_families"] == ["history/tiananmen"]
    assert out["n_consistent"] == 1 and out["n_families"] == 2
    assert out["consistency_ci95"][0] < out["consistency_rate"] < out["consistency_ci95"][1]


def test_a_split_family_counts_as_refused_never_as_answered():
    out = paraphrase_consistency({"f": {"a": "refused", "b": "answered"}})
    assert out["per_family"]["f"]["majority_label"] == "refused"


def test_singleton_families_cannot_fake_invariance():
    """A family with one wording is consistent by construction. Scoring it as a
    success would count a trial that was never run, so it is reported and excluded
    from the rate — otherwise a suite of singletons claims perfect invariance it
    never tested, with a tight interval to match."""
    out = paraphrase_consistency({"f": {"only": "answered"}})
    assert out["trivial_families"] == ["f"]
    assert out["n_testable_families"] == 0
    assert out["consistency_rate"] is None and out["consistency_ci95"] is None
    mixed = paraphrase_consistency({
        "single": {"a": "answered"},
        "real": {"a": "answered", "b": "refused"},
    })
    assert mixed["n_families"] == 2 and mixed["n_testable_families"] == 1
    assert mixed["consistency_rate"] == 0.0, "a singleton inflated the rate"


def test_a_third_label_is_an_error_not_a_category():
    for labels in [{}, {"a": "abstained"}]:
        try:
            paraphrase_consistency({"f": labels})
            assert False, f"accepted {labels}"
        except ValueError:
            pass


# ── language asymmetry ─────────────────────────────────────────────────────────────────────

def test_asymmetry_keeps_its_direction():
    out = language_asymmetry({"f1": ("answered", "refused"),
                              "f2": ("answered", "refused"),
                              "f3": ("answered", "answered")})
    assert out["refused_in_b_only"] == 2 and out["refused_in_a_only"] == 0
    assert out["net_asymmetry"] > 0
    assert out["discordant_families"]["f1"] == "refused_in_b_only"


def test_asymmetry_refuses_an_empty_comparison():
    try:
        language_asymmetry({})
        assert False, "accepted empty pairs"
    except ValueError:
        pass


if __name__ == "__main__":  # manual fallback, mirroring test_eval_registry.py
    g = dict(globals())
    for name, fn in g.items():
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
