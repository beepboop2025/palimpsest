# Validated node backups

The Common Crawl evidence Volume has a separate encrypted, Object-Lock-aware
path because it contains private SQLite/WARC relationships and deliberately
excludes the reconstructible bulk mirror. See
[`COMMON-CRAWL-OFFSITE.md`](COMMON-CRAWL-OFFSITE.md). The generic nightly backup
described below still covers PostgreSQL, `readings/`, `data/`, the private
evidence-wire ledger, and the investigative-analysis state and immutable runs.

`palimpsest-backup.sh` creates one timestamped directory containing:

- a PostgreSQL custom-format dump and the successful `pg_restore --list`
  output that validated it;
- one gzip archive of `readings/`, `data/`, private `newswire/`, and private
  `analysis/`, plus its successful tar listing;
- a small, secret-free manifest and SHA-256 checksums for every backup file.

Work is written under `.incomplete-*` and renamed to `YYYYMMDDTHHMMSSZ` only
after both archives validate. The job uses `flock`, so timer catch-up cannot
overlap a manually started backup.

`PALIMPSEST_ROOT` locates immutable application code and Compose configuration.
`PALIMPSEST_STATE_ROOT` separately locates the operator-owned `readings/` and
`data/` directories (normally `/var/lib/palimpsest`). Keeping them separate
prevents collectors from dirtying the git checkout. The archive still uses the
portable top-level names `readings/` and `data/` regardless of the host path.
`PALIMPSEST_ANALYSIS_ROOT` locates the investigative-analysis tree
(normally `/var/lib/palimpsest-analysis`). It must be a real directory with
real `runs/`, `private/`, and `delivery/` children. The delivery subtree is
closed to the single bounded `wire-claim-audits-latest.json` projection and
retains its analysis identity and mode-0644 delivery contract. The archive
records the complete tree under the portable top-level name `analysis/`; it
never writes the host path or private payloads into the text manifest.
`PALIMPSEST_NEWSWIRE_ROOT` locates the exact latest/lineage/status triplet used
by the analytical freezer (normally `/var/lib/palimpsest/newswire`). It is
archived under the portable top-level name `newswire/`.

## Install the nightly timer

From `/home/deploy/palimpsest` on the node:

```bash
sudo install -d -o deploy -g deploy -m 0700 /home/deploy/backups/palimpsest
sudo install -d -o root -g root -m 0755 /etc/palimpsest
sudo install -m 0600 ops/backup/backup.env.example /etc/palimpsest/backup.env
sudo install -m 0644 ops/systemd/palimpsest-backup.service /etc/systemd/system/
sudo install -m 0644 ops/systemd/palimpsest-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-backup.timer
sudo systemctl start palimpsest-backup.service
sudo systemctl status palimpsest-backup.service --no-pager
systemctl list-timers palimpsest-backup.timer
```

## Required pre-change release proof

A node release stops this timer and service before changing the checkout, then
records the newest complete snapshot name. Before an exact-SHA checkout, image
build, migration, receipt change, or candidate process, it starts this service
once while every receipt and immutable host bundle still names the old
deployment. If the removable node-offsite `OnSuccess` drop-in is
installed, the release first installs the reviewed lexically-last
`zz-release-quiesce.conf` drop-in on this local backup service. Its empty
`OnSuccess=` resets every success trigger while the transaction is in flight.
A runtime mask of the offsite service is not sufficient because a unit file in
`/etc/systemd/system` has higher load-path priority than a mask in `/run`.

`systemctl start` and `Result=success` are not sufficient proof. A oneshot whose
conditions do not pass can be skipped without running its command. Once the
timer is stopped, systemd may also garbage-collect the unreferenced service and
erase those result fields before they are read. The release runbook's
`start_and_verify_oneshot` helper therefore creates a transient, After-only
proof pin that does not start the service, retains its exact invocation, and is
removed immediately after requiring `ConditionResult=yes`,
`ExecMainStatus=0`, a fresh invocation ID, and a nonzero monotonic start time.
The release additionally requires a new complete snapshot name and the
standalone snapshot verifier. The verifier independently requires the
exact six-file inventory, nonempty dump/list/manifest artifacts, exact checksum
and manifest inventories, valid PostgreSQL framing, and an archive that
exactly matches its listing:

