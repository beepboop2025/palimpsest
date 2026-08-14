#!/usr/bin/env bash
# Create one self-validating Palimpsest node backup and publish it atomically.
#
# The script deliberately runs pg_dump/pg_restore from the pinned PostgreSQL
# container. The host therefore needs Docker + Compose, not a second set of
# database client binaries that can drift away from the server version.

set -Eeuo pipefail
umask 077

log() {
  printf '[palimpsest-backup] %s\n' "$*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_absolute_nonroot_path() {
  local label="$1"
  local value="$2"
  [[ "$value" == /* ]] || die "$label must be an absolute path: $value"
  [[ "$value" != "/" ]] || die "$label must not be /"
}

repo_root="${PALIMPSEST_ROOT:-/home/deploy/palimpsest}"
state_root="${PALIMPSEST_STATE_ROOT:-}"
analysis_root="${PALIMPSEST_ANALYSIS_ROOT:-/var/lib/palimpsest-analysis}"
newswire_root="${PALIMPSEST_NEWSWIRE_ROOT:-/var/lib/palimpsest/newswire}"
backup_root="${PALIMPSEST_BACKUP_DIR:-/home/deploy/backups/palimpsest}"
retention_days="${PALIMPSEST_BACKUP_RETENTION_DAYS:-14}"
minimum_free_mb="${PALIMPSEST_BACKUP_MIN_FREE_MB:-1024}"
copy_root="${PALIMPSEST_BACKUP_COPY_DIR:-}"
copy_hook="${PALIMPSEST_BACKUP_HOOK:-}"
offsite_encrypted="${PALIMPSEST_BACKUP_OFFSITE_ENCRYPTED:-}"
compose_project="${PALIMPSEST_COMPOSE_PROJECT:-palimpsest}"
artifact_service="${PALIMPSEST_BACKUP_ARTIFACT_SERVICE:-worker}"

require_absolute_nonroot_path PALIMPSEST_ROOT "$repo_root"
require_absolute_nonroot_path PALIMPSEST_ANALYSIS_ROOT "$analysis_root"
require_absolute_nonroot_path PALIMPSEST_NEWSWIRE_ROOT "$newswire_root"
require_absolute_nonroot_path PALIMPSEST_BACKUP_DIR "$backup_root"
[[ "$retention_days" =~ ^[0-9]+$ ]] || die "retention days must be an integer"
(( retention_days >= 1 && retention_days <= 3650 )) || \
  die "retention days must be between 1 and 3650"
[[ "$minimum_free_mb" =~ ^[0-9]+$ ]] || die "minimum free MB must be an integer"
(( minimum_free_mb >= 64 )) || die "minimum free MB must be at least 64"
[[ -z "$copy_root" ]] || \
  die "PALIMPSEST_BACKUP_COPY_DIR is retired; use the isolated node-offsite service"
[[ -z "$copy_hook" ]] || \
  die "PALIMPSEST_BACKUP_HOOK is retired; use the isolated node-offsite service"
[[ -z "$offsite_encrypted" ]] || \
  die "PALIMPSEST_BACKUP_OFFSITE_ENCRYPTED is retired with the generic offsite path"
[[ "$compose_project" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || \
  die "unsafe Compose project name: $compose_project"
[[ "$artifact_service" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || \
  die "unsafe artifact service name: $artifact_service"

for command_name in docker flock sha256sum tar find awk date hostname df \
  mkdir dirname basename mv rm; do
  require_command "$command_name"
done

[[ -d "$repo_root" ]] || die "repository does not exist: $repo_root"
repo_root="$(cd "$repo_root" && pwd -P)"
require_absolute_nonroot_path PALIMPSEST_ROOT "$repo_root"
if [[ -z "$state_root" ]]; then
  state_root="$repo_root"
fi
require_absolute_nonroot_path PALIMPSEST_STATE_ROOT "$state_root"
[[ -d "$state_root" ]] || die "state root does not exist: $state_root"
state_root="$(cd "$state_root" && pwd -P)"
require_absolute_nonroot_path PALIMPSEST_STATE_ROOT "$state_root"
[[ -d "$analysis_root" && ! -L "$analysis_root" ]] || \
  die "analysis root is missing or is not a real directory: $analysis_root"
analysis_root="$(cd "$analysis_root" && pwd -P)"
require_absolute_nonroot_path PALIMPSEST_ANALYSIS_ROOT "$analysis_root"
for analysis_subtree in runs private delivery; do
  [[ -d "$analysis_root/$analysis_subtree" && \
      ! -L "$analysis_root/$analysis_subtree" ]] || \
    die "analysis $analysis_subtree directory is missing or unsafe"
done
[[ -d "$newswire_root" && ! -L "$newswire_root" ]] || \
  die "newswire root is missing or is not a real directory: $newswire_root"
newswire_root="$(cd "$newswire_root" && pwd -P)"
require_absolute_nonroot_path PALIMPSEST_NEWSWIRE_ROOT "$newswire_root"
for newswire_file in \
  newswire-latest.json newswire-versions.jsonl newswire-status.json newswire.lock; do
  [[ -f "$newswire_root/$newswire_file" && \
      ! -L "$newswire_root/$newswire_file" ]] || \
    die "newswire recovery artifact is missing or unsafe: $newswire_file"
done
compose_file="$repo_root/ops/docker/docker-compose.prod.yml"
compose_env="$repo_root/ops/docker/.env"
[[ -r "$compose_file" ]] || die "Compose file is not readable: $compose_file"
[[ -r "$compose_env" ]] || die "production env is not readable: $compose_env"
readings_root="$state_root/readings"
data_root="$state_root/data"
[[ -d "$readings_root" && ! -L "$readings_root" ]] || \
  die "readings directory is missing or is not a real directory"
[[ -d "$data_root" && ! -L "$data_root" ]] || \
  die "data directory is missing or is not a real directory"
readings_root="$(cd "$readings_root" && pwd -P)"
data_root="$(cd "$data_root" && pwd -P)"
require_absolute_nonroot_path PALIMPSEST_READINGS_ROOT "$readings_root"
require_absolute_nonroot_path PALIMPSEST_DATA_ROOT "$data_root"

mkdir -p -- "$backup_root"
backup_root="$(cd "$backup_root" && pwd -P)"
require_absolute_nonroot_path PALIMPSEST_BACKUP_DIR "$backup_root"
[[ "$backup_root" != "$repo_root" ]] || die "backup directory cannot be the repository"
[[ "$backup_root/" != "$repo_root/"* ]] || \
  die "backup directory cannot live inside the repository"
[[ "$backup_root" != "$state_root" ]] || die "backup directory cannot equal the state root"
[[ "$backup_root/" != "$state_root/"* && "$state_root/" != "$backup_root/"* ]] || \
  die "backup directory and state root must not contain one another"
[[ "$backup_root" != "$analysis_root" ]] || \
  die "backup directory cannot equal the analysis root"
[[ "$backup_root/" != "$analysis_root/"* && \
    "$analysis_root/" != "$backup_root/"* ]] || \
  die "backup directory and analysis root must not contain one another"
[[ "$backup_root" != "$newswire_root" ]] || \
  die "backup directory cannot equal the newswire root"
[[ "$backup_root/" != "$newswire_root/"* && \
    "$newswire_root/" != "$backup_root/"* ]] || \
  die "backup directory and newswire root must not contain one another"
[[ "$state_root" != "$analysis_root" ]] || \
  die "state root cannot equal the analysis root"
[[ "$state_root/" != "$analysis_root/"* && \
    "$analysis_root/" != "$state_root/"* ]] || \
  die "state root and analysis root must not contain one another"
[[ "$newswire_root" != "$readings_root" && \
    "$newswire_root/" != "$readings_root/"* && \
    "$readings_root/" != "$newswire_root/"* && \
    "$newswire_root" != "$data_root" && \
    "$newswire_root/" != "$data_root/"* && \
    "$data_root/" != "$newswire_root/"* ]] || \
  die "newswire root must not overlap the archived state subroots"
[[ "$newswire_root" != "$analysis_root" && \
    "$analysis_root/" != "$newswire_root/"* && \
    "$newswire_root/" != "$analysis_root/"* ]] || \
  die "newswire root and analysis root must not contain one another"

exec 9>"$backup_root/.backup.lock"
flock -n 9 || die "another backup is already running"

available_kb="$(df -Pk "$backup_root" | awk 'NR == 2 {print $4}')"
[[ "$available_kb" =~ ^[0-9]+$ ]] || die "could not determine free disk space"
(( available_kb >= minimum_free_mb * 1024 )) || \
  die "less than ${minimum_free_mb} MiB free in $backup_root"

snapshot_id="$(date -u +%Y%m%dT%H%M%SZ)"
final_dir="$backup_root/$snapshot_id"
staging_dir="$backup_root/.incomplete-${snapshot_id}.$$"

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$staging_dir" && -d "$staging_dir" && \
        "$(dirname -- "$staging_dir")" == "$backup_root" && \
        "$(basename -- "$staging_dir")" == .incomplete-* ]]; then
    rm -rf -- "$staging_dir"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

[[ ! -e "$final_dir" ]] || die "backup already exists: $final_dir"
mkdir -m 0700 -- "$staging_dir"

compose=(
  docker compose
  --project-name "$compose_project"
  --env-file "$compose_env"
  -f "$compose_file"
)

artifact_container="$("${compose[@]}" ps -q "$artifact_service")"
[[ "$artifact_container" =~ ^[a-f0-9]{12,64}$ ]] || \
  die "artifact service is not running exactly one container: $artifact_service"
artifact_image="$(docker inspect --format '{{.Image}}' "$artifact_container")"
[[ "$artifact_image" =~ ^sha256:[a-f0-9]{64}$ ]] || \
  die "artifact container has an unsafe image identity: $artifact_image"

verify_bind_mount() {
  local destination="$1"
  local expected="$2"
  local actual_source=""
  local actual_type=""
  local mount_destination mount_source mount_type

  while IFS=$'\t' read -r mount_destination mount_source mount_type; do
    if [[ "$mount_destination" == "$destination" ]]; then
      [[ -z "$actual_source" ]] || \
        die "artifact container has duplicate $destination mounts"
      actual_source="$mount_source"
      actual_type="$mount_type"
    fi
  done < <(
    docker inspect --format \
      '{{range .Mounts}}{{printf "%s\t%s\t%s\n" .Destination .Source .Type}}{{end}}' \
      "$artifact_container"
  )

  [[ "$actual_type" == "bind" && -n "$actual_source" ]] || \
    die "artifact container $destination is not a host bind mount"
  [[ -d "$actual_source" ]] || \
    die "artifact container mount source is not a directory: $actual_source"
  actual_source="$(cd "$actual_source" && pwd -P)"
  expected="$(cd "$expected" && pwd -P)"
  [[ "$actual_source" == "$expected" ]] || \
    die "artifact container $destination maps $actual_source, expected $expected"
}

# Do not trust a running container merely because it belongs to the Compose
# project. Bind its two archive paths to the exact configured state root before
# trusting its image as the capability-bounded archive runtime.
verify_bind_mount /app/readings "$readings_root"
verify_bind_mount /app/data "$data_root"

log "dumping PostgreSQL in custom format"
# The quoted variables below intentionally expand in the container, where the
# official image provides POSTGRES_USER/POSTGRES_DB, not in this host shell.
# shellcheck disable=SC2016
"${compose[@]}" exec -T postgres sh -eu -c \
  'exec pg_dump --format=custom --no-owner --no-privileges --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
  >"$staging_dir/postgres.dump"
[[ -s "$staging_dir/postgres.dump" ]] || die "pg_dump produced an empty archive"

log "validating the PostgreSQL archive with pg_restore --list"
"${compose[@]}" exec -T postgres pg_restore --list \
  <"$staging_dir/postgres.dump" >"$staging_dir/postgres.list"
[[ -s "$staging_dir/postgres.list" ]] || die "pg_restore produced an empty listing"

log "archiving readings/, data/, evidence wire, and private analysis state"
# Use the exact content-addressed image from the inspected worker, but do not
# execute inside that live process. The one-shot archive container has no
# network, a read-only root and source mounts, and only CAP_DAC_READ_SEARCH so
# it can read every producer-owned mode-0600 artifact without mutating it. Its
# fixed helper holds a shared cascade lease only while it reads analytical state.
docker run --rm --pull never --network none --read-only --log-driver none \
  --security-opt no-new-privileges:true --cap-drop ALL \
  --cap-add DAC_READ_SEARCH --user 0:0 --pids-limit 64 \
  --memory 512m --memory-swap 512m --cpus 1.0 \
  --mount "type=bind,src=$readings_root,dst=/source/readings,readonly" \
  --mount "type=bind,src=$data_root,dst=/source/data,readonly" \
  --mount "type=bind,src=$analysis_root,dst=/source/analysis,readonly" \
  --mount "type=bind,src=$newswire_root,dst=/source/newswire,readonly" \
  --entrypoint /usr/local/bin/python3 "$artifact_image" -I -B \
  /app/scripts/palimpsest_backup_archive.py >"$staging_dir/artifacts.tar.gz"
tar --list --gzip --file "$staging_dir/artifacts.tar.gz" \
  >"$staging_dir/artifacts.list"
[[ -s "$staging_dir/artifacts.list" ]] || die "artifact archive listing is empty"

postgres_version="$(
  # shellcheck disable=SC2016
  "${compose[@]}" exec -T postgres sh -eu -c \
    'exec psql --no-psqlrc --tuples-only --no-align --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --command="show server_version;"' \
    2>/dev/null || printf 'unknown'
)"
{
  printf 'format_version=3\n'
  printf 'snapshot_id=%s\n' "$snapshot_id"
  printf 'created_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'host=%s\n' "$(hostname)"
  printf 'compose_project=%s\n' "$compose_project"
  printf 'postgres_version=%s\n' "$postgres_version"
  printf 'artifact_roots=readings,data,newswire,analysis\n'
  printf 'contents=postgres.dump,postgres.list,artifacts.tar.gz,artifacts.list\n'
} >"$staging_dir/MANIFEST.txt"

(
  cd "$staging_dir"
  sha256sum postgres.dump postgres.list artifacts.tar.gz artifacts.list MANIFEST.txt \
    >SHA256SUMS
  sha256sum --check SHA256SUMS >/dev/null
)

# Only a completely validated directory gets the stable timestamp name.
mv -- "$staging_dir" "$final_dir"
staging_dir=""
log "published validated backup: $final_dir"

# Retention is intentionally restricted to direct children with the exact UTC
# snapshot naming convention. Dot-prefixed incomplete work and arbitrary
# operator files are never eligible.
while IFS= read -r -d '' expired; do
  expired_name="$(basename -- "$expired")"
  if [[ "$(dirname -- "$expired")" == "$backup_root" && \
        "$expired_name" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
    rm -rf -- "$expired"
    log "pruned expired local backup: $expired_name"
  fi
done < <(
  find "$backup_root" -mindepth 1 -maxdepth 1 -type d \
    -name '????????T??????Z' -mtime "+$retention_days" -print0
)

log "backup complete: $snapshot_id"
