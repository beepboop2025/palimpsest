#!/usr/bin/env bash
# Validate a registry-only feature ref as data from trusted current-main workflow code.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: validate_china_econ_registry_candidate.sh WORKFLOW_SHA REF SHA" >&2
  exit 2
fi

workflow_sha=$1
registry_ref=$2
registry_sha=$3
git_bin=/usr/bin/git
remote_candidate=refs/remotes/origin/china-econ-registry-candidate
registry_path=config/china_econ_wdi_series.json

if [[ ! "$workflow_sha" =~ ^[0-9a-f]{40}$ ]] || \
   [[ ! "$registry_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "workflow and registry SHAs must be full lowercase commit IDs" >&2
  exit 2
fi
if [[ ! "$registry_ref" =~ ^refs/heads/review/china-econ-registry-[A-Za-z0-9][A-Za-z0-9._-]{0,80}$ ]]; then
  echo "registry ref is outside refs/heads/review/china-econ-registry-*" >&2
  exit 2
fi

export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_NO_REPLACE_OBJECTS=1
export GIT_TERMINAL_PROMPT=0
export LC_ALL=C

$git_bin fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
$git_bin fetch --no-tags origin "+$registry_ref:$remote_candidate"
main_sha=$($git_bin --no-replace-objects rev-parse --verify 'origin/main^{commit}')
candidate_sha=$($git_bin --no-replace-objects rev-parse --verify "$remote_candidate^{commit}")
if [ "$main_sha" != "$workflow_sha" ]; then
  echo "origin/main advanced beyond the trusted workflow SHA" >&2
  exit 2
fi
if [ "$candidate_sha" != "$registry_sha" ]; then
  echo "registry ref does not resolve to the operator-pinned SHA" >&2
  exit 2
fi
if [ "$($git_bin --no-replace-objects merge-base "$candidate_sha" "$main_sha")" != "$main_sha" ]; then
  echo "registry candidate is not based on exact current main" >&2
  exit 2
fi
change=$($git_bin --no-replace-objects diff --name-status \
  "$main_sha...$candidate_sha")
if [ "$change" != $'M\tconfig/china_econ_wdi_series.json' ]; then
  echo "registry candidate tree must modify only $registry_path" >&2
  exit 2
fi
entry=$($git_bin --no-replace-objects ls-tree "$candidate_sha" -- "$registry_path")
IFS=$' \t' read -r mode type object actual extra <<< "$entry"
if [ "$mode" != 100644 ] || [ "$type" != blob ] || \
   [[ ! "$object" =~ ^[0-9a-f]{40}$ ]] || [ "$actual" != "$registry_path" ] || \
   [ -n "${extra:-}" ]; then
  echo "registry candidate is not one exact 100644 Git blob" >&2
  exit 2
fi
printf '%s\n' "$candidate_sha"
