#!/usr/bin/env bash
# Install a revision-bound Common Crawl bundle and a validated bulk-volume bind mount.

set -Eeuo pipefail

die() {
  printf 'palimpsest-common-crawl install: %s\n' "$*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || die "run this installer as root"
(( $# == 2 )) && [[ "$1" == "--warehouse-source" ]] \
  || die "usage: $0 --warehouse-source /mounted/volume/path"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/../.." && pwd -P)"
warehouse_source="$2"
state_root="/var/lib/palimpsest/common-crawl"
bundle_root="/usr/local/libexec/palimpsest-common-crawl"
lane_state_root="/var/lib/palimpsest/network-lane"
lane_bundle_root="/usr/local/libexec/palimpsest-network-lane"
receipt_path="/etc/palimpsest/deployed-commit"
duckdb_pin_path="/etc/palimpsest/duckdb.sha256"
mount_template="$script_dir/palimpsest-common-crawl.mount.in"
minimum_initial_free_bytes=$((256 * 1024 * 1024 * 1024))
service_units=(
  palimpsest-common-crawl-import.service
  palimpsest-common-crawl-import.path
  palimpsest-common-crawl-context.service
  palimpsest-common-crawl-context.timer
  palimpsest-common-crawl-filter@.service
)
backup_units=(
  palimpsest-common-crawl-backup.service
  palimpsest-common-crawl-backup.timer
)
network_units=(
  palimpsest-bleedthrough.service
  palimpsest-bleedthrough.timer
  palimpsest-common-crawl-mirror@.service
)

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE

for command_name in awk bash chmod chown cmp df dirname find findmnt getent \
  getfacl grep install ln mktemp mountpoint mv pgrep readlink realpath rm sed \
  sha256sum sort stat sync systemctl systemd-analyze systemd-escape \
  systemd-tmpfiles; do
  command -v "$command_name" >/dev/null 2>&1 \
    || die "required command is missing: $command_name"
done

[[ -x /usr/bin/git && ! -L /usr/bin/git ]] \
  || die "the pinned Git executable is missing or unsafe"
[[ -d "$repo_root/.git" && ! -L "$repo_root/.git" \
    && -d "$repo_root/.git/objects" && ! -L "$repo_root/.git/objects" \
    && -f "$repo_root/.git/index" && ! -L "$repo_root/.git/index" ]] \
  || die "the deployment checkout Git metadata is unsafe"
for forbidden_path in \
    "$repo_root/.git/info/grafts" \
    "$repo_root/.git/objects/info/alternates"; do
  [[ ! -e "$forbidden_path" && ! -L "$forbidden_path" ]] \
    || die "Git grafts or object alternates are forbidden"
done
if [[ -e "$repo_root/.git/refs/replace" \
    || -L "$repo_root/.git/refs/replace" ]]; then
  [[ -d "$repo_root/.git/refs/replace" \
      && ! -L "$repo_root/.git/refs/replace" ]] \
    || die "Git replacement refs path is unsafe"
  [[ -z "$(find "$repo_root/.git/refs/replace" \
      -mindepth 1 -print -quit)" ]] \
    || die "Git replacement refs are forbidden"
fi
[[ ! -L "$repo_root/.git/packed-refs" ]] \
  || die "packed Git refs path is unsafe"
if [[ -f "$repo_root/.git/packed-refs" ]] \
    && grep -Eq '[[:space:]]refs/replace/' "$repo_root/.git/packed-refs"; then
  die "packed Git replacement refs are forbidden"
fi

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null
export GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1
export GIT_NO_LAZY_FETCH=1 GIT_TERMINAL_PROMPT=0 GIT_PROTOCOL_FROM_USER=0
audit_git="$(mktemp -d /run/palimpsest-common-crawl-git.XXXXXX)"
chmod 0700 "$audit_git"
cleanup_audit_git() {
  if [[ -n "${audit_git:-}" && -d "$audit_git" ]]; then
    rm -rf -- "$audit_git"
  fi
}
trap cleanup_audit_git EXIT
/usr/bin/git --no-replace-objects init --bare --quiet "$audit_git" \
  || die "cannot initialize the isolated Git audit view"
install -o root -g root -m 0600 "$repo_root/.git/index" "$audit_git/index"
export GIT_ALTERNATE_OBJECT_DIRECTORIES="$repo_root/.git/objects"
safe_git() {
  /usr/bin/git --no-replace-objects --git-dir="$audit_git" \
    --work-tree="$repo_root" -c core.bare=false -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null -c core.attributesFile=/dev/null \
    -c credential.helper= -c protocol.file.allow=never "$@"
}

[[ "$warehouse_source" =~ ^/[A-Za-z0-9._/-]+$ ]] \
  || die "warehouse source contains unsupported characters"
[[ "$warehouse_source" != "/" && "$warehouse_source" != */ \
    && "$warehouse_source" != *"//"* && "$warehouse_source" != *"/./"* \
    && "$warehouse_source" != *"/../"* ]] \
  || die "warehouse source is not a normalized non-root path"

source_parent="$(dirname -- "$warehouse_source")"
source_name="${warehouse_source##*/}"
[[ -d "$source_parent" && ! -L "$source_parent" ]] \
  || die "warehouse parent must be an existing real directory"
source_parent="$(realpath -e -- "$source_parent")"
warehouse_source="$source_parent/$source_name"
[[ "$warehouse_source" != "$state_root" ]] \
  || die "warehouse source and stable state path must differ"

backing_target="$(findmnt -n -o TARGET --target "$source_parent" 2>/dev/null || true)"
backing_options="$(findmnt -n -o OPTIONS --target "$source_parent" 2>/dev/null || true)"
[[ -n "$backing_target" && "$backing_target" != "/" ]] \
  || die "warehouse source must live on a mounted non-root filesystem"
[[ ",$backing_options," == *,rw,* ]] \
  || die "warehouse backing filesystem is not writable"

if [[ -e "$warehouse_source" || -L "$warehouse_source" ]]; then
  [[ -d "$warehouse_source" && ! -L "$warehouse_source" ]] \
    || die "warehouse source exists but is not a real directory"
else
  available_bytes="$(df -PB1 "$source_parent" | awk 'NR == 2 {print $4}')"
  [[ "$available_bytes" =~ ^[0-9]+$ ]] \
    || die "cannot determine initial warehouse capacity"
  (( available_bytes >= minimum_initial_free_bytes )) \
    || die "new warehouse requires at least 256 GiB free on its backing filesystem"
fi

revision="$(cat "$repo_root/.git/HEAD")" \
  || die "cannot read the detached Git revision"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die "Git returned a malformed revision"
printf '%s\n' "$revision" >"$audit_git/HEAD"
checkout_status="$(
  safe_git status --porcelain=v1 --untracked-files=all 2>/dev/null
)" || die "cannot verify the Git checkout through the isolated audit view"
[[ "$revision" =~ ^[0-9a-f]{40}$ && -z "$checkout_status" ]] \
  || die "checkout is dirty or has a malformed revision"
[[ -r "$receipt_path" ]] || die "deployed commit receipt is missing"
IFS= read -r deployed_revision <"$receipt_path" \
  || die "cannot read deployed commit receipt"
[[ "$deployed_revision" == "$revision" ]] \
  || die "deployed commit receipt does not match Git HEAD"

[[ -f /usr/local/bin/cc-downloader && ! -L /usr/local/bin/cc-downloader \
    && -x /usr/local/bin/cc-downloader ]] \
  || die "cc-downloader must be a real executable at /usr/local/bin/cc-downloader"
[[ "$(stat -c '%u:%g' /usr/local/bin/cc-downloader)" == "0:0" ]] \
  || die "cc-downloader must be owned by root:root"
downloader_mode="$(stat -c '%a' /usr/local/bin/cc-downloader)"
[[ "$downloader_mode" =~ ^[0-7]{3,4}$ ]] \
  || die "cc-downloader has an unreadable mode"
(( (8#$downloader_mode & 0022) == 0 )) \
  || die "cc-downloader must not be group/world-writable"
[[ "$(/usr/local/bin/cc-downloader --version)" == "cc-downloader 1.0.1" ]] \
  || die "cc-downloader must report exact version 1.0.1"

validate_and_enroll_duckdb() {
  local duckdb_path="${duckdb_path:-/usr/local/bin/duckdb}"
  local pin_parent pin_parent_mode duckdb_mode duckdb_version duckdb_sha256
  local pinned_duckdb_sha256
  [[ -f "$duckdb_path" && ! -L "$duckdb_path" && -x "$duckdb_path" ]] \
    || die "DuckDB must be a real executable at $duckdb_path"
  [[ "$(stat -c '%u:%g' "$duckdb_path")" == "0:0" ]] \
    || die "DuckDB must be owned by root:root"
  duckdb_mode="$(stat -c '%a' "$duckdb_path")"
  [[ "$duckdb_mode" =~ ^[0-7]{3,4}$ ]] \
    || die "DuckDB has an unreadable mode"
  (( (8#$duckdb_mode & 0022) == 0 )) \
    || die "DuckDB must not be group/world-writable"
  duckdb_version="$("$duckdb_path" --version)"
  [[ "$duckdb_version" =~ ^v1\.5\.5([[:space:]].*)?$ ]] \
    || die "DuckDB must report exact version 1.5.5"
  duckdb_sha256="$(sha256sum "$duckdb_path" | awk '{print $1}')"
  [[ "$duckdb_sha256" =~ ^[0-9a-f]{64}$ ]] \
    || die "cannot compute DuckDB SHA-256"
  pin_parent="$(dirname -- "$duckdb_pin_path")"
  [[ -d "$pin_parent" && ! -L "$pin_parent" \
      && "$(stat -c '%u:%g' "$pin_parent")" == "0:0" ]] \
    || die "DuckDB hash pin parent is unsafe"
  pin_parent_mode="$(stat -c '%a' "$pin_parent")"
  [[ "$pin_parent_mode" =~ ^[0-7]{3,4}$ ]] \
    || die "DuckDB hash pin parent has an unreadable mode"
  (( (8#$pin_parent_mode & 0022) == 0 )) \
    || die "DuckDB hash pin parent is writable by group/other"
  if [[ -e "$duckdb_pin_path" || -L "$duckdb_pin_path" ]]; then
    [[ -f "$duckdb_pin_path" && ! -L "$duckdb_pin_path" \
        && "$(stat -c '%u:%g:%a:%h' "$duckdb_pin_path")" == "0:0:444:1" ]] \
      || die "DuckDB hash pin ownership/mode/link count is unsafe"
    IFS= read -r pinned_duckdb_sha256 <"$duckdb_pin_path" \
      || die "cannot read DuckDB hash pin"
    [[ "$pinned_duckdb_sha256" == "$duckdb_sha256" ]] \
      || die "DuckDB does not match the enrolled root-owned SHA-256 pin"
  else
    duckdb_pin_tmp="$(mktemp "$pin_parent/.duckdb.sha256.XXXXXX")"
    printf '%s\n' "$duckdb_sha256" >"$duckdb_pin_tmp"
    chown root:root "$duckdb_pin_tmp"
    chmod 0444 "$duckdb_pin_tmp"
    sync -f "$duckdb_pin_tmp"
    mv -T "$duckdb_pin_tmp" "$duckdb_pin_path"
    duckdb_pin_tmp=""
    sync -f "$pin_parent"
  fi
}

# Installers replace these unit paths and cannot preserve masked state. Refuse
# before identity, storage, pin, bundle, or unit mutation.
for unit_name in "${service_units[@]}" "${backup_units[@]}" \
    "${network_units[@]}"; do
  unit_enablement="$(systemctl is-enabled "$unit_name" 2>/dev/null || true)"
  case "$unit_enablement" in
    masked|masked-runtime) die "$unit_name is masked" ;;
  esac
done

bash "$repo_root/ops/investigative-analysis/install-host-bundle.sh" --ensure-identity
user_record="$(getent passwd palimpsest-analysis || true)"
group_record="$(getent group palimpsest-analysis || true)"
IFS=: read -r user_name _ user_id user_group _ user_home user_shell <<<"$user_record"
IFS=: read -r group_name _ group_id _ <<<"$group_record"
[[ "$user_name" == "palimpsest-analysis" && "$user_id" == "10001" \
    && "$user_group" == "10001" && "$user_home" == "/nonexistent" \
    && "$user_shell" == */nologin && "$group_name" == "palimpsest-analysis" \
    && "$group_id" == "10001" ]] \
  || die "the locked palimpsest-analysis identity is invalid"

for unit_name in "${service_units[@]}"; do
  load_state="$(systemctl show --property=LoadState --value "$unit_name" 2>/dev/null || true)"
  [[ -z "$load_state" || "$load_state" == "not-found" ]] && continue
  active_state="$(systemctl show --property=ActiveState --value "$unit_name" 2>/dev/null)" \
    || die "cannot verify systemd state for $unit_name"
  case "$active_state" in
    inactive|failed) ;;
    *) die "$unit_name must be stopped before installation" ;;
  esac
done
backup_state="$(
  systemctl show --property=ActiveState --value \
    palimpsest-common-crawl-backup.service 2>/dev/null || true
)"
case "$backup_state" in
  ""|inactive|failed) ;;
  *) die "palimpsest-common-crawl-backup.service must finish before installation" ;;
esac

# The old BLEED unit does not know about the shared lane. Hold both the timer and
# service down until the revision-bound helper, ACL, and replacement unit exist.
for unit_name in palimpsest-bleedthrough.timer palimpsest-bleedthrough.service; do
  load_state="$(systemctl show --property=LoadState --value "$unit_name" 2>/dev/null || true)"
  [[ -z "$load_state" || "$load_state" == "not-found" ]] && continue
  active_state="$(systemctl show --property=ActiveState --value "$unit_name" 2>/dev/null)" \
    || die "cannot verify systemd state for $unit_name"
  case "$active_state" in
    inactive|failed) ;;
    *) die "$unit_name must be stopped before network-lane installation" ;;
  esac
done
bleed_timer_enablement="$(
  systemctl is-enabled palimpsest-bleedthrough.timer 2>/dev/null || true
)"
case "$bleed_timer_enablement" in
  ""|disabled|not-found) ;;
  masked|masked-runtime) die "palimpsest-bleedthrough.timer is masked" ;;
  *) die "palimpsest-bleedthrough.timer must be disabled during installation" ;;
