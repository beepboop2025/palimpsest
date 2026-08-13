# Encrypted off-node Palimpsest recovery

This lane copies each validated local node recovery point to a private Hetzner
Object Storage bucket. The database dump and the `readings`, `data`, `newswire`,
and private `analysis` artifacts are encrypted on the Palimpsest host before any
bytes leave it. Every upload is downloaded into an isolated work directory,
decrypted, unpacked, and verified again before `RECEIPT.json` is written as the
remote completion marker.

An off-node copy protects against losing the node and its attached storage. It
does not make one Hetzner account, provider, or location independent; a later
copy in another provider remains the stronger disaster-recovery boundary.

## Non-negotiable storage boundary

Create a **separate Hetzner project** for this recovery bucket, with a separate
S3 key and limited project membership. Hetzner S3 credentials are project-wide,
not bucket-scoped: by default a key can read and write all existing and future
buckets in its project. Never reuse the Anchor/Common Crawl key, bucket,
`nodevault` profile, or encryption passphrase. Hetzner displays an S3 secret
only once, so place the generated value directly into the root-only file and
the approved off-node escrow; do not paste it into shell history or a transcript.

Use HEL1 and create a private bucket with **Object Lock enabled at creation**.
Object Lock cannot be added later. Configure its default retention as exactly
90 days in **COMPLIANCE** mode. Compliance retention cannot be shortened or
overridden, even by the owner. Object Lock enables versioning, but expiry of the
retention period does not delete an object, so add a lifecycle expiration rule
whose age is greater than 90 days: the production recommendation is 120 days.
Never use a lifecycle
age shorter than the lock default.

Official references:

