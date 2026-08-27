# Machine Investigations

`readings/machine-investigations-latest.json` is Palimpsest's deterministic,
machine-readable analysis feed. It joins four already published aggregate
artifacts into citation-complete reports for downstream agents and static
renderers. It does not browse, interview people, infer motives, or turn an
unmet evidence threshold into a finding.

The feed is separate from the human-facing investigations desk. Its contract
is defined by:

- `core/machine_investigations.py` -- builder, runtime validator, canonical
  serializer, and command-line entry point;
- `config/machine_investigations.json` -- closed input and case configuration;
- `protocol/machine-investigations-v1.schema.json` -- public wire schema; and
- `readings/machine-investigations-latest.json` -- canonical generated output.

## Quick start

Build the default artifact from checked-in inputs:

```sh
python3 scripts/build_machine_investigations.py
```

Verify that the checked-in artifact is byte-current without writing:

```sh
python3 scripts/build_machine_investigations.py --check
```

The core module exposes the same CLI:

```sh
python3 -m core.machine_investigations --check
```

For a reproducible later decision clock, pass a UTC timestamp:

```sh
python3 scripts/build_machine_investigations.py \
  --as-of 2026-08-12T14:00:00Z \
  --output /tmp/machine-investigations.json
```

The build performs no network I/O. `--check` exits non-zero for a missing or
byte-different output and never rewrites it. A normal build writes a temporary
file in the destination directory, flushes it, and atomically replaces the
destination, so readers do not observe partial JSON.

## Python API

```python
import json
from pathlib import Path

from core.machine_investigations import (
    build_machine_investigations,
    canonical_json_bytes,
    validate_machine_investigations,
)

output_path = Path("readings/machine-investigations-latest.json")
current_head = json.loads(output_path.read_text(encoding="utf-8"))
document = build_machine_investigations(
    readings_dir=Path("readings"),
    config_path=Path("config/machine_investigations.json"),
    as_of=None,
    previous_document=current_head,  # omit only for the first publication
)
validate_machine_investigations(
    document,
    readings_dir=Path("readings"),
    config_path=Path("config/machine_investigations.json"),
)
payload = canonical_json_bytes(document)
```

With `as_of=None`, `generated_at` is the later of the newest required input
clock and the supplied previous document's clock. This floor prevents a routine
refresh from rolling back a preserved head merely because its current inputs
carry older clocks. An explicit `as_of` must be a timezone-aware ISO-8601
timestamp and cannot precede either bound. For fixed input bytes, config bytes,
previous head, and decision clock, the returned value and canonical bytes are
identical. Canonical JSON uses UTF-8, sorted object keys, compact separators,
finite numbers only, and one trailing newline.

The validator always verifies the closed config and complete public document.
Supplying `readings_dir` additionally re-hashes all four configured input files
and checks their byte counts and schema declarations against `input_receipts`.
It then rebuilds both cases in memory without treating either public candidate
as its own predecessor, and requires each report's deterministic content to
equal the derivation of those verified inputs. Correction-chain integrity is
validated independently against the current revision ID.

## Fixed public shape

Unknown fields fail closed. The top-level object has exactly these keys:

```text
schema_version       desk_id              generated_at
source               method               scope
publication_profiles input_receipts       n_cases
cases                reproducibility_receipt
```

Each case has exactly these keys:

```text
case_id              revision_id          source_case_id
source_revision_id   slug                 url
title                dek                  profile
status               report_type          status_reason
published_at         updated_at           hypotheses
claim_blocks         evidence             countercases
limitations          falsifiers           methodology
corrections           safety               evaluation_receipt
```

The JSON Schema fixes the public top-level and case envelopes. The standard-
library runtime validator is authoritative for the stricter nested object
fields, identifiers, reference integrity, derived unions, state transitions,
and resource bounds.

## Inputs and reproducibility

The v1 config admits exactly four inputs in canonical order:

1. `evidence-mesh-latest.json` for evidence class, freshness, independence, and
   upstream lineage;
2. `osint-china-latest.json` for the current network measurements;
3. `china-economic-pulse-latest.json` for economic readiness gates; and
4. `primary-documents-latest.json` for capture and parsing coverage.

Each `input_receipts` row records the configured identity, filename, public
URL, schema version, source clock, exact SHA-256 digest, byte length, and
validation state. The builder hashes the same bytes it parses.

`reproducibility_receipt` binds three integrity units using SHA-256:

- the exact config bytes;
- the canonical input-receipt set; and
- the canonical case set.

It also names the deterministic builder version. A case ID remains stable for
its predeclared case key; a source revision binds only that case's cited
evidence, gates, and publication state; and the `machinev-...` revision ID binds
the substantive report content plus the complete correction history. The
current history row's revision-ID field alone is normalized to break its
unavoidable self-reference; every other history byte, including prior IDs,
event clocks, change types, and summaries, participates in the digest. A mere
decision-clock advance does not manufacture a new content revision. Each
case's correction history ends at the current revision.

Do not silently drop an invalid case and publish the remainder. The document
is one integrity unit: an invalid field, citation, digest, gate, or revision
invalidates the whole feed.

## Profiles and report semantics

`publication_profiles` is the ordered pair `machine_brief` and
`automated_evidence_analysis`. V1 maps the two predeclared cases as follows:

| Case | Profile | Gate outcome | Report type |
| --- | --- | --- | --- |
| Network filtering denominators | `automated_evidence_analysis` | derived each build | `AnalysisReport` or `AbstentionReport` |
| Economic-state readiness | `machine_brief` | derived each build | `AnalysisReport` or `AbstentionReport` |

