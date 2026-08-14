#!/usr/bin/env bash
# Install the immutable host runner and atomically certify one deployed commit.

set -Eeuo pipefail

die() {
  printf 'palimpsest-analysis install: %s\n' "$*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || die "run this installer as root"
(( $# <= 1 )) || die "usage: $0 [--ensure-identity|--certify-image|image-reference]"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/../.." && pwd -P)"
mode="install"
image_ref="palimpsest/app:local"
if (( $# == 1 )); then
  if [[ "$1" == "--ensure-identity" ]]; then
    mode="identity-only"
  elif [[ "$1" == "--certify-image" ]]; then
    mode="certify-only"
  else
    image_ref="$1"
  fi
fi
if [[ "$mode" == "install" ]]; then
  [[ -n "$image_ref" && "$image_ref" != -* \
      && "$image_ref" != *[[:space:]]* ]] \
    || die "image reference is empty or malformed"
fi
bundle_root="/usr/local/libexec/palimpsest-analysis"
receipt_dir="/etc/palimpsest"
receipt_path="$receipt_dir/deployed-commit"
service_name="palimpsest-investigative-analysis.service"
timer_name="palimpsest-investigative-analysis.timer"
broker_socket_name="palimpsest-investigative-broker.socket"
broker_service_name="palimpsest-investigative-broker@.service"
runtime_name="palimpsest-analysis"
runtime_id="10001"
analysis_root="/var/lib/palimpsest-analysis"
runs_root="$analysis_root/runs"
private_root="$analysis_root/private"
delivery_root="$analysis_root/delivery"

# Prevent an invoking shell from redirecting Git's object/index/worktree view
# away from the checkout whose files this installer copies.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST="unix:///var/run/docker.sock"

for command_name in cat chmod find getent grep groupadd groupdel install mktemp \
    passwd readlink rm useradd; do
  command -v "$command_name" >/dev/null 2>&1 \
    || die "required command is missing: $command_name"
done
if [[ "$mode" != "identity-only" ]]; then
  for command_name in chmod chown cmp docker find install ln mktemp mv rm \
    sha256sum stat sync; do
    command -v "$command_name" >/dev/null 2>&1 \
      || die "required command is missing: $command_name"
  done
fi
if [[ "$mode" == "install" ]]; then
  for command_name in systemctl systemd-analyze; do
    command -v "$command_name" >/dev/null 2>&1 \
      || die "required command is missing: $command_name"
  done
fi

nologin_shell="$(command -v nologin)" \
  || die "required command is missing: nologin"

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
audit_git="$(mktemp -d /run/palimpsest-analysis-git.XXXXXX)"
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

enumerate_identity_record() {
  local database="$1"
  local rows row record_name record_id match=""

  rows="$(getent "$database")" || return 1
  while IFS= read -r row; do
    [[ -n "$row" ]] || continue
    IFS=: read -r record_name _ record_id _ <<<"$row"
    [[ "$record_name" == "$runtime_name" || "$record_id" == "$runtime_id" ]] \
      || continue
    [[ -z "$match" ]] || return 2
    match="$row"
  done <<<"$rows"
  printf '%s' "$match"
}

ensure_runtime_identity() {
  local group_by_name group_by_id group_enumerated group_name group_id
  local user_by_name user_by_id user_enumerated
  local user_name user_id user_group user_home user_shell
  local password_name password_state canonical_expected_shell canonical_user_shell
  local created_group="false"

  group_by_name="$(getent group "$runtime_name" || true)"
  group_by_id="$(getent group "$runtime_id" || true)"
  user_by_name="$(getent passwd "$runtime_name" || true)"
  user_by_id="$(getent passwd "$runtime_id" || true)"
  group_enumerated="$(enumerate_identity_record group)" \
    || die "cannot prove the analysis group name/GID is unique"
  user_enumerated="$(enumerate_identity_record passwd)" \
    || die "cannot prove the analysis user name/UID is unique"
  [[ "$group_by_name" == "$group_enumerated" \
      && "$group_by_id" == "$group_enumerated" \
      && "$user_by_name" == "$user_enumerated" \
      && "$user_by_id" == "$user_enumerated" ]] \
    || die "keyed and enumerated analysis identity records disagree"

  if [[ -z "$group_by_name" && -z "$group_by_id" \
      && -z "$user_by_name" && -z "$user_by_id" ]]; then
    groupadd --system --gid "$runtime_id" "$runtime_name" \
      || die "cannot create the analysis group"
    created_group="true"
    if ! useradd --system --uid "$runtime_id" --gid "$runtime_id" \
        --home-dir /nonexistent --no-create-home --shell "$nologin_shell" \
        "$runtime_name"; then
      groupdel "$runtime_name" \
        || die "user creation failed and the new analysis group could not be rolled back"
      die "cannot create the analysis user; the new group was rolled back"
    fi
    user_by_name="$(getent passwd "$runtime_name" || true)"
    user_by_id="$(getent passwd "$runtime_id" || true)"
    group_by_name="$(getent group "$runtime_name" || true)"
    group_by_id="$(getent group "$runtime_id" || true)"
    group_enumerated="$(enumerate_identity_record group)" \
      || die "cannot prove the new analysis group is unique"
    user_enumerated="$(enumerate_identity_record passwd)" \
      || die "cannot prove the new analysis user is unique"
    [[ "$group_by_name" == "$group_enumerated" \
        && "$group_by_id" == "$group_enumerated" \
        && "$user_by_name" == "$user_enumerated" \
        && "$user_by_id" == "$user_enumerated" ]] \
      || die "new keyed and enumerated analysis identity records disagree"
  elif [[ -z "$group_by_name" || -z "$group_by_id" \
      || -z "$user_by_name" || -z "$user_by_id" ]]; then
    die "analysis identity is partial or collides; no account changes were made"
  fi

  [[ -n "$group_by_name" && "$group_by_name" == "$group_by_id" ]] \
    || die "UID/GID 10001 group identity is missing or collides"
  IFS=: read -r group_name _ group_id _ <<<"$group_by_name"
  [[ "$group_name" == "$runtime_name" && "$group_id" == "$runtime_id" ]] \
    || die "analysis group does not match palimpsest-analysis:10001"
  [[ -n "$user_by_name" && "$user_by_name" == "$user_by_id" ]] \
    || die "UID/GID 10001 user identity is missing or collides"
  IFS=: read -r user_name _ user_id user_group _ user_home user_shell \
    <<<"$user_by_name"
  canonical_expected_shell="$(readlink -f -- "$nologin_shell" 2>/dev/null || true)"
  canonical_user_shell="$(readlink -f -- "$user_shell" 2>/dev/null || true)"
  [[ "$user_name" == "$runtime_name" && "$user_id" == "$runtime_id" \
      && "$user_group" == "$runtime_id" && "$user_home" == "/nonexistent" \
      && -n "$canonical_expected_shell" \
      && "$canonical_user_shell" == "$canonical_expected_shell" ]] \
    || die "analysis user is not the locked no-home UID/GID 10001 identity"
  read -r password_name password_state _ < <(passwd --status "$runtime_name")
  [[ "$password_name" == "$runtime_name" && "$password_state" == "L" ]] \
    || die "analysis user password is not locked"

  [[ "$created_group" == "false" || -n "$user_by_name" ]] \
    || die "analysis identity creation did not converge"
}

normalize_analysis_storage() {
  local entry unsafe_member

  if [[ -e "$analysis_root" || -L "$analysis_root" ]]; then
    [[ -d "$analysis_root" && ! -L "$analysis_root" ]] \
      || die "analysis root is not a real directory"
  else
    install -d -o root -g root -m 0711 "$analysis_root"
  fi
  chown root:root "$analysis_root"
  chmod 0711 "$analysis_root"
  if [[ -e "$runs_root" || -L "$runs_root" ]]; then
    [[ -d "$runs_root" && ! -L "$runs_root" ]] \
      || die "analysis runs root is not a real directory"
  else
    install -d -o root -g "$runtime_name" -m 0710 "$runs_root"
  fi

  # Take rename authority away before inspecting legacy user-owned run trees.
  chown root:"$runtime_name" "$runs_root"
  chmod 0710 "$runs_root"
  while IFS= read -r -d '' entry; do
    [[ -d "$entry" && ! -L "$entry" ]] \
      || die "analysis runs root contains a non-directory entry"
    [[ "${entry##*/}" =~ ^run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]] \
      || die "analysis runs root contains an unrecognized or stale entry"
  done < <(find "$runs_root" -mindepth 1 -maxdepth 1 -print0)
  unsafe_member="$(
    find "$runs_root" -mindepth 1 \
      \( -type l -o \( ! -type d ! -type f \) \) -print -quit
  )"
  [[ -z "$unsafe_member" ]] \
    || die "analysis run tree contains a link or special file"
  find "$runs_root" -mindepth 1 -type d \
    -exec chown root:"$runtime_name" {} + -exec chmod 0750 {} +
  find "$runs_root" -mindepth 1 -type f \
    -exec chown root:"$runtime_name" {} + -exec chmod 0640 {} +

  if [[ -e "$private_root" || -L "$private_root" ]]; then
    [[ -d "$private_root" && ! -L "$private_root" ]] \
      || die "analysis private root is not a real directory"
  else
    install -d -o "$runtime_name" -g "$runtime_name" -m 0700 "$private_root"
  fi
  chown "$runtime_name":"$runtime_name" "$private_root"
  chmod 0700 "$private_root"
  [[ "$(stat -c '%u:%g:%a' "$analysis_root")" == "0:0:711" ]] \
    || die "analysis root ownership or mode is unsafe"
  [[ "$(stat -c '%u:%g:%a' "$runs_root")" == "0:$runtime_id:710" ]] \
    || die "analysis runs root ownership or mode is unsafe"
  [[ "$(stat -c '%u:%g:%a' "$private_root")" \
      == "$runtime_id:$runtime_id:700" ]] \
    || die "analysis private root ownership or mode is unsafe"

  if [[ -e "$delivery_root" || -L "$delivery_root" ]]; then
    [[ -d "$delivery_root" && ! -L "$delivery_root" ]] \
      || die "analysis delivery root is not a real directory"
  else
    install -d -o "$runtime_name" -g "$runtime_name" -m 0711 "$delivery_root"
  fi
  unsafe_member="$(
    find "$delivery_root" -mindepth 1 -maxdepth 1 \
      \( -type l -o ! -type f \) -print -quit
  )"
  [[ -z "$unsafe_member" ]] \
    || die "analysis delivery root contains a link or special file"
  while IFS= read -r -d '' entry; do
    [[ "${entry##*/}" == "wire-claim-audits-latest.json" ]] \
      || die "analysis delivery root contains an unrecognized file"
  done < <(find "$delivery_root" -mindepth 1 -maxdepth 1 -type f -print0)
  chown "$runtime_name":"$runtime_name" "$delivery_root"
  chmod 0711 "$delivery_root"
  find "$delivery_root" -mindepth 1 -maxdepth 1 -type f \
    -exec chown "$runtime_name":"$runtime_name" {} + -exec chmod 0644 {} +
  [[ "$(stat -c '%u:%g:%a' "$delivery_root")" \
      == "$runtime_id:$runtime_id:711" ]] \
    || die "analysis delivery root ownership or mode is unsafe"
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

if [[ "$mode" == "identity-only" ]]; then
  ensure_runtime_identity
  printf 'validated Palimpsest analysis identity %s:%s\n' \
    "$runtime_name" "$runtime_id"
  exit 0
fi

verify_git_blob() {
  local repository_path="$1"
  local installed_path="$2"
  safe_git show "$revision:$repository_path" \
    | cmp -s - "$installed_path" \
    || die "installed bytes do not match Git HEAD: $repository_path"
}

# Deploys are serialized outside the recurring job.  A stopped timer plus an
# inactive oneshot prevents the current symlink and receipt changing underneath
# a run.  Unknown/systemd-error states fail closed instead of being treated as
# inactive.
if [[ "$mode" == "install" ]]; then
for unit_name in "$timer_name" "$service_name" "$broker_socket_name"; do
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
unit_enablement="$(
  systemctl is-enabled "$broker_service_name" 2>/dev/null || true
)"
case "$unit_enablement" in
  masked|masked-runtime) die "$broker_service_name is masked" ;;
esac
active_broker_instances="$(
  systemctl list-units --no-legend --plain --state=active,activating,deactivating \
    'palimpsest-investigative-broker@*.service' 2>/dev/null || true
)"
[[ -z "$active_broker_instances" ]] \
  || die "an investigative broker request is still active"
fi

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

ensure_runtime_identity
if [[ "$mode" == "certify-only" ]]; then
  install -d -o root -g root -m 0755 "$receipt_dir"
  receipt_tmp="$(mktemp "$receipt_dir/.deployed-commit.XXXXXX")"
  # Invoked indirectly by the EXIT trap below.
  # shellcheck disable=SC2329
  cleanup_certify() {
    rm -f -- "${receipt_tmp:-}"
    cleanup_audit_git
  }
  trap cleanup_certify EXIT
  printf '%s\n' "$revision" >"$receipt_tmp"
  chown root:root "$receipt_tmp"
  chmod 0644 "$receipt_tmp"
  sync -f "$receipt_tmp"
  mv -Tf "$receipt_tmp" "$receipt_path"
  receipt_tmp=""
  [[ -f "$receipt_path" && ! -L "$receipt_path" \
      && "$(stat -c '%u:%g:%a:%h' "$receipt_path")" == "0:0:644:1" ]] \
    || die "deployed receipt did not converge safely"
  [[ "$(cat "$receipt_path")" == "$revision" ]] \
    || die "deployed receipt does not match the certified image"
  sync -f "$receipt_dir"
  printf 'certified Palimpsest image %s (%s)\n' "$revision" "$image_id"
  exit 0
fi
normalize_analysis_storage

systemd-analyze verify \
  "$repo_root/ops/systemd/$service_name" \
  "$repo_root/ops/systemd/$timer_name" \
  "$repo_root/ops/systemd/$broker_socket_name" \
  "$repo_root/ops/systemd/$broker_service_name" \
  "$repo_root/ops/systemd/palimpsest-public-osint-sync.service"

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
  cleanup_audit_git
}
trap cleanup EXIT

