#!/usr/bin/env bash
# Install the immutable host runner and atomically certify one deployed commit.

set -Eeuo pipefail

die() {
  printf 'palimpsest-analysis install: %s\n' "$*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || die "run this installer as root"
(( $# <= 1 )) || die "usage: $0 [image-reference]"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/../.." && pwd -P)"
image_ref="${1:-palimpsest/app:local}"
[[ -n "$image_ref" && "$image_ref" != -* && "$image_ref" != *[[:space:]]* ]] \
  || die "image reference is empty or malformed"
bundle_root="/usr/local/libexec/palimpsest-analysis"
receipt_dir="/etc/palimpsest"
receipt_path="$receipt_dir/deployed-commit"
service_name="palimpsest-investigative-analysis.service"
timer_name="palimpsest-investigative-analysis.timer"

# Prevent an invoking shell from redirecting Git's object/index/worktree view
# away from the checkout whose files this installer copies.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST="unix:///var/run/docker.sock"

for command_name in chmod chown cmp docker git install ln mktemp mv readlink rm \
  sha256sum stat sync systemctl systemd-analyze; do
  command -v "$command_name" >/dev/null 2>&1 \
    || die "required command is missing: $command_name"
done

if ! revision="$(
  git -c "safe.directory=$repo_root" -C "$repo_root" \
    rev-parse --verify HEAD 2>/dev/null
)"; then
  die "cannot resolve the checked-out Git revision"
fi
if ! checkout_status="$(
  git -c "safe.directory=$repo_root" -C "$repo_root" \
    status --porcelain=v1 --untracked-files=all 2>/dev/null
)"; then
  die "cannot verify that the Git checkout is clean"
fi
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die "Git returned a malformed revision"
[[ -z "$checkout_status" ]] || die "checkout has modified or untracked files"

verify_git_blob() {
  local repository_path="$1"
  local installed_path="$2"
  git -c "safe.directory=$repo_root" -C "$repo_root" \
    show "$revision:$repository_path" \
    | cmp -s - "$installed_path" \
    || die "installed bytes do not match Git HEAD: $repository_path"
}

# Deploys are serialized outside the recurring job.  A stopped timer plus an
# inactive oneshot prevents the current symlink and receipt changing underneath
# a run.  Unknown/systemd-error states fail closed instead of being treated as
# inactive.
for unit_name in "$timer_name" "$service_name"; do
  if ! unit_state="$(
    systemctl show --property=ActiveState --value "$unit_name" 2>/dev/null
  )"; then
    die "cannot verify systemd state for $unit_name"
  fi
  case "$unit_state" in
    inactive|failed) ;;
    active|activating|deactivating|reloading)
      die "$unit_name must be stopped before installation"
      ;;
    *) die "unexpected systemd state for $unit_name: $unit_state" ;;
  esac
done

if ! image_metadata="$(
  docker image inspect --format \
    '{{index .Config.Labels "org.opencontainers.image.revision"}} {{.Id}}' \
    "$image_ref" 2>/dev/null
)"; then
  die "cannot inspect image: $image_ref"
fi
read -r image_revision image_id extra <<<"$image_metadata"
[[ -z "${extra:-}" ]] || die "image inspection returned unexpected fields"
[[ "$image_revision" == "$revision" ]] \
  || die "image revision does not match checked-out HEAD"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "image inspection returned a malformed immutable ID"

systemd-analyze verify \
  "$repo_root/ops/systemd/$service_name" \
  "$repo_root/ops/systemd/$timer_name"

install -d -o root -g root -m 0755 "$bundle_root" "$receipt_dir"
bundle_tmp="$(mktemp -d "$bundle_root/.bundle-$revision.XXXXXX")"
chown root:root "$bundle_tmp"
chmod 0755 "$bundle_tmp"
link_tmp="$bundle_root/.current.$$.tmp"
receipt_tmp=""

cleanup() {
  if [[ -n "${bundle_tmp:-}" && -d "$bundle_tmp" ]]; then
    rm -rf -- "$bundle_tmp"
  fi
  if [[ -L "${link_tmp:-}" ]]; then
    rm -- "$link_tmp"
  fi
  if [[ -n "${receipt_tmp:-}" && -f "$receipt_tmp" ]]; then
    rm -- "$receipt_tmp"
  fi
}
trap cleanup EXIT

install -d -o root -g root -m 0755 "$bundle_tmp/core"
install -o root -g root -m 0555 \
  "$repo_root/ops/investigative_analysis_runner.py" \
  "$bundle_tmp/investigative_analysis_runner.py"
install -o root -g root -m 0444 \
  "$repo_root/core/__init__.py" "$bundle_tmp/core/__init__.py"
install -o root -g root -m 0444 \
  "$repo_root/core/investigative_candidates.py" \
  "$bundle_tmp/core/investigative_candidates.py"
install -o root -g root -m 0444 \
  "$repo_root/ops/investigative-analysis/README.md" "$bundle_tmp/README.md"
install -o root -g root -m 0555 \
  "$repo_root/ops/investigative-analysis/verify-host-bundle.sh" \
  "$bundle_tmp/verify-host-bundle.sh"
verify_git_blob ops/investigative_analysis_runner.py \
  "$bundle_tmp/investigative_analysis_runner.py"
