# Explicit disposition of the 20 October ambiguous upload

This is a single-incident operational decision, **not proof of no provider
mutation**. Normal candidate reconciliation remains unchanged. The 502 response
does not establish whether Railway accepted an unregistered job. The maintainer
accepted that residual uncertainty for this incident on 2026-10-31, and it is
explicitly recorded in the decision and completion records.

The helper accepts only candidate journal
`ed73080a31732f0ca2ce656a8c5023c8079c41de5316f5cc5106157fbc39adc8`, original
DATA HOLD `15027b0d752b4484f2c80c4c67a3245606a99059c24cdf189c689ca96ec8e3a9`,
base pin `de1f08959413869e362fc354f2dbde749dd772712dcd413128fe5d24fe89c2cd`,
original upload-output bytes
`448710321ecd2687b1912117c80b116171042d09ecc67d7973de5e8659b13a2d`,
submission `f0e823dcc632569cbfcff69f2b1eef78` and captured predecessor
deployment `0bcbcc89-7540-486a-b55e-a4005fde4fd2`. It requires the same reviewed
reconciler dependency as September,
`fb2bdd9542691842492620e78be60e9bc23b6ff3daa50045fe0f71ff8a0651ca`.

## The second identical 502 in three weeks

The candidate prepared at 2026-10-20T17:13:26Z failed in `railway up` with
`Failed to upload code with status code 502 Bad Gateway` (code
`UPLOAD_FAILED`). Those output bytes are identical to the 28 September failure.
Both times Railway created no deployment row, and the captured predecessor kept
serving byte-identical manifests on both origins.

Each upload carries the whole rights-scrubbed static tree. The October release
manifest lists 49,554 files and 1,034,231,587 bytes; September's listed 49,846
files and 1,037,812,832 bytes. The 100 deployments before this incident span
2026-10-19T13:51Z to 2026-10-20T16:56Z, about one every 16 minutes, so the lane
sends roughly 90 uploads a day, each of a tree of about 1 GB before packing.
Each ambiguous failure stops publication until a person reviews it: the
September DATA HOLD lasted about 40 hours and this one more than eleven days.

## Recommended durable fix (follow-up only)

Nothing below is implemented here. Each option needs its own reviewed change.

1. Send less per publication. The publisher already skips a capture whose
   input is unchanged, but every changed input re-sends the whole tree,
   however little of it changed. Moving the bulky, slowly changing static
   files to content-addressed storage would let Railway receive a small tree.
   Smaller transfers give a gateway fault fewer chances to land
   mid-mutation.
2. Learn the provider identity before the transfer. Prefer a deployment path
   in which the provider identity, or an image digest the publisher controls,
   exists before the bytes move. A transport failure then leaves an identity
   to reconcile instead of an absence to argue about. Do not add blind retries
   of `railway up`: the first attempt may have been accepted.
3. Replace per-incident helpers with one reviewed policy for a failure before
   provider registration: the exact upload-output bytes, no provider row, no
   provider event and a quiet observer for a stated period. The policy would
   still record uncertainty and never claim no mutation. Its thresholds are a
   maintainer decision and are not set here.

## Preconditions

`ops/railway/retire-upload-502-20261020` defaults to read-only eligibility
checking. It requires root, the existing project-scoped private environment,
the exact reviewed installed reconciler dependency, the ordinary publication
lock, no continuity maintenance or base rotation, and an incident age between
24 hours and 14 days after submission. The window closes at
2026-11-03T17:13:26Z; from then on the helper refuses and a new reviewed
decision is needed. It validates the original retained bundle, release
manifest, predecessor receipt, original active topology and dual-origin
manifests.

No rollback may have been attempted: no attempt record, saved attempt topology
or rollback response may exist for the journal, and the hold must record
`attempted: false`. No ordinary recovery receipt may exist. Any
`pending-preparation.json` refuses, not only one the reconciler would match.
The candidate must name the exact submission, preparation clock and
predecessor, although the journal digest already binds them.

It reads two fresh all-status deployment inventories with `includeDeleted:true`
and two provider event histories, separated by 30 seconds. They must span the
captured predecessor, contain no candidate or newer deployment, and show no
other deployment that could run or activate. Each observation checks strict
active topology before and after byte-identical provider/public manifest reads.
The whole observation pair must finish within five minutes. Original local
authority is re-read, and the age window rechecked, before the decision.

## Provider evidence eleven days later

The September helper refused any project or environment event after the
submission. On 2026-10-31 the newest 100 events contained exactly one later
event: `82dcb88c-3b02-4803-9771-47a2dcac5b38`, object `Token`, action
`created`, at 2026-10-23T20:20:42.075Z, whose payload carries only a token
name. It names no deployment, image or CLI message, and a token creation alone
neither creates nor activates a deployment. The helper admits that event only
by its exact identity, object, action, clock and payload keys. A second token,
any other later event, or any change to this one refuses.

On the same day the deployment inventory's newest row was the predecessor,
`SUCCESS` and running. The 99 older rows were `REMOVED` and stopped, no row
carried the candidate message, and nothing was newer than the predecessor.
Both origins served predecessor manifest
`d285f70b1770963e306feeb9447dac54f8c1ec34f89ce36773dc25b7c8341bd2`.
The helper re-proves all of this at run time; these observations are context,
not authority.

## Reviewed activation sequence

1. Retain the exact tested helper and systemd unit bytes with the incident
   evidence. Install the helper root-owned at
   `/usr/local/sbin/palimpsest-retire-upload-502-20261020` and install the two
   `palimpsest-retired-upload-observer-20261020` units from this revision.
   Leave the September helper, observer and timer installed and running.
2. Start the October observer once and enable its one-minute timer. Require a
   fresh successful observation from this exact helper. The helper refuses
   actual disposition unless that timer is enabled and active and its
   observation is less than five minutes old.