```bash
PRE_CHANGE_SNAPSHOT_BEFORE="$(latest_node_snapshot)"
start_and_verify_oneshot palimpsest-backup.service
PRE_CHANGE_SNAPSHOT="$(latest_node_snapshot)"
test -n "$PRE_CHANGE_SNAPSHOT"
test "$PRE_CHANGE_SNAPSHOT" != "$PRE_CHANGE_SNAPSHOT_BEFORE"
sudo bash -c 'cd "$1" && sha256sum --check SHA256SUMS' \
  _ "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT"
BACKUP_EXPECTED_INVENTORY=$'MANIFEST.txt\nSHA256SUMS\nartifacts.list\nartifacts.tar.gz\npostgres.dump\npostgres.list'
BACKUP_ACTUAL_INVENTORY="$(sudo find \
  "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT" -mindepth 1 -maxdepth 1 \
  -printf '%f\n' | LC_ALL=C sort)"
test "$BACKUP_ACTUAL_INVENTORY" = "$BACKUP_EXPECTED_INVENTORY"
for backup_file in MANIFEST.txt artifacts.list artifacts.tar.gz \
    postgres.dump postgres.list; do
  sudo test -s "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT/$backup_file"
done
BACKUP_VERIFICATION_JSON="$(sudo python3 \
  ops/backup/node_backup_snapshot.py verify \
  "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT" \
  --snapshot-id "$PRE_CHANGE_SNAPSHOT")"
printf '%s\n' "$BACKUP_VERIFICATION_JSON" | python3 -c '
import json, sys
value = json.load(sys.stdin)
if value.get("schema") != "palimpsest-node-backup-verification.v1" \
        or value.get("status") != "verified":
    raise SystemExit("pre-change snapshot receipt is not verified")
'
```

The `start_and_verify_oneshot` and `latest_node_snapshot` helpers plus the
fixed production `NODE_BACKUP_ROOT` are defined in Step 9 of
[`../DEPLOY-HETZNER.md`](../DEPLOY-HETZNER.md). A failed or skipped proof
blocks every receipt-changing installer. Restore the local backup timer only
after the analysis, Common Crawl/network-lane, and node-offsite bundles plus
the public OSINT sync bundle all match `EXPECTED_DEPLOY_SHA`. Only then
remove the exact temporary
drop-in, reload systemd, and require the captured original `OnSuccess` value to
be restored. A failed transaction leaves the quiesce installed and every
captured timer stopped.

The backup verifies that the always-on `worker` Compose service's `/app/readings`
and `/app/data` are bind-mounted from the exact configured state root. It then
binds those two verified roots plus the exact newswire and analysis roots read-only
into a one-shot container using the worker's exact image digest, no network, a
read-only root, numeric archive ownership, and only `CAP_DAC_READ_SEARCH` as
root. This reads producer-owned mode-0600 private state and immutable run
artifacts without any ownership, mode, or ACL mutation. A missing analysis
root/subtree, missing service, named volume, mismatched host source, or unpinned
image identity fails the backup before publication.
Before the archive stream starts, the image-bundled fixed helper opens
`analysis/private/cascade.lock` without following symlinks and requires a
one-link, mode-0600 regular file owned by numeric UID/GID `10001`. It takes a
blocking shared lock while its in-process, fixed archive writer streams
`analysis/`; it releases that lock before streaming `readings/`, `data/`, and
`newswire/`. The writer stores numeric UID/GID values
with blank account names, rejects links and special members, and reports only a
generic helper failure so a read error cannot leak a private filename. The
helper runs with the image's exact `/usr/local/bin/python3 -I -B`, requires
exactly the `runs/`, `private/`, and `delivery/` top-level analysis directories,
requires the single delivery projection to remain a one-link regular file no
larger than 16 MiB, and rejects staging/malformed run names, symlinks, special files, wrong numeric
owners/modes, excess depth, excess entries, or more than 48 run directories. It
fingerprints the accepted tree and rechecks both that fingerprint and the
opened lock pathname/inode after the complete stream. The analytical runner
holds an exclusive lock on the same file, so run promotion, private-state
replacement, ledger updates, and pruning cannot race the analysis archive. A
missing or weakened lock fails closed.
Container logging is disabled so the private archive stream is not duplicated
into Docker's root-disk JSON logs.
`PALIMPSEST_BACKUP_ARTIFACT_SERVICE` can select another reviewed service with
the same image and exact bind mounts.

