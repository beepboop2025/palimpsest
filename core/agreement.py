"""INTER-CODER AGREEMENT — how much the humans agree, and how sure we are of that.

> Every refusal rate this project publishes is produced by a lexical rule. Whether that
> rule is RIGHT is not a question the hash chain can answer, and it is not a question
> the rule can answer about itself. It is answered by two people reading the same
> responses without seeing each other's labels or the machine's, and by a statistic
> that says how much they agreed. This module is that statistic, with an interval on it.

Two coefficients, and the reason for both:

  Cohen's kappa       — the familiar one, kept because reviewers look for it. Derives
                        chance agreement from each coder's SEPARATE marginals, which is
                        also its weakness.
  Krippendorff's alpha — the preferred one. Pools the marginals instead, extends to more
                        than two coders and to missing labels, and does not exhibit the
                        "kappa paradox" where a dominant category drives kappa toward
                        zero at very high raw agreement. That regime is not hypothetical
                        here: the GFI draw has 489 `answered` responses in its pool
                        against 17 `refused`, so a kappa alone would understate
                        reliability and a reviewer would rightly say so.

Both come with a bootstrap interval over UNITS, because a point estimate on 145 rows
(with strata of 17 and 38) is a number pretending to be a measurement. Krippendorff's
own thresholds are carried in `ALPHA_THRESHOLDS` and applied by `verdict()`: at least
0.800 to rely on the labels, 0.667 to 0.800 for tentative conclusions only, and below
0.667 the labelling scheme is rejected rather than reported.

Pure stdlib, deterministic given a seed, no network. Nothing here knows what a refusal
is; it takes labels and returns agreement.

References: Krippendorff, *Content Analysis* (3rd ed. 2013) and "Reliability in Content
Analysis: Some Common Misconceptions", *Human Communication Research* 30(3), 2004;
Hayes & Krippendorff, "Answering the Call for a Standard Reliability Measure",
*Communication Methods and Measures* 1(1), 2007, for the bootstrap over units;
Cohen, *Educational and Psychological Measurement* 20, 1960.
"""
from __future__ import annotations

import random
from collections import Counter

__all__ = [
    "ALPHA_THRESHOLDS", "krippendorff_alpha", "cohens_kappa", "raw_agreement",
    "bootstrap_ci", "verdict", "agreement_report",
]

# Krippendorff's published cut-offs. Stated as data so the report cannot quietly use a
# friendlier bar than the one the pre-registration froze.
ALPHA_THRESHOLDS = {"rely": 0.800, "tentative": 0.667}


def _units(codings: dict[str, list[str]]) -> list[list[str]]:
    """Units with at least two labels. A unit only one coder reached carries no
    information about agreement and is dropped here rather than counted as agreeing."""
    return [labels for labels in codings.values() if len(labels) >= 2]


def raw_agreement(codings: dict[str, list[str]]) -> float | None:
    """Share of units on which every coder chose the same label. Descriptive only:
    quoting it alone is how a study with one dominant category claims reliability it
    has not shown."""
    us = _units(codings)
    if not us:
        return None
    return sum(1 for labels in us if len(set(labels)) == 1) / len(us)


def krippendorff_alpha(codings: dict[str, list[str]]) -> float | None:
    """Krippendorff's alpha for NOMINAL data, any number of coders, missing labels ok.

    Input: {unit_id: [label, label, ...]} — one entry per coder who labelled that unit.

    Builds the coincidence matrix: for each unit with m labels, every ORDERED pair of
    labels within it contributes 1/(m-1). Then, with delta being 0 for c == k and 1
    otherwise,

        D_o = (1/n) · sum_{c != k} o_ck
        D_e = (1/(n(n-1))) · sum_{c != k} n_c · n_k
        alpha = 1 - D_o / D_e

    Returns None when alpha is undefined, which happens when every coder used one and
    the same single label throughout: expected disagreement is then zero and the
    quotient is 0/0. That is reported as undefined, never as 1.0, because a sample
    carrying no discrimination has demonstrated nothing about the coders.
    """
    us = _units(codings)
    if not us:
        return None
    coincidence: Counter = Counter()
    for labels in us:
        m = len(labels)
        for i, a in enumerate(labels):
            for j, b in enumerate(labels):
                if i != j:
                    coincidence[(a, b)] += 1.0 / (m - 1)
    if not coincidence:
        return None
    marginal: Counter = Counter()
    for (a, _b), w in coincidence.items():
        marginal[a] += w
    n = sum(marginal.values())
    if n <= 1:
        return None
    observed = sum(w for (a, b), w in coincidence.items() if a != b) / n
    expected = sum(marginal[a] * marginal[b]
                   for a in marginal for b in marginal if a != b) / (n * (n - 1))
    if expected == 0:
        return None
    return 1.0 - observed / expected


def cohens_kappa(codings: dict[str, list[str]], labels=None) -> float | None:
    """Cohen's kappa for exactly two coders. None when chance agreement is 1.0.

    Kept for familiarity, and reported beside alpha rather than instead of it. Where the
    two disagree, alpha is the one to believe: kappa's chance correction uses each
    coder's own marginals, which inflates expected agreement when one category dominates
    and drives the coefficient down even at very high observed agreement.
    """
    pairs = [tuple(v) for v in codings.values() if len(v) == 2]
    if not pairs:
        return None
    n = len(pairs)
    seen = sorted(labels) if labels else sorted({x for p in pairs for x in p})
    po = sum(1 for a, b in pairs if a == b) / n
    c1, c2 = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum((c1[l] / n) * (c2[l] / n) for l in seen)
    return None if pe >= 1 else (po - pe) / (1 - pe)


