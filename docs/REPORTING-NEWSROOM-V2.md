# Reporting Newsroom v2

## Decision

Palimpsest will keep the fast evidence wire, but add a primary-document plane and
an explicit editorial-readiness plane above it. A source can be operational for
document capture without being operational for structured economic estimates.
That distinction is part of the public record.

The first release covers the priority source families named in the newsroom
audit: NBS housing and macro releases, PBOC credit, GACC trade, MOT transport,
SPB parcels, NEA electricity, IMF PortWatch, Sentinel-5P, VIIRS, the three
Chinese exchange filing systems, and the World Bank Enterprise Survey.

## Requirements

### Functional

1. Preserve exact source bytes and a content-addressed manifest.
2. Record original URL, publication and retrieval clocks, coverage, units,
   denominators, methodology, revision lineage, rights, and independence group.
3. Never let a backfilled release appear in an as-of cut before Palimpsest first
   collected it.
4. Make primary documents available to dossier matching without treating a
   topical link as corroboration.
5. Preserve frozen network panel identities, synchronized rounds, protocol and
   vantage scope, control locations, and routing/outage controls.
6. Keep protected notes and source identities outside the public repository,
   while publishing enough aggregate workflow state to enforce consent,
   attribution, expert/affected-voice, fact-check, and right-to-reply gates.
7. Apply different publication thresholds to wire briefs, explainers, and
   investigations. No profile permits automatic publication.

### Non-functional

- Collection must be bounded, keyless unless explicitly configured otherwise,
  polite, and fail closed on redirects, private-network resolution, oversized
  bodies, or unreviewed source broadening.
- Deterministic builders must remain offline and byte-replayable.
- Raw documents and protected reporting material remain in private, mode-0600
  stores. Public outputs contain hashes, provenance, aggregate readiness, and
  explicit limitations only.
- A single failed source must not erase the last good capture or become a zero.

## Architecture

```text
official release/catalog URLs                 human reporting
             |                                      |
             v                                      v
  closed primary-source registry          private source-workflow store
             |                                      |
             v                                      |
  bounded fetch + EvidenceDocument v1               |
             |                                      |
             +-----------> public receipts <--------+
                              |
              +---------------+----------------+
              |                                |
              v                                v
       EconomicObservation ledger       network round ledger
       (only parsed measurements)        (frozen panel identity)
              |                                |
              +---------------+----------------+
                              v
                    cross-source dossiers
                              |
                              v
             wire / explainer / investigation gate
                              |
                              v
                  human-reviewed publication only
```

## Source states

Every registered source has two independent capability states:

| State | Meaning |
| --- | --- |
| `catalog_metadata` | The official dataset or filing catalog is captured, but no observation is claimed. |
| `release_document` | Exact official release bytes and revision lineage are captured. |
| `structured_observations` | A reviewed parser emits aggregate `EconomicObservation` rows with units and denominators. |
| `licensed_adapter` | Code exists, but operation requires a reviewed licence or credential. |
| `blocked` | Access controls, terms, safety, or unsupported interfaces prevent collection. |

The public coverage matrix reports both document and observation state. A
landing page can therefore improve primary-source discovery without inflating
the number of measured series.

## Primary-document contract

A public receipt contains:

- source, publisher, original URL, media type, and content SHA-256;
- publication time when the source declares one, retrieval and trusted
  acceptance times always;
- geography, sector, series families, units, and denominator definitions;
- methodology URL/version and known methodology-change notes;
- `vintage_id`, revision number, and the prior content hash for a changed URL;
- rights mode and independence group;
- capture scope (`catalog_metadata`, `release_document`, or
  `structured_observations`) and an explicit interpretation limit.

The exact bytes live in the existing private `EvidenceDocumentStore`. The
public receipt never contains a private filesystem path. Historical builds use
the source publication clock and Palimpsest acceptance clock together, so a
backfill is useful context but not false real-time knowledge.

## Corroboration

Evidence groups are publisher/method families, not URLs. Mirrors and multiple
feeds from one publisher remain one group. Matching proceeds in conservative
layers:

1. exact canonical URL;
2. exact source release identifier;
3. reviewed subject key plus compatible period and geography;
4. bounded title/time similarity.

A declared subject key creates a candidate edge, not corroboration. An event
becomes corroborated only when at least two independent groups attach to the
same bounded event. Ambiguous candidates stay separate.

