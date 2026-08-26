#!/usr/bin/env bash
# Install the revision-bound, root-owned node offsite-backup runner.

set -Eeuo pipefail

die() {
  printf 'palimpsest-node-offsite install: %s\n' "$*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || die "run this installer as root"
(( $# == 0 )) || die "usage: $0"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/../.." && pwd -P)"
bundle_root="/usr/local/libexec/palimpsest-node-offsite"
receipt_path="/etc/palimpsest/deployed-commit"
service_name="palimpsest-node-offsite-backup.service"
timer_name="palimpsest-node-offsite-backup.timer"

# Do not let an invoking shell redirect Git commands to a different repository.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE

for command_name in cat chmod chown cmp find grep install ln mktemp mv readlink rm \
  docker sha256sum sort stat sync systemctl systemd-analyze; do
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
audit_git="$(mktemp -d /run/palimpsest-node-offsite-git.XXXXXX)"
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

revision="$(cat "$repo_root/.git/HEAD")" \
  || die "cannot read the detached Git revision"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die "Git returned a malformed revision"
printf '%s\n' "$revision" >"$audit_git/HEAD"
if ! checkout_status="$(
  safe_git status --porcelain=v1 --untracked-files=all 2>/dev/null
)"; then
  die "cannot verify the Git checkout through the isolated audit view"
fi
[[ -z "$checkout_status" ]] || die "checkout has modified or untracked files"

[[ -f "$receipt_path" && ! -L "$receipt_path" ]] \
  || die "deployed commit receipt is missing or unsafe"
[[ "$(stat -c '%u:%h' "$receipt_path")" == "0:1" ]] \
  || die "deployed commit receipt must be a root-owned single-link file"
receipt_mode="$(stat -c '%a' "$receipt_path")"
[[ "$receipt_mode" =~ ^[0-7]{3,4}$ ]] \
  || die "deployed commit receipt mode is unreadable"
(( (8#$receipt_mode & 0022) == 0 )) \
  || die "deployed commit receipt is writable by group or other"
cmp -s "$receipt_path" <(printf '%s\n' "$revision") \
  || die "deployed commit receipt does not exactly match Git HEAD"

# A stopped recurring job makes the current symlink switch atomic from the
# runner's point of view. Unknown systemd states fail closed.
for unit_name in "$timer_name" "$service_name"; do
  unit_enablement="$(systemctl is-enabled "$unit_name" 2>/dev/null || true)"
  case "$unit_enablement" in
    masked|masked-runtime) die "$unit_name is masked" ;;
  esac
  load_state="$(systemctl show --property=LoadState --value \
    "$unit_name" 2>/dev/null || true)"
  [[ -z "$load_state" || "$load_state" == "not-found" ]] && continue
  unit_state="$(systemctl show --property=ActiveState --value \
    "$unit_name" 2>/dev/null)" \
    || die "cannot verify systemd state for $unit_name"
  case "$unit_state" in
    inactive|failed) ;;
    active|activating|deactivating|reloading)
      die "$unit_name must be stopped before installation"
      ;;
    *) die "unexpected systemd state for $unit_name: $unit_state" ;;
  esac
done
timer_enablement="$(systemctl is-enabled "$timer_name" 2>/dev/null || true)"
case "$timer_enablement" in
  ""|disabled|not-found) ;;
  masked|masked-runtime) die "$timer_name is masked" ;;
  enabled|enabled-runtime|linked|linked-runtime|alias|indirect|generated|transient)
    die "$timer_name must be disabled before installation"
    ;;
  *) die "unexpected enablement state for $timer_name: $timer_enablement" ;;
esac

postgres_container="$(
  docker ps --filter 'label=com.docker.compose.project=palimpsest' \
    --filter 'label=com.docker.compose.service=postgres' --quiet
)"
[[ "$postgres_container" =~ ^[0-9a-f]{12,64}$ ]] \
  || die "cannot identify exactly one running production PostgreSQL container"
