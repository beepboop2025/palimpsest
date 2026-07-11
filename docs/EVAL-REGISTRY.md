# The Verifiable Eval Registry

**One line:** open infrastructure for tamper-evident, pre-registered AI model
evaluations. The questions are frozen before the model is queried, and every
result is hash-chained so it cannot be quietly re-run, cherry-picked, or revised.
Any model, any suite. Evals you can prove were not gamed.

This is the AI-safety-shaped generalization of Palimpsest's sealed ledger. Where
`core/sealed_ledger.py` seals our own erasure readings, `core/eval_registry.py`
seals *anyone's* model evaluation, and adds one rule the plain ledger does not
have: a result must reference a probe set that was pre-registered earlier in the
chain. A run whose questions were never frozen first fails verification.

## The problem it addresses

AI evaluation has a trust gap. Labs grade their own homework; third-party audits
are hard to reproduce; and an eval can be silently re-run until the number is
flattering, or revised after publication with no trace. As models become the
primary interface to human knowledge, the ability to audit them, and to *prove the
audit was not rewritten*, is missing infrastructure. This is a governance problem,
not only a censorship one.

## What it guarantees

- **Pre-registration.** A probe set is hashed (order-independent) and sealed as a
  `preregistration` attestation before any model is queried. This is the
  anti-p-hacking property: you cannot have chosen the questions to fit the answers.
- **Tamper-evidence.** Every attestation is hash-chained to the previous one, and a
  Merkle root fingerprints the whole registry in one value. Alter a metric, a
  responses hash, a timestamp, or the order, and the chain fails to recompute.
- **Independent verification.** `verify()` reports every break, including a run
  whose questions were never frozen first. Anyone who clones the repo can run
  `scripts/verify_eval_registry.py` against any past commit. Exit 0 = intact.
- **Reproducibility.** A run commits a `responses_hash` over the full results;
  publish the raw responses alongside and anyone can recompute the hash to confirm
  the record matches what the model actually said.

## What is anchored in it now

Two real audits, sealed and pre-registered:

1. **Chinese-aligned model suppression** (the Generative Firewall): a 10-concept
   sensitive probe set, one sealed run per model (DeepSeek, Qwen), ~40% suppression.
2. **Frontier refusal drift** (`scripts/refusal_drift_pull.py`): a benign,
   informational 12-probe set run against a Western frontier model (default
   openai/gpt-4o-mini) with a real, live result — 0% refused at baseline, including
   the Tiananmen question the aligned models declined. Each run is sealed; drift is
   the diff versus the last run, so a frontier model that *quietly* stops answering
   a question later is caught and cannot be un-recorded.

The contrast is the point: the same tamper-evident, pre-registered machinery audits
a state-aligned model and a Western frontier model side by side, and will surface an
undisclosed behavioral change in either. This is what makes it universal AI-
transparency infrastructure rather than a regional censorship tool. `core/refusal_drift.py`
computes the drift (answered -> refused = the erasure events) and is pure and offline-tested.

## Why this reduces long-term risk (the mechanism)

As AI systems mediate more of what humanity can know, the capacity to silently
shape or withhold answers, unprovably, is a durable degradation of the shared
epistemic environment and a path toward value and information lock-in. Detecting
undisclosed behavioral change in frontier models, and recording it in a way that
cannot be retroactively edited, is a building block for AI transparency, for
model-release accountability, and for defending the epistemic commons against
AI-mediated manipulation. The registry is that building block: verifiable evidence
that a specific model behaved a specific way at a specific time, that no lab or
auditor can later quietly revise.

## Honest scope

- This is infrastructure, not a benchmark. Its value is the guarantee, not any one
  number. The suppression rates shown are from an existing small audit; the point
  is that they are sealed and pre-registered, not that they are comprehensive.
- Interoperability with the wider eval ecosystem (independent audit teams) is a
  design goal; anchoring an external audit into the chain is a few lines.
- Overclaiming to a technical reviewer is fatal. The correct claim is precise:
  a tamper-evident, pre-registration-enforcing record for model evaluations, with a
  real model-erasure audit as its first content.

## Files

- `core/eval_registry.py` — the registry (preregister, submit_run, verify, summary).
- `scripts/eval_registry_ingest.py` — records the Generative Firewall eval as sealed,
  pre-registered attestations. Idempotent.
- `scripts/verify_eval_registry.py` — the public verification tool.
- `readings/eval-registry.html` — the public page.
- `readings/eval-registry.jsonl` — the chain. `eval-registry-latest.json` — the summary.
- `tests/test_eval_registry.py` — tamper detection and the pre-registration rule (6/6).
