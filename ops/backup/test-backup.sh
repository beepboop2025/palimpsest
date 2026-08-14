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
analysis_root="$fixture_root/analysis"
# Production keeps the evidence wire under the broader state root while the
# archive mounts only the disjoint readings/ and data/ subtrees from that root.
newswire_root="$state_root/newswire"
backup_root="$fixture_root/backups"
failed_root="$fixture_root/failed-backups"
fake_bin="$fixture_root/bin"
fake_container="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
fake_image="sha256:abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd"
mkdir -p "$repo/ops/docker" "$state_root/readings" "$state_root/data/raw" \
  "$state_root/data/evidence-documents" \
  "$analysis_root/private" \
  "$analysis_root/delivery" \
  "$analysis_root/runs/run-20260813T010203Z-0123456789ab/private" \
  "$newswire_root" \
  "$backup_root" "$failed_root" "$fake_bin"
# Match the canonical path the backup resolves before it constructs Docker
# bind mounts (TMPDIR may carry a harmless trailing slash on some runners).
state_root="$(cd "$state_root" && pwd -P)"
analysis_root="$(cd "$analysis_root" && pwd -P)"
newswire_root="$(cd "$newswire_root" && pwd -P)"

printf 'services: {}\n' >"$repo/ops/docker/docker-compose.prod.yml"
printf 'POSTGRES_USER=palimpsest\nPOSTGRES_DB=palimpsest\n' \
  >"$repo/ops/docker/.env"
printf '{"status":"ok"}\n' >"$state_root/readings/probe.json"
printf 'immutable raw sample\n' >"$state_root/data/raw/sample.txt"
printf 'private evidence sample\n' \
  >"$state_root/data/evidence-documents/private.json"
chmod 0600 "$state_root/data/evidence-documents/private.json"
private_state_payload='private-analysis-state-do-not-log-7f839a'
immutable_run_payload='immutable-analysis-run-do-not-log-a25c19'
delivery_payload='delivery-safe-wire-projection-82fb0d'
printf '%s\n' "$private_state_payload" >"$analysis_root/private/state.json"
printf '%s\n' "$immutable_run_payload" \
  >"$analysis_root/runs/run-20260813T010203Z-0123456789ab/private/analytical-packets-latest.json"
printf 'analysis-lock-fixture\n' >"$analysis_root/private/cascade.lock"
printf '%s\n' "$delivery_payload" \
  >"$analysis_root/delivery/wire-claim-audits-latest.json"
chmod 0600 "$analysis_root/private/cascade.lock" \
  "$analysis_root/private/state.json" \
  "$analysis_root/runs/run-20260813T010203Z-0123456789ab/private/analytical-packets-latest.json"
chmod 0711 "$analysis_root/delivery"
chmod 0644 "$analysis_root/delivery/wire-claim-audits-latest.json"
printf '{"generated_at":"2026-08-13T01:02:03Z"}\n' \
  >"$newswire_root/newswire-latest.json"
printf '{"event_id":"fixture"}\n' >"$newswire_root/newswire-versions.jsonl"
printf '{"status":"success"}\n' >"$newswire_root/newswire-status.json"
printf 'newswire-lock-fixture\n' >"$newswire_root/newswire.lock"
chmod 0600 "$newswire_root/newswire.lock"

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
  '  [[ "$joined" == *" --read-only "* ]] || exit 50' \
  '  [[ "$joined" == *" --cap-drop ALL "* ]] || exit 51' \
  '  [[ "$joined" == *" --cap-add DAC_READ_SEARCH "* ]] || exit 46' \
  '  [[ "$joined" == *" --user 0:0 "* ]] || exit 52' \
  '  [[ "$joined" == *"src=$FAKE_STATE_ROOT/readings,dst=/source/readings,readonly"* ]] || exit 53' \
  '  [[ "$joined" == *"src=$FAKE_STATE_ROOT/data,dst=/source/data,readonly"* ]] || exit 54' \
  '  [[ "$joined" == *"src=$FAKE_ANALYSIS_ROOT,dst=/source/analysis,readonly"* ]] || exit 55' \
  '  [[ "$joined" == *"src=$FAKE_NEWSWIRE_ROOT,dst=/source/newswire,readonly"* ]] || exit 56' \
  '  [[ "$joined" == *" --entrypoint /usr/local/bin/python3 $FAKE_IMAGE_ID -I -B /app/scripts/palimpsest_backup_archive.py "* ]] || exit 47' \
  '  archive_fixture="$(mktemp -d)"' \
  '  trap '\''rm -rf -- "$archive_fixture"'\'' EXIT' \
  '  cp -a "$FAKE_STATE_ROOT/readings" "$archive_fixture/readings"' \
  '  cp -a "$FAKE_STATE_ROOT/data" "$archive_fixture/data"' \
  '  cp -a "$FAKE_ANALYSIS_ROOT" "$archive_fixture/analysis"' \
  '  cp -a "$FAKE_NEWSWIRE_ROOT" "$archive_fixture/newswire"' \
  '  tar --create --gzip --file - --directory "$archive_fixture" analysis readings data newswire' \
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
  "PALIMPSEST_ANALYSIS_ROOT=$analysis_root"
  "PALIMPSEST_NEWSWIRE_ROOT=$newswire_root"
  "PALIMPSEST_BACKUP_RETENTION_DAYS=14"
  "PALIMPSEST_BACKUP_MIN_FREE_MB=64"
  "FAKE_CONTAINER_ID=$fake_container"
  "FAKE_IMAGE_ID=$fake_image"
  "FAKE_STATE_ROOT=$state_root"
  "FAKE_ANALYSIS_ROOT=$analysis_root"
  "FAKE_NEWSWIRE_ROOT=$newswire_root"
)

