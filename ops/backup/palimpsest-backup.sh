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
witness_root="${PALIMPSEST_WITNESS_ROOT:-/home/palimpsest/.palimpsest-witness}"
backup_root="${PALIMPSEST_BACKUP_DIR:-/home/deploy/backups/palimpsest}"
retention_days="${PALIMPSEST_BACKUP_RETENTION_DAYS:-14}"
minimum_free_mb="${PALIMPSEST_BACKUP_MIN_FREE_MB:-1024}"
copy_root="${PALIMPSEST_BACKUP_COPY_DIR:-}"
copy_hook="${PALIMPSEST_BACKUP_HOOK:-}"
offsite_encrypted="${PALIMPSEST_BACKUP_OFFSITE_ENCRYPTED:-}"
compose_project="${PALIMPSEST_COMPOSE_PROJECT:-palimpsest}"
artifact_service="${PALIMPSEST_BACKUP_ARTIFACT_SERVICE:-worker}"
censorwatch_mode="${PALIMPSEST_CENSORWATCH_BACKUP_MODE:-}"

require_absolute_nonroot_path PALIMPSEST_ROOT "$repo_root"
require_absolute_nonroot_path PALIMPSEST_ANALYSIS_ROOT "$analysis_root"
require_absolute_nonroot_path PALIMPSEST_NEWSWIRE_ROOT "$newswire_root"
require_absolute_nonroot_path PALIMPSEST_WITNESS_ROOT "$witness_root"
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
[[ "$censorwatch_mode" == absent || "$censorwatch_mode" == included ]] || \
  die "PALIMPSEST_CENSORWATCH_BACKUP_MODE must be explicitly absent or included"

for command_name in docker flock sha256sum tar find awk date hostname df python3 \
  mkdir dirname basename mv rm rmdir sleep stat seq; do
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
[[ -d "$witness_root" && ! -L "$witness_root" ]] || \
  die "witness root is missing or is not a real directory: $witness_root"
witness_root="$(cd "$witness_root" && pwd -P)"
require_absolute_nonroot_path PALIMPSEST_WITNESS_ROOT "$witness_root"
witness_identity="$(python3 - "$witness_root" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
try:
    path_metadata = os.stat(path, follow_symlinks=False)
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
    )
except (AttributeError, OSError):
    raise SystemExit(1)
try:
    descriptor_metadata = os.fstat(descriptor)
finally:
    os.close(descriptor)
if (
    not stat.S_ISDIR(path_metadata.st_mode)
    or path_metadata.st_dev != descriptor_metadata.st_dev
    or path_metadata.st_ino != descriptor_metadata.st_ino
):
    raise SystemExit(1)
print(f"{descriptor_metadata.st_dev}:{descriptor_metadata.st_ino}")
PY
)" || die "witness root identity cannot be captured safely"
[[ "$witness_identity" =~ ^[0-9]+:[0-9]+$ ]] || \
  die "witness root identity is malformed"
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
for witness_peer in "$repo_root" "$state_root" "$readings_root" "$data_root" \
    "$analysis_root" "$newswire_root" "$backup_root"; do
  [[ "$witness_root" != "$witness_peer" && \
      "$witness_root/" != "$witness_peer/"* && \
      "$witness_peer/" != "$witness_root/"* ]] || \
    die "witness root must not overlap another backup source or destination"
done

exec 9>"$backup_root/.backup.lock"
flock -n 9 || die "another backup is already running"

available_kb="$(df -Pk "$backup_root" | awk 'NR == 2 {print $4}')"
[[ "$available_kb" =~ ^[0-9]+$ ]] || die "could not determine free disk space"
(( available_kb >= minimum_free_mb * 1024 )) || \
  die "less than ${minimum_free_mb} MiB free in $backup_root"

snapshot_id="$(date -u +%Y%m%dT%H%M%SZ)"
final_dir="$backup_root/$snapshot_id"
staging_root="$backup_root/.incomplete-${snapshot_id}.$$"
staging_dir="$staging_root/$snapshot_id"
censorwatch_data_redis_stopped=0
censorwatch_restart_needed=0
censorwatch_running_writers=()
censorwatch_running_writer_containers=()

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if (( censorwatch_restart_needed == 1 )); then
    restart_censorwatch_after_snapshot >/dev/null 2>&1 || \
      log "ERROR: CensorWatch services could not be restored during cleanup"
  fi
  if [[ -n "$staging_root" && -d "$staging_root" && \
        "$(dirname -- "$staging_root")" == "$backup_root" && \
        "$(basename -- "$staging_root")" == .incomplete-* ]]; then
    rm -rf -- "$staging_root"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