3. Run the default eligibility check with the private environment loaded.
   Review the exact predecessor, both observation clocks, hashes, the admitted
   later event, the window and the residual uncertainty. A dry run does not
   consume the pending journal or DATA HOLD.
4. Only after explicit review, and before the window closes, invoke the helper
   with `--retire` and `--acknowledge
   accept-unregistered-provider-job-uncertainty-for-f0e823dcc632569cbfcff69f2b1eef78`.
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
   The decision adds four fields to the September record: `submission_id`,
   `helper_sha256`, `disposition_window` and `reviewed_later_events`.
6. Let the normal publisher capture fresh inputs and submit one new candidate.
   Require its usual full provider, dual-origin, rights, content and freshness
   proof. Check `/freshness` and connected research catalogs independently.
   Require a second normal scheduled publication before treating recurrence as
   repaired. This incident disposition alone is not a successful publication.

The retained directory is
`/var/lib/palimpsest/railway-control/incident-dispositions/palimpsest-upload-502-20261020/`.
Original candidate, predecessor and release-bundle archives remain in place.

## Delayed candidate response

The October observer searches both bounded deployment history and active/latest
topology for the October submission message only. The September observer keeps
watching its own message; neither matches the other. Checking active topology
avoids hiding an old `createdAt` behind a page of newer history.
Provider/query/parser errors fail visibly. Read-only observations do not
acquire the publication lock, so a long build does not delay them. After
disposition, it uses the archived exact reconciler dependency so a future
routine-helper upgrade does not silently break its parser dependency.

While a newer deployment is still building, the strict topology parser refuses
the topology, and an observer that found no match exits unsuccessfully until
the deployment settles. That is a visible parser refusal, not a match: a match
is always reported as one of the `late_candidate_observed` states.

During an ordinary publication both deployments are briefly active, and the
parser refuses with `Railway active deployment inventory is not singular`. By
then the observer has already searched the bounded inventory and the topology
for the retired message. It exits 75, which its unit maps through
`SuccessExitStatus=75`, and logs `incident observation deferred`, only when
every one of these holds:

- the two active deployments are exactly the captured predecessor and the
  deployment that carries the pending journal's own message, and that
  deployment is the latest one;
- the pending journal passes the reviewed reconciler's validator, is not the
  retired submission, and has a receipt deadline at most 600 seconds after
  preparation;
- the observation is no later than that deadline plus the publisher's 10 second
  commit reserve;
- `palimpsest-railway-publish.service` is `activating`/`start` with a main PID,
  and the journal was prepared strictly after that invocation started.

The proof reads only the pending journal and systemd. It takes no lock, makes
no further provider call, and writes no observation, evidence or hold, so the
last clean observation keeps its clock. Any other non-singular topology, and
every other refusal, still exits 1. A retired message seen during a rollout is
still a late sighting.

Any matching row, including a queued or terminal row, is retained and raises a
root DATA HOLD through the existing strict hold writer under the ordinary
publication lock. A busy writer lock records the match and an unsuccessful
`late_candidate_observed_hold_deferred` observation, with `hold_written: false`;
the next timer cycle retries safely. A hold conflict or local write error is
recorded as `late_candidate_observed_hold_failed` and also exits unsuccessfully.
Each matching observation and its raw provider evidence are retained before
updating the latest observation. Existing unrelated holds are preserved. The
observer never cancels, restarts, rolls back or uploads to Railway. It exits
unsuccessfully on late appearance. Public freshness and the ordinary direct
watchdog remain independent checks and cannot be relabeled fresh by this
disposition.

The observer reduces detection time but is **not a provider fence**. If a late
deployment appears, use its newly authoritative ID to inspect/cancel it if it
can still activate, or prepare exact restoration to the then-current successful
receipt if it activated. Preserve the incident evidence and do not delete the
new hold to retry. No additional automatic mutation is authorized by this
helper.

## Crash behavior

A failure before the durable decision consumes neither gate. A crash after
hold consumption leaves the pending journal blocking publication. Reentry must
re-prove current authority and live state, inside the same window, before
consuming the same journal. If the candidate recovery timer raises a new DATA
HOLD in that interval, reentry refuses because the new hold is not the exact
original; preserve the evidence and seek a new reviewed decision rather than
deleting it. A crash after journal consumption can write only the separate
completion from the existing durable decision; it does not repeat a Railway
operation or rewrite a successful-publication receipt. Changed gates fail
closed.

Rollback of the helper installation is independent of publication: retain the
observer until the residual submission is authoritatively resolved. Do not
restore old journal/hold bytes over a later transaction. A later incident
requires its own reviewed decision.

## Relation to the September disposition

The September helper, units and tests land on `main` unchanged in this change,
merged with their original commits `62184011b1`, `cadd6cffd5` and `80af44af41`.
The September helper installed on the host hashes to
`fc6e5383821ff24ebb804bfdf3aaacbbeb3d1cad5de3989e747541795c51d04c`, the blob at
`80af44af41`, and its two installed units match this revision. Its decision and
completion remain the only authority for that incident. Neither helper can
consume the other's journal or hold: each is pinned to its own digests,
incident directory, acknowledgement and observer timer.

## Observer in-flight revision

After publication resumed on 2026-11-01, both observers refused two or three
times during every rollout, about every 16 minutes, because the parser refuses
two active deployments; each refusal left its unit briefly failed. This
revision changes only how an observer classifies that one refusal, as described
under the delayed candidate response, and lands identically in the September
helper. The disposition path, the durable decision and completion, message
matching, the hold writer, both observer units and the reviewed reconciler
dependency are unchanged. Both units already declared `SuccessExitStatus=75`,
which observation never returned before this revision.
