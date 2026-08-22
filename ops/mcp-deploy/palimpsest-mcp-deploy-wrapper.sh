#!/usr/bin/env bash
# Root-owned forced-command controller for the Palimpsest MCP production service.
# The deploy key must be restricted to this file in authorized_keys.  No caller-
# supplied path, branch, URL, shell fragment, or service name is accepted.
set -Eeuo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

# Keep caller-controlled SSH environment variables (including proxy and Git
# configuration knobs) out of the privileged transaction. Re-enter only the
# root-owned installed controller with the one datum the protocol accepts.
if [[ "$#" != "1" || "${1:-}" != "--clean-environment" ]]; then
  exec /usr/bin/env -i \
    PATH="$PATH" \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_PAGER=cat \
    GIT_TERMINAL_PROMPT=0 \
    SSH_ORIGINAL_COMMAND="${SSH_ORIGINAL_COMMAND:-}" \
    /usr/bin/bash /usr/local/libexec/palimpsest-mcp-deploy --clean-environment
fi
shift

readonly EXPECTED_REMOTE="https://github.com/beepboop2025/palimpsest.git"
readonly EXPECTED_AUTHOR_EMAIL="mrinallovesbhature@gmail.com"
readonly STATE_DIR="/var/lib/palimpsest-mcp-deploy"
readonly REPOSITORY="${STATE_DIR}/repository.git"
readonly BACKUP_DIR="${STATE_DIR}/backups"
readonly RECEIPT_DIR="${STATE_DIR}/receipts"
readonly DEPLOYED_SHA_FILE="${STATE_DIR}/deployed-sha"
readonly LOCK_FILE="${STATE_DIR}/deploy.lock"
readonly TARGET_DIR="/opt/palimpsest-mcp"
readonly TARGET_FILE="${TARGET_DIR}/palimpsest_mcp.py"
readonly SERVICE="palimpsest-mcp.service"
readonly CONTROLLER="/usr/local/libexec/palimpsest-mcp-deploy"
readonly VERIFY_RELEASE="/usr/local/libexec/palimpsest-mcp-verify-release.py"
readonly SMOKE="/usr/local/libexec/palimpsest-mcp-smoke.py"
readonly UNIT_FILE="/etc/systemd/system/palimpsest-mcp.service"
readonly LOCAL_ENDPOINT="http://127.0.0.1:8793/"
readonly RUNTIME_USER="palimpsest-mcp"
readonly VERIFY_USER="palimpsest-mcp-verify"
readonly EXPECTED_VERIFY_SHA256="1d84e5d78e83185b54343cda4104f6e665de69159ea52c34d7060287ab7dc3b7"
readonly EXPECTED_SMOKE_SHA256="1e3f1c4eb6d5b8a4960aa1f55dd3a74f6df277f93fc17a42db5a0ee2ec8846f1"
readonly EXPECTED_UNIT_SHA256="9891f7e321b718b841eba7ea3d1b0377f2e59f09a133c579688e4fd59554d4c2"

stage_dir=""
api_json=""
candidate_tmp=""
marker_tmp=""
receipt_tmp=""
backup_file=""
previous_marker=""
previous_receipt=""
receipt_path=""
promoted=0
committed=0

fail() {
  printf 'palimpsest MCP deploy refused: %s\n' "$*" >&2
  exit 1
}

