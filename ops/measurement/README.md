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

## Dedicated Generative Firewall schedule

The 2026-09-13 recovery panel reached the generic 12-minute timeout before its
660 reads completed. No new observation or registry run was admitted. GFI now
uses a separate `generative-firewall` scope, private state/refresh lock, and daily
08:00 UTC timer. The core and regional scopes cannot dispatch a GFI request.
The dedicated scope treats a failed or empty run as a service failure so the
watchdog can detect it. The daily timer is the cadence guard; a prior slow
completion cannot make the next daily run skip because of a 24-hour success
stamp. The service has no automatic restart or rapid failure retry.

The request policy remains six workers, 44 prompt arms, three preregistered
models, five samples per cell, 700 output tokens, 25-second request timeout,
and the existing maximum two attempts. This is 660 samples and at most 1,320
HTTP attempts per run. Six workers processing two timed-out attempts plus the
one-second retry delay can take approximately `660 * 51 / 6 = 5,610` seconds
(93.5 minutes). The dedicated outer bound is therefore 100 minutes, with a
110-minute service ceiling including checkout/admission. The outer bound is
authoritative even if a transport's socket timeout does not limit its entire
wall time. The existing OpenRouter $5/day cap remains in place. No new models,
concurrency increase, prompt changes, classifier changes or denominator changes
are introduced. Count-only progress every 30 samples records elapsed time and
per-model completed/abstained counts; prompts and responses stay out of logs.

Before activation, the root operator must install the exact reviewed merged
source and matching marker `/etc/palimpsest/gfi-commit`, and bind the source
read-only at `/opt/palimpsest-gfi/source`. Follow the existing measurement
source ownership contract: the checkout is owned by `palimpsest`, so the shared
helper's Git commands do not cross Git's repository ownership boundary. Its
private state is bound from the large volume at `/var/lib/palimpsest/gfi-refresh`,
owned by `palimpsest` with mode 0700. Both mounts must be present before service
activation. Keep the source marker/configuration root controlled, and load the
existing root-owned, mode 0600 OpenRouter environment only through the GFI service.

Install the stable `/var/lib/palimpsest/readings/.eval-registry.jsonl.lock` once
as `palimpsest:palimpsest-analysis`, mode 0664, without replacing any existing
lock inode. The five existing GFI outputs were observed as UID/GID 10001,
mode 0664; transcripts were absent. Under data -> registry locks, the root
installer normalizes only those existing output UIDs to 1001, preserving their
group, modes, access ACLs, inodes and exact bytes. The service has supplementary
`palimpsest-analysis` membership. New outputs use UID 1001/GID 1001 and mode 0644.
Before models, `--check-host` validates the installed locks, registry and file
ownership; publication preserves all existing ownership/mode/ACL metadata.

Hold the existing core refresh lock while installing the matching new core
helper/source that excludes GFI. Verify no old core GFI process is running
before enabling the new daily timer. Do not start the new service against an
old core helper that could still initiate a second panel. Verify shipping
`python -m scripts.preregister_gfi_v2 --check` and byte equality of the protocol
to the already-public preregistration on both public origins before any model
run. The runtime progress change does not alter the committed classifier hash.

The model run holds only its private refresh lock. Before collection, the longer
exact source/host history prefix is retained and copied into private
`gfi-history-before.jsonl`; divergent histories stop before model calls. This
retains all 46 reviewed source rows when the host has only the older prefix of 37 rows.
Promotion verifies every historical date against that capture and current host.
Only the existing daily-point upsert may replace the current day's prior point;
its prior bytes remain in the private capture.

Promotion holds data -> eval-registry locks through the final replacement,
verifies the full transcript/label/matrix seals, requires the current protocol,
and checks that the host eval registry is an exact byte prefix of the candidate.
A concurrent append/divergence rejects promotion before any host file changes.
Observation clocks must advance, except an exact-byte replay of the identical
reading can finish a partial prior copy without inventing a new clock.
The shared readings-ledger is never an output of this job. A completed candidate
whose promotion fails is retained privately in the reported `work.*` path;
inspect it and use the following explicit recovery entrypoint if appropriate.
Run as `palimpsest` with supplementary `palimpsest-analysis`, from the installed
immutable GFI source directory. Substitute only the reviewed retained work path:

```bash
flock -n /var/lib/palimpsest/gfi-refresh/refresh.lock \
  /opt/palimpsest/collector-venv/bin/python -B -m scripts.verify_gfi_promotion \
  --source /var/lib/palimpsest/gfi-refresh/work.REVIEWED/source \
  --history-baseline /var/lib/palimpsest/gfi-refresh/work.REVIEWED/gfi-history-before.jsonl \
  --host-readings /var/lib/palimpsest/readings \
  --data-lock /var/lib/palimpsest/railway-publication/data.lock --promote
```

Add `--reseal` only for a verified registry-append race. That mode first verifies
the complete measured candidate, uses the existing `_seal_gfi_v2` attestation
routine on the current host prefix, and preserves the original response and
observation bytes/clocks. It imports code only from the installed runtime and
does not call a model or load credentials. The helper keeps both shared locks
through verification and replacement, refuses missing locks, and supports exact
replay after an interrupted copy. Retain the private recovery directory until
public proof succeeds. Partial model runs are never public output.

After the first invocation, verify all three 220-sample matrices and their
abstention counts, registry chain and transcript seals. A successful service or
updated timestamp alone does not establish full provider coverage. Then verify
the exact completed observation/transcripts/registry on both public origins
through the normal publisher proof. Root deployment coordinates the GFI and
primary-documents watchdog entries after their unit files are integrated.