postgres_image_id="$(docker inspect --format '{{.Image}}' "$postgres_container")"
[[ "$postgres_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "production PostgreSQL image identity is malformed"
[[ "$(docker inspect --format '{{.State.Running}}' "$postgres_container")" == true ]] \
  || die "production PostgreSQL container is not running"
redis_container="$(
  docker ps --filter 'label=com.docker.compose.project=palimpsest' \
    --filter 'label=com.docker.compose.service=redis' --quiet
)"
[[ "$redis_container" =~ ^[0-9a-f]{12,64}$ ]] \
  || die "cannot identify exactly one running production Redis container"
redis_image_id="$(docker inspect --format '{{.Image}}' "$redis_container")"
[[ "$redis_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "production Redis image identity is malformed"
[[ "$(docker inspect --format '{{.State.Running}}' "$redis_container")" == true ]] \
  || die "production Redis container is not running"
postgres_binary_version="$(
  docker run --rm --pull never --network none --read-only --log-driver none \
    --security-opt no-new-privileges:true --cap-drop ALL \
    --entrypoint postgres "$postgres_image_id" --version
)"
[[ "$postgres_binary_version" == "postgres (PostgreSQL) 16."* ]] \
  || die "production PostgreSQL image is not major version 16"
redis_binary_version="$(
  docker run --rm --pull never --network none --read-only --log-driver none \
    --security-opt no-new-privileges:true --cap-drop ALL \
    --entrypoint redis-server "$redis_image_id" --version
)"
[[ "$redis_binary_version" == "Redis server v=7."* ]] \
  || die "production Redis image is not major version 7"

systemd-analyze verify \
  "$repo_root/ops/systemd/$service_name" \
  "$repo_root/ops/systemd/$timer_name"

verify_git_blob() {
  local repository_path="$1"
  local installed_path="$2"

  safe_git show "$revision:$repository_path" \
    | cmp -s - "$installed_path" \
    || die "installed bytes do not match Git HEAD: $repository_path"
}

if [[ -e "$bundle_root" || -L "$bundle_root" ]]; then
  [[ -d "$bundle_root" && ! -L "$bundle_root" \
      && "$(stat -c '%u:%g:%a' "$bundle_root")" == "0:0:755" ]] \
    || die "bundle root ownership, mode, or type is unsafe"
else
  install -d -o root -g root -m 0755 "$bundle_root"
fi
bundle_tmp="$(mktemp -d "$bundle_root/.bundle-$revision.XXXXXX")"
chown root:root "$bundle_tmp"
chmod 0755 "$bundle_tmp"
link_tmp="$bundle_root/.current.$$.tmp"

cleanup() {
  if [[ -n "${bundle_tmp:-}" && -d "$bundle_tmp" ]]; then
    rm -rf -- "$bundle_tmp"
  fi
  if [[ -L "${link_tmp:-}" ]]; then
    rm -- "$link_tmp"
  fi
  cleanup_audit_git
}
trap cleanup EXIT

install -o root -g root -m 0555 \
  "$repo_root/ops/backup/palimpsest-node-offsite-backup.sh" \
  "$bundle_tmp/palimpsest-node-offsite-backup.sh"
install -o root -g root -m 0444 \
  "$repo_root/ops/backup/node_backup_snapshot.py" \
  "$bundle_tmp/node_backup_snapshot.py"
install -o root -g root -m 0444 \
  "$repo_root/ops/node-offsite/README.md" "$bundle_tmp/README.md"
install -o root -g root -m 0555 \
  "$repo_root/ops/node-offsite/verify-host-bundle.sh" \
  "$bundle_tmp/verify-host-bundle.sh"

verify_git_blob ops/backup/palimpsest-node-offsite-backup.sh \
  "$bundle_tmp/palimpsest-node-offsite-backup.sh"
verify_git_blob ops/backup/node_backup_snapshot.py \
  "$bundle_tmp/node_backup_snapshot.py"
verify_git_blob ops/node-offsite/README.md "$bundle_tmp/README.md"
verify_git_blob ops/node-offsite/verify-host-bundle.sh \
  "$bundle_tmp/verify-host-bundle.sh"

printf '%s\n' "$revision" >"$bundle_tmp/REVISION"
chown root:root "$bundle_tmp/REVISION"
chmod 0444 "$bundle_tmp/REVISION"
printf '%s\n' "$postgres_image_id" >"$bundle_tmp/POSTGRES_IMAGE_ID"
chown root:root "$bundle_tmp/POSTGRES_IMAGE_ID"
chmod 0444 "$bundle_tmp/POSTGRES_IMAGE_ID"
printf '%s\n' "$redis_image_id" >"$bundle_tmp/REDIS_IMAGE_ID"
chown root:root "$bundle_tmp/REDIS_IMAGE_ID"
chmod 0444 "$bundle_tmp/REDIS_IMAGE_ID"
(
  cd "$bundle_tmp"
  sha256sum README.md REVISION POSTGRES_IMAGE_ID REDIS_IMAGE_ID \
    node_backup_snapshot.py \
    palimpsest-node-offsite-backup.sh verify-host-bundle.sh \
    >MANIFEST.sha256
)
chown root:root "$bundle_tmp/MANIFEST.sha256"
chmod 0444 "$bundle_tmp/MANIFEST.sha256"
sync -f "$bundle_tmp"

# Re-check after copying. A checkout mutation during staging must never become
# the root-owned version selected by `current`.
if ! final_revision="$(cat "$repo_root/.git/HEAD")" \
  || ! checkout_status="$(
    safe_git status --porcelain=v1 --untracked-files=all 2>/dev/null
  )"; then
  die "cannot re-verify the checkout after staging the bundle"
