# Censored Planet ingestion — LIVE

**Status: live signal, no registration needed.** The earlier "needs a
registration form" assumption was wrong — Censored Planet now exposes an
**open, keyless GraphQL API** at `https://data.censoredplanet.org/query`.

## What we query (introspected 2026-07)

- `interferenceRateByCountry(range){country, unexpectedRate}` — China's
  interference rate (headline: ~4.84%).
- `cenalertTimeseries(range, country: "CN"!){date, value}` — CN alert-intensity
  time series (~70 points).
- `cenalertEvents(range, country){...}` — discrete censorship *events*. For
  China this is intentionally **empty**: CenAlert flags anomalous *spikes*, and
  China's censorship is a continuous baseline, not episodic. Persistent-not-
  event-driven is itself a finding.
- `DateRange = {startDate, endDate}` (both `Date!`). Note `country` on
  timeseries is `String!` (non-null).

## Value

An independent second method (remote DNS/HTTP side-channel, 95k+ vantage
points) confirming OONI's active-probe measurement — methodological
triangulation, the core of the Undertext many-vantage thesis. The two rates
are NOT the same number (different denominators/methods); the point is two
independent methods both detecting China interference.

## Files (live)

- `collectors/censored_planet.py`, `scripts/censored_planet_pull.py`
- `.github/workflows/censored-planet-refresh.yml` (daily)
- `readings/censored-planet-latest.json` + `-history.jsonl`
- surfaced as an "Independent cross-check — Censored Planet" card on the GFW
  Live page.
