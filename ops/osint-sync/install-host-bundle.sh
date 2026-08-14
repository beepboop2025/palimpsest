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
release_mode_repository_path='ops/osint-sync/release-mode'
compatibility_dropin_repository_path='ops/systemd/palimpsest-public-osint-sync.compatibility.conf'
compatibility_dropin_dir='/etc/systemd/system/palimpsest-public-osint-sync.service.d'
compatibility_dropin_target="$compatibility_dropin_dir/10-compatibility-mirror.conf"

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE

for command_name in cat chmod chown cmp find grep install ln mktemp mv readlink rm \
    sha256sum stat sync systemctl systemd-analyze; do
  command -v "$command_name" >/dev/null 2>&1 \
    || die "required command is missing: $command_name"
done

[[ -x /usr/bin/git && ! -L /usr/bin/git ]] \
  || die "the pinned Git executable is missing or unsafe"
[[ -d "$repo_root/.git" && ! -L "$repo_root/.git" \
    && -d "$repo_root/.git/objects" && ! -L "$repo_root/.git/objects" \
    && -f "$repo_root/.git/index" && ! -L "$repo_root/.git/index" ]] \
  || die "the deployment checkout Git metadata is unsafe"
[[ ! -e "$repo_root/.git/info/grafts" \
    && ! -L "$repo_root/.git/info/grafts" ]] \
  || die "legacy Git grafts are forbidden"
[[ ! -e "$repo_root/.git/objects/info/alternates" \
    && ! -L "$repo_root/.git/objects/info/alternates" ]] \
  || die "source Git object alternates are forbidden"
if [[ -e "$repo_root/.git/refs/replace" \
    || -L "$repo_root/.git/refs/replace" ]]; then
  [[ -d "$repo_root/.git/refs/replace" \
      && ! -L "$repo_root/.git/refs/replace" ]] \
    || die "Git replacement refs path is unsafe"
  if [[ -n "$(find "$repo_root/.git/refs/replace" \
      -mindepth 1 -print -quit)" ]]; then
    die "Git replacement refs are forbidden"
  fi
fi
[[ ! -L "$repo_root/.git/packed-refs" ]] \
  || die "packed Git refs path is unsafe"
if [[ -f "$repo_root/.git/packed-refs" ]] \
    && grep -Eq '[[:space:]]refs/replace/' "$repo_root/.git/packed-refs"; then
  die "packed Git replacement refs are forbidden"
fi

export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_SYSTEM=/dev/null
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_NO_REPLACE_OBJECTS=1
export GIT_NO_LAZY_FETCH=1
export GIT_TERMINAL_PROMPT=0
export GIT_PROTOCOL_FROM_USER=0
audit_git="$(mktemp -d /run/palimpsest-osint-git.XXXXXX)"
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
    -c protocol.file.allow=never "$@"
}

revision="$(cat "$repo_root/.git/HEAD")" \
  || die "cannot read the detached Git revision"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die "Git returned a malformed revision"
printf '%s\n' "$revision" >"$audit_git/HEAD"
checkout_status="$(safe_git status --porcelain=v1 --untracked-files=all 2>/dev/null)" \
  || die "cannot verify the Git checkout through the isolated audit view"
[[ -z "$checkout_status" ]] || die "checkout has modified or untracked files"
release_mode="$(safe_git show "$revision:$release_mode_repository_path")" \
  || die "cannot read the release mode from Git"
case "$release_mode" in
  legacy-mirror|protected-only) ;;
  *) die "release mode is invalid" ;;
esac
[[ -f "$receipt_path" && ! -L "$receipt_path" ]] \
  || die "the deployed receipt is missing or unsafe"
[[ "$(stat -c '%u:%g:%a:%h' "$receipt_path")" == '0:0:644:1' ]] \
  || die "the deployed receipt ownership or mode is unsafe"
[[ "$(cat "$receipt_path")" == "$revision" ]] \
  || die "the deployed receipt does not match Git HEAD"

