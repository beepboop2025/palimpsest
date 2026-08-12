# Common Crawl off-host evidence backups

This lane protects the private Common Crawl evidence that cannot be recreated
from the public URL Index: selected WARC records, future editorial decisions or
labels, and the SQLite mappings that bind them together. It also carries the
small inbox export and derived context needed to audit the point-in-time join.
The multi-hundred-gigabyte public Parquet mirror remains excluded and
reconstructible.

## Recovery boundary

Each run performs this sequence:

1. Own the lake's `.common-crawl.lock`, shared with imports and WARC retention.
2. Use SQLite's online backup API and copy only allowlisted private state.
3. Validate SQLite integrity, foreign keys, file hashes, and every
   `record_objects` content-addressed WARC mapping.
4. Build a salted GPG AES-256 archive and upload it to a unique snapshot key.
5. Confirm the bucket's Object Lock default, download the complete archive,
   compare SHA-256, decrypt into an isolated directory, and repeat every
   snapshot validation.
6. Upload `RECEIPT.json` last. Its presence is the completion marker; a prefix
   without that receipt is incomplete and must not be restored.

The uploader uses `rclone copyto --immutable`. It never uses `sync`, purge, or a
remote delete operation. The production bucket should be private, versioned,
and created with Object Lock. The production default is 90-day compliance
retention. No user, including the bucket owner, can shorten an uploaded object's
retention window. The 2 TB allocation makes that stronger default practical;
review capacity before increasing snapshot scope or duration.

Object Lock protects objects in Object Storage; client-side encryption protects
private URLs, review state, and selected bytes if storage credentials or the
storage account are exposed. The encryption passphrase must exist in a separate
recovery escrow, not only on the Hetzner node.

## Production configuration

Install `/etc/palimpsest/common-crawl-backup.env` as root-owned mode `0600`, using
`common-crawl-backup.env.example` as the field list. Install a cryptographically
random passphrase at
`/etc/palimpsest/common-crawl-backup.passphrase`, root-owned mode `0400`. Never
commit either file. The systemd service reads the existing root-only S3 remote
configuration and uses its own bucket and prefix from the Palimpsest environment.
The current operator escrow uses the macOS Keychain service
`palimpsest-common-crawl-backup` and the local operator account; retrieve it with
`security find-generic-password -a "$USER" -s palimpsest-common-crawl-backup -w`
only when provisioning or performing an isolated recovery drill. Do not paste
the returned value into a terminal transcript.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-common-crawl-backup.timer
sudo systemctl start palimpsest-common-crawl-backup.service
sudo systemctl status palimpsest-common-crawl-backup.service --no-pager
sudo cat /var/lib/palimpsest-common-crawl-backup/status.json
```

The timer runs weekly with deterministic jitter. A manual start is safe: the
snapshot lock prevents two backups from overlapping, and the warehouse lock
waits for an active import or selected-record write instead of racing it.

The fail-closed top-level allowlist is intentional. Adding a future durable
warehouse directory requires adding it to `INCLUDED_DIRECTORIES` with a test.
Otherwise the backup fails visibly rather than continuing while omitting new
editorial state.

## Isolated recovery drill

Choose only a snapshot prefix that contains `RECEIPT.json`. Download the receipt,
encrypted archive, and checksums without writing over the production warehouse.
Compare the archive SHA-256 with the receipt, then obtain the passphrase from the
off-node escrow and place it in a temporary mode-`0600` file.

```bash
SNAPSHOT=YYYYMMDDTHHMMSSZ
RESTORE_ROOT="$(mktemp -d /var/tmp/palimpsest-cc-restore.XXXXXX)"
rclone copyto \
  "anchor:${PALIMPSEST_CC_BACKUP_BUCKET}/${PALIMPSEST_CC_BACKUP_PREFIX}/v1/snapshots/${SNAPSHOT}/common-crawl-backup.tar.gz.gpg" \
  "$RESTORE_ROOT/backup.tar.gz.gpg" --config=/dev/null --s3-no-check-bucket
gpg --batch --pinentry-mode loopback --passphrase-file /secure/temporary/passphrase \
  --decrypt --output "$RESTORE_ROOT/backup.tar.gz" \
  "$RESTORE_ROOT/backup.tar.gz.gpg"
mkdir "$RESTORE_ROOT/extracted"
tar -xzf "$RESTORE_ROOT/backup.tar.gz" -C "$RESTORE_ROOT/extracted"
python3 /usr/local/libexec/palimpsest-common-crawl/current/backup/common_crawl_backup.py \
  verify "$RESTORE_ROOT/extracted/$SNAPSHOT" --snapshot-id "$SNAPSHOT"
```

This is a verification drill only. The tool deliberately has no command that
replaces the live warehouse. A production restore requires scheduled downtime,
a separately reviewed destination, and a final operator decision after the
isolated verifier succeeds.

## Remaining correlated risk

This closes the off-host node/Volume failure gap. A Hetzner node and Hetzner
Object Storage bucket are separate storage systems, but they remain with one
provider and one location. A second-provider copy is still the stronger option
for provider-wide or location-wide disaster recovery. Do not describe this
single-provider arrangement as geographically or provider independent.
