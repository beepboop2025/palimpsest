"""VERIFY THE TRANSCRIPTS AGAINST THE CHAIN — the audit v1 could not offer.

The Verifiable Eval Registry proves a sealed run was never altered. It has never been
able to prove the sealed number was RIGHT, because what it sealed was a derived label
and the raw response was not published: INTEGRITY.md has to concede that nobody,
including us, could recompute a label from the response it came from.

This tool closes that. Given the published transcripts and the chain, it checks three
things, in increasing order of how much it lets you distrust us:

  1. INTEGRITY — the chain verifies and the pre-registration rule holds. (What v1 had.)
  2. COMMITMENT — the digest of {arm: sha256(response text)} over the published
     transcripts equals the `responses_hash` in the sealed run. So the text you are
     reading is the text we sealed, not a later edit.
  3. LABELS — re-derive every label from the published text with the shipping
     classifier and compare against the run's own published labels. Any disagreement
     is printed with the response, so you can read it and form your own view.

Step 3 uses OUR classifier, so it detects a broken pipeline rather than proving the
labels are correct. That is the honest limit and it is stated in the output: if you
think the classifier is wrong, the transcripts are right here, and disagreeing with us
now costs you a text editor rather than an API key.

Exit 0 = all three pass. Exit 1 = a mismatch. Exit 2 = nothing to verify.
Stdlib only, no network.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from collectors.generative_firewall import is_refusal  # noqa: E402
from core import eval_registry as reg  # noqa: E402
from core.sealed_ledger import _sha256  # noqa: E402

READINGS = os.path.join(ROOT, "readings")
REGISTRY = os.path.join(READINGS, "eval-registry.jsonl")
TRANSCRIPTS = os.path.join(READINGS, "refusal-drift-transcripts.json")
READING = os.path.join(READINGS, "refusal-drift-latest.json")


def main() -> int:
    for path in (TRANSCRIPTS, READING):
        if not os.path.exists(path):
            print(f"nothing to verify: {os.path.relpath(path, ROOT)} is not present")
            return 2
    with open(TRANSCRIPTS, encoding="utf-8") as f:
        tr = json.load(f)
    with open(READING, encoding="utf-8") as f:
        reading = json.load(f)

    entries = reg.read_ledger(REGISTRY)
    ok, problems = reg.verify(entries)
    print(f"1. chain integrity ....... {'INTACT' if ok else 'BROKEN'}"
          + ("" if ok else f" — {len(problems)} problem(s)"))
    for p in problems:
        print(f"     {p}")
    failures = 0 if ok else 1

    commitment = tr.get("probe_commitment")
    if commitment != reading.get("probe_commitment"):
        print("2. transcript commitment . MISMATCHED — the transcripts and the reading "
              "describe different probe sets; they are not from the same run")
        return 1

    # The prompts are published too, so the PRE-REGISTRATION is checkable and not just
    # the responses. Without this, "the questions were frozen before the answers
    # existed" rests on our word: the transcripts could carry a reworded prompt beside
    # a genuine response hash and nothing would notice.
    prompts = tr.get("prompts") or {}
    if prompts:
        recomputed = reg.probe_set_hash(
            sorted(f"{pid}\t{_sha256(t.encode('utf-8'))}" for pid, t in prompts.items()))
        if recomputed == commitment:
            print(f"1b. pre-registration ..... MATCHES — the {len(prompts)} published "
                  "prompts hash to the pre-registered probe commitment")
        else:
            print("1b. pre-registration ..... MISMATCH — the published prompts do not "
                  "hash to the commitment this run was sealed under")
            print(f"     sealed under {commitment}")
            print(f"     prompts give {recomputed}")
            failures += 1
        frozen = any(e.get("kind") == reg.PREREGISTRATION
                     and e.get("probe_set_hash") == commitment for e in entries)
        if not frozen:
            print("1b. pre-registration ..... MISSING — no pre-registration in the chain "
                  "for this commitment; the questions were never frozen")
            failures += 1

    # The published transcripts must reproduce the responses_hash of the newest sealed
    # run per model, for this probe commitment.
    #
    # ITERATE THE SEALS, NOT THE TRANSCRIPTS. Walking the published file and asking the
    # chain about each entry only ever proves that what was published is genuine; it
    # cannot notice what is ABSENT. Delete one model's responses wholesale — the
    # embarrassing model, say — and a transcript-driven check passes with a smaller
    # `checked` count and no complaint. The seals are the authority on what should be
    # there, so the loop is driven by them and a missing model is a failure.
    #
    # `entries` is append-only and in chain order, so the last matching run per model is
    # the newest one. That is the run the current transcripts must correspond to.
    runs = [e for e in entries
            if e.get("kind") == reg.RUN and e.get("probe_set_hash") == commitment]
    latest = {}
    for e in runs:
        latest[e.get("model")] = e
    published = tr.get("responses", {})
    if not latest:
        print("2. transcript commitment . nothing to check — no sealed run at this "
              "commitment yet")
        # A broken chain detected in step 1 is still a failure. "Nothing further to
        # check" must never launder an integrity break into a clean-ish exit 2.
        return 1 if failures else 2
    checked = mismatched = 0
    for model, sealed_run in sorted(latest.items()):
        checked += 1
        texts = published.get(model)
        if texts is None:
            print(f"2. transcript commitment . MISSING — {model} has a sealed run at "
                  "this commitment but no published responses")
            mismatched += 1
            continue
        digests = {pid: _sha256(t.encode("utf-8")) for pid, t in texts.items()}
        computed = reg.responses_hash(digests)
        sealed = sealed_run.get("responses_hash")
        if computed != sealed:
            print(f"2. transcript commitment . MISMATCH for {model}")
            print(f"     sealed   {sealed}")
            print(f"     computed {computed}")
            # Name the shape of the difference, since "the hash moved" is useless to
            # someone trying to work out whether an arm was dropped or a text edited.
            n_sealed = sealed_run.get("metrics", {}).get("n_arms")
            if isinstance(n_sealed, int) and n_sealed != len(texts):
                print(f"     the seal covers {n_sealed} arms, the transcripts carry "
                      f"{len(texts)} — arms were added or dropped")
            mismatched += 1
    extra = sorted(set(published) - set(latest))
    if extra:
        print(f"2. transcript commitment . EXTRA — {', '.join(extra)} appear in the "
              "transcripts with no sealed run at this commitment")
        mismatched += 1
    if not mismatched:
        print(f"2. transcript commitment . MATCHES for all {checked} sealed model(s) — "
              "the published text is the text that was sealed, and nothing is missing")
    failures += bool(mismatched)

    # Re-derive every label from the published text.
    published = {m["model"]: m.get("labels", {}) for m in reading.get("models", [])}
    disagreements = []
    total = 0
    for model, texts in sorted(tr.get("responses", {}).items()):
        for pid, text in sorted(texts.items()):
            want = published.get(model, {}).get(pid)
            if want is None:
                continue
            total += 1
            got = "refused" if is_refusal(text) else "answered"
            if got != want:
                disagreements.append((model, pid, want, got, text))
    if not total:
        print("3. label recomputation ... nothing to check — no labels published")
    elif not disagreements:
        print(f"3. label recomputation ... ALL {total} labels re-derive from the "
              "published text with the shipping classifier")
    else:
        print(f"3. label recomputation ... {len(disagreements)}/{total} DISAGREE:")
        for model, pid, want, got, text in disagreements:
            print(f"     {model} {pid}: published {want!r}, recomputed {got!r}")
            print(f"       {text[:200]!r}")
        failures += 1

    print()
    print("What this does and does not establish: steps 1 and 2 are cryptographic and "
          "need no trust in us. Step 3 re-runs OUR classifier, so it proves the "
          "pipeline is consistent, not that the labels are correct. The transcripts "
          "above are the evidence for that separate question, and they are published "
          "precisely so you can disagree with us on the record.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