install -d -o root -g root -m 0755 "$bundle_tmp/core"
install -o root -g root -m 0555 \
  "$repo_root/ops/investigative_analysis_runner.py" \
  "$bundle_tmp/investigative_analysis_runner.py"
install -o root -g root -m 0555 \
  "$repo_root/ops/investigative_analysis_broker.py" \
  "$bundle_tmp/investigative_analysis_broker.py"
install -o root -g root -m 0444 \
  "$repo_root/core/__init__.py" "$bundle_tmp/core/__init__.py"
install -o root -g root -m 0444 \
  "$repo_root/core/investigative_candidates.py" \
  "$bundle_tmp/core/investigative_candidates.py"
install -o root -g root -m 0444 \
  "$repo_root/core/analytical_pieces.py" \
  "$bundle_tmp/core/analytical_pieces.py"
install -o root -g root -m 0444 \
  "$repo_root/core/wire_claim_audits.py" \
  "$bundle_tmp/core/wire_claim_audits.py"
install -o root -g root -m 0444 \
  "$repo_root/core/investigative_container_contract.py" \
  "$bundle_tmp/core/investigative_container_contract.py"
install -o root -g root -m 0444 \
  "$repo_root/ops/investigative-analysis/README.md" "$bundle_tmp/README.md"
install -o root -g root -m 0555 \
  "$repo_root/ops/investigative-analysis/verify-host-bundle.sh" \
  "$bundle_tmp/verify-host-bundle.sh"
