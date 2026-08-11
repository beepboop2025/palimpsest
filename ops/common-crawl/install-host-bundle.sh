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
receipt_path="/etc/palimpsest/deployed-commit"
mount_template="$script_dir/palimpsest-common-crawl.mount.in"
minimum_initial_free_bytes=$((256 * 1024 * 1024 * 1024))
service_units=(
  palimpsest-common-crawl-import.service
  palimpsest-common-crawl-import.path
  palimpsest-common-crawl-context.service
  palimpsest-common-crawl-context.timer
)

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE

for command_name in awk bash chmod chown cmp df dirname find findmnt getent git \
  install ln mktemp mountpoint mv readlink realpath rm sed sha256sum stat sync \
  systemctl systemd-analyze systemd-escape; do
  command -v "$command_name" >/dev/null 2>&1 \
    || die "required command is missing: $command_name"
done

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

if ! revision="$(
  git -c "safe.directory=$repo_root" -C "$repo_root" rev-parse --verify HEAD 2>/dev/null
)"; then
  die "cannot resolve the checked-out Git revision"
fi
checkout_status="$(
  git -c "safe.directory=$repo_root" -C "$repo_root" \
    status --porcelain=v1 --untracked-files=all 2>/dev/null
)" || die "cannot verify that the Git checkout is clean"
[[ "$revision" =~ ^[0-9a-f]{40}$ && -z "$checkout_status" ]] \
  || die "checkout is dirty or has a malformed revision"
[[ -r "$receipt_path" ]] || die "deployed commit receipt is missing"
IFS= read -r deployed_revision <"$receipt_path" \
  || die "cannot read deployed commit receipt"
[[ "$deployed_revision" == "$revision" ]] \
  || die "deployed commit receipt does not match Git HEAD"

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
  git -c "safe.directory=$repo_root" -C "$repo_root" \
    show "$revision:$repository_path" \
    | cmp -s - "$installed_path" \
    || die "installed bytes do not match Git HEAD: $repository_path"
}

install -d -o root -g root -m 0755 "$bundle_root"
bundle_tmp="$(mktemp -d "$bundle_root/.bundle-$revision.XXXXXX")"
unit_stage="$(mktemp -d /run/palimpsest-common-crawl-units.XXXXXX)"
link_tmp="$bundle_root/.current.$$.tmp"

cleanup() {
  if [[ -n "${bundle_tmp:-}" && -d "$bundle_tmp" ]]; then
    rm -rf -- "$bundle_tmp"
  fi
  if [[ -n "${unit_stage:-}" && -d "$unit_stage" ]]; then
    rm -rf -- "$unit_stage"
  fi
  if [[ -L "${link_tmp:-}" ]]; then
    rm -- "$link_tmp"
  fi
}
trap cleanup EXIT

for directory in collectors config core processors scripts; do
  install -d -o root -g root -m 0755 "$bundle_tmp/$directory"
done
bundle_files=(
  "ops/common-crawl/README.md:README.md:0444"
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
    README.md REVISION \
    collectors/__init__.py collectors/common_crawl_lake.py \
    config/common_crawl_targets.json \
    core/__init__.py core/governance.py core/safe_fetch.py \
    processors/__init__.py processors/archive_context.py \
    processors/editorial_priority.py scripts/common_crawl_lake.py \
    verify-host-bundle.sh >MANIFEST.sha256
)
chown root:root "$bundle_tmp/MANIFEST.sha256"
chmod 0444 "$bundle_tmp/MANIFEST.sha256"
(cd "$bundle_tmp" && sha256sum --quiet --check MANIFEST.sha256) \
  || die "staged Common Crawl bundle failed validation"
sync -f "$bundle_tmp"

final_revision="$(
  git -c "safe.directory=$repo_root" -C "$repo_root" rev-parse --verify HEAD
)"
checkout_status="$(
  git -c "safe.directory=$repo_root" -C "$repo_root" \
    status --porcelain=v1 --untracked-files=all
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
  cmp -s "$bundle_tmp/MANIFEST.sha256" "$bundle_final/MANIFEST.sha256" \
    || die "existing revision bundle has different contents"
  (cd "$bundle_final" && sha256sum --quiet --check MANIFEST.sha256) \
    || die "existing revision bundle failed validation"
else
  mv -T "$bundle_tmp" "$bundle_final"
  bundle_tmp=""
  sync -f "$bundle_root"
fi

mount_unit="$(systemd-escape --path --suffix=mount "$state_root")"
[[ "$mount_unit" == 'var-lib-palimpsest-common\x2dcrawl.mount' ]] \
  || die "unexpected systemd mount unit name: $mount_unit"
sed "s|@WAREHOUSE_SOURCE@|$warehouse_source|g" \
  "$mount_template" >"$unit_stage/$mount_unit"
for unit_name in "${service_units[@]}"; do
  install -m 0644 "$repo_root/ops/systemd/$unit_name" "$unit_stage/$unit_name"
done
unit_paths=("$unit_stage/$mount_unit")
for unit_name in "${service_units[@]}"; do
  unit_paths+=("$unit_stage/$unit_name")
done
systemd-analyze verify "${unit_paths[@]}"

install -o root -g root -m 0644 \
  "$unit_stage/$mount_unit" "/etc/systemd/system/$mount_unit"
for unit_name in "${service_units[@]}"; do
  install -o root -g root -m 0644 \
    "$unit_stage/$unit_name" "/etc/systemd/system/$unit_name"
  verify_git_blob "ops/systemd/$unit_name" "/etc/systemd/system/$unit_name"
done
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

printf 'installed Palimpsest Common Crawl bundle %s at %s\n' \
  "$revision" "$warehouse_source"