esac
active_mirrors="$(
  systemctl list-units --state=activating,active,reloading,deactivating \
    --no-legend --plain 'palimpsest-common-crawl-mirror@*.service' 2>/dev/null \
    || true
)"
[[ -z "$active_mirrors" ]] \
  || die "all Common Crawl mirror instances must be stopped before installation"
active_filters="$(
  systemctl list-units --state=activating,active,reloading,deactivating \
    --no-legend --plain 'palimpsest-common-crawl-filter@*.service' 2>/dev/null \
    || true
)"
[[ -z "$active_filters" ]] \
  || die "all Common Crawl filter instances must be stopped before installation"
if pgrep -u palimpsest -f '/ops/bleedthrough_prober[.]sh([[:space:]]|$)' >/dev/null; then
  die "a BLEEDTHROUGH prober remains outside the stopped unit"
fi
if pgrep -u palimpsest-analysis -f '(^|/)cc-downloader([[:space:]]|$)' >/dev/null; then
  die "a Common Crawl downloader remains outside the stopped unit"
fi
if pgrep -u palimpsest-analysis \
    -f '(^|[[:space:]])/usr/local/bin/[d]uckdb([[:space:]]|$)' >/dev/null; then
  die "a DuckDB filter remains outside the stopped unit"
fi

