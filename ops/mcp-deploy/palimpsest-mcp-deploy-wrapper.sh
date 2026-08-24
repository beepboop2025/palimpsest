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
# root-owned installed controller with the exact forced command; standard input
# remains an untrusted, bounded public-provenance channel verified below.
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
readonly STATE_DIR="/var/lib/palimpsest-mcp-deploy"
readonly REPOSITORY="${STATE_DIR}/repository.git"
readonly BACKUP_DIR="${STATE_DIR}/backups"
readonly RECEIPT_DIR="${STATE_DIR}/receipts"
readonly DEPLOYED_SHA_FILE="${STATE_DIR}/deployed-sha"
readonly INCIDENT_STATE_FILE="${STATE_DIR}/incident-degraded.json"
readonly LOCK_FILE="${STATE_DIR}/deploy.lock"
readonly TARGET_DIR="/opt/palimpsest-mcp"
readonly TARGET_FILE="${TARGET_DIR}/palimpsest_mcp.py"
readonly SERVICE="palimpsest-mcp.service"
readonly CONTROLLER="/usr/local/libexec/palimpsest-mcp-deploy"
readonly VERIFY_RELEASE="/usr/local/libexec/palimpsest-mcp-verify-release.py"
readonly GITHUB_SIGNING_KEY="/usr/local/libexec/palimpsest-github-web-flow-signing-key.asc"
readonly SMOKE="/usr/local/libexec/palimpsest-mcp-smoke.py"
readonly UNIT_FILE="/etc/systemd/system/palimpsest-mcp.service"
readonly GPGV="/usr/bin/gpgv"
readonly LOCAL_ENDPOINT="http://127.0.0.1:8793/"
readonly RUNTIME_USER="palimpsest-mcp"
readonly VERIFY_USER="palimpsest-mcp-verify"
readonly GITHUB_PROVENANCE_MAX_BYTES=262144
readonly LEGACY_SOURCE_SHA="2a80981815680006f3daf7caf503a125d6299c3c"
readonly EXPECTED_LEGACY_RUNTIME_SHA256="47d419e81ff048771acab14895a9b1e27868d7bbe14874e5cd8c1c94acfc4ed4"
readonly EXPECTED_VERIFY_SHA256="4036220cdd7c9199244652e3b659bf19d9c6bee416611bb962f14191231b089a"
readonly EXPECTED_GITHUB_SIGNING_KEY_SHA256="c135dfc1e3add3eb84e6119af7095dec97e0e92730a468d234f925a72bacaf74" # gitleaks:allow -- public trust-root digest
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
previous_incident_state=""
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

require_service_value() {
  local property=$1
  local expected=$2
  local actual
  actual=$(systemctl show --property="$property" --value "$SERVICE")
  [[ "$actual" = "$expected" ]] \
    || fail "service ${property} is ${actual@Q}, expected ${expected@Q}"
}

