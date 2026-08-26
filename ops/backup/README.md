# Validated node backups

The Common Crawl evidence Volume has a separate encrypted, Object-Lock-aware
path because it contains private SQLite/WARC relationships and deliberately
excludes the reconstructible bulk mirror. See
[`COMMON-CRAWL-OFFSITE.md`](COMMON-CRAWL-OFFSITE.md). The generic nightly backup
described below still covers PostgreSQL, `readings/`, `data/`, the private
evidence-wire ledger, the investigative-analysis state and immutable runs, and
the independent witness's append-only observations. Format v5 also makes the
isolated CensorWatch PostgreSQL/durable-data-Redis pair an explicit all-or-none
recovery component. Its control Redis is intentionally ephemeral and never
restored, so an old heartbeat cannot become current evidence.

`palimpsest-backup.sh` creates one timestamped directory containing:

- a PostgreSQL custom-format dump and the successful `pg_restore --list`
  output that validated it;
- one gzip archive of `readings/`, `data/`, private `newswire/`, private
  `analysis/`, and bounded `witness/` recovery state, plus its successful tar
  listing;
- a small, secret-free manifest and SHA-256 checksums for every backup file.

`PALIMPSEST_CENSORWATCH_BACKUP_MODE` is mandatory and accepts only `absent` or
`included`. `absent` produces the six-file base inventory and fails if any
CensorWatch profile service is running. `included` produces the ten-file
inventory by adding `censorwatch-postgres.dump`,
`censorwatch-postgres.list`, `censorwatch-redis.tar.gz`, and
`censorwatch-redis.list`. It first stops both schedulers plus the data and
control/heartbeat workers; dumps the isolated PostgreSQL database; cleanly
stops the durable data Redis; and archives its complete cold `/data` named
volume. The separate control Redis has no persistence and is excluded. Redis ACLs and all
password/URL files live under Docker's `/run/secrets`, outside that volume, so
the snapshot cannot copy them. The script restores only services that were
running before the fence. Any dump, stop, archive, validation, or restart
failure removes the incomplete directory and publishes neither isolated store.
The root-owned CensorWatch runtime files (staged as `root:10001`, mode `0640`,
one link, never symlinks) are configuration secrets, not state: neither `/etc/palimpsest/censorwatch` nor `/run/secrets`
is a backup source. Preserve
those values only through the approved secret escrow and recreate their exact
host metadata during recovery.

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
`PALIMPSEST_NEWSWIRE_ROOT` locates the exact latest/lineage/status artifacts and
their coordination lock used by the analytical freezer (normally
`/var/lib/palimpsest/newswire`). That closed four-file inventory is archived
under the portable top-level name `newswire/`.
`PALIMPSEST_WITNESS_ROOT` locates the canonical independent observer directory
(normally `/home/palimpsest/.palimpsest-witness`). The v5 archive requires
exactly both nonempty `*.witness.jsonl` histories and the bounded public
freshness latch. Every record is parsed with duplicate-field rejection and
fixed fields, hashes, timestamps, and integer bounds; all three files must be
single-link regular files owned by numeric UID/GID `1001`. A historical
mode-`0755` directory with mode-`0644` histories is accepted because neither is
group/world writable and the histories contain only observations of public
bytes; newer mode-`0700`/`0600` state is also accepted. The freshness latch
must be mode `0600` in either layout. The host captures the real directory's
device/inode with `O_NOFOLLOW`; the container requires that same identity on
its read-only bind before traversal. Numeric ownership and the exact source
modes are preserved.

Format v5 has exactly five portable artifact roots, in this order:
`readings`, `data`, `newswire`, `analysis`, and `witness`. Neither the witness
machine-status envelope nor an arbitrary file from its home directory is part
of that archive; `witness/` is closed to the two append-only histories and
`public-freshness-state.json`.

Those five roots remain one `artifacts.tar.gz`; CensorWatch database/broker
state is not mixed into it. The manifest always records `censorwatch_mode`,
the isolated PostgreSQL and durable data-Redis versions, and the canonical
four-service writer fence. The standalone
verifier accepts only the exact six-file `absent` or ten-file `included`
inventory, verifies every checksum, checks both PostgreSQL custom-format
framings, and inspects the Redis archive against its exact listing without
extracting it. Redis state is closed to `dump.rdb` and Redis 7 multi-part-AOF
files under `appendonlydir/`; configuration, ACL, link, special, traversal, and
unexpected files fail closed.

## Install the nightly timer

From `/home/palimpsest/palimpsest` on the node, as the `palimpsest` service
account (which must also have access to the reviewed local Docker daemon):

```bash
sudo install -d -o palimpsest -g palimpsest -m 0700 /home/palimpsest/backups/node
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

The example starts with `PALIMPSEST_CENSORWATCH_BACKUP_MODE=absent`. Before
CensorWatch activation, change it to `included`, run one manual local backup
and the off-site drill, and retain `included` for every subsequent snapshot.
Never activate the velocity profile while the mode remains `absent`.

## Required pre-change release proof

A node release stops this timer and service before changing the checkout, then
records the newest complete snapshot name. Before an exact-SHA checkout, image
build, migration, receipt change, or candidate process, it starts the installed
service once while every receipt and immutable host bundle still names the old
deployment. That first snapshot is the exact database/core rollback point. If
the installed image predates format v5, it may necessarily be a v4 snapshot;
the release must preserve it rather than pretending the old image explicitly
covered CensorWatch. If the removable node-offsite `OnSuccess` drop-in is
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
exact mode-dependent six- or ten-file inventory, nonempty dump/list/manifest
artifacts, exact checksum and manifest inventories, valid PostgreSQL framing,
and archives that exactly match their listings:

```bash
PRE_CHANGE_SNAPSHOT_BEFORE="$(latest_node_snapshot)"
start_and_verify_oneshot palimpsest-backup.service
PRE_CHANGE_SNAPSHOT="$(latest_node_snapshot)"
test -n "$PRE_CHANGE_SNAPSHOT"
test "$PRE_CHANGE_SNAPSHOT" != "$PRE_CHANGE_SNAPSHOT_BEFORE"
sudo bash -c 'cd "$1" && sha256sum --check SHA256SUMS' \
  _ "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT"