install -d -o palimpsest-analysis -g palimpsest-analysis -m 0750 \
  "$warehouse_source" "$warehouse_source/inbox"
if mountpoint -q "$state_root"; then
  [[ "$(stat -c '%d:%i' "$warehouse_source")" == "$(stat -c '%d:%i' "$state_root")" ]] \
    || die "stable state path is mounted from a different source"
else
  [[ ! -L "$state_root" ]] || die "stable state path must not be a symlink"
  if [[ -d "$state_root" ]]; then
    [[ -z "$(find "$state_root" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
      || die "unmounted stable state path is not empty"
  else
    install -d -o root -g root -m 0755 "$state_root"
  fi
fi

verify_git_blob() {
  local repository_path="$1"
  local installed_path="$2"
  safe_git show "$revision:$repository_path" \
    | cmp -s - "$installed_path" \
    || die "installed bytes do not match Git HEAD: $repository_path"
}

install_verified_file() {
  local repository_path="$1"
  local installed_path="$2"
  local installed_mode="$3"
  install -o root -g root -m "$installed_mode" \
    "$repo_root/$repository_path" "$installed_path"
  verify_git_blob "$repository_path" "$installed_path"
}

require_exact_acl() {
  local acl_path="$1"
  local expected_acl="$2"
  local actual_acl normalized_expected_acl
  actual_acl="$(
    getfacl -cp -- "$acl_path" \
      | sed '/^[[:space:]]*$/d' \
      | LC_ALL=C sort
  )"
  normalized_expected_acl="$(
    printf '%s\n' "$expected_acl" \
      | sed '/^[[:space:]]*$/d' \
      | LC_ALL=C sort
  )"
  [[ "$actual_acl" == "$normalized_expected_acl" ]] \
    || die "network-lane ACL does not exactly match policy on $acl_path"
}

