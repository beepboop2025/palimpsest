#!/usr/bin/env bash
# Dependency-light contract test for the backup's success and failure paths.

set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
backup_script="$here/palimpsest-backup.sh"
fixture_root="$(mktemp -d "${TMPDIR:-/tmp}/palimpsest-backup-test.XXXXXX")"

cleanup() {
  rm -rf -- "$fixture_root"
}
trap cleanup EXIT INT TERM

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

repo="$fixture_root/repo"
state_root="$fixture_root/state"
backup_root="$fixture_root/backups"
failed_root="$fixture_root/failed-backups"
offsite_root="$fixture_root/offsite"
fake_bin="$fixture_root/bin"
fake_container="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
fake_image="sha256:abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd"
mkdir -p "$repo/ops/docker" "$state_root/readings" "$state_root/data/raw" \
  "$backup_root" "$failed_root" "$offsite_root" "$fake_bin"

printf 'services: {}\n' >"$repo/ops/docker/docker-compose.prod.yml"
printf 'POSTGRES_USER=palimpsest\nPOSTGRES_DB=palimpsest\n' \
  >"$repo/ops/docker/.env"
printf '{"status":"ok"}\n' >"$state_root/readings/probe.json"
printf 'immutable raw sample\n' >"$state_root/data/raw/sample.txt"

# These single-quoted strings are source code for the fake executable; their
# variables must expand when that executable runs, not while this test writes it.
# shellcheck disable=SC2016
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'joined=" $* "' \
  'if [[ "$joined" == *" ps -q worker "* ]]; then' \
  '  printf "%s\n" "$FAKE_CONTAINER_ID"' \
  'elif [[ "$joined" == *" pg_dump "* ]]; then' \
  '  printf "fake-custom-dump\n"' \
  'elif [[ "$joined" == *" pg_restore --list "* ]]; then' \
  '  payload="$(command cat)"' \
  '  [[ "$payload" == "fake-custom-dump" ]] || exit 41' \
  '  [[ "${FAKE_PG_RESTORE_FAIL:-0}" != 1 ]] || exit 42' \
  '  printf "; fake archive listing\n"' \
  'elif [[ "$joined" == *" psql "* ]]; then' \
  '  printf "16.test\n"' \
  'elif [[ "$joined" == *" run --rm --pull never --network none "* ]]; then' \
  '  [[ "$joined" == *" --log-driver none "* ]] || exit 49' \
  '  [[ "$joined" == *" --cap-add DAC_READ_SEARCH "* ]] || exit 46' \
  '  [[ "$joined" == *" $FAKE_IMAGE_ID --create "* ]] || exit 47' \
  '  [[ "$joined" == *" --numeric-owner "* ]] || exit 48' \
  '  exec tar --create --gzip --file - --directory "$FAKE_STATE_ROOT" -- readings data' \
  'else' \
  '  printf "unexpected fake docker invocation: %s\n" "$*" >&2' \
  '  exit 43' \
  'fi' \
  >"$fake_bin/docker"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  '[[ "$1" == inspect && "$2" == --format ]] || exit 44' \
  '[[ "${4:-}" == "$FAKE_CONTAINER_ID" ]] || exit 45' \
  'if [[ "$3" == "{{.Image}}" ]]; then' \
  '  printf "%s\n" "$FAKE_IMAGE_ID"' \
  'else' \
  '  printf "/app/readings\t%s\tbind\n" "$FAKE_STATE_ROOT/readings"' \
  '  printf "/app/data\t%s\tbind\n" "${FAKE_DATA_SOURCE:-$FAKE_STATE_ROOT/data}"' \
  'fi' \
  >"$fake_bin/docker-inspect"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' >"$fake_bin/flock"
chmod 0755 "$fake_bin/docker" "$fake_bin/docker-inspect" "$fake_bin/flock"

# The backup calls both `docker compose` and `docker inspect`. Keep one fake
# entry point while dispatching the latter to its focused fixture.
mv "$fake_bin/docker" "$fake_bin/docker-compose-fake"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'if [[ "${1:-}" == inspect ]]; then' \
  '  exec "$(dirname "$0")/docker-inspect" "$@"' \
  'fi' \
  'exec "$(dirname "$0")/docker-compose-fake" "$@"' \
  >"$fake_bin/docker"
chmod 0755 "$fake_bin/docker"

common_env=(
  "PATH=$fake_bin:$PATH"
  "PALIMPSEST_ROOT=$repo"
  "PALIMPSEST_STATE_ROOT=$state_root"
  "PALIMPSEST_BACKUP_RETENTION_DAYS=14"
  "PALIMPSEST_BACKUP_MIN_FREE_MB=64"
  "FAKE_CONTAINER_ID=$fake_container"
  "FAKE_IMAGE_ID=$fake_image"
  "FAKE_STATE_ROOT=$state_root"
)

env "${common_env[@]}" \
  PALIMPSEST_BACKUP_DIR="$backup_root" \
  PALIMPSEST_BACKUP_COPY_DIR="$offsite_root" \
  "$backup_script"

snapshot="$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -print | head -1)"
[[ -n "$snapshot" ]] || fail "successful run did not publish a snapshot"
[[ -z "$(find "$backup_root" -mindepth 1 -maxdepth 1 -name '.incomplete-*' -print -quit)" ]] || \
  fail "successful run left staging data"
(cd "$snapshot" && sha256sum --check SHA256SUMS >/dev/null)
tar --list --gzip --file "$snapshot/artifacts.tar.gz" | \
  grep -q '^readings/probe.json$' || fail "readings artifact is missing"
tar --list --gzip --file "$snapshot/artifacts.tar.gz" | \
  grep -q '^data/raw/sample.txt$' || fail "data artifact is missing"

snapshot_id="$(basename "$snapshot")"
[[ -d "$offsite_root/$snapshot_id" ]] || fail "off-host copy is missing"
(cd "$offsite_root/$snapshot_id" && sha256sum --check SHA256SUMS >/dev/null)

if env "${common_env[@]}" \
  PALIMPSEST_BACKUP_DIR="$failed_root" \
  FAKE_PG_RESTORE_FAIL=1 \
  "$backup_script"; then
  fail "invalid pg_restore listing unexpectedly published"
fi
[[ -z "$(find "$failed_root" -mindepth 1 -maxdepth 1 -type d -print -quit)" ]] || \
  fail "failed validation left a published or incomplete directory"

wrong_mount_root="$fixture_root/wrong-mount-backups"
mkdir -p "$wrong_mount_root" "$fixture_root/wrong-data"
if env "${common_env[@]}" \
  PALIMPSEST_BACKUP_DIR="$wrong_mount_root" \
  FAKE_DATA_SOURCE="$fixture_root/wrong-data" \
  "$backup_script"; then
  fail "mismatched data bind mount unexpectedly published"
fi
[[ -z "$(find "$wrong_mount_root" -mindepth 1 -maxdepth 1 -type d -print -quit)" ]] || \
  fail "mount mismatch left a published or incomplete directory"

printf 'PASS: backup publication, mount binding, off-host verification, and failure cleanup\n'
