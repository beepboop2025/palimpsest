#!/usr/bin/env bash
# Install the exact checked-out public OSINT sync runtime as an immutable bundle.

set -Eeuo pipefail

die() {
  printf 'palimpsest-public-osint-sync install: %s\n' "$*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || die "run this installer as root"
(( $# == 0 )) || die "usage: $0"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/../.." && pwd -P)"
bundle_root='/usr/local/libexec/palimpsest-public-osint-sync'
receipt_path='/etc/palimpsest/deployed-commit'
service_name='palimpsest-public-osint-sync.service'
timer_name='palimpsest-public-osint-sync.timer'

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE

for command_name in cat chmod chown cmp git install ln mktemp mv readlink rm \
    sha256sum stat sync systemctl systemd-analyze; do
  command -v "$command_name" >/dev/null 2>&1 \
    || die "required command is missing: $command_name"
done

revision="$(git -c "safe.directory=$repo_root" -C "$repo_root" \
  rev-parse --verify HEAD 2>/dev/null)" \
  || die "cannot resolve the checked-out Git revision"
checkout_status="$(git -c "safe.directory=$repo_root" -C "$repo_root" \
  status --porcelain=v1 --untracked-files=all 2>/dev/null)" \
  || die "cannot verify the Git checkout"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die "Git returned a malformed revision"
[[ -z "$checkout_status" ]] || die "checkout has modified or untracked files"
[[ -f "$receipt_path" && ! -L "$receipt_path" ]] \
  || die "the deployed receipt is missing or unsafe"
[[ "$(stat -c '%u:%g:%a:%h' "$receipt_path")" == '0:0:644:1' ]] \
  || die "the deployed receipt ownership or mode is unsafe"
[[ "$(cat "$receipt_path")" == "$revision" ]] \
  || die "the deployed receipt does not match Git HEAD"

verify_git_blob() {
  git -c "safe.directory=$repo_root" -C "$repo_root" \
    show "$revision:$1" | cmp -s - "$2" \
    || die "installed bytes do not match Git HEAD: $1"
}

for unit_name in "$timer_name" "$service_name"; do
  enablement="$(systemctl is-enabled "$unit_name" 2>/dev/null || true)"
  case "$enablement" in
    masked|masked-runtime) die "$unit_name is masked" ;;
  esac
  active_state="$(systemctl show --property=ActiveState --value \
    "$unit_name" 2>/dev/null || true)"
  case "$active_state" in
    ''|inactive|failed) ;;
    *) die "$unit_name must be stopped before installation" ;;
  esac
done

if [[ -e "$bundle_root" || -L "$bundle_root" ]]; then
  [[ -d "$bundle_root" && ! -L "$bundle_root" \
      && "$(stat -c '%u:%g:%a' "$bundle_root")" == '0:0:755' ]] \
    || die "bundle root ownership, mode, or type is unsafe"
else
  install -d -o root -g root -m 0755 "$bundle_root"
fi
bundle_tmp="$(mktemp -d "$bundle_root/.bundle-$revision.XXXXXX")"
chown root:root "$bundle_tmp"
chmod 0755 "$bundle_tmp"
link_tmp="$bundle_root/.current.$$.tmp"
current_path="$bundle_root/current"
validation_current_added=0
cleanup() {
  if [[ -n "${bundle_tmp:-}" && -d "$bundle_tmp" ]]; then
    rm -rf -- "$bundle_tmp"
  fi
  if [[ -L "$link_tmp" ]]; then
    rm -- "$link_tmp"
  fi
  if (( validation_current_added == 1 )) \
      && [[ -L "$current_path" ]] \
      && [[ "$(readlink "$current_path")" == "$revision" ]]; then
    rm -- "$current_path"
  fi
}
trap cleanup EXIT

install -o root -g root -m 0555 \
  "$repo_root/ops/osint-sync/public_osint_sync.py" \
  "$bundle_tmp/public_osint_sync.py"
install -o root -g root -m 0555 \
  "$repo_root/ops/osint-sync/verify-host-bundle.sh" \
  "$bundle_tmp/verify-host-bundle.sh"
install -o root -g root -m 0444 \
  "$repo_root/ops/osint-sync/README.md" "$bundle_tmp/README.md"
verify_git_blob ops/osint-sync/public_osint_sync.py \
  "$bundle_tmp/public_osint_sync.py"
verify_git_blob ops/osint-sync/verify-host-bundle.sh \
  "$bundle_tmp/verify-host-bundle.sh"