validate_network_lane_state() {
  [[ -d "$lane_state_root" && ! -L "$lane_state_root" \
      && -O "$lane_state_root" && -G "$lane_state_root" ]] \
    || die "network-lane root must be a real root-owned directory"
  for lock_name in lane.lock dataset.lock; do
    lock_path="$lane_state_root/$lock_name"
    [[ -f "$lock_path" && ! -L "$lock_path" \
        && -O "$lock_path" && -G "$lock_path" \
        && "$(stat -c '%h' "$lock_path")" == "1" ]] \
      || die "network-lane $lock_name must be a real root-owned regular file"
  done
  for shared_directory in state receipts; do
    shared_path="$lane_state_root/$shared_directory"
    [[ -d "$shared_path" && ! -L "$shared_path" \
        && -O "$shared_path" && -G "$shared_path" ]] \
      || die "network-lane $shared_directory must be a real root-owned directory"
  done

  require_exact_acl "$lane_state_root" $'user::rwx\nuser:palimpsest:r-x\nuser:palimpsest-analysis:r-x\ngroup::r-x\nmask::r-x\nother::---\ndefault:user::rwx\ndefault:user:palimpsest:r-x\ndefault:user:palimpsest-analysis:r-x\ndefault:group::r-x\ndefault:mask::r-x\ndefault:other::---'
  require_exact_acl "$lane_state_root/lane.lock" $'user::rw-\nuser:palimpsest:rw-\nuser:palimpsest-analysis:rw-\ngroup::r--\nmask::rw-\nother::---'
  require_exact_acl "$lane_state_root/dataset.lock" $'user::rw-\nuser:palimpsest-analysis:rw-\ngroup::r--\nmask::rw-\nother::---'
  for shared_directory in state receipts; do
    shared_path="$lane_state_root/$shared_directory"
    require_exact_acl "$shared_path" $'user::rwx\nuser:palimpsest:rwx\nuser:palimpsest-analysis:rwx\ngroup::r-x\nmask::rwx\nother::---\ndefault:user::rwx\ndefault:user:palimpsest:rwx\ndefault:user:palimpsest-analysis:rwx\ndefault:group::r-x\ndefault:mask::rwx\ndefault:other::---'
  done
}

