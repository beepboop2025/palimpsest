# Validated node backups

The Common Crawl evidence Volume has a separate encrypted, Object-Lock-aware
path because it contains private SQLite/WARC relationships and deliberately
excludes the reconstructible bulk mirror. See
[`COMMON-CRAWL-OFFSITE.md`](COMMON-CRAWL-OFFSITE.md). The generic nightly backup
described below still covers PostgreSQL, `readings/`, and `data/`.

`palimpsest-backup.sh` creates one timestamped directory containing:

- a PostgreSQL custom-format dump and the successful `pg_restore --list`
  output that validated it;
- one gzip archive of `readings/` and `data/`, plus its successful tar listing;
- a small, secret-free manifest and SHA-256 checksums for every backup file.

Work is written under `.incomplete-*` and renamed to `YYYYMMDDTHHMMSSZ` only
after both archives validate. The job uses `flock`, so timer catch-up cannot
overlap a manually started backup.

`PALIMPSEST_ROOT` locates immutable application code and Compose configuration.
`PALIMPSEST_STATE_ROOT` separately locates the operator-owned `readings/` and
`data/` directories (normally `/var/lib/palimpsest`). Keeping them separate
prevents collectors from dirtying the git checkout. The archive still uses the
portable top-level names `readings/` and `data/` regardless of the host path.

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

For an existing node under `/home/palimpsest/palimpsest`, also install
`ops/systemd/palimpsest-backup.override.example.conf` as
`/etc/systemd/system/palimpsest-backup.service.d/override.conf`, and set the
matching `PALIMPSEST_ROOT`/`PALIMPSEST_BACKUP_DIR` paths in the environment
file before reloading systemd. The ready-to-install
`ops/backup/backup.palimpsest-layout.example.env` provides those paths; create
`/home/palimpsest/backups/node` as mode `0700`, owned by `palimpsest`, first.

The repository script must be executable (`git` preserves that mode). Configure
retention or an off-host destination in `/etc/palimpsest/backup.env`. A remote
copy directory must already be mounted and writable; the job refuses to create
it. Local retention does not prune the remote. An uploader hook must be an
absolute executable path, not a shell command.

The job intentionally fails when any included evidence file is unreadable. Do
not work around that by changing ownership or widening a private subtree to its
ordinary group or to everyone. For an isolated mode-0600 artifact that belongs
in the backup, derive the effective service principal rather than assuming the
generic `deploy` layout or the Palimpsest-specific `palimpsest` override:

```bash
BACKUP_USER="$(systemctl show --property=User --value palimpsest-backup.service)"
test -n "$BACKUP_USER"
getent passwd "$BACKUP_USER" >/dev/null

# Replace this placeholder with one reviewed, absolute regular-file path.
EVIDENCE_FILE=/absolute/path/to/the/exact-unreadable-artifact
test -f "$EVIDENCE_FILE" && test ! -L "$EVIDENCE_FILE"
BEFORE_HASH="$(sudo sha256sum -- "$EVIDENCE_FILE")"
BEFORE_OWNER_SIZE="$(sudo stat -c '%u:%g:%s' -- "$EVIDENCE_FILE")"
sudo setfacl -m "u:${BACKUP_USER}:r--" -- "$EVIDENCE_FILE"
sudo -u "$BACKUP_USER" test -r "$EVIDENCE_FILE"
test "$BEFORE_HASH" = "$(sudo sha256sum -- "$EVIDENCE_FILE")"
test "$BEFORE_OWNER_SIZE" = "$(sudo stat -c '%u:%g:%s' -- "$EVIDENCE_FILE")"
sudo getfacl -cp -- "$EVIDENCE_FILE"
```

If the final read check reports a traversal error, use `namei -l` to identify
the exact inaccessible parent and grant that principal execute-only access on
only that parent. POSIX `stat` group bits reflect the ACL mask; the final
`getfacl` output must retain `group::---` and `other::---`, with only the named
backup identity receiving read access.

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
over the live repository:

```bash
mkdir -p /home/deploy/restore-check
tar -xzf "$B/artifacts.tar.gz" -C /home/deploy/restore-check
```

Only after inspecting both restores should an operator schedule downtime and
replace the production database/artifact trees. Dropping the live database is
intentionally not automated by this repository.