verify_git_blob ops/investigative_analysis_runner.py \
  "$bundle_tmp/investigative_analysis_runner.py"
verify_git_blob ops/investigative_analysis_broker.py \
  "$bundle_tmp/investigative_analysis_broker.py"
verify_git_blob core/__init__.py "$bundle_tmp/core/__init__.py"
verify_git_blob core/investigative_candidates.py \
  "$bundle_tmp/core/investigative_candidates.py"
verify_git_blob core/analytical_pieces.py \
  "$bundle_tmp/core/analytical_pieces.py"
verify_git_blob core/wire_claim_audits.py \
  "$bundle_tmp/core/wire_claim_audits.py"
verify_git_blob core/investigative_container_contract.py \
  "$bundle_tmp/core/investigative_container_contract.py"
verify_git_blob ops/investigative-analysis/README.md "$bundle_tmp/README.md"
verify_git_blob ops/investigative-analysis/verify-host-bundle.sh \
  "$bundle_tmp/verify-host-bundle.sh"
printf '%s\n' "$revision" >"$bundle_tmp/REVISION"
chown root:root "$bundle_tmp/REVISION"
chmod 0444 "$bundle_tmp/REVISION"
printf '%s\n' "$image_id" >"$bundle_tmp/IMAGE_ID"
chown root:root "$bundle_tmp/IMAGE_ID"
chmod 0444 "$bundle_tmp/IMAGE_ID"
(
  cd "$bundle_tmp"
  sha256sum README.md REVISION IMAGE_ID core/__init__.py \
    core/investigative_candidates.py core/analytical_pieces.py \
    core/wire_claim_audits.py \
    core/investigative_container_contract.py \
    investigative_analysis_runner.py investigative_analysis_broker.py \
    verify-host-bundle.sh \
    >MANIFEST.sha256
)
chown root:root "$bundle_tmp/MANIFEST.sha256"
chmod 0444 "$bundle_tmp/MANIFEST.sha256"
sync -f "$bundle_tmp"