[[ ! -e "$final_dir" ]] || die "backup already exists: $final_dir"
mkdir -m 0700 -- "$staging_root" "$staging_dir"

compose=(
  docker compose
  --project-name "$compose_project"
  --env-file "$compose_env"
  -f "$compose_file"
)
censorwatch_compose=("${compose[@]}" --profile velocity)
censorwatch_services=(
  preflight-censorwatch postgres-censorwatch
  redis-censorwatch-data redis-censorwatch-control migrate-censorwatch
  worker-velocity worker-velocity-control
  beat-velocity-data beat-velocity-control
  censorwatch-egress-proxy censorwatch-render-gateway api-censorwatch
)
censorwatch_writers=(
  beat-velocity-data beat-velocity-control
  worker-velocity worker-velocity-control
)

running_compose_container() {
  local service_name="$1"
  local container_output=""
  local -a container_ids=()
  container_output="$(
    docker ps \
      --filter "label=com.docker.compose.project=$compose_project" \
      --filter "label=com.docker.compose.service=$service_name" \
      --format '{{.ID}}'
  )" || die "Docker could not inspect $service_name"
  if [[ -n "$container_output" ]]; then
    mapfile -t container_ids <<<"$container_output"
  fi
  (( ${#container_ids[@]} <= 1 )) || \
    die "multiple running containers found for $service_name"
  if (( ${#container_ids[@]} == 1 )); then
    [[ "${container_ids[0]}" =~ ^[a-f0-9]{12,64}$ ]] || \
      die "unsafe container identity for $service_name"
    printf '%s\n' "${container_ids[0]}"
  fi
}

wait_for_healthy_container() {
  local container_id="$1"
  local health=""
  for _ in $(seq 1 60); do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id")"
    [[ "$health" == healthy ]] && return 0
    if [[ "$health" == missing ]]; then
      [[ "$(docker inspect --format '{{.State.Running}}' "$container_id")" == true ]] \
        && return 0
      return 1
    fi
    [[ "$health" == unhealthy ]] && return 1
    sleep 1
  done
  return 1
}

require_cleanly_stopped_container() {
  local service_name="$1"
  local container_id="$2"
  local stopped_state=""
  stopped_state="$(
    docker inspect --format \
      '{{printf "%s|%d|%t|%s" .State.Status .State.ExitCode .State.OOMKilled .State.Error}}' \
      "$container_id"
  )" || die "Docker could not inspect stopped $service_name"
  [[ "$stopped_state" == 'exited|0|false|' ]] || \
    die "$service_name did not stop cleanly: $stopped_state"
}

restart_censorwatch_after_snapshot() {
  local writer service_container
  if (( censorwatch_data_redis_stopped == 1 )); then
    "${censorwatch_compose[@]}" start redis-censorwatch-data >/dev/null
    service_container="$(running_compose_container redis-censorwatch-data)" || return 1
    [[ -n "$service_container" ]] || return 1
    wait_for_healthy_container "$service_container" || return 1
    censorwatch_data_redis_stopped=0
  fi
  for writer in \
    worker-velocity-control worker-velocity \
    beat-velocity-data beat-velocity-control; do
    if [[ " ${censorwatch_running_writers[*]} " == *" $writer "* ]]; then
      "${censorwatch_compose[@]}" start "$writer" >/dev/null || return 1
      service_container="$(running_compose_container "$writer")" || return 1
      [[ -n "$service_container" ]] || return 1
      wait_for_healthy_container "$service_container" || return 1
    fi
  done
  censorwatch_restart_needed=0
}

if [[ "$censorwatch_mode" == absent ]]; then
  for service_name in "${censorwatch_services[@]}"; do
    service_container="$(running_compose_container "$service_name")" || \
      die "cannot inspect running CensorWatch service: $service_name"
    [[ -z "$service_container" ]] || \
      die "CensorWatch mode is absent but $service_name is running"
  done
  censorwatch_postgres_version=absent
  censorwatch_redis_version=absent
  censorwatch_writer_fence=not-applicable
else
  censorwatch_postgres_container="$(
    running_compose_container postgres-censorwatch
  )" || die "cannot inspect postgres-censorwatch"
  censorwatch_data_redis_container="$(
    running_compose_container redis-censorwatch-data
  )" || die "cannot inspect redis-censorwatch-data"
  censorwatch_control_redis_container="$(
    running_compose_container redis-censorwatch-control
  )" || die "cannot inspect redis-censorwatch-control"
  [[ -n "$censorwatch_postgres_container" ]] || \
    die "included CensorWatch backup requires postgres-censorwatch running"
  [[ -n "$censorwatch_data_redis_container" ]] || \
    die "included CensorWatch backup requires redis-censorwatch-data running"
  [[ -n "$censorwatch_control_redis_container" ]] || \
    die "included CensorWatch backup requires redis-censorwatch-control running"
  migrate_container="$(running_compose_container migrate-censorwatch)" || \
    die "cannot inspect migrate-censorwatch"
  [[ -z "$migrate_container" ]] || \
    die "CensorWatch migration is still running"

  censorwatch_postgres_image="$(
    docker inspect --format '{{.Image}}' "$censorwatch_postgres_container"
  )"
  censorwatch_data_redis_image="$(
    docker inspect --format '{{.Image}}' "$censorwatch_data_redis_container"
  )"
  censorwatch_control_redis_image="$(
    docker inspect --format '{{.Image}}' "$censorwatch_control_redis_container"
  )"
  [[ "$censorwatch_postgres_image" =~ ^sha256:[a-f0-9]{64}$ ]] || \
    die "CensorWatch PostgreSQL image identity is unsafe"
  [[ "$censorwatch_data_redis_image" =~ ^sha256:[a-f0-9]{64}$ ]] || \
    die "CensorWatch data Redis image identity is unsafe"
  [[ "$censorwatch_control_redis_image" =~ ^sha256:[a-f0-9]{64}$ ]] || \
    die "CensorWatch control Redis image identity is unsafe"
  [[ "$censorwatch_data_redis_image" == "$censorwatch_control_redis_image" ]] || \
    die "CensorWatch Redis planes must use the same pinned image"
  censorwatch_postgres_version="$(
    # shellcheck disable=SC2016
    "${censorwatch_compose[@]}" exec -T postgres-censorwatch sh -eu -c \
      'exec psql --no-psqlrc --tuples-only --no-align --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --command="show server_version;"'
  )"
  censorwatch_redis_version="$(
    docker exec "$censorwatch_data_redis_container" redis-server --version \
      | awk '{for (i=1; i<=NF; i++) if ($i ~ /^v=/) {sub(/^v=/, "", $i); print $i; exit}}'
  )"
  [[ "$censorwatch_postgres_version" =~ ^[0-9][A-Za-z0-9.+_-]{0,63}$ ]] || \
    die "CensorWatch PostgreSQL version is malformed"
  [[ "$censorwatch_redis_version" =~ ^[0-9][A-Za-z0-9.+_-]{0,63}$ ]] || \
    die "CensorWatch Redis version is malformed"
  [[ "$censorwatch_postgres_version" == 16.* ]] || \
    die "CensorWatch PostgreSQL must remain major version 16"
  [[ "$censorwatch_redis_version" == 7.* ]] || \
    die "CensorWatch Redis must remain major version 7"

  censorwatch_data_redis_volume="$(
    docker inspect --format \
      '{{range .Mounts}}{{if eq .Destination "/data"}}{{printf "%s\t%s\n" .Type .Name}}{{end}}{{end}}' \
      "$censorwatch_data_redis_container"
  )"
  [[ "$censorwatch_data_redis_volume" == $'volume\t'* ]] || \
    die "CensorWatch data Redis /data is not a named volume"
  censorwatch_data_redis_volume="${censorwatch_data_redis_volume#*$'\t'}"
  [[ "$censorwatch_data_redis_volume" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$ ]] || \
    die "CensorWatch data Redis volume identity is unsafe"
  censorwatch_writer_fence=beat-velocity-data,beat-velocity-control,worker-velocity,worker-velocity-control
