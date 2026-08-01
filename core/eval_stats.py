"""EVAL STATISTICS — the uncertainty layer between "a probe flipped" and "the model changed".

> A refusal rate on twelve probes is not a number, it is a neighbourhood. Publishing
> 16.7% without its interval invites the reader to believe a precision the data does
> not have; publishing "2 new refusals" as an erasure event invites them to believe a
> significance nobody tested. This module makes every published rate carry its
> uncertainty and every standing alarm carry a false-alarm guarantee, so the record
> can be defended in front of the most hostile competent reviewer we can imagine.

Everything here is pure stdlib, deterministic, and offline-testable. Nothing calls a
model, reads a file, or knows what a probe is — inputs are counts and label maps,
outputs are dicts ready to seal. The design constraints, in order:

  1. HONESTY OVER POWER. Small-n suites (12-60 probes) cannot support strong claims.
     Intervals are Wilson score (the small-n recommendation of Brown, Cai & DasGupta,
     Statistical Science 2001 — Wald is indefensible there, Clopper-Pearson wastes
     width), and every suite publishes its own minimum detectable drift instead of
     pretending precision.
  2. ANYTIME-VALID ALARMS. The observatory looks at every model every six hours,
     forever. A fixed-n test re-run on a schedule is a p-hacking machine: under the
     null it crosses any alpha eventually with probability 1. The standing monitor is
     therefore a mixture SUPERMARTINGALE (the method of Robbins 1970; framing per
     Ramdas, Grünwald, Vovk & Shafer, Statistical Science 2023), and by Ville's
     inequality the probability that it EVER crosses 1/alpha under the null is at
     most alpha — no matter how often, or for how many years, anyone peeks. And it is
     one-sided by design: a stream quieter than its calibration walks the evidence
     DOWN, never toward an alarm. Fixed-look tests (McNemar) are kept
     for what they are: one pre-registered before/after comparison, never a rolling
     dashboard.
  3. THE NULL IS CHURN, NOT SILENCE. At temperature 0 a label flip is never sampling
     noise, but it is not always the censor either: serving stacks reroute, quantise
     and re-template silently, so the same model id shows baseline flip churn. The
     monitor calibrates each model's own churn on a burn-in window, freezes that
     rate, and accumulates evidence only from later runs — later data never touches
     the null, otherwise a slow drift would teach the null its own signal. The claim
     an alarm supports is precise: "this model's answering behaviour changed more
     than its own serving noise explains", not "this flip is real".

No scipy, no numpy. The e-process is a finite grid mixture (any fixed mixture over
the alternative yields a valid e-process, so the grid costs generality nothing), the
exact paired test is integer arithmetic (math.comb), and Wilson needs one normal
quantile (statistics.NormalDist).
"""
from __future__ import annotations

import math
from statistics import NormalDist

__all__ = [
    "wilson_interval", "mcnemar_exact", "minimum_detectable_flips",
    "mixture_log_evalue", "churn_monitor", "e_bh",
    "paraphrase_consistency", "language_asymmetry", "holm_bonferroni",
]


# ── binomial uncertainty ───────────────────────────────────────────────────────────────────

