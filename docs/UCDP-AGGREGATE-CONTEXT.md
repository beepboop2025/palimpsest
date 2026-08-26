# UCDP annual aggregate context

Palimpsest has an `adapter_ready` path for three registry-pinned Uppsala
Conflict Data Program (UCDP) version 26.1 annual bulk archives. The checked-in
acquisition lock starts at `review_required`; this repository does not claim
that UCDP observations are live or publication-approved. The adapter is an
aggregate historical-context surface for Balochistan and Myanmar. It is not an
event feed, actor dossier, route monitor, early-warning system, or attribution
engine.

## Rights and citation gate

Every acquisition captures the exact UCDP Download Center page and a canonical
receipt containing its URL, byte count, SHA-256, observation time, and transport
policy. An independent Palimpsest review must then bind that page snapshot, its
receipt, a review date, an expiry date, the three input archives, their receipts,
and all three required citations into
[`config/ucdp_acquisition_lock.json`](../config/ucdp_acquisition_lock.json).
The lock is closed by
[`protocol/ucdp-reviewed-acquisition-lock-v1.schema.json`](../protocol/ucdp-reviewed-acquisition-lock-v1.schema.json)
and independently revalidated by the strict typed parser.

The reviewed Git lock is Palimpsest's explicit approval anchor. It is not a
cryptographic signature by UCDP, proof that UCDP approved the acquisition, or a
substitute for checking the captured rights page. The lock permits only annual
aggregate context with attribution, and its exact SHA-256 travels in the public
bundle. The required references are:

- rights and citation index: <https://ucdp.uu.se/downloads/>
- license identified by the reviewed decision: <https://creativecommons.org/licenses/by/4.0/>
- Davies, Pettersson and Oberg, “Organized violence 1989-2025, and violent political protests”: <https://doi.org/10.1093/jopres/xjag046>
- Gleditsch et al., “Armed Conflict 1946-2001: A New Dataset”: <https://doi.org/10.1177/0022343302039005007>
- Sundberg and Melander, “Introducing the UCDP Georeferenced Event Dataset”: <https://doi.org/10.1177/0022343313484347>

## Evidence boundary

The private acquisition layer accepts exactly these versioned archives:

| Input | Exact member | Encoding | Public use |
|---|---|---|---|
| UCDP/PRIO Armed Conflict 26.1 | `UcdpPrioConflict_v26_1.csv` | UTF-8 | annual conflict, territory, and source-side actor IDs |
| UCDP Actor 26.1 | `Actor_v26_1.csv` | Latin-1 | referential-integrity validation; names never cross the public boundary |
| Organized Violence Country-Year 26.1 | `OrganizedViolenceCYDataSet26_1.csv` | UTF-8 | Pakistan and Myanmar annual low/best/high fatality bounds |

Each acquisition receipt binds the exact URL, ZIP byte count and SHA-256, exact
member name, decompressed byte count and SHA-256, HTTP `Last-Modified`, retrieval
time, version, transport policy, and byte ceilings. These Palimpsest receipts
prove captured-byte identity and replay consistency; they do not authorize
publication. Redirects are disabled. TLS certificate and hostname verification
are required. The safe-fetch layer pins validated public IP addresses, while the
ZIP layer independently enforces one flat member, CRC validity, supported
compression, decompressed-size caps, and a compression-ratio ceiling.

Publication uses separate current, publication, retrieval, source, rights
observation, review, and expiry clocks. The gate requires:

- an explicit publication clock no earlier than the rights review and no more
  than 300 seconds ahead of the current build clock;
- both the publication and current clocks to be within the rights decision's
  validity window;
- rights observation, review, and expiry clocks in order, with future clocks
  rejected beyond the same 300-second allowance;
- all three retrieval clocks within 900 seconds of one another; and
- each input's `Last-Modified` and retrieval clock no more than 550 days old at
  both publication and build time, and not future-dated beyond 300 seconds.

A missing or duplicate `Last-Modified`, unknown actor ID, changed CSV header,
changed version, cumulative-total disagreement, incomplete country-year matrix,
extra ZIP member, expired decision, stale clock, or receipt/hash/lock mismatch
fails closed. Failure never produces zero fatalities, an empty “healthy”
history, or a fresh timestamp.

## Public contract

The public JSON schema is
[`protocol/ucdp-aggregate-v1.schema.json`](../protocol/ucdp-aggregate-v1.schema.json).
It permits only:

- Balochistan conflict-year rows where UCDP location is Pakistan and territory
  is exactly Balochistan;
- Myanmar conflict-year rows where the reviewed UCDP location token is present;
- Myanmar aggregate territory labels only from the reviewed version 26.1
  vocabulary (a new label fails closed for review);
- annual conflict ID, aggregate territory name, and no more than 64 distinct
  source-side actor IDs per side;
- Pakistan and Myanmar country-year state-based, non-state, and one-sided
  uncertainty bounds; and
- an explicit total that must agree with both the three category bounds and the
  UCDP cumulative total fields.

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

## Private acquisition and offline replay

Network acquisition is a private-only step. It writes eight immutable files and
does not build a public artifact:

```bash
mkdir -m 700 /private/evidence/ucdp-26.1
python3 -m scripts.ucdp_bulk_pull acquire \
  --fetch \
  --evidence-output-dir /private/evidence/ucdp-26.1
```

The evidence directory contains:

```text
armed_conflict.zip
armed_conflict.receipt.json
actor_registry.zip
actor_registry.receipt.json
organized_country_year.zip
organized_country_year.receipt.json
rights-page.snapshot.html
rights-page.receipt.json
```

`archive-check` verifies private byte, ZIP, receipt, and rights-snapshot
self-consistency. It deliberately does not consult the reviewed lock and cannot
authorize a public build:

```bash
python3 -m scripts.ucdp_bulk_pull archive-check \
  --input-dir /private/evidence/ucdp-26.1
```

After an independent reviewer has inspected the exact evidence and committed an
approved lock, `check` can evaluate publication eligibility without writing an
artifact:

```bash
python3 -m scripts.ucdp_bulk_pull check \
  --input-dir /private/evidence/ucdp-26.1 \
  --publication-at 2026-08-27T00:00:00Z
```

With the same reviewed evidence, build the public review artifact outside the
private evidence directory:

```bash
python3 -m scripts.ucdp_bulk_pull build \
  --input-dir /private/evidence/ucdp-26.1 \
  --publication-at 2026-08-27T00:00:00Z \
  --output /review/ucdp-aggregate-v1.json
```

The same ZIPs, receipts, rights snapshot, approved lock, and publication clock
produce identical JSON bytes and the same bundle ID. `generated_at` is the
explicit publication clock; `latest_retrieved_at` separately preserves the
latest receipt-bound input retrieval time.

`check` and `build` deliberately do not accept a caller-selected lock path.
Publication authority comes only from the repository-controlled
`config/ucdp_acquisition_lock.json`; accepting a temporary lock would let
self-issued bytes approve themselves.

Running `check` with no evidence only validates the adapter configuration and
reports the lock state. The repository remains `adapter_ready`, not `live`, while
the checked-in lock says `review_required`. Before any live promotion, reviewers
must also record an exact release receipt, run public scrub and egress tests,
deploy the exact revision, and verify the served bytes. This branch performs no
acquisition, deployment, or promotion.