- [S3 credential scope](https://docs.hetzner.com/storage/object-storage/faq/s3-credentials/)
- [Generate S3 credentials](https://docs.hetzner.com/storage/object-storage/getting-started/generating-s3-keys/)
- [Create a bucket](https://docs.hetzner.com/storage/object-storage/getting-started/creating-a-bucket/)
- [Object Lock and default retention](https://docs.hetzner.com/storage/object-storage/howto-protect-objects/protect-object-lock-retention/)
- [Lifecycle rules](https://docs.hetzner.com/storage/object-storage/howto-protect-objects/manage-lifecycle/)
- [Object Storage locations and limits](https://docs.hetzner.com/storage/object-storage/faq/general/)
- [Account-wide Object Storage package](https://docs.hetzner.com/storage/object-storage/overview/)
- [Object Storage pricing](https://www.hetzner.com/storage/object-storage/)

At the current roughly 65 MB recovery point, 90 daily copies are about 5.9 GB
(decimal), well inside the included average 1 TB Object Storage allowance.
Hetzner bills the base Object Storage package account-wide, not per project, so
a separate project on the same billing account should add no new base fee while
the account stays within its included storage and egress. Verify the invoice and
current pricing for the actual account; VAT and any overage still apply.

## Root-only configuration

Do not commit any of these three production files:

1. `/etc/palimpsest/node-offsite.env`, copied from
   `ops/backup/node-offsite.env.example`, mode `0600`, root-owned.
2. `/etc/palimpsest/node-offsite-rclone.conf`, mode `0400`, root-owned.
3. `/etc/palimpsest/node-offsite.passphrase`, a new random value of at least 32
   bytes, mode `0400`, root-owned, with a separately tested off-node escrow copy.

The installer records the running production PostgreSQL image's exact
`sha256:` identity inside the immutable root-owned bundle. The drill uses that
exact local image with `--pull never`, so a tag cannot silently change the
restore environment.

The rclone file has this shape. Substitute only the dedicated recovery key and
secret; do not use the Anchor/Common Crawl values.

```ini
[nodevault]
type = s3
provider = Other
access_key_id = DEDICATED_RECOVERY_ACCESS_KEY
secret_access_key = DEDICATED_RECOVERY_SECRET_KEY
endpoint = https://hel1.your-objectstorage.com
region = hel1
```

Systemd `LoadCredential=` copies the rclone profile and passphrase into its
private credential directory for the duration of one run. They are not exported
as environment variables, and the service receives only read access to the
source snapshot plus private cache/status directories. The installer never
creates, reads, or changes these secrets and never enables the timer.

Install the bundle only from the clean, already deployed Git commit, while both
offsite units are stopped:

```bash
sudo systemctl disable --now palimpsest-node-offsite-backup.timer
sudo systemctl stop \
  palimpsest-node-offsite-backup.service
sudo bash ops/node-offsite/install-host-bundle.sh
```

The installer requires `/etc/palimpsest/deployed-commit` to exactly equal Git
`HEAD`, verifies every copied file against its Git blob, and atomically selects a
root-owned revision under `/usr/local/libexec/palimpsest-node-offsite`. It does
not alter the global deployed-commit receipt.

## Mandatory initial restore drill

Keep the timer disabled until this complete drill succeeds:

```bash
sudo systemctl daemon-reload
sudo systemctl is-enabled palimpsest-node-offsite-backup.timer
sudo systemctl start palimpsest-node-offsite-backup.service
sudo systemctl status palimpsest-node-offsite-backup.service --no-pager
sudo cat /var/lib/palimpsest-node-offsite/status.json
```

This manual start is the initial restore drill because the job does not accept
an upload as success: it immediately downloads the ciphertext into a new private
cache directory, decrypts and safely extracts it there, repeats all snapshot
checks, compares a fresh `pg_restore --list` with the captured database
listing, and restores into an ephemeral networkless PostgreSQL instance. It
also requires the four reviewed core relations before issuing a receipt.
Success means all of the following are true:

- The service exits successfully and status reports `"status": "success"` plus
  the expected snapshot ID and ciphertext SHA-256.
- The new remote prefix contains the encrypted archive, checksum, and a
  byte-for-byte verified `RECEIPT.json` uploaded last. Its status must be
  `"isolated_restore_verified"`.
- Every uploaded object reports 90-day COMPLIANCE retention.
- The service downloaded the remote archive, matched its SHA-256, decrypted it
  into its isolated cache, and repeated the snapshot/database/artifact checks.
- An operator also proves the separately escrowed passphrase can be retrieved
  under the recovery procedure. Never perform a drill over production paths.

Only after recording that successful drill should the recurring job be enabled:

```bash
sudo systemctl enable --now palimpsest-node-offsite-backup.timer
systemctl list-timers palimpsest-node-offsite-backup.timer --no-pager
```

Then install the reviewed `OnSuccess` drop-in so a freshly completed local
snapshot is picked up immediately, while the daily timer remains a catch-up
path after downtime:

```bash
sudo install -d -o root -g root -m 0755 \
  /etc/systemd/system/palimpsest-backup.service.d
sudo install -o root -g root -m 0644 \
  ops/systemd/palimpsest-backup.offsite-trigger.conf \
  /etc/systemd/system/palimpsest-backup.service.d/offsite-trigger.conf
sudo systemctl daemon-reload
```

Rollback removes that one drop-in and disables the node-offsite timer/service;
it never deletes local snapshots or immutable remote objects.

The local producer runs at 03:15 UTC with up to 30 minutes of jitter. This timer
runs daily at 04:15 UTC with its own deterministic delay of up to 30 minutes, and
the shared `.backup.lock` prevents it from reading a snapshot while the producer
is changing the backup set. A remote prefix without `RECEIPT.json` is incomplete
and must never be selected for recovery.

## Recovery semantics

The v3 node snapshot is a **component-restorable** recovery point. PostgreSQL
and each artifact root are captured and independently checked, but they are not
one transactionally atomic snapshot across stores. The drill proves ciphertext,
archive, database-dump, inventory, and checksum integrity; it does not prove
application behavior and it never promotes the restored data into live paths.
A live recovery still requires downtime, an explicit destination, application
validation, and a separately reviewed promotion decision.
