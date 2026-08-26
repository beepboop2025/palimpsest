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

Publication deployment remains a local-upload operation. Build the immutable
bundle with `ops/railway/build-static-bundle.sh`, then run `railway up` from the
new bundle directory. The bundle intentionally excludes this hidden
`.railway` directory while retaining the public `.well-known` directory.

Railway's backend reports the default restart type (`ON_FAILURE`) as `null` in
the current IaC graph. The definition therefore omits `restartPolicyType` while
retaining `restartPolicyMaxRetries: 5`; an apply is complete only when the
immediate follow-up `railway config plan` reports zero changes.
