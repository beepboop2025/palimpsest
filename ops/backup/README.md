# Validated node backups

`palimpsest-backup.sh` creates one timestamped directory containing:

- a PostgreSQL custom-format dump and the successful `pg_restore --list`
  output that validated it;
- one gzip archive of `readings/` and `data/`, plus its successful tar listing;
- a small, secret-free manifest and SHA-256 checksums for every backup file.

Work is written under `.incomplete-*` and renamed to `YYYYMMDDTHHMMSSZ` only
after both archives validate. The job uses `flock`, so timer catch-up cannot
overlap a manually started backup.

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