fi

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

log "archiving readings/, data/, evidence wire, private analysis, and witness history"
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
  --mount "type=bind,src=$witness_root,dst=/source/witness,readonly" \
  --env "PALIMPSEST_EXPECTED_WITNESS_IDENTITY=$witness_identity" \
  --entrypoint /usr/local/bin/python3 "$artifact_image" -I -B \
  /app/scripts/palimpsest_backup_archive.py >"$staging_dir/artifacts.tar.gz"
tar --list --gzip --file "$staging_dir/artifacts.tar.gz" \
  >"$staging_dir/artifacts.list"
[[ -s "$staging_dir/artifacts.list" ]] || die "artifact archive listing is empty"

if [[ "$censorwatch_mode" == included ]]; then
  log "fencing every CensorWatch Redis/PostgreSQL writer"
  for writer in "${censorwatch_writers[@]}"; do
    writer_container="$(running_compose_container "$writer")" || \
      die "cannot inspect CensorWatch writer: $writer"
    if [[ -n "$writer_container" ]]; then
      censorwatch_running_writers+=("$writer")
      censorwatch_running_writer_containers+=("$writer=$writer_container")
    fi
  done
  censorwatch_restart_needed=1
  for writer in "${censorwatch_writers[@]}"; do
    if [[ " ${censorwatch_running_writers[*]} " == *" $writer "* ]]; then
      "${censorwatch_compose[@]}" stop --timeout 180 "$writer"
    fi
  done
  for writer in "${censorwatch_writers[@]}"; do
    writer_container="$(running_compose_container "$writer")" || \
      die "cannot verify the CensorWatch writer fence: $writer"
    [[ -z "$writer_container" ]] || \
      die "CensorWatch writer remained active after the fence: $writer"
  done
  for writer_specification in "${censorwatch_running_writer_containers[@]}"; do
    IFS='=' read -r writer writer_container <<<"$writer_specification"
    require_cleanly_stopped_container "$writer" "$writer_container"
  done
  migrate_container="$(running_compose_container migrate-censorwatch)" || \
    die "cannot verify the CensorWatch migration fence"
  [[ -z "$migrate_container" ]] || \
    die "CensorWatch migration started during the backup fence"

  log "dumping isolated CensorWatch PostgreSQL behind the writer fence"
  # shellcheck disable=SC2016
  "${censorwatch_compose[@]}" exec -T postgres-censorwatch sh -eu -c \
    'exec pg_dump --format=custom --no-owner --no-privileges --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
    >"$staging_dir/censorwatch-postgres.dump"
  [[ -s "$staging_dir/censorwatch-postgres.dump" ]] || \
    die "CensorWatch pg_dump produced an empty archive"
  "${censorwatch_compose[@]}" exec -T postgres-censorwatch pg_restore --list \
    <"$staging_dir/censorwatch-postgres.dump" \
    >"$staging_dir/censorwatch-postgres.list"
  [[ -s "$staging_dir/censorwatch-postgres.list" ]] || \
    die "CensorWatch pg_restore produced an empty listing"

  log "stopping isolated data Redis before copying its persistence volume"
  censorwatch_data_redis_stopped=1
  "${censorwatch_compose[@]}" stop --timeout 60 redis-censorwatch-data
  redis_running_after_stop="$(running_compose_container redis-censorwatch-data)" || \
    die "cannot verify the CensorWatch data Redis cold stop"
  [[ -z "$redis_running_after_stop" ]] || \
    die "CensorWatch data Redis remained active after its cold-stop request"
  require_cleanly_stopped_container \
    redis-censorwatch-data "$censorwatch_data_redis_container"

  # Capture the complete stopped durable data-plane /data volume. The control
  # plane is deliberately ephemeral and excluded so a restore cannot replay an
  # old heartbeat. ACL and health-password secrets live under /run/secrets and
  # cannot enter this archive. The exact already-present Redis image is used
  # only as a bounded tar runtime; it receives no environment or network.
  docker run --rm --pull never --network none --read-only --log-driver none \
    --security-opt no-new-privileges:true --cap-drop ALL \
    --cap-add DAC_READ_SEARCH --user 0:0 --pids-limit 32 \
    --memory 256m --memory-swap 256m --cpus 0.5 \
    --mount "type=volume,src=$censorwatch_data_redis_volume,dst=/source/redis,readonly" \
    --entrypoint /bin/sh "$censorwatch_data_redis_image" -eu -c \
    'cd /source && exec tar -czf - redis' \
    >"$staging_dir/censorwatch-redis.tar.gz"
  [[ -s "$staging_dir/censorwatch-redis.tar.gz" ]] || \
    die "CensorWatch cold Redis archive is empty"
  tar --list --gzip --file "$staging_dir/censorwatch-redis.tar.gz" \
    >"$staging_dir/censorwatch-redis.list"
  [[ -s "$staging_dir/censorwatch-redis.list" ]] || \
    die "CensorWatch cold Redis archive listing is empty"

  restart_censorwatch_after_snapshot || \
    die "CensorWatch runtime could not be restored after the cold snapshot"
