# Scheduled measurement cadence and review metadata

The Hetzner timer polls this runner hourly, with up to three minutes of jitter.
OONI GFW, in-path interference and IODA use a 90-minute successful-run guard;
normal collection therefore occurs about every two hours. Their public catalog
retains PT2H and the existing five-hour stale deadline. The previous six-hour
guard exceeded that deadline even when every request succeeded. Failed or empty
collections still preserve last-good bytes and retry in a later timer cycle.

A network round uses eight OONI GFW queries, seven in-path OONI queries and two
IODA queries. The normal two-hour schedule is approximately 180 OONI and 24 IODA
requests/day. Existing per-query caps, OONI throttling and bounded 429 retries
remain unchanged; there are no new query types, parallel requests or direct
probes. OONI's API guidance asks for a modest request rate and excludes bulk raw
measurement transfers: https://measurements.ooni.io/ and
https://github.com/ooni/data . A successful run cannot repeat within 90 minutes;
long-running jobs and upstream refusal can delay the advertised target.

`processors/board_alarm.py` keeps the historical workflow cadence table intact.
Newly rendered wall-clock explanations use a separate conservative maximum of
16 scheduled readings/day for OONI and IODA, derived from the 90-minute guard.
The usual hourly polling cadence is slower (12/day). Reading-based guarantees,
detector calculations and every historical row remain unchanged; the new
`readings_to_days_basis` metadata explains the current conversion.

The peer-context ranker runs after the cached peer warehouse, at six-hour
cadence. It reads existing GreatFire/OONI/CDT metadata and writes only the already
published `peer-context-rank-latest.json` and append-only movement history.
`config/public_data_catalog.json` declares this product a review rank, and
`tests/test_publication_contract.py` registers those public metadata fields.
The existing publisher overlays these tracked files and applies the same source
rights scrub; the ranker receives no publication exception. Its
`automatic_publication=prohibited`, `human_review_required=true` and
`generative_model=prohibited` flags are retained: output is a review queue, never
an approved finding, event-analysis sentence or automatic Situation-desk join.
The fixed fixture integration test disables network access, checks the rights
scanner, retains every prior history byte, and verifies unchanged warehouse
files and no duplicate movement rows.

Install a reviewed immutable measurement source and matching marker under the
existing guarded host procedure; do not edit the active checkout or replace its
marker during a running refresh. Publisher/controller source rotation remains a
separate deployment step. A code merge alone does not activate the new cadence.
After activation, verify successful job stamps and source timestamps against the
public catalog, and confirm that review suggestions still carry their gates.
