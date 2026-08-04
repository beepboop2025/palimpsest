# Palimpsest Evidence Capsule v1

Status: **v1, frozen**. The normative schema is
[`evidence-capsule-v1.schema.json`](evidence-capsule-v1.schema.json). A conforming
verifier must also enforce the behavioral rules below; JSON Schema validation by
itself is not verification.

## Purpose and security boundary

An evidence capsule is a portable claim plus the exact evidence bytes that support
it. It is data, never an instruction channel. A verifier:

- reads only the capsule and an explicitly supplied local artifact root;
- never dereferences `source.uri`, opens a socket, invokes a program, imports a
  plugin, renders active content, or follows an intent automatically;
- treats every artifact as inert bytes, including artifacts marked
  `untrusted: false`;
- fails closed on unknown versions, canonicalizations, binding/proof types,
  derivation proofs, artifact locations, claim types, or intent types.

`untrusted` is mandatory provenance metadata. `true` means the bytes originated
outside the capsule producer's trust boundary. It never makes execution safe;
there is no execution path in v1.

## Envelope and identity

```json
{
  "content": { "spec_version": "palimpsest-evidence-capsule/v1", "...": "..." },
  "content_sha256": "64 lowercase hex characters",
  "attestations": []
}
```

`content_sha256` is the capsule ID. It is SHA-256 over the canonical UTF-8 bytes
of `content`, and nothing else. Attestations live outside that hash so independent
parties can append statements without changing the evidence object's identity.
Each attestation repeats the `content_sha256` it targets. V1 binds that statement
but does not authenticate the actor; signature schemes are deliberately not
invented here.

### `palimpsest-json-sorted-utf8-v1`

Hashed content is serialized with JSON object keys sorted by Unicode code-point,
separators `,` and `:` with no whitespace, literal Unicode encoded as UTF-8, and
JSON `true`, `false`, and `null`. Numbers in hashed content are integers only;
decimal observations must be strings with their unit stated in the claim. NaN and
infinities, duplicate keys, non-string keys, lone surrogates, and non-JSON values
are invalid. This is a small versioned transform, not a claim of RFC 8785
conformance.

The historical Palimpsest ledger uses the separately named
`palimpsest-ledger-json-v1`, which is sorted compact JSON and permits finite JSON
decimals. A ledger binding must name that algorithm explicitly.

## Content

Content has exactly these fields:

- `producer` and `subject` identify who made the capsule and what it concerns.
- `artifacts` bind an ID to an exact SHA-256, byte size, media type, source URI,
  capture time, collector, trust flag, and either base64 bytes or a safe relative
  local path. Source URIs are provenance strings only.
- `claims` use an allowlisted type (`observation`, `measurement`, `provenance`,
  `integrity`, or `analytical-lead`) and an explicit evidence level (`direct`,
  `derived`, `sampled`, or `reported`). Limitations are first-class.
- `derivations` use a typed operation and one proof. V1 can recompute only
  `json-pointer-equals-v1`. `declared-nonrecomputable-v1` is an accepted honest
  negative result and requires a reason. Unknown proofs are invalid.
- `intents` are advisory and allowlisted: `human-review`, `investigate`,
  `preserve`, `compare`, or `cite`. `advisory` must be literal `true`. There is no
  shell, URL-open, webhook, mutation, or generic `action` intent.
- `bindings` are optional Palimpsest ledger/anchor proofs.

Relative artifact paths use `/`, contain no empty, `.` or `..` component, and are
resolved beneath an explicitly supplied root after symlink resolution. Absolute
paths, backslashes, root escapes, and non-files are rejected.

## Palimpsest ledger and anchor binding

`palimpsest-ledger-anchor-v1` binds one JSON artifact to:

1. the **full ledger entry**, whose `payload_sha256` is recomputed from the parsed
   artifact under `palimpsest-ledger-json-v1` and whose `entry_hash` is recomputed;
2. a `palimpsest-merkle-duplicate-last-v1` inclusion proof. The verifier checks
   `seq`, tree width, every sibling side, duplicate-last padding, path length, and
   the final root;
3. an exact anchored prefix: entry count, head, Merkle root and anchor time must
   occur byte-for-byte in the bound anchor-input artifact, and the proof artifact
   must be an OpenTimestamps v1 SHA-256 detached proof whose file digest commits
   to those exact anchor-input bytes.

The standard-library verifier reports the anchor status as `bound`: it proves the
capsule names the exact timestamp input and that the detached proof commits to
those bytes, but it does not interpret the timestamp tree or claim to validate
the Bitcoin attestation. Doing that requires a separately audited OpenTimestamps
verifier. It is never invoked automatically.

An adapter must refuse to emit a Palimpsest capsule unless the reading's complete
canonical payload has an exact ledger entry and a valid anchored prefix containing
that entry. A digest or root mentioned in prose is not a seal.

## Verification report

Conforming implementations report these independently:

- `integrity`: canonical content hash;
- `artifacts`: exact byte digest and size for every artifact;
- `ledger`: payload, full entry, and inclusion proof;
- `anchor`: exact prefix/proof-byte binding, distinct from external timestamp
  validation;
- `recomputability`: `verified`, `partial`, `not_recomputable`,
  `not_applicable`, or `failed`.

An honest `not_recomputable` derivation does not make a capsule malformed. It
prevents a consumer from confusing tamper evidence with independent reproduction.
Any failed or unknown proof, digest, path, root, action, version, or
canonicalization makes the overall result fail closed.

## Golden vectors

`test-vectors/palimpsest-erasure-v1.json` binds a current Erasure Observatory
source reading to its complete ledger entry, inclusion proof, and exact anchored
prefix. `test-vectors/nemesis-ddti-v1.json` is a CDT-backed, sampled DDTI lead and
states why its score is not independently recomputable from the included sample.
Both repository verifiers must accept both files.
