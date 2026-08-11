# EvidenceDocument v1

EvidenceDocument v1 is a private ingestion and training boundary for exact source
bytes. A producer supplies bytes it has already collected plus bounded metadata.
The implementation performs no network request, never executes content, and
never creates a mutable `latest` pointer.

The normative shapes are in
[`evidence-document-v1.schema.json`](evidence-document-v1.schema.json). Runtime
cross-field, hash, URL, rights, filesystem, and clock invariants described here
are also normative; JSON Schema cannot express all of them.

## Identities and canonicalization

All SHA-256 values are 64 lowercase hexadecimal characters.

- Content identity is SHA-256 of the exact caller-supplied bytes.
- Capture-request identity is SHA-256 of canonical caller metadata plus the
  computed content digest and byte size.
- Acceptance-receipt identity is SHA-256 of the canonical receipt, which embeds
  the complete capture request and the store-issued `accepted_at` time.
- Manifest identity is SHA-256 of the canonical manifest bytes.
- Rights-decision identity is SHA-256 of the canonical decision body.
- A trusted rights-ledger head is SHA-256 of the complete canonical supplied
  ledger, including decisions not yet visible at the cut time.
- Training-cut identity is SHA-256 of the complete canonical cut bytes.

None of those hashed objects contains its own identity. A rights-ledger entry
places `decision_sha256` beside its decision body and the runtime recomputes it.
A manifest similarly carries the identities of its capture request and receipt.

V1 uses `palimpsest-json-sorted-utf8-v1`: object keys sorted by Unicode code
point, separators `,` and `:` without whitespace, literal UTF-8, and integer-only
I-JSON numbers. Floats, NaN, infinities, duplicate keys, lone surrogates, and
non-JSON values are invalid. This is the same named implementation used by
Evidence Capsule v1; it is not a claim of RFC 8785 conformance.

## Private POSIX store

The caller supplies an absolute, non-filesystem-root path with no lexical `..`.
Every existing component is inspected without following symlinks. The store root
and every store-owned directory must be owned by the effective UID with mode
`0700`; committed and staged files must be owned by that UID with mode `0600`.
Finalized files must have exactly one hard link. Existing roots with broader
modes or different ownership fail closed rather than being silently changed.

Every existing ancestor of the store root must be owned by either the effective
UID or root. An ancestor writable by group or other is accepted only when it has
the sticky bit (for example a root-owned `/tmp`). This prevents an untrusted UID
from replacing the caller-owned child entry during path creation or reopening.

V1 deliberately requires a POSIX filesystem supporting same-filesystem hard
links, `flock`, `dir_fd` operations, file `fsync`, and directory `fsync`.
Unsupported link semantics raise `HardLinkUnsupportedError`; failed durability
barriers raise `DurabilityError`. Permission failures are store-safety failures,
not misreported as missing link support.

```text
<store>/
  .staging/
    .recovery.lock
    .intent-content-<content-sha256>-<payload-sha256>.tmp
    .intent-receipt-<capture-request-sha256>-<receipt-sha256>.tmp
    .intent-manifest-<manifest-sha256>-<payload-sha256>.tmp
  objects/sha256/ab/<content-sha256>.bin
  receipts/capture-sha256/ab/<capture-request-sha256>/<receipt-sha256>.json
  manifests/sha256/cd/<manifest-sha256>.json
```

`.staging` is outside all accepted-object trees and is never enumerated as
evidence. Each intent name binds the object purpose, exact destination identity,
and payload digest. A killed process can therefore leave a verifiable recovery
alias without turning it into accepted evidence.

`.recovery.lock` is the store-wide POSIX `flock` transaction barrier. Ingestion
and recovery hold it exclusively. Manifest/content reads and training-cut builds
hold it shared from enumeration through validation, content reads, and cut
construction. A reader therefore cannot observe a cooperating writer halfway
through its transaction. The lock is advisory: all processes granted store
access must use this implementation or the same locking protocol.

