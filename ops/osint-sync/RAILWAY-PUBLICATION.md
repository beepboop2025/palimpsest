# Private OSINT authority after direct Railway publication

The earlier synchronizer required generated publication commits to be ancestors
of GitHub `main`. Direct Railway releases are instead generated locally from a
reviewed source base and retained as exact incremental Git bundles. Reinstalling
the old synchronizer at the protected host revision cannot repair that mismatch.

`railway_osint_sync.py` accepts only a verified direct-publication receipt, its
retained bundle and manifest, and identical proof from both configured public
origins. It verifies protected revision → reviewed base → release ancestry using
a private temporary Git repository borrowing read-only canonical objects. Git
cannot fetch lazy objects or contact any remote. It validates the complete ledger,
the newest OSINT seal, the actual OSINT source commit and generation clock (at
most two hours old, at most five minutes future skew). The live OSINT endpoint
must remain a restricted stub tied to the exact master rights decision and
manifest. Restricted values are never copied to the shared readings tree.

Only after every proof passes and the publication receipt and protected marker
are rechecked does the adapter advance the existing private authority. Each
publication ledger must extend its exact Git source-base ledger. The initial
migration must also extend the entire existing private ledger. Later generated
editions may have distinct sealed suffixes only when their retained,
content-addressed publication predecessor chain reaches the exact installed
publication receipt. Both source bases must have ancestor and byte-prefix
continuity. Installed receipts, old seals, generation monotonicity and generation
equivocation checks remain mandatory; no historical row is rewritten or merged.

The September 25 producer repair has one finite additional proof path. Its
5,125-entry build fork must match SHA-256
`9a54fa4f296d215837778d70bcef8e4e4c53e7e92f1828be4c5e7f5643815df8`
and remain byte-identical in the release's audit archive. The candidate must
extend the reviewed 5,537-entry collector prefix with SHA-256
`9c33d2f89feb4682d2ff6b61ac5ef48026ffea02891cdffd98d160f00adfe8ee`.
Both chains are verified, their common 5,102 entries must match, and the sealed
admission receipt must bind the entire captured collector prefix, its head and
clock, the archived fork, and reviewed source ancestry. Unknown repairs refuse.
Later editions still require the exact installed publication's predecessor
chain and must retain that same collector prefix. This only teaches the private
consumer to verify the producer's existing reviewed repair; it neither changes
the publisher's ledger nor authorizes different public or private source values.

Before switching, retain both complete authority states (artifact, full ledger
and Railway receipt) beneath the private `railway-history/<manifest-sha256>/`.
Each directory is immutable to this adapter and binds every byte with a manifest.
Write a durable `railway-transaction.json` naming both states, then install the
ledger, artifact and receipt. On interruption, the next sync first proves every
current member is exactly an old or candidate member, restores the previous
complete state, and retries current publication verification. Foreign bytes,
unsafe metadata or altered retained history refuse recovery without replacement.
`--check` refuses pending recovery and never changes authority. History from
aborted candidates is retained too. The existing `receipt.json`, any
`release-proof.json`, host readings ledger, source checkout, protected deployed
marker, and public bytes remain untouched. The independent canonical host ledger
may advance after a publication capture; this adapter does not overwrite it or
claim that a previously built edition contains those later observations.

The existing publisher regenerates OSINT with its captured publication clock in
`scripts.build_osint_china` on every successful publication cycle. Its timer
reconciles ten seconds after the service becomes inactive (up to five seconds of
jitter), and the publication unit is bounded at fifty minutes. The two-hour
input/proof limit therefore covers the normal cycle without disguising a stalled
publisher. A refreshed OSINT envelope does not make old constituent observations
fresh: their original clocks and availability remain in the private artifact.

## Deployment contract

This is a separately pinned adapter, not a relabeling of the earlier C1 release.
Prepare and review an exact Git-blob bundle containing `railway_osint_sync.py`,
`public_osint_sync.py`, `verify-host-bundle.sh`, this document, `REVISION`, and a
complete `MANIFEST.sha256`. Install under the new root-owned immutable
`/usr/local/libexec/palimpsest-railway-osint/<reviewed-source-sha>` and atomically
link `current`. Bind the reviewed SHA through the root-owned
`/etc/palimpsest/osint-publication-source` marker. Preserve the old synchronizer
bundle, its unit and every legacy receipt for rollback. Never modify canonical
`b22d809bca5ca8aed8255e8a89a06a88dc9cbcb9` or its deployed marker.

Install `railway-publication.conf` as the specifically reviewed service drop-in.
It retains the existing root service sandbox, writable private authority only,
and existing `palimpsest-common-crawl-context.service` dependency on this service.
The canonical Git object directory is exposed through a read-only runtime bind;
`ProtectHome=true` remains. Both public origins use normal certificate and host
verification; redirects and arbitrary URLs are refused. No OpenRouter key is used.

Before activation, verify exact hashes and effective unit/source/marker/runtime
bind identity, run the actual root identity in a disposable private state with
real publication inputs using `--check`, and prove zero authority/canonical/private
warehouse changes. A check creates only a temporary private Git proof directory
and takes the existing stable `sync.lock`; it never advances authority bytes.
The no-enable installer must preserve prior enabled/active states and must not
start archive mirror/filter/backup work or touch the active BLEEDTHROUGH lane.
Only then may the owner start the existing OSINT/context chain and separately
reviewed metadata bridge. The context service's `Requires=` must not be removed.

`--verify-installed` validates the new receipt, exact installed pair, newest seal
and current freshness. The context service receives the private OSINT and ledger
through its existing read-only binds. A public archive bridge must use a separate
allowlisted metadata projection; this private receipt does not authorize publishing
OSINT values or relaxing existing human-review and rights restrictions.

If any origin, hash, rights, clock, ledger prefix or seal fails, retain the old
authority and report the stable refusal code. Never substitute timestamps or
accept an older seal to make a new artifact appear valid. If publication changes
during verification, retry against one complete verified release later.
