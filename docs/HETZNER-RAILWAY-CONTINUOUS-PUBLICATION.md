# Continuous Hetzner-to-Railway publication

## Outcome

Palimpsest can use Hetzner as its dense, persistent observation plane while
Railway serves a current, immutable public edition. The connection is an
admission pipeline, not a direct database tunnel:

1. Hetzner collects and retains private evidence.
2. Only allowlisted, sanitized projections cross the public snapshot boundary.
3. GitHub Actions validates those projections, commits an exact public edition,
   and runs the complete publication and rights contract.
4. In scheduled steady state, an hourly controller coalesces all accepted
   commits into one newest changed Railway release. Manual canary and force runs
   remain possible and use the same serialized transaction.
5. Railway activates the bundle only after its health check passes; an
   independent verifier then proves the exact provider and custom-domain bytes.

This separation is deliberate. Railway never mounts the Hetzner warehouse,
Hetzner never receives a Railway credential, and a compromised collector host
cannot become the public release authority.

This document is both an architecture record and an activation runbook. A
merged workflow is not evidence that continuous publication has been activated;
the gates in [Activation](#activation) must also be completed and receipted.

## Trust and data flow

```text
 public upstreams
       │
       ▼
 ┌───────────────────────────────────────────────────────────────┐
 │ Hetzner: private observation plane                            │
 │ collectors → PostgreSQL / Redis / private readings / warehouse│
 │              │                                                │
 │              └─ allowlisted sanitized latest projections      │
 └──────────────────────────────┬────────────────────────────────┘
                                │ fixed HTTPS paths; strict schema,
                                │ size, clock and last-good checks
                                ▼
 ┌───────────────────────────────────────────────────────────────┐
 │ GitHub: admission and release authority                       │
 │ source-specific refresh → reviewed main commit → complete CI  │
 │ → rights gate → sealed Git-archive bundle                     │
 └──────────────────────────────┬────────────────────────────────┘
                               │ hourly, coalesced, exact SHA;
                               │ protected token + exclusive-writer proof
                                ▼
 ┌───────────────────────────────────────────────────────────────┐
 │ Railway: immutable static serving plane                       │
 │ upload → Docker build → /healthz gate → traffic activation    │
 └──────────────────────────────┬────────────────────────────────┘
                                │ independent no-cache reads
                       ┌────────┴────────┐
                       ▼                 ▼
              Railway provider      palimpsest.info
                  origin            custom domain
                       └────────┬────────┘
                                ▼
                  exact-byte release receipt
```

The existing [`scripts/import_host_snapshot.py`](../scripts/import_host_snapshot.py)
boundary illustrates the first crossing. It accepts only code-pinned HTTPS
origins and paths, bounded payloads, strict document shapes and valid clocks;
it refuses redirects and preserves the last known-good value when an allowed
source is merely stale. Other snapshot contracts may add HMAC authentication.
Neither mechanism permits a raw warehouse, database, source URL, probe identity
or private local path to enter the public tree.

The host's broader collection and retention design remains documented in
[`HETZNER-NODE-ARCHITECTURE.md`](HETZNER-NODE-ARCHITECTURE.md). The exact host
release and recovery transaction remains authoritative in
[`ops/DEPLOY-HETZNER.md`](../ops/DEPLOY-HETZNER.md); this runbook does not replace
or shortcut it.

## Authority boundaries

| Plane | May do | Must not do |
| --- | --- | --- |
| Hetzner | Collect public sources, retain private evidence, expose specifically reviewed sanitized projections | Hold a Railway token, push its private tree to Railway, or silently write canonical public Git history |
| GitHub refresh workflows | Collect declared public sources and import allowlisted host projections, preserve abstentions and last-good state, build and test candidate public commits | Convert missing/stale/invalid evidence into healthy or zero-valued output |
| Complete publication contract | Bind a clean current-main SHA, run rights/public-surface checks, and build an immutable bundle | Publish a divergent, dirty, unreviewed or rights-failing tree |
| Hourly controller | Compare live Railway identity with current `main` and request one exact transaction | Deploy directly, downgrade a non-ancestral live edition, or dispatch a moving SHA |
| Protected GitHub environment | Release one exact admitted bundle with a project-scoped Railway token | Expose the token to collector jobs, forks, logs, artifacts or Hetzner |
| Railway | Build and serve the sealed static artifact behind `/healthz` | Attach a mutable GitHub source, run collectors or cron, or mount the private warehouse |

## One publication transaction

### 1. Admit a public candidate

Source-specific publishers collect only their declared public sources and/or
import only their declared host projection, then produce a base-locked
candidate. They rebuild derived readings, seal the readings ledger, scan the
public surface, and dispatch the complete publication contract for the exact
accepted commit. A producer failure remains visible and does not create a
replacement value.

The continuous Railway controller does not make collector commits. Its only
input is accepted public `main` history.

### 2. Coalesce accepted commits

[`railway-publication-controller.yml`](../.github/workflows/railway-publication-controller.yml)
is scheduled once per hour and may also be manually dispatched. Every run is
serialized without cancellation. It fetches the live `/railway-release.json`,
strictly validates its release identity, and compares the live source commit
with the exact current `main` tip.

- If the live source commit equals current `main`, a non-forced run is a no-op.
  This controller decision compares source identity; it does not rebuild a
  candidate or compare a local tree or manifest.
- A manual `force` bypasses only that source-identity no-op and requests a
  complete re-proof of the current tip. The new rights-admission clock is
  embedded as `built_at`, so the resealed manifest normally differs and the
  transaction normally uploads even when source and tree are unchanged.
- If `main` advanced, the live commit must exist in public Git history and be
  an ancestor of `main`.
- Immediately before dispatch, the controller re-reads remote `main`. Any
  movement aborts this transaction; the next hourly run evaluates the newer
  aggregate state.
- Before dispatch, the controller writes one canonical request, uploads it as a
  90-day immutable Actions artifact, and transports its exact run ID, run
  attempt, artifact ID, artifact digest and request digest. The complete
  contract downloads that exact artifact and rejects a stale request, a
  different run attempt, branch, workflow, repository, SHA, byte stream or
  retention policy. A syntactically valid `repository_dispatch` is not release
  authority by itself. The artifact remains evidence for approximately 90
  days, but it authorizes admission for only one hour with at most 60 seconds of
  future clock skew. The contract polls the exact controller run attempt and
  requires `completed`/`success`. Its bound `requested_at` is also the Railway
  rights-admission clock, making a replay of the same authenticated artifact
  byte-identical; ordinary publication requests use their current admission
  time.
- A valid difference dispatches the existing complete publication contract
  with `deploy_railway: true`. Producer-triggered publication dispatches do not
  set that flag and therefore cannot create a deployment storm.

This is level-triggered scheduled behavior: absent a manual canary or force run,
five accepted collector commits in one interval become one deployment of the
newest admitted aggregate edition, not five successive Railway builds.

### 3. Re-prove and seal the exact edition

The complete contract checks out the requested SHA with full ancestry and
requires it to be the current `main` tip. The Railway transaction freshly
repeats that exact-tip and clean-worktree check before building, then repeats it
again immediately before its mutation boundary. Railway receives a new
directory produced by
[`ops/railway/build-static-bundle.sh`](../ops/railway/build-static-bundle.sh),
not the mutable checkout.

The builder:

- archives only tracked content from the exact commit;
- excludes hidden control directories while retaining `.well-known`;
- rejects tracked symbolic links and a dirty checkout;
- stages and rechecks the Pages rights decision;
- verifies reviewed aggregate and report-only publication locks;
- rebuilds the wire archive and release manifest;
- independently enumerates the complete staged tree, including the nested
  `railway-release.json`, and seals its file count, total bytes and digest while
  excluding only the self-referential root manifest;
- seals every artifact read-only before promotion.

If the live source SHA, tree digest and manifest digest all match the candidate,
the transaction records `recovered_existing`, performs the live verifier and
MCP rights smoke, and makes no upload. This exact three-field recovery path must
not be inferred merely from equal source and tree values: a newly resealed
admission clock changes the manifest digest. If `main` advances after the final
pre-mutation fetch, the already bound release may finish; the next hourly
controller evaluates the successor. The transaction does not claim that `main`
stayed motionless for the entire Railway build.

### 4. Upload without transferring authority

Only the protected `palimpsest-railway-production` GitHub environment may read
`PALIMPSEST_RAILWAY_TOKEN`. The credential must be a Railway **project token**
scoped to the pinned Palimpsest project and production environment. The release
job validates that scope before any mutation. The same protected environment
must supply
`RAILWAY_EXCLUSIVE_WRITER_ACK=palimpsest-github-environment-v1`. That
acknowledgement is valid only after an operator has proved that no dashboard,
local CLI, attached source, Railway cron or other workflow can mutate this
service concurrently. Railway exposes no conditional deployment lock or
compare-and-swap rollback, so this operational single-writer invariant is part
of the release protocol, not a convenience flag.

The upload is a local-directory deployment. Railway remains static-only:

- no attached repository or image source;
- no Railway cron schedule;
- no volume or required mount;
- one Dockerfile-built replica;
- `/healthz` as the deployment health gate.

Do not convert this service to native GitHub source deployment. That would let
Railway build a repository revision without the sealed-bundle and rights
transaction that defines the public artifact.

### 5. Prove the activated bytes

The deploy job permits exactly one `railway up` submission and creates one
run-unique `cliMessage` containing the publication SHA, workflow run and run
attempt. It parses the detached CLI JSON for a deployment ID but does not trust
the exit code or assume the newest list entry belongs to this run. Instead it
repeatedly lists a bounded deployment inventory, requires exactly one row whose
`meta.cliMessage` equals that message, cross-checks any CLI-reported ID, and
thereafter polls only that exact ID. This recovers identity when a request
reached Railway but the CLI returned a transient error. Missing, duplicate or
conflicting identities fail closed; none permits a second upload.

A success is accepted only when Railway reports that selected deployment as
`SUCCESS` with a valid image digest and the effective service topology reports
the same ID and digest as its latest running deployment.

[`ops/railway/verify_continuous_release.py`](../ops/railway/verify_continuous_release.py)
then independently proves:

- clean checkout SHA = requested SHA = the `origin/main` value fetched at the
  final pre-mutation boundary;
- local release-manifest digest, source commit, tree digest and freshness;
- independently recomputed full-tree file count, total bytes and digest, so an
  extra unlisted file fails proof as surely as a changed critical file;
- pinned project, environment and service topology;
- exactly one running service instance;
- latest running deployment ID and image digest;
- static-only Dockerfile, health-check, cron and volume invariants;
- no-cache `/healthz` identity at the Railway provider origin;
- byte-identical `/railway-release.json` at provider and public origins;
- exact size and SHA-256 of every critical file named by the manifest at both
  origins.

Only then does it write an atomic, mode-0600, secret-free receipt conforming to
[`railway-continuous-release-receipt-v1.schema.json`](../protocol/railway-continuous-release-receipt-v1.schema.json).
Raw Railway responses and HTTP headers are deliberately not copied into that
receipt because they may gain provider metadata that is not part of the public
proof contract.

The verifier does not fetch Git itself. Its `current_main_source_commit` is the
transaction's pre-mutation `origin/main` capture, so the receipt proves the
release was current at that authority boundary—not that Git history could not
advance while Railway built and activated the already admitted bundle.

## Freshness semantics

Continuous deployment improves publication latency; it does not redefine
evidence freshness. Keep these clocks distinct:

1. **Observation clock** — when the source was actually measured.
2. **Candidate clock** — when the sanitized projection was accepted into Git.
3. **Admission clock** — when rights and public-surface gates sealed the bundle.
4. **Deployment clock** — when Railway created the exact deployment.
5. **Verification clock** — when both origins and all critical bytes were
   independently checked.

The expected steady-state deployment lag is less than one controller interval
plus CI/build time after an accepted public commit. An old upstream observation
must still appear stale even if it was bundled and deployed five minutes ago.
Likewise, a healthy Railway response proves serving health, not collector
health.

## Failure and pre-mutation preservation behavior

| Failure point | Required behavior |
| --- | --- |
| Host collection or sanitized import fails | Preserve the prior valid public value and record failure/abstention; do not dispatch fabricated freshness |
| Candidate, rights, scrub or complete CI fails | Do not upload; Railway continues serving the previous edition |
| Live commit is absent from Git or not ancestral to `main` | Stop as a release-history incident; never overwrite it automatically |
| Remote `main` moves before the final mutation preflight | Abort the exact transaction; let the next hourly controller coalesce the new tip |
| Remote `main` moves after the mutation boundary | Finish proving the already bound exact release; let the next hourly controller publish the successor |
| Railway build or `/healthz` fails before activation | Treat the deployment as failed; Railway's prior active deployment remains the last good |
| Upload output is ambiguous | Reconcile the one run-unique message against a bounded deployment inventory; never submit a second upload |
| A known candidate remains nonterminal when recovery begins | Re-read the exact ID/message, request one bounded cancellation, and wait for that exact candidate to fail terminally or race to `SUCCESS` |
| Candidate identity remains unknown or conflicts | Refuse to claim preservation or rollback; disable publication, remove the exclusive-writer acknowledgement and escalate with the transaction evidence |
| Post-activation topology, byte or MCP verification fails | Request rollback to the pre-mutation deployment ID, then require both origins to serve their exact captured pre-mutation manifest bytes |
| Rollback request or pre-mutation manifest preservation proof fails | Disable `RAILWAY_PUBLICATION_ENABLED`, remove the exclusive-writer acknowledgement, preserve evidence, and escalate; never use `railway down` as a substitute for recovery |
| Normal transaction deadline is exhausted | Stop forward work and spend the reserved recovery interval only on exact candidate reconciliation, cancellation and restoration |

Rollback proof is deliberately narrower than forward-release verification. The
transaction checks that the chosen pre-mutation deployment reports
`canRollback`, rechecks the exact candidate under the exclusive-writer
invariant, and requests `deploymentRollback` for the predecessor ID. Railway
restores that image as a **new** deployment. Recovery therefore requires a new
deployment ID distinct from both candidate and predecessor, the predecessor
image digest, `deploymentRollback` reason, and provider and public
`/railway-release.json` bytes equal to the copies captured before mutation.

It does not emit a new full forward-verifier receipt for the rollback and does
not re-run `/healthz`, every critical-file read, or MCP rights smoke. Treat the
transaction receipt's `restored_deployment_id`, `restored_image_digest`,
`restored_reason` and manifest comparison as bounded in-run preservation
evidence, not as a fully receipted forward release. Those `restored_*` fields
remain empty unless topology and both manifest byte checks pass. Raw rollback
API responses and compared manifest copies remain in the ephemeral control
directory. If rollback follows a later MCP failure, an already written forward
verification receipt is historical proof of the candidate before rollback; it
must not be cited as the post-rollback live state.

The submission state advances only through `not_started`,
`submitted_unknown`, `active`, and `terminal_failed`. There is no upload retry.
The workflow ceiling is 55 minutes; the transaction defaults to 3,000 seconds
and reserves its final 900 seconds for recovery. The configured recovery budget
is validated against cancellation, restoration, command ceilings and timeout
kill grace; current defaults require at least 830 seconds and retain 70 seconds
of additional scheduling/receipt slack. Individual rollback calls are bounded:
30 seconds per command, 180 seconds for candidate cancellation and 300 seconds
for restoration proof. The pre-mutation predecessor may be old because it is
rollback authority (default maximum one year), while the candidate admission
must be current (default maximum one day). Failures after evidence-directory
initialization retain diagnostic artifacts;
pre-initialization failures may retain only Actions logs and job status. Only a
fully verified forward activation or verified `recovered_existing` edition may
emit a `status: verified` continuous-release receipt.

## Activation

Keep `RAILWAY_PUBLICATION_ENABLED` unset or unequal to `true` until every item
below is complete. The controller intentionally does nothing while that gate is
closed.

### Release prerequisites

- [ ] The current Railway source commit has been integrated into public Git
      history, and the proposed `main` tip is its descendant.
- [ ] Hosted CI is green for the exact bridge revision, including workflow,
      bundle, rights, verifier and receipt-schema tests.
- [ ] Railway infrastructure still matches [`.railway/railway.ts`](../.railway/railway.ts),
      and an immediate `railway config plan` reports no unexplained drift.
- [ ] The protected GitHub environment `palimpsest-railway-production` exists,
      admits only the intended release branch/reviewers, and contains a newly
      created project-scoped `PALIMPSEST_RAILWAY_TOKEN`.
- [ ] The Railway project has exactly one production writer: this protected
      environment. Native source deployment is detached, Railway cron is
      absent, no manual/dashboard or local-CLI release is in flight, and no
      other workflow or credential can mutate the service.
- [ ] Only after that audit, the protected environment contains
      `RAILWAY_EXCLUSIVE_WRITER_ACK=palimpsest-github-environment-v1`. Leave it
      unset during implementation, CI and all read-only topology checks.
- [ ] No Railway credential exists on Hetzner, in repository variables, in a
      general-purpose repository secret, or in workflow artifacts.
- [ ] The exact previous successful Railway deployment ID, source commit, tree
      digest, manifest digest and live receipt have been captured as rollback
      authority.

### Close the Hetzner release freeze

Any prepared host recovery transaction without its matching completion receipt
is a hard activation freeze. The pre-implementation audit found such unfinished
state, so re-audit the live host and close or formally reconcile it; do not
unfreeze producers merely because the bridge code merged.

- [ ] Set and keep the repository variable
      `RAILWAY_PUBLICATION_ENABLED=false`; leave scheduled publishers disabled.
- [ ] Execute exactly one reviewed production Newswire refresh. Its accepted
      commit must carry fresh, digest-bound Newswire and China-situation source
      clocks within the two-hour Phase 1 window. Do not require their public
      same-path endpoints to equal the raw Git artifacts: rights quarantine
      deliberately replaces both with restricted stubs. Use
      [`ops/railway/run-newswire-prerequisite.sh`](../ops/railway/run-newswire-prerequisite.sh)
      from the bounded first-activation block in
      [`ops/DEPLOY-HETZNER.md`](../ops/DEPLOY-HETZNER.md#first-newswire-prerequisite-and-railway-activation-canary)
      and preserve its canonical receipt.
- [ ] Dispatch the Railway controller once with `activation_canary=true` and
      `force=false` while the schedule flag remains false, using
      [`ops/railway/run-activation-canary`](../ops/railway/run-activation-canary)
      with that prerequisite receipt. Require complete controller-to-downstream
      selection, typed-SHA environment approval, immutable artifact evidence,
      byte-identical provider/custom-domain manifests, and an exact
      rights-suppressed freshness attestation bound to both raw input clocks and
      canonical digests; controller success by itself is not deployment
      success. Then pin the canonical
      `https://www.palimpsest.info/railway-release.json` source commit as the
      exact Phase 1 forward-repair target.
- [ ] Run host Phase 1. Before its first mutation, its watchdog must report no
      publication problem, `publication.mode=rights-suppressed`, and
      `publication.publication_sha` equal to the pinned deployment SHA.
- [ ] Complete or formally reconcile every open prepared recovery transaction
      using [`ops/DEPLOY-HETZNER.md`](../ops/DEPLOY-HETZNER.md).
- [ ] Prove `/etc/palimpsest/deployed-commit`, the detached checkout, certified
      image and installed host bundles all name the same admitted release.
- [ ] Prove PostgreSQL, Redis, Beat, required workers, collector queue and
      localhost operator API are healthy before relying on new snapshots.
- [ ] Keep active probing gates off unless a separate reviewed activation names
      Hetzner as the exclusive owner.
- [ ] Restore only the host services and activators selected and proved by the
      exact Hetzner release transaction. This document does not authorize
      independently enabling stopped systemd timers or all collector profiles.
- [ ] Let Phase 2 perform its bounded enable-dispatch-disable OSINT v2
      publication `P`; do not substitute a permanently enabled producer.
- [ ] Still with `RAILWAY_PUBLICATION_ENABLED=false`, let Phase 2 dispatch a
      second controlled `activation_canary=true` run. Its manifest source
      release `R` must contain `P`. Require the canonical www origin to serve
      the exact restricted OSINT same-path stub and digest-bound master rights
      status, while its public ledger equals Git `R` and has the candidate `P`
      ledger as a prefix. Public raw OSINT is a refusal condition.
- [ ] Run Phase 3 in the original paused Phase 1 shell. It must recheck the
      exact `R` public identities from the Phase 2 handoff, persist the exact
      canonical `palimpsest-public-osint-release-proof.v2` bytes unchanged at
      the root-only provider proof path, and let the root-mode provider
      independently repeat the `P -> R`, latest-OSINT and ledger proof before
      any producer ownership is restored. A v2-to-v1 projection is forbidden
      because it would discard the public release and digest evidence.
- [ ] Leave unrelated disabled GitHub collectors frozen. Restore only the
      minimum public-write workflow chain, one owner at a time:
      `newswire-refresh.yml`, then `osint-china-v2-refresh.yml`, then
      `collector-health-watchdog.yml`.
- [ ] After each enablement, require one exact successful run and a closed
      outcome before refreezing that owner: `published` requires changed bytes
      and an accepted descendant main, while `no_change` requires unchanged
      main and truthful freshness/abstention evidence.

The three YAML names above are GitHub public-publication owners, not Hetzner
systemd units. The fixed cross-plane sequence is:

1. keep `RAILWAY_PUBLICATION_ENABLED=false` and every schedule frozen;
2. run one production Newswire refresh;
3. run the first controlled `activation_canary=true` Railway publication;
4. complete host Phase 1;
5. let Phase 2 publish OSINT commit `P`;
6. run the second controlled canary so public release `R` contains `P`;
7. complete host Phase 3 and its independent restricted-publication proof;
8. prove scheduled producer ownership one workflow at a time—Newswire, OSINT
   v2, then the watchdog—refreezing each owner after its exact manual outcome;
9. re-enable all three at one short boundary and prove a quiet all-owner state;
   and
10. only then let the hourly helper set `RAILWAY_PUBLICATION_ENABLED=true`
    inside the acquired UTC `:09:00-:10:30` arming window, after freezing all
    three producers and before binding exactly one `:13` scheduled controller
    run and disabling that controller; and
11. prove the controller result while all four schedules remain disabled, then
    reactivate all four only in the next UTC `:20-:30` admission window, prove
    quiet authority by `:40`, and seal live-byte evidence and the receipt before
    the hard `:50` cutoff.

Do not translate that sequence into ad hoc `systemctl enable` commands; the
Hetzner runbook owns the host unit set and activator restoration.

The executable handoff after Phase 3 is:

1. copy the exact root-only finalized and proof-complete receipts to a private
   workstation evidence directory and compare both SHA-256 values;
2. extract the canonical `palimpsest-public-osint-release-proof.v2` object from
   `proof_complete.publication.handoff` without changing its bytes;
3. run [`ops/railway/run-producer-restore`](../ops/railway/run-producer-restore)
   with distinct `EXPECTED_HOST_SHA=H` and
   `EXPECTED_PUBLIC_RELEASE_SHA=R`, the three private receipt paths, a new
   evidence directory and a new terminal receipt path; and
4. after its `palimpsest.github-producer-restore.v2` receipt is `verified`, run
   [`ops/railway/enable-hourly-publication`](../ops/railway/enable-hourly-publication)
   with that receipt and a new `HOURLY_ACTIVATION_RECEIPT` path.

Use the complete copy and invocation block in
[`ops/DEPLOY-HETZNER.md`](../ops/DEPLOY-HETZNER.md#copy-phase-3-authority-and-restore-the-scheduled-producers).
The restore helper proves `H -> P -> R`, never collapses host and public release
identity, and accepts a producer-advanced retry only through the exact prior
`failed-closed` receipt. The hourly helper keeps the gate false until a quiet
UTC `:09:00-:10:30` arming window after the watchdog's `:05` tick, requires the
typed exact final main SHA, freezes Newswire, OSINT and the watchdog, and proves
unchanged producer, controller and Tests inventories before opening the gate.
It binds exactly one real `:13` scheduled controller and then disables that
controller, so all four schedules remain disabled throughout release proof.
`dispatched` binds the request, exact attempt-1 Tests child, protected approval
and release artifacts; `no_change` proves no Tests child and exact already-live
main bytes. Both branches require both live origins to agree. A variable write
by itself is not an activation receipt.

After either branch closes, the helper waits for the next UTC `:20-:30`
admission window, which deliberately misses Newswire's `:17` tick and leaves
margin before OSINT's `:58` tick. It re-enables the three producers and the
controller together, double-proves unchanged run inventories and authority by
`:40`, and bounds the final provider/www fetch and receipt commit by `:50`.

### Canary and steady state

The two activation canaries are explicitly authorized manual dispatches while
`RAILWAY_PUBLICATION_ENABLED=false`; the controller accepts them only when
`activation_canary=true`. Use `force=false`. A `force=true` upload is outside
this host activation transaction and requires a separate billable-release
decision.

For a `dispatched` canary, require protected-environment approval,
provider-origin proof, custom-domain proof at the exact canonical www origin,
critical-file proof, MCP rights smoke and a schema-valid receipt. Record whether
the transaction reports `deployed` or the stricter exact-triple
`recovered_existing` result; do not infer the latter from equal source/tree
values alone. A canary may instead close as exact `no_change` only when the
controller emits no request or Tests child and both origins already prove the
expected main manifest and freshness bytes.

After Phase 3 succeeds, prove the minimum producer chain in the fixed order
above. Each owner is enabled for one exact manual proof, accepts its closed
workflow-specific `published`, `no_change` or watchdog `abstained` outcome, and
is refrozen before the next owner. The helper then re-enables all three together
and seals a quiet all-owner receipt. Only that receipt may let
`enable-hourly-publication` set
`RAILWAY_PUBLICATION_ENABLED=true`.

The hourly success receipt always names the exact scheduled controller
run/attempt, outcome artifact, served source SHA, manifest digest and
freshness-attestation digest. It also records that producers were disabled
before the gate, the controller was disabled after binding, the terminal
reactivation clock and the `:20/:30/:40/:50` boundaries. Its `dispatched` branch
additionally names the request artifact, Tests run/attempt and
transaction/verification digests; those fields are canonically null for exact
`no_change`, which instead proves no Tests addition and already-current live
bytes. Leave all other collectors disabled until their ownership, source
rights, output path, freshness budget and recovery behavior receive the same
review.

At any failure, reset `RAILWAY_PUBLICATION_ENABLED=false`, remove the protected
exclusive-writer acknowledgement, and restore all three producers plus the
controller to `active`. A `failed-closed` hourly receipt additionally requires
the acknowledgement to be proved absent, no active run across the three
producer, controller and Tests workflow inventories, and every bound
controller/Tests run to be terminal (or proves that no such run exists);
otherwise the result is `cleanup-unproved`. Preserve the secret-free receipts
and exact deployment identities, and follow the failure table. Never print, copy to Hetzner, or add a Railway token to a handoff or diagnostic artifact.

Only a producer `failed-closed` receipt may authorize a fresh producer retry;
`cleanup-unproved` requires manual run/authority reconciliation first. An
hourly-only retry retains the same verified producer receipt, uses a fresh
hourly receipt path, reconciles any producer/controller/Tests run or workflow-
state uncertainty, re-audits and recreates the exact acknowledgement, and
reruns only the hourly helper.

## Operations and evidence to retain

For every activation proof, retain the branch-applicable artifacts together.
An exact `no_change` binds an already-live edition rather than creating a
changed one:

- controller run ID/attempt and canonical controller-outcome artifact; for
  `dispatched`, also the requested exact source SHA, canonical request, request
  digest, artifact ID and artifact digest;
- complete publication-contract run ID/attempt for `dispatched`, or exact proof
  of no Tests addition for `no_change`;
- Pages rights receipt and its SHA-256;
- local `railway-release.json` and its SHA-256;
- exact Railway deployment ID, image digest and allowlisted topology evidence;
- continuous-release receipt and its SHA-256;
- MCP rights smoke receipt;
- canonical Phase 2 `palimpsest-public-osint-release-proof.v2` handoff digest,
  plus proof that Phase 3 installed those exact bytes unchanged while the
  provider ran and retained all five public identities in its
  `palimpsest-public-osint-sync.v3` receipt and the proof-complete receipt;
- canonical producer-restoration terminal receipt, its three per-stage receipt
  digests and, when applicable, the exact prior failed-closed resume receipt;
- canonical hourly-activation receipt with the exact scheduled controller,
  closed result, workflow freeze/reactivation boundaries, live manifest digest
  and freshness-attestation digest; retain Tests run IDs/attempts, controller
  request digest and transaction/verification receipt digests only for
  `dispatched` (the canonical receipt records nulls for `no_change`);
- transaction receipt with submission state, exact candidate ID/message,
  rollback target and verified restored deployment ID/digest/reason if rollback
  occurred.
  Raw rollback API responses and compared manifest copies are not included in
  the uploaded evidence artifact; do not label the result a full rollback
  verifier receipt.

Alert on any of the following:

- accepted `main` is newer than the live Railway source commit by more than two
  controller intervals plus the normal CI/build budget;
- `/healthz` and `/railway-release.json` disagree on source or tree identity;
- provider and public origins differ by manifest or critical inventory digest;
- the Railway service gains an attached source, cron or volume;
- a verified receipt is absent after a reported successful changed deployment;
- collector freshness degrades even while deployments remain healthy.

When diagnosing lag, start at the observation clock and walk forward through
candidate, admission, deployment and verification. Redeploying an unchanged
bundle cannot repair a stopped collector or an unaccepted host snapshot.

## Rejected alternatives

**Direct Hetzner-to-Railway sync.** Fast, but it places public deployment
authority and a production secret on the most exposed, stateful host. It also
bypasses Git review, rights checks and reproducible release history.

**Mount the Hetzner warehouse in Railway.** This would mix private evidence and
public serving, create a cross-provider availability dependency, and make
rollback depend on mutable remote state.

**Attach Railway directly to the repository.** Simpler deployment wiring, but
it omits the exact sealed-bundle admission step and can publish a source tip that
has not passed the complete public contract.

**Deploy after every collector commit.** It increases build cost and overlapping
failure states without improving the observation cadence. Hourly level-triggered
coalescing publishes the latest admitted aggregate state with one serialized
transaction.