require_hardened_runtime() {
  systemctl is-active --quiet "$SERVICE" \
    || fail "service is not active under the reviewed unit"
  require_service_value "User" "$RUNTIME_USER"
  require_service_value "Group" "$RUNTIME_USER"
  require_service_value "NoNewPrivileges" "yes"
  require_service_value "ProtectSystem" "strict"
  require_service_value "PrivateDevices" "yes"
  require_service_value "PrivateTmp" "yes"
  require_service_value "PrivateUsers" "yes"
  require_service_value "ProtectHome" "yes"
  require_service_value "ProtectKernelTunables" "yes"
  require_service_value "RestrictSUIDSGID" "yes"
  require_service_value "LockPersonality" "yes"
  require_service_value "MemoryDenyWriteExecute" "yes"
  require_service_value "RemoveIPC" "yes"
  require_service_value "CapabilityBoundingSet" ""
  require_service_value "AmbientCapabilities" ""

  local main_pid exec_main_pid runtime_uid process_uid
  main_pid=$(systemctl show --property=MainPID --value "$SERVICE")
  exec_main_pid=$(systemctl show --property=ExecMainPID --value "$SERVICE")
  [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] \
    || fail "service has no live MainPID"
  [[ "$exec_main_pid" = "$main_pid" ]] \
    || fail "service MainPID and ExecMainPID differ"
  runtime_uid=$(id -u "$RUNTIME_USER")
  process_uid=$(ps -o uid= -p "$main_pid" | tr -d '[:space:]') \
    || fail "cannot inspect the service process owner"
  [[ "$process_uid" = "$runtime_uid" ]] \
    || fail "service PID is not owned by the unprivileged runtime account"
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
    local restore_tmp
    restore_tmp=$(mktemp "${destination}.rollback.XXXXXX")
    install -o root -g root -m 0600 -- "$saved" "$restore_tmp"
    mv -fT -- "$restore_tmp" "$destination"
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
  restore_state_file "$previous_incident_state" "$INCIDENT_STATE_FILE"
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

receive_github_provenance() {
  local output=$1
  local received_bytes
  timeout --kill-after=5s 20s \
    head --bytes="$((GITHUB_PROVENANCE_MAX_BYTES + 1))" >"$output" \
    || fail "could not read authenticated GitHub provenance from standard input"
  received_bytes=$(wc -c <"$output" | tr -d '[:space:]')
  [[ "$received_bytes" =~ ^[0-9]+$ ]] \
    || fail "authenticated GitHub provenance size is invalid"
  (( received_bytes > 0 )) \
    || fail "authenticated GitHub provenance is empty"
  (( received_bytes <= GITHUB_PROVENANCE_MAX_BYTES )) \
    || fail "authenticated GitHub provenance exceeds the 256 KiB cap"
  chmod 0444 "$output"
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
  [[ -z "$previous_incident_state" ]] || rm -f -- "$previous_incident_state"
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
require_regular_root_file "$GITHUB_SIGNING_KEY"
require_regular_root_file "$SMOKE"
require_regular_root_file "$UNIT_FILE"
require_regular_root_file "$GPGV"
[[ "$(sha256sum "$VERIFY_RELEASE" | awk '{print $1}')" = \
  "$EXPECTED_VERIFY_SHA256" ]] || fail "installed release verifier drifted"
[[ "$(sha256sum "$GITHUB_SIGNING_KEY" | awk '{print $1}')" = \
  "$EXPECTED_GITHUB_SIGNING_KEY_SHA256" ]] \
  || fail "installed GitHub signing key drifted"
[[ "$(sha256sum "$SMOKE" | awk '{print $1}')" = \
  "$EXPECTED_SMOKE_SHA256" ]] || fail "installed live smoke drifted"
[[ "$(sha256sum "$UNIT_FILE" | awk '{print $1}')" = \
  "$EXPECTED_UNIT_SHA256" ]] || fail "installed systemd unit drifted"
id -u "$RUNTIME_USER" >/dev/null 2>&1 || fail "runtime account does not exist"
id -u "$VERIFY_USER" >/dev/null 2>&1 || fail "verification account does not exist"
[[ ! -L "$LOCK_FILE" ]] || fail "lock path is a symlink"
[[ ! -L "$DEPLOYED_SHA_FILE" ]] || fail "deployed SHA path is a symlink"
[[ ! -L "$INCIDENT_STATE_FILE" ]] || fail "incident state path is a symlink"

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
current_main=$(git --git-dir="$REPOSITORY" rev-parse --verify \
  refs/remotes/origin/main)
[[ "$current_main" = "$target_sha" ]] \
  || fail "target is not the exact origin/main tip"
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
receive_github_provenance "$api_json"

run_as_verify_user "$VERIFY_RELEASE" \
  --module "${stage_dir}/palimpsest_mcp.py" \
  --manifest "${stage_dir}/server.json" \
  --target-sha "$target_sha" \
  --github-commit-json "$api_json" \
  --github-signing-key "$GITHUB_SIGNING_KEY" \
  --gpgv "$GPGV"

service_exec=$(systemctl show --property=ExecStart --value "$SERVICE")
[[ "$service_exec" == *"$TARGET_FILE"* ]] \
  || fail "service ExecStart does not use the controlled runtime path"
[[ "$(systemctl show --property=FragmentPath --value "$SERVICE")" = "$UNIT_FILE" ]] \
  || fail "service does not use the pinned unit file"
[[ "$(systemctl show --property=NeedDaemonReload --value "$SERVICE")" = "no" ]] \
  || fail "systemd has not loaded the pinned unit bytes"
[[ -z "$(systemctl show --property=DropInPaths --value "$SERVICE")" ]] \
  || fail "service has unreviewed systemd drop-ins"
require_hardened_runtime

version=$(python3 -c \
  'import json,sys; v=json.load(open(sys.argv[1], encoding="utf-8"))["version"]; assert isinstance(v,str); print(v)' \
  "${stage_dir}/server.json")
candidate_sha256=$(sha256sum "${stage_dir}/palimpsest_mcp.py" | awk '{print $1}')

current_marker=""
if [[ -e "$DEPLOYED_SHA_FILE" ]]; then
  require_regular_root_file "$DEPLOYED_SHA_FILE"
  [[ "$(wc -c <"$DEPLOYED_SHA_FILE" | tr -d '[:space:]')" = 41 ]] \
    || fail "deployed SHA marker has invalid bytes"
  grep -Eq '^[0-9a-f]{40}$' "$DEPLOYED_SHA_FILE" \
    || fail "deployed SHA marker is invalid"
  IFS= read -r current_marker <"$DEPLOYED_SHA_FILE"
fi
current_sha256=$(sha256sum "$TARGET_FILE" | awk '{print $1}')
current_runtime_source_sha=""
if [[ -n "$current_marker" ]]; then
  marker_source_sha256=$(git --git-dir="$REPOSITORY" \
    show "${current_marker}:mcp/palimpsest_mcp.py" | sha256sum | awk '{print $1}') \
    || fail "cannot bind the deployed marker to repository runtime bytes"
  if [[ "$marker_source_sha256" = "$current_sha256" ]]; then
    [[ ! -e "$INCIDENT_STATE_FILE" ]] \
      || fail "incident state exists although runtime matches the deployed marker"
    current_runtime_source_sha=$current_marker
  else
    [[ -e "$INCIDENT_STATE_FILE" ]] \
      || fail "runtime and deployed marker diverge without incident state"
    require_regular_root_file "$INCIDENT_STATE_FILE"
    incident_release_receipt="${RECEIPT_DIR}/${current_marker}.json"
    require_regular_root_file "$incident_release_receipt"
    current_marker_sha256=$(sha256sum "$DEPLOYED_SHA_FILE" | awk '{print $1}')
    incident_release_receipt_sha256=$(sha256sum "$incident_release_receipt" | \
      awk '{print $1}')
    incident_fields=$(python3 - "$INCIDENT_STATE_FILE" \
      "$current_marker" "$current_sha256" "$current_marker_sha256" \
      "$incident_release_receipt_sha256" <<'PY'
import json
import re
import sys


def reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


path, marker, runtime_digest, marker_digest, release_receipt_digest = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    receipt = json.load(
        handle,
        object_pairs_hook=strict_object,
        parse_constant=reject_constant,
    )
expected_keys = {
    "deployment_receipt_sha256",
    "incident_at_utc",
    "preserved_deployed_sha",
    "preserved_marker_sha256",
    "previous_deployed_sha",
    "restored_runtime_sha256",
    "restored_source_sha",
    "schema",
    "source_backup_basename",
    "state",
}
if not isinstance(receipt, dict) or set(receipt) != expected_keys:
    raise SystemExit("incident state shape drifted")
if receipt.get("schema") != "palimpsest.mcp-emergency-rollback-receipt.v1":
    raise SystemExit("incident state schema drifted")
if receipt.get("state") != "incident-degraded":
    raise SystemExit("incident state is not degraded")
if receipt.get("preserved_deployed_sha") != marker:
    raise SystemExit("incident state marker does not match deployed-sha")
if receipt.get("restored_runtime_sha256") != runtime_digest:
    raise SystemExit("incident state runtime digest does not match live bytes")
if receipt.get("preserved_marker_sha256") != marker_digest:
    raise SystemExit("incident state marker digest does not match live marker")
if receipt.get("deployment_receipt_sha256") != release_receipt_digest:
    raise SystemExit("incident state release receipt digest does not match history")
if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", receipt.get("incident_at_utc", "")) is None:
    raise SystemExit("incident state timestamp is invalid")
previous_sha = receipt.get("previous_deployed_sha")
if previous_sha is not None and (
    not isinstance(previous_sha, str)
    or re.fullmatch(r"[0-9a-f]{40}", previous_sha) is None
):
    raise SystemExit("incident state previous deployed SHA is invalid")
source_sha = receipt.get("restored_source_sha")
if not isinstance(source_sha, str) or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
    raise SystemExit("incident state restored source SHA is invalid")
backup = receipt.get("source_backup_basename")
if not isinstance(backup, str) or re.fullmatch(
    r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{64}\.[A-Za-z0-9]{6}\.py", backup
) is None:
    raise SystemExit("incident state source backup basename is invalid")
print(f"{source_sha}\t{backup}")
PY
    ) || fail "incident state does not bind the live degraded runtime"
    IFS=$'\t' read -r current_runtime_source_sha incident_backup_basename \
      <<<"$incident_fields"
    incident_backup_file="${BACKUP_DIR}/${incident_backup_basename}"
    require_regular_root_file "$incident_backup_file"
    [[ "$(sha256sum "$incident_backup_file" | awk '{print $1}')" = \
      "$current_sha256" ]] || fail "incident-restored backup does not match live bytes"
    incident_source_sha256=$(git --git-dir="$REPOSITORY" \
      show "${current_runtime_source_sha}:mcp/palimpsest_mcp.py" | \
      sha256sum | awk '{print $1}') \
      || fail "cannot read incident-restored source bytes"
    [[ "$incident_source_sha256" = "$current_sha256" ]] \
      || fail "incident-restored source does not match live runtime bytes"
  fi
elif [[ -e "$INCIDENT_STATE_FILE" ]]; then
  fail "incident state exists without a deployed marker"
else
  legacy_source_sha256=$(git --git-dir="$REPOSITORY" \
    show "${LEGACY_SOURCE_SHA}:mcp/palimpsest_mcp.py" | \
    sha256sum | awk '{print $1}') \
    || fail "cannot read the markerless legacy source bytes"
  [[ "$legacy_source_sha256" = "$EXPECTED_LEGACY_RUNTIME_SHA256" ]] \
    || fail "the pinned markerless legacy source drifted"
  [[ "$current_sha256" = "$EXPECTED_LEGACY_RUNTIME_SHA256" ]] \
    || fail "markerless runtime is not the pinned bootstrap legacy release"
fi

# Reacquire the moving ref immediately before either accepting an idempotent
# result or beginning the host transaction. Candidate verification can take
# long enough for main to advance after the initial fetch.
timeout --kill-after=5s 120s \
  git --git-dir="$REPOSITORY" fetch --force --no-tags --prune \
    origin '+refs/heads/main:refs/remotes/origin/main'
current_main=$(git --git-dir="$REPOSITORY" rev-parse --verify \
  refs/remotes/origin/main)
[[ "$current_main" = "$target_sha" ]] \
  || fail "target ceased to be the exact origin/main tip before promotion"

receipt_path="${RECEIPT_DIR}/${target_sha}.json"
if [[ "$current_marker" = "$target_sha" && "$current_sha256" = "$candidate_sha256" ]]; then
  [[ -e "$receipt_path" ]] \
    || fail "target runtime is present but its immutable host receipt is missing"
  require_regular_root_file "$receipt_path"
  receipt_backup_fields=$(python3 - "$receipt_path" "$target_sha" \
    "$candidate_sha256" "$version" <<'PY'
import json
import re
import sys

def reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


path, target, runtime_digest, version = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    receipt = json.load(
        handle,
        object_pairs_hook=strict_object,
        parse_constant=reject_constant,
    )
expected_keys = {
    "deployed_at_utc",
    "previous_runtime_backup",
    "previous_runtime_sha256",
    "previous_runtime_source_sha",
    "previous_sha",
    "schema_version",
    "server_file_sha256",
    "server_version",
    "service",
    "target_sha",
    "verification",
}
if not isinstance(receipt, dict) or set(receipt) != expected_keys:
    raise SystemExit("host receipt shape drifted")
if receipt.get("schema_version") != 2:
    raise SystemExit("host receipt schema drifted")
if receipt.get("service") != "palimpsest-mcp.service":
    raise SystemExit("host receipt service drifted")
if receipt.get("target_sha") != target:
    raise SystemExit("host receipt target drifted")
if receipt.get("server_version") != version:
    raise SystemExit("host receipt server version drifted")
if receipt.get("server_file_sha256") != runtime_digest:
    raise SystemExit("host receipt runtime digest drifted")
previous_digest = receipt.get("previous_runtime_sha256")
backup = receipt.get("previous_runtime_backup")
previous = receipt.get("previous_sha")
previous_source = receipt.get("previous_runtime_source_sha")
if not isinstance(previous_digest, str) or re.fullmatch(r"[0-9a-f]{64}", previous_digest) is None:
    raise SystemExit("host receipt previous runtime digest is invalid")
backup_match = re.fullmatch(
    r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{64}\.[A-Za-z0-9]{6}\.py", backup
) if isinstance(backup, str) else None
if backup_match is None or previous_digest not in backup:
    raise SystemExit("host receipt backup basename is invalid")
for name, value in (("previous SHA", previous), ("previous runtime source SHA", previous_source)):
    if value is not None and (
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None
    ):
        raise SystemExit(f"host receipt {name} is invalid")
if (previous is None) != (previous_source is None):
    raise SystemExit("host receipt previous marker/source presence disagrees")
if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", receipt.get("deployed_at_utc", "")) is None:
    raise SystemExit("host receipt deployment timestamp is invalid")
if receipt.get("verification") != {
    "github_signature": "valid",
    "local_initialize_list_call": "passed",
    "target_on_origin_main": True,
}:
    raise SystemExit("host receipt verification evidence drifted")
print(f"{previous_digest}\t{backup}")
PY
  ) || fail "immutable host receipt does not match the live target"
  IFS=$'\t' read -r receipt_previous_digest receipt_backup_basename \
    <<<"$receipt_backup_fields"
  receipt_backup_file="${BACKUP_DIR}/${receipt_backup_basename}"
  require_regular_root_file "$receipt_backup_file"
  [[ "$(sha256sum "$receipt_backup_file" | awk '{print $1}')" = \
    "$receipt_previous_digest" ]] || fail "immutable host receipt backup drifted"
  run_as_verify_user "$SMOKE" --url "$LOCAL_ENDPOINT" --allow-http-loopback \
    --module "$TARGET_FILE" --manifest "${stage_dir}/server.json"
  printf 'already deployed and live: %s version %s\n' "$target_sha" "$version"
  committed=1
  exit 0
fi
[[ ! -e "$receipt_path" && ! -L "$receipt_path" ]] \
  || fail "target already has an immutable receipt but is not the live exact runtime"

timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
backup_file=$(mktemp "${BACKUP_DIR}/${timestamp}-${current_sha256}.XXXXXX.py")
install -o root -g root -m 0600 -- "$TARGET_FILE" "$backup_file"
[[ "$(sha256sum "$backup_file" | awk '{print $1}')" = "$current_sha256" ]] \
  || fail "previous runtime backup digest mismatch"
backup_basename=${backup_file##*/}

if [[ -f "$DEPLOYED_SHA_FILE" && ! -L "$DEPLOYED_SHA_FILE" ]]; then
  previous_marker=$(mktemp "${STATE_DIR}/previous-marker.XXXXXX")
  install -o root -g root -m 0600 -- "$DEPLOYED_SHA_FILE" "$previous_marker"
fi
if [[ -f "$INCIDENT_STATE_FILE" && ! -L "$INCIDENT_STATE_FILE" ]]; then
  previous_incident_state=$(mktemp "${STATE_DIR}/previous-incident-state.XXXXXX")
  install -o root -g root -m 0600 -- \
    "$INCIDENT_STATE_FILE" "$previous_incident_state"
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
require_hardened_runtime

receipt_tmp=$(mktemp "${RECEIPT_DIR}/.${target_sha}.XXXXXX")
python3 - "$receipt_tmp" "$target_sha" "$version" "$candidate_sha256" \
  "$current_marker" "$current_sha256" "$backup_basename" \
  "$current_runtime_source_sha" "$timestamp" <<'PY'
import json
import os
import sys

(
    path,
    target,
    version,
    digest,
    previous,
    previous_runtime_digest,
    previous_runtime_backup,
    previous_runtime_source,
    deployed_at,
) = sys.argv[1:]
receipt = {
    "schema_version": 2,
    "service": "palimpsest-mcp.service",
    "target_sha": target,
    "server_version": version,
    "server_file_sha256": digest,
    "previous_sha": previous or None,
    "previous_runtime_sha256": previous_runtime_digest,
    "previous_runtime_backup": previous_runtime_backup,
    "previous_runtime_source_sha": previous_runtime_source or None,
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
rm -f -- "$INCIDENT_STATE_FILE"
[[ ! -e "$INCIDENT_STATE_FILE" && ! -L "$INCIDENT_STATE_FILE" ]] \
  || fail "incident state was not cleared at the release commit boundary"
sync "$STATE_DIR"
committed=1
promoted=0

printf 'release complete: %s version %s sha256 %s\n' \
  "$target_sha" "$version" "$candidate_sha256"