fi

postgres_version="$(
  # shellcheck disable=SC2016
  "${compose[@]}" exec -T postgres sh -eu -c \
    'exec psql --no-psqlrc --tuples-only --no-align --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --command="show server_version;"' \
    2>/dev/null || printf 'unknown'
)"
{
  printf 'format_version=5\n'
  printf 'snapshot_id=%s\n' "$snapshot_id"
  printf 'created_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'host=%s\n' "$(hostname)"
  printf 'compose_project=%s\n' "$compose_project"
  printf 'postgres_version=%s\n' "$postgres_version"
  printf 'artifact_roots=readings,data,newswire,analysis,witness\n'
  printf 'censorwatch_mode=%s\n' "$censorwatch_mode"
  printf 'censorwatch_postgres_version=%s\n' "$censorwatch_postgres_version"
  printf 'censorwatch_redis_version=%s\n' "$censorwatch_redis_version"
  printf 'censorwatch_writer_fence=%s\n' "$censorwatch_writer_fence"
  if [[ "$censorwatch_mode" == included ]]; then
    printf '%s\n' \
      'contents=postgres.dump,postgres.list,artifacts.tar.gz,artifacts.list,censorwatch-postgres.dump,censorwatch-postgres.list,censorwatch-redis.tar.gz,censorwatch-redis.list'
  else
    printf 'contents=postgres.dump,postgres.list,artifacts.tar.gz,artifacts.list\n'
  fi
} >"$staging_dir/MANIFEST.txt"

