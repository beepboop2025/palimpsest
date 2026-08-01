"""Offline proof of the agreement statistics. No network, stdlib only.

Krippendorff's alpha is checked against a HAND-COMPUTED coincidence matrix rather than
against another implementation, because the point of having it is that the number the
study rests on can be recomputed by someone with a pencil. The degenerate cases are
pinned too: a coefficient that returns 1.0 when both coders used a single label
throughout would report perfect reliability on a sample that demonstrated nothing.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.agreement import (  # noqa: E402
    ALPHA_THRESHOLDS, agreement_report, bootstrap_ci, cohens_kappa, krippendorff_alpha,
    raw_agreement, verdict,
)


# ── Krippendorff's alpha, against arithmetic done by hand ───────────────────────────────────

def test_alpha_matches_a_hand_computed_coincidence_matrix():
    """Four units, two coders, labels A/B: AA, AB, BB, BA.

    Each unit has m=2, so each of its two ordered pairs contributes 1/(m-1) = 1:
        o_AA = 2, o_BB = 2, o_AB = 2, o_BA = 2
        n_A = o_AA + o_AB = 4, n_B = o_BA + o_BB = 4, n = 8
        D_o = (1/8)(o_AB + o_BA) = 4/8 = 0.5
        D_e = (1/(8*7))(n_A*n_B + n_B*n_A) = 32/56 = 0.571428...
        alpha = 1 - 0.5/0.571428... = 0.125
    """
    codings = {"u1": ["A", "A"], "u2": ["A", "B"], "u3": ["B", "B"], "u4": ["B", "A"]}
    assert abs(krippendorff_alpha(codings) - 0.125) < 1e-12


def test_alpha_and_kappa_are_not_the_same_statistic():
    """On the case above kappa is exactly 0 while alpha is 0.125. They use different
    chance corrections, so reporting one as the other would be wrong even when the two
    happen to be close."""
    codings = {"u1": ["A", "A"], "u2": ["A", "B"], "u3": ["B", "B"], "u4": ["B", "A"]}
    assert abs(cohens_kappa(codings) - 0.0) < 1e-12
    assert krippendorff_alpha(codings) != cohens_kappa(codings)


def test_perfect_agreement_is_one_and_disagreement_can_go_negative():
    assert krippendorff_alpha({"a": ["X", "X"], "b": ["Y", "Y"], "c": ["X", "X"]}) == 1.0
    # Systematically opposite coders are WORSE than chance, and alpha says so with a
    # negative number rather than flooring at zero.
    systematic = {"a": ["X", "Y"], "b": ["Y", "X"], "c": ["X", "Y"], "d": ["Y", "X"]}
    assert krippendorff_alpha(systematic) < 0


def test_a_single_label_throughout_is_undefined_not_perfect():
    """The failure mode that would silently manufacture a result: if both coders label
    everything `answered`, expected disagreement is zero and alpha is 0/0. Returning 1.0
    there would claim perfect reliability from a sample with no discrimination in it."""
    assert krippendorff_alpha({"a": ["X", "X"], "b": ["X", "X"], "c": ["X", "X"]}) is None
    assert cohens_kappa({"a": ["X", "X"], "b": ["X", "X"]}) is None
    assert verdict(None)["band"] == "undefined"
    assert verdict(None)["usable"] is False


def test_alpha_handles_more_than_two_coders_and_missing_labels():
    """Kappa cannot do either, which is the practical reason alpha is primary. A unit
    only one coder reached carries no agreement information and must be dropped, not
    counted as agreeing with itself."""
    codings = {"a": ["X", "X", "X"], "b": ["X", "Y", "X"], "c": ["Y", "Y"], "d": ["X"]}
    a = krippendorff_alpha(codings)
    assert a is not None and 0 < a < 1
    rep = agreement_report(codings, iterations=200)
    assert rep["n_units_coded_by_all"] == 3      # 'd' excluded
    assert rep["n_units_submitted"] == 4


def test_units_only_one_coder_reached_are_excluded():
    both = {"a": ["X", "X"], "b": ["X", "Y"]}
    plus_singleton = dict(both, c=["X"])
    assert krippendorff_alpha(both) == krippendorff_alpha(plus_singleton)


# ── the kappa paradox, which is why alpha is primary ───────────────────────────────────────

def test_a_dominant_category_is_penalised_by_both_and_alpha_is_the_defensible_one():
    """This draw is dominated by `answered` (489 in pool against 17 refused), the regime
    where kappa is known to misbehave. Both coefficients must fall well below the raw
    agreement rate, so neither can be used to claim reliability that high agreement on a
    skewed sample does not establish."""
    codings = {}
    for i in range(200):
        if i < 190:
            codings[f"u{i}"] = ["answered", "answered"]
        elif i < 195:
            codings[f"u{i}"] = ["refused", "refused"]
        else:
            codings[f"u{i}"] = ["answered", "refused"]
    assert raw_agreement(codings) > 0.97
    assert krippendorff_alpha(codings) < 0.8      # nowhere near the raw rate
    assert cohens_kappa(codings) < 0.8


# ── the bootstrap ──────────────────────────────────────────────────────────────────────────

def test_the_bootstrap_brackets_the_point_estimate_and_is_deterministic():
    codings = {f"u{i}": (["X", "X"] if i % 4 else ["X", "Y"]) for i in range(80)}
    point = krippendorff_alpha(codings)
    lo, hi, undef = bootstrap_ci(codings, krippendorff_alpha, iterations=500, seed=1)
    assert lo is not None and lo <= point <= hi
    again = bootstrap_ci(codings, krippendorff_alpha, iterations=500, seed=1)
    assert (lo, hi, undef) == again, "same seed must give the same interval"


def test_undefined_resamples_are_discarded_not_counted_as_zero():
    """With a dominant category a resample can easily contain one label only, making
    alpha undefined. Folding those in as zeros would drag the interval down for a reason
    that is arithmetic rather than empirical, so they are dropped and counted."""
    codings = {f"u{i}": (["X", "X"] if i else ["X", "Y"]) for i in range(6)}
    lo, hi, undef = bootstrap_ci(codings, krippendorff_alpha, iterations=800, seed=3)
    assert undef > 0
    if lo is not None:
        assert lo >= -1.0 and hi <= 1.0


def test_a_bootstrap_with_too_few_usable_resamples_abstains():
    lo, hi, _ = bootstrap_ci({"a": ["X", "X"]}, krippendorff_alpha, iterations=100)
    assert lo is None and hi is None


# ── the verdict, which is the gate the study is read through ───────────────────────────────

def test_the_bands_are_krippendorffs_and_only_rely_is_usable():
    assert verdict(0.85)["band"] == "rely" and verdict(0.85)["usable"] is True
    assert verdict(ALPHA_THRESHOLDS["rely"])["band"] == "rely"
    # Tentative is NOT usable: it means the labels may be reported with a caveat, not
    # that a machine score measured against them may be quoted as established.
    assert verdict(0.70)["band"] == "tentative" and verdict(0.70)["usable"] is False
    assert verdict(ALPHA_THRESHOLDS["tentative"])["band"] == "tentative"
    assert verdict(0.5)["band"] == "reject" and verdict(0.5)["usable"] is False


def test_the_report_never_hands_out_a_bare_coefficient():
    codings = {f"u{i}": (["X", "X"] if i % 3 else ["X", "Y"]) for i in range(60)}
    rep = agreement_report(codings, iterations=300)
    # Anything quoting the alpha has the interval and the band in the same dict.
    assert rep["krippendorff_alpha"] is not None
    assert rep["krippendorff_alpha_ci95"] is not None
    assert rep["verdict"]["band"] in ("rely", "tentative", "reject")
    assert rep["thresholds"] == ALPHA_THRESHOLDS
    assert "alpha" in rep["which_to_believe"]


def test_empty_input_abstains_rather_than_raising():
    assert krippendorff_alpha({}) is None
    assert raw_agreement({}) is None
    assert cohens_kappa({}) is None
