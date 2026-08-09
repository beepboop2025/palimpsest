# Lab Evidence Envelope v1

Status: **v1 public interchange contract**. The normative structural schema is
[`lab-evidence-envelope-v1.schema.json`](lab-evidence-envelope-v1.schema.json).
The behavioral checks in this document are also required; JSON Schema cannot
compare timestamps, decimal strings, or references into sibling arrays.

## Purpose

The Lab Evidence Envelope carries one bounded, aggregate measurement between
public research projects without flattening observation, derivation, and
scenario into the same kind of number. It is designed for the Palimpsest
Intelligence Commons, but it contains no Palimpsest-specific execution hook.

The envelope is inert data. A consumer must not dereference a source URI, run a
program, import a plugin, mutate a store, or publish a value merely because an
envelope asks it to. Source URIs are provenance labels. A receiving project
independently applies its licence, privacy, freshness, and review policy.

This envelope complements, rather than replaces, the
[Palimpsest Evidence Capsule v1](evidence-capsule-v1.md). The envelope
normalizes time, jurisdiction, measurement, uncertainty, and publication
policy. An Evidence Capsule can separately bind exact supporting bytes and
claims to a tamper-evident record.

## Public-data and claim boundary

V1 is aggregate-only. `contains_exact_iocs` and `contains_raw_messages` are
literal `false`; there is no field in which to place a handle, phone number,
wallet, message fragment, person-level allegation, or private case record.
`CONTROLLED_AGGREGATE` still means aggregate: it allows a record to be exchanged
for review, not personal or case-level data to be smuggled through the common
contract.

An envelope records evidence relationships. It does not establish guilt,
source of funds, ownership, coordination, or causation. In particular,
co-movement between illicit-market or scam aggregates and a money-market series
does not show that one caused the other. A typology match is a lead, not an
attribution. A scenario is an assumption-bearing counterfactual, not an
observation.

## Field semantics

| Field | Meaning |
| --- | --- |
| `schema` | Literal `lab-evidence-envelope/v1`. Unknown versions fail closed. |
| `record_id` | Stable lowercase identifier for this published record. A correction gets a new ID and names the old one in `supersedes`. |
| `signal_id` | Stable semantic series or signal identifier. It is not a person, account, or indicator identifier. |
| `event_time` | When the measured event or reference period ended. A period measure uses its period end and explains the period in `limitations`. |
| `knowledge_time` | First time the producer could have known the value from the cited material. |
| `publication_time` | Time this exact envelope became available to consumers. |
| `jurisdiction` | ISO alpha-2, ISO alpha-3, UN M49, or one closed special code: `GLOBAL`, `MULTI`, or `UNKNOWN`. |
| `dimensions` | Optional controlled substance and typology identifiers. Free-text people and entities are deliberately absent. |
| `measure` | A typed decimal-string point `value`, or a typed decimal-string `interval`, plus a unit. |
| `evidence_status` | Whether the published measure is `OBSERVED`, `DERIVED`, or `SCENARIO`. |
| `measured_fraction` | Decimal string from 0 through 1. It describes how much of the result is measured, not statistical confidence. |
| `support_level` | Strength and kind of the evidence relationship; definitions appear below. |
| `source_groups` | Declared independent upstream collection or publication groups. |
| `source_refs` | Public source or method references, each bound to a source group and exact content SHA-256. |
| `method` | Versioned method, input record IDs, and assumptions. Required for derived and scenario records. |
| `hashes` | SHA-256 identities for the record projection, source set, and optional method or artifacts. |
| `redistribution_status` | Whether values may be redistributed, must be attributed, are link-only, restricted, or unknown. |
| `public_value_allowed` | A separate fail-closed gate for showing the numeric value on a public surface. |
| `privacy_tier` | Public or controlled aggregate. Neither tier permits exact indicators or raw messages. |
| `review_status` | Machine and human review state; `HUMAN_REVIEW_REQUIRED` is not publication approval. |
| `limitations` | Required, record-specific reasons not to overgeneralize the measure. |
| `supersedes` | IDs of earlier records replaced by this one. An empty list means this is not a declared correction. |

## Time and revision rules

Producers must emit UTC timestamps with an explicit offset and consumers must
parse them as instants. The required order is:

```text
event_time <= knowledge_time <= publication_time
```

If a source revises an older event, the new envelope retains the original
`event_time`, uses the new discovery and publication times, receives a new
`record_id`, and lists the prior record in `supersedes`. `record_id` must not
supersede itself, every referenced ID must exist in the consumer's admitted
record set, and a supersession graph must be acyclic.

Annual, monthly, daily, and intraday records keep their native cadence. A
consumer must not forward-fill an annual NarcoScope observation into daily
ScamShield or Seiche rows, use publication time as event time, or interpret a
missing/stale record as zero activity.

