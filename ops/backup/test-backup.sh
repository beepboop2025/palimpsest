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
backup_root="$fixture_root/backups"
failed_root="$fixture_root/failed-backups"
offsite_root="$fixture_root/offsite"
fake_bin="$fixture_root/bin"
mkdir -p "$repo/ops/docker" "$repo/readings" "$repo/data/raw" \
  "$backup_root" "$failed_root" "$offsite_root" "$fake_bin"

printf 'services: {}\n' >"$repo/ops/docker/docker-compose.prod.yml"
printf 'POSTGRES_USER=palimpsest\nPOSTGRES_DB=palimpsest\n' \
  >"$repo/ops/docker/.env"
printf '{"status":"ok"}\n' >"$repo/readings/probe.json"
printf 'immutable raw sample\n' >"$repo/data/raw/sample.txt"

# These single-quoted strings are source code for the fake executable; their
# variables must expand when that executable runs, not while this test writes it.
# shellcheck disable=SC2016
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'joined=" $* "' \
  'if [[ "$joined" == *" pg_dump "* ]]; then' \
  '  printf "fake-custom-dump\n"' \
  'elif [[ "$joined" == *" pg_restore --list "* ]]; then' \
  '  payload="$(command cat)"' \
  '  [[ "$payload" == "fake-custom-dump" ]] || exit 41' \
  '  [[ "${FAKE_PG_RESTORE_FAIL:-0}" != 1 ]] || exit 42' \
  '  printf "; fake archive listing\n"' \
  'elif [[ "$joined" == *" psql "* ]]; then' \
  '  printf "16.test\n"' \
  'else' \
  '  printf "unexpected fake docker invocation: %s\n" "$*" >&2' \
  '  exit 43' \
  'fi' \
  >"$fake_bin/docker"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' >"$fake_bin/flock"
chmod 0755 "$fake_bin/docker" "$fake_bin/flock"

common_env=(
  "PATH=$fake_bin:$PATH"
  "PALIMPSEST_ROOT=$repo"
  "PALIMPSEST_BACKUP_RETENTION_DAYS=14"
  "PALIMPSEST_BACKUP_MIN_FREE_MB=64"
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

printf 'PASS: backup publication, off-host verification, and failure cleanup\n'