verify_git_blob core/__init__.py "$bundle_tmp/core/__init__.py"
verify_git_blob core/investigative_candidates.py \
  "$bundle_tmp/core/investigative_candidates.py"
verify_git_blob ops/investigative-analysis/README.md "$bundle_tmp/README.md"
verify_git_blob ops/investigative-analysis/verify-host-bundle.sh \
  "$bundle_tmp/verify-host-bundle.sh"
printf '%s\n' "$revision" >"$bundle_tmp/REVISION"
chown root:root "$bundle_tmp/REVISION"
chmod 0444 "$bundle_tmp/REVISION"
(
  cd "$bundle_tmp"
  sha256sum README.md REVISION core/__init__.py \
    core/investigative_candidates.py investigative_analysis_runner.py \
    verify-host-bundle.sh \
    >MANIFEST.sha256
)
chown root:root "$bundle_tmp/MANIFEST.sha256"
chmod 0444 "$bundle_tmp/MANIFEST.sha256"
sync -f "$bundle_tmp"

# Re-check after copying so a concurrent checkout mutation cannot be certified.
if ! final_revision="$(
  git -c "safe.directory=$repo_root" -C "$repo_root" \
    rev-parse --verify HEAD 2>/dev/null
)" \
  || ! checkout_status="$(
    git -c "safe.directory=$repo_root" -C "$repo_root" \
      status --porcelain=v1 --untracked-files=all 2>/dev/null
  )"; then
  die "cannot re-verify the checkout after staging the bundle"
fi
[[ "$final_revision" == "$revision" && -z "$checkout_status" ]] \
  || die "checkout changed while the bundle was staged"

bundle_final="$bundle_root/$revision"
if [[ -e "$bundle_final" ]]; then
  [[ -d "$bundle_final" && ! -L "$bundle_final" ]] \
    || die "existing version path is not a regular directory"
  [[ "$(stat -c '%u:%g:%a' "$bundle_final")" == "0:0:755" ]] \
    || die "existing bundle directory ownership or mode is unsafe"
  [[ -d "$bundle_final/core" && ! -L "$bundle_final/core" ]] \
    || die "existing bundle core path is unsafe"
  [[ "$(stat -c '%u:%g:%a' "$bundle_final/core")" == "0:0:755" ]] \
    || die "existing bundle core ownership or mode is unsafe"
  for bundle_file in README.md REVISION MANIFEST.sha256 core/__init__.py \
    core/investigative_candidates.py investigative_analysis_runner.py \
    verify-host-bundle.sh; do
    [[ -f "$bundle_final/$bundle_file" && ! -L "$bundle_final/$bundle_file" ]] \
      || die "existing bundle file is unsafe: $bundle_file"
    expected_mode="444"
    [[ "$bundle_file" == "investigative_analysis_runner.py" \
        || "$bundle_file" == "verify-host-bundle.sh" ]] \
      && expected_mode="555"
    [[ "$(stat -c '%u:%g:%a' "$bundle_final/$bundle_file")" \
        == "0:0:$expected_mode" ]] \
      || die "existing bundle ownership or mode is unsafe: $bundle_file"
  done
  cmp -s "$bundle_tmp/MANIFEST.sha256" "$bundle_final/MANIFEST.sha256" \
    || die "existing bundle for this revision has different contents"
  (cd "$bundle_final" && sha256sum --quiet --check MANIFEST.sha256) \
    || die "existing bundle for this revision failed validation"
else
  mv -T "$bundle_tmp" "$bundle_final"
  bundle_tmp=""
  sync -f "$bundle_root"
fi

install -o root -g root -m 0644 \
  "$repo_root/ops/systemd/$service_name" "/etc/systemd/system/$service_name"
install -o root -g root -m 0644 \
  "$repo_root/ops/systemd/$timer_name" "/etc/systemd/system/$timer_name"
for unit_name in "$service_name" "$timer_name"; do
  verify_git_blob "ops/systemd/$unit_name" "/etc/systemd/system/$unit_name"
  [[ "$(stat -c '%u:%g:%a' "/etc/systemd/system/$unit_name")" == "0:0:644" ]] \
    || die "installed unit ownership or mode is unsafe: $unit_name"
done
systemctl daemon-reload

ln -s "$revision" "$link_tmp"
mv -Tf "$link_tmp" "$bundle_root/current"
[[ "$(readlink "$bundle_root/current")" == "$revision" ]] \
  || die "current bundle link does not name the staged revision"
sync -f "$bundle_root"

# The receipt is the final commit point.  Until this rename succeeds, the old
# receipt remains intact and the runner cannot claim the new deployment.
receipt_tmp="$(mktemp "$receipt_dir/.deployed-commit.XXXXXX")"
printf '%s\n' "$revision" >"$receipt_tmp"
chown root:root "$receipt_tmp"
chmod 0644 "$receipt_tmp"
sync -f "$receipt_tmp"
mv -Tf "$receipt_tmp" "$receipt_path"
receipt_tmp=""
cmp -s "$bundle_root/current/REVISION" "$receipt_path" \
  || die "installed bundle and deployed receipt differ"
sync -f "$receipt_dir"

printf 'installed Palimpsest analysis bundle %s (%s)\n' "$revision" "$image_id"