checksum_files=(
  postgres.dump postgres.list artifacts.tar.gz artifacts.list
)
if [[ "$censorwatch_mode" == included ]]; then
  checksum_files+=(
    censorwatch-postgres.dump censorwatch-postgres.list
    censorwatch-redis.tar.gz censorwatch-redis.list
  )
fi
checksum_files+=(MANIFEST.txt)
(
  cd "$staging_dir"
  sha256sum "${checksum_files[@]}" >SHA256SUMS
  sha256sum --check SHA256SUMS >/dev/null
)

staging_identity="$(
  python3 - "$staging_dir" <<'PY'
import os
import sys

metadata = os.stat(sys.argv[1], follow_symlinks=False)
print(f"{metadata.st_uid}:{metadata.st_gid}")
PY
)"
[[ "$staging_identity" =~ ^[0-9]+:[0-9]+$ ]] || \
  die "staging ownership identity is malformed"
python3 "$repo_root/ops/backup/node_backup_snapshot.py" verify "$staging_dir" \
  --snapshot-id "$snapshot_id" \
  --expected-uid "${staging_identity%%:*}" \
  --expected-gid "${staging_identity#*:}" \
  >/dev/null

# A successful checksum is not yet a durable rollback point. Flush every exact
# snapshot file and then the containing directory before publishing its name.
python3 - "$staging_dir" "$censorwatch_mode" <<'PY'
import os
import stat
import sys

root = sys.argv[1]
mode = sys.argv[2]
names = [
    "MANIFEST.txt",
    "SHA256SUMS",
    "artifacts.list",
    "artifacts.tar.gz",
    "postgres.dump",
    "postgres.list",
]
if mode == "included":
    names.extend(
        (
            "censorwatch-postgres.dump",
            "censorwatch-postgres.list",
            "censorwatch-redis.list",
            "censorwatch-redis.tar.gz",
        )
    )
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
file_flags = os.O_RDONLY | os.O_NOFOLLOW
directory = os.open(root, directory_flags)
try:
    for name in names:
        descriptor = os.open(name, file_flags, dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise SystemExit("backup durability preflight rejected a file")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    os.fsync(directory)
finally:
    os.close(directory)
PY

# Only a completely validated directory gets the stable timestamp name.
mv -- "$staging_dir" "$final_dir"
staging_dir=""
rmdir -- "$staging_root"
staging_root=""
# Persist the rename itself before reporting the rollback point as published.
python3 - "$backup_root" <<'PY'
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
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