Recovery bounds both entries inspected and actions performed (each at most
1,024) per call. A process-local directory cursor resumes where the prior call
stopped; exhaustion closes it, and process restart safely begins a new pass.
Repeated calls on one recovery worker therefore traverse a larger or mostly
fresh backlog without repeatedly scanning its first ineligible entries. Exact
two-link recovery uses the deterministic intent name directly. Other intent
lookups have the same 1,024-entry ceiling and fail closed with an instruction to
run batched recovery rather than scanning without limit.

There is no source-name index, version counter, or latest link/file. A source's
versions are its immutable manifests. The root is a caller-owned same-user trust
boundary; a hostile process running as the same effective UID remains inside
that boundary. Do not grant untrusted same-UID writers access to the store or its
parents.

## Manifest, request, receipt, and clocks

A manifest contains exactly:

| Field | Meaning |
|---|---|
| `spec_version` | Literal `palimpsest-evidence-document/v1`. |
| `canonicalization` | Literal `palimpsest-json-sorted-utf8-v1`. |
| `source.id` | Stable source identity; never used as a path. |
| `source.canonical_url` | Inert absolute HTTP(S) provenance URL; never fetched. |
| `media_type` | Lowercase, parameter-free type/subtype. |
| `language` | Lowercase BCP-47-style tag or explicit `und`. |
| `event_time` | Event instant, or `null`; may be a scheduled future event. |
| `publication_time` | Source-version publication instant, or `null`. |
| `knowledge_time` | Earliest instant this version could be known from the cited source. |
| `collected_at` | Instant these exact bytes entered the caller's collection. |
| `acceptance.accepted_at` | Trusted store receipt time for this exact capture request. |
| `acceptance.capture_request_sha256` | Hash of canonical metadata plus computed content identity/size. |
| `acceptance.receipt_sha256` | Hash of the immutable receipt containing the request and `accepted_at`. |
| `collection.run_id` | Stable collection-run identity. |
| `collection.parent_feed_sha256` | Exact parent-feed hash or `null`. |
| `retention_class` | Bounded operational retention class; never training permission. |
| `rights` | Collection-time rights provenance; it cannot authorize a training cut. |
| `content` | Exact SHA-256 and byte size computed by ingest. |

Required availability order:

```text
publication_time <= knowledge_time <= collected_at <= acceptance.accepted_at
```

`event_time` is independent. A notice may describe a past event or announce a
future scheduled event. Event time is never an availability cutoff.
`knowledge_time` and `collected_at` remain caller/source metadata; `accepted_at`
is the trusted local receipt clock. All timestamps are real UTC instants with
whole-second `Z` precision.

Canonical URLs use lowercase schemes and authorities, omit credentials,
fragments, empty/default ports, empty query delimiters, and dot segments, and use
canonical decimal ports without leading zeros and two uppercase hexadecimal
digits for every percent escape. DNS or canonical unscoped IPv6 hosts are
required. The schema supplies a conservative lexical pattern; runtime performs
the complete semantic checks listed here.

### Create-once acceptance receipt

The store constructs a canonical capture request from validated caller metadata
and the digest and size computed from the supplied bytes. It creates one
immutable receipt containing that complete request, its hash, and a trusted
injected-or-system `accepted_at` time. Receipt files are content addressed by
their own hash beneath the capture-request hash, and exactly one committed
receipt is permitted for a request. Two receipt identities under one request are
a collision and fail closed.

The receipt clock must not precede `collected_at`. A new clock sample must not
move backward from the greatest time in either a committed receipt or a valid,
fsynced pending receipt intent. A first attempt that crashes after sealing a
receipt can therefore leave a digest-bound intent which an exact retry reuses.
That sealed time is not compared retroactively with receipts sampled later: an
unrelated later ingest cannot strand the earlier exact request. Once committed,
every exact repeat reuses the same receipt without sampling the clock again.
Changing any metadata or bytes creates a different capture request and preserves
a distinct source version.

## Immutable rights decisions and trusted head

