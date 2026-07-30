"""A term seen once is not as new as a term seen twenty times.

    PYTHONPATH=. python3 -m pytest tests/test_novelty_evidence_shrinkage.py -q

Repairing the CDT feed exposed a knob that had never been turned. While DDTI ran on one page
of one feed, every term fell inside the current window with nothing behind it, so every term
scored novelty=1.0 and the novelty weight was a no-op — the ordering was decided by attention
alone. With a real history band underneath it, novelty finally discriminates, and the first
thing it did was put a place name at the top of a censorship board: a term seen ONCE, with
novelty 1.0, outranking `censorship` seen eight times with novelty 0.38.

That is not a tuning nit. For a term seen once, "absent from the history band" is what you
would expect by chance from any rare term. It is not evidence the term became sensitive, and
granting it the same novelty as a twenty-sighting term states a confidence the data does not
carry.

THE CRITICAL PART IS THAT THIS IS OFF BY DEFAULT. recent_count is a sample size only on a
sampled stream. In blocklist archaeology a keyword shipped in version N+1 of a censorship
client is a dated directive — one sighting is a COMPLETE fact, a census rather than a thin
sample — so shrinking it would understate something known exactly. The CDT deletion stream
opts in; the surfaces where a single sighting is a whole fact stay untouched. Both halves
are tested here, because getting this backwards would silently restate every published
number on the board.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from processors.ddti_index import NOVELTY_EVIDENCE_K, compute_selectivity_novelty

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


def _obs(term, days_ago, i=0):
    return {"terms": [term], "detected_at": NOW - timedelta(days=days_ago),
            "title": f"{term} {i}", "url": f"https://example/{term}/{i}", "source": "cdt"}


def _ranked(obs, **kw):
    idx = compute_selectivity_novelty(obs, NOW, current_window_days=45,
                                      history_window_days=180, **kw)
    return {r["term"]: r for r in idx["ranked"]}


# ── the default must not have moved ──────────────────────────────────────────

def test_shrinkage_is_off_by_default():
    """A shared processor that changes what it claims for every consumer at once is how
    several bugs on this board happened. The default is the old behaviour, exactly."""
    assert NOVELTY_EVIDENCE_K == 0.0

    r = _ranked([_obs("单次词", 1)])
    assert r["单次词"]["is_new"] is True
    assert r["单次词"]["novelty"] == 1.0
    assert r["单次词"]["novelty_evidence"] == 1.0


def test_a_single_sighting_census_keeps_full_novelty():
    """The blocklist-archaeology contract: a keyword added in one version diff is a complete
    fact. Shrinking it would damp every term identically and claim less than we know."""
    r = _ranked([_obs("新疆抗议", 1)], novelty_evidence_k=0.0)
    assert r["新疆抗议"]["novelty"] == 1.0


# ── the opted-in sampled-stream behaviour ────────────────────────────────────

def test_evidence_scales_the_novelty_claim_not_the_fact():
    """is_new stays true — it is a fact about the history band. What shrinks is how much
    the ranking is willing to bet on it."""
    obs = [_obs("单次词", 1)] + [_obs("多次词", d, d) for d in (1, 2, 3, 4, 5, 6, 7, 8)]
    r = _ranked(obs, novelty_evidence_k=1.0)

    assert r["单次词"]["is_new"] is True and r["多次词"]["is_new"] is True
    assert r["单次词"]["novelty_raw"] == 1.0          # the claim is unchanged
    assert r["单次词"]["novelty"] == 0.5              # the weight behind it is not
    assert r["多次词"]["novelty"] > r["单次词"]["novelty"]


def test_shrinkage_penalises_thin_evidence_more_than_thick():
    """The invariant, stated without tuning: shrinkage costs a once-seen term proportionally
    more than a repeatedly-seen one. Everything else here is a consequence of this."""
    thin = [_obs("单次词", 1)]
    thick = [_obs("多次词", d, d) for d in (1, 2, 3, 4, 5, 6, 7, 8)]

    before = _ranked(thin + thick, novelty_evidence_k=0.0)
    after = _ranked(thin + thick, novelty_evidence_k=1.0)

    thin_kept = after["单次词"]["threat"] / before["单次词"]["threat"]
    thick_kept = after["多次词"]["threat"] / before["多次词"]["threat"]
    assert thin_kept < thick_kept, "thin evidence must lose more of its score than thick"
    assert thick_kept > 0.9, "a well-evidenced term is barely touched"


def test_the_singleton_stops_outranking_the_chronic_term():
    """The headline symptom itself, on data shaped like the reading that produced it.

    The losing term must carry real HISTORY — that is what damps its novelty to ~0.39 and
    lets a once-seen newcomer at novelty 1.0 edge past it. These figures track the live
    reading closely: there the newcomer scored 1.89 against `censorship` at 1.75, novelty
    0.38. The margin is genuinely thin in both, which is the point — the ordering was being
    decided by a confidence the single observation did not support."""
    chronic = ([_obs("审查", d, d) for d in (2, 4, 5, 7, 9, 11)]          # current window
               + [_obs("审查", 50 + 4 * i, 100 + i) for i in range(11)])   # history band
    obs = [_obs("长沙", 1)] + chronic

    before = _ranked(obs, novelty_evidence_k=0.0)
    after = _ranked(obs, novelty_evidence_k=1.0)

    assert before["审查"]["is_new"] is False and before["长沙"]["is_new"] is True
    assert 0.3 < before["审查"]["novelty"] < 0.5, "history damps the chronic term's novelty"
    assert before["长沙"]["threat"] > before["审查"]["threat"], "the symptom, before"
    assert after["审查"]["threat"] > after["长沙"]["threat"], "the fix"
    assert "长沙" in after, "the singleton must stay ranked, not be filtered out"


def test_shrinkage_applies_to_burst_terms_too():
    """A burst ratio computed from one sighting is as thin as a first-ever sighting, so
    both are shrunk the same way rather than special-casing is_new."""
    obs = [_obs("突发词", 1)] + [_obs("突发词", 100), _obs("突发词", 120, 2)]
    r = _ranked(obs, novelty_evidence_k=1.0)

    assert r["突发词"]["is_new"] is False              # it has history: a burst, not a debut
    assert r["突发词"]["novelty"] < r["突发词"]["novelty_raw"]
    assert r["突发词"]["novelty_evidence"] == 0.5      # one sighting in the current window


def test_evidence_curve_is_monotone_and_bounded():
    for k in (0.5, 1.0, 2.0):
        prev = -1.0
        for n in range(1, 12):
            obs = [_obs("词", 1 + i * 0.01, i) for i in range(n)]
            e = _ranked(obs, novelty_evidence_k=k)["词"]["novelty_evidence"]
            assert 0.0 < e < 1.0
            assert e > prev, f"evidence must rise with n (k={k}, n={n})"
            prev = e


def test_the_parameter_is_published_so_a_reading_is_reproducible():
    idx = compute_selectivity_novelty([_obs("词", 1)], NOW, novelty_evidence_k=1.0)
    assert idx["window"]["novelty_evidence_k"] == 1.0


def test_the_cdt_pull_opts_in():
    """The one surface where recent_count really is a sample size. If this ever silently
    reverts, the place-name headline comes back."""
    import inspect
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import ddti_live_pull

    src = inspect.getsource(ddti_live_pull.main)
    assert "novelty_evidence_k=1.0" in src
