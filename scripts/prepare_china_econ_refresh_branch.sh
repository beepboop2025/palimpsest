#!/usr/bin/env bash
# Fail closed unless this manual refresh still runs on the exact current main SHA.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: prepare_china_econ_refresh_branch.sh WORKFLOW_SHA CANDIDATE_SHA BRANCH" >&2
  exit 2
fi

workflow_sha=$1
candidate_sha=$2
branch=$3
git_bin=/usr/bin/git

if [[ ! "$workflow_sha" =~ ^[0-9a-f]{40}$ ]] || \
   [[ ! "$candidate_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "workflow and candidate SHAs must be full lowercase commit IDs" >&2
  exit 2
fi
if [[ ! "$branch" =~ ^automation/china-econ-refresh-[0-9]+-[0-9]+$ ]]; then
  echo "refresh branch name is outside the reviewed namespace" >&2
  exit 2
fi

export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_NO_REPLACE_OBJECTS=1
export GIT_TERMINAL_PROMPT=0
export LC_ALL=C

head_sha=$($git_bin --no-replace-objects rev-parse --verify 'HEAD^{commit}')
main_sha=$($git_bin --no-replace-objects rev-parse --verify 'origin/main^{commit}')
if [ "$head_sha" != "$workflow_sha" ]; then
  echo "checked-out refresh workflow SHA differs from GITHUB_SHA" >&2
  exit 2
fi
if [ "$main_sha" != "$workflow_sha" ]; then
  echo "origin/main advanced after refresh dispatch; start a new manual run" >&2
  exit 2
fi
if [ -n "$($git_bin status --porcelain=v1 --untracked-files=all)" ]; then
  echo "refresh checkout is dirty before branch creation" >&2
  exit 2
fi

$git_bin --no-replace-objects cat-file -e "$candidate_sha^{commit}"
$git_bin checkout -B "$branch" "$candidate_sha"
test "$($git_bin --no-replace-objects rev-parse --verify 'HEAD^{commit}')" = "$candidate_sha"
printf 'prepared exact refresh branch %s at %s\n' "$branch" "$candidate_sha"