Manifest rights preserve what the collector believed at ingest. They are not an
evergreen authorization: source terms can later change or be revoked. Every cut
therefore requires both an explicit caller-supplied complete rights ledger and a
separate trusted SHA-256 head for those complete canonical ledger bytes. Runtime
recomputes and compares the head before reading the store. Omission or a
truncated/mismatched ledger fails closed. An explicit empty ledger is valid only
when its exact hash is trusted, and it authorizes no content.

In production the trusted head must arrive out of band from a persisted, signed,
transparency-logged, or otherwise anchored rights-control plane. Merely hashing
an untrusted ledger beside the cut call does not prove completeness.

A decision has:

- canonicalization and version literals;
- subject `(source_id, content_sha256)`, identical to the training dedupe key;
- `decision_type`: `policy_set` or explicit `revocation`;
- one fail-closed `training_use`: `prohibited`, `metadata_only`, `derived_only`,
  or `full_text`;
- `effective_at`, when the decision becomes operational;
- `knowledge_time`, when the decision became knowable to the cut producer;
- licence/terms reference and bounded reason;
- sorted unique decision hashes in `supersedes`.

A `revocation` must set `training_use` to `prohibited`. A supersession must
reference a decision present in the complete supplied ledger, target the same
subject, not move knowledge time backwards, and the graph must be acyclic.

For cut time `as_of`:

1. A decision is visible when `knowledge_time <= as_of`.
2. Every visible decision, including a known future-effective decision, is
   embedded in the cut's `rights_ledger` and therefore its identity.
3. A visible decision is active when `effective_at <= as_of`.
4. An active superseding decision retires the active decisions it names.
5. Exactly one unsuperseded active terminal authorizes the subject.
6. No terminal excludes the subject. More than one terminal raises
   `RightsConflictError`; timestamps never guess between independent decisions.

Future-knowledge decisions are omitted from the historical projection. Adding
one to the complete ledger cannot rewrite an earlier cut identity. A later-known
but retroactively effective revocation changes cuts only once its knowledge time
arrives. The evolving complete trusted head is deliberately not embedded in a
historical cut; complete visible decisions and the applied terminal decision
identity are embedded and hashed.

## Ingestion and crash durability

Ingestion validates all bytes and metadata before filesystem mutation, creates
or validates the private layout, and then holds the exclusive store barrier for
the complete receipt/content/manifest transaction:

1. Required private directories are created with `fsync` barriers for each new
   directory and its parent.
2. The create-once acceptance receipt is found or created; collisions and a
   backward trusted clock are rejected.
3. A purpose/destination/digest-bound intent is written beneath `.staging`, mode
   `0600` is applied before the file `fsync`, and `.staging` is `fsync`ed.
4. Stable staging and destination directory descriptors are opened without
   following symlinks and verified to reside on one filesystem.
5. The fully written inode is atomically hard-linked to its final create-only
   content-addressed name.
6. Both names are verified as the same private regular inode with the expected
   bytes, owner, mode, purpose, destination, and link count two.
7. The destination directory is `fsync`ed while the recovery alias still exists.
8. Only that verified alias is unlinked through its anchored descriptor;
   `.staging` is `fsync`ed and the destination is verified at link count one.
9. The operation is repeated for the receipt, content, and finally manifest.

A manifest is accepted only after its destination directory has been durably
synced and its bound recovery alias has been durably removed. A crash before the
hard link leaves a one-link intent. A crash after the link but before alias
removal leaves exactly two names for the same inode. Training enumeration
recognizes but excludes that state. Recovery or an exact retry verifies the
intent binding, both inode identities, bytes, owner, mode, and link count, then
replays both directory barriers and removes only the verified alias. Any
mismatch fails closed. A failure between content and manifest commits may leave
an immutable unreferenced receipt or content object; neither is accepted
evidence.

`recover_staging()` takes the exclusive barrier. Separate `maximum_entries` and
`maximum_files` bounds cap inspection and actions at 1,024 each. Its resumable
process-local cursor lets repeated calls traverse a larger or mostly fresh
backlog. It finalizes verified two-link intents immediately and removes only old,
private, regular one-link intents or legacy `.partial-*` debris. It never removes
an unknown, mismatched, symlinked, multi-linked, wrong-owner, or wrong-mode entry.