BACKUP_CENSORWATCH_MODE="$(sudo awk -F= \
  '$1 == "censorwatch_mode" {print $2}' \
  "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT/MANIFEST.txt")"
case "$BACKUP_CENSORWATCH_MODE" in
  absent)
    BACKUP_EXPECTED_INVENTORY=$'MANIFEST.txt\nSHA256SUMS\nartifacts.list\nartifacts.tar.gz\npostgres.dump\npostgres.list'
    ;;
  included)
    BACKUP_EXPECTED_INVENTORY=$'MANIFEST.txt\nSHA256SUMS\nartifacts.list\nartifacts.tar.gz\ncensorwatch-postgres.dump\ncensorwatch-postgres.list\ncensorwatch-redis.list\ncensorwatch-redis.tar.gz\npostgres.dump\npostgres.list'
    ;;
  *) exit 1 ;;
esac
BACKUP_ACTUAL_INVENTORY="$(sudo find \
  "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT" -mindepth 1 -maxdepth 1 \
  -printf '%f\n' | LC_ALL=C sort)"
test "$BACKUP_ACTUAL_INVENTORY" = "$BACKUP_EXPECTED_INVENTORY"
for backup_file in MANIFEST.txt artifacts.list artifacts.tar.gz \
    postgres.dump postgres.list; do
  sudo test -s "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT/$backup_file"
done
if test "$BACKUP_CENSORWATCH_MODE" = included; then
  for backup_file in censorwatch-postgres.dump censorwatch-postgres.list \
      censorwatch-redis.list censorwatch-redis.tar.gz; do
    sudo test -s "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT/$backup_file"
  done
fi
BACKUP_VERIFICATION_JSON="$(sudo python3 \
  ops/backup/node_backup_snapshot.py verify \
  "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT" \
  --snapshot-id "$PRE_CHANGE_SNAPSHOT")"
printf '%s\n' "$BACKUP_VERIFICATION_JSON" | python3 -c '
import json, sys
value = json.load(sys.stdin)
if value.get("schema") != "palimpsest-node-backup-verification.v1" \
        or value.get("status") != "verified" \
        or value.get("format_version") != 5 \
        or value.get("censorwatch", {}).get("mode") not in {"absent", "included"}:
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

When upgrading a node whose installed image cannot create v5, the runbook then
builds the exact target image while the database and broker remain drained. It
starts only the candidate default worker, proves that worker quiet and fences
its consumer, and invokes the target backup once before migration or bundle
installation. The target verifier must accept format v5, all five artifact
roots, a positive `witness_history_records` count, and the operator-selected
CensorWatch mode. A velocity activation requires `censorwatch.mode=included`.
That verified candidate
snapshot becomes the transaction's restore snapshot and the temporary worker
is stopped. This bounded bootstrap does not replace the earlier core snapshot
and does not authorize any producer, scheduler, migration, or public write.

The backup verifies that the always-on `worker` Compose service's `/app/readings`
and `/app/data` are bind-mounted from the exact configured state root. It then
binds those two verified roots plus the exact `newswire`, `analysis`, and
`witness` roots read-only into a one-shot container using the worker's exact
image digest, no network, a read-only root, numeric archive ownership, and only
`CAP_DAC_READ_SEARCH` as root. This reads producer-owned mode-0600 private state
and immutable run artifacts without any ownership, mode, or ACL mutation. A
missing artifact root/subtree, missing service, named volume, mismatched host
source, or unpinned image identity fails the backup before publication.
Before the archive stream starts, the image-bundled fixed helper opens
`analysis/private/cascade.lock` without following symlinks and requires a
one-link, mode-0600 regular file owned by numeric UID/GID `10001`. It takes a
blocking shared lock while its in-process, fixed archive writer streams
`analysis/`; it releases that lock before streaming `readings/`, `data/`,
`newswire/`, and `witness/`. The writer stores numeric UID/GID values
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

If an existing node still has a pre-canonical base unit, also install
`ops/systemd/palimpsest-backup.override.example.conf` as
`/etc/systemd/system/palimpsest-backup.service.d/override.conf`, and set the
matching `PALIMPSEST_ROOT`/`PALIMPSEST_BACKUP_DIR` paths in the environment
file before reloading systemd. The compatibility drop-in deliberately matches
the current base unit, so it can remain installed through a forward upgrade.
The ready-to-install `ops/backup/backup.palimpsest-layout.example.env` provides
those paths; create `/home/palimpsest/backups/node` as mode `0700`, owned by
`palimpsest`, first.

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
`readings/`, `data/`, `newswire/`, `analysis/`, and `witness/`. Verify the
restored private modes, the exact `newswire/`, `analysis/delivery/`, and
`witness/` inventories, and
numeric owners before using them. Only after inspecting both restores should an
operator schedule downtime and separately promote those five roots to
`/var/lib/palimpsest/readings`, `/var/lib/palimpsest/data`,
`/var/lib/palimpsest/newswire`, `/var/lib/palimpsest-analysis`, and
`/home/palimpsest/.palimpsest-witness`. Dropping the live database or replacing any
live artifact tree is intentionally not automated by this repository.
