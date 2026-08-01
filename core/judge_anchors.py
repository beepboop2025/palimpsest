"""JUDGE ANCHORS — is it the model that changed, or our instrument?

> A longitudinal record whose labeller can move underneath it is measuring two things
> at once and reporting one. SpeechMap.ai migrated its LLM judge in March 2026 and
> roughly 5% of 569,000 historical labels flipped; the honest response to that class
> of problem is not to hope, it is to re-score a frozen set of texts with the current
> classifier on every single run and publish the agreement.

Our classifier is lexical rather than a model, which makes it auditable but not
immune: it is shared with the Generative Firewall index and the erasure observatory,
so editing one marker string moves three published surfaces at once. This module
re-scores `config/refusal_judge_anchors.json` every run and reports two different
things, which must never be conflated:

  agreement   — how often the classifier matches the AUTHOR-assigned label. This is
                an honest accuracy figure ON THIS SET and nothing more. The labels
                are not independently double-coded, so this does not establish that
                the classifier is right; that is the human coder study's job
                (validation/CODEBOOK.md), which remains open.
  fingerprint — the sha256 of the classifier's own labels across the frozen set. This
                is the drift detector, and it needs no notion of correctness at all:
                if the fingerprint moves, the instrument moved, and every rate
                published before and after that moment is on a different footing.

Known divergences are declared IN the anchor file, so a documented false positive
counts as expected behaviour rather than reappearing as noise on every run — and the
count of them is published, so the debt stays visible instead of being absorbed.

Pure stdlib. The classifier is injected, so this module never decides what a refusal
is and can be tested without importing the collector.
"""
from __future__ import annotations

import json
import os

from core.sealed_ledger import _canonical, _sha256

_DEFAULT_ANCHORS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "refusal_judge_anchors.json")


def load_anchors(path: str | None = None) -> list[dict]:
    with open(path or _DEFAULT_ANCHORS, encoding="utf-8") as f:
        anchors = json.load(f)["anchors"]
    if not anchors:
        raise ValueError("judge anchor set is empty — the classifier would go unwatched")
    seen = set()
    for a in anchors:
        if a["id"] in seen:
            raise ValueError(f"duplicate anchor id {a['id']!r}")
        seen.add(a["id"])
        if a["expect"] not in ("refused", "answered"):
            raise ValueError(f"anchor {a['id']}: expect must be refused|answered")
        if not a.get("text", "").strip():
            raise ValueError(f"anchor {a['id']}: empty text")
    return anchors


def score_anchors(is_refusal, anchors: list[dict]) -> dict:
    """Re-score the frozen set with the classifier as it ships today.

    `is_refusal` is any callable text -> bool. Returns a dict ready to publish:
    the label fingerprint (the drift signal), agreement with the author labels
    (an accuracy figure on this set only), and every disagreement named — split
    into the ones the anchor file already documents and the ones it does not.
    An UNDOCUMENTED disagreement is the actionable case: either the classifier
    changed or the anchor set is wrong, and both need a human.
    """
    labels, undocumented, documented = {}, [], []
    for a in anchors:
        got = "refused" if is_refusal(a["text"]) else "answered"
        labels[a["id"]] = got
        if got != a["expect"]:
            (documented if a.get("known_divergence") else undocumented).append(
                {"id": a["id"], "expected": a["expect"], "classifier_said": got,
                 "why": a.get("known_divergence", "")})
    n = len(anchors)
    agree = sum(1 for a in anchors if labels[a["id"]] == a["expect"])
    return {
        "n_anchors": n,
        "fingerprint": _sha256(_canonical(labels)),
        "agreement_with_author_labels": round(agree / n, 4),
        "n_agreeing": agree,
        "n_documented_divergences": len(documented),
        "n_undocumented_divergences": len(undocumented),
        "undocumented_divergences": undocumented,
        "documented_divergences": documented,
        "what_agreement_means": ("accuracy on this frozen set against author-assigned "
                                 "labels; NOT an independently coded validation, which "
                                 "is still open work"),
        "what_the_fingerprint_means": ("sha256 of the classifier's labels over the frozen "
                                       "set; a change means the instrument moved, so rates "
                                       "either side of it are not directly comparable"),
    }


def judge_drift(previous_fingerprint: str | None, current: dict) -> dict:
    """Compare this run's classifier fingerprint to the last published one.

    Three states, and the middle one is the whole point of the module:
      baseline  — nothing to compare against yet.
      stable    — same instrument, so a model-drift claim stands on its own.
      CHANGED   — the classifier moved. Model-drift numbers across this boundary are
                  confounded and must be re-baselined, not explained.
    """
    if not previous_fingerprint:
        return {"state": "baseline", "instrument_changed": False,
                "note": "first run under the anchored classifier; nothing to compare"}
    changed = previous_fingerprint != current["fingerprint"]
    return {
        "state": "changed" if changed else "stable",
        "instrument_changed": changed,
        "previous_fingerprint": previous_fingerprint,
        "note": ("the refusal classifier changed since the last run — drift across this "
                 "boundary is instrument movement until proven otherwise, and the series "
                 "is re-baselined rather than diffed"
                 if changed else
                 "same classifier as the last run, so model drift is not classifier drift"),
    }
