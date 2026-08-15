#!/usr/bin/env bash
# Deploy the one-time C0 bridge before the protected-only C1 transaction.

set -Eeuo pipefail
umask 077

die() {
  printf 'palimpsest compatibility seed: %s\n' "$*" >&2
  exit 1
}

if [[ "$EUID" -eq 0 ]]; then
  [[ "${PALIMPSEST_ALLOW_ROOT_COMPATIBILITY_SEED:-}" == 1 ]] \
    || die "root execution requires PALIMPSEST_ALLOW_ROOT_COMPATIBILITY_SEED=1"
fi
for variable in C0_DEPLOY_SHA EXPECTED_PREVIOUS_DEPLOY_SHA \
    COMMON_CRAWL_WAREHOUSE_SOURCE; do
  [[ -n "${!variable:-}" ]] || die "required variable is missing: $variable"
done
[[ "$C0_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] \
  || die "C0_DEPLOY_SHA must be one full commit ID"
[[ "$EXPECTED_PREVIOUS_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] \
  || die "EXPECTED_PREVIOUS_DEPLOY_SHA must be one full commit ID"
[[ "$C0_DEPLOY_SHA" != "$EXPECTED_PREVIOUS_DEPLOY_SHA" ]] \
  || die "C0 must differ from the current deployment"
[[ "$COMMON_CRAWL_WAREHOUSE_SOURCE" =~ ^/[A-Za-z0-9._/-]+$ \
    && "$COMMON_CRAWL_WAREHOUSE_SOURCE" != / \
    && "$COMMON_CRAWL_WAREHOUSE_SOURCE" != */ \
    && "$COMMON_CRAWL_WAREHOUSE_SOURCE" != *//* \
    && "$COMMON_CRAWL_WAREHOUSE_SOURCE" != */./* \
    && "$COMMON_CRAWL_WAREHOUSE_SOURCE" != */../* ]] \
  || die "COMMON_CRAWL_WAREHOUSE_SOURCE is not a normalized absolute path"

repo_root='/home/palimpsest/palimpsest'
node_backup_root='/home/palimpsest/backups/node'
quiesce_repository_path='ops/systemd/palimpsest-backup.release-quiesce.conf'
quiesce_target='/etc/systemd/system/palimpsest-backup.service.d/zz-release-quiesce.conf'
seed_state_dir='/var/lib/palimpsest-release'
seed_state_path="$seed_state_dir/compatibility-seed-$C0_DEPLOY_SHA.json"
cd "$repo_root"
[[ -d .git && ! -L .git ]] || die "deployment checkout is missing or unsafe"

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null
export GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1
export GIT_NO_LAZY_FETCH=1 GIT_TERMINAL_PROMPT=0 GIT_PROTOCOL_FROM_USER=0
release_git() {
  /usr/bin/git --no-replace-objects -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null -c core.attributesFile=/dev/null \
    -c "safe.directory=$repo_root" \
    -c credential.helper= -c protocol.allow=never \
    -c protocol.https.allow=always "$@"
}

[[ ! -e .git/info/grafts && ! -L .git/info/grafts ]] \
  || die "Git grafts are forbidden"
[[ ! -e .git/objects/info/alternates \
    && ! -L .git/objects/info/alternates ]] \
  || die "Git object alternates are forbidden"
if [[ -e .git/refs/replace || -L .git/refs/replace ]]; then
  [[ -d .git/refs/replace && ! -L .git/refs/replace \
      && -z "$(find .git/refs/replace -mindepth 1 -print -quit)" ]] \
    || die "Git replacement refs are forbidden"
fi
[[ ! -L .git/packed-refs ]] || die "packed refs path is unsafe"
! grep -Eq '[[:space:]]refs/replace/' .git/packed-refs 2>/dev/null \
  || die "packed Git replacement refs are forbidden"
[[ -z "$(release_git status --porcelain=v1 --untracked-files=all)" ]] \
  || die "deployment checkout is not clean"
previous_receipt="$(sudo cat /etc/palimpsest/deployed-commit)"
[[ "$previous_receipt" == "$EXPECTED_PREVIOUS_DEPLOY_SHA" ]] \
  || die "deployed receipt does not match the reviewed starting commit"
[[ "$(release_git rev-parse HEAD)" == "$EXPECTED_PREVIOUS_DEPLOY_SHA" ]] \
  || die "checkout does not match the reviewed starting commit"

release_git -c fetch.fsckObjects=true -c transfer.fsckObjects=true fetch \
  --force --prune --no-tags https://github.com/beepboop2025/palimpsest.git \
  '+refs/heads/main:refs/remotes/origin/main'
release_git cat-file -e "${C0_DEPLOY_SHA}^{commit}"
release_git merge-base --is-ancestor \
  "$C0_DEPLOY_SHA" refs/remotes/origin/main
release_git merge-base --is-ancestor \
  "$EXPECTED_PREVIOUS_DEPLOY_SHA" "$C0_DEPLOY_SHA"

c0_contract_paths=(
  ops/backup/node_backup_snapshot.py
  ops/investigative-analysis/install-host-bundle.sh
  ops/common-crawl/install-host-bundle.sh
  ops/node-offsite/install-host-bundle.sh
  ops/osint-sync/deploy-compatibility-seed.sh
  ops/osint-sync/install-host-bundle.sh
  ops/osint-sync/public_osint_sync.py
  ops/osint-sync/release-mode
  ops/systemd/palimpsest-backup.release-quiesce.conf
  ops/systemd/palimpsest-public-osint-sync.compatibility.conf
  ops/systemd/palimpsest-public-osint-sync.service
  ops/systemd/palimpsest-public-osint-sync.timer
  ops/systemd/palimpsest-freshness-watchdog.service
  ops/systemd/palimpsest-freshness-watchdog.timer
)
for path in "${c0_contract_paths[@]}"; do
  release_git cat-file -e "${C0_DEPLOY_SHA}:${path}"
done
[[ "$(release_git show "$C0_DEPLOY_SHA:ops/osint-sync/release-mode")" \
    == legacy-mirror ]] \
  || die "reviewed C0 is not the legacy-mirror release mode"

# C0 may carry unrelated reviewed changes since the deployed revision, including
# a new legacy-path watchdog, but it must not change any OSINT authority edge.
# Provider dependencies, protected bind mounts, and container authority values
# belong only to C1.
legacy_authority_paths=(
  ops/docker/docker-compose.prod.yml
  ops/systemd/palimpsest-investigative-analysis.service
  ops/systemd/palimpsest-common-crawl-context.service
)
authority_boundary() {
  local commit="$1" repository_path="$2"
  if release_git cat-file -e "${commit}:${repository_path}" 2>/dev/null; then
    release_git show "${commit}:${repository_path}" \
      | LC_ALL=C grep -E \
        'palimpsest-public-osint-sync|PALIMPSEST_OSINT_AUTHORITY|PALIMPSEST_OSINT_PATH|PALIMPSEST_READINGS_HOST_PATH|PALIMPSEST_READINGS_LEDGER_PATH|/app/readings|/app/osint-authority|/var/lib/palimpsest/readings' \
      || true
  fi
}
for repository_path in "${legacy_authority_paths[@]}"; do
  previous_boundary="$(
    authority_boundary "$EXPECTED_PREVIOUS_DEPLOY_SHA" "$repository_path"
  )"
  c0_boundary="$(authority_boundary "$C0_DEPLOY_SHA" "$repository_path")"
  [[ "$c0_boundary" == "$previous_boundary" ]] \
    || die "C0 changes the OSINT authority boundary: $repository_path"
done
watchdog_c0="$(release_git show \
  "$C0_DEPLOY_SHA:ops/systemd/palimpsest-freshness-watchdog.service")"
grep -Fqx \
  'Environment=PALIMPSEST_LOCAL_OSINT_PATH=/var/lib/palimpsest/readings/osint-china-latest.json' \
  <<<"$watchdog_c0" \
  || die "C0 watchdog does not use the legacy OSINT path"

[[ -d "$COMMON_CRAWL_WAREHOUSE_SOURCE" \
    && ! -L "$COMMON_CRAWL_WAREHOUSE_SOURCE" ]] \
  || die "Common Crawl warehouse is missing or unsafe"
[[ "$(realpath -e -- "$COMMON_CRAWL_WAREHOUSE_SOURCE")" \
    == "$COMMON_CRAWL_WAREHOUSE_SOURCE" ]] \
  || die "Common Crawl warehouse is not canonical"
mount_target="$(findmnt -n -o TARGET \
  --target "$COMMON_CRAWL_WAREHOUSE_SOURCE")"
mount_options="$(findmnt -n -o OPTIONS \
  --target "$COMMON_CRAWL_WAREHOUSE_SOURCE")"
[[ -n "$mount_target" && "$mount_target" != / \
    && ",$mount_options," == *,rw,* ]] \
  || die "Common Crawl warehouse is not a writable dedicated mount"
[[ "$(/usr/local/bin/cc-downloader --version)" == 'cc-downloader 1.0.1' ]] \
  || die "cc-downloader is not the reviewed version"
[[ "$(/usr/local/bin/duckdb --version)" \
    =~ ^v1\.5\.5([[:space:]].*)?$ ]] \
  || die "DuckDB is not the reviewed version"
duckdb_sha256="$(sha256sum /usr/local/bin/duckdb | awk '{print $1}')"
[[ "$duckdb_sha256" =~ ^[0-9a-f]{64}$ \
    && "$(sudo cat /etc/palimpsest/duckdb.sha256)" == "$duckdb_sha256" ]] \
  || die "DuckDB does not match its root-owned pin"

read_enablement() {
  local state
  state="$(systemctl is-enabled "$1" 2>/dev/null || true)"
  [[ -n "$state" ]] || state='not-found'
  case "$state" in
    enabled|enabled-runtime|disabled|static|indirect|not-found) ;;
    masked|masked-runtime) die "release unit is masked: $1" ;;
    *) die "unexpected enablement for $1: $state" ;;
  esac
  printf '%s\n' "$state"
}

release_activators=(
  palimpsest-backup.timer
  palimpsest-common-crawl-backup.timer
  palimpsest-node-offsite-backup.timer
  palimpsest-evidence-wire.timer
  palimpsest-investigative-analysis.timer
  palimpsest-investigative-broker.socket
  palimpsest-common-crawl-import.path
  palimpsest-common-crawl-context.timer
  palimpsest-bleedthrough.timer
  palimpsest-public-osint-sync.timer
  palimpsest-freshness-watchdog.timer
  palimpsest-witness.timer
)
release_services=(
  palimpsest-backup.service
  palimpsest-common-crawl-backup.service
  palimpsest-node-offsite-backup.service
  palimpsest-evidence-wire.service
  palimpsest-investigative-analysis.service
  palimpsest-common-crawl-import.service
  palimpsest-common-crawl-context.service
  palimpsest-bleedthrough.service
  palimpsest-public-osint-sync.service
  palimpsest-freshness-watchdog.service
  palimpsest-witness.service
)
declare -A was_active enablement
for unit in "${release_activators[@]}"; do
  enablement["$unit"]="$(read_enablement "$unit")"
  active_state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  case "$active_state" in
    active) was_active["$unit"]=1 ;;
    inactive|failed|unknown|'') was_active["$unit"]=0 ;;
    *) die "unit is changing state: $unit ($active_state)" ;;
  esac
done
state_units_tmp="$(mktemp)"
chmod 0600 "$state_units_tmp"
for unit in "${release_activators[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" "${enablement[$unit]}" \
    "${was_active[$unit]}" >>"$state_units_tmp"
done
sudo test ! -e "$seed_state_path"
sudo test ! -L "$seed_state_path"

node_offsite_configured=0
node_offsite_config_count=0
for path in /etc/palimpsest/node-offsite.env \
    /etc/palimpsest/node-offsite-rclone.conf \
    /etc/palimpsest/node-offsite.passphrase; do
  sudo test ! -L "$path" || die "node-offsite configuration is a symlink"
  sudo test ! -e "$path" \
    || node_offsite_config_count=$((node_offsite_config_count + 1))
done
case "$node_offsite_config_count" in
  0) ;;
  3) node_offsite_configured=1 ;;
  *) die "node-offsite configuration is partial" ;;
esac
if (( node_offsite_configured == 0 )) \
    && { [[ "${enablement[palimpsest-node-offsite-backup.timer]}" \
        == enabled* ]] \
      || [[ "${was_active[palimpsest-node-offsite-backup.timer]}" == 1 ]]; }; then
  die "unconfigured node-offsite timer is enabled or active"
fi

stop_loaded_unit() {
  local unit="$1" load_state active_state
  load_state="$(systemctl show --property=LoadState --value \
    "$unit" 2>/dev/null || true)"
  case "$load_state" in
    ''|not-found) return 0 ;;
    loaded) ;;
    masked) die "release unit is masked: $unit" ;;
    *) die "unexpected load state for $unit: $load_state" ;;
  esac
  sudo systemctl stop "$unit"
  active_state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  case "$active_state" in
    inactive|failed|unknown|'') ;;
    *) die "unit did not stop: $unit ($active_state)" ;;
  esac
}

mutation_started=0
seed_committed=0
seed_fail_safe() {
  local unit
  (( mutation_started == 1 && seed_committed == 0 )) || return 0
  trap - ERR HUP INT TERM
  set +e
  printf 'compatibility seed failed; leaving every activator disabled\n' >&2
  for unit in "${release_activators[@]}"; do
    sudo systemctl stop "$unit" >/dev/null 2>&1 || true
    sudo systemctl disable "$unit" >/dev/null 2>&1 || true
  done
  sudo systemctl daemon-reload >/dev/null 2>&1 || true
  rm -f -- "${state_units_tmp:-}" >/dev/null 2>&1 || true
}
trap seed_fail_safe ERR
trap 'seed_fail_safe; exit 1' HUP INT TERM

mutation_started=1
for unit in "${release_activators[@]}"; do
  stop_loaded_unit "$unit"
done
for unit in "${release_services[@]}"; do
  stop_loaded_unit "$unit"
done
sudo systemctl stop 'palimpsest-common-crawl-mirror@*.service' \
  'palimpsest-common-crawl-filter@*.service' 2>/dev/null || true
for unit in "${release_activators[@]}"; do
  case "${enablement[$unit]}" in
    enabled|enabled-runtime) sudo systemctl disable "$unit" ;;
    disabled|static|indirect|not-found) ;;
  esac
done

backup_on_success="$(systemctl show --property=OnSuccess --value \
  palimpsest-backup.service 2>/dev/null || true)"
sudo test ! -e "$quiesce_target"
sudo test ! -L "$quiesce_target"
quiesce_added=0
if [[ -n "$backup_on_success" ]]; then
  quiesce_tmp="$(mktemp)"
  release_git show "$C0_DEPLOY_SHA:$quiesce_repository_path" >"$quiesce_tmp"
  [[ -s "$quiesce_tmp" ]] || die "C0 quiesce file is empty"
  sudo install -d -o root -g root -m 0755 \
    /etc/systemd/system/palimpsest-backup.service.d
  sudo install -o root -g root -m 0644 "$quiesce_tmp" "$quiesce_target"
  sudo cmp -s "$quiesce_tmp" "$quiesce_target"
  rm -f -- "$quiesce_tmp"
  sudo systemctl daemon-reload
  [[ -z "$(systemctl show --property=OnSuccess --value \
    palimpsest-backup.service)" ]] \
    || die "backup success triggers were not quiesced"
  quiesce_added=1
fi

latest_node_snapshot() {
  sudo find "$node_backup_root" -mindepth 1 -maxdepth 1 -type d \
    -name '20??????T??????Z' -printf '%f\n' \
    | LC_ALL=C sort | tail -n 1
}

create_and_verify_snapshot() {
  local before="$1" output_variable="$2" snapshot actual expected receipt
  sudo systemctl reset-failed palimpsest-backup.service
  sudo systemctl start palimpsest-backup.service
  [[ "$(sudo systemctl show --property=ConditionResult --value \
      palimpsest-backup.service)" == yes ]] \
    || die "backup condition did not run"
  [[ "$(sudo systemctl show --property=Result --value \
      palimpsest-backup.service)" == success \
      && "$(sudo systemctl show --property=ExecMainStatus --value \
        palimpsest-backup.service)" == 0 ]] \
    || die "backup service did not succeed"
  snapshot="$(latest_node_snapshot)"
  [[ -n "$snapshot" && "$snapshot" != "$before" ]] \
    || die "backup did not publish a new snapshot"
  sudo test -d "$node_backup_root/$snapshot"
  sudo test ! -L "$node_backup_root/$snapshot"
  sudo bash -c 'cd "$1" && sha256sum --check SHA256SUMS' \
    _ "$node_backup_root/$snapshot"
  expected=$'MANIFEST.txt\nSHA256SUMS\nartifacts.list\nartifacts.tar.gz\npostgres.dump\npostgres.list'
  actual="$(sudo find "$node_backup_root/$snapshot" \
    -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort)"
  [[ "$actual" == "$expected" ]] || die "backup inventory is not exact"
  for file in MANIFEST.txt artifacts.list artifacts.tar.gz postgres.dump \
      postgres.list; do
    sudo test -s "$node_backup_root/$snapshot/$file"
  done
  receipt="$(sudo python3 ops/backup/node_backup_snapshot.py verify \
    "$node_backup_root/$snapshot" --snapshot-id "$snapshot")"
  printf '%s\n' "$receipt" | python3 -c '
import json, sys
snapshot = sys.argv[1]
value = json.load(sys.stdin)
checks = (
    value.get("schema") == "palimpsest-node-backup-verification.v1",
    value.get("status") == "verified",
    value.get("snapshot") == snapshot,
    value.get("counts", {}).get("snapshot_files") == 6,
    value.get("counts", {}).get("checksum_entries") == 5,
    value.get("counts", {}).get("artifact_members", 0) > 0,
)
if not all(checks):
    raise SystemExit("backup verification receipt failed")
' "$snapshot"
  printf -v "$output_variable" '%s' "$snapshot"
}

write_seed_state() {
  local status="$1" post_snapshot="$2" state_tmp state_stage
  state_tmp="$(mktemp)"
  python3 - "$status" "$EXPECTED_PREVIOUS_DEPLOY_SHA" "$C0_DEPLOY_SHA" \
    "$pre_seed_snapshot" "$post_snapshot" "$backup_on_success" \
    "$state_units_tmp" >"$state_tmp" <<'PY'
import json
import pathlib
import sys

status, previous, c0, pre_snapshot, post_snapshot, on_success, units_path = (
    sys.argv[1:]
)
activators = []
for line in pathlib.Path(units_path).read_text(encoding="utf-8").splitlines():
    unit, enablement, active = line.split("\t")
    activators.append({
        "unit": unit,
        "enablement": enablement,
        "was_active": active == "1",
    })
value = {
    "schema": "palimpsest-compatibility-seed.v1",
    "status": status,
    "previous_deploy_sha": previous,
    "c0_deploy_sha": c0,
    "pre_seed_snapshot": pre_snapshot,
    "post_seed_snapshot": post_snapshot or None,
    "backup_on_success": on_success,
    "captured_activators": activators,
}
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
  sudo install -d -o root -g root -m 0700 "$seed_state_dir"
  state_stage="$seed_state_dir/.compatibility-seed-$C0_DEPLOY_SHA.$$"
  sudo test ! -e "$state_stage"
  sudo test ! -L "$state_stage"
  sudo install -o root -g root -m 0600 "$state_tmp" "$state_stage"
  rm -f -- "$state_tmp"
  if [[ "$status" == prepared ]]; then
    sudo test ! -e "$seed_state_path"
    sudo test ! -L "$seed_state_path"
  else
    [[ "$(sudo stat -c '%u:%g:%a:%h' "$seed_state_path")" \
        == '0:0:600:1' ]] \
      || die "prepared seed state is unsafe"
  fi
  sudo mv -Tf "$state_stage" "$seed_state_path"
  [[ "$(sudo stat -c '%u:%g:%a:%h' "$seed_state_path")" \
      == '0:0:600:1' ]] \
    || die "seed state did not converge safely"
  sudo python3 -m json.tool "$seed_state_path" >/dev/null
}

snapshot_before="$(latest_node_snapshot)"
pre_seed_snapshot=''
create_and_verify_snapshot "$snapshot_before" pre_seed_snapshot
[[ -n "$pre_seed_snapshot" ]] || die "pre-seed snapshot was not recorded"
write_seed_state prepared ''

shared_artifact='/var/lib/palimpsest/readings/osint-china-latest.json'
shared_ledger='/var/lib/palimpsest/readings/readings-ledger.jsonl'
for path in "$shared_artifact" "$shared_ledger"; do
  sudo test -f "$path"
  sudo test ! -L "$path"
done
artifact_identity_before="$(sudo stat -c '%u:%g:%a' "$shared_artifact")"
ledger_identity_before="$(sudo stat -c '%u:%g:%a' "$shared_ledger")"

release_git switch --detach "$C0_DEPLOY_SHA"
[[ "$(release_git rev-parse HEAD)" == "$C0_DEPLOY_SHA" \
    && -z "$(release_git status --porcelain=v1 --untracked-files=all)" ]] \
  || die "C0 checkout did not converge"
[[ "$(cat ops/osint-sync/release-mode)" == legacy-mirror ]] \
  || die "checked-out C0 is not the compatibility mode"
ops/docker/prod-compose build
sudo bash ops/investigative-analysis/install-host-bundle.sh --certify-image
sudo bash ops/osint-sync/install-host-bundle.sh
sudo systemctl reset-failed palimpsest-public-osint-sync.service
sudo systemctl start palimpsest-public-osint-sync.service
[[ "$(systemctl show --property=ConditionResult --value \
    palimpsest-public-osint-sync.service)" == yes \
    && "$(systemctl show --property=Result --value \
      palimpsest-public-osint-sync.service)" == success \
    && "$(systemctl show --property=ExecMainStatus --value \
      palimpsest-public-osint-sync.service)" == 0 ]] \
  || die "C0 public OSINT provider did not succeed"
sudo /usr/bin/python3 \
  /usr/local/libexec/palimpsest-public-osint-sync/current/public_osint_sync.py \
  --legacy-readings-mirror --verify-installed >/dev/null
authority='/var/lib/palimpsest-public-osint-sync/authoritative'
sudo cmp -s "$authority/osint-china-latest.json" "$shared_artifact"
sudo cmp -s "$authority/readings-ledger.jsonl" "$shared_ledger"
[[ "$(sudo stat -c '%u:%g:%a' "$shared_artifact")" \
    == "$artifact_identity_before" \
    && "$(sudo stat -c '%u:%g:%a' "$shared_ledger")" \
      == "$ledger_identity_before" ]] \
  || die "C0 changed legacy reading ownership or mode"

# These consumers retain the deployed OSINT authority boundary proved above.
# Reinstalling their C0 bundles may carry unrelated reviewed reliability changes
# while advancing every revision receipt without activating C1 mounts.
sudo bash ops/investigative-analysis/install-host-bundle.sh
sudo bash ops/common-crawl/install-host-bundle.sh \
  --warehouse-source "$COMMON_CRAWL_WAREHOUSE_SOURCE"
sudo bash ops/node-offsite/install-host-bundle.sh
for observer_source in \
    ops/systemd/palimpsest-freshness-watchdog.service \
    ops/systemd/palimpsest-freshness-watchdog.timer; do
  observer_target="/etc/systemd/system/${observer_source##*/}"
  sudo test ! -L "$observer_target" \
    || die "freshness observer target is a symlink: $observer_target"
  sudo install -o root -g root -m 0644 "$observer_source" "$observer_target"
  sudo cmp -s "$observer_source" "$observer_target" \
    || die "freshness observer did not install exactly: $observer_target"
  [[ "$(sudo stat -c '%u:%g:%a:%h' "$observer_target")" == '0:0:644:1' ]] \
    || die "freshness observer metadata is unsafe: $observer_target"
done
sudo systemd-analyze verify \
  /etc/systemd/system/palimpsest-freshness-watchdog.service \
  /etc/systemd/system/palimpsest-freshness-watchdog.timer
sudo systemctl daemon-reload
for revision_path in \
    /etc/palimpsest/deployed-commit \
    /usr/local/libexec/palimpsest-analysis/current/REVISION \
    /usr/local/libexec/palimpsest-network-lane/current/REVISION \
    /usr/local/libexec/palimpsest-common-crawl/current/REVISION \
    /usr/local/libexec/palimpsest-public-osint-sync/current/REVISION \
    /usr/local/libexec/palimpsest-node-offsite/current/REVISION; do
  [[ "$(sudo cat "$revision_path")" == "$C0_DEPLOY_SHA" ]] \
    || die "bundle revision parity failed: $revision_path"
done
ops/docker/prod-compose up -d
[[ "$(ops/docker/prod-compose port api 8000)" == '127.0.0.1:8010' ]]
curl --fail --silent --show-error \
  http://127.0.0.1:8010/api/v1/node/status \
  | python3 -m json.tool >/dev/null

# Prove the old consumers can parse the later mirrored ledger and artifact.
sudo systemctl reset-failed palimpsest-common-crawl-import.service
sudo systemctl start palimpsest-common-crawl-import.service
[[ "$(systemctl show --property=ConditionResult --value \
    palimpsest-common-crawl-import.service)" == yes \
    && "$(systemctl show --property=Result --value \
      palimpsest-common-crawl-import.service)" == success \
    && "$(systemctl show --property=ExecMainStatus --value \
      palimpsest-common-crawl-import.service)" == 0 ]] \
  || die "C0 Common Crawl import did not succeed"
for service in palimpsest-investigative-analysis.service \
    palimpsest-common-crawl-context.service; do
  sudo systemctl reset-failed "$service"
  sudo systemctl start "$service"
  [[ "$(systemctl show --property=ConditionResult --value "$service")" == yes \
      && "$(systemctl show --property=Result --value "$service")" == success \
      && "$(systemctl show --property=ExecMainStatus --value "$service")" == 0 ]] \
    || die "legacy consumer failed against C0 mirror: $service"
done

post_seed_before="$(latest_node_snapshot)"
post_seed_snapshot=''
create_and_verify_snapshot "$post_seed_before" post_seed_snapshot
[[ -n "$post_seed_snapshot" ]] || die "post-seed snapshot was not recorded"

if (( quiesce_added == 1 )); then
  sudo cmp -s ops/systemd/palimpsest-backup.release-quiesce.conf \
    "$quiesce_target"
  sudo rm -- "$quiesce_target"
  sudo systemctl daemon-reload
  [[ "$(systemctl show --property=OnSuccess --value \
    palimpsest-backup.service)" == "$backup_on_success" ]] \
    || die "backup success triggers were not restored"
fi

restore_enablement() {
  local unit="$1" previous="${enablement[$1]}" first_install='disable'
  [[ "$unit" == palimpsest-public-osint-sync.timer \
      || "$unit" == palimpsest-freshness-watchdog.timer ]] \
    && first_install='enable'
  case "$previous" in
    enabled) sudo systemctl enable "$unit" ;;
    enabled-runtime) sudo systemctl enable --runtime "$unit" ;;
    disabled) sudo systemctl disable "$unit" ;;
    static|indirect) [[ "$(read_enablement "$unit")" == "$previous" ]] ;;
    not-found)
      if [[ "$first_install" == enable ]]; then
        sudo systemctl enable "$unit"
      else
        sudo systemctl disable "$unit"
      fi
      ;;
  esac
}
for unit in "${release_activators[@]}"; do
  if [[ "$unit" == palimpsest-node-offsite-backup.timer \
      && "$node_offsite_configured" == 0 ]]; then
    [[ "${was_active[$unit]}" == 0 ]] \
      || die "unconfigured node-offsite timer was active"
  fi
  restore_enablement "$unit"
done
for unit in "${release_activators[@]}"; do
  if [[ "${was_active[$unit]}" == 1 ]] \
      || { [[ "${enablement[$unit]}" == not-found ]] \
        && { [[ "$unit" == palimpsest-public-osint-sync.timer ]] \
          || [[ "$unit" == palimpsest-freshness-watchdog.timer ]]; }; }; then
    sudo systemctl start "$unit"
  else
    stop_loaded_unit "$unit"
  fi
done

write_seed_state complete "$post_seed_snapshot"
rm -f -- "$state_units_tmp"
seed_committed=1
trap - ERR HUP INT TERM
printf 'C0 compatibility seed complete: previous=%s c0=%s pre=%s post=%s\n' \
  "$EXPECTED_PREVIOUS_DEPLOY_SHA" "$C0_DEPLOY_SHA" \
  "$pre_seed_snapshot" "$post_seed_snapshot"
