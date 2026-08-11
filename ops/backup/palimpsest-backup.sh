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
backup_root="${PALIMPSEST_BACKUP_DIR:-/home/deploy/backups/palimpsest}"
retention_days="${PALIMPSEST_BACKUP_RETENTION_DAYS:-14}"
minimum_free_mb="${PALIMPSEST_BACKUP_MIN_FREE_MB:-1024}"
copy_root="${PALIMPSEST_BACKUP_COPY_DIR:-}"
copy_hook="${PALIMPSEST_BACKUP_HOOK:-}"
compose_project="${PALIMPSEST_COMPOSE_PROJECT:-palimpsest}"

require_absolute_nonroot_path PALIMPSEST_ROOT "$repo_root"
require_absolute_nonroot_path PALIMPSEST_BACKUP_DIR "$backup_root"
[[ "$retention_days" =~ ^[0-9]+$ ]] || die "retention days must be an integer"
(( retention_days >= 1 && retention_days <= 3650 )) || \
  die "retention days must be between 1 and 3650"
[[ "$minimum_free_mb" =~ ^[0-9]+$ ]] || die "minimum free MB must be an integer"
(( minimum_free_mb >= 64 )) || die "minimum free MB must be at least 64"
[[ "$compose_project" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || \
  die "unsafe Compose project name: $compose_project"

for command_name in docker flock sha256sum tar find awk date hostname df \
  mkdir dirname basename mv cp rm; do
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
compose_file="$repo_root/ops/docker/docker-compose.prod.yml"
compose_env="$repo_root/ops/docker/.env"
[[ -r "$compose_file" ]] || die "Compose file is not readable: $compose_file"
[[ -r "$compose_env" ]] || die "production env is not readable: $compose_env"
[[ -d "$state_root/readings" ]] || die "readings directory is missing from state root"
[[ -d "$state_root/data" ]] || die "data directory is missing from state root"

mkdir -p -- "$backup_root"
backup_root="$(cd "$backup_root" && pwd -P)"
require_absolute_nonroot_path PALIMPSEST_BACKUP_DIR "$backup_root"
[[ "$backup_root" != "$repo_root" ]] || die "backup directory cannot be the repository"
[[ "$backup_root/" != "$repo_root/"* ]] || \
  die "backup directory cannot live inside the repository"
[[ "$backup_root" != "$state_root" ]] || die "backup directory cannot equal the state root"
[[ "$backup_root/" != "$state_root/"* && "$state_root/" != "$backup_root/"* ]] || \
  die "backup directory and state root must not contain one another"

exec 9>"$backup_root/.backup.lock"
flock -n 9 || die "another backup is already running"

available_kb="$(df -Pk "$backup_root" | awk 'NR == 2 {print $4}')"
[[ "$available_kb" =~ ^[0-9]+$ ]] || die "could not determine free disk space"
(( available_kb >= minimum_free_mb * 1024 )) || \
  die "less than ${minimum_free_mb} MiB free in $backup_root"

snapshot_id="$(date -u +%Y%m%dT%H%M%SZ)"
final_dir="$backup_root/$snapshot_id"
staging_dir="$backup_root/.incomplete-${snapshot_id}.$$"
copy_staging=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$staging_dir" && -d "$staging_dir" && \
        "$(dirname -- "$staging_dir")" == "$backup_root" && \
        "$(basename -- "$staging_dir")" == .incomplete-* ]]; then
    rm -rf -- "$staging_dir"
  fi
  if [[ -n "$copy_staging" && -d "$copy_staging" && \
        "$(basename -- "$copy_staging")" == .incomplete-* ]]; then
    rm -rf -- "$copy_staging"
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

log "archiving readings/ and data/"
tar --create --gzip --file "$staging_dir/artifacts.tar.gz" \
  --directory "$state_root" -- readings data
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
  printf 'format_version=1\n'
  printf 'snapshot_id=%s\n' "$snapshot_id"
  printf 'created_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'host=%s\n' "$(hostname)"
  printf 'compose_project=%s\n' "$compose_project"
  printf 'postgres_version=%s\n' "$postgres_version"
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

if [[ -n "$copy_root" ]]; then
  require_absolute_nonroot_path PALIMPSEST_BACKUP_COPY_DIR "$copy_root"
  # Do not create this path: if it is meant to be a mounted remote and the
  # mount disappeared, creating it would silently put a second copy locally.
  [[ -d "$copy_root" && -w "$copy_root" ]] || \
    die "off-host copy directory is unavailable or not writable: $copy_root"
  copy_root="$(cd "$copy_root" && pwd -P)"
  require_absolute_nonroot_path PALIMPSEST_BACKUP_COPY_DIR "$copy_root"
  [[ "$copy_root" != "$backup_root" ]] || die "copy directory equals backup directory"
  [[ "$copy_root/" != "$backup_root/"* && "$backup_root/" != "$copy_root/"* ]] || \
    die "copy directory and backup directory must not contain one another"
  copy_final="$copy_root/$snapshot_id"
  copy_staging="$copy_root/.incomplete-${snapshot_id}.$$"
  [[ ! -e "$copy_final" && ! -e "$copy_staging" ]] || \
    die "off-host destination already exists for $snapshot_id"
  mkdir -m 0700 -- "$copy_staging"
  cp -a -- "$final_dir/." "$copy_staging/"
  (cd "$copy_staging" && sha256sum --check SHA256SUMS >/dev/null)
  mv -- "$copy_staging" "$copy_final"
  copy_staging=""
  log "copied and revalidated backup: $copy_final"
fi

if [[ -n "$copy_hook" ]]; then
  require_absolute_nonroot_path PALIMPSEST_BACKUP_HOOK "$copy_hook"
  [[ -x "$copy_hook" && -f "$copy_hook" ]] || \
    die "backup hook is not an executable file: $copy_hook"
  "$copy_hook" "$final_dir" "$snapshot_id"
  log "off-host backup hook completed"
fi

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
