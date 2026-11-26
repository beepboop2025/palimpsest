# Private OSINT authority after native publication

Railway is retired, so `railway_osint_sync.py` can never again see a fresh
`palimpsest.hetzner-railway-publication.v2` receipt or its provider origin. It
now refuses with `publication-stale`, and the context and analysis services
that `Requires=` this unit stay blocked. `native_osint_sync.py` replaces the
Railway anchor with the native pipeline's own receipts. It keeps the same
private authority files, consumers and sandbox.

## What it trusts

- **Selection.** `latest-delivery.json` names the edition. Its bytes must equal
  the retained `deliveries/<release>.json` that the root delivery service wrote
  after the Singapore receiver accepted, read back and served that release. The
  same bytes also sit on the receiver as `receipts/accepted-<release>.json`.
  Both live routes must serve exactly that edition's manifest, and every
  witnessed path is then fetched once; all later public checks use those bytes.
  If the receiver accepted a newer edition meanwhile, the run follows the
  pointer once more; an unchanged pointer with different live bytes refuses.
- **Freshness.** `accepted_at` and the OSINT generation are each at most two
  hours old, with five minutes of future skew, exactly as before.
- **Receiver attestation.** The delivery receipt must say `public_ingress`,
  `fallback_ingress` and `fallback_hostname` `palimpsest.liquilens.in`. Every
  build field must equal the builder's sealed `releases/<release>/build.json`.
- **Owner approval.** `/etc/palimpsest/native-publication-lineage.json` lists
  the historical base and every approved native source generation with its
  pinned record digest and builder hashes. A native base other than the
  historical one must have exactly one root-owned
  `/etc/palimpsest/native-*/selection.json` with that digest. Its pinned source
  bundle supplies the base commit, which must be a single child of the
  historical base with the recorded tree.
- **Byte authority.** The retained `source.bundle` (head `HEAD`, prerequisite
  the native base) is verified and fetched into a temporary private repository
  that borrows canonical objects read-only. The proof requires protected
  revision, historical base, native base and release ancestry, then reads the
  private OSINT artifact and full ledger from the release commit. The public
  manifest copy and the rights receipt must match the build receipt's hashes.
- **Public witnesses.** `https://www.palimpsest.info` (DNS straight to the
  receiver) and `https://palimpsest.liquilens.in` (Cloudflare to the receiver's
  separate fallback route) must serve identical manifest, ledger, restricted
  stub and rights bytes for the release. Each response must carry exactly one
  `X-Hosting-Origin: hetzner-sin1`; only the fallback route may and must carry
  `X-Migration-Fallback: palimpsest.liquilens.in`. Redirects, other URLs and an
  unrestricted OSINT body refuse. Requests name a fixed client, because
  Cloudflare refuses Python's default one.

## Sealed-record continuity

- **Handover.** The first native install requires the frozen last Railway
  receipt to hash to the bridge's `historical_receipt_sha256`, sit on the
  historical base, and chain to the installed Railway receipt through the
  retained Railway receipts. The delivery chain from the selected edition must
  reach the bridge's `native_initial_release_sha`, accepted on the historical
  generation after that Railway receipt was recorded.
- **Predecessors.** Each delivery receipt names the container it replaced.
  Later installs must walk those links, with strictly falling acceptance clocks
  and unique release prefixes, to the exact delivery receipt bytes recorded in
  the installed native receipt. A different receipt for the installed release
  refuses as equivocation.
- **Ledgers.** The finite reviewed forks still decide how a release ledger may
  differ from its Git base ledger. Where a reviewed producer commit is absent
  because native bases carry selected upstream files, the owner-approved
  selection must have carried exactly that ledger blob onto the historical
  base. As before, the installed ledger must extend its own Git base ledger,
  or the reviewed collector prefix when that base was a reviewed fork. Git base
  ledgers may only grow between generations, generation clocks may not move
  back, and equal clocks need identical artifacts. Once a native receipt records the
  captured collector chain, every later edition must retain it byte for byte.

## Deployment contract

This is a separately pinned adapter. Prepare an exact bundle of
`native_osint_sync.py`, `railway_osint_sync.py`, `public_osint_sync.py`,
`verify-host-bundle.sh`, this document and `REVISION` with a complete
`MANIFEST.sha256`. Install it under the root-owned immutable
`/usr/local/libexec/palimpsest-native-osint/<reviewed-source-sha>`, link
`current`, and bind the revision through `/etc/palimpsest/osint-native-source`.
Install `native-publication.conf` as `95-native-publication.conf`. Keep the
Railway bundle, its marker, `90-direct-publication.conf`, `receipt.json`,
`railway-receipt.json` and `railway-history/` untouched for rollback.

Install, check and roll back with the owner's procedure. Run `--check` through
the real unit before activation, keeping dependents blocked. A check reads
inputs, fetches public bytes and creates only a temporary proof repository
under the private state; it never advances authority bytes. Never remove the
context service's `Requires=`.

Installs retain the previous and new complete states under `native-history/`
and switch under `native-transaction.json`. Interrupted switches restore the
previous state before any new verification. `railway-receipt.json` is never
rewritten; every native receipt binds its hash. A pending Railway transaction
refuses until the retired adapter recovers it.

If any receipt, hash, approval, route, clock, ledger rule or predecessor link
fails, the previous authority stays in place and the run reports one stable
refusal code. Never relax a check or hand-edit a receipt to make an edition
pass. Fix the input, or record a new owner decision, and let the next run
verify it.
