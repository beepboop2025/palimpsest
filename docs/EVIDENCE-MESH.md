# Evidence Mesh v1

Status: deterministic offline inventory contract. The mesh describes what the
machine-investigation system may use; it does not itself join observations,
score a hypothesis, or publish a story.

## Purpose

Palimpsest, Seiche, LiquiLens, ScamShield, and NarcoScope expose different
analytical lenses. Counting those products as five sources would be wrong when
two products ultimately use the same release, mirror, or collection pipeline.
Evidence Mesh v1 makes that dependency visible before a machine-investigation
gate evaluates corroboration.

For every admitted resource, the mesh records:

- project, namespace, contract, availability, and permitted analytical role;
- evidence class and rights/reuse/training disposition;
- event, knowledge, and publication clocks when the admitted source exposes
  them, plus source temporal coverage when a source describes a period rather
  than an instant;
- freshness, cadence, and explicit unavailable states;
- one pipeline-level `independence_group`, the ultimate `upstream_groups`, and
  explicit dependencies on other resources; and
- limitations without copying underlying rows, messages, indicators, or
  person-level records.

The normative public shape is
[`protocol/evidence-mesh-v1.schema.json`](../protocol/evidence-mesh-v1.schema.json).
Runtime and referential checks live in
[`core/evidence_mesh.py`](../core/evidence_mesh.py). Project declarations and
input contracts live in
[`config/evidence_mesh.json`](../config/evidence_mesh.json).

## Resource identity and independence

A resource is an inspectable surface. An independent group is an upstream
collection or publication lineage. They are intentionally not the same thing.

For example, `palimpsest:catalog:ddti` and `palimpsest:osint:ddti` are two
resources because one is the catalog declaration and one is the OSINT command
surface. Both retain the same independence group and upstream publisher. The
second rendering therefore adds no corroboration. OONI-derived GFW and in-path
interference resources also retain their shared OONI upstream group.

Derived products such as vantage fusion, the board alarm, economic pulse, and
editorial readiness have their own pipeline identity for audit, but set
`independence_eligible` to `false`. Their `upstream_groups` are recursively
inherited from their declared dependencies. A newsroom artifact cannot cite a
fusion of three sources and then count that fusion as a fourth source.

The `evidence-mesh` and `machine-investigations` catalog entries are publication
planes, not evidence inputs. The builder observes only whether their checked-in
artifacts exist; it deliberately does not load their bytes or clocks. This
keeps both surfaces discoverable without creating the recursive dependency
`evidence mesh -> machine publication -> evidence mesh`.

Consumers should count only distinct `independence_group` values from resources
that are all of:

1. `availability == "available"`;
2. `independence_eligible == true`;
3. admitted for the claim's `allowed_role`; and
4. actually supportive of the claim at the relevant clocks and grain.

The summary's `independent_groups_available` is an inventory health count, not
proof that any particular claim is corroborated.

## Roles

The four roles are closed and intentionally asymmetric:

| Role | Permitted use |
| --- | --- |
| `evidence` | May support an aggregate claim after claim-level validation. |
| `context` | May frame or challenge a finding; cannot by itself corroborate it. |
| `typology` | Defines a reviewed pattern or mechanism; never establishes that a case matches it. |
| `candidate-only` | May open a private analytical lead; cannot support automatic publication. |

ScamShield's checked-in intelligence pack is represented as `typology`.
Consent-scoped assessment exports remain candidate-only unless a separate
aggregate Lab Evidence Envelope passes its own publication contract.
NarcoScope's five official aggregate datasets are evidence, with publisher
lineage and native limitations retained. Cross-lane co-movement never implies
that illicit markets caused a monetary, censorship, or market event.

Seiche and LiquiLens are `REVIEW_GATED` capabilities with no verified public
data URL in this contract. The previously declared Seiche CN-CNY overview does
not resolve to a live dataset, and the LiquiLens repository is not publicly
readable without authentication. Neither address is emitted as public access.
A bounded local Lab Evidence snapshot must be caller-supplied and validated
before the mesh exposes either product's observation; absence remains
`unavailable`.

## Inputs and validation

The builder strictly consumes and validates:

- all entries in `config/public_data_catalog.json`;
- all current signals in `readings/osint-china-latest.json`;
- the Intelligence Commons manifest and its project references;
- ScamShield's inert typology pack and source references;
- NarcoScope's public aggregate through
  `core.narcoscope_bridge.validate_artifact`, plus its append-only pin receipt
  through `validate_receipt`; and
- optional Seiche and LiquiLens `lab-evidence-envelope/v1` snapshots through
  the normative Lab Evidence runtime, including content hashes, source groups,
  clock order, correction graphs, and publication gates.

Required malformed inputs fail closed. A valid NarcoScope artifact whose bytes
or `dataAsOf` no longer match the separately valid current pin is retained as a
`stale` resource with `byte_identity == "mismatch"`; it is not described as the
current producer object. Invalid artifacts or invalid receipts fail closed.
Each NarcoScope dataset retains its native `source_temporal_coverage`. Its event
clock is the source period end or snapshot date, while knowledge, publication,
and freshness use the pin's `admitted_at` clock. The producer's build-day
`dataAsOf` is never substituted for five unrelated event dates, and a future
admission relative to the mesh build fails closed rather than yielding a
negative age.

Optional absent inputs do not fail a Palimpsest-only investigation. They emit
both an unavailable receipt and an unavailable placeholder resource. Their
hash, byte count, observation time, and `resource_count` are `null`, never
zero. A supplied optional file that is malformed, unsealed, person-level, or
contains contact data fails closed.

All JSON inputs are bounded, duplicate keys and non-finite values are rejected,
unknown contract fields fail, local configured paths are repository-relative,
and source URIs are inert provenance labels. The mesh copies no measurement
values from the source artifacts.

## Deterministic build and check

Build at an explicit clock for reproducible bytes:

```bash
python3 -m core.evidence_mesh \
  --now 2026-08-12T13:00:00Z \
  --output /tmp/evidence-mesh.json
```

Include optional aggregate partner snapshots:

```bash
python3 -m core.evidence_mesh \
  --seiche-snapshot /controlled/inbox/seiche-envelopes.json \
  --liquilens-snapshot /controlled/inbox/liquilens-envelopes.json \
  --output /tmp/evidence-mesh.json
```

Rebuild and compare against the recorded `generated_at` clock:

```bash
python3 -m core.evidence_mesh \
  --check \
  --output /tmp/evidence-mesh.json
```

The Python API is:

```python
from core.evidence_mesh import (
    build_evidence_mesh,
    check_evidence_mesh,
    validate_evidence_mesh,
    write_evidence_mesh,
)
```

`write_evidence_mesh` validates, fsyncs, and atomically replaces the destination.
`check_evidence_mesh` validates the stored document and compares its exact bytes
with a deterministic offline rebuild.

## Machine-investigation use

An investigation engine should select resources by role, freshness, geography,
grain, and claim time; traverse dependencies; collapse shared independence
groups; and then run its hypothesis, counterevidence, falsification, and
citation checks. The mesh is a prerequisite for that work, not a substitute
for it.

The contract prohibits person-level data, contact data, exact indicators,
missing-as-zero semantics, and automatic attribution. It supports novel
aggregate analysis while making the boundary between an original computation
and an independent fact mechanically auditable.

Project `status` describes operational availability, not evidentiary admission.
For example, ScamShield is `ACTIVE`, while its public contract remains limited to
the `typology` role and cannot independently corroborate a factual allegation.
