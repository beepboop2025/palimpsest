# BRI WDI national-context observations

Status: **repository-ready, not fetched or published**.

This tranche provides a bounded World Bank World Development Indicators adapter
for China (`CHN`), Myanmar (`MMR`), and Pakistan (`PAK`). It is an annual,
country-aggregate economic context layer. It is not a BRI project register, a
measure of Chinese involvement, or evidence that BRI caused a national outcome.

## Evidence boundary

- Scope is fixed to national aggregates. Project, organization, person,
  respondent, route, coordinate, and tactical fields do not exist in the
  protocol.
- Country-period context may be joined to the BRI observatory only as context.
  It may not attribute a value to a project, actor, corridor, or policy.
- A World Bank `null` remains an `unavailable` observation with a null value. It
  is never converted to `0`, carried forward, or silently removed.
- The reviewed dataset-level terms are CC BY 4.0 with attribution to World Bank,
  World Development Indicators. Dataset reuse terms do not change the authority
  or independence of an indicator's upstream producer.

The source registry is [`config/bri_wdi_series.json`](../config/bri_wdi_series.json),
the observation implementation is
[`core/bri_observation.py`](../core/bri_observation.py), and the public bundle
shape is
[`protocol/bri-economic-observations-v1.schema.json`](../protocol/bri-economic-observations-v1.schema.json).

## Clocks and identities

Each row preserves three distinct time concepts:

1. `period_start` / `period_end`: the calendar year measured;
2. `source_release_upper_bound`: the earlier of the World Bank dataset-wide
   `lastupdated` end-of-day and the retrieval clock; and
3. `retrieved_at`: when Palimpsest possessed the exact response bytes.

The API does not provide a per-row release timestamp, so `lastupdated` is an
upper bound, not an invented exact publication time. Every row carries the full
response SHA-256, a canonical source-row SHA-256, and a request ID binding the
response hash to the exact request URL. The collection and observation IDs are
deterministic hashes over canonical JSON.

## Offline review

Validate the reviewed registry without network access:

```bash
PYTHONPATH=. python3 -m scripts.bri_wdi_pull check
```

Validate exact saved response bytes with their post-response retrieval clock:

```bash
PYTHONPATH=. python3 -m scripts.bri_wdi_pull check \
  --input /path/to/exact-wdi-response.json \
  --retrieved-at 2026-08-26T10:30:00Z \
  --start-year 1960 \
  --end-year 2026
```

Build a deterministic review artifact. There is deliberately no public output
default:

```bash
PYTHONPATH=. python3 -m scripts.bri_wdi_pull build \
  --input /path/to/exact-wdi-response.json \
  --retrieved-at 2026-08-26T10:30:00Z \
  --start-year 1960 \
  --end-year 2026 \
  --output /path/to/review/bri-economic-observations.json
```

## Explicit live acquisition

Outbound access occurs only with `--fetch`:

```bash
PYTHONPATH=. python3 -m scripts.bri_wdi_pull build \
  --fetch \
  --start-year 1960 \
  --end-year 2026 \
  --raw-output /path/to/controlled/raw/wdi-response.json \
  --output /path/to/review/bri-economic-observations.json
```

The transport pins the reviewed HTTPS host, verifies TLS, disables redirects,
enforces response/row/year/series limits, and samples the retrieval clock after
the response returns. `--raw-output` is mandatory for live acquisition, retains
the exact bytes authenticated by the request receipt, and is never published
automatically. The fetched bytes must still be reviewed by the release operator;
a successful fetch is not publication proof.

## Publication checklist

Before changing `world_bank_wdi` from `adapter_ready` in the BRI source registry:

1. retain the exact raw response and its SHA-256 in the controlled provenance
   store;
2. review any indicator-title or response-shape drift instead of weakening the
   parser;
3. validate the generated bundle against the protocol and confirm the request,
   observation, and collection hashes;
4. publish through the normal exact-main release path; and
5. verify the served bytes and discovery/API surfaces before describing the
   source as live.

No current public artifact or live-coverage claim is created by this tranche.
