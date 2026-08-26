# BRI WDI national-context observations

Status: **production-verified public release**. Exact Release A bytes were
cache-busted and re-fetched from `palimpsest.info` at
`2026-08-26T15:55:34Z`; the immutable publication receipt is linked below.

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
- A nonempty World Bank `obs_status` is accepted only when it is exactly `F`;
  that row is emitted as `forecast`, never `observed`. Any other nonempty
  status fails closed pending review.
- `obs_status`, `footnote`, and `scale` are preserved verbatim in every row and
  participate directly in the authenticated observation ID.
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
response hash to the exact request URL. It also carries an acquisition ID that
binds the canonical raw-response sidecar: exact request URL, method, user agent,
TLS and redirect policy, response length and hash, and the post-response
retrieval clock. The collection and observation IDs are deterministic hashes
over canonical JSON.

Downstream consumers must keep the three evidence states distinct: `observed`
is a numeric source value without a forecast marker; `forecast` is a numeric
source value carrying `obs_status=F`; and `unavailable` is a source null, never
zero or an imputed value. Country-period joins are context only and cannot be
used for project, actor, corridor, or causal attribution.

## Offline review

Validate the reviewed registry without network access:

```bash
PYTHONPATH=. python3 -m scripts.bri_wdi_pull check
```

Validate exact saved response bytes with their canonical acquisition sidecar:

```bash
PYTHONPATH=. python3 -m scripts.bri_wdi_pull check \
  --input /path/to/exact-wdi-response.json \
  --receipt-input /path/to/exact-wdi-response.receipt.json \
  --start-year 1960 \
  --end-year 2025
```

Build a deterministic review artifact. There is deliberately no public output
default:

```bash
PYTHONPATH=. python3 -m scripts.bri_wdi_pull build \
  --input /path/to/exact-wdi-response.json \
  --receipt-input /path/to/exact-wdi-response.receipt.json \
  --start-year 1960 \
  --end-year 2025 \
  --output /path/to/review/bri-economic-observations.json
```

## Explicit live acquisition

Outbound access occurs only with `--fetch`:

```bash
PYTHONPATH=. python3 -m scripts.bri_wdi_pull build \
  --fetch \
  --start-year 1960 \
  --end-year 2025 \
  --raw-output /path/to/controlled/raw/wdi-response.json \
  --receipt-output /path/to/controlled/raw/wdi-response.receipt.json \
  --output /path/to/review/bri-economic-observations.json
```

The transport pins the reviewed HTTPS host, verifies TLS, disables redirects,
enforces response/row/year/series limits, and samples the retrieval clock after
the response returns. Both `--raw-output` and `--receipt-output` are mandatory.
They are immutable: an existing path is accepted only when its bytes are
identical, and differing evidence is never replaced. Offline replay requires
both files and verifies canonical receipt encoding, request scope, retrieval
clock authentication, response length, and response hash before parsing.

The admitted live range ends at 2025 because the official source-2 time
dimension currently spans `YR1960` through `YR2025`. The API silently clips a
request for 2026 instead of returning an explicit missing period, so release
operators must confirm the source time dimension before advancing this bound.
The current environmental series is `EN.GHG.CO2.PC.CE.AR5`; the former
`EN.ATM.CO2E.PC` code is now available only from the WDI archive and is
intentionally excluded from this source-2 bundle.

The generated bundle is explicitly derived. An identical rebuild is accepted;
replacing a differing derived artifact requires `--replace-derived`. None of
these files is published automatically. The fetched bytes must still be
reviewed by the release operator; a successful fetch is not publication proof.

## Published release

The admitted release was retrieved at
`2026-08-26T13:17:34.790676Z` from the reviewed WDI API scope and carries source
release upper bound `2026-07-13T23:59:59Z`. Its normalized public bundle is
[`readings/bri-economic-observations-latest.json`](../readings/bri-economic-observations-latest.json):

- 18 indicators across China, Myanmar and Pakistan;
- 3,564 country-year-indicator rows covering 1960 through 2025;
- 1,940 observed rows, zero forecast rows and 1,624 explicitly unavailable rows;
- collection ID
  `b279cf29edafa4e6e8f3a5b817239a1d9a5169a12cefb609c231067d72d65d1e`;
- normalized-bundle SHA-256
  `68b9f96e3cfc1e5692b4305c93b42b64dd45d065655ccade6947e086285dc099`.

The exact 870,155-byte source response and its acquisition receipt remain in
the controlled provenance store; they are not copied into the public site.
World Bank WDI redistribution is attributed under CC BY 4.0 in the normalized
bundle.

The [canonical Pages publication receipt](../.well-known/receipts/bri-wdi-pages-publication-v1.json)
has SHA-256
`239a6b5e1496eaf3f97d8d0502cbf1581f24b02ba386d7d806adc79a877d2a06`
and validates under the
[receipt schema](../protocol/bri-wdi-pages-publication-v1.schema.json). It binds:

- exact Release A commit `14b06772dfed6cdc736279c9ab61b444e5846598`;
- successful [workflow run 32984946320](https://github.com/beepboop2025/palimpsest/actions/runs/32984946320),
  Pages package/deploy jobs, artifact digests and deployment identity;
- the public WDI bundle, its schema and the reviewed series registry to their
  byte lengths and SHA-256 digests after HTTP 200 responses; and
- served-byte verification at `2026-08-26T15:55:34Z`, with the point-in-time
  freshness window ending `2026-08-27T15:55:34Z`.

That last window is availability evidence, not an economic-data timestamp and
not continuous monitoring. After `fresh_until`, the receipt remains durable
historical proof of the exact Release A publication, while claims about current
availability require another public re-probe. The separate WDI acquisition
clock (`2026-08-26T13:17:34.790676Z`) and source release upper bound
(`2026-07-13T23:59:59Z`) remain authoritative for data currency.

## Publication and refresh checklist

The Release A proof completed the initial promotion of `world_bank_wdi` to
`live`. Every later source refresh or production release must repeat the same
fail-closed sequence:

1. retain the exact raw response and its canonical acquisition sidecar in the
   controlled provenance store without overwriting either file;
2. review any indicator-title or response-shape drift instead of weakening the
   parser;
3. validate the generated bundle against the protocol and confirm the request,
   observation, and collection hashes;
4. publish through the normal exact-main release path;
5. verify the served bytes and discovery/API surfaces; and
6. publish a new receipt instead of editing this immutable Release A proof.

A repository artifact, successful build, or fresh page timestamp alone is not
a live-coverage claim. Production status requires an exact-main Pages
deployment receipt and a byte-for-byte fetch of every canonical resource named
by that receipt.