def bootstrap_ci(codings: dict[str, list[str]], statistic, *, iterations: int = 2000,
                 seed: int = 20260801, confidence: float = 0.95):
    """Percentile bootstrap interval, resampling UNITS with replacement.

    Units are the independent things here, not individual labels: resampling labels
    would break the pairing that agreement is defined over. Resamples on which the
    statistic is undefined are DISCARDED and counted, not silently treated as zero —
    with a dominant category, a resample can easily contain one label only, and folding
    those in as zeros would drag the interval down for a reason that is arithmetic
    rather than empirical.

    Returns (lo, hi, n_undefined). (None, None, n) when too few resamples were usable
    for an interval to mean anything.
    """
    ids = list(codings)
    if len(ids) < 2:
        return (None, None, 0)
    rng = random.Random(seed)
    draws, undefined = [], 0
    for _ in range(iterations):
        picked = [ids[rng.randrange(len(ids))] for _ in ids]
        # Re-key duplicates: a unit drawn twice must count twice, so it cannot collide.
        resample = {f"{uid}#{n}": codings[uid] for n, uid in enumerate(picked)}
        value = statistic(resample)
        if value is None:
            undefined += 1
        else:
            draws.append(value)
    if len(draws) < 100:
        return (None, None, undefined)
    draws.sort()
    tail = (1.0 - confidence) / 2.0
    lo = draws[max(0, int(tail * len(draws)) - 1)]
    hi = draws[min(len(draws) - 1, int((1.0 - tail) * len(draws)))]
    return (lo, hi, undefined)


def verdict(alpha: float | None) -> dict:
    """Krippendorff's own reading of his coefficient, applied without softening.

    The `usable` flag is what a caller should gate on. It is False for a tentative
    result as well as a rejected one, because "tentative" means the labels may be
    reported with the caveat attached, not that a machine-agreement number computed
    against them may be quoted as established.
    """
    if alpha is None:
        return {"band": "undefined", "usable": False,
                "reading": ("alpha is undefined: the coders used a single identical label "
                            "throughout, so there is no agreement beyond chance to measure "
                            "and the sample has demonstrated nothing")}
    if alpha >= ALPHA_THRESHOLDS["rely"]:
        return {"band": "rely", "usable": True,
                "reading": (f"alpha {alpha:.3f} is at or above {ALPHA_THRESHOLDS['rely']}: "
                            "the labels may be relied on, so a machine-agreement number "
                            "measured against them can be published as such")}
    if alpha >= ALPHA_THRESHOLDS["tentative"]:
        return {"band": "tentative", "usable": False,
                "reading": (f"alpha {alpha:.3f} is between {ALPHA_THRESHOLDS['tentative']} and "
                            f"{ALPHA_THRESHOLDS['rely']}: tentative conclusions only. The "
                            "codebook needs sharpening before any machine-agreement figure "
                            "is quoted as established")}
    return {"band": "reject", "usable": False,
            "reading": (f"alpha {alpha:.3f} is below {ALPHA_THRESHOLDS['tentative']}: the "
                        "labelling scheme is rejected. The construct is too fuzzy for humans "
                        "to apply consistently, so no machine score against it means anything "
                        "and the honest result is to say so and rebuild the codebook")}


def agreement_report(codings: dict[str, list[str]], *, labels=None,
                     iterations: int = 2000, seed: int = 20260801) -> dict:
    """The human-vs-human block, ready to publish.

    Deliberately reports the interval alongside every coefficient and the verdict
    alongside the interval, so no consumer of this dict can quote a bare number.
    """
    us = _units(codings)
    alpha = krippendorff_alpha(codings)
    kappa = cohens_kappa(codings, labels=labels)
    a_lo, a_hi, a_undef = bootstrap_ci(codings, krippendorff_alpha,
                                       iterations=iterations, seed=seed)
    n_coders = sorted({len(v) for v in codings.values() if v})
    return {
        "n_units_coded_by_all": len(us),
        "n_units_submitted": len(codings),
        "labels_per_unit": n_coders,
        "raw_agreement": _r(raw_agreement(codings), 4),
        "krippendorff_alpha": _r(alpha, 4),
        "krippendorff_alpha_ci95": ([_r(a_lo, 4), _r(a_hi, 4)] if a_lo is not None else None),
        "bootstrap": {"iterations": iterations, "seed": seed,
                      "resamples_undefined_and_discarded": a_undef},
        "cohens_kappa": _r(kappa, 4),
        "thresholds": dict(ALPHA_THRESHOLDS),
        "verdict": verdict(alpha),
        "which_to_believe": ("alpha, where the two coefficients disagree: kappa derives "
                            "chance agreement from each coder's separate marginals, which "
                            "inflates it when one label dominates the sample, and this "
                            "sample is dominated by `answered`"),
    }


def _r(x, places):
    return round(x, places) if isinstance(x, float) else x
