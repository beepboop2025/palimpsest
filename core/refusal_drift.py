"""REFUSAL DRIFT — undisclosed behavioral change in a frontier model over time.

> The censor that matters most for the long run may not be a state. As a single model
> mediates more of what people can ask and learn, a quiet change in what it will answer,
> shipped with no changelog, is an erasure of the knowable. This measures it: run the same
> frozen probe set against the same model at two points in time, and report what flipped
> from answered to refused. New refusals are the deletion events.

Model- and provider-agnostic. The probe set is deliberately benign and informational (the
point is to measure OVER-refusal drift on legitimate questions, never to extract harmful
content). Classification is lexical and auditable (reuses the Generative Firewall's
``is_refusal``). Each run is sealed into the Verifiable Eval Registry, so the longitudinal
record cannot be quietly revised and a "we never changed the model" claim is checkable.

``diff_runs`` is pure and offline-testable: it takes two {probe -> label} maps and returns
the drift. Drift needs two observations across time, so the first run is an honest baseline
(drift = null) and drift is reported from the second run onward.
"""
from __future__ import annotations

ANSWERED = "answered"
REFUSED = "refused"


def label_for(is_refusal_result: bool) -> str:
    return REFUSED if is_refusal_result else ANSWERED


def diff_runs(prev: dict, cur: dict) -> dict:
    """Compare two runs on the same probe set. Values are labels (answered/refused).

    Returns the drift: which probes newly became refused (the erasure signal), which newly
    became answered (a loosening), and the stable sets. Only probes present in BOTH runs are
    compared; probes unique to one run are reported separately, never silently counted as a
    flip (fail loud).
    """
    shared = sorted(set(prev) & set(cur))
    new_refusals = [p for p in shared if prev[p] == ANSWERED and cur[p] == REFUSED]
    new_answers = [p for p in shared if prev[p] == REFUSED and cur[p] == ANSWERED]
    stable_refused = [p for p in shared if prev[p] == REFUSED and cur[p] == REFUSED]
    stable_answered = [p for p in shared if prev[p] == ANSWERED and cur[p] == ANSWERED]
    only_prev = sorted(set(prev) - set(cur))
    only_cur = sorted(set(cur) - set(prev))
    n = len(shared)
    return {
        "n_compared": n,
        "new_refusals": new_refusals,          # answered -> refused (the deletion events)
        "new_answers": new_answers,            # refused -> answered (a loosening)
        "stable_refused": stable_refused,
        "stable_answered": stable_answered,
        "only_in_prev": only_prev,
        "only_in_cur": only_cur,
        "drift_rate_pct": round(100.0 * len(new_refusals) / n, 1) if n else None,
        "net_refusal_change": len(new_refusals) - len(new_answers),
    }