verify_git_blob ops/osint-sync/README.md "$bundle_tmp/README.md"
printf '%s\n' "$revision" >"$bundle_tmp/REVISION"
chown root:root "$bundle_tmp/REVISION"
chmod 0444 "$bundle_tmp/REVISION"
(
  cd "$bundle_tmp"
  sha256sum README.md REVISION public_osint_sync.py verify-host-bundle.sh \
    >MANIFEST.sha256
)
chown root:root "$bundle_tmp/MANIFEST.sha256"
chmod 0444 "$bundle_tmp/MANIFEST.sha256"
sync -f "$bundle_tmp"

final_revision="$(git -c "safe.directory=$repo_root" -C "$repo_root" \
  rev-parse --verify HEAD 2>/dev/null)" \
  || die "cannot re-resolve Git HEAD"
final_status="$(git -c "safe.directory=$repo_root" -C "$repo_root" \
  status --porcelain=v1 --untracked-files=all 2>/dev/null)" \
  || die "cannot re-verify the Git checkout"
[[ "$final_revision" == "$revision" && -z "$final_status" ]] \
  || die "checkout changed while the bundle was staged"

bundle_final="$bundle_root/$revision"
if [[ -e "$bundle_final" || -L "$bundle_final" ]]; then
  [[ -d "$bundle_final" && ! -L "$bundle_final" \
      && "$(stat -c '%u:%g:%a' "$bundle_final")" == '0:0:755' ]] \
    || die "existing bundle directory is unsafe"
  for bundle_file in README.md REVISION MANIFEST.sha256 \
      public_osint_sync.py verify-host-bundle.sh; do
    [[ -f "$bundle_final/$bundle_file" \
        && ! -L "$bundle_final/$bundle_file" ]] \
      || die "existing bundle file is unsafe: $bundle_file"
    expected_mode=444
    [[ "$bundle_file" == public_osint_sync.py \
        || "$bundle_file" == verify-host-bundle.sh ]] \
      && expected_mode=555
    [[ "$(stat -c '%u:%g:%a' "$bundle_final/$bundle_file")" \
        == "0:0:$expected_mode" ]] \
      || die "existing bundle ownership or mode is unsafe: $bundle_file"
  done
  cmp -s "$bundle_tmp/MANIFEST.sha256" "$bundle_final/MANIFEST.sha256" \
    || die "existing bundle for this revision differs"
  "$bundle_final/verify-host-bundle.sh" \
    || die "existing bundle failed verification"
else
  mv -T "$bundle_tmp" "$bundle_final"
  bundle_tmp=''
  sync -f "$bundle_root"
fi

# systemd-analyze resolves executable paths on the host. On a first install,
# expose the already verified bundle only for validation while both units are
# stopped, then remove that temporary selector before installing either unit.
if [[ -e "$current_path" || -L "$current_path" ]]; then
  [[ -L "$current_path" ]] || die "current bundle selector is unsafe"
  current_revision="$(readlink "$current_path")"
  [[ "$current_revision" =~ ^[0-9a-f]{40}$ \
      && -d "$bundle_root/$current_revision" \
      && ! -L "$bundle_root/$current_revision" ]] \
    || die "current bundle selector target is unsafe"
else
  ln -s "$revision" "$current_path"
  validation_current_added=1
fi
systemd-analyze verify \
  "$repo_root/ops/systemd/$service_name" \
  "$repo_root/ops/systemd/$timer_name"
if (( validation_current_added == 1 )); then
  [[ -L "$current_path" && "$(readlink "$current_path")" == "$revision" ]] \
    || die "temporary validation selector changed"
  rm -- "$current_path"
  validation_current_added=0
  sync -f "$bundle_root"
fi

for unit_name in "$service_name" "$timer_name"; do
  install -o root -g root -m 0644 \
    "$repo_root/ops/systemd/$unit_name" "/etc/systemd/system/$unit_name"
  verify_git_blob "ops/systemd/$unit_name" "/etc/systemd/system/$unit_name"
  [[ "$(stat -c '%u:%g:%a' "/etc/systemd/system/$unit_name")" == '0:0:644' ]] \
    || die "installed unit ownership or mode is unsafe: $unit_name"
done
systemctl daemon-reload

ln -s "$revision" "$link_tmp"
mv -Tf "$link_tmp" "$bundle_root/current"
[[ "$(readlink "$bundle_root/current")" == "$revision" ]] \
  || die "current bundle link does not name the staged revision"
"$bundle_root/current/verify-host-bundle.sh"
cmp -s "$bundle_root/current/REVISION" "$receipt_path" \
  || die "sync bundle and deployed receipt differ"
sync -f "$bundle_root"

printf 'installed Palimpsest public OSINT sync bundle %s\n' "$revision"
