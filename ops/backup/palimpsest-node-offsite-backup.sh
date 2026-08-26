#!/usr/bin/env bash
# Encrypt one completed node snapshot, upload it immutably, and prove restore.

set -Eeuo pipefail
umask 077

log() {
  printf '[palimpsest-node-offsite] %s\n' "$*" >&2
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
snapshot_tool="$script_dir/node_backup_snapshot.py"
backup_root="${PALIMPSEST_NODE_OFFSITE_BACKUP_ROOT:-/home/palimpsest/backups/node}"
work_root="${PALIMPSEST_NODE_OFFSITE_WORK_DIR:-/var/cache/palimpsest-node-offsite}"
status_path="${PALIMPSEST_NODE_OFFSITE_STATUS_PATH:-/var/lib/palimpsest-node-offsite/status.json}"
revision_file="${PALIMPSEST_NODE_OFFSITE_REVISION_FILE:-/etc/palimpsest/deployed-commit}"
credentials_directory="${CREDENTIALS_DIRECTORY:-}"
passphrase_file="${credentials_directory}/node-offsite-passphrase"
rclone_config="${credentials_directory}/node-offsite-rclone.conf"
bucket="${PALIMPSEST_NODE_OFFSITE_BUCKET:-}"
prefix="${PALIMPSEST_NODE_OFFSITE_PREFIX:-palimpsest/node}"
rclone_remote="${PALIMPSEST_NODE_OFFSITE_RCLONE_REMOTE:-nodevault}"
retention_mode="${PALIMPSEST_NODE_OFFSITE_RETENTION_MODE:-COMPLIANCE}"
retention_days="${PALIMPSEST_NODE_OFFSITE_RETENTION_DAYS:-90}"
minimum_free_mb="${PALIMPSEST_NODE_OFFSITE_MIN_FREE_MB:-512}"
source_uid="${PALIMPSEST_NODE_OFFSITE_SOURCE_UID:-1001}"
source_gid="${PALIMPSEST_NODE_OFFSITE_SOURCE_GID:-1001}"
postgres_image_id_file="$script_dir/POSTGRES_IMAGE_ID"
redis_image_id_file="$script_dir/REDIS_IMAGE_ID"

for command_name in awk bash cmp curl date df dirname docker flock gpg grep mktemp \
  mv python3 readlink realpath rm rclone seq sha256sum sleep stat tar tr wc; do
  require_command "$command_name"
done
[[ -f "$snapshot_tool" && ! -L "$snapshot_tool" ]] \
  || die "snapshot verifier is missing or unsafe"
for specification in \
  "PALIMPSEST_NODE_OFFSITE_BACKUP_ROOT:$backup_root" \
  "PALIMPSEST_NODE_OFFSITE_WORK_DIR:$work_root" \
  "PALIMPSEST_NODE_OFFSITE_STATUS_PATH:$status_path" \
  "PALIMPSEST_NODE_OFFSITE_REVISION_FILE:$revision_file"; do
  IFS=: read -r label value <<<"$specification"
  require_absolute_nonroot_path "$label" "$value"
done
[[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] \
  || die "PALIMPSEST_NODE_OFFSITE_BUCKET is missing or malformed"
[[ "$prefix" =~ ^[A-Za-z0-9._/-]+$ && "$prefix" != /* && "$prefix" != */ \
    && "$prefix" != *"//"* && "$prefix" != *".."* ]] \
  || die "PALIMPSEST_NODE_OFFSITE_PREFIX is unsafe"
[[ "$rclone_remote" =~ ^[A-Za-z][A-Za-z0-9_-]{0,31}$ ]] \
  || die "PALIMPSEST_NODE_OFFSITE_RCLONE_REMOTE is unsafe"
require_absolute_nonroot_path CREDENTIALS_DIRECTORY "$credentials_directory"
[[ -f "$rclone_config" && ! -L "$rclone_config" ]] \
  || die "runtime rclone credential is missing or unsafe"
[[ "$(stat -c '%u:%g:%a:%h' "$rclone_config")" == "0:0:400:1" ]] \
  || die "runtime rclone credential mode is unsafe"
[[ "$retention_mode" == COMPLIANCE ]] \
  || die "node backup retention mode must remain COMPLIANCE"
[[ "$retention_days" =~ ^[0-9]+$ && "$retention_days" -ge 30 \
    && "$retention_days" -le 3650 ]] \
  || die "retention days must be between 30 and 3650"
[[ "$minimum_free_mb" =~ ^[0-9]+$ && "$minimum_free_mb" -ge 128 ]] \
  || die "minimum free MB must be at least 128"
[[ "$source_uid" =~ ^[0-9]+$ && "$source_gid" =~ ^[0-9]+$ ]] \
  || die "source UID/GID must be numeric"

[[ -d "$backup_root" && ! -L "$backup_root" ]] \
  || die "local backup root is missing or symlinked"
backup_root="$(realpath -e -- "$backup_root")"
[[ -f "$backup_root/.backup.lock" && ! -L "$backup_root/.backup.lock" ]] \
  || die "local backup lock is missing or unsafe"
[[ "$(stat -c '%u:%g:%a:%h' "$backup_root/.backup.lock")" \
    == "${source_uid}:${source_gid}:600:1" ]] \
  || die "local backup lock ownership or mode is unsafe"
[[ -f "$revision_file" && ! -L "$revision_file" ]] \
  || die "deployment revision receipt is missing or unsafe"
revision="$(tr -d '\n' <"$revision_file")"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die "deployment revision is malformed"
[[ -f "$postgres_image_id_file" && ! -L "$postgres_image_id_file" \
    && "$(stat -c '%u:%g:%a:%h' "$postgres_image_id_file")" == "0:0:444:1" ]] \
  || die "pinned PostgreSQL image receipt is missing or unsafe"
postgres_image="$(tr -d '\n' <"$postgres_image_id_file")"
[[ "$postgres_image" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "pinned PostgreSQL image identity is malformed"
[[ -f "$redis_image_id_file" && ! -L "$redis_image_id_file" \
    && "$(stat -c '%u:%g:%a:%h' "$redis_image_id_file")" == "0:0:444:1" ]] \
  || die "pinned Redis image receipt is missing or unsafe"
redis_image="$(tr -d '\n' <"$redis_image_id_file")"
[[ "$redis_image" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "pinned Redis image identity is malformed"
postgres_runtime_version="$(
  docker run --rm --pull never --network none --read-only --log-driver none \
    --security-opt no-new-privileges:true --cap-drop ALL \
    --entrypoint postgres "$postgres_image" --version
)"
[[ "$postgres_runtime_version" == "postgres (PostgreSQL) 16."* ]] \
  || die "pinned PostgreSQL verifier is not major version 16"
redis_runtime_version="$(
  docker run --rm --pull never --network none --read-only --log-driver none \
    --security-opt no-new-privileges:true --cap-drop ALL \
    --entrypoint redis-server "$redis_image" --version
)"
[[ "$redis_runtime_version" == "Redis server v=7."* ]] \
  || die "pinned Redis verifier is not major version 7"
[[ -f "$passphrase_file" && ! -L "$passphrase_file" ]] \
  || die "backup passphrase is missing or unsafe"
[[ "$(stat -c '%u:%g:%a:%h' "$passphrase_file")" == "0:0:400:1" ]] \
  || die "backup passphrase must be root-owned mode 0400 with one link"
python3 - "$passphrase_file" <<'PY' \
  || die "backup passphrase is not one canonical 32-4096 byte line"
import sys
payload = open(sys.argv[1], "rb").read()
if payload.endswith(b"\n"):
    payload = payload[:-1]
if not 32 <= len(payload) <= 4096 or b"\n" in payload or b"\r" in payload or b"\0" in payload:
    raise SystemExit(1)
PY

status_root="$(dirname -- "$status_path")"
mkdir -p -- "$work_root" "$status_root"
chmod 0700 "$work_root" "$status_root"
[[ -d "$work_root" && ! -L "$work_root" ]] || die "work root is unsafe"
[[ -d "$status_root" && ! -L "$status_root" ]] || die "status root is unsafe"
work_root="$(realpath -e -- "$work_root")"
[[ "$work_root" != "$backup_root" && "$work_root/" != "$backup_root/"* \
    && "$backup_root/" != "$work_root/"* ]] \
  || die "work root and backup root must not contain one another"
available_kb="$(df -Pk "$work_root" | awk 'NR == 2 {print $4}')"
[[ "$available_kb" =~ ^[0-9]+$ ]] || die "cannot determine work-root capacity"
(( available_kb >= minimum_free_mb * 1024 )) \
  || die "insufficient work-root capacity"

attempt_id="$(date -u +%Y%m%dT%H%M%SZ)"
run_root="$(mktemp -d "$work_root/.run-${attempt_id}.XXXXXX")"
export GNUPGHOME="$run_root/gnupg"
mkdir -m 0700 -- "$GNUPGHOME"
archive="$run_root/palimpsest-node-backup.tar.gpg"
download="$run_root/downloaded.tar.gpg"
restore_tar="$run_root/restore.tar"
restore_root="$run_root/restore"
receipt="$run_root/RECEIPT.json"
checksum="$run_root/SHA256SUMS"
curl_config="$run_root/curl.conf"
lock_document="$run_root/object-lock.xml"
headers="$run_root/object.headers"
completed=0
snapshot_id=""
archive_sha=""
archive_bytes=0
censorwatch_mode=""

mapfile -t storage_metadata < <(
  python3 - "$rclone_config" "$rclone_remote" "$curl_config" <<'PY'
import configparser
import os
import re
import sys

path, remote, curl_path = sys.argv[1:]
parser = configparser.ConfigParser(interpolation=None)
with open(path, encoding="utf-8") as handle:
    parser.read_file(handle)
if parser.sections() != [remote]:
    raise SystemExit("credential must contain exactly the selected remote")
values = dict(parser[remote])
required = {"type", "provider", "access_key_id", "secret_access_key", "endpoint", "region"}
allowed = required | {"acl", "no_check_bucket"}
if set(values) < required or not set(values) <= allowed:
    raise SystemExit("credential fields are missing or unexpected")
if values["type"] != "s3" or values["provider"] != "Other":
    raise SystemExit("credential is not an explicit S3/Other profile")
if values.get("acl", "private") != "private":
    raise SystemExit("credential does not require private objects")
endpoint = values["endpoint"]
region = values["region"]
credential_pattern = re.compile(r"[A-Za-z0-9._/+=-]+")
if not re.fullmatch(r"https://[a-z0-9.-]+", endpoint):
    raise SystemExit("endpoint is unsafe")
if not re.fullmatch(r"[A-Za-z0-9_-]+", region):
    raise SystemExit("region is unsafe")
if not credential_pattern.fullmatch(values["access_key_id"]) or not credential_pattern.fullmatch(values["secret_access_key"]):
    raise SystemExit("credential characters are unsafe")
with open(curl_path, "x", encoding="utf-8") as handle:
    handle.write(f'user = "{values["access_key_id"]}:{values["secret_access_key"]}"\n')
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(curl_path, 0o600)
print(endpoint)
print(region)
PY
) || die "Object Storage credential cannot be validated"
(( ${#storage_metadata[@]} == 2 )) \
  || die "Object Storage credential metadata is incomplete"
endpoint="${storage_metadata[0]}"
region="${storage_metadata[1]}"

write_status() {
  local state="$1" failure_class="${2:-}"
  local temporary previous_success=""
  if [[ -f "$status_path" && ! -L "$status_path" ]]; then
    previous_success="$(
      python3 - "$status_path" <<'PY' 2>/dev/null || true
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
success = value.get("last_success")
if isinstance(success, dict):
    print(json.dumps(success, sort_keys=True, separators=(",", ":")))
PY
    )"
  fi
  temporary="$(mktemp "${status_path}.tmp.XXXXXX")"
  python3 - "$temporary" "$state" "$attempt_id" "$snapshot_id" "$bucket" \
    "$prefix" "$archive_sha" "$archive_bytes" "$retention_days" \
    "$failure_class" "$previous_success" "$censorwatch_mode" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

(path, state, attempt, snapshot, bucket, prefix, digest, size, days,
 failure_class, previous_success, censorwatch_mode) = sys.argv[1:]
now = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
document = {
    "schema_version": "palimpsest-node-offsite-status/v1",
    "status": state,
    "attempt_id": attempt,
    "observed_at": now,
    "snapshot_id": snapshot or None,
    "provider": "hetzner-object-storage",
    "bucket": bucket,
    "prefix": prefix,
    "archive_sha256": digest or None,
    "archive_bytes": int(size),
    "object_lock": {"mode": "COMPLIANCE", "days": int(days)},
    "censorwatch_mode": censorwatch_mode or None,
    "failure_class": failure_class or None,
    "pending": {"attempt_id": attempt, "snapshot_id": snapshot or None}
    if state == "running" else None,
    "last_success": json.loads(previous_success) if previous_success else None,
}
if state == "success":
    document["last_success"] = {
        "attempt_id": attempt,
        "snapshot_id": snapshot,
        "archive_sha256": digest,
        "archive_bytes": int(size),
        "censorwatch_mode": censorwatch_mode,
        "verified_at": now,
    }
with open(path, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(path, 0o600)
PY
  mv -f -- "$temporary" "$status_path"
}

cleanup() {
  local result="${1:-$?}"
  trap - EXIT INT TERM
  set +e
  if [[ -n "${restore_container:-}" \
        && "${restore_container_started:-0}" == 1 ]]; then
    docker rm -f "$restore_container" >/dev/null 2>&1 || true
  fi
  if [[ -n "${redis_restore_container:-}" \
        && "${redis_restore_container_started:-0}" == 1 ]]; then
    docker rm -f "$redis_restore_container" >/dev/null 2>&1 || true
  fi
  if (( result != 0 && completed == 0 )); then
    write_status failed operational_failure 2>/dev/null || true
  fi
  if [[ -n "${run_root:-}" && -d "$run_root" \
        && "$(dirname -- "$run_root")" == "$work_root" \
        && "$(basename -- "$run_root")" == .run-* ]]; then
    rm -rf -- "$run_root"
  fi
  exit "$result"
}
trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

write_status running
exec 9<"$backup_root/.backup.lock"
flock -s 9 || die "cannot acquire the local backup lease"
snapshot_id="$(
  find "$backup_root" -mindepth 1 -maxdepth 1 -type d \
    -name '????????T??????Z' -printf '%f\n' | LC_ALL=C sort | tail -n 1
)"
[[ "$snapshot_id" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
  || die "no completed local node snapshot exists"
snapshot_path="$backup_root/$snapshot_id"
attempt_key="attempts/$attempt_id"

log "validating local snapshot $snapshot_id under the producer lease"
python3 "$snapshot_tool" verify "$snapshot_path" \
  --snapshot-id "$snapshot_id" --expected-uid "$source_uid" \
  --expected-gid "$source_gid" >"$run_root/local-verification.json"
censorwatch_mode="$(
  python3 - "$run_root/local-verification.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    receipt = json.load(handle)
mode = receipt.get("censorwatch", {}).get("mode")
if (
    receipt.get("schema") != "palimpsest-node-backup-verification.v1"
    or receipt.get("status") != "verified"
    or receipt.get("format_version") != 5
    or mode not in {"absent", "included"}
):
    raise SystemExit("local backup verification receipt is not v5")
print(mode)
PY
)" || die "local backup does not carry a valid v5 CensorWatch mode"

log "encrypting the complete recovery point before network transfer"
python3 "$snapshot_tool" pack "$snapshot_path" --snapshot-id "$snapshot_id" \
  --expected-uid "$source_uid" --expected-gid "$source_gid" \
  --output "$run_root/snapshot.tar"
gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file "$passphrase_file" \
    --symmetric --cipher-algo AES256 --compress-algo none \
    --output "$archive" "$run_root/snapshot.tar"
flock -u 9
exec 9<&-
[[ -s "$archive" ]] || die "encrypted archive is empty"
archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
archive_bytes="$(stat -c '%s' "$archive")"
printf '%s  %s\n' "$archive_sha" palimpsest-node-backup.tar.gpg >"$checksum"

endpoint_host="${endpoint#https://}"

verify_bucket_lock() {
  local code
  code="$(curl --config "$curl_config" --silent --show-error \
    --aws-sigv4 "aws:amz:${region}:s3" \
    --output "$lock_document" --write-out '%{http_code}' \
    "https://${bucket}.${endpoint_host}/?object-lock")"
  [[ "$code" == 200 ]] || die "Object Lock probe returned HTTP $code"
  grep -q '<ObjectLockEnabled>Enabled</ObjectLockEnabled>' "$lock_document" \
    || die "bucket Object Lock is not enabled"
  grep -q '<Mode>COMPLIANCE</Mode>' "$lock_document" \
    || die "bucket default retention is not COMPLIANCE"
  grep -q "<Days>${retention_days}</Days>" "$lock_document" \
    || die "bucket retention days differ from policy"
}

verify_object_lock() {
  local key="$1" label="$2" code retain_until minimum_epoch retain_epoch
  code="$(curl --config "$curl_config" --silent --show-error --head \
    --aws-sigv4 "aws:amz:${region}:s3" \
    --output "$headers" --write-out '%{http_code}' \
    "https://${bucket}.${endpoint_host}/${key}")"
  [[ "$code" == 200 ]] || die "$label retention probe returned HTTP $code"
  tr -d '\r' <"$headers" | grep -qi '^x-amz-object-lock-mode: COMPLIANCE$' \
    || die "$label is not protected by COMPLIANCE retention"
  tr -d '\r' <"$headers" | grep -qi '^x-amz-object-lock-retain-until-date: ' \
    || die "$label has no retention deadline"
  retain_until="$(
    tr -d '\r' <"$headers" \
      | awk -F': ' 'tolower($1) == "x-amz-object-lock-retain-until-date" {print $2}'
  )"
  retain_epoch="$(date -u -d "$retain_until" +%s 2>/dev/null || true)"
  minimum_epoch="$(( $(date -u +%s) + (retention_days - 1) * 86400 ))"
  [[ "$retain_epoch" =~ ^[0-9]+$ && "$retain_epoch" -ge "$minimum_epoch" ]] \
    || die "$label retention deadline is shorter than policy"
}

verify_bucket_lock
remote_base="${rclone_remote}:${bucket}/${prefix}/v1/snapshots/${snapshot_id}/${attempt_key}"
object_base="${prefix}/v1/snapshots/${snapshot_id}/${attempt_key}"
rclone_flags=(
  --config="$rclone_config" --s3-no-check-bucket --immutable --transfers=1
  --checkers=2 --retries=5 --low-level-retries=10
)
log "uploading ciphertext to the unique immutable snapshot key"
rclone copyto "$archive" "$remote_base/palimpsest-node-backup.tar.gpg" \
  "${rclone_flags[@]}"
verify_object_lock "$object_base/palimpsest-node-backup.tar.gpg" archive
rclone copyto "$checksum" "$remote_base/SHA256SUMS" "${rclone_flags[@]}"
verify_object_lock "$object_base/SHA256SUMS" checksums

log "downloading, decrypting, and independently validating the restore"
rclone copyto "$remote_base/palimpsest-node-backup.tar.gpg" "$download" \
  --config="$rclone_config" --s3-no-check-bucket --transfers=1 --checkers=2 \
  --retries=5 --low-level-retries=10
[[ "$(sha256sum "$download" | awk '{print $1}')" == "$archive_sha" ]] \
  || die "downloaded ciphertext hash differs"
gpg --batch --yes --pinentry-mode loopback \
  --passphrase-file "$passphrase_file" \
  --decrypt --output "$restore_tar" "$download"
mkdir -m 0700 -- "$restore_root"
python3 "$snapshot_tool" inspect-outer "$restore_tar" --snapshot-id "$snapshot_id"
tar --extract --no-same-owner --no-same-permissions --directory "$restore_root" \
  --file "$restore_tar"
python3 "$snapshot_tool" verify "$restore_root/$snapshot_id" \
  --snapshot-id "$snapshot_id" --scratch-restore \
  >"$run_root/restore-verification.json"
restored_censorwatch_mode="$(
  python3 - "$run_root/restore-verification.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    receipt = json.load(handle)
if (
    receipt.get("schema") != "palimpsest-node-backup-verification.v1"
    or receipt.get("status") != "verified"
    or receipt.get("format_version") != 5
):
    raise SystemExit("remote restore verification receipt is not v5")
print(receipt.get("censorwatch", {}).get("mode", ""))
PY
)" || die "downloaded backup did not reproduce its v5 verification receipt"
[[ "$restored_censorwatch_mode" == "$censorwatch_mode" ]] || \
  die "downloaded backup changed its CensorWatch mode"

docker run --rm --pull never --network none --read-only --log-driver none \
  --security-opt no-new-privileges:true --cap-drop ALL \
  --entrypoint pg_restore "$postgres_image" --list \
  <"$restore_root/$snapshot_id/postgres.dump" \
  >"$run_root/restored-postgres.list"
cmp -s "$run_root/restored-postgres.list" \
  "$restore_root/$snapshot_id/postgres.list" \
  || die "restored PostgreSQL archive listing differs"
if [[ "$censorwatch_mode" == included ]]; then
  docker run --rm --pull never --network none --read-only --log-driver none \
    --security-opt no-new-privileges:true --cap-drop ALL \
    --entrypoint pg_restore "$postgres_image" --list \
    <"$restore_root/$snapshot_id/censorwatch-postgres.dump" \
    >"$run_root/restored-censorwatch-postgres.list"
  cmp -s "$run_root/restored-censorwatch-postgres.list" \
    "$restore_root/$snapshot_id/censorwatch-postgres.list" \
    || die "restored CensorWatch PostgreSQL archive listing differs"
fi

restore_container="palimpsest-node-restore-${attempt_id,,}-$$"
restore_container="${restore_container//:/-}"
restore_container_started=0
cleanup_restore_container() {
  if (( restore_container_started == 1 )); then
    docker rm -f "$restore_container" >/dev/null 2>&1 || true
  fi
}
log "materializing PostgreSQL into an isolated, networkless verifier"
docker run --detach --pull never --network none --read-only --log-driver none \
  --name "$restore_container" --security-opt no-new-privileges:true \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
  --cap-add SETGID --cap-add SETUID \
  --pids-limit 128 --memory 768m --memory-swap 768m \
  --cpus 1.0 --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=512m \
  --tmpfs /var/run/postgresql:rw,noexec,nosuid,size=16m \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --env POSTGRES_HOST_AUTH_METHOD=trust "$postgres_image" >/dev/null
restore_container_started=1
for _ in $(seq 1 60); do
  docker exec "$restore_container" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$restore_container" pg_isready -U postgres >/dev/null 2>&1 \
  || die "isolated PostgreSQL verifier did not become ready"
docker exec "$restore_container" createdb -U postgres palimpsest_restore
docker exec -i "$restore_container" pg_restore --exit-on-error --no-owner \
  --no-privileges --username postgres --dbname palimpsest_restore \
  <"$restore_root/$snapshot_id/postgres.dump"
for relation in articles collection_logs observation_artifacts ddti_index_snapshots; do
  [[ "$(docker exec "$restore_container" psql --no-psqlrc --tuples-only \
    --no-align --username postgres --dbname palimpsest_restore \
    --command "select to_regclass('public.${relation}') is not null;")" == t ]] \
    || die "isolated restore is missing a required core relation"
done
if [[ "$censorwatch_mode" == included ]]; then
  docker exec "$restore_container" createdb -U postgres censorwatch_restore
  docker exec -i "$restore_container" pg_restore --exit-on-error --no-owner \
    --no-privileges --username postgres --dbname censorwatch_restore \
    <"$restore_root/$snapshot_id/censorwatch-postgres.dump"
  for relation in censored_posts post_deletions deletion_velocity_snapshots; do
    [[ "$(docker exec "$restore_container" psql --no-psqlrc --tuples-only \
      --no-align --username postgres --dbname censorwatch_restore \
      --command "select to_regclass('public.${relation}') is not null;")" == t ]] \
      || die "isolated restore is missing a required CensorWatch relation"
  done
fi
cleanup_restore_container
restore_container_started=0

redis_restore_container="palimpsest-redis-restore-${attempt_id,,}-$$"
redis_restore_container="${redis_restore_container//:/-}"
redis_restore_container_started=0
cleanup_redis_restore_container() {
  if (( redis_restore_container_started == 1 )); then
    docker rm -f "$redis_restore_container" >/dev/null 2>&1 || true
  fi
}
if [[ "$censorwatch_mode" == included ]]; then
  log "materializing CensorWatch Redis in an isolated, networkless verifier"
  docker run --detach --pull never --network none --read-only --log-driver none \
    --name "$redis_restore_container" \
    --security-opt no-new-privileges:true --cap-drop ALL \
    --pids-limit 96 --memory 512m --memory-swap 512m --cpus 0.5 \
    --tmpfs /data:rw,noexec,nosuid,size=512m \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --mount "type=bind,src=$restore_root/$snapshot_id/censorwatch-redis.tar.gz,dst=/snapshot/censorwatch-redis.tar.gz,readonly" \
    --entrypoint /bin/sh "$redis_image" -eu -c \
    'cd /data; tar -xzf /snapshot/censorwatch-redis.tar.gz; exec redis-server --dir /data/redis --appendonly yes --protected-mode yes --bind 127.0.0.1 --port 6379 --save ""' \
    >/dev/null
  redis_restore_container_started=1
  for _ in $(seq 1 60); do
    docker exec "$redis_restore_container" redis-cli -h 127.0.0.1 ping \
      2>/dev/null | grep -qx PONG && break
    sleep 1
  done
  docker exec "$redis_restore_container" redis-cli -h 127.0.0.1 ping \
    | grep -qx PONG \
    || die "isolated CensorWatch Redis verifier did not become ready"
  docker exec "$redis_restore_container" redis-cli -h 127.0.0.1 \
    info persistence \
    | tr -d '\r' >"$run_root/restored-censorwatch-redis-persistence.txt"
  grep -qx 'loading:0' "$run_root/restored-censorwatch-redis-persistence.txt" \
    || die "isolated CensorWatch Redis restore is still loading"
  grep -qx 'aof_enabled:1' "$run_root/restored-censorwatch-redis-persistence.txt" \
    || die "isolated CensorWatch Redis restore did not load AOF state"
  for database in 0 1 2; do
    restored_keys="$(
      docker exec "$redis_restore_container" redis-cli -h 127.0.0.1 \
        -n "$database" dbsize
    )"
    [[ "$restored_keys" =~ ^[0-9]+$ ]] || \
      die "isolated CensorWatch Redis DB $database is unreadable"
  done
  cleanup_redis_restore_container
  redis_restore_container_started=0
fi

python3 - "$receipt" "$snapshot_id" "$revision" "$bucket" "$prefix" \
  "$archive_sha" "$archive_bytes" "$retention_days" "$attempt_id" \
  "$postgres_image" "$redis_image" "$censorwatch_mode" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

(path, snapshot, revision, bucket, prefix, digest, size, days, attempt,
 postgres_image, redis_image, censorwatch_mode) = sys.argv[1:]
receipt = {
    "schema_version": "palimpsest-node-offsite-receipt/v1",
    "status": "isolated_restore_verified",
    "attempt_id": attempt,
    "snapshot_id": snapshot,
    "verified_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source_revision": revision,
    "provider": "hetzner-object-storage",
    "bucket": bucket,
    "prefix": prefix,
    "archive": {"bytes": int(size), "sha256": digest},
    "encryption": "gpg-symmetric-aes256-salted-s2k",
    "verification": "remote-download-sha256-decrypt-safe-extract-node-v5-and-networkless-postgresql16-redis7-restore",
    "postgres_verifier_image": postgres_image,
    "redis_verifier_image": redis_image,
    "censorwatch_mode": censorwatch_mode,
    "object_lock": {"mode": "COMPLIANCE", "days": int(days)},
}
with open(path, "x", encoding="utf-8") as handle:
    json.dump(receipt, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(path, 0o600)
PY

# Receipt-last publication is the remote commit marker.
rclone copyto "$receipt" "$remote_base/RECEIPT.json" "${rclone_flags[@]}"
verify_object_lock "$object_base/RECEIPT.json" receipt
rclone copyto "$remote_base/RECEIPT.json" "$run_root/receipt.downloaded.json" \
  --config="$rclone_config" --s3-no-check-bucket --transfers=1 --checkers=2
cmp -s "$receipt" "$run_root/receipt.downloaded.json" \
  || die "remote receipt differs from the validated local receipt"

write_status success
completed=1
log "off-node backup complete and restore-verified: $snapshot_id"