validate_lane_bundle_permissions() {
  local candidate_bundle="$1"
  local relative_path expected_mode candidate_path
  for relative_path in . ops scripts collectors core config; do
    candidate_path="$candidate_bundle/$relative_path"
    [[ -d "$candidate_path" && ! -L "$candidate_path" \
        && "$(stat -c '%u:%g:%a' "$candidate_path")" == "0:0:755" ]] \
      || die "network-lane bundle directory is unsafe: $relative_path"
  done
  for specification in \
    'README.md:444' \
    'REVISION:444' \
    'MANIFEST.sha256:444' \
    'mirror-config.example.json:444' \
    'network_lane.py:555' \
    'verify-host-bundle.sh:555' \
    'ops/bleedthrough_prober.sh:555' \
    'scripts/bleedthrough_fetch_prefixes.py:444' \
    'scripts/bleedthrough_curate.py:444' \
    'scripts/bleedthrough_pull.py:444' \
    'collectors/__init__.py:444' \
    'collectors/bleedthrough.py:444' \
    'collectors/undertext.py:444' \
    'core/__init__.py:444' \
    'core/claim_support.py:444' \
    'core/governance.py:444' \
    'config/bleedthrough_asns.json:444'; do
    IFS=: read -r relative_path expected_mode <<<"$specification"
    candidate_path="$candidate_bundle/$relative_path"
    [[ -f "$candidate_path" && ! -L "$candidate_path" ]] \
      || die "network-lane bundle file is unsafe: $relative_path"
    [[ "$(stat -c '%u:%g:%a:%h' "$candidate_path")" \
        == "0:0:$expected_mode:1" ]] \
      || die "network-lane bundle ownership/mode/link count is unsafe: $relative_path"
  done
}

install -d -o root -g root -m 0755 "$bundle_root" "$lane_bundle_root"
bundle_tmp="$(mktemp -d "$bundle_root/.bundle-$revision.XXXXXX")"
chown root:root "$bundle_tmp"
chmod 0755 "$bundle_tmp"
lane_bundle_tmp="$(mktemp -d "$lane_bundle_root/.bundle-$revision.XXXXXX")"
chown root:root "$lane_bundle_tmp"
chmod 0755 "$lane_bundle_tmp"
for directory in ops scripts collectors core config; do
  install -d -o root -g root -m 0755 "$lane_bundle_tmp/$directory"
done
unit_stage="$(mktemp -d /run/palimpsest-common-crawl-units.XXXXXX)"
link_tmp="$bundle_root/.current.$$.tmp"
lane_link_tmp="$lane_bundle_root/.current.$$.tmp"
previous_lane_current=""
lane_current_switched=0

cleanup() {
  if (( lane_current_switched == 1 )); then
    if [[ -n "$previous_lane_current" ]]; then
      rollback_link="$lane_bundle_root/.rollback-current.$$.tmp"
      ln -s "$previous_lane_current" "$rollback_link"
      mv -Tf "$rollback_link" "$lane_bundle_root/current"
    else
      rm -f -- "$lane_bundle_root/current"
    fi
    sync -f "$lane_bundle_root"
  fi
  if [[ -n "${duckdb_pin_tmp:-}" && -f "$duckdb_pin_tmp" ]]; then
    rm -- "$duckdb_pin_tmp"
  fi
  if [[ -n "${bundle_tmp:-}" && -d "$bundle_tmp" ]]; then
    rm -rf -- "$bundle_tmp"
  fi
  if [[ -n "${unit_stage:-}" && -d "$unit_stage" ]]; then
    rm -rf -- "$unit_stage"
  fi
  if [[ -n "${lane_bundle_tmp:-}" && -d "$lane_bundle_tmp" ]]; then
    rm -rf -- "$lane_bundle_tmp"
  fi
  if [[ -L "${link_tmp:-}" ]]; then
    rm -- "$link_tmp"
  fi
  if [[ -L "${lane_link_tmp:-}" ]]; then
    rm -- "$lane_link_tmp"
  fi
  cleanup_audit_git
}
trap cleanup EXIT

