# Investigative Data Plane v1

Status: implementation and deployment contract. Production rollout remains
blocked until the off-node bucket exists and the investigative analysis unit no
longer grants an application identity Docker-group access.

## Objective

Palimpsest is the acquisition and evidence plane shared by Seiche and
LiquiLens. It should retain large lawful corpora without treating every retained
byte as training material. The scarce asset is a reproducible, reviewed
decision with a later outcome; raw volume primarily supports retrieval,
timeline reconstruction, and contradiction discovery.

The first integration seam is `EvidenceDocument v1`. Collectors supply bytes
and source metadata. The contract content-addresses the exact bytes, preserves
event/publication/knowledge/acquisition clocks, applies an explicit rights
disposition, and creates deterministic point-in-time cuts. It performs no
network access.

## Storage tiers

### Hot landing and transforms

Use the mounted Hetzner Volume for downloads, decompression, parsing, OCR,
entity extraction, and Parquet compaction. A collector writes into a private
run directory, emits its receipt last, and never mutates an accepted evidence
object. Temporary and reproducible derivatives are deleted before evidence
when capacity is constrained.

### Immutable off-node evidence

Use a dedicated Hetzner Object Storage bucket created with Object Lock enabled;
that option cannot be retrofitted after bucket creation. Credentials are
runtime-only and scoped by responsibility. A collector or archiver may append
objects but may not change retention configuration. A verifier has read-only
access. Evaluation-only evidence uses a different bucket or credentials from
training-eligible material.

Tiny responses are packed into immutable WARC/tar shards with a per-member
digest index. Derived tables use Parquet with bounded row groups. Individual
large source PDFs and archives remain individual objects. The local object and
manifest hashes are the cross-tier identities; an object-store key or URL is a
location, not identity.

### Independent recovery

Object retention is not a backup by itself. Maintain a separately credentialed,
encrypted recovery copy and perform restore drills. A snapshot in the same
service or account is useful for rollback but does not satisfy independent
recovery.

## Evidence lifecycle

1. **Discover:** store source identity, canonical URL, collection policy, and
   rights disposition. Discovery does not imply body-text retention rights.
2. **Capture:** hash exact response bytes and headers; record source-native ID,
   response and acquisition clocks, collection run, and content type.
3. **Normalize:** emit language-preserving text, tables, entities, claims, and
   links as derivatives that cite the raw object hash and parser version.
4. **Review:** append inclusion, exclusion, quarantine, entity-merge, rights,
   contradiction, and editorial decisions. Never overwrite the capture.
5. **Label:** append later editorial or market outcomes with their own knowledge
   clocks. Pending is not negative.
6. **Seal:** create a manifest containing exact evidence/decision IDs, `as_of`,
   rights-policy version, entity-map version, parser/model versions, and shard
   hashes.

## Rights and temporal gates

The minimum text-use dispositions are `prohibited`, `metadata_only`,
`derived_only`, and `full_text`. Retention, quotation, redistribution,
evaluation, derived-feature training, and full-text training remain separate
policy questions. A later, knowable rights decision can revoke an earlier
permission without altering the source manifest. Conflicting terminal decisions
fail closed.

The default training cut requires both `knowledge_time <= as_of` and
`collected_at <= as_of`. A capture made today cannot prove that today's exact
bytes existed last year merely because the page contains an old publication
date. A separately reviewed archived snapshot may establish earlier version
availability, but that is a distinct evidence claim and policy.

Evaluation material is physically and logically isolated. Random train/test
splits are forbidden for investigative and market tasks. Use chronological,
entity/event-grouped splits with an embargo, then freeze evaluation manifests
before development.

## Source lanes

Start with high-identity sources and expand only when each lane has a rights
policy, completeness accounting, and a review owner:

- NFRA, PBOC, State Council, NBS and other official China notices/statistics;
- SEC EDGAR submissions/company facts, FRED/ALFRED vintages, and BIS bulk data;
- OONI bulk measurements under bounded country/test hypotheses;
- targeted Common Crawl WARC retrieval for already relevant domains, subject
  to the originating site's rights;
- reputable reporting and public corporate sources used for corroboration and
  contradiction, not indiscriminate duplication.

## Training boundary

The large evidence corpus powers retrieval, entity timelines, citation, and
historical reconstruction. It is not automatically a fine-tuning corpus.
Fine-tuning candidates are small, human-reviewed procedure traces: queries
attempted, evidence accepted/rejected, identity decisions, contradictions,
abstention reasons, and final disposition. These traces reference evidence IDs
rather than copying protected text.

Promotion requires an untouched evaluation set and deterministic metrics for
citation validity, temporal leakage, independent-source diversity,
contradiction recall, entity-link precision, coverage/abstention, and replay
reproducibility. Market probability work additionally reports calibration and
proper scoring against predeclared baselines. The product emits leads and
calibrated hypotheses, never guarantees of an unseen event.

## Production safety gate

Do not install the draft investigative systemd unit with
`SupplementaryGroups=docker`. On the current rootful-Docker host, that grants
general host authority. Install a reviewed, root-owned, no-argument launcher in
`/usr/local/libexec` instead. It may run only a digest-pinned, revision-labelled
image with fixed networkless/read-only arguments and bounded staging paths.
The application UID never receives the Docker socket. Build and verify the
image revision label, run the oneshot twice (second result must be unchanged),
then enable the timer.

## Rollout order

1. Land the evidence contract and security/adversarial tests.
2. Create the locked off-node bucket and recovery target; inject scoped
   credentials outside Git.
3. Mirror a small sealed corpus and verify object, manifest, and restore hashes.
4. Connect one official source lane (NFRA) and one prospective outcome lane
   (Seiche) through immutable IDs.
5. Freeze an evaluation set and run the retrieval-only investigative baseline.
6. Accumulate reviewed decisions and matured outcomes before any fine-tune.
