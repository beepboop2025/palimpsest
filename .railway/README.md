# Railway infrastructure contract

This project uses Railway Infrastructure as Code rather than the deprecated
per-deployment `railway.json` contract.

From the linked source checkout, review and apply infrastructure separately:

```sh
npm ci --prefix .railway
npm test --prefix .railway
railway config plan
railway config apply
```

Production publication remains a local-directory upload, but it is executed
only by the protected GitHub job through
`ops/railway/deploy-continuous-release.sh`. Do not invoke raw `railway up`
manually: that bypasses exact-current-main admission, rights proof,
exclusive-writer enforcement, recovery and durable evidence. The transaction
builds the immutable bundle with `ops/railway/build-static-bundle.sh`; the
bundle intentionally excludes this hidden `.railway` directory while retaining
the public `.well-known` directory.

Continuous publication is configured to be mediated by GitHub, never by a
Railway credential on the Hetzner collector host. Configuration is not proof of
activation; complete the linked runbook's freeze and canary checklist first.
Host-derived inputs enter only through workflows that import allowlisted
sanitized projections and admit an exact public `main` commit. The scheduled
hourly controller then coalesces accepted commits, re-runs the complete rights
contract, and uploads the sealed bundle through the protected
`palimpsest-railway-production` environment. A non-forced run is a controller
no-op only when the live source SHA already equals current `main`.

The transaction freshly requires the release SHA to equal `origin/main` before
the bundle build and again immediately before mutation. For a changed bundle,
it permits exactly one `railway up` submission, discovers exactly one Railway
deployment by a run-unique `cliMessage`, cross-checks any CLI-reported ID, then
polls that exact ID. Ambiguous output is reconciled against Railway state and
never causes a second upload. `recovered_existing` skips upload only when
source, tree and manifest hashes all match. A forced run reseals the admission
clock, so it normally changes the manifest and uploads even when source and tree
are equal.

The protected environment must also provide
`RAILWAY_EXCLUSIVE_WRITER_ACK=palimpsest-github-environment-v1`, but only after
operators prove that it is the service's sole writer. Railway exposes no
conditional deployment lock: source attachment, Railway cron, dashboard/local
CLI deployments and other workflow credentials must therefore remain absent.
If recovery is required, a known nonterminal candidate is cancelled once and
polled by exact ID. Railway rollback creates a fresh deployment, so restoration
must prove that new ID with the predecessor image digest,
`deploymentRollback` reason and the captured manifest bytes at both origins.

After Railway's `/healthz` gate succeeds, the release verifier binds the exact
deployment ID and image digest to the static-only topology and checks the
release manifest, independently recomputes the complete local sealed-bundle
inventory, and checks the manifest plus every manifest-listed critical file at
both the Railway origin and the custom domain. Retain its secret-free receipt
and digest with the deployment record. See
[`docs/HETZNER-RAILWAY-CONTINUOUS-PUBLICATION.md`](../docs/HETZNER-RAILWAY-CONTINUOUS-PUBLICATION.md)
for activation, freeze, pre-mutation preservation, bounded rollback scope and
incident procedures, and
[`protocol/railway-continuous-release-receipt-v1.schema.json`](../protocol/railway-continuous-release-receipt-v1.schema.json)
for the closed receipt contract.

Railway's backend reports the default restart type (`ON_FAILURE`) as `null` in
the current IaC graph. The definition therefore omits `restartPolicyType` while
retaining `restartPolicyMaxRetries: 5`; an apply is complete only when the
immediate follow-up `railway config plan` reports zero changes.
