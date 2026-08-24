#!/usr/bin/env bash
# Emit the registry bytes that authenticated the currently tracked WDI receipt.
set -euo pipefail

if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: extract_china_econ_prior_registry.sh FULL_COMMIT_SHA" >&2
  exit 2
fi

revision=$1
git_bin=/usr/bin/git
latest_path=readings/china-econ-wdi-latest.json
registry_path=config/china_econ_wdi_series.json

export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_NO_REPLACE_OBJECTS=1
export GIT_TERMINAL_PROMPT=0
export LC_ALL=C

head_sha=$($git_bin --no-replace-objects rev-parse --verify 'HEAD^{commit}')
if [ "$head_sha" != "$revision" ]; then
  echo "prior-registry extraction revision differs from exact checkout" >&2
  exit 2
fi
latest_commit=$($git_bin --no-replace-objects log \
  --first-parent --max-count=1 --format=%H "$revision" -- "$latest_path")
if [[ ! "$latest_commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "tracked WDI receipt has no bounded first-parent change" >&2
  exit 2
fi

regular_blob_sha() {
  local commit_sha=$1
  local path=$2
  local entry mode type object actual extra
  entry=$($git_bin --no-replace-objects ls-tree "$commit_sha" -- "$path")
  IFS=$' \t' read -r mode type object actual extra <<< "$entry"
  if [ "$mode" != 100644 ] || [ "$type" != blob ] || \
     [[ ! "$object" =~ ^[0-9a-f]{40}$ ]] || [ "$actual" != "$path" ] || \
     [ -n "${extra:-}" ]; then
    echo "$commit_sha:$path is not one exact 100644 Git blob" >&2
    exit 2
  fi
  printf '%s\n' "$object"
}

# Validate both sides of the authority pair at the same historical tree. The
# latest blob is not emitted, but proving it is regular prevents a symlink or
# absent-path history entry from choosing registry authority.
regular_blob_sha "$latest_commit" "$latest_path" > /dev/null
registry_object=$(regular_blob_sha "$latest_commit" "$registry_path")
echo "prior WDI registry authority commit: $latest_commit" >&2
$git_bin --no-replace-objects cat-file blob "$registry_object"