verify_git_blob() {
  safe_git show "$revision:$1" | cmp -s - "$2" \
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
  if [[ -n "${audit_git:-}" && -d "$audit_git" ]]; then
    rm -rf -- "$audit_git"
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
install -o root -g root -m 0444 \
  "$repo_root/$release_mode_repository_path" "$bundle_tmp/release-mode"
install -o root -g root -m 0444 \
  "$repo_root/$compatibility_dropin_repository_path" \
  "$bundle_tmp/compatibility-mirror.conf"
verify_git_blob ops/osint-sync/public_osint_sync.py \
  "$bundle_tmp/public_osint_sync.py"
verify_git_blob ops/osint-sync/verify-host-bundle.sh \
  "$bundle_tmp/verify-host-bundle.sh"
verify_git_blob ops/osint-sync/README.md "$bundle_tmp/README.md"
verify_git_blob "$release_mode_repository_path" "$bundle_tmp/release-mode"
verify_git_blob "$compatibility_dropin_repository_path" \
  "$bundle_tmp/compatibility-mirror.conf"
[[ "$(cat "$bundle_tmp/release-mode")" == "$release_mode" ]] \
  || die "staged release mode differs from Git"
printf '%s\n' "$revision" >"$bundle_tmp/REVISION"
chown root:root "$bundle_tmp/REVISION"
chmod 0444 "$bundle_tmp/REVISION"
(
  cd "$bundle_tmp"
  sha256sum README.md REVISION compatibility-mirror.conf public_osint_sync.py \
    release-mode verify-host-bundle.sh >MANIFEST.sha256
)
chown root:root "$bundle_tmp/MANIFEST.sha256"
chmod 0444 "$bundle_tmp/MANIFEST.sha256"
sync -f "$bundle_tmp"

final_revision="$(cat "$repo_root/.git/HEAD")" \
  || die "cannot re-read the detached Git revision"
final_status="$(safe_git status --porcelain=v1 --untracked-files=all 2>/dev/null)" \
  || die "cannot re-verify the Git checkout"
[[ "$final_revision" == "$revision" && -z "$final_status" ]] \
  || die "checkout changed while the bundle was staged"

bundle_final="$bundle_root/$revision"
if [[ -e "$bundle_final" || -L "$bundle_final" ]]; then
  [[ -d "$bundle_final" && ! -L "$bundle_final" \
      && "$(stat -c '%u:%g:%a' "$bundle_final")" == '0:0:755' ]] \
    || die "existing bundle directory is unsafe"
  for bundle_file in README.md REVISION MANIFEST.sha256 \
      compatibility-mirror.conf public_osint_sync.py release-mode \
      verify-host-bundle.sh; do
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

# The C0 compatibility commit deliberately mirrors the verified bytes into the
# legacy reading paths. The C1 commit removes only that exact reviewed drop-in
# before any requiring consumer is installed. An unknown local override is not
# deleted or overwritten.
if [[ "$release_mode" == legacy-mirror ]]; then
  install -d -o root -g root -m 0755 "$compatibility_dropin_dir"
  install -o root -g root -m 0644 \
    "$repo_root/$compatibility_dropin_repository_path" \
    "$compatibility_dropin_target"
  verify_git_blob "$compatibility_dropin_repository_path" \
    "$compatibility_dropin_target"
  [[ "$(stat -c '%u:%g:%a:%h' "$compatibility_dropin_target")" \
      == '0:0:644:1' ]] \
    || die "installed compatibility drop-in is unsafe"
else
  if [[ -e "$compatibility_dropin_target" \
      || -L "$compatibility_dropin_target" ]]; then
    [[ -f "$compatibility_dropin_target" \
        && ! -L "$compatibility_dropin_target" \
        && "$(stat -c '%u:%g:%a:%h' "$compatibility_dropin_target")" \
          == '0:0:644:1' ]] \
      || die "installed compatibility drop-in is unsafe"
    verify_git_blob "$compatibility_dropin_repository_path" \
      "$compatibility_dropin_target"
    rm -- "$compatibility_dropin_target"
  fi
  [[ ! -e "$compatibility_dropin_target" \
      && ! -L "$compatibility_dropin_target" ]] \
    || die "compatibility drop-in was not removed"
fi
systemctl daemon-reload
systemd-analyze verify \
  "/etc/systemd/system/$service_name" \
  "/etc/systemd/system/$timer_name"

ln -s "$revision" "$link_tmp"
mv -Tf "$link_tmp" "$bundle_root/current"
[[ "$(readlink "$bundle_root/current")" == "$revision" ]] \
  || die "current bundle link does not name the staged revision"
"$bundle_root/current/verify-host-bundle.sh"
cmp -s "$bundle_root/current/REVISION" "$receipt_path" \
  || die "sync bundle and deployed receipt differ"
sync -f "$bundle_root"

printf 'installed Palimpsest public OSINT sync bundle %s\n' "$revision"