`NewsArticle` is never a valid report type. A published analysis has a passing,
publishable evaluation receipt with no failed gate IDs. An abstention has a
failed, non-publishable receipt whose `failed_gate_ids` are derived from the
failed gates. Both records have `published_at`: it is the publication clock for
the report record, not evidence that the abstained economic conclusion was
made.

An abstention is a first-class result rather than an empty record. It retains
cited evidence, countercases, limitations, falsifiers, methodology, correction
history, and the exact gates that prevented an analysis.

## Sentence-level citations

A claim block is a paragraph split into explicit sentences. Every sentence has
a non-empty array of exact evidence IDs. Each ID resolves to one evidence row
in the same case; dangling citations and unused evidence both fail validation.

The block is derived, not free-form:

- `paragraph` is the sentence text joined in order with a single space;
- `citation_ids` is the stable first-seen union of sentence citations; and
- `independence_group_ids` is the sorted, de-duplicated set of groups belonging
  to the block's cited evidence.

Repeating an artifact or derived product cannot manufacture corroboration.
For example, the OONI reachability and in-path measurements remain distinct
evidence rows but share `publisher:ooni`, so they contribute one independence
group at the network publication gate. `upstream_groups` preserves the deeper
lineage supplied by the evidence mesh.

Every evidence row carries an artifact identity and URL, artifact and source
clocks, SHA-256 receipt, selector, scalar value and denominator, evidence
class, independence and upstream groups, integrity and freshness state, and an
interpretation limit. Countercases, hypotheses, and falsifiers cite through
that same case-local evidence table.

Evidence URLs are immutable, hash-addressed redacted capsules under
`/news/analysis/evidence/sha256-<digest>.json`, where the digest addresses the
exact verified original input bytes. The capsule records aggregate citations
plus the rights and attribution context at first publication; it does not copy
the raw input. A later policy change updates mutable rendered pages and the
exact-edition Pages rights gate, but never rewrites that first-published capsule.
The builder revalidates the retained capsule against every current or historical
citation before reusing it. Mutable `readings/*-latest.json` endpoints remain
useful for discovery but are not the citation target of a published revision.

## Evaluation and correction receipts

`evaluation_receipt.gates` is the executable publication decision. Gate IDs
are unique, `failed_gate_ids` is the ordered projection of gates whose `passed`
value is false, and the independent-group count equals the length of its sorted
group list. Citation coverage is `1.0` because every analytical sentence is
cited.

The current network report passes citation, rights, lineage-independent
corroboration, freshness, and adversarial-review gates. The current economy
report records failed substantive-desk, baseline-history, and
parsed-primary-observation gates. Those are current outcomes, not hard-coded
roles: either case emits an abstention when a gate fails and may graduate to an
analysis when every declared gate passes. A consumer must not infer the
opposite status from prose or document volume.

Correction history is append-only. The current v1 records begin with an
`initial-publication` entry whose revision ID matches the case. A material
change appends a new content-addressed revision while preserving the original
`published_at`; an identical rebuild preserves the prior head and does not add
an event. The builder validates the previous document before carrying its
history forward and matches cases by their stable case IDs. A malformed,
rewritten, rolled-back, duplicate, or non-chronological history is rejected
before the current head can be replaced; changing history bytes necessarily
changes the current revision ID.

Each case retains at most 2,048 correction-history entries. At the expected
upper cadence of four material revisions per day, that is about 512 days of
append-only history before a protocol migration or archival rollover is
required. The 2 MiB document-wide output ceiling remains an independent final
guard: reaching either limit fails closed instead of silently truncating the
chain. Contract tests exercise more than 100 real successive revisions and the
full two-case capacity shape, keeping this protocol, runtime limit, and byte
budget aligned.

## Safety boundary

Only checked-in, public, aggregate artifacts may cross this boundary. Safety
state is explicit on every case:

```text
analysis_mode          deterministic-machine-analysis
human_interviews       none
personal_data          none
individual_allegations none
inferred_motives       none
```

The builder and validator reject:

- person, respondent, interview-source, contact, device, email, phone, or home
  address data in configuration or public report text;
- input filenames that are not `-latest.json` basenames, including absolute
  paths and `..` traversal;
- non-public or malformed URLs, credentials, fragments, and hostile schemes;
- duplicate JSON keys, unknown fields, non-finite values, invalid timestamps,
  dangling or duplicate IDs, and inconsistent counts;
- inputs over 8 MiB or a generated document over 2 MiB; and
- missing, malformed, schema-incompatible, or byte-mismatched inputs.

These are publication boundaries, not cleanup suggestions. The implementation
rejects the build or document instead of stripping a prohibited field and
continuing.

## Consumer checklist

Before using a report, a machine consumer should:

1. Validate the complete document and require the v1 schema and desk IDs.
2. Treat `status`, `report_type`, and `evaluation_receipt` as one state
   transition; never infer publication from prose.
3. Resolve every sentence citation and retain the cited evidence's denominator
   and interpretation limit.
4. De-duplicate corroboration by `independence_group` and preserve
   `upstream_groups` lineage.
5. Present countercases, limitations, and falsifiers with the claim blocks.
6. Treat an `AbstentionReport` as the result of the run and never synthesize the
   missing conclusion.
7. Preserve canonical bytes and receipts when caching or mirroring a revision.

Run the standard-library contract suite with:

```sh
python3 -m unittest tests.test_machine_investigations
```