For an existing node under `/home/palimpsest/palimpsest`, also install
`ops/systemd/palimpsest-backup.override.example.conf` as
`/etc/systemd/system/palimpsest-backup.service.d/override.conf`, and set the
matching `PALIMPSEST_ROOT`/`PALIMPSEST_BACKUP_DIR` paths in the environment
file before reloading systemd. The ready-to-install
`ops/backup/backup.palimpsest-layout.example.env` provides those paths; create
`/home/palimpsest/backups/node` as mode `0700`, owned by `palimpsest`, first.

The repository script must be executable (`git` preserves that mode). Configure
retention in `/etc/palimpsest/backup.env`. Off-host publication is intentionally
outside this unprivileged job. The historical copy-directory, arbitrary-hook,
and encryption-attestation settings are rejected because a flag cannot prove
that private analysis was encrypted or restored. Use the separately
credentialed, root-owned node-offsite service documented in
[`../node-offsite/README.md`](../node-offsite/README.md). Never reuse the Common
Crawl backup credentials or passphrase.

The job intentionally fails when any included evidence or analysis file is
unreadable. Do
not work around that by changing ownership or widening a private subtree to its
ordinary group or to everyone. Files under the backed-up `readings/` and `data/`
trees must be readable through the reviewed, capability-bounded archive
container. Repair a producer that creates unsupported objects or paths at that
producer's ownership contract; never mutate `data/evidence-documents` modes or
ACLs because its store validates strict private modes.
Do not delete, replace, chown, or chmod the analysis `cascade.lock`; the archive
contract intentionally rejects attempts to work around its locked runtime
identity.

For interactive stack operations, use `ops/docker/prod-compose`. It supplies
the same `.env` to both Compose interpolation and the running containers; a
bare `docker compose -f ...` command can otherwise render defaults before the
service-level `env_file` is loaded.

## Verify and restore safely

Choose one completed snapshot and verify it before extracting anything:

```bash
B=/home/deploy/backups/palimpsest/20260812T031500Z
cd "$B"
sha256sum --check SHA256SUMS
docker compose --project-name palimpsest --env-file /home/deploy/palimpsest/ops/docker/.env \
  -f /home/deploy/palimpsest/ops/docker/docker-compose.prod.yml \
  exec -T postgres pg_restore --list < postgres.dump >/dev/null
```

First restore the database into a **new validation database**. This does not
overwrite production:

```bash
C="docker compose --project-name palimpsest --env-file /home/deploy/palimpsest/ops/docker/.env -f /home/deploy/palimpsest/ops/docker/docker-compose.prod.yml"
$C exec -T postgres createdb -U palimpsest palimpsest_restore
$C exec -T postgres pg_restore --exit-on-error --no-owner --no-privileges \
  -U palimpsest -d palimpsest_restore < "$B/postgres.dump"
$C exec -T postgres psql -U palimpsest -d palimpsest_restore -c '\\dt'
```

Extract node artifacts into a separate inspection directory, never directly
over a live state or analysis tree:

```bash
mkdir -p /home/deploy/restore-check
tar -xzf "$B/artifacts.tar.gz" -C /home/deploy/restore-check
```

Inspection as an unprivileged user intentionally does not restore ownership.
For the final, reviewed recovery, extract into one empty replacement bundle.
Preserve the archive's numeric producer identities and do not resolve container
account names through the host's different passwd database:

```bash
sudo tar --extract --gzip --numeric-owner --same-owner \
  --file "$B/artifacts.tar.gz" --directory /absolute/replacement-bundle
```

The result must have exactly the reviewed top-level roots
`readings/`, `data/`, `newswire/`, and `analysis/`. Verify the restored private
modes, the exact `analysis/delivery/` inventory, and
numeric owners before using them. Only after inspecting both restores should an
operator schedule downtime and separately promote those four roots to
`/var/lib/palimpsest/readings`, `/var/lib/palimpsest/data`,
`/var/lib/palimpsest/newswire`, and `/var/lib/palimpsest-analysis`. Dropping the live database or replacing any
live artifact tree is intentionally not automated by this repository.
