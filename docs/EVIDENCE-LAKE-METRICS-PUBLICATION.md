# Evidence-lake metrics publication boundary

Palimpsest publishes a small four-lane `bulk.public-metrics.v1` subset, not the
private evidence lake. The committed seed at
`readings/evidence-lake-metrics-latest.json` contains aggregate counts, reviewed
source citations and fail-closed gates. It contains no raw or Parquet rows, private
collector receipts, credentials, local paths or private warehouse locator.

## Current state

The committed file is the byte-exact, point-in-time Neo projection read at
`2026-08-28T11:13:49Z` (SHA-256
`221fce3463261470f2941e8d35e479876737431b33f87eb1e0411baac882523b`). Its
`palimpsest_release_files` value of 38,255 is the producer's catalog at that same
clock, not this candidate Git tree's file count. No value was spliced from a later
clock. It makes no claim that Hetzner import, Railway deployment or continuous
refresh is active.

The v1 totals include only WDI, UNODC IDS, OFR STFM and the manifest-only Binance
lane named by the closed schema. Neo's verified UCDP and GDELT materializations are
deliberately excluded because they require a separately reviewed public-metrics v2;
they must not be silently mixed into the v1 total. The zero Telegram field describes
only Neo's blocked large-corpus lane, not Palimpsest's separate bounded,
privacy-reviewed public-channel observations.

The HMAC receipt contract is versioned at
`protocol/evidence-lake-metrics-producer-receipt-v1.schema.json`, but no production
receipt is committed yet. The owner-controlled host key and matching GitHub secret
have not been provisioned. A placeholder or test signature would weaken the boundary,
so the route remains pending instead.

The future host origin is code-pinned as `EVIDENCE_LAKE_SNAPSHOT` in
`scripts/import_host_snapshot.py`, and its exact Caddy handler is declared in
`ops/caddy/palimpsest-host-snapshots.caddy`. The spec is deliberately held in
`PENDING_SNAPSHOTS`; normal refreshes do not request it. This prevents an unproven
host route from breaking the existing publication workflow or being described as
live.

The intended flow is:

```text
private evidence lake
  -> aggregate-only four-lane bulk.public-metrics.v1 subset
  -> shared-secret HMAC admission receipt bound to exact projection bytes
  -> two exact no-store Hetzner paths
  -> receipt / projection / receipt GitHub admission
  -> committed Palimpsest reading and producer receipt
  -> rights-gated immutable Railway static bundle
```

Browsers fetch the committed Palimpsest reading. They do not query the private host
or warehouse directly.

## Admission contract

The importer admits only this exact pair:

`https://api.seiche.info/palimpsest/evidence-lake-metrics/evidence-lake-metrics-latest.json`

`https://api.seiche.info/palimpsest/evidence-lake-metrics/evidence-lake-metrics-producer-receipt.json`

Redirects are disabled. The projection is capped at 64 KiB and the receipt at
16 KiB. When the route is active, an `EVIDENCE_LAKE_METRICS_HMAC_KEY` outside the
32-through-4096-byte bound fails before any network request. Admission then
fetches receipt A, the projection, and receipt B; the receipt bytes must be identical
or the complete triplet is retried, at most three times.

Shared-secret receipt admission requires:

- closed canonical `bulk.public-metrics-producer-receipt.v1` bytes;
- HMAC-SHA256 verified with `compare_digest` over the canonical receipt core;
- reviewed key ID `neo-public-metrics-2026-08`;
- producer ID `palimpsest-bulk-data-plane` and the exact reviewed producer release;
- raw projection byte count and SHA-256 bindings;
- exact `generated_at`, `edition`, and `metrics_sha256` bindings; and
- a private-status SHA-256 commitment without publishing the private status.

After shared-secret receipt admission, projection validation requires:

- top-level and nested closed shapes;
- canonical WDI, UNODC, OFR and manifest-only Binance lane order;
- exact reviewed product views, publication states and upstream citations;
- WDI and UNODC eligible counts equal to their queryable counts;
- OFR eligible rows remain zero while its gate is `review-required`;
- the Neo large-corpus Telegram field and crypto payload records remain zero while
  blocked;
- summary totals equal the canonical lane sums;
- `metrics_sha256` equals SHA-256 of canonical JSON plus a final newline for exactly
  `{summary, lanes, gates}`; and
- `edition` equals the first 16 hexadecimal characters of that digest.

Unknown keys, noncanonical receipts, an ambient URL, a private path, a weak or wrong
key, producer-release drift, receipt races, count drift, gate drift, rollback,
equal-clock equivocation and an invalid digest all fail closed. Projection and receipt
are staged and committed as one local pair; an interrupted write cannot publish half
the pair. The existing 404 rule is not weakened: after activation, disappearance of
an already published host snapshot is an error rather than a synthetic zero or silent
fallback.

The browser does not possess the HMAC key and therefore does not perform shared-secret
admission. It validates the closed projection shape and recomputes the inner metrics
digest only. Browser copy says “metrics digest verified,” never “signature verified.”

HMAC is symmetric: both the Neo host and the GitHub verifier hold the same secret.
It proves that the receipt came from a holder of that admission key; it is not
asymmetric non-repudiation and does not, by itself, prove exclusive producer identity.
Exact release pins, private-status commitment, HTTPS pair acquisition and downstream
schema/high-water checks remain separate gates.

## Activation gate

Do not activate import from repository intent alone. Complete these steps in order:

1. Re-catalog the exact clean Palimpsest candidate on the reviewed producer release,
   then generate the projection from its validated private status wrapper.
2. The owner provisions a new random HMAC key in a descriptor-safe host secret file
   and the matching `EVIDENCE_LAKE_METRICS_HMAC_KEY` GitHub Actions secret. Never
   commit, print, or transmit that key through a receipt.
3. Generate the closed producer receipt with key ID
   `neo-public-metrics-2026-08`; publish the projection and receipt as regular,
   single-link files under `/var/lib/palimpsest/evidence-lake-metrics/current`.
4. Deploy the two exact Caddy paths. Do not add a wildcard or directory browser.
5. Independently fetch receipt / projection / receipt with redirects disabled.
   Confirm byte-stable receipts, `Content-Type`, `Cache-Control: no-store,
   no-transform`, both byte ceilings, UTC clocks, HMAC, schema, lane arithmetic and
   digest.
6. Run `tests/test_import_host_snapshot.py` and
   `tests/test_evidence_lake_metrics.py` against the candidate bytes.
7. In the same reviewed activation commit, stage the shared-secret-admitted
   `readings/evidence-lake-metrics-producer-receipt.json` and add that exact path to
   `ops/railway/build_release_manifest.py` `CRITICAL_PATHS` and the matching static
   release contract test. Add the receipt path to the static server's exact `no-store`
   set and response-header regression as well, so caches cannot serve a receipt from a
   different release than the mutable projection. The Railway manifest must bind the
   receipt bytes; do not make the path critical while the production receipt is still
   absent.
8. In `scripts/import_host_snapshot.py`, change the single active-tuple suffix
   `+ ()` to `+ PENDING_SNAPSHOTS`.
9. Let the existing GitHub refresh verify, import, test, scrub, seal and commit
   the pair. Verify the resulting Railway release and served bytes separately before
   describing continuous publication as active.

Until step 8, tests require that normal refreshes never request the pending route.
No environment variable or CLI flag can bypass this activation gate.
