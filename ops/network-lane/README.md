# Shared heavy-network lane

This bundle serializes the two heavy jobs authorized on the node:

- the manually started Common Crawl URL Index mirror; and
- the existing six-hour BLEEDTHROUGH measurement.

Both processes take the same non-blocking `fcntl.flock` and retain its file
descriptor until their child has exited and durable receipts have been written.
Busy, not-yet-quiet, and orphan-marker refusals return `EX_TEMPFAIL` (75). The
Common Crawl mirror has deliberately **no timer**; each crawl is an explicit
operator action.

## State and crash contract

`/var/lib/palimpsest/network-lane` is root-owned, traversable by the two service
identities, and not directory-writable by either. Its precreated root-owned
`lane.lock` grants both identities only the `rw` access needed by `flock`, so a
service cannot unlink the name and substitute another inode. A second precreated
root-owned `dataset.lock` is writable only through the analysis identity's named
ACL. Mirrors hold it from before downloader launch until the inventory and
completion receipt are durable; local filters hold it while verifying and
reading that exact inventory. This prevents read/write overlap and serializes
all 6 GiB filter instances. Mutable files live
under `state/` and `receipts/`; those two directories receive named and default
ACLs through the committed tmpfiles file. Every marker and receipt is written
to a same-directory temporary file, `fsync`ed, atomically replaced, and followed
by a directory `fsync`.

`state/active.json` exists for the complete child lifetime. A normal exit writes
a per-invocation receipt, updates `state/mirror-completed.json` for mirror jobs,
then removes the active marker before releasing the lock. If power loss or
`SIGKILL` leaves the marker, a later holder refuses to infer completion. Local
filter readiness also rejects any active marker before trusting an older mirror
stamp: a crashed later mirror may have changed bytes even when path, size, and
Parquet framing still match. Mirror reconciliation acquires the lane and dataset
locks in that order, so it cannot clear this evidence while a filter is reading.

Reconciliation is manual and exact. Stop both units, inspect the marker, copy
its 32-character `invocation_id`, and explain the operator decision:

```bash
sudo systemctl disable --now palimpsest-bleedthrough.timer
sudo systemctl stop palimpsest-bleedthrough.service
sudo systemctl stop 'palimpsest-common-crawl-mirror@*.service'
sudo systemctl stop 'palimpsest-common-crawl-filter@*.service'
sudo python3 /usr/local/libexec/palimpsest-network-lane/current/network_lane.py \
  --state-dir /var/lib/palimpsest/network-lane reconcile \
  --expected-invocation-id 0123456789abcdef0123456789abcdef \
  --reason 'host reboot confirmed; no downloader or prober process remains'
```

The command must run as root, acquires the same lock, requires an exact marker
ID, and preserves the old marker in a reconciliation receipt. Reconciling an
orphaned mirror sets its completion time to the reconciliation time. Therefore
BLEEDTHROUGH still waits at least 900 seconds after the earliest time at which
the operator established that mirror traffic had stopped.

## Reviewed Common Crawl flow

Use Common Crawl's official `cc-downloader` 1.0.1. Install its release binary
only after checking the release SHA-512, then copy it as a real (non-symlink)
regular file at `/usr/local/bin/cc-downloader`, owned by `root:root` and not
group/world-writable. The wrapper additionally pins its SHA-256 in each
root-owned crawl plan and rejects any version other than 1.0.1.

Prepare one `warc`-subset URL Index path manifest. This is a small operator
preparation step, not an unattended mirror:

```bash
CRAWL=CC-MAIN-2026-30
sudo install -d -o root -g root -m 0755 \
  /mnt/HC_Volume_REPLACE/palimpsest/warehouse/common-crawl-mirror
sudo install -d -o palimpsest-analysis -g palimpsest-analysis -m 0750 \
  /mnt/HC_Volume_REPLACE/palimpsest/warehouse/common-crawl-mirror/cc-index
sudo /usr/local/bin/cc-downloader download-paths crawl \
  "$CRAWL" cc-index-table \
  /mnt/HC_Volume_REPLACE/palimpsest/warehouse/common-crawl-mirror \
  --subset warc
sudo chown root:root \
  /mnt/HC_Volume_REPLACE/palimpsest/warehouse/common-crawl-mirror/cc-index-table.paths.gz
sudo chmod 0644 \
  /mnt/HC_Volume_REPLACE/palimpsest/warehouse/common-crawl-mirror/cc-index-table.paths.gz
```