env "${common_env[@]}" \
  PALIMPSEST_BACKUP_DIR="$backup_root" \
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
tar --list --gzip --file "$snapshot/artifacts.tar.gz" | \
  grep -q '^data/evidence-documents/private.json$' || \
  fail "private evidence artifact is missing"
private_payload="$(
  tar --extract --gzip --to-stdout --file "$snapshot/artifacts.tar.gz" \
    data/evidence-documents/private.json
)"
[[ "$private_payload" == "private evidence sample" ]] || \
  fail "private evidence artifact is unreadable"
for analysis_member in \
  analysis/delivery/wire-claim-audits-latest.json \
  analysis/private/cascade.lock \
  analysis/private/state.json \
  analysis/runs/run-20260813T010203Z-0123456789ab/private/analytical-packets-latest.json; do
  tar --list --gzip --file "$snapshot/artifacts.tar.gz" | \
    grep -q "^${analysis_member}$" || \
    fail "private analysis artifact is missing: $analysis_member"
done
for newswire_member in \
  newswire/newswire-latest.json \
  newswire/newswire-versions.jsonl \
  newswire/newswire-status.json \
  newswire/newswire.lock; do
  tar --list --gzip --file "$snapshot/artifacts.tar.gz" | \
    grep -q "^${newswire_member}$" || \
    fail "evidence-wire recovery artifact is missing: $newswire_member"