validate_and_enroll_duckdb

for directory in backup collectors config core processors scripts; do
  install -d -o root -g root -m 0755 "$bundle_tmp/$directory"
done
lane_bundle_files=(
  "ops/network-lane/README.md:README.md:0444"
  "ops/network-lane/mirror-config.example.json:mirror-config.example.json:0444"
  "ops/network-lane/network_lane.py:network_lane.py:0555"
  "ops/network-lane/verify-host-bundle.sh:verify-host-bundle.sh:0555"
  "ops/bleedthrough_prober.sh:ops/bleedthrough_prober.sh:0555"
  "scripts/bleedthrough_fetch_prefixes.py:scripts/bleedthrough_fetch_prefixes.py:0444"
  "scripts/bleedthrough_curate.py:scripts/bleedthrough_curate.py:0444"
  "scripts/bleedthrough_pull.py:scripts/bleedthrough_pull.py:0444"
  "collectors/__init__.py:collectors/__init__.py:0444"
  "collectors/bleedthrough.py:collectors/bleedthrough.py:0444"
  "collectors/undertext.py:collectors/undertext.py:0444"
  "core/__init__.py:core/__init__.py:0444"
  "core/claim_support.py:core/claim_support.py:0444"
  "core/governance.py:core/governance.py:0444"
  "config/bleedthrough_asns.json:config/bleedthrough_asns.json:0444"
)
for specification in "${lane_bundle_files[@]}"; do
  IFS=: read -r source_path destination_path file_mode <<<"$specification"
  install_verified_file \
    "$source_path" "$lane_bundle_tmp/$destination_path" "$file_mode"
done
printf '%s\n' "$revision" >"$lane_bundle_tmp/REVISION"
chown root:root "$lane_bundle_tmp/REVISION"
chmod 0444 "$lane_bundle_tmp/REVISION"
(
  cd "$lane_bundle_tmp"
  sha256sum \
    README.md REVISION mirror-config.example.json network_lane.py \
    verify-host-bundle.sh ops/bleedthrough_prober.sh \
    scripts/bleedthrough_fetch_prefixes.py \
    scripts/bleedthrough_curate.py scripts/bleedthrough_pull.py \
    collectors/__init__.py collectors/bleedthrough.py collectors/undertext.py \
    core/__init__.py core/claim_support.py core/governance.py \
    config/bleedthrough_asns.json >MANIFEST.sha256
)
chown root:root "$lane_bundle_tmp/MANIFEST.sha256"
chmod 0444 "$lane_bundle_tmp/MANIFEST.sha256"
(cd "$lane_bundle_tmp" && sha256sum --quiet --check MANIFEST.sha256) \
  || die "staged network-lane bundle failed validation"
validate_lane_bundle_permissions "$lane_bundle_tmp"
sync -f "$lane_bundle_tmp"
bundle_files=(
  "ops/common-crawl/README.md:README.md:0444"
  "ops/backup/COMMON-CRAWL-OFFSITE.md:backup/README.md:0444"
  "ops/backup/common_crawl_backup.py:backup/common_crawl_backup.py:0555"
  "ops/backup/palimpsest-common-crawl-offsite-backup.sh:backup/palimpsest-common-crawl-offsite-backup.sh:0555"
  "collectors/__init__.py:collectors/__init__.py:0444"
  "collectors/common_crawl_lake.py:collectors/common_crawl_lake.py:0444"
  "config/common_crawl_targets.json:config/common_crawl_targets.json:0444"
  "core/__init__.py:core/__init__.py:0444"
  "core/governance.py:core/governance.py:0444"
  "core/safe_fetch.py:core/safe_fetch.py:0444"
  "processors/__init__.py:processors/__init__.py:0444"
  "processors/archive_context.py:processors/archive_context.py:0444"
  "processors/editorial_priority.py:processors/editorial_priority.py:0444"
  "scripts/common_crawl_lake.py:scripts/common_crawl_lake.py:0444"
  "ops/common-crawl/verify-host-bundle.sh:verify-host-bundle.sh:0555"
  "ops/common-crawl/run_duckdb_filter.py:run_duckdb_filter.py:0555"
)
for specification in "${bundle_files[@]}"; do
  IFS=: read -r source_path destination_path file_mode <<<"$specification"
  install -o root -g root -m "$file_mode" \
    "$repo_root/$source_path" "$bundle_tmp/$destination_path"
  verify_git_blob "$source_path" "$bundle_tmp/$destination_path"
