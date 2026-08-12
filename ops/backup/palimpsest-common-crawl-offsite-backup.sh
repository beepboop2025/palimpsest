#!/usr/bin/env bash
# Encrypt, upload, download, and fully restore-verify one Common Crawl snapshot.

set -Eeuo pipefail
umask 077

log() {
  printf '[palimpsest-common-crawl-backup] %s\n' "$*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_absolute_nonroot_path() {
  local label="$1" value="$2"
  [[ "$value" == /* && "$value" != / ]] \
    || die "$label must be an absolute non-root path"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
snapshot_tool="$script_dir/common_crawl_backup.py"
warehouse="${PALIMPSEST_CC_BACKUP_WAREHOUSE:-/var/lib/palimpsest/common-crawl}"
work_root="${PALIMPSEST_CC_BACKUP_WORK_DIR:-/var/cache/palimpsest-common-crawl-backup}"
status_path="${PALIMPSEST_CC_BACKUP_STATUS_PATH:-/var/lib/palimpsest-common-crawl-backup/status.json}"
revision_file="${PALIMPSEST_CC_BACKUP_REVISION_FILE:-/etc/palimpsest/deployed-commit}"
passphrase_file="${PALIMPSEST_CC_BACKUP_PASSPHRASE_FILE:-/etc/palimpsest/common-crawl-backup.passphrase}"
bucket="${PALIMPSEST_CC_BACKUP_BUCKET:-}"
prefix="${PALIMPSEST_CC_BACKUP_PREFIX:-palimpsest/common-crawl}"
rclone_remote="${PALIMPSEST_CC_BACKUP_RCLONE_REMOTE:-anchor}"
require_object_lock="${PALIMPSEST_CC_BACKUP_REQUIRE_OBJECT_LOCK:-1}"
retention_mode="${PALIMPSEST_CC_BACKUP_RETENTION_MODE:-COMPLIANCE}"
retention_days="${PALIMPSEST_CC_BACKUP_RETENTION_DAYS:-90}"
minimum_free_mb="${PALIMPSEST_CC_BACKUP_MIN_FREE_MB:-1024}"

for command_name in bash cmp curl date df dirname gpg grep mktemp mv python3 \
  readlink realpath rm rclone sha256sum stat tar tr wc; do
  require_command "$command_name"
done
[[ -f "$snapshot_tool" && ! -L "$snapshot_tool" ]] \
  || die "snapshot tool is missing or unsafe: $snapshot_tool"
for specification in \
  "PALIMPSEST_CC_BACKUP_WAREHOUSE:$warehouse" \
  "PALIMPSEST_CC_BACKUP_WORK_DIR:$work_root" \
  "PALIMPSEST_CC_BACKUP_STATUS_PATH:$status_path" \
  "PALIMPSEST_CC_BACKUP_REVISION_FILE:$revision_file" \
  "PALIMPSEST_CC_BACKUP_PASSPHRASE_FILE:$passphrase_file"; do
  IFS=: read -r label value <<<"$specification"
  require_absolute_nonroot_path "$label" "$value"
done
[[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] \
  || die "PALIMPSEST_CC_BACKUP_BUCKET is missing or malformed"
[[ "$prefix" =~ ^[A-Za-z0-9._/-]+$ && "$prefix" != /* && "$prefix" != */ \
    && "$prefix" != *"//"* && "$prefix" != *".."* ]] \
  || die "PALIMPSEST_CC_BACKUP_PREFIX is unsafe"
[[ "$rclone_remote" =~ ^[A-Za-z][A-Za-z0-9_-]{0,31}$ ]] \
  || die "PALIMPSEST_CC_BACKUP_RCLONE_REMOTE is unsafe"
[[ "$require_object_lock" == 1 ]] \
  || die "PALIMPSEST_CC_BACKUP_REQUIRE_OBJECT_LOCK must remain enabled"
[[ "$retention_mode" == GOVERNANCE || "$retention_mode" == COMPLIANCE ]] \
  || die "retention mode must be GOVERNANCE or COMPLIANCE"
[[ "$retention_days" =~ ^[0-9]+$ && "$retention_days" -ge 1 \
    && "$retention_days" -le 3650 ]] \
  || die "retention days must be between 1 and 3650"
[[ "$minimum_free_mb" =~ ^[0-9]+$ && "$minimum_free_mb" -ge 64 ]] \
  || die "minimum free MB must be at least 64"

[[ -d "$warehouse" && ! -L "$warehouse" ]] \
  || die "warehouse is missing or symlinked: $warehouse"
warehouse="$(realpath -e -- "$warehouse")"
[[ -f "$revision_file" && ! -L "$revision_file" ]] \
  || die "deployment revision receipt is missing or unsafe"
[[ -f "$passphrase_file" && ! -L "$passphrase_file" ]] \
  || die "backup passphrase file is missing or unsafe"
passphrase_mode="$(stat -c '%a' "$passphrase_file")"
passphrase_owner="$(stat -c '%u' "$passphrase_file")"
[[ "$passphrase_owner" == "$EUID" ]] || die "backup passphrase owner is unsafe"
[[ "$passphrase_mode" == 400 || "$passphrase_mode" == 600 ]] \
  || die "backup passphrase must have mode 0400 or 0600"
passphrase_bytes="$(wc -c <"$passphrase_file" | tr -d '[:space:]')"
[[ "$passphrase_bytes" =~ ^[0-9]+$ && "$passphrase_bytes" -ge 32 \
    && "$passphrase_bytes" -le 4096 ]] \
  || die "backup passphrase length is outside the reviewed range"

for variable_name in \
  RCLONE_CONFIG_ANCHOR_ACCESS_KEY_ID \
  RCLONE_CONFIG_ANCHOR_SECRET_ACCESS_KEY \
  RCLONE_CONFIG_ANCHOR_ENDPOINT \
  RCLONE_CONFIG_ANCHOR_REGION \
  RCLONE_CONFIG_ANCHOR_TYPE; do
  [[ -n "${!variable_name:-}" ]] || die "required Object Storage variable is missing: $variable_name"
done
[[ "$RCLONE_CONFIG_ANCHOR_TYPE" == s3 ]] || die "rclone remote is not S3"
[[ "$RCLONE_CONFIG_ANCHOR_ENDPOINT" =~ ^https://[a-z0-9.-]+$ ]] \
  || die "Object Storage endpoint is unsafe"
[[ "$RCLONE_CONFIG_ANCHOR_REGION" =~ ^[A-Za-z0-9_-]+$ ]] \
  || die "Object Storage signing region is unsafe"
[[ "$RCLONE_CONFIG_ANCHOR_ACCESS_KEY_ID" =~ ^[A-Za-z0-9._/+=-]+$ \
    && "$RCLONE_CONFIG_ANCHOR_SECRET_ACCESS_KEY" =~ ^[A-Za-z0-9._/+=-]+$ ]] \
  || die "Object Storage credentials contain unsupported characters"

status_root="$(dirname -- "$status_path")"
mkdir -p -- "$work_root" "$status_root"
chmod 0700 "$work_root"
[[ -d "$work_root" && ! -L "$work_root" ]] || die "work root is unsafe"
[[ -d "$status_root" && ! -L "$status_root" ]] || die "status root is unsafe"
work_root="$(realpath -e -- "$work_root")"
[[ "$work_root" != "$warehouse" && "$work_root/" != "$warehouse/"* \
    && "$warehouse/" != "$work_root/"* ]] \
  || die "work root and warehouse must not contain one another"
available_kb="$(df -Pk "$work_root" | awk 'NR == 2 {print $4}')"
[[ "$available_kb" =~ ^[0-9]+$ ]] || die "cannot determine work-root free space"
(( available_kb >= minimum_free_mb * 1024 )) \
  || die "less than ${minimum_free_mb} MiB free in the backup work root"

snapshot_id="$(date -u +%Y%m%dT%H%M%SZ)"
run_root="$(mktemp -d "$work_root/.run-${snapshot_id}.XXXXXX")"
export GNUPGHOME="$run_root/gnupg"
mkdir -m 0700 -- "$GNUPGHOME"
snapshot_root="$run_root/snapshots"
archive="$run_root/common-crawl-backup.tar.gz.gpg"
download="$run_root/downloaded.tar.gz.gpg"
restore_tar="$run_root/restore.tar.gz"
restore_root="$run_root/restore"
receipt="$run_root/RECEIPT.json"
checksum="$run_root/SHA256SUMS"
curl_config="$run_root/curl.conf"
lock_document="$run_root/object-lock.xml"
headers="$run_root/object.headers"
remote_base="${rclone_remote}:${bucket}/${prefix}/v1/snapshots/${snapshot_id}"
object_key="${prefix}/v1/snapshots/${snapshot_id}/common-crawl-backup.tar.gz.gpg"
completed=0

write_status() {
  local state="$1" archive_sha="${2:-}" archive_bytes="${3:-0}"
  local temporary
  temporary="$(mktemp "${status_path}.tmp.XXXXXX")"
  python3 - "$temporary" "$state" "$snapshot_id" "$bucket" "$prefix" \
    "$archive_sha" "$archive_bytes" "$retention_mode" "$retention_days" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, state, snapshot, bucket, prefix, digest, size, mode, days = sys.argv[1:]
value = {
    "schema_version": "palimpsest-common-crawl-offsite-status/v1",
    "status": state,
    "observed_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "snapshot_id": snapshot,
    "provider": "hetzner-object-storage",
    "bucket": bucket,
    "prefix": prefix,
    "archive_sha256": digest or None,
    "archive_bytes": int(size),
    "object_lock": {"mode": mode, "days": int(days)},
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(value, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(path, 0o600)
PY
  mv -f -- "$temporary" "$status_path"
}

cleanup() {
  local result=$?
  trap - EXIT INT TERM
  if (( result != 0 && completed == 0 )); then
    write_status failed "" 0 2>/dev/null || true
  fi
  if [[ -n "${run_root:-}" && -d "$run_root" \
        && "$(dirname -- "$run_root")" == "$work_root" \
        && "$(basename -- "$run_root")" == .run-* ]]; then
    rm -rf -- "$run_root"
  fi
  exit "$result"
}
trap cleanup EXIT INT TERM

printf 'user = "%s:%s"\n' \
  "$RCLONE_CONFIG_ANCHOR_ACCESS_KEY_ID" \
  "$RCLONE_CONFIG_ANCHOR_SECRET_ACCESS_KEY" >"$curl_config"
chmod 0600 "$curl_config"

verify_bucket_lock() {
  local endpoint="${RCLONE_CONFIG_ANCHOR_ENDPOINT#https://}" code
  code="$(curl --config "$curl_config" --silent --show-error \
    --aws-sigv4 "aws:amz:${RCLONE_CONFIG_ANCHOR_REGION}:s3" \
    --output "$lock_document" --write-out '%{http_code}' \
    "https://${bucket}.${endpoint}/?object-lock")"
  [[ "$code" == 200 ]] || die "Object Lock configuration probe returned HTTP $code"
  grep -q '<ObjectLockEnabled>Enabled</ObjectLockEnabled>' "$lock_document" \
    || die "bucket Object Lock is not enabled"
  grep -q "<Mode>${retention_mode}</Mode>" "$lock_document" \
    || die "bucket default retention mode differs from the configured mode"
  grep -q "<Days>${retention_days}</Days>" "$lock_document" \
    || die "bucket default retention days differ from the configured duration"
}

verify_object_lock() {
  local key="$1" label="$2"
  local endpoint="${RCLONE_CONFIG_ANCHOR_ENDPOINT#https://}" code
  code="$(curl --config "$curl_config" --silent --show-error --head \
    --aws-sigv4 "aws:amz:${RCLONE_CONFIG_ANCHOR_REGION}:s3" \
    --output "$headers" --write-out '%{http_code}' \
    "https://${bucket}.${endpoint}/${key}")"
  [[ "$code" == 200 ]] \
    || die "uploaded ${label} retention probe returned HTTP $code"
  tr -d '\r' <"$headers" | grep -qi "^x-amz-object-lock-mode: ${retention_mode}$" \
    || die "uploaded ${label} does not carry the expected Object Lock mode"
  tr -d '\r' <"$headers" | grep -qi '^x-amz-object-lock-retain-until-date: ' \
    || die "uploaded ${label} has no retention deadline"
}

verify_bucket_lock
mkdir -m 0700 -- "$snapshot_root" "$restore_root"
log "creating a lock-consistent private evidence snapshot"
python3 "$snapshot_tool" create \
  --warehouse "$warehouse" \
  --output-root "$snapshot_root" \
  --snapshot-id "$snapshot_id" \
  --revision-file "$revision_file"

log "encrypting the validated snapshot with AES-256"
tar --create --gzip --directory "$snapshot_root" "$snapshot_id" | \
  gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file "$passphrase_file" \
    --symmetric --cipher-algo AES256 --compress-algo none \
    --output "$archive"
[[ -s "$archive" ]] || die "encrypted archive is empty"
archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
archive_bytes="$(stat -c '%s' "$archive")"
printf '%s  %s\n' "$archive_sha" common-crawl-backup.tar.gz.gpg >"$checksum"

rclone_flags=(
  --config=/dev/null
  --s3-no-check-bucket
  --immutable
  --transfers=1
  --checkers=2
  --retries=5
  --low-level-retries=10
)
log "uploading encrypted bytes under the append-only snapshot key"
rclone copyto "$archive" \
  "$remote_base/common-crawl-backup.tar.gz.gpg" "${rclone_flags[@]}"
verify_object_lock "$object_key" archive
rclone copyto "$checksum" "$remote_base/SHA256SUMS" "${rclone_flags[@]}"
verify_object_lock "${prefix}/v1/snapshots/${snapshot_id}/SHA256SUMS" checksums

log "downloading and performing an isolated full restore verification"
rclone copyto "$remote_base/common-crawl-backup.tar.gz.gpg" "$download" \
  --config=/dev/null --s3-no-check-bucket --transfers=1 --checkers=2 \
  --retries=5 --low-level-retries=10
download_sha="$(sha256sum "$download" | awk '{print $1}')"
[[ "$download_sha" == "$archive_sha" ]] || die "downloaded archive hash differs"
gpg --batch --yes --pinentry-mode loopback \
  --passphrase-file "$passphrase_file" \
  --decrypt --output "$restore_tar" "$download"
tar --extract --gzip --no-same-owner --no-same-permissions \
  --directory "$restore_root" --file "$restore_tar"
python3 "$snapshot_tool" verify "$restore_root/$snapshot_id" \
  --snapshot-id "$snapshot_id"

source_revision="$(tr -d '\n' <"$revision_file")"
python3 - "$receipt" "$snapshot_id" "$source_revision" "$bucket" "$prefix" \
  "$archive_sha" "$archive_bytes" "$retention_mode" "$retention_days" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, snapshot, revision, bucket, prefix, digest, size, mode, days = sys.argv[1:]
receipt = {
    "schema_version": "palimpsest-common-crawl-offsite-receipt/v1",
    "status": "verified",
    "snapshot_id": snapshot,
    "verified_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source_revision": revision,
    "provider": "hetzner-object-storage",
    "bucket": bucket,
    "prefix": prefix,
    "archive": {"bytes": int(size), "sha256": digest},
    "encryption": "gpg-symmetric-aes256-salted-s2k",
    "verification": "remote-download-sha256-decrypt-extract-sqlite-and-record-validation",
    "object_lock": {"mode": mode, "days": int(days)},
    "public_parquet_mirror_included": False,
}
with open(path, "x", encoding="utf-8") as handle:
    json.dump(receipt, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(path, 0o600)
PY

# The receipt is the commit marker.  It appears only after the remote archive
# itself has completed a real, isolated restore.
rclone copyto "$receipt" "$remote_base/RECEIPT.json" "${rclone_flags[@]}"
verify_object_lock "${prefix}/v1/snapshots/${snapshot_id}/RECEIPT.json" receipt
rclone copyto "$remote_base/RECEIPT.json" "$run_root/receipt.downloaded.json" \
  --config=/dev/null --s3-no-check-bucket --transfers=1 --checkers=2
cmp -s "$receipt" "$run_root/receipt.downloaded.json" \
  || die "remote completion receipt differs from the validated local receipt"

write_status success "$archive_sha" "$archive_bytes"
completed=1
log "backup complete and restore-verified: $snapshot_id"
