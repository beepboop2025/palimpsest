# Backups for the API-only runtime

The current host uses systemd collectors and an API container. The legacy node
backup timer remains coupled to the protected host-release procedure. The
separate `palimpsest-runtime-backup.timer` covers the active API node through the
existing version-4 archive and `node_backup_snapshot.py` verifier.

It retains PostgreSQL, readings, data, analytical state, the evidence wire, and
witness history in `/home/palimpsest/backups/runtime-node`. Every new snapshot
must pass the complete archive/checksum/restore contract before a success receipt
is written. The timer attempts hourly and skips when a verified snapshot is less
than six hours old; failures are retried the next hour. Retention is seven days,
with a 16-GiB minimum free-space preflight. This is an attached-volume backup;
it does not establish external offsite protection.

Installation requires root-owned immutable copies of the reviewed backup runner
and verifier, and the deployed host's Compose file and archive helper, under
`/usr/local/libexec/palimpsest-runtime-backup/source`:

- `ops/backup/palimpsest-backup.sh`
- `ops/backup/node_backup_snapshot.py`
- `ops/docker/docker-compose.prod.yml`
- `ops/docker/cloudflare-radar-token.disabled`
- `scripts/palimpsest_backup_archive.py`

Record each source revision and file hash in the private installation manifest.
The standalone archiver is standard-library-only. Set
`PALIMPSEST_BACKUP_ARCHIVE_RUNTIME_IMAGE` in
`/etc/palimpsest/runtime-backup.env` to a locally available, full `sha256:` image
ID of an official Python runtime. The backup verifies that identity and mounts
the root-owned helper read-only, with no network and the existing capability,
memory and CPU bounds. This avoids depending on the lifecycle of the API image.

Create `ops/docker/.env` in that isolated source with the two mandatory Compose
substitutions (`POSTGRES_PASSWORD`, `DATABASE_URL`) set to obvious unused
discovery placeholders. The archive command only discovers and executes inside
existing containers; `pg_dump` uses the running database container's own
environment. Never use this source directory for Compose `up`, `create`, or a
deployment. No live credential needs to be copied into it.

Install the wrapper, service, and timer from the reviewed source. Create the
backup directory as `palimpsest:palimpsest`, mode 0700, and require its filesystem
to be the existing attached-volume mount. Start one backup and independently
check `/var/lib/palimpsest-runtime-backup/latest-success.json` and the referenced
snapshot before enabling the timer. Validate a PostgreSQL restore into a
temporary isolated instance; never restore over production during this check.

Use the existing [node restore instructions](README.md) for the six-file snapshot.
The direct publisher's incremental bundles and publication-control receipts
remain a separate recovery inventory; this archive does not substitute for them.