## Measurement and uncertainty

Decimals are strings so independent runtimes do not silently round a hashed
record. A measure contains exactly one of:

- `value`: one decimal string; or
- `interval`: `lower`, `upper`, `kind`, and optional `level`, all with the same
  declared unit.

Consumers must parse decimals without binary-floating-point coercion when
checking a record and enforce `lower <= upper`. `level` is allowed only when it
has a method-defined meaning. A producer must explain denominators, sampling
frames, period starts, normalization, and withheld sums in `limitations`.

`measured_fraction` is lineage, not precision:

- `OBSERVED` requires exactly `"1"`;
- `SCENARIO` requires exactly `"0"`, `SCENARIO_ONLY`, a method, and at least one
  stated assumption;
- `DERIVED` may range from `"0"` through `"1"`, requires a method, and must not
  hide assumed inputs. The producer defines and versions how it computes the
  fraction. A count of terms is not automatically a defensible weighting rule.

Measured and scenario values must remain separate through every sum, chart,
export, and label. A public consumer must not turn scenario injection values
into observed illicit proceeds or use sparse reviewed samples as a population
estimate.

## Support levels

- `DIRECT_OBSERVATION`: the cited material directly contains the aggregate
  measurement.
- `CORROBORATED_OBSERVATION`: at least two genuinely independent source groups
  support the observation.
- `REPORTED_OBSERVATION`: a cited publisher reports the measure, but this
  producer did not independently reproduce it.
- `DERIVED_ESTIMATE`: a versioned method transforms cited inputs.
- `TYPOLOGY_MATCH`, `CORROBORATED_LEAD`, and `DIRECT_LINK`: the bounded
  ScamShield evidence relationships. They never mean guilt or risk. Even a
  `DIRECT_LINK` envelope contains only an aggregate result; the exact binding
  remains in the private, access-controlled assessment.
- `SCENARIO_ONLY`: the value exists only inside an explicit scenario.
- `NOT_ASSESSED`: no stronger support classification was performed.

`CORROBORATED_OBSERVATION` and `CORROBORATED_LEAD` require at least two source
groups. A primary endpoint and its GitHub mirror are one group when they share
an upstream pipeline. Repetition by many pages from the same dataset does not
manufacture independence. Every `source_refs[].group_id` must occur exactly in
`source_groups`; source-ref IDs must be unique; every declared group must back
at least one source ref.

## Hash rules

All digests are 64 lowercase hexadecimal SHA-256 values.

`source_refs[].content_sha256` hashes the exact retrieved bytes before parsing.
`hashes.source_set_sha256` hashes the `source_refs` array sorted by `id`, using
`palimpsest-json-sorted-utf8-v1` from Evidence Capsule v1. This source-set
projection contains only strings and therefore does not invoke the
canonicalizer's integer rule.

`hashes.record_sha256` hashes a deep copy of the complete envelope after only
`hashes.record_sha256` has been removed, using the same canonicalization. This
avoids a self-referential digest while binding the source-set digest, method,
policy fields, limitations, and supersession links. Optional
`method_sha256` and `artifact_sha256` values hash exact bytes, not URLs or
rendered text.

A schema-valid digest is not automatically a verified digest. Consumers must
recompute hashes before admitting a record. Hash equality proves byte identity,
not source truth or independent corroboration.

## Redistribution, privacy, and review

`public_value_allowed: true` is valid only for `OPEN` or
`ATTRIBUTION_REQUIRED` data that is `PUBLIC_AGGREGATE` and either
`MACHINE_VALIDATED` or `HUMAN_REVIEWED`. `LINK_ONLY`, `RESTRICTED`, and
`UNKNOWN` values fail closed: a public projection may show provenance and a
redacted state, but not the value. The source's terms remain authoritative;
this flag cannot grant a licence the producer does not hold.

A human-reviewed record requires `reviewed_at`. Rejection stays part of the
audit trail but is not a public measurement. Automated producers should publish
their validation rules and abstain when coverage, freshness, source dominance,
or required review is insufficient.

Private owner-operated analysis may consume admitted public envelopes inward.
It must not make public projects depend on private code, publish private
calibrations, or relabel private scenario output as an open observation. The
public contract and its documentation intentionally expose no private
repository or filesystem location.

## Conformance checklist

A consumer accepts an envelope only after it has:

1. rejected duplicate JSON keys, non-finite numbers, unknown fields, and an
   unknown schema version;
2. applied the JSON Schema and the time, decimal, interval, reference,
   supersession, and hashing rules above;
3. checked freshness at the signal's native cadence;
4. applied redistribution and privacy gates before exposing any value;
5. preserved the evidence status, measured fraction, support level, sources,
   limitations, and review state in every downstream representation; and
6. kept cross-lane comparisons descriptive unless a separately preregistered,
   held-out analysis earns a narrower claim.
