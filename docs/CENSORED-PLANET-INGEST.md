# Censored Planet ingestion — status and how to finish it

**Status: blocked on a one-time human registration, not on code.** This is the
honest reason it isn't a live signal yet, and what it takes to turn it on.

## Why it's not automated

Censored Planet (University of Michigan) publishes weekly global censorship
scans (Satellite/DNS, Hyperquack/HTTP-HTTPS). Unlike OONI, it has **no live
REST API** — access is **bulk `.tar.gz` file download** from
`https://data.censoredplanet.org/raw`, and historically that download is gated
behind a **short registration form** (name / email / affiliation). A form
submission that creates an access grant is a human action; automating it would
be circumventing their access process, which I won't do. So this needs one
manual step from the operator.

It's also **weekly and lagged**, so it's enrichment (deeper DNS/HTTP evidence
per site), not a live signal — the OONI pull already covers "what's blocked in
CN right now."

## To finish it (one-time, ~10 minutes)

1. Register at **https://data.censoredplanet.org/** (or email
   `censoredplanet@umich.edu`) — request raw-data access, and while there,
   **confirm republication terms** for a public-good observatory (the data
   page has no machine-readable license).
2. Once you can download, the CN weekly archives are the files whose names
   carry the technique + `CN` country code under
   `https://data.censoredplanet.org/raw` (Satellite for DNS poisoning,
   Hyperquack for HTTP/SNI blocking). Format: `.tar.gz` of JSON.

## The scaffold to write once access exists

Mirror the OONI signal's shape (collector + runner + workflow), but as a
**weekly** job:

- `collectors/censored_planet.py` — list `data.censoredplanet.org/raw`, pick
  the newest `*-CN-*` Satellite + Hyperquack archives, stream-download,
  `tarfile` + `json` parse per-domain reachability. Fail-soft per file.
- `scripts/censored_planet_pull.py` — diff domain reachability vs the prior
  week, compute a CN DNS/HTTP block-rate, cross-reference OONI's
  `top_blocked` (does Censored Planet independently confirm the same sites?),
  write `readings/censored-planet-latest.json`. Abstain if the download or
  parse yields nothing.
- `.github/workflows/censored-planet-refresh.yml` — `cron: weekly`, plus a
  **repository secret** if the download needs an auth token from registration.
  Note: multi-GB archives may exceed a GitHub-runner's disk/time — if so, run
  this one on the box (it's public-data ingest, no probing, so it doesn't
  violate the no-box-probe rule) rather than in Actions.

## Value once on

A second, methodologically-independent confirmation of OONI's China blocking
(DNS-poisoning + SNI evidence from 95k+ vantage points), which strengthens the
"many-vantage differential" story — exactly what the Undertext method wants.
But OONI alone is a complete, live signal; this is depth, not a dependency.

## What was built instead (live now)

- **OONI GFW signal** (`ooni-gfw`) — live, 6-hourly, the primary quantitative
  measure. History backfilled 2 months via `scripts/ooni_gfw_backfill.py`.
- **net4people/bbs event stream** (`net4people`) — live, 12-hourly, the
  qualitative event log.
- **GreatFire** cross-check — deferred: no cleanly-accessible public API
  (probed endpoints return 404/000); not worth scraping an undocumented
  endpoint into a public signal.
