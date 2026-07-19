# The Information Erasure Observatory

**One-line thesis:** the censorship-measurement field answers *"can you reach X
right now?"* Palimpsest answers *"what did the record lose over time, and can you
prove our measurement of the loss was not itself edited afterward?"*

This is a positioning and integrity layer over signals Palimpsest already runs.
It re-measures nothing. It (1) frames three existing surfaces as one phenomenon —
erasure — (2) publishes a single composite **Erasure Index**, and (3) adds the one
thing no access-index competitor has: a **tamper-evident sealed ledger** that makes
our own record un-rewritable-in-secret.

## Why this exists (the competitive read, July 2026)

A near-identical open censorship observatory now exists (a solo-run project that
synthesizes OONI + Censored Planet + IODA + Citizen Lab into a live country-ranked
*access* index). Competing on "another live access dashboard" is a breadth race
against an incumbent with a head start. So we do not compete there.

Instead we move to an axis the whole field leaves open. Access is a present-tense
snapshot; the published research on **erasure over time** lives in three separate
communities that nobody has combined into one live, sealed observatory:

| Layer | What it measures | Instrument (already in-repo) | Literature |
|---|---|---|---|
| **Network** | reachability that disappeared | `ooni_gfw.py`, `censored_planet.py` | survey arXiv:2502.14945 |
| **Narrative** | encyclopedia entries rewritten to the state line | `baike_redaction.py` | Ruwiki fork study arXiv:2504.10663 |
| **Model** | answers a state-aligned model will no longer give | `generative_firewall.py` | forbidden-topic discovery arXiv:2505.17441; DeepSeek suppression arXiv:2506.12349 |

The integrity backbone is grounded in the trusted-public-archive literature
(ARCHANGEL, arXiv:1804.08342; tamper-evident logging, arXiv:2509.03821).

## What was added

- **`core/sealed_ledger.py`** — pure-stdlib, offline-verifiable hash-chained
  append-only ledger. Each source reading is anchored by the sha256 of its
  canonicalized full body; entries are linked by `prev_hash`; a Merkle root gives
  a one-value fingerprint of the whole chain. `verify()` reports *every* break
  (bad link, bad hash, reorder, drop) rather than a silent boolean.
- **`scripts/erasure_pull.py`** — the unifying runner. Reads each surface's latest
  reading, seals it, computes the composite index (mean of the layers that
  actually reported — absent layers are shown absent, never zero-filled), and
  publishes `readings/erasure-observatory-latest.json` + history.
- **`scripts/verify_ledger.py`** — the "you can check us" tool. Exit 0 = intact,
  1 = tampered. Anyone who cloned the repo can run it against any past commit.
- **`readings/erasure-observatory.html`** — the public page. Composite index, the
  three layers, the cross-checks, and the integrity panel with the live Merkle
  root and the verify command. Fully client-side, XSS-safe (all external strings
  escaped, numbers coerced).
- **`tests/test_sealed_ledger.py`** — proves tamper detection offline (payload
  edit, reorder, drop, idempotent-skip, Merkle commitment). 6/6.
- **`.github/workflows/erasure-refresh.yml`** — 6-hourly; verifies the ledger
  *before* sealing (fail-loud on any prior tamper), seals, commits if changed.

## Why the sealed ledger is the moat

Anyone can publish a live number, so the number alone proves nothing. When the
subject under measurement is *the retroactive rewriting of the record*, the first
attack on the observatory is "you fabricated the before-state / you changed your
own history." No censorship observatory in the field answers that. The sealed
ledger does: publication of the JSONL into the public repo is the anchoring, and a
third party who cloned the repo yesterday holds a witness to yesterday's chain
head. It is the one differentiator that is both technically real and impossible to
hand-wave: un-censorable public infrastructure.

## Status and honesty notes

- **Live now:** network + model layers report real numbers; the composite is
  their mean. First sealed ledger entries are committed with this change.
- **Narrative layer is ARMED, not yet reporting:** `baike_redaction.py` is built
  but its first sealed reading needs the outside-the-wall egress seam
  (`PALIMPSEST_PROXY`), since the GFW blocks Wikipedia. Until then the page shows
  it as ARMED with the reason, never a fabricated value.
- **False positives are the enemy.** Content changes for boring reasons (redesigns,
  quality retraining). Every layer must stay conservative and fail-loud; a null
  result is a reportable result.
- **Cite, don't erase, the neighbours.** In any grant text, name the access
  observatories and the LLM-audit work (AI Watchman, the Pan-lineage cross-national
  audit) as related work. The novelty is the *erasure axis + tamper-evident
  sealing*, not "first observatory." Overclaiming is the fastest way to lose a
  reviewer who knows the field.

## The paper hook

The combination is a methods contribution across three communities:
*"A Tamper-Evident Observatory of Information Erasure."* It feeds the arXiv track
and the Jennifer Pan endorsement (the Ruwiki method already uses bootstrapped
confidence intervals, which answers her k-sample uncertainty-band request), which
in turn feed the grant pipeline.
