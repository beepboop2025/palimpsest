# Out-of-band freshness watchdog

This host timer is independent of Celery Beat. Every five minutes it reads the
dynamic loopback node-status endpoint, evaluates the local `osint-china.v1`
evidence deadlines against its own UTC clock, and independently checks five
documents at the canonical public authority:

- `https://www.palimpsest.info/readings/newswire-latest.json`
- `https://www.palimpsest.info/readings/china-situation-latest.json`
- `https://www.palimpsest.info/readings/publication-freshness-attestation-latest.json`
- `https://www.palimpsest.info/readings/china-publication-rights-latest.json`
- `https://www.palimpsest.info/railway-release.json`

Those HTTPS authorities are fixed in code and cannot be replaced by an
environment value or CLI flag. Redirects are refused; requests ask for JSON and
cache revalidation, time out after ten seconds, and stop at 12 MiB per artifact.
All five requests share one internally generated five-minute cache-busting token
so an edge cache cannot mix publication generations. They run concurrently,
which bounds the public-outage path to one ten-second request window and leaves
time to persist fail-closed state and deliver an alert before systemd's deadline.

The watchdog accepts exactly two publication modes:

- In **full** mode, both endpoint documents must retain their original
  `palimpsest-newswire.v1` and `palimpsest-china-situation.v1` schemas. Their
  `generated_at` values have a two-hour deadline, and the situation's embedded
  Newswire clock and canonical SHA-256 must reproduce the independently fetched
  Newswire.
- In **rights-suppressed** mode, both endpoint documents must be exact
  unavailable/restricted stubs. Their publication SHA, policy identity, rights
  evaluation clock, and master-status identity must agree. The independently
  fetched master must reproduce that identity. The compact freshness
  attestation must bind the original schemas, clocks, canonical document
  digests, and Situation-to-Newswire lineage. Finally, `railway-release.json`
  must name that exact publication SHA and reproduce the served byte identity of
  both stubs, the attestation, and the rights master in `critical_files`.

The reusable evaluator retains full mode for offline compatibility checks. The
production systemd unit sets `--required-publication-mode rights-suppressed`, so
a raw or legacy full-mode origin cannot report healthy after the Railway
cutover.

An original endpoint beside a restricted stub is a corrupt mixed generation,
not partial availability. Missing support documents, malformed metadata,
endpoint-path drift, lineage drift, policy/master drift, release-SHA drift, or
critical-file drift fail both existing `publication/*` conditions closed.

Only the original Newswire and China Situation `generated_at` values establish
evidence freshness. `attested_at`, `rights_evaluated_at`, manifest `built_at`,
HTTP response clocks, and deployment time are control/build clocks and never
refresh stale evidence. A fresh rights pass or Railway build over an old source
pair therefore remains stale. The bounded status document reports the validated
publication `mode`, exact `publication_sha` in restricted mode, original source
clocks, attestation identity, and release-manifest identity; it contains no
quarantined observations or per-record identifiers.

An unavailable API or publication is itself a condition and does not prevent
the other independent checks.

The watchdog does not call the GitHub API, dispatch jobs, run probes, or edit
`readings/`. Its only writes are the bounded status and condition-latch files in
the systemd-managed `/var/lib/palimpsest-watchdog` directory. Exit `2` means a
condition is active, so the unit remains visible in `systemctl --failed` even
without a webhook.

During a host release, exit `2` is never treated as systemd success. The
three-phase transaction runs the reviewed target watchdog in isolated state
before mutation, then runs the installed watchdog again after exact public
publication. `ops/observer_release_gate.py` permits finalization only when each
final semantic condition identity was present in that fresh baseline, its
baseline state is listed in the rule's `baseline_states`, and its complete
final identity exactly matches the reasoned, expiring
`ops/observer-release-policy-20260824.json`. This admits an explicitly reviewed
state transition such as `stale` to `degraded`; it does not admit a state change
merely because the condition name is unchanged. Resolved problems may
disappear. New conditions, unlisted transitions, stale or malformed reports,
policy drift, and unapproved deployment-controlled failures abort while
leaving activators quiesced. The watchdog unit remains failed after an accepted
degraded release, so journald and `systemctl --failed` continue to carry the
incident.

## Install beside the collector stack

The optional localhost API must use the same port as
`PALIMPSEST_LOCAL_STATUS_URL`; host port 8010 is the committed production
default because another service owns host port 8000. The API container still
listens on port 8000 internally. From the
deployed, reviewed checkout:

```bash
sudo install -m 0644 ops/systemd/palimpsest-freshness-watchdog.service \
  /etc/systemd/system/
sudo install -m 0644 ops/systemd/palimpsest-freshness-watchdog.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-freshness-watchdog.timer
sudo systemctl start palimpsest-freshness-watchdog.service
sudo systemctl status palimpsest-freshness-watchdog.service
sudo cat /var/lib/palimpsest-watchdog/status.json
```

The existing `palimpsest` service identity needs read permission on
`/var/lib/palimpsest/readings`; it receives no new write permission there.
`StateDirectory=` creates the private watchdog directory automatically.

## Optional transition alerts

Create `/etc/palimpsest/freshness-watchdog.env` as root with mode `0600`:

```text
PALIMPSEST_WATCHDOG_WEBHOOK_URL=https://alerts.example.invalid/operator-hook
```

Only public HTTPS destinations without URL credentials are accepted. Redirects
are refused. Payloads contain normalized condition IDs/states and counts only,
never raw observations, exception text, local paths, environment values, or the
webhook URL. A newly failing source alerts even while another source remains
failed; recoveries silently clear their own latch so a later regression alerts
again. Delivery failure deliberately leaves new conditions unlatched for retry.

For a deterministic offline replay, supply `--now` plus fixture paths. Never
point `--output` or `--state` into `readings/` in production; the hardened unit
fixes both paths under `/var/lib/palimpsest-watchdog`.