## Network rounds

The longitudinal network contract identifies the immutable target panel,
domain category, protocol (`DNS`, `HTTP`, `HTTPS_TLS`, or `QUIC`), method,
inside-China ASN/region, same-round external control, start/end clocks, and
routing/outage controls. A comparable series requires at least three completed
rounds with the same panel and method version. Each comparable round must also
record a bounded 15-minute window, complete per-target external controls,
resolved routing ownership, and a time-aligned outage control. The editorial
gate reads this ledger directly, so a prose attestation cannot bypass the
three-round threshold. Public summaries always use the phrase “this target
failed under this method and vantage”; the contract has no field for a national
censorship percentage.

## Human reporting boundary

The source-workflow store uses pseudonymous source IDs and accepts protected
notes only as already encrypted bytes. It records consent scope, attribution
mode, voice role, safety review, verification state, and right-to-reply state.
Names, contact details, plaintext notes, probe operators, and interview text are
not valid public fields. Public readiness exports only aggregate counts and
derived gate states; it publishes neither source identifiers nor note-level
ciphertext hashes that could become stable correlators.

## Publication profiles

### Wire

Wire briefs may be single-source. They require attribution, a source receipt,
scope limitations, and a visible single-source label. They are never promoted
as corroborated merely because an internal instrument has a topical link.

### Explainer

An explainer requires a primary document, two independent evidence groups,
historical/comparable context, counterevidence or an alternative explanation,
an expert voice, an affected voice when relevant, an explanatory visual,
sentence-level citations, explicit limitations, and human editing.

### Investigation

An investigation requires every explainer check plus fact-check completion,
right-to-reply before institutional allegations, a visible correction/update
history, assessed falsification conditions, and safety review. Automatic
publication is prohibited for every profile.

## Reliability and rollout

1. Ship contracts, registries, replay fixtures, and private storage first.
2. Activate bounded document capture for every priority family.
3. Promote parsers to `structured_observations` one source at a time after
   replay tests cover units, denominator changes, revisions, and abstention.
4. Join primary receipts into dossiers only after deterministic matching tests.
5. Deploy the collector job to the isolated Hetzner queue; publish only the
   scrubbed receipt projection to GitHub Pages.

Metrics to watch are capture success by source, changed vintages, documents
with known publication clocks, parsed observation coverage, multi-group event
rate, false-join audit rate, and failed editorial checks. Alerting tracks
collection health; it does not turn missing data into evidence.

## Shipped commands and public artifacts

The closed registry has fourteen source families. A production capture is:

```bash
python -m scripts.primary_documents_pull
python -m scripts.primary_documents_pull --check
```

Exact bytes and manifests go to `PALIMPSEST_EVIDENCE_DOCUMENT_STORE`; only
`readings/primary-documents-latest.json` is public. A failed publisher remains
in the coverage matrix and retains any last-good vintage. TLS verification and
the no-redirect policy are never weakened to increase the success count.

The deterministic publication graph is:

```bash
python -m scripts.build_network_rounds
python -m scripts.build_corroboration
python -m scripts.build_editorial_readiness
python -m scripts.build_newsroom
python -m scripts.build_data_catalog
python scripts/seal_readings.py
```

GitHub executes that graph over checked-in public evidence. It does not fetch
primary documents because an ephemeral runner cannot preserve the private
immutable store. The dedicated collector node performs primary capture daily;
the public workflow publishes its metadata projection and recomputes every
downstream gate.

Reporter-managed source intake uses `scripts.source_workflow`. `commit` accepts
only an already encrypted age or OpenPGP object plus strict metadata; `export`
publishes aggregate package readiness. Plaintext interview notes, identities,
contact details, attribution labels and probe-operator information are outside
the public contract.

## Trade-offs and revisit points

- Conservative matching leaves some true events split, but false
  corroboration is more damaging than missed automation. Revisit after a
  labelled bilingual match set exists.
- Document capture provides primary evidence before every table has a parser,
  but it must not be counted as structured coverage. Revisit each source state
  only through fixtures and code review.
- Encrypted-note ingestion avoids inventing cryptography in this repository,
  but requires reporters to use an approved encryption tool before intake.
  Revisit when the newsroom chooses a managed source-protection system.
- GitHub Pages is suitable for public receipts, not raw corpora or protected
  notes. Revisit public object storage only after retention, takedown, and
  redistribution policy are operational.