# Re-check after copying so a concurrent checkout mutation cannot be certified.
if ! final_revision="$(cat "$repo_root/.git/HEAD")" \
  || ! checkout_status="$(
    safe_git status --porcelain=v1 --untracked-files=all 2>/dev/null
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
  for bundle_file in README.md REVISION IMAGE_ID MANIFEST.sha256 core/__init__.py \
    core/investigative_candidates.py core/analytical_pieces.py \
    core/wire_claim_audits.py \
    core/investigative_container_contract.py \
    investigative_analysis_runner.py investigative_analysis_broker.py \
    verify-host-bundle.sh; do
    [[ -f "$bundle_final/$bundle_file" && ! -L "$bundle_final/$bundle_file" ]] \
      || die "existing bundle file is unsafe: $bundle_file"
    expected_mode="444"
    [[ "$bundle_file" == "investigative_analysis_runner.py" \
        || "$bundle_file" == "investigative_analysis_broker.py" \
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
install -o root -g root -m 0644 \
  "$repo_root/ops/systemd/$broker_socket_name" \
  "/etc/systemd/system/$broker_socket_name"
install -o root -g root -m 0644 \
  "$repo_root/ops/systemd/$broker_service_name" \
  "/etc/systemd/system/$broker_service_name"
for unit_name in "$service_name" "$timer_name" "$broker_socket_name" \
  "$broker_service_name"; do
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