The root-owned plan names the canonical mounted-volume root. The mirror root is
root-owned so UID 10001 cannot replace the reviewed manifest between validation
and exec; only its `cc-index/` data subtree is writable by the downloader. The wrapper
rejects a root-disk volume, symlink components, a mirror outside that volume,
cross-device nesting, and any manifest path other than
`<mirror_root>/cc-index-table.paths.gz`. It also refuses a non-root-owned or
writable manifest, duplicate rows, non-ASCII rows, and every object outside that
crawl's `cc-index-table/subset=warc` Parquet prefix. It records the manifest
SHA-256 and object count. `cc-downloader` confirms the manifest transfer, but
the upstream path list does not supply a cryptographic digest for each Parquet
object; the receipt states this integrity limitation rather than claiming
object-level verification. After `cc-downloader` exits, the wrapper independently
walks the exact crawl subtree. A successful mirror now requires exactly the
manifest paths with no extras, only single-link regular files and no symlinks,
and `PAR1` at both ends of every file. The completion receipt seals sorted
canonical path+byte-size lines with SHA-256 plus object count and total bytes.
This catches truncation, substitution by path, and output drift without reading
and hashing the complete roughly 169 GiB corpus; the receipt states that full
Parquet content hashes remain unavailable.

Copy `mirror-config.example.json` to
`/etc/palimpsest/common-crawl-mirror/$CRAWL.json`, replace the downloader hash,
and keep it root-owned and non-writable by group/other. The volume root and
mirror root must already exist on the dedicated non-root filesystem below
`/mnt`; the unit's writable sandbox and the plan validator align on that mount.
Threads are bounded to 1–10 and per-object retries to 1–1000.
Immediately before launching the child—and while holding the shared lock—the
wrapper also requires at least 256 GiB free on the configured Volume. The
receipt records the before/after free-byte observations so capacity drift is
auditable. This is a start-time reserve, not a promise that an unexpectedly
large upstream crawl cannot consume more space; operators must still monitor
the mounted Volume.

## Install and run

The Common Crawl host installer is the only supported installer for this lane.
It refuses a dirty checkout or mismatched deployed-commit receipt, stages the
helper, this README, and the complete BLEED prober/runtime in a content-hashed
revision directory, Git-blob verifies
the tmpfiles and systemd units, validates all units together, applies and checks
the ACL, and atomically switches `current`. Before every heavy run, systemd also
checks the bundle manifest and compares its `REVISION` with the deployed-commit
receipt; the invocation receipt records that revision and the bundled prober
hash. The prober binds its ASN inventory to the same verified bundle and ignores
legacy environment values that pointed into the protected mutable checkout.
BLEED no longer executes code or reads method configuration from that checkout.
Stop legacy BLEED first: it does not own this lock. Keep it stopped if any
install check fails.

```bash
sudo systemctl disable --now palimpsest-bleedthrough.timer
sudo systemctl stop palimpsest-bleedthrough.service
sudo systemctl stop 'palimpsest-common-crawl-mirror@*.service'
sudo bash ops/common-crawl/install-host-bundle.sh \
  --warehouse-source /mnt/HC_Volume_<volume-id>/palimpsest/warehouse/common-crawl
sudo systemctl enable --now palimpsest-bleedthrough.timer
# Mirrors are never enabled by the installer and have no timer.
CRAWL=CC-MAIN-2026-30
MIRROR_UNIT="palimpsest-common-crawl-mirror@${CRAWL}.service"
sudo systemctl start "$MIRROR_UNIT"
```

The completion stamp is written after success, failure, or forwarded
termination because all three mean the mirror's network activity ended. The
child's exit status is propagated. If a nominally successful leader leaves a
process-group descendant behind, the wrapper terminates it and returns 70; if
the group survives `SIGKILL`, the active marker is deliberately retained and
requires reconciliation. BLEEDTHROUGH acquires the same lock and,
while holding it, refuses to start until the latest mirror completion is at
least 900 seconds old. Exit 75 is configured as a successful skipped BLEED
round, so the next timer activation retries without a false service-health
failure. Local Common Crawl import and context services do not use this lane
because they have `IPAddressDeny=any` and perform no network work.
