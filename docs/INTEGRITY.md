# Integrity architecture and threat model

Palimpsest's central claim is that its published record cannot be revised
after the fact. This document says exactly what enforces that, layer by
layer, who each layer defends against, and what none of them can do. A trust
claim without a threat model is marketing; this is the threat model.

## The layers

| # | Layer | What it proves | Who has to be defeated to fake it |
|---|-------|----------------|-----------------------------------|
| 1 | Hash chain (`core/sealed_ledger.py`, `core/eval_registry.py`) | No entry was altered, reordered, or dropped *within* the file as served. Every entry commits to its predecessor; the registry additionally rejects any run whose probe set was not frozen earlier in the chain. | Nobody. Anyone who holds the file can recompute it offline, stdlib only. |
| 2 | Merkle root + inclusion proofs (`scripts/prove_inclusion.py`) | One 64-char value fingerprints the whole record; any single attestation can be checked against it with log2(N) hashes. | Same as layer 1, but a verifier no longer needs the whole chain. |
| 3 | Public git history | Every refresh is a timestamped commit on a public repository. Force-pushes and branch deletion on main are blocked by an active repository ruleset with no bypass actors ("history-can-only-grow"), so rewriting served history first requires visibly changing the rules; and any rewrite would still be visible to anyone with a clone, a fork, or a fetched ref. | GitHub, plus everyone who ever cloned or forked. |
| 4 | Internet Archive snapshots (`scripts/anchor_roots.py`) | A dated third-party copy of the exact chain bytes, held by a library outside our infrastructure and jurisdiction. | The Internet Archive. |
| 5 | OpenTimestamps / Bitcoin (`scripts/anchor_roots.py`) | The Merkle roots existed no later than a Bitcoin block time. The `.ots` proofs verify with the standard client against the blockchain, not against us. | Bitcoin's proof-of-work. |
| 6 | Independent witness (`ops/witness/`) | A from-scratch reimplementation on separate infrastructure re-verifies the served chains and checks that every previously witnessed head is still present, unchanged. Detects split views (serving different histories to different people) and retroactive rewrites, and alerts. | Every running witness, simultaneously and retroactively. |

Layers 1 and 2 are self-verification: strong against post-hoc editing, worth
nothing against an operator who rewrites the entire file and re-serves it.
Layers 3 to 6 exist for exactly that adversary — including us. If we edited a
published number, our own verifier would report the break, the anchors would
date the old root, and any witness would name the rewritten entry.

## What this does NOT protect against — honestly

- **Lying at capture time.** The chain proves what was sealed, not that the
  sealed reading was true. If the collector recorded a false response, the
  chain faithfully preserves the falsehood. Mitigations live upstream:
  runs commit a `responses_hash` over the full raw responses so a re-run can
  be compared; probes are pre-registered so results cannot be cherry-picked
  after the answers exist; the classifier is a transparent lexical rule under
  human validation (see the coder study), not an unauditable model.
- **The window between seal and first anchor.** History could in principle be
  rewritten in the gap before any external party has seen it, at most one
  anchor cadence (currently 6 hours) after sealing. Older history is
  progressively harder to touch: it is held by the Archive, by Bitcoin, and
  by every witness log.
- **Suppression by omission.** We could simply not seal an embarrassing
  reading. The schedule is public (GitHub Actions cron) and gaps in the
  cadence are themselves visible in the history files, but a sufficiently
  careful omission is not mechanically detectable. This is why the pipelines
  are open source and the collectors abstain loudly instead of skipping
  silently.
- **Endpoint compromise.** If the publishing key or CI is compromised, an
  attacker can append false *new* entries (they still cannot rewrite old
  ones without tripping layers 3 to 6). Signed attestations are the planned
  next layer; see below.

## Verify it yourself

```bash
git clone https://github.com/beepboop2025/palimpsest && cd palimpsest
python3 scripts/verify_eval_registry.py     # chain + pre-registration rule
python3 scripts/verify_ledger.py            # the erasure ledger
python3 scripts/prove_inclusion.py 5        # inclusion proof for one attestation
ots verify readings/anchors/*.ots           # Bitcoin timestamp on the roots
python3 ops/witness/palimpsest_witness.py   # become a witness yourself
```

## Planned hardening

- **Ed25519-signed attestations** so a CI compromise cannot mint entries that
  pass as ours (adds a key, so it lands together with a documented key
  ceremony and rotation story rather than quietly).
- **More witnesses.** The witness is deliberately trivial to run; each
  independent copy shrinks the rewrite window. If you run one, tell us.
- **Cross-registry anchoring** with peer projects, so records vouch for each
  other's roots.