done
printf '%s\n' "$revision" >"$bundle_tmp/REVISION"
chown root:root "$bundle_tmp/REVISION"
chmod 0444 "$bundle_tmp/REVISION"
(
  cd "$bundle_tmp"
  sha256sum \
    README.md REVISION backup/README.md backup/common_crawl_backup.py \
    backup/palimpsest-common-crawl-offsite-backup.sh \
    collectors/__init__.py collectors/common_crawl_lake.py \
    config/common_crawl_targets.json \
    core/__init__.py core/governance.py core/safe_fetch.py \
    processors/__init__.py processors/archive_context.py \
    processors/editorial_priority.py scripts/common_crawl_lake.py \
    run_duckdb_filter.py verify-host-bundle.sh >MANIFEST.sha256
)
chown root:root "$bundle_tmp/MANIFEST.sha256"
chmod 0444 "$bundle_tmp/MANIFEST.sha256"
(cd "$bundle_tmp" && sha256sum --quiet --check MANIFEST.sha256) \
  || die "staged Common Crawl bundle failed validation"
sync -f "$bundle_tmp"

final_revision="$(cat "$repo_root/.git/HEAD")"
checkout_status="$(
  safe_git status --porcelain=v1 --untracked-files=all
)"
[[ "$final_revision" == "$revision" && -z "$checkout_status" ]] \
  || die "checkout changed while the bundle was staged"
IFS= read -r deployed_revision <"$receipt_path"
[[ "$deployed_revision" == "$revision" ]] \
  || die "deployed receipt changed while the bundle was staged"

bundle_final="$bundle_root/$revision"
if [[ -e "$bundle_final" ]]; then
  [[ -d "$bundle_final" && ! -L "$bundle_final" ]] \
    || die "existing revision bundle is unsafe"
  [[ "$(stat -c '%u:%g:%a' "$bundle_final")" == "0:0:755" ]] \
    || die "existing revision bundle ownership or mode is unsafe"
  cmp -s "$bundle_tmp/MANIFEST.sha256" "$bundle_final/MANIFEST.sha256" \
    || die "existing revision bundle has different contents"
  (cd "$bundle_final" && sha256sum --quiet --check MANIFEST.sha256) \
    || die "existing revision bundle failed validation"
else
  mv -T "$bundle_tmp" "$bundle_final"
  bundle_tmp=""
  sync -f "$bundle_root"
fi
[[ -f "$bundle_final/run_duckdb_filter.py" \
    && ! -L "$bundle_final/run_duckdb_filter.py" \
    && "$(stat -c '%u:%g:%a:%h' "$bundle_final/run_duckdb_filter.py")" \
      == "0:0:555:1" ]] \
  || die "Common Crawl DuckDB runner ownership/mode/link count is unsafe"

lane_bundle_final="$lane_bundle_root/$revision"
if [[ -e "$lane_bundle_final" ]]; then
  [[ -d "$lane_bundle_final" && ! -L "$lane_bundle_final" ]] \
    || die "existing network-lane revision bundle is unsafe"
  [[ "$(stat -c '%u:%g:%a' "$lane_bundle_final")" == "0:0:755" ]] \
    || die "existing network-lane bundle ownership or mode is unsafe"
  validate_lane_bundle_permissions "$lane_bundle_final"
  cmp -s "$lane_bundle_tmp/MANIFEST.sha256" "$lane_bundle_final/MANIFEST.sha256" \
    || die "existing network-lane revision has different contents"
  (cd "$lane_bundle_final" && sha256sum --quiet --check MANIFEST.sha256) \
    || die "existing network-lane revision bundle failed validation"
else
  mv -T "$lane_bundle_tmp" "$lane_bundle_final"
  lane_bundle_tmp=""
  sync -f "$lane_bundle_root"
fi
validate_lane_bundle_permissions "$lane_bundle_final"

# Both network units remain disabled/stopped. Publish the validated helper now
# so first-install systemd verification can resolve its absolute ExecStart path.
if [[ -e "$lane_bundle_root/current" || -L "$lane_bundle_root/current" ]]; then
  [[ -L "$lane_bundle_root/current" ]] \
    || die "current network-lane path must be a revision symlink"
  previous_lane_current="$(readlink "$lane_bundle_root/current")"
  [[ "$previous_lane_current" =~ ^[0-9a-f]{40}$ ]] \
    || die "existing network-lane current target is malformed"
fi
ln -s "$revision" "$lane_link_tmp"
mv -Tf "$lane_link_tmp" "$lane_bundle_root/current"
lane_current_switched=1
[[ "$(readlink "$lane_bundle_root/current")" == "$revision" ]] \
  || die "current network-lane bundle link is invalid"