done
[[ "$(
  tar --extract --gzip --to-stdout --file "$snapshot/artifacts.tar.gz" \
    analysis/delivery/wire-claim-audits-latest.json
)" == "$delivery_payload" ]] || fail "analysis delivery projection is unreadable"
[[ "$(
  tar --extract --gzip --to-stdout --file "$snapshot/artifacts.tar.gz" \
    analysis/private/state.json
)" == "$private_state_payload" ]] || fail "private analysis state is unreadable"
[[ "$(
  tar --extract --gzip --to-stdout --file "$snapshot/artifacts.tar.gz" \
    analysis/runs/run-20260813T010203Z-0123456789ab/private/analytical-packets-latest.json
)" == "$immutable_run_payload" ]] || fail "immutable analysis run is unreadable"

restore_check="$fixture_root/restore-check"
mkdir -m 0700 "$restore_check"
tar --extract --gzip --file "$snapshot/artifacts.tar.gz" --directory "$restore_check"
find "$restore_check/analysis/private/state.json" -prune -type f -perm 0600 \
  -print -quit | grep -q . || fail "private analysis state mode was not preserved"
find "$restore_check/analysis/private/cascade.lock" -prune -type f -perm 0600 \
  -print -quit | grep -q . || fail "analysis cascade lock mode was not preserved"
find "$restore_check/analysis/delivery" -prune -type d -perm 0711 \
  -print -quit | grep -q . || fail "analysis delivery mode was not preserved"
find "$restore_check/analysis/delivery/wire-claim-audits-latest.json" \
  -prune -type f -perm 0644 -print -quit | grep -q . || \
  fail "analysis delivery artifact mode was not preserved"
find "$restore_check/analysis/runs/run-20260813T010203Z-0123456789ab/private/analytical-packets-latest.json" \
  -prune -type f -perm 0600 -print -quit | grep -q . || \
  fail "immutable analysis run mode was not preserved"
grep -Fq 'format_version=3' "$snapshot/MANIFEST.txt" || \
  fail "backup manifest format was not upgraded"
grep -Fq 'artifact_roots=readings,data,newswire,analysis' "$snapshot/MANIFEST.txt" || \
  fail "backup manifest omits an artifact restore root"
if grep -Fq "$private_state_payload" \
  "$snapshot/MANIFEST.txt" "$snapshot/artifacts.list"; then
  fail "private analysis payload leaked into backup metadata"
fi
if grep -Fq "$delivery_payload" \
  "$snapshot/MANIFEST.txt" "$snapshot/artifacts.list"; then
  fail "analysis delivery payload leaked into backup metadata"
fi

for retired_setting in \
  PALIMPSEST_BACKUP_COPY_DIR PALIMPSEST_BACKUP_HOOK \
  PALIMPSEST_BACKUP_OFFSITE_ENCRYPTED; do
  retired_root="$fixture_root/retired-${retired_setting}"
  mkdir -p "$retired_root"
  retired_value=1
  [[ "$retired_setting" == PALIMPSEST_BACKUP_COPY_DIR ]] && \
    retired_value="$fixture_root/retired-copy-destination"
  [[ "$retired_setting" == PALIMPSEST_BACKUP_HOOK ]] && \
    retired_value="$fixture_root/retired-hook"
  if env "${common_env[@]}" \
    PALIMPSEST_BACKUP_DIR="$retired_root" \
    "$retired_setting=$retired_value" \
    "$backup_script"; then
    fail "retired generic offsite setting unexpectedly ran: $retired_setting"
  fi
  [[ -z "$(find "$retired_root" -mindepth 1 -maxdepth 1 -type d -print -quit)" ]] || \
    fail "retired offsite refusal left backup output: $retired_setting"
done

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

wrong_analysis_mount_root="$fixture_root/wrong-analysis-mount-backups"
mkdir -p "$wrong_analysis_mount_root"
if env "${common_env[@]}" \
  PALIMPSEST_BACKUP_DIR="$wrong_analysis_mount_root" \
  FAKE_ANALYSIS_ROOT="$fixture_root/unexpected-analysis-source" \
  "$backup_script"; then
  fail "mismatched analysis bind mount unexpectedly published"
fi
[[ -z "$(find "$wrong_analysis_mount_root" -mindepth 1 -maxdepth 1 -type d -print -quit)" ]] || \
  fail "analysis mount mismatch left a published or incomplete directory"

missing_analysis_root="$fixture_root/missing-analysis-backups"
mkdir -p "$missing_analysis_root"
if env "${common_env[@]}" \
  PALIMPSEST_ANALYSIS_ROOT="$fixture_root/does-not-exist" \
  PALIMPSEST_BACKUP_DIR="$missing_analysis_root" \
  "$backup_script"; then
  fail "missing analysis root unexpectedly published"
fi
[[ -z "$(find "$missing_analysis_root" -mindepth 1 -maxdepth 1 -type d -print -quit)" ]] || \
  fail "missing analysis root left a published or incomplete directory"

symlink_state_root="$fixture_root/symlink-state"
symlink_data_target="$fixture_root/symlink-data-target"
symlink_backup_root="$fixture_root/symlink-backups"
symlink_copy_root="$symlink_data_target/offsite-copy"
mkdir -p "$symlink_state_root/readings" "$symlink_data_target" \
  "$symlink_backup_root" "$symlink_copy_root"
ln -s "$symlink_data_target" "$symlink_state_root/data"
if env "${common_env[@]}" \
  PALIMPSEST_STATE_ROOT="$symlink_state_root" \
  PALIMPSEST_BACKUP_DIR="$symlink_backup_root" \
  PALIMPSEST_BACKUP_COPY_DIR="$symlink_copy_root" \
  PALIMPSEST_BACKUP_OFFSITE_ENCRYPTED=1 \
  "$backup_script"; then
  fail "symlinked data root unexpectedly bypassed containment checks"
fi
[[ -z "$(find "$symlink_backup_root" -mindepth 1 -maxdepth 1 -type d -print -quit)" ]] || \
  fail "symlinked data refusal left a published or incomplete directory"

checkout_copy_backup_root="$fixture_root/checkout-copy-backups"
checkout_copy_root="$repo/private-backups"
mkdir -p "$checkout_copy_backup_root" "$checkout_copy_root"
if env "${common_env[@]}" \
  PALIMPSEST_BACKUP_DIR="$checkout_copy_backup_root" \
  PALIMPSEST_BACKUP_COPY_DIR="$checkout_copy_root" \
  PALIMPSEST_BACKUP_OFFSITE_ENCRYPTED=1 \
  "$backup_script"; then
  fail "copy directory inside the checkout unexpectedly ran"
fi
[[ -z "$(find "$checkout_copy_backup_root" -mindepth 1 -maxdepth 1 -type d -print -quit)" ]] || \
  fail "checkout copy refusal left a published or incomplete directory"

printf 'PASS: local backup publication, newswire/analysis coverage, mount binding, retired offsite rejection, and failure cleanup\n'
