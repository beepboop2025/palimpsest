# Public OSINT host sync

The public OSINT sync is the only writer that advances the authoritative
`osint-china-latest.json` and `readings-ledger.jsonl`. They live under the
root-only `0700` StateDirectory at
`/var/lib/palimpsest-public-osint-sync/authoritative`, not in the runtime-
writable shared readings tree. The shared files are accepted once as a sealed
bootstrap prefix. After that they are an untrusted shadow and are never a sync
target in the protected-only release mode.

Each run fetches `main` into a dedicated root-only bare repository and pins the
fetched object ID. It finds the last commit that changed the OSINT artifact and
extracts both the artifact and `readings-ledger.jsonl` from that exact commit.
The updater rejects oversized or ambiguous JSON, a broken ledger, a non-prefix
ledger, timestamp rollback or equivocation, invalid commit ancestry, a missing
newest OSINT seal, and public Pages bytes that differ from the Git blob.

Under one stable exclusive lock, it replaces the ledger first and the artifact
last. Both writes stage in the protected authority directory, remain root-owned,
are read back and hashed before rename, use `fsync`, and converge to mode
`0444`. The attesting receipt is another `0444` authority file at
`/var/lib/palimpsest-public-osint-sync/authoritative/receipt.json`. A failure
writes only a timestamp and stable error code to `last-failure.json`; a later
success updates the current receipt without erasing that historical evidence.
Candidate generation must be no more than two hours old and no more than five
minutes in the future.

## One-time compatibility bridge

The first protected rollout uses two commits. C0 contains `legacy-mirror` in
the immutable bundle's `release-mode` file. Its installer adds the exact
reviewed `10-compatibility-mirror.conf` systemd drop-in, which invokes the same
validator with `--legacy-readings-mirror` and grants write access only to the
legacy readings directory. After the protected pair is installed, the bridge
advances the legacy ledger first and artifact last while preserving each
existing file's owner, group, and mode. Offline verification in this mode also
requires both legacy bytes to equal the protected authority.

C1 contains `protected-only`. Its installer removes the compatibility drop-in
only when the installed file is a root-owned, one-link, mode-0644 exact match
for the reviewed Git blob. A symlink, metadata mismatch, or unknown content
blocks installation. C1 consumers can therefore move to read-only authority
mounts without leaving a second writable copy behind. A rollback to C0
reinstalls the same bridge before its legacy consumers start.

The executable and recovery receipt contract for this transition is in
`deploy-compatibility-seed.sh` and the "First protected rollout" section of the
Hetzner runbook.

Install from a clean exact-SHA checkout only after the analysis installer has
certified the matching image and deployed receipt. On first installation, the
provider must sync successfully before any requiring consumer unit is installed
or any application container is started:

```bash
sudo bash ops/osint-sync/install-host-bundle.sh
sudo systemctl start palimpsest-public-osint-sync.service
sudo systemctl show palimpsest-public-osint-sync.service \
  -p ConditionResult -p Result -p ExecMainStatus
sudo python3 -m json.tool \
  /var/lib/palimpsest-public-osint-sync/authoritative/receipt.json
sudo systemctl enable --now palimpsest-public-osint-sync.timer
```

The consumer units bind-mount the two protected files read-only over their
legacy paths in private mount namespaces. Production containers mount the
whole authority directory read-only, so an atomic rename is visible without
pinning an obsolete inode while unrelated readings remain writable. The timer
runs before the private analysis and Common Crawl context windows. Those two
consumers require a successful ordered sync in their own start transaction.
The watchdog only wants the ordered sync, so it still runs and reports stale
local state when a sync attempt fails.

During a release, `release-proof.json` is a root-owned `0600` handoff installed
from Phase 2. It pins the deployed SHA, fetched main SHA, publication SHA, raw
artifact hash, ledger hash, and Phase 1 resume token. Every dependency-triggered
rerun remains idempotent at that exact publication until Phase 3 verifies that
the final receipt is byte-for-byte unchanged and removes the proof. Normal
timer runs then resume newest-main selection.
