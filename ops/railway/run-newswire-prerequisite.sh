#!/usr/bin/env bash

# Run the one bounded Newswire refresh required before Palimpsest's first
# authenticated Railway activation canary. This operator transaction never
# opens the hourly Railway gate and always restores the Newswire schedule freeze.

set -Eeuo pipefail
umask 077

PALIMPSEST_REPOSITORY="${PALIMPSEST_REPOSITORY:-beepboop2025/palimpsest}"
NEWSWIRE_WORKFLOW="${NEWSWIRE_WORKFLOW:-newswire-refresh.yml}"
PUBLICATION_WORKFLOW="${PUBLICATION_WORKFLOW:-tests.yml}"
RAILWAY_PRODUCTION_ENVIRONMENT="${RAILWAY_PRODUCTION_ENVIRONMENT:-palimpsest-railway-production}"
EXPECTED_NEWSWIRE_BASE_SHA="${EXPECTED_NEWSWIRE_BASE_SHA:-}"
NEWSWIRE_PREREQUISITE_RECEIPT="${NEWSWIRE_PREREQUISITE_RECEIPT:-}"
NEWSWIRE_TMP_DIR=''
NEWSWIRE_WORKFLOW_RESTORE_DISABLED=0
NEWSWIRE_TRANSACTION_COMPLETE=0
NEWSWIRE_RUN_ID=''
PUBLICATION_CONTRACT_RUN_ID=''