sync -f "$lane_bundle_root"

mount_unit="$(systemd-escape --path --suffix=mount "$state_root")"
[[ "$mount_unit" == 'var-lib-palimpsest-common\x2dcrawl.mount' ]] \
  || die "unexpected systemd mount unit name: $mount_unit"
sed "s|@WAREHOUSE_SOURCE@|$warehouse_source|g" \
  "$mount_template" >"$unit_stage/$mount_unit"
for unit_name in "${service_units[@]}" "${backup_units[@]}"; do
  install -m 0644 "$repo_root/ops/systemd/$unit_name" "$unit_stage/$unit_name"
  verify_git_blob "ops/systemd/$unit_name" "$unit_stage/$unit_name"
done
for unit_name in "${network_units[@]}"; do
  install -m 0644 "$repo_root/ops/systemd/$unit_name" "$unit_stage/$unit_name"
  verify_git_blob "ops/systemd/$unit_name" "$unit_stage/$unit_name"
done
install -m 0644 "$repo_root/ops/systemd/palimpsest-network-lane.tmpfiles.conf" \
  "$unit_stage/palimpsest-network-lane.tmpfiles.conf"
verify_git_blob \
  "ops/systemd/palimpsest-network-lane.tmpfiles.conf" \
  "$unit_stage/palimpsest-network-lane.tmpfiles.conf"
osint_provider_unit='/etc/systemd/system/palimpsest-public-osint-sync.service'
[[ -f "$osint_provider_unit" && ! -L "$osint_provider_unit" \
    && "$(stat -c '%u:%g:%a:%h' "$osint_provider_unit")" == '0:0:644:1' ]] \
  || die "the required public OSINT provider unit is not safely installed"
unit_paths=("$osint_provider_unit" "$unit_stage/$mount_unit")
for unit_name in "${service_units[@]}" "${backup_units[@]}" "${network_units[@]}"; do
  unit_paths+=("$unit_stage/$unit_name")
done
systemd-analyze verify "${unit_paths[@]}"

final_revision="$(cat "$repo_root/.git/HEAD")"
checkout_status="$(
  safe_git status --porcelain=v1 --untracked-files=all
)"
[[ "$final_revision" == "$revision" && -z "$checkout_status" ]] \
  || die "checkout changed while systemd units were validated"
IFS= read -r deployed_revision <"$receipt_path"
[[ "$deployed_revision" == "$revision" ]] \
  || die "deployed receipt changed while systemd units were validated"

install -o root -g root -m 0644 \
  "$unit_stage/$mount_unit" "/etc/systemd/system/$mount_unit"
for unit_name in "${service_units[@]}" "${backup_units[@]}"; do
  install -o root -g root -m 0644 \
    "$unit_stage/$unit_name" "/etc/systemd/system/$unit_name"
  verify_git_blob "ops/systemd/$unit_name" "/etc/systemd/system/$unit_name"
done
for unit_name in "${network_units[@]}"; do
  install -o root -g root -m 0644 \
    "$unit_stage/$unit_name" "/etc/systemd/system/$unit_name"
  verify_git_blob "ops/systemd/$unit_name" "/etc/systemd/system/$unit_name"
done
install -o root -g root -m 0644 \
  "$unit_stage/palimpsest-network-lane.tmpfiles.conf" \
  "/etc/tmpfiles.d/palimpsest-network-lane.conf"
verify_git_blob \
  "ops/systemd/palimpsest-network-lane.tmpfiles.conf" \
  "/etc/tmpfiles.d/palimpsest-network-lane.conf"
systemd-tmpfiles --create /etc/tmpfiles.d/palimpsest-network-lane.conf
validate_network_lane_state
cmp -s "$unit_stage/$mount_unit" "/etc/systemd/system/$mount_unit" \
  || die "installed mount unit differs from its validated rendering"

systemctl daemon-reload
systemctl enable --now "$mount_unit"
mountpoint -q "$state_root" || die "stable warehouse path is not a mountpoint"
[[ "$(stat -c '%d:%i' "$warehouse_source")" == "$(stat -c '%d:%i' "$state_root")" ]] \
  || die "stable warehouse bind does not resolve to the requested source"

ln -s "$revision" "$link_tmp"
mv -Tf "$link_tmp" "$bundle_root/current"
[[ "$(readlink "$bundle_root/current")" == "$revision" ]] \
  || die "current Common Crawl bundle link is invalid"
sync -f "$bundle_root"
lane_current_switched=0

printf 'installed Palimpsest Common Crawl bundle %s at %s\n' \
  "$revision" "$warehouse_source"
printf '%s\n' \
  'BLEEDTHROUGH remains stopped; no Common Crawl mirror was enabled or started.'
