# Explicit disposition of the 28 September ambiguous upload

This is a single-incident operational decision, **not proof of no provider
mutation**. Normal candidate reconciliation remains unchanged. The 502 response
does not establish whether Railway accepted an unregistered job. That residual
uncertainty is explicitly recorded in the decision and completion records.

The helper accepts only candidate journal
`e06381214a2c40495a83daa38d2137586a140bc5d525cd480c10a37abb87ae02`, original
DATA HOLD `070575e1202df0fc15956a53491b2c9e61b75b44c2e34f2a17e6d4c874af4a4c`,
base pin `17a018bf67a056d35fe04da7c9ca95c26c58b646127b5572ec5cd319f9d9fd65`,
and original upload-output bytes
`448710321ecd2687b1912117c80b116171042d09ecc67d7973de5e8659b13a2d`.

## Preconditions

`ops/railway/retire-upload-502-20260928` defaults to read-only eligibility
checking. It requires root, the existing project-scoped private environment,
the exact reviewed installed reconciler dependency, the ordinary publication
lock, no continuity maintenance or base rotation, and an incident age of
24–96 hours. It validates the original retained bundle, release manifest,
predecessor receipt, original active topology and dual-origin manifests. No
rollback may have been attempted, and no ordinary recovery receipt may exist.

It reads two fresh all-status deployment inventories with `includeDeleted:true`
and two provider event histories, separated by 30 seconds. They must span the
captured predecessor, contain no candidate or newer deployment/event, and show
no other deployment that could run or activate. Each observation checks strict
active topology before and after byte-identical provider/public manifest reads.
The whole observation pair must finish within five minutes. Original local
authority is re-read before the decision.

## Reviewed activation sequence

1. Retain the exact tested helper and systemd unit bytes with the incident
   evidence. Install the helper root-owned at
   `/usr/local/sbin/palimpsest-retire-upload-502-20260928` and install the two
   `palimpsest-retired-upload-observer` units from this revision.
2. Start the observer once and enable its one-minute timer. Require a fresh
   successful observation from this exact helper. The helper refuses actual
   disposition unless that timer is enabled/active and its observation is less
   than five minutes old.
3. Run the default eligibility check with the private environment loaded.
   Review the exact predecessor, both observation clocks, hashes and residual
   uncertainty. A dry run does not consume the pending journal or DATA HOLD.
4. Only after explicit review, invoke the helper with `--retire` and
   `--acknowledge
   accept-unregistered-provider-job-uncertainty-for-600a32145644507e6111213ecd18edfc`.
   It performs fresh checks again. It preserves the original gates, local
   authority, exact reconciler dependency, and provider/origin observations
   under the root-controlled incident directory. Evidence writes are
   content-addressed and never replace different bytes.
5. The durable `decision.json` precedes consumption of the exact original hold,
   then the exact original pending journal. The publication lock remains held.
   A separate `completion.json` records
   `retired_with_unregistered_provider_job_uncertainty` and
   `no_mutation_proved: false`. Neither file is an ordinary reconciliation or
   successful-publication receipt. The previous successful receipt is retained.
6. Let the normal publisher capture fresh inputs and submit one new candidate.
   Require its usual full provider, dual-origin, rights, content and freshness
   proof. Check `/freshness` and connected research catalogs independently.
   Require a second normal scheduled publication before treating recurrence as
   repaired. This incident disposition alone is not a successful publication.

The retained directory is
`/var/lib/palimpsest/railway-control/incident-dispositions/palimpsest-upload-502-20260928/`.
Original candidate, predecessor and release-bundle archives remain in place.

## Delayed candidate response

The observer searches both bounded deployment history and active/latest
topology for the old unique submission message. Checking active topology avoids
hiding an old `createdAt` behind a page of newer history. Provider/query/parser
errors fail visibly. Read-only observations do not acquire the publication
lock, so a long build does not delay them. After disposition, it uses the archived exact
reconciler dependency so a future routine-helper upgrade does not silently
break its parser dependency.

Any matching row, including a queued or terminal row, is retained and raises a
root DATA HOLD through the existing strict hold writer under the ordinary
publication lock. A busy writer lock records the match and an unsuccessful
`late_candidate_observed_hold_deferred` observation, with `hold_written: false`;
the next timer cycle retries safely. A hold conflict or local write error is
recorded as `late_candidate_observed_hold_failed` and also exits unsuccessfully.
Each matching observation and its raw provider evidence are retained before
updating the latest observation. Existing unrelated holds are preserved. The observer never cancels, restarts, rolls back or uploads
to Railway. It exits unsuccessfully on late appearance. Public freshness and
the ordinary direct watchdog remain independent checks and cannot be relabeled
fresh by this disposition.

The observer reduces detection time but is **not a provider fence**. If a late
deployment appears, use its newly authoritative ID to inspect/cancel it if it
can still activate, or prepare exact restoration to the then-current successful
receipt if it activated. Preserve the incident evidence and do not delete the
new hold to retry. No additional automatic mutation is authorized by this
helper.

## Crash behavior

A failure before the durable decision consumes neither gate. A crash after
hold consumption leaves the pending journal blocking publication. Reentry must
re-prove current authority and live state before consuming the same journal.
A crash after journal consumption can write only the separate completion from
the existing durable decision; it does not repeat a Railway operation or
rewrite a successful-publication receipt. Changed gates fail closed.

Rollback of the helper installation is independent of publication: retain the
observer until the residual submission is authoritatively resolved. Do not
restore old journal/hold bytes over a later transaction. A later incident
requires its own reviewed decision.

## Observer-only concurrency revision

The original reviewed helper hash
`cffe1bcc8c52529c12178cabcc308ae3475bae051ceed9a50ffc23dd14938ba3`
was used for the disposition and is retained with the immutable incident
evidence. The later observer concurrency revision changes only observation and
hold-lock timing. It does not rewrite the durable decision, re-run disposition,
change the ordinary publisher or relax predecessor capture.