fi
[[ "$final_revision" == "$revision" && -z "$checkout_status" ]] \
  || die "checkout changed while the bundle was staged"
cmp -s "$receipt_path" <(printf '%s\n' "$revision") \
  || die "deployed commit receipt changed while the bundle was staged"

bundle_final="$bundle_root/$revision"
if [[ -e "$bundle_final" || -L "$bundle_final" ]]; then
  [[ -d "$bundle_final" && ! -L "$bundle_final" ]] \
    || die "existing version path is not a regular directory"
  [[ "$(stat -c '%u:%g:%a' "$bundle_final")" == "0:0:755" ]] \
    || die "existing bundle directory ownership or mode is unsafe"
  expected_inventory=$'MANIFEST.sha256\nPOSTGRES_IMAGE_ID\nREADME.md\nREDIS_IMAGE_ID\nREVISION\nnode_backup_snapshot.py\npalimpsest-node-offsite-backup.sh\nverify-host-bundle.sh'
  actual_inventory="$(
    find "$bundle_final" -mindepth 1 -maxdepth 1 -printf '%f\n' \
      | LC_ALL=C sort
  )"
  [[ "$actual_inventory" == "$expected_inventory" ]] \
    || die "existing bundle inventory is not exact"
  for specification in \
    'MANIFEST.sha256:444' \
    'POSTGRES_IMAGE_ID:444' \
    'REDIS_IMAGE_ID:444' \
    'README.md:444' \
    'REVISION:444' \
    'node_backup_snapshot.py:444' \
    'palimpsest-node-offsite-backup.sh:555' \
    'verify-host-bundle.sh:555'; do
    IFS=: read -r bundle_file expected_mode <<<"$specification"
    [[ -f "$bundle_final/$bundle_file" \
        && ! -L "$bundle_final/$bundle_file" ]] \
      || die "existing bundle file is unsafe: $bundle_file"
    [[ "$(stat -c '%u:%g:%a:%h' "$bundle_final/$bundle_file")" \
        == "0:0:$expected_mode:1" ]] \
      || die "existing bundle metadata is unsafe: $bundle_file"
  done
  cmp -s "$bundle_tmp/MANIFEST.sha256" "$bundle_final/MANIFEST.sha256" \
    || die "existing bundle for this revision has different contents"
  /bin/sh "$bundle_final/verify-host-bundle.sh" \
    || die "existing bundle for this revision failed validation"
else
  mv -T "$bundle_tmp" "$bundle_final"
  bundle_tmp=""
  sync -f "$bundle_root"
fi

for unit_name in "$service_name" "$timer_name"; do
  install -o root -g root -m 0644 \
    "$repo_root/ops/systemd/$unit_name" "/etc/systemd/system/$unit_name"
  verify_git_blob "ops/systemd/$unit_name" "/etc/systemd/system/$unit_name"
  [[ "$(stat -c '%u:%g:%a:%h' "/etc/systemd/system/$unit_name")" \
      == "0:0:644:1" ]] \
    || die "installed unit ownership or mode is unsafe: $unit_name"
done
# The OnSuccess trigger is intentionally not installed here. It is added as a
# removable drop-in only after the mandatory first manual recovery drill.
systemctl daemon-reload

ln -s "$revision" "$link_tmp"
mv -Tf "$link_tmp" "$bundle_root/current"
[[ "$(readlink "$bundle_root/current")" == "$revision" ]] \
  || die "current bundle link does not name the staged revision"
/bin/sh "$bundle_root/current/verify-host-bundle.sh" \
  || die "selected bundle failed validation"
cmp -s "$bundle_root/current/REVISION" "$receipt_path" \
  || die "selected bundle and deployed commit receipt differ"
sync -f "$bundle_root"

printf 'installed Palimpsest node-offsite bundle %s; timer remains unchanged\n' \
  "$revision"