bounded_gh() {
  (( $# >= 2 ))
  local command_timeout_seconds="$1"
  shift
  [[ "$command_timeout_seconds" =~ ^[1-9][0-9]*$ ]]
  python3 - "$command_timeout_seconds" "$@" <<'PY'
import os
import signal
import subprocess
import sys

timeout_seconds = int(sys.argv[1], 10)
if timeout_seconds <= 0:
    raise SystemExit("GitHub command timeout must be positive")
try:
    process = subprocess.Popen(
        ["gh", *sys.argv[2:]],
        start_new_session=True,
    )
    returncode = process.wait(timeout=timeout_seconds)
except subprocess.TimeoutExpired:
    print("bounded GitHub command timed out", file=sys.stderr)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    raise SystemExit(124) from None
raise SystemExit(returncode)
PY
}

private_directory_is_owned_0700() {
  (( $# == 1 ))
  python3 - "$1" "$(id -u)" "$(id -g)" <<'PY'
import os
import stat
import sys

try:
    metadata = os.lstat(sys.argv[1])
except OSError:
    raise SystemExit(1) from None
if (
    not stat.S_ISDIR(metadata.st_mode)
    or metadata.st_uid != int(sys.argv[2], 10)
    or metadata.st_gid != int(sys.argv[3], 10)
    or stat.S_IMODE(metadata.st_mode) != 0o700
):
    raise SystemExit(1)
PY
}

begin_newswire_receipt_commit() {
  # Bash can defer a trapped signal until a foreground writer returns. Ignore
  # those signals only across the durable write and completion-flag handoff so
  # cleanup can never observe a committed receipt with a false completion flag.
  trap '' HUP INT TERM
}

restore_newswire_receipt_signal_handlers() {
  trap 'cleanup_newswire_prerequisite 129' HUP
  trap 'cleanup_newswire_prerequisite 130' INT
  trap 'cleanup_newswire_prerequisite 143' TERM
}

finish_newswire_receipt_commit() {
  NEWSWIRE_TRANSACTION_COMPLETE=1
  restore_newswire_receipt_signal_handlers
}

newswire_workflow_state() {
  bounded_gh 60 api \
    "repos/$PALIMPSEST_REPOSITORY/actions/workflows/$NEWSWIRE_WORKFLOW" \
    --jq .state
}

restore_newswire_workflow_freeze() {
  local workflow_state=''
  (( NEWSWIRE_WORKFLOW_RESTORE_DISABLED == 1 )) || return 0
  for _ in {1..3}; do
    workflow_state="$(newswire_workflow_state)" || workflow_state=''
    if [[ "$workflow_state" == disabled_manually ]]; then
      NEWSWIRE_WORKFLOW_RESTORE_DISABLED=0
      return 0
    fi
    if bounded_gh 60 workflow disable "$NEWSWIRE_WORKFLOW" \
        --repo "$PALIMPSEST_REPOSITORY"; then
      workflow_state="$(newswire_workflow_state)" || workflow_state=''
      if [[ "$workflow_state" == disabled_manually ]]; then
        NEWSWIRE_WORKFLOW_RESTORE_DISABLED=0
        return 0
      fi
    fi
    sleep 2
  done
  printf 'failed to restore the Newswire workflow freeze\n' >&2
  return 1
}

clear_railway_writer_authority_on_failure() {
  (( $# == 1 ))
  local failure_status="$1" acknowledgement_state=''
  local acknowledgement_count=''
  local cleanup_status=0
  (( failure_status != 0 )) || return 0

  if ! bounded_gh 60 variable set RAILWAY_PUBLICATION_ENABLED --body false \
      --repo "$PALIMPSEST_REPOSITORY"; then
    printf 'failed to force the Railway hourly publication gate closed\n' >&2
    cleanup_status=1
  elif [[ "$(bounded_gh 60 variable get RAILWAY_PUBLICATION_ENABLED \
      --repo "$PALIMPSEST_REPOSITORY" 2>/dev/null)" != false ]]; then
    printf 'Railway hourly publication gate did not remain closed\n' >&2
    cleanup_status=1
  fi

  acknowledgement_state="$(bounded_gh 60 variable list \
      --repo "$PALIMPSEST_REPOSITORY" \
      --env "$RAILWAY_PRODUCTION_ENVIRONMENT" --json name,value \
      --jq '[.[] | select(.name == "RAILWAY_EXCLUSIVE_WRITER_ACK") | .value]
        | if length == 0 then "absent"
          elif length == 1 and .[0] == "palimpsest-github-environment-v1"
          then "exact" else "unexpected" end' 2>/dev/null)" \
    || acknowledgement_state='unproved'
  case "$acknowledgement_state" in
    absent) ;;
    exact)
      if ! bounded_gh 60 variable delete RAILWAY_EXCLUSIVE_WRITER_ACK \
          --repo "$PALIMPSEST_REPOSITORY" \
          --env "$RAILWAY_PRODUCTION_ENVIRONMENT"; then
        printf 'failed to remove the Railway writer acknowledgement\n' >&2
        cleanup_status=1
      fi
      ;;
    unexpected)
      printf 'refusing to delete an unfamiliar Railway writer acknowledgement\n' >&2
      cleanup_status=1
      ;;
    *)
      printf 'Railway writer acknowledgement state was not proved\n' >&2
      cleanup_status=1
      ;;
  esac
  acknowledgement_count="$(bounded_gh 60 variable list \
    --repo "$PALIMPSEST_REPOSITORY" \
    --env "$RAILWAY_PRODUCTION_ENVIRONMENT" --json name \
    --jq '[.[] | select(.name == "RAILWAY_EXCLUSIVE_WRITER_ACK")] | length' \
    2>/dev/null)" || acknowledgement_count='unknown'
  if [[ "$acknowledgement_count" != 0 ]]; then
    printf 'Railway writer acknowledgement absence was not proved\n' >&2
    cleanup_status=1
  fi
  if [[ "$PUBLICATION_CONTRACT_RUN_ID" =~ ^[0-9]+$ ]]; then
    printf 'Reconcile publication contract run %s; cleanup did not cancel it.\n' \
      "$PUBLICATION_CONTRACT_RUN_ID" >&2
  fi
  return "$cleanup_status"
}

cleanup_newswire_prerequisite() {
  local original_status="${1:-$?}" restore_status=0 temp_status=0
  local authority_status=0 final_failure_status=0
  trap - ERR EXIT HUP INT TERM
  set +e
  restore_newswire_workflow_freeze || restore_status=$?
  if [[ -n "$NEWSWIRE_TMP_DIR" ]]; then
    if ! private_directory_is_owned_0700 "$NEWSWIRE_TMP_DIR"; then
      printf 'refusing unauthenticated Newswire temporary cleanup\n' >&2
      temp_status=1
    elif ! rm -rf -- "$NEWSWIRE_TMP_DIR"; then
      temp_status=1
    fi
  fi
  if (( original_status != 0 )); then
    final_failure_status="$original_status"
  elif (( restore_status != 0 || temp_status != 0 )); then
    final_failure_status=1
  fi
  if (( NEWSWIRE_TRANSACTION_COMPLETE == 1 )); then
    if (( restore_status != 0 || temp_status != 0 )); then
      printf 'Newswire receipt is committed; post-commit cleanup needs manual attention\n' >&2
    fi
    exit 0
  fi
  clear_railway_writer_authority_on_failure "$final_failure_status" \
    || authority_status=$?
  if (( original_status != 0 )); then
    exit "$original_status"
  fi
  if (( restore_status != 0 || temp_status != 0 || authority_status != 0 )); then
    exit 1
  fi
  exit 0
}

trap 'cleanup_newswire_prerequisite "$?"' EXIT
trap 'cleanup_newswire_prerequisite 129' HUP
trap 'cleanup_newswire_prerequisite 130' INT
trap 'cleanup_newswire_prerequisite 143' TERM

[[ "$EXPECTED_NEWSWIRE_BASE_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ -n "$NEWSWIRE_PREREQUISITE_RECEIPT" ]]
[[ "$NEWSWIRE_PREREQUISITE_RECEIPT" = /* ]]
test ! -e "$NEWSWIRE_PREREQUISITE_RECEIPT"
test ! -L "$NEWSWIRE_PREREQUISITE_RECEIPT"
test -d "$(dirname "$NEWSWIRE_PREREQUISITE_RECEIPT")"
test ! -L "$(dirname "$NEWSWIRE_PREREQUISITE_RECEIPT")"
bounded_gh 30 auth status --hostname github.com
test "$(bounded_gh 60 variable get RAILWAY_PUBLICATION_ENABLED \
  --repo "$PALIMPSEST_REPOSITORY")" = false
test "$(bounded_gh 60 variable get RAILWAY_EXCLUSIVE_WRITER_ACK \
  --repo "$PALIMPSEST_REPOSITORY" \
  --env "$RAILWAY_PRODUCTION_ENVIRONMENT")" \
  = palimpsest-github-environment-v1
test "$(newswire_workflow_state)" = disabled_manually
test "$(bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/commits/main" --jq .sha)" \
  = "$EXPECTED_NEWSWIRE_BASE_SHA"

# The schedule is at minute 17. Refuse the surrounding window even though the
# post-dispatch selector also rejects a newly delivered schedule event.
NEWSWIRE_UTC_MINUTE="$(date -u +%M)"
[[ "$NEWSWIRE_UTC_MINUTE" =~ ^[0-5][0-9]$ ]]
NEWSWIRE_UTC_MINUTE_DECIMAL=$((10#$NEWSWIRE_UTC_MINUTE))
if (( NEWSWIRE_UTC_MINUTE_DECIMAL >= 12 \
    && NEWSWIRE_UTC_MINUTE_DECIMAL <= 22 )); then
  printf 'refusing Newswire activation inside the minute-17 schedule window\n' >&2
  exit 1
fi

NEWSWIRE_TMP_DIR="$(mktemp -d)"
chmod 0700 "$NEWSWIRE_TMP_DIR"
NEWSWIRE_RUNS_BEFORE="$NEWSWIRE_TMP_DIR/newswire-runs-before.json"
NEWSWIRE_RUNS_AFTER="$NEWSWIRE_TMP_DIR/newswire-runs-after.json"
CONTRACT_RUNS_BEFORE="$NEWSWIRE_TMP_DIR/contract-runs-before.json"
CONTRACT_RUNS_AFTER="$NEWSWIRE_TMP_DIR/contract-runs-after.json"
BASE_PUSH_RUNS="$NEWSWIRE_TMP_DIR/base-push-runs.json"
PUBLICATION_JOBS="$NEWSWIRE_TMP_DIR/publication-jobs.json"
NEWSWIRE_JSON="$NEWSWIRE_TMP_DIR/newswire-latest.json"
SITUATION_JSON="$NEWSWIRE_TMP_DIR/china-situation-latest.json"
NEWSWIRE_RUN_METADATA="$NEWSWIRE_TMP_DIR/newswire-run-metadata.json"
NEWSWIRE_COMMIT_JSON="$NEWSWIRE_TMP_DIR/newswire-commit.json"
NEWSWIRE_COMMIT_PROOF="$NEWSWIRE_TMP_DIR/newswire-commit-proof.json"
NEWSWIRE_ARTIFACTS_JSON="$NEWSWIRE_TMP_DIR/newswire-artifacts.json"
NEWSWIRE_ARTIFACT_PROOF="$NEWSWIRE_TMP_DIR/newswire-artifact-proof.json"
NEWSWIRE_ARTIFACT_ZIP="$NEWSWIRE_TMP_DIR/newswire-acquisition.zip"
NEWSWIRE_ARTIFACT_JSON="$NEWSWIRE_TMP_DIR/artifact-newswire-latest.json"
NEWSWIRE_ARTIFACT_VERSIONS="$NEWSWIRE_TMP_DIR/artifact-newswire-versions.jsonl"
BASE_NEWSWIRE_METADATA="$NEWSWIRE_TMP_DIR/base-newswire-metadata.json"
BASE_SITUATION_METADATA="$NEWSWIRE_TMP_DIR/base-situation-metadata.json"
OUTPUT_NEWSWIRE_METADATA="$NEWSWIRE_TMP_DIR/output-newswire-metadata.json"
OUTPUT_SITUATION_METADATA="$NEWSWIRE_TMP_DIR/output-situation-metadata.json"
RECEIPT_TMP="$NEWSWIRE_TMP_DIR/newswire-prerequisite-receipt.json"

bounded_gh 60 run list --repo "$PALIMPSEST_REPOSITORY" \
  --workflow "$NEWSWIRE_WORKFLOW" --limit 100 \
  --json databaseId,event,headSha,status,conclusion,workflowName \
  >"$NEWSWIRE_RUNS_BEFORE"
python3 - "$NEWSWIRE_RUNS_BEFORE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    runs = json.load(handle)
if not isinstance(runs, list):
    raise SystemExit("Newswire run inventory is not a list")
active = [run for run in runs if run.get("status") != "completed"]
if active:
    raise SystemExit("a Newswire run is already active")
PY

bounded_gh 60 run list --repo "$PALIMPSEST_REPOSITORY" \
  --workflow "$PUBLICATION_WORKFLOW" --event push --limit 100 \
  --json databaseId,event,headSha,status,conclusion,workflowName \
  >"$BASE_PUSH_RUNS"
BASE_PUSH_RUN_ID="$(python3 - "$BASE_PUSH_RUNS" \
  "$EXPECTED_NEWSWIRE_BASE_SHA" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    runs = json.load(handle)
candidates = [
    run for run in runs
    if run.get("event") == "push"
    and run.get("headSha") == sys.argv[2]
    and run.get("workflowName") == "Tests"
]
if len(candidates) != 1:
    raise SystemExit("exact base push acceptance run is not unique")
run = candidates[0]
if run.get("status") != "completed" or run.get("conclusion") != "success":
    raise SystemExit("exact base push acceptance did not succeed")
print(run["databaseId"])
PY
)"
[[ "$BASE_PUSH_RUN_ID" =~ ^[0-9]+$ ]]

bounded_gh 60 run list --repo "$PALIMPSEST_REPOSITORY" \
  --workflow "$PUBLICATION_WORKFLOW" --event repository_dispatch --limit 100 \
  --json databaseId,event,headSha,status,conclusion,workflowName \
  >"$CONTRACT_RUNS_BEFORE"

NEWSWIRE_WORKFLOW_RESTORE_DISABLED=1
bounded_gh 60 workflow enable "$NEWSWIRE_WORKFLOW" \
  --repo "$PALIMPSEST_REPOSITORY"
test "$(newswire_workflow_state)" = active
bounded_gh 60 workflow run "$NEWSWIRE_WORKFLOW" \
  --repo "$PALIMPSEST_REPOSITORY" --ref main
restore_newswire_workflow_freeze
test "$(newswire_workflow_state)" = disabled_manually

for _ in {1..60}; do
  bounded_gh 60 run list --repo "$PALIMPSEST_REPOSITORY" \
    --workflow "$NEWSWIRE_WORKFLOW" --limit 100 \
    --json databaseId,event,headSha,status,conclusion,workflowName \
    >"$NEWSWIRE_RUNS_AFTER"
  NEWSWIRE_RUN_ID="$(python3 - "$NEWSWIRE_RUNS_BEFORE" \
    "$NEWSWIRE_RUNS_AFTER" "$EXPECTED_NEWSWIRE_BASE_SHA" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    before = {run["databaseId"] for run in json.load(handle)}
with open(sys.argv[2], encoding="utf-8") as handle:
    after = json.load(handle)
new = [run for run in after if run.get("databaseId") not in before]
if any(run.get("event") == "schedule" for run in new):
    raise SystemExit("a scheduled Newswire run raced the controlled dispatch")
if len(new) > 1:
    raise SystemExit("more than one new Newswire run appeared")
if not new:
    raise SystemExit(0)
run = new[0]
if (
    run.get("event") != "workflow_dispatch"
    or run.get("headSha") != sys.argv[3]
    or run.get("workflowName") != "Refresh evidence wire"
):
    raise SystemExit("the new Newswire run is not the controlled dispatch")
print(run["databaseId"])
PY
)"
  [[ -z "$NEWSWIRE_RUN_ID" ]] || break
  sleep 2
done
[[ "$NEWSWIRE_RUN_ID" =~ ^[0-9]+$ ]]
test "$(bounded_gh 60 run view "$NEWSWIRE_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json event --jq .event)" \
  = workflow_dispatch
test "$(bounded_gh 60 run view "$NEWSWIRE_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json headBranch --jq .headBranch)" = main
test "$(bounded_gh 60 run view "$NEWSWIRE_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json headSha --jq .headSha)" \
  = "$EXPECTED_NEWSWIRE_BASE_SHA"
bounded_gh 5700 run watch "$NEWSWIRE_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --exit-status
test "$(bounded_gh 60 run view "$NEWSWIRE_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json conclusion --jq .conclusion)" = success
bounded_gh 60 run view "$NEWSWIRE_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json startedAt,updatedAt \
  >"$NEWSWIRE_RUN_METADATA"
# Re-list after completion so a late-delivered scheduled event cannot hide
# behind the already selected manual run.
bounded_gh 60 run list --repo "$PALIMPSEST_REPOSITORY" \
  --workflow "$NEWSWIRE_WORKFLOW" --limit 100 \
  --json databaseId,event,headSha,status,conclusion,workflowName \
  >"$NEWSWIRE_RUNS_AFTER"
test "$(python3 - "$NEWSWIRE_RUNS_BEFORE" "$NEWSWIRE_RUNS_AFTER" \
  "$NEWSWIRE_RUN_ID" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    before = {run["databaseId"] for run in json.load(handle)}
with open(sys.argv[2], encoding="utf-8") as handle:
    after = json.load(handle)
new = [run for run in after if run.get("databaseId") not in before]
if len(new) != 1 or str(new[0].get("databaseId")) != sys.argv[3]:
    raise SystemExit("Newswire run set changed after controlled selection")
if new[0].get("event") != "workflow_dispatch":
    raise SystemExit("controlled Newswire run was replaced by another event")
print(new[0]["databaseId"])
PY
)" = "$NEWSWIRE_RUN_ID"
NEWSWIRE_RUN_ATTEMPT="$(bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/actions/runs/$NEWSWIRE_RUN_ID" \
  --jq .run_attempt)"
[[ "$NEWSWIRE_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]]

NEWSWIRE_PUBLICATION_SHA="$(bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/commits/main" --jq .sha)"
[[ "$NEWSWIRE_PUBLICATION_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$NEWSWIRE_PUBLICATION_SHA" != "$EXPECTED_NEWSWIRE_BASE_SHA"
test "$(bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/compare/${EXPECTED_NEWSWIRE_BASE_SHA}...${NEWSWIRE_PUBLICATION_SHA}" \
  --jq .status)" = ahead
test "$(bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/compare/${EXPECTED_NEWSWIRE_BASE_SHA}...${NEWSWIRE_PUBLICATION_SHA}" \
  --jq .ahead_by)" = 1
test "$(bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/commits/$NEWSWIRE_PUBLICATION_SHA" \
  --jq '[.parents[].sha] | join(",")')" = "$EXPECTED_NEWSWIRE_BASE_SHA"

# Bind the output to one bot-authored commit created inside the selected run.
# Contents metadata is queried directly because a commit's `.files` array is
# capped and cannot prove that both required blobs changed.
bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/commits/$NEWSWIRE_PUBLICATION_SHA" \
  >"$NEWSWIRE_COMMIT_JSON"
bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/contents/readings/newswire-latest.json?ref=$EXPECTED_NEWSWIRE_BASE_SHA" \
  >"$BASE_NEWSWIRE_METADATA"
bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/contents/readings/china-situation-latest.json?ref=$EXPECTED_NEWSWIRE_BASE_SHA" \
  >"$BASE_SITUATION_METADATA"
bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/contents/readings/newswire-latest.json?ref=$NEWSWIRE_PUBLICATION_SHA" \
  >"$OUTPUT_NEWSWIRE_METADATA"
bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/contents/readings/china-situation-latest.json?ref=$NEWSWIRE_PUBLICATION_SHA" \
  >"$OUTPUT_SITUATION_METADATA"
python3 - "$NEWSWIRE_COMMIT_JSON" "$NEWSWIRE_RUN_METADATA" \
  "$BASE_NEWSWIRE_METADATA" "$OUTPUT_NEWSWIRE_METADATA" \
  "$BASE_SITUATION_METADATA" "$OUTPUT_SITUATION_METADATA" \
  "$NEWSWIRE_COMMIT_PROOF" "$EXPECTED_NEWSWIRE_BASE_SHA" \
  "$NEWSWIRE_PUBLICATION_SHA" <<'PY'
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load(path):
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SystemExit(f"GitHub evidence is not an object: {Path(path).name}")
    return value


def timestamp(value, label):
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ) is None:
        raise SystemExit(f"{label} is not a strict UTC timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


commit = load(sys.argv[1])
run = load(sys.argv[2])
base_sha = sys.argv[8]
publication_sha = sys.argv[9]
parents = commit.get("parents")
if commit.get("sha") != publication_sha or not isinstance(parents, list):
    raise SystemExit("Newswire commit identity is invalid")
if [parent.get("sha") for parent in parents if isinstance(parent, dict)] != [base_sha]:
    raise SystemExit("Newswire publication is not the one direct child of base")
commit_record = commit.get("commit")
if not isinstance(commit_record, dict):
    raise SystemExit("Newswire commit record is invalid")
message = commit_record.get("message")
match = re.fullmatch(
    r"data: evidence wire refresh \(([0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z)\) \[skip pytest\]",
    message if isinstance(message, str) else "",
)
if match is None:
    raise SystemExit("Newswire commit message is not the controlled data refresh")
commit_at = timestamp(match.group(1), "Newswire commit message clock")
for role in ("author", "committer"):
    identity = commit_record.get(role)
    if not isinstance(identity, dict):
        raise SystemExit(f"Newswire {role} identity is missing")
    if identity.get("name") != "palimpsest-bot" or identity.get("email") != "bot@palimpsest.info":
        raise SystemExit(f"Newswire {role} is not the publication bot")
    if timestamp(identity.get("date"), f"Newswire {role} clock") != commit_at:
        raise SystemExit(f"Newswire {role} clock does not match the commit message")
run_started = timestamp(run.get("startedAt"), "Newswire run start")
run_completed = timestamp(run.get("updatedAt"), "Newswire run completion")
if run_completed < run_started or not (
    run_started <= commit_at <= run_completed + timedelta(minutes=5)
):
    raise SystemExit("Newswire commit clock falls outside the selected run")

paths = (
    ("newswire", load(sys.argv[3]), load(sys.argv[4])),
    ("situation", load(sys.argv[5]), load(sys.argv[6])),
)
blob_proof = {}
for label, before, after in paths:
    for version, value in (("before", before), ("after", after)):
        if value.get("type") != "file":
            raise SystemExit(f"{label} {version} Git object is not a file")
        digest = value.get("sha")
        size = value.get("size")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{40}", digest) is None:
            raise SystemExit(f"{label} {version} blob identity is invalid")
        if type(size) is not int or not 1 <= size <= 12 * 1024 * 1024:
            raise SystemExit(f"{label} {version} blob size is invalid")
    if before["sha"] == after["sha"]:
        raise SystemExit(f"Newswire transaction did not change the {label} blob")
    blob_proof[label] = {
        "before_sha": before["sha"],
        "after_sha": after["sha"],
    }

proof = {
    "blobs": blob_proof,
    "commit_at": commit_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "run_completed_at": run_completed.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "run_started_at": run_started.strftime("%Y-%m-%dT%H:%M:%SZ"),
}
Path(sys.argv[7]).write_text(
    json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY

# The scrubbed acquisition artifact must exist and carry the exact fetched
# Newswire bytes. Its archive digest binds GitHub's immutable run artifact to
# the output commit independently of the workflow conclusion.
bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/actions/runs/$NEWSWIRE_RUN_ID/artifacts?per_page=100" \
  >"$NEWSWIRE_ARTIFACTS_JSON"
python3 - "$NEWSWIRE_ARTIFACTS_JSON" "$NEWSWIRE_ARTIFACT_PROOF" \
  "$EXPECTED_NEWSWIRE_BASE_SHA" "$NEWSWIRE_RUN_ID" \
  "$NEWSWIRE_RUN_ATTEMPT" <<'PY'
import json
import re
import sys
from pathlib import Path

with Path(sys.argv[1]).open(encoding="utf-8") as handle:
    response = json.load(handle)
artifacts = response.get("artifacts") if isinstance(response, dict) else None
if not isinstance(artifacts, list):
    raise SystemExit("Newswire artifact inventory is invalid")
expected_name = f"newswire-acquisition-{sys.argv[3]}-{sys.argv[4]}-{sys.argv[5]}"
matches = [item for item in artifacts if item.get("name") == expected_name]
if len(matches) != 1 or not isinstance(matches[0], dict):
    raise SystemExit("exact Newswire acquisition artifact is not unique")
artifact = matches[0]
workflow_run = artifact.get("workflow_run")
digest = artifact.get("digest")
checks = (
    artifact.get("expired") is False,
    type(artifact.get("id")) is int and artifact["id"] >= 1,
    type(artifact.get("size_in_bytes")) is int
    and 1 <= artifact["size_in_bytes"] <= 16 * 1024 * 1024,
    isinstance(digest, str)
    and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None,
    isinstance(workflow_run, dict),
    workflow_run.get("id") == int(sys.argv[4]),
    workflow_run.get("head_branch") == "main",
    workflow_run.get("head_sha") == sys.argv[3],
)
if not all(checks):
    raise SystemExit("Newswire acquisition artifact identity is invalid")
proof = {
    "digest": digest.removeprefix("sha256:"),
    "id": artifact["id"],
    "name": expected_name,
    "size_in_bytes": artifact["size_in_bytes"],
}
Path(sys.argv[2]).write_text(
    json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
NEWSWIRE_ARTIFACT_ID="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["id"])' \
  "$NEWSWIRE_ARTIFACT_PROOF")"
NEWSWIRE_ARTIFACT_SHA256="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["digest"])' \
  "$NEWSWIRE_ARTIFACT_PROOF")"
[[ "$NEWSWIRE_ARTIFACT_ID" =~ ^[1-9][0-9]*$ ]]
[[ "$NEWSWIRE_ARTIFACT_SHA256" =~ ^[0-9a-f]{64}$ ]]
bounded_gh 120 api \
  "repos/$PALIMPSEST_REPOSITORY/actions/artifacts/$NEWSWIRE_ARTIFACT_ID/zip" \
  >"$NEWSWIRE_ARTIFACT_ZIP"
python3 - "$NEWSWIRE_ARTIFACT_ZIP" "$NEWSWIRE_ARTIFACT_SHA256" \
  "$NEWSWIRE_ARTIFACT_JSON" "$NEWSWIRE_ARTIFACT_VERSIONS" <<'PY'
import hashlib
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

archive_path = Path(sys.argv[1])
archive = archive_path.read_bytes()
if not 1 <= len(archive) <= 16 * 1024 * 1024:
    raise SystemExit("Newswire artifact archive size is invalid")
if hashlib.sha256(archive).hexdigest() != sys.argv[2]:
    raise SystemExit("Newswire artifact archive digest is invalid")
with zipfile.ZipFile(archive_path) as bundle:
    members = bundle.infolist()
    if len(members) != 2:
        raise SystemExit("Newswire artifact member set is invalid")
    by_basename = {}
    for member in members:
        path = PurePosixPath(member.filename)
        if (
            path.is_absolute()
            or ".." in path.parts
            or member.is_dir()
            or stat.S_ISLNK(member.external_attr >> 16)
        ):
            raise SystemExit("Newswire artifact member path is unsafe")
        if path.name in by_basename:
            raise SystemExit("Newswire artifact member basename is duplicated")
        by_basename[path.name] = member
    expected = {"newswire-latest.json", "newswire-versions.jsonl"}
    if set(by_basename) != expected:
        raise SystemExit("Newswire artifact filenames are invalid")
    latest = bundle.read(by_basename["newswire-latest.json"])
    versions = bundle.read(by_basename["newswire-versions.jsonl"])
if not 1 <= len(latest) <= 12 * 1024 * 1024:
    raise SystemExit("artifact Newswire document size is invalid")
if not 1 <= len(versions) <= 32 * 1024 * 1024:
    raise SystemExit("artifact Newswire history size is invalid")
latest.decode("utf-8", "strict")
versions.decode("utf-8", "strict")
Path(sys.argv[3]).write_bytes(latest)
Path(sys.argv[4]).write_bytes(versions)
PY

bounded_gh 60 api -H 'Accept: application/vnd.github.raw+json' \
  "repos/$PALIMPSEST_REPOSITORY/contents/readings/newswire-latest.json?ref=$NEWSWIRE_PUBLICATION_SHA" \
  >"$NEWSWIRE_JSON"
bounded_gh 60 api -H 'Accept: application/vnd.github.raw+json' \
  "repos/$PALIMPSEST_REPOSITORY/contents/readings/china-situation-latest.json?ref=$NEWSWIRE_PUBLICATION_SHA" \
  >"$SITUATION_JSON"
cmp --silent "$NEWSWIRE_ARTIFACT_JSON" "$NEWSWIRE_JSON"

for _ in {1..90}; do
  bounded_gh 60 run list --repo "$PALIMPSEST_REPOSITORY" \
    --workflow "$PUBLICATION_WORKFLOW" --event repository_dispatch --limit 100 \
    --json databaseId,event,headSha,status,conclusion,workflowName \
    >"$CONTRACT_RUNS_AFTER"
  PUBLICATION_CONTRACT_RUN_ID="$(python3 - "$CONTRACT_RUNS_BEFORE" \
    "$CONTRACT_RUNS_AFTER" "$NEWSWIRE_PUBLICATION_SHA" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    before = {run["databaseId"] for run in json.load(handle)}
with open(sys.argv[2], encoding="utf-8") as handle:
    after = json.load(handle)
candidates = [
    run["databaseId"] for run in after
    if run.get("databaseId") not in before
    and run.get("event") == "repository_dispatch"
    and run.get("headSha") == sys.argv[3]
    and run.get("workflowName") == "Tests"
]
if len(candidates) > 1:
    raise SystemExit("more than one publication contract matches the Newswire commit")
if candidates:
    print(candidates[0])
PY
)"
  [[ -z "$PUBLICATION_CONTRACT_RUN_ID" ]] || break
  sleep 2
done
[[ "$PUBLICATION_CONTRACT_RUN_ID" =~ ^[0-9]+$ ]]
test "$(bounded_gh 60 run view "$PUBLICATION_CONTRACT_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json event --jq .event)" \
  = repository_dispatch
test "$(bounded_gh 60 run view "$PUBLICATION_CONTRACT_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json headBranch --jq .headBranch)" = main
test "$(bounded_gh 60 run view "$PUBLICATION_CONTRACT_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json headSha --jq .headSha)" \
  = "$NEWSWIRE_PUBLICATION_SHA"
bounded_gh 5400 run watch "$PUBLICATION_CONTRACT_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --exit-status
test "$(bounded_gh 60 run view "$PUBLICATION_CONTRACT_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json conclusion --jq .conclusion)" = success
PUBLICATION_CONTRACT_RUN_ATTEMPT="$(bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/actions/runs/$PUBLICATION_CONTRACT_RUN_ID" \
  --jq .run_attempt)"
[[ "$PUBLICATION_CONTRACT_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]]
bounded_gh 60 run view "$PUBLICATION_CONTRACT_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json jobs >"$PUBLICATION_JOBS"
python3 - "$PUBLICATION_JOBS" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
jobs = document.get("jobs") if isinstance(document, dict) else None
if not isinstance(jobs, list):
    raise SystemExit("publication contract jobs are invalid")
observed = {}
for job in jobs:
    if not isinstance(job, dict) or not isinstance(job.get("name"), str):
        raise SystemExit("publication contract job is invalid")
    if job["name"] in observed:
        raise SystemExit("publication contract job name is duplicated")
    observed[job["name"]] = job.get("conclusion")
expected = {
    "pytest": "success",
    "contract": "success",
    "Admit the exact tested publication": "success",
    "Admit exact deployed MCP release before Pages": "success",
    "Package exact complete Pages edition": "success",
    "Deploy exact complete Pages edition": "success",
    "Deploy and prove exact Railway publication": "skipped",
    "Verify exact Pages and native MCP rights closure": "success",
}
for name, conclusion in expected.items():
    if observed.get(name) != conclusion:
        raise SystemExit(f"publication contract job is not exact: {name}")
PY
test "$(bounded_gh 60 api \
  "repos/$PALIMPSEST_REPOSITORY/commits/main" --jq .sha)" \
  = "$NEWSWIRE_PUBLICATION_SHA"
# Action-token publication pushes intentionally suppress push workflows. The
# exact repository_dispatch above is the sole output-commit acceptance run.
OUTPUT_PUSH_RUNS="$NEWSWIRE_TMP_DIR/output-push-runs.json"
bounded_gh 60 run list --repo "$PALIMPSEST_REPOSITORY" \
  --workflow "$PUBLICATION_WORKFLOW" --event push --limit 100 \
  --json databaseId,event,headSha,workflowName >"$OUTPUT_PUSH_RUNS"
python3 - "$OUTPUT_PUSH_RUNS" "$NEWSWIRE_PUBLICATION_SHA" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    runs = json.load(handle)
if any(
    run.get("event") == "push"
    and run.get("headSha") == sys.argv[2]
    and run.get("workflowName") == "Tests"
    for run in runs
):
    raise SystemExit("unexpected push Tests run exists for the Action-token commit")
PY
test "$(bounded_gh 60 variable get RAILWAY_PUBLICATION_ENABLED \
  --repo "$PALIMPSEST_REPOSITORY")" = false
test "$(newswire_workflow_state)" = disabled_manually

python3 - "$NEWSWIRE_JSON" "$SITUATION_JSON" "$RECEIPT_TMP" \
  "$PALIMPSEST_REPOSITORY" "$EXPECTED_NEWSWIRE_BASE_SHA" \
  "$BASE_PUSH_RUN_ID" "$NEWSWIRE_RUN_ID" "$NEWSWIRE_RUN_ATTEMPT" \
  "$NEWSWIRE_PUBLICATION_SHA" "$PUBLICATION_CONTRACT_RUN_ID" \
  "$PUBLICATION_CONTRACT_RUN_ATTEMPT" "$NEWSWIRE_COMMIT_PROOF" \
  "$NEWSWIRE_ARTIFACT_PROOF" <<'PY'
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.china_situation import validate_china_situation
from core.newswire import canonical_json_bytes, validate_newswire_document


def reject_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def load_document(path_text, maximum_bytes):
    path = Path(path_text)
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"release document is not a regular file: {path.name}")
    raw = path.read_bytes()
    if not 1 <= len(raw) <= maximum_bytes:
        raise SystemExit(f"release document has invalid size: {path.name}")
    document = json.loads(
        raw.decode("utf-8", "strict"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    return raw, document


def strict_utc(value, label):
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ) is None:
        raise SystemExit(f"{label} is not a strict UTC timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


newswire_raw, newswire = load_document(sys.argv[1], 12 * 1024 * 1024)
situation_raw, situation = load_document(sys.argv[2], 12 * 1024 * 1024)
validate_newswire_document(newswire)
validate_china_situation(situation)
newswire_canonical_sha256 = hashlib.sha256(canonical_json_bytes(newswire)).hexdigest()
situation_canonical_sha256 = hashlib.sha256(
    canonical_json_bytes(situation)
).hexdigest()
if situation["inputs"]["newswire_generated_at"] != newswire["generated_at"]:
    raise SystemExit("situation Newswire clock does not match the exact wire")
if situation["inputs"]["newswire_sha256"] != newswire_canonical_sha256:
    raise SystemExit("situation Newswire digest does not match the exact wire")
now = datetime.now(timezone.utc)
newswire_at = strict_utc(newswire["generated_at"], "Newswire generated_at")
situation_at = strict_utc(situation["generated_at"], "situation generated_at")
for label, observed in (("Newswire", newswire_at), ("situation", situation_at)):
    if observed > now + timedelta(minutes=5):
        raise SystemExit(f"{label} clock is implausibly in the future")
    if now - observed > timedelta(hours=2):
        raise SystemExit(f"{label} clock exceeds the two-hour activation window")
if situation_at < newswire_at:
    raise SystemExit("situation synthesis predates its Newswire input")
if newswire["n_items"] <= 0 or newswire["n_events"] <= 0:
    raise SystemExit("Newswire activation edition is empty")
if newswire["coverage"].get("accepted_items") != newswire["n_items"]:
    raise SystemExit("Newswire coverage count does not match items")
if situation["coverage"].get("wire_events") != newswire["n_events"]:
    raise SystemExit("situation wire-event count does not match Newswire")
if situation["coverage"].get("in_scope_events") != len(situation["situations"]):
    raise SystemExit("situation coverage count does not match situations")
with Path(sys.argv[12]).open(encoding="utf-8") as handle:
    commit_proof = json.load(handle)
with Path(sys.argv[13]).open(encoding="utf-8") as handle:
    artifact_proof = json.load(handle)
if not isinstance(commit_proof, dict) or set(commit_proof) != {
    "blobs", "commit_at", "run_completed_at", "run_started_at"
}:
    raise SystemExit("Newswire commit proof changed shape before receipt")
blobs = commit_proof.get("blobs")
if not isinstance(blobs, dict) or set(blobs) != {"newswire", "situation"}:
    raise SystemExit("Newswire blob proof changed shape before receipt")
for value in blobs.values():
    if not isinstance(value, dict) or set(value) != {"before_sha", "after_sha"}:
        raise SystemExit("Newswire blob identity proof is invalid")
    if any(
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{40}", digest) is None
        for digest in value.values()
    ) or value["before_sha"] == value["after_sha"]:
        raise SystemExit("Newswire blob identity proof is invalid")
for field in ("commit_at", "run_completed_at", "run_started_at"):
    strict_utc(commit_proof.get(field), f"commit proof {field}")
if not isinstance(artifact_proof, dict) or set(artifact_proof) != {
    "digest", "id", "name", "size_in_bytes"
}:
    raise SystemExit("Newswire artifact proof changed shape before receipt")
if (
    type(artifact_proof.get("id")) is not int
    or artifact_proof["id"] < 1
    or type(artifact_proof.get("size_in_bytes")) is not int
    or not 1 <= artifact_proof["size_in_bytes"] <= 16 * 1024 * 1024
    or not isinstance(artifact_proof.get("digest"), str)
    or re.fullmatch(r"[0-9a-f]{64}", artifact_proof["digest"]) is None
    or artifact_proof.get("name")
    != f"newswire-acquisition-{sys.argv[5]}-{sys.argv[7]}-{sys.argv[8]}"
):
    raise SystemExit("Newswire artifact proof is invalid before receipt")

receipt = {
    "base_push_run_id": int(sys.argv[6]),
    "base_sha": sys.argv[5],
    "created_at": now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "hourly_publication_enabled": False,
    "newswire": {
        "acquisition_artifact": artifact_proof,
        "canonical_sha256": newswire_canonical_sha256,
        "commit": commit_proof,
        "generated_at": newswire["generated_at"],
        "raw_sha256": hashlib.sha256(newswire_raw).hexdigest(),
        "run_attempt": int(sys.argv[8]),
        "run_id": int(sys.argv[7]),
    },
    "publication_contract": {
        "run_attempt": int(sys.argv[11]),
        "run_id": int(sys.argv[10]),
    },
    "publication_sha": sys.argv[9],
    "repository": sys.argv[4],
    "schema_version": "palimpsest.newswire-activation-prerequisite.v1",
    "situation": {
        "canonical_sha256": situation_canonical_sha256,
        "generated_at": situation["generated_at"],
        "inputs": {
            "newswire_canonical_sha256": situation["inputs"]["newswire_sha256"],
            "newswire_generated_at": situation["inputs"]["newswire_generated_at"],
        },
        "raw_sha256": hashlib.sha256(situation_raw).hexdigest(),
    },
    "workflow_state": "disabled_manually",
}
if not re.fullmatch(r"[0-9a-f]{40}", receipt["base_sha"]):
    raise SystemExit("receipt base SHA is invalid")
if not re.fullmatch(r"[0-9a-f]{40}", receipt["publication_sha"]):
    raise SystemExit("receipt publication SHA is invalid")
Path(sys.argv[3]).write_bytes(
    json.dumps(
        receipt,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    + b"\n"
)
PY
chmod 0600 "$RECEIPT_TMP"
begin_newswire_receipt_commit
NEWSWIRE_RECEIPT_COMMIT_STATUS=0
python3 - "$RECEIPT_TMP" "$NEWSWIRE_PREREQUISITE_RECEIPT" <<'PY' \
  || NEWSWIRE_RECEIPT_COMMIT_STATUS=$?
import os
from pathlib import Path
import stat
import sys


source = Path(sys.argv[1])
target = Path(sys.argv[2])
source_stat = source.lstat()
if source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
    raise SystemExit("Newswire receipt candidate is not a regular file")
raw = source.read_bytes()
if not 1 <= len(raw) <= 256 * 1024:
    raise SystemExit("Newswire receipt candidate has an invalid size")

flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = None
created_identity = None
try:
    descriptor = os.open(target, flags, 0o600)
    created = os.fstat(descriptor)
    created_identity = (created.st_dev, created.st_ino)
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while committing Newswire receipt")
        view = view[written:]
    os.fsync(descriptor)
    os.close(descriptor)
    descriptor = None
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    if descriptor is not None:
        os.close(descriptor)
    if created_identity is not None:
        try:
            observed = target.lstat()
            if (
                stat.S_ISREG(observed.st_mode)
                and (observed.st_dev, observed.st_ino) == created_identity
            ):
                target.unlink()
        except FileNotFoundError:
            pass
    raise
PY
if (( NEWSWIRE_RECEIPT_COMMIT_STATUS != 0 )); then
  restore_newswire_receipt_signal_handlers
  exit "$NEWSWIRE_RECEIPT_COMMIT_STATUS"
fi
finish_newswire_receipt_commit
if ! printf 'NEWSWIRE_PUBLICATION_SHA=%s\n' "$NEWSWIRE_PUBLICATION_SHA"; then
  printf 'Newswire receipt committed; stdout reporting failed\n' >&2
fi
if ! printf 'NEWSWIRE_PREREQUISITE_RECEIPT=%s\n' \
    "$NEWSWIRE_PREREQUISITE_RECEIPT"; then
  printf 'Newswire receipt committed; stdout reporting failed\n' >&2
fi
exit 0