An exact-existing idempotent path re-reads and compares complete bytes, then
`fsync`s the file and parent directory. A collision, noncanonical manifest,
digest/size mismatch, special file, wrong owner/mode/link count, symlink, or
concurrent inode change fails closed.

## Deterministic training cut

The only v1 policy is `palimpsest-full-text-utf8/v1`; its complete machine object
is embedded in every cut and fixed by the schema. Unknown policies fail closed.

The builder holds the shared store barrier across enumeration, receipt/manifest
validation, content reads, and cut construction. It applies all three
availability clocks before opening content:

```text
manifest.knowledge_time             <= as_of
manifest.collected_at               <= as_of
manifest.acceptance.accepted_at     <= as_of
```

A historical-availability alternative which ignores either collection or trusted
acceptance is not v1 and is intentionally unsupported. Backfilled bytes cannot
appear in a cut dated before caller-declared collection or store acceptance.

Admitted manifests are grouped by `(source.id, content.sha256)`. One record is
emitted per group, sorted by that key. Every admitted immutable manifest remains
referenced in a provenance array sorted by manifest SHA-256, including URL,
media type, language, event/publication/knowledge/collection/acceptance clocks,
collection run, retention class, and original manifest rights. No arbitrary
duplicate wins.

Before content is opened, the group must have exactly one effective rights
terminal with `training_use == full_text`, and every admitted provenance media
type must satisfy the embedded textual rule:

- prefix: `text/`;
- exact: `application/javascript`, `application/json`,
  `application/x-ndjson`, `application/xml`;
- structured syntax suffix: `+json` or `+xml`;
- media-type parameters are forbidden at ingest;
- exact bytes must decode as UTF-8.

Eligible records carry decoded text and base64 of the same bytes, allowing exact
reconstruction. The cut contains exactly `spec_version`, `canonicalization`,
`as_of`, the complete policy, the as-of rights-ledger projection, and ordered
records. Its identity hashes all canonical bytes. Filesystem enumeration order,
ledger input order, JSON whitespace, and duplicate manifest order cannot affect
it.

Direct `TrainingCut` construction is private and always rejected. The public
`validate_training_cut` verifier requires both the complete rights ledger and
its independently trusted head. It validates that head and recomputes the exact
as-of visible projection before checking canonical encoding and digest,
schema/version/as-of/policy equality, ledger ordering and clocks, record order
and bounds, manifest/provenance hashes, media and rights eligibility, and content
text/base64/size/digest agreement. A grant-only cut with a visible revocation
omitted therefore remains invalid even when every embedded hash is recomputed.

`TrainingCut` stores only immutable canonical bytes plus scalar identity/clocks.
Policy, ledger, record, and dictionary accessors parse fresh detached values;
mutating returned nested objects cannot alter later reads or the cut digest.

## Resource limits and non-goals

- content: at most 8 MiB, with a smaller caller-configurable ingest bound;
- input metadata, rights-decision body, and manifest: at most 64 KiB each;
- acceptance receipt: at most 128 KiB;
- rights ledger: at most 512 decisions and 2 MiB canonical bytes;
- training cut: at most 512 dedupe groups, 512 provenance manifests per group,
  16 MiB decoded source bytes, and 24 MiB canonical output;
- accepted manifest scan: at most 100,000 manifests;
- accepted receipt scan: at most 100,000 receipts;
- staging recovery: batches of at most 1,024 inspected entries and 1,024 actions,
  with a minimum removal age of 60 seconds; repeated calls resume the current
  process-local pass through larger backlogs.

EvidenceDocument v1 establishes byte identity, immutable provenance, explicit
cut-time permission, deterministic dedupe, trusted acceptance, and point-in-time
selection. It does not prove source truth, authenticate a collector, adjudicate
copyright, infer a licence, execute retention deletion, fetch URLs, or make
untrusted bytes safe to run.
