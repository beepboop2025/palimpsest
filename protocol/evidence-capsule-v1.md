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
JSON `true`, `false`, and `null`. Numbers in hashed content are integers only and
must be inside the I-JSON safe range `[-9007199254740991, 9007199254740991]`;
decimal observations must be strings with their unit stated in the claim. NaN and
infinities, duplicate keys, non-string keys, lone surrogates, and non-JSON values
are invalid. This is a small versioned transform, not a claim of RFC 8785
conformance.

JSON strings, including object keys, have exactly one v1 encoding. Surround the
Unicode-scalar sequence with `"`, then encode:

- `"` as `\"` and `\` as `\\`;
- U+0008, U+0009, U+000A, U+000C, and U+000D as `\b`, `\t`, `\n`, `\f`,
  and `\r`, respectively;
- every other U+0000–U+001F scalar as `\u00xx`, using lowercase hexadecimal;
- every other scalar literally as UTF-8, including `/`, U+2028, U+2029, and
  non-BMP scalars. Surrogate escapes and optional escaping are forbidden.

No Unicode normalization is performed.

Integers use their shortest base-10 form: negative values have one leading `-`,
nonnegative values have no sign, there are no leading zeroes, and zero is `0`.
These rules, rather than a runtime's default JSON writer, are normative.

The historical Palimpsest ledger uses the separately named
`palimpsest-ledger-python-json-v1`: CPython `json.loads` followed by `json.dumps`
with `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`, and finite
binary64 decimals. That legacy transform is explicitly Python-specific rather
than falsely claiming cross-runtime decimal portability. A ledger binding must
name it exactly.

## Content

Content has exactly these fields:

- `producer` and `subject` identify who made the capsule and what it concerns.
- `artifacts` bind an ID to an exact SHA-256, byte size, media type, source URI,
  capture time, collector, trust flag, and either base64 bytes or a safe relative
  local path. Source URIs are provenance strings only.
- `claims` use an allowlisted type (`observation`, `measurement`, `provenance`,
  `integrity`, or `analytical-lead`) and an explicit evidence level (`direct`,
  `derived`, `sampled`, or `reported`). Every claim cites at least one artifact;
  `derivation_refs` and `binding_refs` make machine support explicit. A binding
  referenced by a claim must bind an artifact that the same claim cites.
  Limitations are first-class.
- `derivations` use a typed operation and one proof. V1 can recompute only
  `json-pointer-equals-v1`. `declared-nonrecomputable-v1` is an accepted honest
  negative result and requires a reason. Unknown proofs are invalid.
- `intents` are advisory and allowlisted: `human-review`, `investigate`,
  `preserve`, `compare`, or `cite`. `advisory` must be literal `true`. There is no
  shell, URL-open, webhook, mutation, or generic `action` intent.
- `bindings` have stable IDs and are optional Palimpsest entry-membership and
  anchor-envelope evidence. They are not full-ledger-chain proofs.

Relative artifact paths use `/`, contain no empty, `.` or `..` component, and are
resolved beneath an explicitly supplied root after symlink resolution. Absolute
paths, backslashes, root escapes, and non-files are rejected.

## Palimpsest entry membership and anchor-envelope binding

`palimpsest-ledger-anchor-v1` binds one JSON artifact to:

1. the **full ledger entry**, whose `payload_sha256` is recomputed from the parsed
   artifact under `palimpsest-ledger-python-json-v1` and whose `entry_hash` is
   recomputed. This binds the canonical JSON value, not its original whitespace
   or key order;
2. a `palimpsest-merkle-duplicate-last-v1` inclusion proof. The verifier checks
   `seq`, tree width, every sibling side, duplicate-last padding, path length, and
   the final root;
3. an exact anchor input: entry count, head, Merkle root and locally recorded
   anchor time must parse to the exact values declared by the binding, and the
   proof artifact must be a complete, bounded OpenTimestamps v1 SHA-256
   detached-envelope serialization whose embedded subject digest equals SHA-256
   of those exact anchor-input bytes.

The standard-library verifier reports `envelope_bound`: it parses the timestamp
tree through EOF and proves that the envelope names the exact input digest. It
does **not** prove calendar acceptance, authenticate any attestation, inspect a
block header, validate consensus, establish a time, or prove that anyone
submitted the input to OpenTimestamps. Those require a separately audited
OpenTimestamps verifier and trusted block-header source, neither invoked
automatically.

The transported capsule proves one recomputed entry's Merkle membership in a
declared root. It does not carry the predecessor entries needed to verify the
ledger hash chain. A trusted adapter may check a frozen complete chain before
emission, but the offline report remains `entry_membership_verified`, never
`chain_verified`.

An adapter must refuse to emit a Palimpsest capsule unless the reading's complete
canonical payload has an exact ledger entry and a valid anchored prefix containing
that entry. A digest or root mentioned in prose is not entry-membership evidence.

## Verification report

Conforming implementations report these independently:

- `integrity`: canonical content hash;
- `artifacts`: exact byte digest and size for every artifact;
- `ledger`: `entry_membership_verified`, explicitly distinct from chain integrity;
- `anchor`: `envelope_bound`, distinct from calendar, attestation, consensus, and
  timestamp validation;
- `recomputability`: `verified`, `partial`, `not_recomputable`,
  `not_applicable`, or `failed`.
- `claims`: per-claim artifact, derivation, and binding-reference results. These
  results do not evaluate the truth of natural-language statements.

An honest `not_recomputable` derivation does not make a capsule malformed. It
prevents a consumer from confusing tamper evidence with independent reproduction.
Any failed or unknown proof, digest, path, root, action, version, or
canonicalization makes the overall result fail closed.

`ok` therefore means only: the envelope is well formed, its content identity and
artifact bytes match, and every declared reference/proof relationship satisfies
the v1 rules. It does not authenticate the producer, prove source truth, verify a
complete ledger chain, or establish a timestamp.

## Resource limits

A v1 verifier rejects capsules over 32 MiB, canonical content over 24 MiB,
individual artifacts over 8 MiB, cumulative resolved artifacts over 16 MiB, JSON
nesting deeper than 32, and collections above the schema maxima (64 artifacts,
128 claims, 128 derivations, 32 intents, 32 bindings, and 64 attestations).
OpenTimestamps envelopes are limited to 1 MiB, depth 64, and 2,048 tree nodes.
JSON-pointer array indices are canonical ASCII decimal digits only.

## Golden vectors

`test-vectors/palimpsest-erasure-v1.json` binds a current Erasure Observatory
source reading to its complete ledger entry, inclusion proof, and exact anchored
prefix. `test-vectors/nemesis-ddti-v1.json` is a CDT-backed, sampled DDTI lead and
states why its score is not independently recomputable from the included sample.
`test-vectors/canonicalization-v1.json` fixes the exact bytes for quotes,
backslashes, slashes, every control-character escape class, U+2028/U+2029, and
non-BMP Unicode, including the U+E000/U+10000 key-order boundary that differs
from UTF-16 code-unit ordering. Both repository conformance suites must validate
all vectors.

`conformance-v1.json` pins the verifier, schema, protocol, and golden-vector
digests as one release unit. Both repositories carry and test the same manifest;
changing one component requires a new release identity. `conformance_release` is
`sha256:` followed by SHA-256 of the `files` object serialized under
`palimpsest-json-sorted-utf8-v1`, so two divergent file maps cannot truthfully
claim the same conformance release.