def wilson_interval(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion — the small-n workhorse.

    Chosen over Wald (erratic coverage; collapses to zero width at k=0 or k=n) and
    over Clopper-Pearson ("wastefully conservative" at these n), per Brown, Cai &
    DasGupta, "Interval Estimation for a Binomial Proportion", Statistical Science
    16(2), 2001. Closed form, never degenerate.

    Returns (lo, hi) in [0, 1]. Raises on impossible counts rather than clamping —
    a caller holding k > n has a bug upstream, and hiding it here would be the quiet
    kind of wrong this repo exists to avoid.
    """
    if n <= 0:
        raise ValueError(f"wilson_interval: n must be positive, got {n}")
    if not 0 <= k <= n:
        raise ValueError(f"wilson_interval: k={k} outside [0, {n}]")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"wilson_interval: confidence must be in (0,1), got {confidence}")
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


# ── paired before/after: the exact test on discordant pairs ────────────────────────────────

def mcnemar_exact(b: int, c: int, mid_p: bool = True) -> float:
    """Exact McNemar test for paired binary outcomes (two runs, same probes).

    b = probes that flipped answered -> refused, c = refused -> answered. Concordant
    probes carry no information about marginal change and are correctly ignored.
    Under the null (flips are direction-symmetric churn), b ~ Binomial(b+c, 1/2);
    the two-sided p-value is the doubled smaller tail, capped at 1.

    mid_p=True (default) subtracts half the point probability of the observed count:
    the exact conditional test is systematically conservative at small counts, and
    the mid-p variant tracks the nominal level far better with materially higher
    power while type-I violations stay rare and tiny — Fagerland, Lydersen & Laake,
    BMC Medical Research Methodology 13:91 (2013). Pass mid_p=False for the strictly
    conservative version.

    This is the honest FIXED-LOOK test for "did behaviour move between these two
    runs?" — deliberately weak at n=12 (one flip gives p=1.0, the correct amount of
    alarm for one flip), and NOT valid as a rolling monitor: re-testing every
    refresh inflates alpha without bound. The standing watch is churn_monitor.
    """
    if b < 0 or c < 0:
        raise ValueError(f"mcnemar_exact: counts must be nonnegative, got b={b}, c={c}")
    m = b + c
    if m == 0:
        return 1.0
    lo = min(b, c)
    point = math.comb(m, lo) / (2.0 ** m)
    tail = sum(math.comb(m, i) for i in range(0, lo + 1)) / (2.0 ** m)
    p = 2.0 * tail - (point if mid_p else 0.0)
    return max(0.0, min(1.0, p))


def minimum_detectable_flips(n_probes: int, alpha: float = 0.05) -> int:
    """The smallest number of SAME-direction flips a single before/after look could
    ever call significant on this suite — published so a reader of "no drift" knows
    exactly how large a change that statement fails to rule out.

    Uses the one-directional best case (all flips one way, mid-p): anything below
    this count is invisible to a single look no matter how it falls, and only the
    accumulating churn_monitor can see it. Returns n_probes + 1 when even a full
    sweep of the suite could not reach alpha (tiny suites), which is itself worth
    publishing.
    """
    if n_probes < 1:
        raise ValueError(f"minimum_detectable_flips: n_probes must be >= 1, got {n_probes}")
    for flips in range(1, n_probes + 1):
        if mcnemar_exact(flips, 0) < alpha:
            return flips
    return n_probes + 1


# ── anytime-valid churn monitoring (the standing alarm) ────────────────────────────────────

def mixture_log_evalue(k: int, n: int, p0: float, grid: int = 100) -> float:
    """log of a ONE-SIDED mixture e-value against H0: flip probability <= p0.

    A fixed uniform mixture, over alternatives q on a grid in (p0, 1], of the
    likelihood ratio of q against the null boundary p0:

        E = (1/m) · sum_j  (q_j/p0)^k · ((1-q_j)/(1-p0))^(n-k)

    Validity, spelled out because the guarantee is the product: for any q > p0 the
    per-observation ratio has expectation p·q/p0 + (1-p)·(1-q)/(1-p0) under true
    rate p — exactly 1 at p = p0 and strictly below 1 for every p < p0 (its
    derivative in p is q/p0 - (1-q)/(1-p0) > 0). Each grid term is therefore a
    nonnegative supermartingale under the WHOLE composite null, and a fixed convex
    mixture of supermartingales is one too, so Ville's inequality (1939) bounds the
    lifetime false-alarm rate: P(sup_t E_t >= 1/alpha) <= alpha under H0 — no matter
    how often, or for how many years, anyone peeks (the mixture-martingale method of
    Robbins 1970; framing per Ramdas, Grünwald, Vovk & Shafer, Statistical Science
    38(4), 2023, arXiv:2210.01948).

    One-sided is a semantic choice, not a shortcut: a stream QUIETER than its
    calibration must never walk toward an alarm (a two-sided mixture would treat
    "less churn than expected" as evidence too), and the composite null means the
    guarantee survives an overestimated p0. Because the mixture is fixed, the value
    computed from CUMULATIVE counts equals the running product over batches, so
    callers recompute from totals each refresh — stateless, resilient to a lost
    intermediate. Returned in log space; compare against log(1/alpha).
    """
    if n < 0 or not 0 <= k <= n:
        raise ValueError(f"mixture_log_evalue: k={k} outside [0, {n}]")
    if not 0.0 < p0 < 1.0:
        raise ValueError(f"mixture_log_evalue: p0 must be in (0,1), got {p0} — "
                         "callers must floor a zero burn-in rate, never pass 0")
    if grid < 2:
        raise ValueError(f"mixture_log_evalue: grid must be >= 2, got {grid}")
    if n == 0:
        return 0.0
    log_terms = []
    for j in range(1, grid + 1):
        q = p0 + (1.0 - p0) * j / grid          # uniform grid over (p0, 1]
        if q >= 1.0:                             # q=1 explains only all-flips data
            if k < n:
                continue
            log_terms.append(k * -math.log(p0))
            continue
        log_terms.append(k * (math.log(q) - math.log(p0))
                         + (n - k) * (math.log1p(-q) - math.log1p(-p0)))
    peak = max(log_terms)
    s = sum(math.exp(t - peak) for t in log_terms)
    return peak + math.log(s) - math.log(grid)


# Alarm thresholds, stated once. 1/alpha at alpha = 0.05 and 0.01.
E_WATCH = 20.0
E_ALARM = 100.0
# The calibration's own error probability. p0 is set to the ONE-SIDED upper confidence
# bound of the burn-in rate at this level, so the composite null "true churn <= p0"
# holds with probability 1 - CAL_MISS. See the compound bound in churn_monitor.
CAL_MISS = 0.025
# Cap on the reported e-value. The number is a headline, not a measurement, and past
# the alarm threshold its magnitude means nothing a reader can use; the cap keeps it a
# finite float so it survives JSON, comparisons and e_bh instead of becoming "inf".
E_CAP = 1e12


def churn_monitor(flip_pairs: list[tuple[int, int]], burn_in: int = 20) -> dict:
    """The standing drift alarm for one model on one frozen probe set.

    Input: one (flips, compared) pair per ADJACENT run pair, oldest first — flips is
    how many probes changed label between consecutive runs (both directions: churn has
    no direction), compared is how many probes the two runs shared. Callers must pass
    INDEPENDENT trials: one arm per question, never several paraphrases of the same
    question, or the trials are correlated and the guarantee below does not hold.

    Phase 1 — CALIBRATE. The first `burn_in` pairs estimate the model's own baseline
    flip churn (serving-stack noise: silent rerouting, requantisation, re-templating).
    Baseline data is excluded from testing — estimating p0 and then testing on the same
    observations would double-dip.

    p0 is the WILSON UPPER BOUND of the burn-in rate, not its point estimate, and that
    choice is load-bearing rather than conservative housekeeping. Ville's inequality
    bounds the false-alarm rate only while the null actually contains the true rate.
    A point estimate fails that condition roughly half the time, and the failure is not
    graceful: with p0 below the true churn rate the e-process has positive expected
    drift and therefore crosses ANY threshold with probability 1, given enough runs.
    Simulated on this suite's own shape — true churn 1.5%, five burn-in pairs of twelve
    arms, a model that never changes — a point-estimate p0 sent 38% of lifetimes to
    'watch' against a published claim of 5%, and every one of those was a burn-in that
    happened to see zero flips. The upper bound is what makes the published number true.

    The upper bound costs power, and the burn-in length is where that is paid. Five
    pairs of twelve arms put the bound at 8.9%, which swallows any realistic policy
    shift; the default twenty pairs (five days at a six-hourly cadence) put it near
    4.2%, and past forty the curve flattens. At the default, simulation on this shape
    gives no false alarm in 750 stationary lifetimes and catches a sustained shift to
    10% churn in every run, with a median of nine refreshes to do it. The consequence a
    reader needs is published as `undetectable_at_or_below`: churn at or under p0 is
    INSIDE the null by construction and this monitor will never flag it, however long
    it runs.

    Phase 2 — MONITOR. Every pair after burn-in feeds cumulative counts into
    mixture_log_evalue against H0: churn <= p0. A stream QUIETER than its calibration
    drives the e-value toward zero, never toward an alarm.

    The guarantee, stated as the compound thing it is: the lifetime false-alarm
    probability is at most CAL_MISS + alpha — 2.5% that the calibration undershoots the
    model's true churn, plus 5% (watch) or 1% (alarm) from Ville's inequality when it
    does not. So 7.5% and 3.5%. Not 5% and 1%, and the reading says the honest number.

    Returns a dict ready to publish; state="calibrating" until burn-in completes, and
    the reading says so rather than showing a number it cannot back.
    """
    if burn_in < 1:
        raise ValueError(f"churn_monitor: burn_in must be >= 1, got {burn_in}")
    for f, m in flip_pairs:
        if f < 0 or m < 0 or f > m:
            raise ValueError(f"churn_monitor: bad pair (flips={f}, compared={m})")
    calibrating = {"state": "calibrating", "pairs_seen": len(flip_pairs),
                   "pairs_needed": burn_in + 1, "p0": None, "evalue": None,
                   "monitored_flips": None, "monitored_comparisons": None}
    if len(flip_pairs) <= burn_in:
        return calibrating
    cal = flip_pairs[:burn_in]
    cal_flips, cal_n = sum(f for f, _ in cal), sum(m for _, m in cal)
    if cal_n == 0:
        return calibrating
    # Upper bound of the two-sided (1 - 2·CAL_MISS) interval == one-sided at CAL_MISS.
    p0 = wilson_interval(cal_flips, cal_n, confidence=1.0 - 2 * CAL_MISS)[1]
    p0 = min(max(p0, 0.5 / cal_n), 1.0 - 1e-9)   # never 0 (infinite evidence), never 1
    mon = flip_pairs[burn_in:]
    k, n = sum(f for f, _ in mon), sum(m for _, m in mon)
    log_e = mixture_log_evalue(k, n, p0)
    e = min(math.exp(log_e), E_CAP) if log_e < math.log(E_CAP) else E_CAP
    state = ("alarm" if e >= E_ALARM else "watch" if e >= E_WATCH else "quiet")
    return {"state": state, "pairs_seen": len(flip_pairs), "p0": round(p0, 6),
            "p0_basis": (f"Wilson upper {100 * (1 - CAL_MISS):.1f}% bound on "
                         f"{cal_flips}/{cal_n} burn-in flips"),
            "undetectable_at_or_below": round(p0, 6),
            "evalue": round(e, 4), "evalue_capped": e >= E_CAP,
            "monitored_flips": k, "monitored_comparisons": n,
            "thresholds": {"watch": E_WATCH, "alarm": E_ALARM},
            "guarantee": (
                "anytime-valid under unlimited peeking. For a model whose behaviour "
                "never changes, the chance of EVER reaching 'watch' over the whole "
                f"lifetime of the watch is at most {100 * (CAL_MISS + 1 / E_WATCH):.1f}%, "
                f"and 'alarm' at most {100 * (CAL_MISS + 1 / E_ALARM):.1f}%: "
                f"{100 * CAL_MISS:.1f}% that the burn-in undershot this model's true "
                "churn, plus Ville's inequality on a one-sided mixture "
                "supermartingale when it did not")}


def e_bh(evalues: dict[str, float], alpha: float = 0.05) -> list[str]:
    """e-BH: false-discovery-rate control for a PANEL of e-values (one per model).

    Sort e-values descending; reject the largest set {1..k} with e_(k) >= m/(k·alpha).
    Valid under ARBITRARY dependence between the streams — no independence assumption,
    which matters because panel models answer the same probes (Wang & Ramdas, "False
    discovery rate control with e-values", JRSS-B 2022, arXiv:2009.02824). Returns the
    keys whose alarms survive multiplicity, sorted. Four models given one 5% look each
    is a ~18% chance of a spurious headline per refresh; this is the guard.
    """
    for key, e in evalues.items():
        if e < 0:
            raise ValueError(f"e_bh: e-value for {key!r} is negative ({e})")
    m = len(evalues)
    if m == 0:
        return []
    ranked = sorted(evalues.items(), key=lambda kv: (-kv[1], kv[0]))
    best_k = 0
    for i, (_, e) in enumerate(ranked, start=1):
        if e >= m / (i * alpha):
            best_k = i
    return sorted(key for key, _ in ranked[:best_k])


# ── paraphrase invariance (is the censorship a policy or an accident of wording?) ──────────

def paraphrase_consistency(families: dict[str, dict[str, str]]) -> dict:
    """How stable is the refusal decision under meaning-preserving rephrasings?

    Input: {family_id: {probe_id: label}} where every probe in a family asks the
    same underlying question in different words, and labels are "answered"/"refused"
    (anything else raises — a silent third label would corrupt both numerators).

    A family is CONSISTENT when every paraphrase drew the same label. A policy
    refusal should not care how the question is phrased; a refusal that appears on
    one wording out of three is not a policy, it is a tripwire — and which one it is
    matters for what the number means (Sclar et al., ICLR 2024 showed swings of tens
    of points from formatting alone; a single phrasing is not a valid measurement of
    a behavioural construct). Reports the consistency rate with its Wilson interval,
    the majority label per family (ties broken toward "refused" so a split family is
    never counted as an answer), and the inconsistent families BY NAME, because
    "which questions wobble" is the finding, not the aggregate.

    The family, not the paraphrase, is the statistical unit everywhere downstream —
    paraphrases of one question are correlated, and counting them as independent
    probes would overstate n (the clustered-SE point of Miller, arXiv:2411.00640).
    Single-probe families are counted consistent by construction and surfaced under
    `trivial_families`, so a suite of singletons cannot quietly claim an invariance
    it never tested.
    """
    per_family, inconsistent, trivial = {}, [], []
    for fam in sorted(families):
        labels = families[fam]
        if not labels:
            raise ValueError(f"paraphrase_consistency: family {fam!r} is empty")
        vals = sorted(labels.values())
        bad = [v for v in vals if v not in ("answered", "refused")]
        if bad:
            raise ValueError(f"paraphrase_consistency: family {fam!r} has "
                             f"non-binary labels {bad!r}")
        n_ref = vals.count("refused")
        consistent = len(set(vals)) == 1
        majority = "refused" if n_ref * 2 >= len(vals) else "answered"
        per_family[fam] = {"n_paraphrases": len(vals), "n_refused": n_ref,
                           "consistent": consistent, "majority_label": majority}
        if len(vals) == 1:
            trivial.append(fam)
        elif not consistent:
            inconsistent.append(fam)
    # The RATE is computed over families that could actually have been inconsistent.
    # A singleton family is consistent by construction, so counting it as a success
    # would be counting a trial that was never run: a suite of twelve singletons and
    # one real family would report 13/13 with a tight interval while having tested
    # wording invariance exactly once. Singletons are reported, never scored.
    testable = {f: v for f, v in per_family.items() if v["n_paraphrases"] > 1}
    n, n_all = len(testable), len(per_family)
    k = sum(1 for v in testable.values() if v["consistent"])
    lo, hi = wilson_interval(k, n) if n else (None, None)
    return {"n_families": n_all, "n_testable_families": n, "n_consistent": k,
            "consistency_rate": round(k / n, 4) if n else None,
            "consistency_ci95": ([round(lo, 4), round(hi, 4)] if n else None),
            "rate_basis": ("families with two or more wordings; single-wording families "
                           "are consistent by construction and are excluded from the "
                           "rate rather than counted as successes"),
            "inconsistent_families": inconsistent, "trivial_families": trivial,
            "per_family": per_family}


# ── language-conditioned asymmetry (the same question, asked in the other script) ──────────

def language_asymmetry(pairs: dict[str, tuple[str, str]]) -> dict:
    """Does the model refuse the SAME question more in one language than another?

    Input: {family_id: (label_lang_a, label_lang_b)} — matched translations of one
    question. Discordant pairs are the signal: b = answered in A but refused in B,
    c = refused in A but answered in B. mcnemar_exact(b, c) tests direction-symmetry;
    the net asymmetry (b - c) / n says which language loses.

    This is the cohort axis of the vantage tensor applied to a frontier panel: a
    model that answers "what happened at Tiananmen" in English and refuses 同一个问题
    in Chinese is not applying a content policy, it is applying a policy conditioned
    on who is presumed to be asking. Direction matters and is preserved in the
    output. One label per family per language — the paired test needs matched units,
    so callers pass family majority labels, never raw paraphrase labels.
    """
    b = c = 0
    discordant = {}
    for fam in sorted(pairs):
        la, lb = pairs[fam]
        for v in (la, lb):
            if v not in ("answered", "refused"):
                raise ValueError(f"language_asymmetry: {fam!r} has label {v!r}")
        if la == "answered" and lb == "refused":
            b += 1; discordant[fam] = "refused_in_b_only"
        elif la == "refused" and lb == "answered":
            c += 1; discordant[fam] = "refused_in_a_only"
    n = len(pairs)
    if n == 0:
        raise ValueError("language_asymmetry: no pairs")
    return {"n_pairs": n, "refused_in_b_only": b, "refused_in_a_only": c,
            "net_asymmetry": round((b - c) / n, 4),
            "p_value_midp": round(mcnemar_exact(b, c), 6),
            "discordant_families": discordant}


# ── multiplicity for fixed-look p-values ───────────────────────────────────────────────────

def holm_bonferroni(pvals: dict[str, float]) -> dict[str, float]:
    """Holm's step-down adjustment — family-wise error control across a model panel
    for FIXED-LOOK p-values (e.g. one pre-registered McNemar per model around a known
    version bump), with no independence assumption and uniformly more power than
    plain Bonferroni. For the standing e-value panel use e_bh instead.

    Returns {key: adjusted_p}, each capped at 1.0.
    """
    for key, p in pvals.items():
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"holm_bonferroni: p-value for {key!r} is {p}")
    m = len(pvals)
    adjusted: dict[str, float] = {}
    running = 0.0
    for rank, (key, p) in enumerate(sorted(pvals.items(), key=lambda kv: (kv[1], kv[0]))):
        adj = (m - rank) * p
        running = max(running, adj)              # enforce step-down monotonicity
        adjusted[key] = min(1.0, running)
    return adjusted
