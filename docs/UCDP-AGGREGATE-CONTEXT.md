# UCDP annual aggregate context

Palimpsest can ingest the three reviewed Uppsala Conflict Data Program (UCDP)
version 26.1 annual bulk archives without an API token. The adapter is an
aggregate historical-context surface for Balochistan and Myanmar. It is not an
event feed, actor dossier, route monitor, early-warning system, or attribution
engine.

The UCDP Download Center states that its datasets are licensed under CC BY 4.0
and identifies the publications that users should cite. This adapter carries
that attribution and the exact rights URL in every bundle:

- catalog and rights: <https://ucdp.uu.se/downloads/>
- license: <https://creativecommons.org/licenses/by/4.0/>
- current annual citation: <https://doi.org/10.1093/jopres/xjag046>

## Evidence boundary

The private acquisition layer accepts exactly these versioned archives:

| Input | Exact member | Encoding | Public use |
|---|---|---|---|
| UCDP/PRIO Armed Conflict 26.1 | `UcdpPrioConflict_v26_1.csv` | UTF-8 | annual conflict, territory, and source-side actor IDs |
| UCDP Actor 26.1 | `Actor_v26_1.csv` | Latin-1 | referential-integrity validation; names never cross the public boundary |
| Organized Violence Country-Year 26.1 | `OrganizedViolenceCYDataSet26_1.csv` | UTF-8 | Pakistan and Myanmar annual low/best/high fatality bounds |

Each acquisition receipt binds the exact URL, ZIP byte count and SHA-256, exact
member name, decompressed byte count and SHA-256, HTTP `Last-Modified`, retrieval
time, version, transport policy, and byte ceilings. Redirects are disabled. TLS
certificate and hostname verification are required. The safe-fetch layer pins
validated public IP addresses, while the ZIP layer independently enforces one
flat member, CRC validity, supported compression, decompressed-size caps, and a
compression-ratio ceiling.

An annual archive more than 550 days older than its authenticated retrieval
clock fails closed. A missing `Last-Modified`, unknown actor ID, changed CSV
header, changed version, incomplete country-year matrix, extra ZIP member, or
receipt/hash mismatch also fails closed. Failure never produces zero fatalities,
an empty “healthy” history, or a fresh timestamp.

## Public contract

The public JSON schema is
[`protocol/ucdp-aggregate-v1.schema.json`](../protocol/ucdp-aggregate-v1.schema.json).
It permits only:

- Balochistan conflict-year rows where UCDP location is Pakistan and territory
  is exactly Balochistan;
- Myanmar conflict-year rows where the reviewed UCDP location token is present;
- Myanmar aggregate territory labels only from the reviewed version 26.1
  vocabulary (a new label fails closed for review);
- annual conflict ID, aggregate territory name, and distinct source-side actor
  IDs;
- Pakistan and Myanmar country-year state-based, non-state, and one-sided
  uncertainty bounds; and
- an explicit total derived by adding the three category bounds.

The Pakistan country-year bounds describe all organized violence within
Pakistan's borders. They are **not** Balochistan-only counts. The Balochistan
surface contains conflict-year identifiers but does not derive provincial death
totals from the country-wide table.

The contract and schema prohibit event coordinates, village names, event dates,
event narratives, source articles, person records, live or tactical fields, and
drug-actor inference. No UCDP row is joined to NarcoScope by actor, route,
project, guilt, or causality. A downstream product may use only coarse geography
and annual historical time as contextual facets, and must preserve each
product's separate provenance and rights.

## Baloch movement semantics

“Baloch movement” is an umbrella research concept, not one modeled actor. The
bundle carries five non-interchangeable lanes:

1. civic society — unavailable from these UCDP inputs;
2. electoral and political activity — unavailable from these UCDP inputs;
3. armed-conflict organizations — distinct UCDP side-B actor IDs by
   conflict-year;
4. state authorities — distinct UCDP side-A actor IDs by conflict-year; and
5. human-rights documentation — unavailable from these UCDP inputs.

Unavailable lanes require separate rights-reviewed evidence. Political parties,
peaceful advocacy, human-rights allegations, administrative designations, and
armed organizations must never be merged merely because they share a geography
or a broad political theme.

## Deterministic offline replay

The input directory must contain all six private evidence files:

```text
armed_conflict.zip
armed_conflict.receipt.json
actor_registry.zip
actor_registry.receipt.json
organized_country_year.zip
organized_country_year.receipt.json
```

Validate without publishing:

```bash
python3 -m scripts.ucdp_bulk_pull check \
  --input-dir /private/evidence/ucdp-26.1
```

Build a review artifact:

```bash
python3 -m scripts.ucdp_bulk_pull build \
  --input-dir /private/evidence/ucdp-26.1 \
  --output /review/ucdp-aggregate-v1.json
```

The same ZIPs and receipts produce identical JSON bytes and the same bundle ID.
The generated clock is the latest authenticated retrieval time, not the replay
time.

## Production acquisition step

The repository status is `adapter_ready`, not `live`. To acquire the pinned
archives, create a non-group/non-world-accessible directory and explicitly opt
into network access:

```bash
mkdir -m 700 /private/evidence/ucdp-26.1
python3 -m scripts.ucdp_bulk_pull build \
  --fetch \
  --evidence-output-dir /private/evidence/ucdp-26.1 \
  --output /review/ucdp-aggregate-v1.json
```

Before any `live` promotion, independently review the six immutable evidence
files, validate the derived bytes against the schema, record an exact release
receipt, run the public scrub and egress tests, deploy the exact revision, and
verify the served bytes. This branch performs none of those deployment or
promotion steps.