require_regular_root_file() {
  local path=$1
  [[ -f "$path" && ! -L "$path" ]] || fail "not a regular file: $path"
  [[ "$(stat -c '%u' "$path")" = "0" ]] || fail "not root-owned: $path"
  [[ "$(stat -c '%h' "$path")" = "1" ]] || fail "hard-linked file: $path"
  local mode
  mode=$(stat -c '%a' "$path")
  (( (8#$mode & 0022) == 0 )) || fail "group/other-writable file: $path"
}

require_root_directory() {
  local path=$1
  [[ -d "$path" && ! -L "$path" ]] || fail "not a real directory: $path"
  [[ "$(stat -c '%u' "$path")" = "0" ]] || fail "not root-owned: $path"
  local mode
  mode=$(stat -c '%a' "$path")
  (( (8#$mode & 0022) == 0 )) || fail "group/other-writable directory: $path"
}

restore_file_if_present() {
  local backup=$1
  local destination=$2
  if [[ -n "$backup" && -f "$backup" && ! -L "$backup" ]]; then
    local restore_tmp
    restore_tmp=$(mktemp "${destination}.rollback.XXXXXX")
    install -o root -g root -m 0644 -- "$backup" "$restore_tmp"
    mv -fT -- "$restore_tmp" "$destination"
  fi
}

restore_state_file() {
  local saved=$1
  local destination=$2
  if [[ -n "$saved" && -f "$saved" && ! -L "$saved" ]]; then
    install -o root -g root -m 0600 -- "$saved" "$destination"
  else
    rm -f -- "$destination"
  fi
}

rollback() {
  local reason=$1
  printf 'candidate failed after promotion (%s); restoring the previous runtime\n' \
    "$reason" >&2
  restore_file_if_present "$backup_file" "$TARGET_FILE"
  restore_state_file "$previous_marker" "$DEPLOYED_SHA_FILE"
  if [[ -n "$receipt_path" ]]; then
    restore_state_file "$previous_receipt" "$receipt_path"
  fi
  if [[ -n "$receipt_tmp" ]]; then
    rm -f -- "$receipt_tmp"
  fi
  systemctl restart "$SERVICE" || true
  if ! systemctl is-active --quiet "$SERVICE"; then
    printf 'rollback restored bytes but the service is not active\n' >&2
  fi
  promoted=0
}

run_as_verify_user() {
  local rc=0
  pkill -KILL -u "$VERIFY_USER" >/dev/null 2>&1 || true
  timeout --kill-after=5s 90s \
    runuser --user "$VERIFY_USER" -- \
      env -i PATH="$PATH" "$@" || rc=$?
  pkill -KILL -u "$VERIFY_USER" >/dev/null 2>&1 || true
  return "$rc"
}

cleanup() {
  local rc=$?
  trap - EXIT
  if [[ "$promoted" = "1" && "$committed" != "1" ]]; then
    rollback "controller exit ${rc}"
  fi
  [[ -z "$stage_dir" ]] || rm -rf -- "$stage_dir"
  [[ -z "$api_json" ]] || rm -f -- "$api_json"
  [[ -z "$candidate_tmp" ]] || rm -f -- "$candidate_tmp"
  [[ -z "$marker_tmp" ]] || rm -f -- "$marker_tmp"
  [[ -z "$receipt_tmp" ]] || rm -f -- "$receipt_tmp"
  [[ -z "$previous_marker" ]] || rm -f -- "$previous_marker"
  [[ -z "$previous_receipt" ]] || rm -f -- "$previous_receipt"
  exit "$rc"
}
trap cleanup EXIT

[[ "$(id -u)" = "0" ]] || fail "controller must run as root"

original_command=${SSH_ORIGINAL_COMMAND:-}
if [[ ! "$original_command" =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
  fail "command must be exactly: deploy <40-lowercase-hex-SHA>"
fi
target_sha=${BASH_REMATCH[1]}

require_root_directory "$STATE_DIR"
require_root_directory "$BACKUP_DIR"
require_root_directory "$RECEIPT_DIR"
require_root_directory "$TARGET_DIR"
require_root_directory "$REPOSITORY"
require_regular_root_file "$TARGET_FILE"
require_regular_root_file "$CONTROLLER"
require_regular_root_file "$VERIFY_RELEASE"
require_regular_root_file "$SMOKE"
require_regular_root_file "$UNIT_FILE"
[[ "$(sha256sum "$VERIFY_RELEASE" | awk '{print $1}')" = \
  "$EXPECTED_VERIFY_SHA256" ]] || fail "installed release verifier drifted"
[[ "$(sha256sum "$SMOKE" | awk '{print $1}')" = \
  "$EXPECTED_SMOKE_SHA256" ]] || fail "installed live smoke drifted"
[[ "$(sha256sum "$UNIT_FILE" | awk '{print $1}')" = \
  "$EXPECTED_UNIT_SHA256" ]] || fail "installed systemd unit drifted"
id -u "$RUNTIME_USER" >/dev/null 2>&1 || fail "runtime account does not exist"
id -u "$VERIFY_USER" >/dev/null 2>&1 || fail "verification account does not exist"
[[ ! -L "$LOCK_FILE" ]] || fail "lock path is a symlink"
[[ ! -L "$DEPLOYED_SHA_FILE" ]] || fail "deployed SHA path is a symlink"

exec 9>"$LOCK_FILE"
flock -n 9 || fail "another release is already in progress"

repository_remote=$(git --git-dir="$REPOSITORY" remote get-url origin)
[[ "$repository_remote" = "$EXPECTED_REMOTE" ]] || fail "repository remote is not pinned"
git --git-dir="$REPOSITORY" config transfer.fsckObjects true
git --git-dir="$REPOSITORY" config fetch.fsckObjects true
timeout --kill-after=5s 120s \
  git --git-dir="$REPOSITORY" fetch --force --no-tags --prune \
    origin '+refs/heads/main:refs/remotes/origin/main'

resolved_sha=$(git --git-dir="$REPOSITORY" rev-parse --verify "${target_sha}^{commit}")
[[ "$resolved_sha" = "$target_sha" ]] || fail "target did not resolve exactly"
git --git-dir="$REPOSITORY" merge-base --is-ancestor \
  "$target_sha" refs/remotes/origin/main \
  || fail "target is not reachable from origin/main"
author_email=$(git --git-dir="$REPOSITORY" show -s --format='%ae' "$target_sha")
[[ "$author_email" = "$EXPECTED_AUTHOR_EMAIL" ]] \
  || fail "target author is not the pinned release principal"

stage_dir=$(mktemp -d "/run/palimpsest-mcp-candidate.${target_sha}.XXXXXX")
git --git-dir="$REPOSITORY" show "${target_sha}:mcp/palimpsest_mcp.py" \
  >"${stage_dir}/palimpsest_mcp.py"
git --git-dir="$REPOSITORY" show "${target_sha}:server.json" \
  >"${stage_dir}/server.json"
chmod 0755 "$stage_dir"
chmod 0444 "${stage_dir}/palimpsest_mcp.py" "${stage_dir}/server.json"
(( $(stat -c '%s' "${stage_dir}/palimpsest_mcp.py") <= 2097152 )) \
  || fail "candidate server exceeds the 2 MiB release cap"
(( $(stat -c '%s' "${stage_dir}/server.json") <= 262144 )) \
  || fail "candidate manifest exceeds the 256 KiB release cap"

expected_blob=$(git --git-dir="$REPOSITORY" rev-parse \
  "${target_sha}:mcp/palimpsest_mcp.py")
actual_blob=$(git hash-object "${stage_dir}/palimpsest_mcp.py")
[[ "$actual_blob" = "$expected_blob" ]] || fail "staged server blob is not target bytes"
expected_manifest_blob=$(git --git-dir="$REPOSITORY" rev-parse \
  "${target_sha}:server.json")
actual_manifest_blob=$(git hash-object "${stage_dir}/server.json")
[[ "$actual_manifest_blob" = "$expected_manifest_blob" ]] \
  || fail "staged manifest blob is not target bytes"

api_json="${stage_dir}/github-commit.json"
curl --disable --fail --silent --show-error \
  --proto '=https' --tlsv1.2 --max-time 20 --max-filesize 262144 \
  --header 'Accept: application/vnd.github+json' \
  --header 'X-GitHub-Api-Version: 2022-11-28' \
  --header 'User-Agent: palimpsest-mcp-release-controller/1' \
  "https://api.github.com/repos/beepboop2025/palimpsest/commits/${target_sha}" \
  --output "$api_json"
chmod 0444 "$api_json"

run_as_verify_user "$VERIFY_RELEASE" \
  --module "${stage_dir}/palimpsest_mcp.py" \
  --manifest "${stage_dir}/server.json" \
  --target-sha "$target_sha" \
  --github-commit-json "$api_json" \
  --expected-author-email "$EXPECTED_AUTHOR_EMAIL"

service_exec=$(systemctl show --property=ExecStart --value "$SERVICE")
[[ "$service_exec" == *"$TARGET_FILE"* ]] \
  || fail "service ExecStart does not use the controlled runtime path"
[[ "$(systemctl show --property=FragmentPath --value "$SERVICE")" = "$UNIT_FILE" ]] \
  || fail "service does not use the pinned unit file"
[[ "$(systemctl show --property=NeedDaemonReload --value "$SERVICE")" = "no" ]] \
  || fail "systemd has not loaded the pinned unit bytes"
[[ -z "$(systemctl show --property=DropInPaths --value "$SERVICE")" ]] \
  || fail "service has unreviewed systemd drop-ins"

version=$(python3 -c \
  'import json,sys; v=json.load(open(sys.argv[1], encoding="utf-8"))["version"]; assert isinstance(v,str); print(v)' \
  "${stage_dir}/server.json")
candidate_sha256=$(sha256sum "${stage_dir}/palimpsest_mcp.py" | awk '{print $1}')

current_marker=""
if [[ -e "$DEPLOYED_SHA_FILE" ]]; then
  require_regular_root_file "$DEPLOYED_SHA_FILE"
  current_marker=$(tr -d '\n' <"$DEPLOYED_SHA_FILE")
  [[ "$current_marker" =~ ^[0-9a-f]{40}$ ]] || fail "deployed SHA marker is invalid"
fi
current_sha256=$(sha256sum "$TARGET_FILE" | awk '{print $1}')
if [[ "$current_marker" = "$target_sha" && "$current_sha256" = "$candidate_sha256" ]]; then
  run_as_verify_user "$SMOKE" --url "$LOCAL_ENDPOINT" --allow-http-loopback \
    --module "$TARGET_FILE" --manifest "${stage_dir}/server.json"
  printf 'already deployed and live: %s version %s\n' "$target_sha" "$version"
  committed=1
  exit 0
fi

timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
backup_file=$(mktemp "${BACKUP_DIR}/${timestamp}-${current_sha256}.XXXXXX.py")
install -o root -g root -m 0600 -- "$TARGET_FILE" "$backup_file"

if [[ -f "$DEPLOYED_SHA_FILE" && ! -L "$DEPLOYED_SHA_FILE" ]]; then
  previous_marker=$(mktemp "${STATE_DIR}/previous-marker.XXXXXX")
  install -o root -g root -m 0600 -- "$DEPLOYED_SHA_FILE" "$previous_marker"
fi
receipt_path="${RECEIPT_DIR}/${target_sha}.json"
if [[ -e "$receipt_path" ]]; then
  require_regular_root_file "$receipt_path"
  previous_receipt=$(mktemp "${STATE_DIR}/previous-receipt.XXXXXX")
  install -o root -g root -m 0600 -- "$receipt_path" "$previous_receipt"
fi

candidate_tmp=$(mktemp "${TARGET_DIR}/.palimpsest_mcp.py.${target_sha}.XXXXXX")
install -o root -g root -m 0644 -- "${stage_dir}/palimpsest_mcp.py" "$candidate_tmp"
sync "$candidate_tmp"
mv -fT -- "$candidate_tmp" "$TARGET_FILE"
candidate_tmp=""
sync "$TARGET_DIR"
promoted=1

systemctl restart "$SERVICE"
for _attempt in $(seq 1 12); do
  if systemctl is-active --quiet "$SERVICE" && \
    run_as_verify_user "$SMOKE" --url "$LOCAL_ENDPOINT" \
      --allow-http-loopback --timeout 10 \
      --module "$TARGET_FILE" --manifest "${stage_dir}/server.json"; then
    break
  fi
  if [[ "$_attempt" = "12" ]]; then
    fail "candidate did not pass the full local MCP smoke"
  fi
  sleep 2
done

receipt_tmp=$(mktemp "${RECEIPT_DIR}/.${target_sha}.XXXXXX")
python3 - "$receipt_tmp" "$target_sha" "$version" "$candidate_sha256" \
  "$current_marker" "$timestamp" <<'PY'
import json
import os
import sys

path, target, version, digest, previous, deployed_at = sys.argv[1:]
receipt = {
    "schema_version": 1,
    "service": "palimpsest-mcp.service",
    "target_sha": target,
    "server_version": version,
    "server_file_sha256": digest,
    "previous_sha": previous or None,
    "deployed_at_utc": deployed_at,
    "verification": {
        "target_on_origin_main": True,
        "github_signature": "valid",
        "local_initialize_list_call": "passed",
    },
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(receipt, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
chown root:root "$receipt_tmp"
chmod 0600 "$receipt_tmp"
mv -fT -- "$receipt_tmp" "$receipt_path"
receipt_tmp=""

marker_tmp=$(mktemp "${STATE_DIR}/.deployed-sha.XXXXXX")
printf '%s\n' "$target_sha" >"$marker_tmp"
chown root:root "$marker_tmp"
chmod 0600 "$marker_tmp"
sync "$marker_tmp"
mv -fT -- "$marker_tmp" "$DEPLOYED_SHA_FILE"
marker_tmp=""
sync "$STATE_DIR"
committed=1
promoted=0

printf 'release complete: %s version %s sha256 %s\n' \
  "$target_sha" "$version" "$candidate_sha256"
