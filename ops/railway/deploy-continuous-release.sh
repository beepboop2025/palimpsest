#!/usr/bin/env bash
# Publish one admitted Palimpsest static edition to Railway as a guarded,
# single-submission transaction. GitHub Actions owns the Railway credential;
# Hetzner never does.

set -Eeuo pipefail
umask 077

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_directory/../.." && pwd -P)"
python_runtime="${PALIMPSEST_RAILWAY_PYTHON:-python3}"

# The workflow has a 55-minute ceiling. Normal work stops before the global
# deadline so the final interval remains available for recovery.
transaction_seconds="${PALIMPSEST_RAILWAY_TRANSACTION_SECONDS:-3000}"
rollback_reserve_seconds="${PALIMPSEST_RAILWAY_ROLLBACK_RESERVE_SECONDS:-900}"
minimum_mutation_seconds="${PALIMPSEST_RAILWAY_MINIMUM_MUTATION_SECONDS:-600}"
command_timeout_seconds="${PALIMPSEST_RAILWAY_COMMAND_TIMEOUT_SECONDS:-90}"
verifier_timeout_seconds="${PALIMPSEST_RAILWAY_VERIFIER_TIMEOUT_SECONDS:-300}"
mcp_timeout_seconds="${PALIMPSEST_RAILWAY_MCP_TIMEOUT_SECONDS:-180}"
rollback_command_timeout_seconds="${PALIMPSEST_RAILWAY_ROLLBACK_COMMAND_TIMEOUT_SECONDS:-30}"
rollback_cancel_timeout_seconds="${PALIMPSEST_RAILWAY_ROLLBACK_CANCEL_TIMEOUT_SECONDS:-180}"
rollback_restore_timeout_seconds="${PALIMPSEST_RAILWAY_ROLLBACK_RESTORE_TIMEOUT_SECONDS:-300}"
predecessor_max_age_seconds="${PALIMPSEST_RAILWAY_PREDECESSOR_MAX_AGE_SECONDS:-31536000}"
candidate_max_age_seconds="${PALIMPSEST_RAILWAY_CANDIDATE_MAX_AGE_SECONDS:-86400}"
discovery_interval_seconds="${PALIMPSEST_RAILWAY_DISCOVERY_INTERVAL_SECONDS:-5}"
poll_interval_seconds="${PALIMPSEST_RAILWAY_POLL_INTERVAL_SECONDS:-10}"
timeout_kill_grace_seconds=5

transaction_started_epoch="$(date +%s)"
transaction_deadline_epoch=0
mutation_deadline_epoch=0
transaction_receipt=""
verification_receipt=""
transaction_terminal=false
transaction_phase=initialization
transaction_result=in_progress
failure_reason=""
submission_state=not_started
deployment_mode=none
deployment_terminal_status=""
candidate_observed_status=""
upload_exit_code=""
upload_message=""
upload_reported_deployment_id=""
previous_deployment_id=""
previous_image_digest=""
previous_deployment_reason=""
previous_source_sha=""
previous_tree_sha256=""
previous_manifest_sha256=""
candidate_tree_sha256=""
candidate_manifest_sha256=""
new_deployment_id=""
new_image_digest=""
new_deployment_reason=""
rollback_result=not_required
rollback_target_deployment_id=""
rollback_restored_deployment_id=""
rollback_restored_image_digest=""
rollback_restored_reason=""
rollback_provisional_deployment_id=""
rollback_provisional_image_digest=""
rollback_provisional_reason=""
rollback_in_progress=false
reconciliation_started=false
transaction_finalizing=false
verification_receipt_sha256=""
mcp_rights_smoke=not_run
received_signal=""

fail() {
  printf '%s\n' "$*" >&2
  return 1
}

require_integer() {
  local name="$1" value="$2" minimum="$3" maximum="$4"
  if [[ ! "$value" =~ ^[0-9]+$ ]] \
    || (( value < minimum || value > maximum )); then
    fail "$name must be an integer from $minimum through $maximum"
  fi
}

now_epoch() {
  date +%s
}

remaining_seconds() {
  local deadline="$1" now
  now="$(now_epoch)" || return 1
  if (( now >= deadline )); then
    printf '0\n'
  else
    printf '%s\n' "$((deadline - now))"
  fi
}

bounded_run() {
  local deadline="$1" ceiling="$2"
  shift 2
  local remaining limit
  remaining="$(remaining_seconds "$deadline")" || return 125
  (( remaining > 0 )) || return 124
  limit="$remaining"
  if (( ceiling < limit )); then
    limit="$ceiling"
  fi
  timeout --signal=TERM --kill-after="${timeout_kill_grace_seconds}s" "${limit}s" "$@"
}

bounded_sleep() {
  local deadline="$1" requested="$2" remaining
  remaining="$(remaining_seconds "$deadline")" || return 1
  (( remaining > 0 )) || return 1
  if (( requested > remaining )); then
    requested="$remaining"
  fi
  (( requested == 0 )) || sleep "$requested"
}

deadline_after() {
  local allowance="$1" hard_deadline="$2" now proposed
  now="$(now_epoch)" || return 1
  proposed=$((now + allowance))
  if (( proposed > hard_deadline )); then
    proposed="$hard_deadline"
  fi
  printf '%s\n' "$proposed"
}

write_transaction_receipt() {
  [[ -n "$transaction_receipt" ]] || return 0
  TX_PHASE="$transaction_phase" \
  TX_RESULT="$transaction_result" \
  TX_FAILURE_REASON="$failure_reason" \
  TX_SUBMISSION_STATE="$submission_state" \
  TX_MODE="$deployment_mode" \
  TX_TERMINAL_STATUS="$deployment_terminal_status" \
  TX_CANDIDATE_OBSERVED_STATUS="$candidate_observed_status" \
  TX_UPLOAD_EXIT_CODE="$upload_exit_code" \
  TX_UPLOAD_MESSAGE="$upload_message" \
  TX_UPLOAD_REPORTED_DEPLOYMENT_ID="$upload_reported_deployment_id" \
  TX_PREVIOUS_DEPLOYMENT_ID="$previous_deployment_id" \
  TX_PREVIOUS_IMAGE_DIGEST="$previous_image_digest" \
  TX_PREVIOUS_DEPLOYMENT_REASON="$previous_deployment_reason" \
  TX_PREVIOUS_SOURCE_SHA="$previous_source_sha" \
  TX_PREVIOUS_TREE_SHA256="$previous_tree_sha256" \
  TX_PREVIOUS_MANIFEST_SHA256="$previous_manifest_sha256" \
  TX_CANDIDATE_TREE_SHA256="$candidate_tree_sha256" \
  TX_CANDIDATE_MANIFEST_SHA256="$candidate_manifest_sha256" \
  TX_NEW_DEPLOYMENT_ID="$new_deployment_id" \
  TX_NEW_IMAGE_DIGEST="$new_image_digest" \
  TX_NEW_DEPLOYMENT_REASON="$new_deployment_reason" \
  TX_ROLLBACK_RESULT="$rollback_result" \
  TX_ROLLBACK_TARGET_DEPLOYMENT_ID="$rollback_target_deployment_id" \
  TX_ROLLBACK_RESTORED_DEPLOYMENT_ID="$rollback_restored_deployment_id" \
  TX_ROLLBACK_RESTORED_IMAGE_DIGEST="$rollback_restored_image_digest" \
  TX_ROLLBACK_RESTORED_REASON="$rollback_restored_reason" \
  TX_VERIFICATION_RECEIPT_SHA256="$verification_receipt_sha256" \
  TX_MCP_RIGHTS_SMOKE="$mcp_rights_smoke" \
  TX_RECEIVED_SIGNAL="$received_signal" \
  TX_STARTED_EPOCH="$transaction_started_epoch" \
  TX_DEADLINE_EPOCH="$transaction_deadline_epoch" \
  TX_MUTATION_DEADLINE_EPOCH="$mutation_deadline_epoch" \
  TX_EXCLUSIVE_WRITER_ACK="$RAILWAY_EXCLUSIVE_WRITER_ACK" \
  TX_RECEIPT_PATH="$transaction_receipt" \
  "$python_runtime" -I -S - <<'PY'
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def optional(name: str) -> str | None:
    value = os.environ[name]
    return value or None


def optional_integer(name: str) -> int | None:
    value = os.environ[name]
    return int(value) if value else None


document = {
    "schema_version": "palimpsest.railway-continuous-transaction.v1",
    "status": os.environ["TX_RESULT"],
    "phase": os.environ["TX_PHASE"],
    "failure_reason": optional("TX_FAILURE_REASON"),
    "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "repository": os.environ.get("GITHUB_REPOSITORY") or None,
    "workflow": os.environ.get("GITHUB_WORKFLOW_REF") or None,
    "run_id": os.environ.get("GITHUB_RUN_ID") or None,
    "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT") or None,
    "publication_sha": os.environ["PUBLICATION_SHA"],
    "rights_admission_epoch": int(os.environ["RIGHTS_ADMISSION_EPOCH"]),
    "pages_rights_receipt_sha256": os.environ["PAGES_RIGHTS_RECEIPT_SHA256"],
    "deadline": {
        "started_epoch": optional_integer("TX_STARTED_EPOCH"),
        "mutation_deadline_epoch": optional_integer("TX_MUTATION_DEADLINE_EPOCH"),
        "transaction_deadline_epoch": optional_integer("TX_DEADLINE_EPOCH"),
    },
    "railway": {
        "project_id": os.environ["RAILWAY_PROJECT_ID"],
        "environment_id": os.environ["RAILWAY_ENVIRONMENT_ID"],
        "service_id": os.environ["RAILWAY_SERVICE_ID"],
        "deployment_mode": os.environ["TX_MODE"],
        "submission_state": os.environ["TX_SUBMISSION_STATE"],
        "terminal_status": optional("TX_TERMINAL_STATUS"),
        "candidate_observed_status": optional("TX_CANDIDATE_OBSERVED_STATUS"),
        "upload_exit_code": optional_integer("TX_UPLOAD_EXIT_CODE"),
        "upload_message": optional("TX_UPLOAD_MESSAGE"),
        "upload_reported_deployment_id": optional("TX_UPLOAD_REPORTED_DEPLOYMENT_ID"),
        "exclusive_writer_ack": os.environ["TX_EXCLUSIVE_WRITER_ACK"],
        "previous_deployment_id": optional("TX_PREVIOUS_DEPLOYMENT_ID"),
        "previous_image_digest": optional("TX_PREVIOUS_IMAGE_DIGEST"),
        "previous_deployment_reason": optional("TX_PREVIOUS_DEPLOYMENT_REASON"),
        "new_deployment_id": optional("TX_NEW_DEPLOYMENT_ID"),
        "new_image_digest": optional("TX_NEW_IMAGE_DIGEST"),
        "new_deployment_reason": optional("TX_NEW_DEPLOYMENT_REASON"),
    },
    "previous_release": {
        "source_commit": optional("TX_PREVIOUS_SOURCE_SHA"),
        "tree_sha256": optional("TX_PREVIOUS_TREE_SHA256"),
        "manifest_sha256": optional("TX_PREVIOUS_MANIFEST_SHA256"),
    },
    "candidate": {
        "tree_sha256": optional("TX_CANDIDATE_TREE_SHA256"),
        "manifest_sha256": optional("TX_CANDIDATE_MANIFEST_SHA256"),
    },
    "verification": {
        "receipt_sha256": optional("TX_VERIFICATION_RECEIPT_SHA256"),
        "mcp_rights_smoke": os.environ["TX_MCP_RIGHTS_SMOKE"],
    },
    "rollback": {
        "result": os.environ["TX_ROLLBACK_RESULT"],
        "target_deployment_id": optional("TX_ROLLBACK_TARGET_DEPLOYMENT_ID"),
        "restored_deployment_id": optional("TX_ROLLBACK_RESTORED_DEPLOYMENT_ID"),
        "restored_image_digest": optional("TX_ROLLBACK_RESTORED_IMAGE_DIGEST"),
        "restored_reason": optional("TX_ROLLBACK_RESTORED_REASON"),
    },
    "signal": optional("TX_RECEIVED_SIGNAL"),
}
payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
path = Path(os.environ["TX_RECEIPT_PATH"])
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
temporary = Path(temporary_name)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
PY
}

finalize_transaction_receipt() {
  local receipt_status
  transaction_finalizing=true
  if write_transaction_receipt; then
    receipt_status=0
  else
    receipt_status=$?
  fi
  # Set the terminal guard only after the atomic receipt writer has returned.
  # A signal delivered during that writer re-enters this idempotent finalizer
  # instead of starting a provider recovery transaction.
  transaction_terminal=true
  transaction_finalizing=false
  return "$receipt_status"
}

write_controller_provenance() {
  local destination="$RAILWAY_EVIDENCE_DIRECTORY/controller-provenance.json"
  PROVENANCE_DESTINATION="$destination" "$python_runtime" -I -S - <<'PY'
import json
import os
import stat
import tempfile
from pathlib import Path


allowed_payload = {
    "sha",
    "scope",
    "deploy_railway",
    "controller_run_id",
    "controller_run_attempt",
    "requested_at",
    "controller_artifact_id",
    "controller_artifact_digest",
    "controller_request_sha256",
}
payload = None
event_path_text = os.environ.get("GITHUB_EVENT_PATH", "")
if event_path_text:
    event_path = Path(event_path_text)
    try:
        metadata = event_path.lstat()
        if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and metadata.st_size <= 1024 * 1024:
            event = json.loads(event_path.read_text(encoding="utf-8"))
            candidate = event.get("client_payload") if isinstance(event, dict) else None
            if isinstance(candidate, dict):
                payload = {
                    key: candidate[key]
                    for key in sorted(allowed_payload)
                    if key in candidate
                }
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = None
document = {
    "schema_version": "palimpsest.railway-controller-provenance.v1",
    "repository": os.environ.get("GITHUB_REPOSITORY") or None,
    "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF") or None,
    "workflow_run_id": os.environ.get("GITHUB_RUN_ID") or None,
    "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT") or None,
    "event_name": os.environ.get("GITHUB_EVENT_NAME") or None,
    "dispatch": payload,
}
encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
path = Path(os.environ["PROVENANCE_DESTINATION"])
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
temporary = Path(temporary_name)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
PY
}

fetch_manifest() {
  local origin="$1" destination="$2" nonce="$3" deadline="$4"
  local ceiling="${5:-$command_timeout_seconds}"
  local temporary="${destination}.partial"
  rm -f -- "$temporary"
  if ! bounded_run "$deadline" "$ceiling" \
    curl --fail --silent --show-error --proto '=https' --tlsv1.2 \
      --connect-timeout 10 --max-time "$ceiling" \
      --max-filesize 2097152 --header 'Accept: application/json' \
      --header 'Accept-Encoding: identity' --header 'Cache-Control: no-cache' \
      "${origin%/}/railway-release.json?continuous_release=${nonce}" \
      --output "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  mv -f -- "$temporary" "$destination"
}

manifest_identity() {
  local manifest_path="$1"
  "$python_runtime" -I -S - "$manifest_path" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SystemExit("duplicate release-manifest key")
        result[key] = value
    return result


raw = Path(sys.argv[1]).read_bytes()
if not 1 <= len(raw) <= 2 * 1024 * 1024:
    raise SystemExit("release manifest has an invalid byte size")
document = json.loads(raw, object_pairs_hook=strict_object)
if document.get("schema_version") != "palimpsest.railway-static-release.v1":
    raise SystemExit("release manifest has the wrong schema")
if document.get("deployment_source") != "local-git-archive":
    raise SystemExit("release manifest has the wrong deployment source")
if document.get("github_required") is not False or document.get("state") != "artifact_ready":
    raise SystemExit("release manifest is not an admitted static artifact")
source = document.get("source_commit")
tree = document.get("tree_sha256")
if not isinstance(source, str) or re.fullmatch(r"[0-9a-f]{40}", source) is None:
    raise SystemExit("release manifest source commit is invalid")
if not isinstance(tree, str) or re.fullmatch(r"[0-9a-f]{64}", tree) is None:
    raise SystemExit("release manifest tree digest is invalid")
print(source, tree, hashlib.sha256(raw).hexdigest(), sep="\t")
PY
}

capture_topology() {
  local destination="$1" deadline="$2"
  local ceiling="${3:-$command_timeout_seconds}"
  local temporary="${destination}.partial"
  rm -f -- "$temporary"
  if ! bounded_run "$deadline" "$ceiling" railway status \
    --project "$RAILWAY_PROJECT_ID" --environment "$RAILWAY_ENVIRONMENT_ID" \
    --json > "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  mv -f -- "$temporary" "$destination"
}

topology_identity() {
  local status_path="$1" age_mode="$2" deadline="$3"
  local ceiling="${4:-$verifier_timeout_seconds}"
  bounded_run "$deadline" "$ceiling" env \
  TOPOLOGY_AGE_MODE="$age_mode" \
  PREDECESSOR_MAX_AGE_SECONDS="$predecessor_max_age_seconds" \
  CANDIDATE_MAX_AGE_SECONDS="$candidate_max_age_seconds" \
  PYTHONPATH="$repo_root" "$python_runtime" - "$status_path" <<'PY'
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from ops.railway.verify_continuous_release import (
    extract_latest_status_deployment,
    parse_status_topology,
)


raw = Path(sys.argv[1]).read_bytes()
mode = os.environ["TOPOLOGY_AGE_MODE"]
now = datetime.now(UTC)
if mode == "candidate":
    evidence = extract_latest_status_deployment(
        raw,
        expected_environment_id=os.environ["RAILWAY_ENVIRONMENT_ID"],
        expected_service_id=os.environ["RAILWAY_SERVICE_ID"],
        now=now,
        maximum_age_seconds=int(os.environ["CANDIDATE_MAX_AGE_SECONDS"]),
        future_skew_seconds=120,
    )
elif mode == "predecessor":
    def strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SystemExit("duplicate predecessor topology key")
            result[key] = value
        return result

    if not 1 <= len(raw) <= 2 * 1024 * 1024:
        raise SystemExit("predecessor topology has an invalid byte size")
    document = json.loads(raw, object_pairs_hook=strict_object)
    environments = (document.get("environments") or {}).get("edges")
    if not isinstance(environments, list) or len(environments) > 100:
        raise SystemExit("predecessor topology environments are invalid")
    environment_matches = [
        edge.get("node")
        for edge in environments
        if isinstance(edge, dict)
        and isinstance(edge.get("node"), dict)
        and edge["node"].get("id") == os.environ["RAILWAY_ENVIRONMENT_ID"]
    ]
    if len(environment_matches) != 1:
        raise SystemExit("predecessor topology environment is not singular")
    instances = (environment_matches[0].get("serviceInstances") or {}).get("edges")
    if not isinstance(instances, list) or len(instances) > 100:
        raise SystemExit("predecessor topology service instances are invalid")
    instance_matches = [
        edge.get("node")
        for edge in instances
        if isinstance(edge, dict)
        and isinstance(edge.get("node"), dict)
        and edge["node"].get("serviceId") == os.environ["RAILWAY_SERVICE_ID"]
    ]
    if len(instance_matches) != 1:
        raise SystemExit("predecessor topology service is not singular")
    latest = instance_matches[0].get("latestDeployment")
    if not isinstance(latest, dict):
        raise SystemExit("predecessor topology latest deployment is missing")
    deployment_id = latest.get("id")
    metadata = latest.get("meta")
    if (
        not isinstance(deployment_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            deployment_id,
        )
        is None
        or not isinstance(metadata, dict)
    ):
        raise SystemExit("predecessor topology identity is invalid")
    image_digest = metadata.get("imageDigest")
    reason = metadata.get("reason")
    if (
        not isinstance(image_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
        or reason not in {"deploy", "deploymentRollback", "rollback"}
    ):
        raise SystemExit("predecessor topology deployment metadata is invalid")
    created_at_text = latest.get("createdAt")
    if not isinstance(created_at_text, str) or len(created_at_text) > 64:
        raise SystemExit("predecessor topology clock is invalid")
    created_at = datetime.fromisoformat(created_at_text.replace("Z", "+00:00"))
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise SystemExit("predecessor topology clock has no timezone")
    created_at = created_at.astimezone(UTC)
    age_seconds = int(os.environ["PREDECESSOR_MAX_AGE_SECONDS"])
    if created_at.timestamp() > now.timestamp() + 120:
        raise SystemExit("predecessor topology clock is in the future")
    if now.timestamp() - created_at.timestamp() > age_seconds:
        raise SystemExit("predecessor topology is older than the bounded policy")
    evidence = SimpleNamespace(
        deployment_id=deployment_id,
        image_digest=image_digest,
        reason=reason,
    )
else:
    raise SystemExit("unknown topology age mode")
parse_status_topology(
    raw,
    expected_project_id=os.environ["RAILWAY_PROJECT_ID"],
    expected_environment_id=os.environ["RAILWAY_ENVIRONMENT_ID"],
    expected_service_id=os.environ["RAILWAY_SERVICE_ID"],
    expected_deployment_id=evidence.deployment_id,
    expected_image_digest=evidence.image_digest,
    expected_deployment_reason=evidence.reason,
)
print(evidence.deployment_id, evidence.image_digest, evidence.reason, sep="\t")
PY
}

assert_current_main() {
  local deadline="$1" require_live_ancestry="$2"
  if ! bounded_run "$deadline" "$command_timeout_seconds" git -C "$repo_root" \
    fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main; then
    return 1
  fi
  local checkout_sha current_main_sha
  checkout_sha="$(bounded_run "$deadline" "$command_timeout_seconds" \
    git -C "$repo_root" rev-parse HEAD)" || return 1
  current_main_sha="$(bounded_run "$deadline" "$command_timeout_seconds" \
    git -C "$repo_root" rev-parse refs/remotes/origin/main)" || return 1
  [[ "$checkout_sha" == "$PUBLICATION_SHA" ]] || return 1
  [[ "$current_main_sha" == "$PUBLICATION_SHA" ]] || return 1
  if ! bounded_run "$deadline" "$command_timeout_seconds" \
    git -C "$repo_root" status --porcelain=v1 --untracked-files=all \
    > "$RAILWAY_CONTROL_DIRECTORY/git-status.txt"; then
    return 1
  fi
  [[ ! -s "$RAILWAY_CONTROL_DIRECTORY/git-status.txt" ]] || return 1
  if [[ "$require_live_ancestry" == true ]]; then
    [[ "$previous_source_sha" =~ ^[0-9a-f]{40}$ ]] || return 1
    bounded_run "$deadline" "$command_timeout_seconds" \
      git -C "$repo_root" cat-file -e "${previous_source_sha}^{commit}" || return 1
    bounded_run "$deadline" "$command_timeout_seconds" \
      git -C "$repo_root" merge-base --is-ancestor \
      "$previous_source_sha" "$PUBLICATION_SHA" || return 1
  fi
}

verify_sealed_bundle() {
  local deadline="$1"
  bounded_run "$deadline" "$verifier_timeout_seconds" env \
    PYTHONPATH="$repo_root" "$python_runtime" - \
    "$RAILWAY_RELEASE_ROOT" "$PUBLICATION_SHA" \
    "$candidate_tree_sha256" "$candidate_manifest_sha256" <<'PY'
import sys
from datetime import UTC, datetime
from pathlib import Path

from ops.railway.verify_continuous_release import validate_sealed_bundle


root = Path(sys.argv[1])
source = sys.argv[2]
tree = sys.argv[3]
manifest_digest = sys.argv[4]
identity = validate_sealed_bundle(
    root,
    expected_source_commit=source,
    expected_tree_sha256=tree,
    expected_manifest_sha256=manifest_digest,
    now=datetime.now(UTC), maximum_age_seconds=24 * 60 * 60,
    future_skew_seconds=120,
)
if identity.tree_sha256 != tree:
    raise SystemExit("sealed bundle full-tree identity does not match")
PY
}

deployment_record_by_message() {
  local deployments_path="$1" expected_message="$2"
  "$python_runtime" -I -S - "$deployments_path" "$expected_message" <<'PY'
import json
import re
import sys
from pathlib import Path


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SystemExit(2)
        result[key] = value
    return result


raw = Path(sys.argv[1]).read_bytes()
if not 1 <= len(raw) <= 2 * 1024 * 1024:
    raise SystemExit(2)
rows = json.loads(raw, object_pairs_hook=strict_object)
if not isinstance(rows, list) or len(rows) > 100:
    raise SystemExit(2)
matches = [
    row for row in rows
    if isinstance(row, dict) and isinstance(row.get("meta"), dict)
    and row["meta"].get("cliMessage") == sys.argv[2]
]
if len(matches) != 1:
    raise SystemExit(3)
row = matches[0]
deployment_id = row.get("id")
status = row.get("status")
metadata = row["meta"]
image_digest = metadata.get("imageDigest") or ""
reason = metadata.get("reason")
if not isinstance(deployment_id, str) or re.fullmatch(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", deployment_id
) is None:
    raise SystemExit(2)
if not isinstance(status, str) or not 1 <= len(status) <= 64:
    raise SystemExit(2)
if image_digest and (
    not isinstance(image_digest, str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
):
    raise SystemExit(2)
if reason != "deploy":
    raise SystemExit(2)
print(deployment_id, status, image_digest or "-", reason, sep="\t")
PY
}

deployment_record_by_id() {
  local deployments_path="$1" expected_id="$2" expected_message="$3"
  "$python_runtime" -I -S - "$deployments_path" "$expected_id" "$expected_message" <<'PY'
import json
import re
import sys
from pathlib import Path


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SystemExit(2)
        result[key] = value
    return result


raw = Path(sys.argv[1]).read_bytes()
if not 1 <= len(raw) <= 2 * 1024 * 1024:
    raise SystemExit(2)
rows = json.loads(raw, object_pairs_hook=strict_object)
if not isinstance(rows, list) or len(rows) > 100:
    raise SystemExit(2)
matches = [row for row in rows if isinstance(row, dict) and row.get("id") == sys.argv[2]]
if len(matches) != 1:
    raise SystemExit(3)
row = matches[0]
metadata = row.get("meta")
if not isinstance(metadata, dict) or metadata.get("cliMessage") != sys.argv[3]:
    raise SystemExit(2)
status = row.get("status")
image_digest = metadata.get("imageDigest") or ""
reason = metadata.get("reason")
if not isinstance(status, str) or not 1 <= len(status) <= 64:
    raise SystemExit(2)
if image_digest and (
    not isinstance(image_digest, str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
):
    raise SystemExit(2)
if reason != "deploy":
    raise SystemExit(2)
print(status, image_digest or "-", reason, sep="\t")
PY
}

up_reported_deployment_id() {
  local upload_path="$1"
  "$python_runtime" -I -S - "$upload_path" <<'PY'
import json
import re
import sys
from pathlib import Path


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SystemExit(2)
        result[key] = value
    return result


raw = Path(sys.argv[1]).read_bytes()
if not 1 <= len(raw) <= 128 * 1024:
    raise SystemExit(2)
try:
    document = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=strict_object)
except (UnicodeError, json.JSONDecodeError):
    raise SystemExit(2)
if not isinstance(document, dict) or set(document) != {"deploymentId", "logsUrl"}:
    raise SystemExit(2)
deployment_id = document.get("deploymentId")
logs_url = document.get("logsUrl")
if not isinstance(deployment_id, str) or re.fullmatch(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    deployment_id,
) is None:
    raise SystemExit(2)
if (
    not isinstance(logs_url, str)
    or not 1 <= len(logs_url) <= 2048
    or not logs_url.startswith("https://")
):
    raise SystemExit(2)
print(deployment_id)
PY
}

list_deployments() {
  local destination="$1" deadline="$2"
  local ceiling="${3:-$command_timeout_seconds}"
  local temporary="${destination}.partial"
  rm -f -- "$temporary"
  if ! bounded_run "$deadline" "$ceiling" railway deployment list \
    --project "$RAILWAY_PROJECT_ID" --environment "$RAILWAY_ENVIRONMENT_ID" \
    --service "$RAILWAY_SERVICE_ID" --limit 100 --json > "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  mv -f -- "$temporary" "$destination"
}

previous_manifests_are_live() {
  local suffix="$1" deadline="$2"
  local provider_candidate="$RAILWAY_CONTROL_DIRECTORY/provider-preservation-${suffix}.json"
  local public_candidate="$RAILWAY_CONTROL_DIRECTORY/public-preservation-${suffix}.json"
  fetch_manifest "$RAILWAY_PROVIDER_ORIGIN" "$provider_candidate" \
    "${GITHUB_RUN_ID:-local}-${suffix}-provider" "$deadline" \
    "$rollback_command_timeout_seconds" || return 1
  fetch_manifest "$RAILWAY_PUBLIC_ORIGIN" "$public_candidate" \
    "${GITHUB_RUN_ID:-local}-${suffix}-public" "$deadline" \
    "$rollback_command_timeout_seconds" || return 1
  cmp --silent "$previous_provider_manifest" "$provider_candidate" || return 1
  cmp --silent "$previous_public_manifest" "$public_candidate" || return 1
}

validate_graphql_deployment() {
  local path="$1" expected_id="$2" deadline="$3"
  bounded_run "$deadline" "$rollback_command_timeout_seconds" \
    "$python_runtime" -I -S - "$path" "$expected_id" <<'PY'
import json
import sys
from pathlib import Path


document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(document, dict) or document.get("errors") not in (None, []):
    raise SystemExit(1)
deployment = (document.get("data") or {}).get("deployment")
if not isinstance(deployment, dict):
    raise SystemExit(1)
if deployment.get("id") != sys.argv[2] or deployment.get("canRollback") is not True:
    raise SystemExit(1)
PY
}

validate_graphql_rollback() {
  local path="$1" deadline="$2"
  bounded_run "$deadline" "$rollback_command_timeout_seconds" \
    "$python_runtime" -I -S - "$path" <<'PY'
import json
import sys
from pathlib import Path


document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(document, dict) or document.get("errors") not in (None, []):
    raise SystemExit(1)
if (document.get("data") or {}).get("deploymentRollback") is not True:
    raise SystemExit(1)
PY
}

validate_graphql_cancel() {
  local path="$1" deadline="$2"
  bounded_run "$deadline" "$rollback_command_timeout_seconds" \
    "$python_runtime" -I -S - "$path" <<'PY'
import json
import sys
from pathlib import Path


document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(document, dict) or document.get("errors") not in (None, []):
    raise SystemExit(1)
if (document.get("data") or {}).get("deploymentCancel") is not True:
    raise SystemExit(1)
PY
}

candidate_status_is_nonterminal() {
  case "$1" in
    BUILDING|DEPLOYING|INITIALIZING|WAITING|QUEUED|REMOVING|NEEDS_APPROVAL) return 0 ;;
    *) return 1 ;;
  esac
}

candidate_status_is_failed_terminal() {
  case "$1" in
    FAILED|CRASHED|REMOVED|SKIPPED) return 0 ;;
    *) return 1 ;;
  esac
}

deployment_id_is_valid() {
  [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
}

refresh_candidate_evidence() {
  local deadline="$1" destination="$2" row status digest reason
  deployment_id_is_valid "$new_deployment_id" || return 1
  list_deployments "$destination" "$deadline" \
    "$rollback_command_timeout_seconds" || return 1
  row="$(deployment_record_by_id "$destination" \
    "$new_deployment_id" "$upload_message")" || return 1
  IFS=$'\t' read -r status digest reason <<< "$row"
  if [[ "$digest" == - ]]; then
    digest=""
  fi
  candidate_observed_status="$status"
  new_deployment_reason="$reason"
  if [[ -n "$digest" ]]; then
    new_image_digest="$digest"
  fi
  return 0
}

discover_candidate_until() {
  local deadline="$1" inventory="$RAILWAY_CONTROL_DIRECTORY/deployments-recovery-discovery.json"
  local row discovered_id discovered_status discovered_digest discovered_reason
  while (( $(remaining_seconds "$deadline") > 0 )); do
    if list_deployments "$inventory" "$deadline" \
      "$rollback_command_timeout_seconds"; then
      if row="$(deployment_record_by_message "$inventory" "$upload_message")"; then
        IFS=$'\t' read -r discovered_id discovered_status discovered_digest discovered_reason \
          <<< "$row"
        if [[ -n "$upload_reported_deployment_id" \
          && "$discovered_id" != "$upload_reported_deployment_id" ]]; then
          candidate_observed_status=IDENTITY_CONFLICT
          return 1
        fi
        new_deployment_id="$discovered_id"
        candidate_observed_status="$discovered_status"
        new_deployment_reason="$discovered_reason"
        if [[ "$discovered_digest" != - ]]; then
          new_image_digest="$discovered_digest"
        fi
        return 0
      fi
    fi
    bounded_sleep "$deadline" "$discovery_interval_seconds" || break
  done
  return 1
}

predecessor_state_is_live() {
  local suffix="$1" deadline="$2"
  local status_path="$RAILWAY_CONTROL_DIRECTORY/status-predecessor-${suffix}.json"
  local row observed_id observed_digest observed_reason
  capture_topology "$status_path" "$deadline" \
    "$rollback_command_timeout_seconds" || return 1
  row="$(topology_identity "$status_path" predecessor "$deadline" \
    "$rollback_command_timeout_seconds")" || return 1
  IFS=$'\t' read -r observed_id observed_digest observed_reason <<< "$row"
  [[ "$observed_id" == "$previous_deployment_id" \
    && "$observed_digest" == "$previous_image_digest" \
    && "$observed_reason" == "$previous_deployment_reason" ]] || return 1
  # Bytes are deliberately observed only after the exact predecessor topology.
  previous_manifests_are_live "$suffix" "$deadline"
}

verify_predecessor_until_deadline() {
  local suffix="$1" deadline="$2" attempt=0
  while (( $(remaining_seconds "$deadline") > 0 )); do
    attempt=$((attempt + 1))
    if predecessor_state_is_live "${suffix}-${attempt}" "$deadline"; then
      return 0
    fi
    bounded_sleep "$deadline" "$poll_interval_seconds" || break
  done
  return 1
}

rollback_state_is_live() {
  local suffix="$1" deadline="$2"
  local status_path="$RAILWAY_CONTROL_DIRECTORY/status-rollback-${suffix}.json"
  local row observed_id observed_digest observed_reason
  capture_topology "$status_path" "$deadline" \
    "$rollback_command_timeout_seconds" || return 1
  row="$(topology_identity "$status_path" candidate "$deadline" \
    "$rollback_command_timeout_seconds")" || return 1
  IFS=$'\t' read -r observed_id observed_digest observed_reason <<< "$row"

  # Railway restores the target image as a new deployment. The protected
  # exclusive-writer invariant plus the exact candidate recheck immediately
  # before mutation binds this fresh rollback deployment to this transaction.
  deployment_id_is_valid "$observed_id" || return 1
  [[ "$observed_id" != "$new_deployment_id" \
    && "$observed_id" != "$previous_deployment_id" \
    && "$observed_digest" == "$previous_image_digest" \
    && "$observed_reason" =~ ^(deploymentRollback|rollback)$ ]] || return 1
  if [[ -n "$rollback_provisional_deployment_id" ]]; then
    [[ "$observed_id" == "$rollback_provisional_deployment_id" \
      && "$observed_digest" == "$rollback_provisional_image_digest" \
      && "$observed_reason" == "$rollback_provisional_reason" ]] || return 1
  else
    rollback_provisional_deployment_id="$observed_id"
    rollback_provisional_image_digest="$observed_digest"
    rollback_provisional_reason="$observed_reason"
  fi

  # Served bytes are deliberately observed after the fresh rollback topology.
  previous_manifests_are_live "$suffix" "$deadline" || return 1
  rollback_restored_deployment_id="$rollback_provisional_deployment_id"
  rollback_restored_image_digest="$rollback_provisional_image_digest"
  rollback_restored_reason="$rollback_provisional_reason"
  return 0
}

verify_rollback_until_deadline() {
  local suffix="$1" deadline="$2" attempt=0
  while (( $(remaining_seconds "$deadline") > 0 )); do
    attempt=$((attempt + 1))
    if rollback_state_is_live "${suffix}-${attempt}" "$deadline"; then
      return 0
    fi
    bounded_sleep "$deadline" "$poll_interval_seconds" || break
  done
  return 1
}

cancel_nonterminal_candidate() {
  local deadline="$1"
  local cancel_response="$RAILWAY_CONTROL_DIRECTORY/cancel-mutation.json"
  local inventory="$RAILWAY_CONTROL_DIRECTORY/deployments-cancel-poll.json"
  local cancel_exit

  # Railway exposes no conditional cancel. The protected GitHub environment is
  # the exclusive writer, and the exact candidate ID/message is re-read before
  # this bounded mutation.
  [[ "$RAILWAY_EXCLUSIVE_WRITER_ACK" == palimpsest-github-environment-v1 ]] \
    || return 1
  refresh_candidate_evidence "$deadline" "$inventory" || return 1
  if [[ "$candidate_observed_status" == SUCCESS ]]; then
    deployment_terminal_status=SUCCESS
    return 2
  fi
  if candidate_status_is_failed_terminal "$candidate_observed_status"; then
    deployment_terminal_status="$candidate_observed_status"
    return 0
  fi
  candidate_status_is_nonterminal "$candidate_observed_status" || return 1

  # The GraphQL dollar expression is intentionally literal for Railway.
  # shellcheck disable=SC2016
  bounded_run "$deadline" "$rollback_command_timeout_seconds" railway api \
    'mutation PalimpsestCancel($id: String!) { deploymentCancel(id: $id) }' \
    --raw-var "id=$new_deployment_id" --compact > "$cancel_response"
  cancel_exit=$?
  if (( cancel_exit == 0 )); then
    validate_graphql_cancel "$cancel_response" "$deadline" || cancel_exit=1
  fi

  # A cancel error is ambiguous just like upload: never repeat it blindly.
  # Poll the exact deployment ID/message until it is terminal or races SUCCESS.
  while (( $(remaining_seconds "$deadline") > 0 )); do
    if refresh_candidate_evidence "$deadline" "$inventory"; then
      if [[ "$candidate_observed_status" == SUCCESS ]]; then
        deployment_terminal_status=SUCCESS
        return 2
      fi
      if candidate_status_is_failed_terminal "$candidate_observed_status"; then
        deployment_terminal_status="$candidate_observed_status"
        return 0
      fi
      candidate_status_is_nonterminal "$candidate_observed_status" || return 1
    fi
    bounded_sleep "$deadline" "$poll_interval_seconds" || break
  done
  : "$cancel_exit"
  return 1
}

rollback_previous_release() {
  rollback_in_progress=true
  transaction_phase=rollback_reconciliation
  rollback_target_deployment_id="$previous_deployment_id"
  rollback_result=checking_preservation
  write_transaction_receipt || true

  [[ "$RAILWAY_EXCLUSIVE_WRITER_ACK" == palimpsest-github-environment-v1 ]] || {
    rollback_result=refused_exclusive_writer_unacknowledged
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  }

  local cancel_deadline restore_deadline cancel_result
  cancel_deadline="$(deadline_after "$rollback_cancel_timeout_seconds" \
    "$transaction_deadline_epoch")" || return 1

  # A successful detached upload normally reports deploymentId. If that output
  # was ambiguous, spend the cancellation slice reconciling the unique message;
  # an unknown candidate is never called preserved.
  if ! deployment_id_is_valid "$new_deployment_id"; then
    discover_candidate_until "$cancel_deadline" || true
  fi
  if ! deployment_id_is_valid "$new_deployment_id"; then
    rollback_result=refused_candidate_identity_unknown
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  fi

  refresh_candidate_evidence "$cancel_deadline" \
    "$RAILWAY_CONTROL_DIRECTORY/deployments-recovery-current.json" || true
  if candidate_status_is_nonterminal "$candidate_observed_status"; then
    cancel_nonterminal_candidate "$cancel_deadline"
    cancel_result=$?
    if (( cancel_result == 0 )); then
      restore_deadline="$(deadline_after "$rollback_restore_timeout_seconds" \
        "$transaction_deadline_epoch")" || return 1
      if verify_predecessor_until_deadline canceled "$restore_deadline"; then
        rollback_result=terminal_candidate_predecessor_proven
        rollback_in_progress=false
        write_transaction_receipt || true
        return 0
      fi
      rollback_result=cancel_predecessor_unproven
      rollback_in_progress=false
      write_transaction_receipt || true
      return 1
    fi
    if (( cancel_result != 2 )); then
      rollback_result=cancel_terminal_state_unproven
      rollback_in_progress=false
      write_transaction_receipt || true
      return 1
    fi
  fi

  if candidate_status_is_failed_terminal "$candidate_observed_status"; then
    restore_deadline="$(deadline_after "$rollback_restore_timeout_seconds" \
      "$transaction_deadline_epoch")" || return 1
    if verify_predecessor_until_deadline terminal "$restore_deadline"; then
      rollback_result=already_preserved_terminal_candidate
      rollback_in_progress=false
      write_transaction_receipt || true
      return 0
    fi
    rollback_result=terminal_candidate_predecessor_unproven
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  fi
  if [[ "$candidate_observed_status" != SUCCESS \
    && "$deployment_terminal_status" != SUCCESS ]]; then
    if [[ "$candidate_observed_status" == SLEEPING ]]; then
      rollback_result=refused_candidate_sleeping
    else
      rollback_result=refused_candidate_state_unknown
    fi
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  fi

  # SUCCESS candidates may not use predecessor bytes alone. First prove which
  # deployment Railway considers active; only an exact predecessor topology
  # followed by both predecessor manifests can short-circuit rollback.
  local writer_status="$RAILWAY_CONTROL_DIRECTORY/status-rollback-writer-check.json"
  local latest_row latest_id latest_digest latest_reason
  if ! capture_topology "$writer_status" "$transaction_deadline_epoch" \
    "$rollback_command_timeout_seconds" \
    || ! latest_row="$(topology_identity "$writer_status" predecessor \
      "$transaction_deadline_epoch" "$rollback_command_timeout_seconds")"; then
    rollback_result=refused_latest_state_unavailable
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  fi
  IFS=$'\t' read -r latest_id latest_digest latest_reason <<< "$latest_row"
  if [[ "$latest_id" == "$previous_deployment_id" \
    && "$latest_digest" == "$previous_image_digest" \
    && "$latest_reason" == "$previous_deployment_reason" ]]; then
    if previous_manifests_are_live rollback-predecessor-after-topology \
      "$transaction_deadline_epoch"; then
      rollback_result=already_preserved
      rollback_in_progress=false
      write_transaction_receipt || true
      return 0
    fi
    rollback_result=predecessor_bytes_unproven
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  fi
  if [[ -z "$new_image_digest" && "$latest_id" == "$new_deployment_id" ]]; then
    new_image_digest="$latest_digest"
  fi
  if [[ "$latest_id" != "$new_deployment_id" \
    || "$latest_digest" != "$new_image_digest" \
    || "$latest_reason" != deploy ]]; then
    rollback_result=refused_unrelated_latest
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  fi

  local rollback_query="$RAILWAY_CONTROL_DIRECTORY/rollback-query.json"
  # Railway has no conditional rollback or deployment lock. This is a
  # double-check under the protected environment's exclusive-writer invariant;
  # it is neither an atomic nor a conditional provider operation.
  # The GraphQL dollar expression is intentionally literal for Railway.
  # shellcheck disable=SC2016
  if ! bounded_run "$transaction_deadline_epoch" "$rollback_command_timeout_seconds" railway api \
    'query PalimpsestRollbackTarget($id: String!) { deployment(id: $id) { id canRollback } }' \
    --raw-var "id=$previous_deployment_id" --compact > "$rollback_query" \
    || ! validate_graphql_deployment "$rollback_query" "$previous_deployment_id" \
      "$transaction_deadline_epoch"; then
    rollback_result=unavailable
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  fi

  local final_writer_status="$RAILWAY_CONTROL_DIRECTORY/status-rollback-final-writer-check.json"
  if ! capture_topology "$final_writer_status" "$transaction_deadline_epoch" \
    "$rollback_command_timeout_seconds" \
    || ! latest_row="$(topology_identity "$final_writer_status" candidate \
      "$transaction_deadline_epoch" "$rollback_command_timeout_seconds")"; then
    rollback_result=refused_latest_state_unavailable
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  fi
  IFS=$'\t' read -r latest_id latest_digest latest_reason <<< "$latest_row"
  if [[ "$latest_id" != "$new_deployment_id" \
    || "$latest_digest" != "$new_image_digest" \
    || "$latest_reason" != deploy ]]; then
    rollback_result=refused_unrelated_latest
    rollback_in_progress=false
    write_transaction_receipt || true
    return 1
  fi

  rollback_result=mutation_attempted
  write_transaction_receipt || true
  local rollback_mutation="$RAILWAY_CONTROL_DIRECTORY/rollback-mutation.json"
  local rollback_mutation_exit rollback_mutation_validated=false
  # The GraphQL dollar expression is intentionally literal for Railway.
  # shellcheck disable=SC2016
  bounded_run "$transaction_deadline_epoch" "$rollback_command_timeout_seconds" railway api \
    'mutation PalimpsestRollback($id: String!) { deploymentRollback(id: $id) }' \
    --raw-var "id=$previous_deployment_id" --compact > "$rollback_mutation"
  rollback_mutation_exit=$?
  if (( rollback_mutation_exit == 0 )) \
    && validate_graphql_rollback "$rollback_mutation" \
      "$transaction_deadline_epoch"; then
    rollback_mutation_validated=true
  fi

  # A command or response error is ambiguous after a provider mutation. Never
  # issue another rollback blindly; reconcile the exact predecessor instead.
  restore_deadline="$(deadline_after "$rollback_restore_timeout_seconds" \
    "$transaction_deadline_epoch")" || return 1
  if verify_rollback_until_deadline rollback "$restore_deadline"; then
    if [[ "$rollback_mutation_validated" == true ]]; then
      rollback_result=passed
    else
      rollback_result=passed_mutation_response_unconfirmed
    fi
    rollback_in_progress=false
    write_transaction_receipt || true
    return 0
  fi
  if [[ "$rollback_mutation_validated" == true ]]; then
    rollback_result=failed
  else
    rollback_result=mutation_response_unconfirmed_predecessor_unproven
  fi
  rollback_in_progress=false
  write_transaction_receipt || true
  return 1
}

finalize_reconciliation() {
  # This mapping depends only on the completed recovery evidence, so it is safe
  # to repeat from a signal trap during the post-recovery handoff.
  case "$rollback_result" in
    already_preserved|already_preserved_terminal_candidate)
      transaction_result=failed_previous_release_preserved
      ;;
    canceled_predecessor_proven|terminal_candidate_predecessor_proven)
      transaction_result=failed_candidate_terminal_previous_release_proven
      ;;
    passed)
      transaction_result=failed_rolled_back
      ;;
    passed_mutation_response_unconfirmed)
      transaction_result=failed_rolled_back_mutation_response_unconfirmed
      ;;
    refused_*)
      transaction_result=failed_rollback_refused
      ;;
    *)
      transaction_result=failed_rollback_failed
      ;;
  esac
  finalize_transaction_receipt || true
}

fail_after_submission() {
  local reason="$1" phase="$2"
  # A signal or EXIT trap may arrive while a prior recovery call is handing its
  # result back. Never enter provider recovery twice for one submission.
  [[ "$reconciliation_started" != true ]] || return 1
  reconciliation_started=true
  rollback_in_progress=true
  failure_reason="$reason"
  transaction_phase="$phase"
  submission_state=terminal_failed
  transaction_result=failed_reconciling
  write_transaction_receipt || true
  if rollback_previous_release; then
    :
  else
    :
  fi
  rollback_in_progress=false
  finalize_reconciliation
  return 1
}

# Invoked through the TERM/INT/HUP traps installed below.
# shellcheck disable=SC2329
on_signal() {
  local signal_name="$1"
  received_signal="$signal_name"
  if [[ "$transaction_finalizing" == true ]]; then
    # The final state is already selected. Finish the same atomic receipt; do
    # not reinterpret a completed deployment or recovery as a new failure.
    finalize_transaction_receipt || true
  elif [[ "$transaction_terminal" == true ]]; then
    :
  elif [[ "$submission_state" == not_started ]]; then
    failure_reason="interrupted_${signal_name}"
    transaction_result=interrupted_before_submission
    transaction_phase=signal
    finalize_transaction_receipt || true
  elif [[ "$reconciliation_started" == true ]]; then
    if [[ "$rollback_in_progress" == true ]]; then
      # Never recurse into a second recovery attempt from a recovery signal.
      failure_reason="interrupted_${signal_name}_during_reconciliation"
      transaction_result=failed_rollback_interrupted
      transaction_phase=rollback_signal
      finalize_transaction_receipt || true
    else
      # Recovery already produced its terminal evidence. Preserve that exact
      # result and add only the signal field; never call the provider again.
      finalize_reconciliation
    fi
  else
    fail_after_submission "interrupted_${signal_name}" signal || true
  fi
  exit 128
}

# Invoked through the EXIT trap installed below.
# shellcheck disable=SC2329
on_exit() {
  local exit_status=$?
  if (( exit_status != 0 )) && [[ "$transaction_terminal" != true ]]; then
    set +e
    if [[ "$submission_state" == not_started ]]; then
      failure_reason=unexpected_pre_submission_exit
      transaction_phase=unexpected_exit
      transaction_result=failed_before_submission
      finalize_transaction_receipt || true
    elif [[ "$reconciliation_started" == true ]]; then
      if [[ "$rollback_in_progress" == true ]]; then
        failure_reason=unexpected_exit_during_reconciliation
        transaction_phase=rollback_unexpected_exit
        transaction_result=failed_rollback_interrupted
        finalize_transaction_receipt || true
      else
        finalize_reconciliation
      fi
    else
      fail_after_submission unexpected_post_submission_exit unexpected_exit || true
    fi
  fi
}

validate_environment() {
  local required_environment=(
    PUBLICATION_SHA RIGHTS_ADMISSION_EPOCH PAGES_RIGHTS_RECEIPT_SHA256
    RAILWAY_TOKEN RAILWAY_EXCLUSIVE_WRITER_ACK
    RAILWAY_PROJECT_ID RAILWAY_ENVIRONMENT_ID RAILWAY_SERVICE_ID
    RAILWAY_PROVIDER_ORIGIN RAILWAY_PUBLIC_ORIGIN RAILWAY_RELEASE_ROOT
    RAILWAY_CONTROL_DIRECTORY RAILWAY_EVIDENCE_DIRECTORY
  )
  local variable_name identifier rollback_required_reserve
  for variable_name in "${required_environment[@]}"; do
    [[ -n "${!variable_name:-}" ]] \
      || fail "missing required continuous-release setting: $variable_name" \
      || return 1
  done
  [[ "$PUBLICATION_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || fail 'PUBLICATION_SHA must be exactly 40 lowercase hex characters' \
    || return 1
  [[ "$RIGHTS_ADMISSION_EPOCH" =~ ^[0-9]+$ ]] \
    || fail 'RIGHTS_ADMISSION_EPOCH must be whole Unix seconds' \
    || return 1
  [[ "$PAGES_RIGHTS_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || fail 'PAGES_RIGHTS_RECEIPT_SHA256 must be a lowercase SHA-256' \
    || return 1
  [[ "$RAILWAY_EXCLUSIVE_WRITER_ACK" == palimpsest-github-environment-v1 ]] \
    || fail 'RAILWAY_EXCLUSIVE_WRITER_ACK does not bind the protected exclusive writer' \
    || return 1
  for identifier in "$RAILWAY_PROJECT_ID" "$RAILWAY_ENVIRONMENT_ID" "$RAILWAY_SERVICE_ID"; do
    [[ "$identifier" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
      || fail 'Railway identifiers must be lowercase canonical UUIDs' \
      || return 1
  done
  [[ "$python_runtime" != *[[:space:]]* ]] \
    || fail 'PALIMPSEST_RAILWAY_PYTHON must name one executable without arguments' \
    || return 1
  if [[ "$python_runtime" == */* ]]; then
    [[ -x "$python_runtime" ]] \
      || fail 'PALIMPSEST_RAILWAY_PYTHON is not executable' \
      || return 1
  else
    python_runtime="$(command -v "$python_runtime")" \
      || fail 'PALIMPSEST_RAILWAY_PYTHON is not on PATH' \
      || return 1
  fi
  require_integer PALIMPSEST_RAILWAY_TRANSACTION_SECONDS "$transaction_seconds" 3 3300 || return 1
  require_integer PALIMPSEST_RAILWAY_ROLLBACK_RESERVE_SECONDS "$rollback_reserve_seconds" 1 1500 || return 1
  require_integer PALIMPSEST_RAILWAY_MINIMUM_MUTATION_SECONDS "$minimum_mutation_seconds" 1 1800 || return 1
  require_integer PALIMPSEST_RAILWAY_COMMAND_TIMEOUT_SECONDS "$command_timeout_seconds" 1 300 || return 1
  require_integer PALIMPSEST_RAILWAY_VERIFIER_TIMEOUT_SECONDS "$verifier_timeout_seconds" 1 600 || return 1
  require_integer PALIMPSEST_RAILWAY_MCP_TIMEOUT_SECONDS "$mcp_timeout_seconds" 1 300 || return 1
  require_integer PALIMPSEST_RAILWAY_ROLLBACK_COMMAND_TIMEOUT_SECONDS "$rollback_command_timeout_seconds" 1 120 || return 1
  require_integer PALIMPSEST_RAILWAY_ROLLBACK_CANCEL_TIMEOUT_SECONDS "$rollback_cancel_timeout_seconds" 1 600 || return 1
  require_integer PALIMPSEST_RAILWAY_ROLLBACK_RESTORE_TIMEOUT_SECONDS "$rollback_restore_timeout_seconds" 1 600 || return 1
  require_integer PALIMPSEST_RAILWAY_PREDECESSOR_MAX_AGE_SECONDS "$predecessor_max_age_seconds" 86400 63072000 || return 1
  require_integer PALIMPSEST_RAILWAY_CANDIDATE_MAX_AGE_SECONDS "$candidate_max_age_seconds" 60 604800 || return 1
  require_integer PALIMPSEST_RAILWAY_DISCOVERY_INTERVAL_SECONDS "$discovery_interval_seconds" 0 60 || return 1
  require_integer PALIMPSEST_RAILWAY_POLL_INTERVAL_SECONDS "$poll_interval_seconds" 0 60 || return 1
  (( transaction_seconds > rollback_reserve_seconds )) \
    || fail 'transaction deadline must exceed the rollback reserve' \
    || return 1
  # A cancel-race-to-SUCCESS path can spend both phase ceilings plus eight
  # standalone command/validator ceilings before restoration. Account for the
  # timeout kill grace on those ten boundaries and retain two command ceilings
  # as receipt, scheduling, and shell overhead rather than exhausting the
  # reserve at its nominal arithmetic limit.
  rollback_required_reserve=$((
    rollback_cancel_timeout_seconds
    + rollback_restore_timeout_seconds
    + 8 * rollback_command_timeout_seconds
    + 10 * timeout_kill_grace_seconds
    + 2 * rollback_command_timeout_seconds
  ))
  (( rollback_reserve_seconds >= rollback_required_reserve )) \
    || fail "rollback reserve must be at least ${rollback_required_reserve}s for bounded recovery and overhead" \
    || return 1
  for variable_name in railway curl git timeout sha256sum install cmp; do
    command -v "$variable_name" >/dev/null \
      || fail "required release command is unavailable: $variable_name" \
      || return 1
  done
  [[ ! -e "$RAILWAY_RELEASE_ROOT" ]] \
    || fail 'release root already exists; refusing to overwrite it' \
    || return 1
  [[ ! -e "$RAILWAY_CONTROL_DIRECTORY" && ! -e "$RAILWAY_EVIDENCE_DIRECTORY" ]] \
    || fail 'continuous-release control or evidence directory already exists' \
    || return 1
}

validate_project_token() {
  local destination="$RAILWAY_CONTROL_DIRECTORY/project-token-scope.json"
  if ! bounded_run "$mutation_deadline_epoch" "$command_timeout_seconds" railway api \
    'query PalimpsestProjectToken { projectToken { projectId environmentId } }' \
    --compact > "$destination"; then
    return 1
  fi
  "$python_runtime" -I -S - "$destination" <<'PY'
import json
import os
import sys
from pathlib import Path


document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(document, dict) or document.get("errors") not in (None, []):
    raise SystemExit("Railway project-token query returned GraphQL errors")
token = (document.get("data") or {}).get("projectToken")
if not isinstance(token, dict):
    raise SystemExit("Railway project token was not recognized")
if token.get("projectId") != os.environ["RAILWAY_PROJECT_ID"]:
    raise SystemExit("Railway token is scoped to the wrong project")
if token.get("environmentId") != os.environ["RAILWAY_ENVIRONMENT_ID"]:
    raise SystemExit("Railway token is scoped to the wrong environment")
PY
}

preserve_release_evidence() {
  if ! install -m 0600 "$candidate_manifest" \
    "$RAILWAY_EVIDENCE_DIRECTORY/candidate-railway-release.json"; then
    return 1
  fi
  if ! install -m 0600 "$candidate_rights_receipt" \
    "$RAILWAY_EVIDENCE_DIRECTORY/candidate-pages-rights-release-receipt.json"; then
    return 1
  fi
  if ! PREVIOUS_IDENTITY_DESTINATION="$RAILWAY_EVIDENCE_DIRECTORY/previous-release-identity.json" \
  PREVIOUS_DEPLOYMENT_ID="$previous_deployment_id" \
  PREVIOUS_IMAGE_DIGEST="$previous_image_digest" \
  PREVIOUS_DEPLOYMENT_REASON="$previous_deployment_reason" \
  PREVIOUS_SOURCE_SHA="$previous_source_sha" \
  PREVIOUS_TREE_SHA256="$previous_tree_sha256" \
  PREVIOUS_MANIFEST_SHA256="$previous_manifest_sha256" \
  "$python_runtime" -I -S - <<'PY'
import json
import os
from pathlib import Path


document = {
    "schema_version": "palimpsest.railway-previous-release-identity.v1",
    "deployment_id": os.environ["PREVIOUS_DEPLOYMENT_ID"],
    "image_digest": os.environ["PREVIOUS_IMAGE_DIGEST"],
    "deployment_reason": os.environ["PREVIOUS_DEPLOYMENT_REASON"],
    "source_commit": os.environ["PREVIOUS_SOURCE_SHA"],
    "tree_sha256": os.environ["PREVIOUS_TREE_SHA256"],
    "manifest_sha256": os.environ["PREVIOUS_MANIFEST_SHA256"],
}
Path(os.environ["PREVIOUS_IDENTITY_DESTINATION"]).write_text(
    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
  then
    return 1
  fi
  if ! chmod 0600 "$RAILWAY_EVIDENCE_DIRECTORY/previous-release-identity.json"; then
    return 1
  fi
}

ensure_mutation_budget() {
  local remaining
  remaining="$(remaining_seconds "$transaction_deadline_epoch")" || return 1
  (( remaining >= rollback_reserve_seconds + minimum_mutation_seconds ))
}

run_post_deploy_verifier() {
  local final_status="$1" deployment_inventory="$2"
  local remaining wrapper_ceiling
  remaining="$(remaining_seconds "$mutation_deadline_epoch")" || return 1
  (( remaining > 0 )) || return 1
  wrapper_ceiling="$verifier_timeout_seconds"
  if (( remaining < wrapper_ceiling )); then
    wrapper_ceiling="$remaining"
  fi
  bounded_run "$mutation_deadline_epoch" "$wrapper_ceiling" env PYTHONPATH="$repo_root" \
    "$python_runtime" "$repo_root/ops/railway/verify_continuous_release.py" \
    --expected-source-commit "$PUBLICATION_SHA" \
    --checkout-source-commit "$PUBLICATION_SHA" \
    --current-main-source-commit "$PUBLICATION_SHA" \
    --git-status-file "$RAILWAY_CONTROL_DIRECTORY/git-status.txt" \
    --release-manifest "$candidate_manifest" \
    --expected-tree-sha256 "$candidate_tree_sha256" \
    --expected-manifest-sha256 "$candidate_manifest_sha256" \
    --deployment-json "$deployment_inventory" \
    --status-json "$final_status" \
    --expected-deployment-id "$new_deployment_id" \
    --expected-image-digest "$new_image_digest" \
    --expected-project-id "$RAILWAY_PROJECT_ID" \
    --expected-environment-id "$RAILWAY_ENVIRONMENT_ID" \
    --expected-service-id "$RAILWAY_SERVICE_ID" \
    --live-base-url "$RAILWAY_PROVIDER_ORIGIN" \
    --public-base-url "$RAILWAY_PUBLIC_ORIGIN" \
    --receipt "$verification_receipt" \
    --attempts 8 --retry-delay-seconds 5 --request-timeout-seconds 20 \
    --max-deployment-age-seconds 86400 --max-release-age-seconds 86400 \
    --max-future-skew-seconds 120 \
    > "$RAILWAY_CONTROL_DIRECTORY/verification-result.json"
}

run_exact_mcp_proof() {
  local mcp_receipt="$RAILWAY_EVIDENCE_DIRECTORY/pages-mcp-rights-live-receipt.json"
  bounded_run "$mutation_deadline_epoch" "$mcp_timeout_seconds" \
    "$python_runtime" "$repo_root/scripts/smoke_palimpsest_mcp.py" \
    --url https://api.seiche.info/palimpsest/mcp \
    --module "$repo_root/mcp/palimpsest_mcp.py" \
    --manifest "$repo_root/server.json" \
    --expected-publication-sha "$PUBLICATION_SHA" \
    --timeout 20 > "$mcp_receipt"
}

main() {
  validate_environment || return 1
  transaction_deadline_epoch=$((transaction_started_epoch + transaction_seconds))
  mutation_deadline_epoch=$((transaction_deadline_epoch - rollback_reserve_seconds))
  install -d -m 0700 "$RAILWAY_CONTROL_DIRECTORY" "$RAILWAY_EVIDENCE_DIRECTORY"
  transaction_receipt="$RAILWAY_EVIDENCE_DIRECTORY/railway-continuous-transaction.json"
  verification_receipt="$RAILWAY_EVIDENCE_DIRECTORY/railway-continuous-verification.json"
  write_controller_provenance || return 1
  write_transaction_receipt || return 1

  transaction_phase=token_scope
  write_transaction_receipt || return 1
  validate_project_token || return 1

  transaction_phase=source_identity
  write_transaction_receipt || return 1
  assert_current_main "$mutation_deadline_epoch" false || return 1

  transaction_phase=topology_preflight
  write_transaction_receipt || return 1
  initial_status="$RAILWAY_CONTROL_DIRECTORY/status-before.json"
  capture_topology "$initial_status" "$mutation_deadline_epoch" || return 1
  initial_row="$(topology_identity "$initial_status" predecessor \
    "$mutation_deadline_epoch")" || return 1
  IFS=$'\t' read -r previous_deployment_id previous_image_digest previous_deployment_reason \
    <<< "$initial_row"

  nonce="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-before"
  previous_provider_manifest="$RAILWAY_CONTROL_DIRECTORY/provider-manifest-before.json"
  previous_public_manifest="$RAILWAY_CONTROL_DIRECTORY/public-manifest-before.json"
  fetch_manifest "$RAILWAY_PROVIDER_ORIGIN" "$previous_provider_manifest" "$nonce" \
    "$mutation_deadline_epoch" || return 1
  fetch_manifest "$RAILWAY_PUBLIC_ORIGIN" "$previous_public_manifest" "$nonce" \
    "$mutation_deadline_epoch" || return 1
  cmp --silent "$previous_provider_manifest" "$previous_public_manifest" || return 1
  previous_manifest_row="$(manifest_identity "$previous_provider_manifest")" || return 1
  IFS=$'\t' read -r previous_source_sha previous_tree_sha256 previous_manifest_sha256 \
    <<< "$previous_manifest_row"

  transaction_phase=bundle
  write_transaction_receipt || return 1
  if ! bounded_run "$mutation_deadline_epoch" "$verifier_timeout_seconds" env \
    PALIMPSEST_RAILWAY_ADMISSION_EPOCH="$RIGHTS_ADMISSION_EPOCH" \
    "$repo_root/ops/railway/build-static-bundle.sh" \
    "$PUBLICATION_SHA" "$RAILWAY_RELEASE_ROOT" \
    > "$RAILWAY_CONTROL_DIRECTORY/bundle-build.txt"; then
    return 1
  fi
  candidate_manifest="$RAILWAY_RELEASE_ROOT/railway-release.json"
  candidate_rights_receipt="${RAILWAY_RELEASE_ROOT}.pages-rights-release-receipt.json"
  candidate_manifest_row="$(manifest_identity "$candidate_manifest")" || return 1
  IFS=$'\t' read -r candidate_source_sha candidate_tree_sha256 candidate_manifest_sha256 \
    <<< "$candidate_manifest_row"
  [[ "$candidate_source_sha" == "$PUBLICATION_SHA" ]] || return 1
  candidate_rights_sha256="$(sha256sum "$candidate_rights_receipt" | awk '{print $1}')" \
    || return 1
  [[ "$candidate_rights_sha256" == "$PAGES_RIGHTS_RECEIPT_SHA256" ]] || return 1
  verify_sealed_bundle "$mutation_deadline_epoch" || return 1
  preserve_release_evidence || return 1

  transaction_phase=mutation_preflight
  write_transaction_receipt || return 1
  ensure_mutation_budget \
    || fail 'insufficient global deadline budget for mutation and rollback' \
    || return 1
  mutation_status="$RAILWAY_CONTROL_DIRECTORY/status-before-mutation.json"
  capture_topology "$mutation_status" "$mutation_deadline_epoch" || return 1
  mutation_row="$(topology_identity "$mutation_status" predecessor \
    "$mutation_deadline_epoch")" || return 1
  IFS=$'\t' read -r mutation_deployment_id mutation_image_digest mutation_reason \
    <<< "$mutation_row"
  [[ "$mutation_deployment_id" == "$previous_deployment_id" \
    && "$mutation_image_digest" == "$previous_image_digest" \
    && "$mutation_reason" == "$previous_deployment_reason" ]] || return 1
  mutation_provider_manifest="$RAILWAY_CONTROL_DIRECTORY/provider-manifest-before-mutation.json"
  mutation_public_manifest="$RAILWAY_CONTROL_DIRECTORY/public-manifest-before-mutation.json"
  fetch_manifest "$RAILWAY_PROVIDER_ORIGIN" "$mutation_provider_manifest" "${nonce}-mutation" \
    "$mutation_deadline_epoch" || return 1
  fetch_manifest "$RAILWAY_PUBLIC_ORIGIN" "$mutation_public_manifest" "${nonce}-mutation" \
    "$mutation_deadline_epoch" || return 1
  cmp --silent "$previous_provider_manifest" "$mutation_provider_manifest" || return 1
  cmp --silent "$previous_public_manifest" "$mutation_public_manifest" || return 1
  verify_sealed_bundle "$mutation_deadline_epoch" || return 1

  # Final authority boundary before a billable mutation.
  assert_current_main "$mutation_deadline_epoch" true || return 1
  ensure_mutation_budget \
    || fail 'mutation budget expired at final source boundary' \
    || return 1

  if [[ "$previous_source_sha" == "$candidate_source_sha" \
    && "$previous_tree_sha256" == "$candidate_tree_sha256" \
    && "$previous_manifest_sha256" == "$candidate_manifest_sha256" ]]; then
    deployment_mode=recovered_existing
    new_deployment_id="$previous_deployment_id"
    new_image_digest="$previous_image_digest"
    new_deployment_reason="$previous_deployment_reason"
    deployment_terminal_status=SUCCESS
  else
    deployment_mode=uploaded
    transaction_phase=upload
    upload_message="palimpsest-continuous-${PUBLICATION_SHA}-run-${GITHUB_RUN_ID:-local}-attempt-${GITHUB_RUN_ATTEMPT:-1}"
    submission_state=submitted_unknown
    write_transaction_receipt || return 1

    # A CLI error can mean Railway accepted the request. Submit exactly once,
    # disable errexit, and reconcile every later failure explicitly.
    set +e
    (
      cd "$RAILWAY_RELEASE_ROOT" || exit 125
      bounded_run "$mutation_deadline_epoch" "$verifier_timeout_seconds" railway up \
        --detach --json --yes --no-gitignore \
        --project "$RAILWAY_PROJECT_ID" \
        --environment "$RAILWAY_ENVIRONMENT_ID" \
        --service "$RAILWAY_SERVICE_ID" --message "$upload_message"
    ) > "$RAILWAY_CONTROL_DIRECTORY/upload.jsonl" 2>&1
    upload_exit_code=$?
    if ! upload_reported_deployment_id="$(up_reported_deployment_id \
      "$RAILWAY_CONTROL_DIRECTORY/upload.jsonl")"; then
      upload_reported_deployment_id=""
    fi
    write_transaction_receipt || true

    deployment_inventory="$RAILWAY_CONTROL_DIRECTORY/deployments-current.json"
    discovered=false
    while (( $(remaining_seconds "$mutation_deadline_epoch") > 0 )); do
      if list_deployments "$deployment_inventory" "$mutation_deadline_epoch"; then
        deployment_row="$(deployment_record_by_message "$deployment_inventory" "$upload_message")"
        record_exit=$?
        if (( record_exit == 0 )); then
          IFS=$'\t' read -r new_deployment_id deployment_status new_image_digest new_deployment_reason \
            <<< "$deployment_row"
          if [[ "$new_image_digest" == - ]]; then
            new_image_digest=""
          fi
          if [[ -n "$upload_reported_deployment_id" \
            && "$new_deployment_id" != "$upload_reported_deployment_id" ]]; then
            new_deployment_id=""
            candidate_observed_status=IDENTITY_CONFLICT
            break
          fi
          candidate_observed_status="$deployment_status"
          submission_state=active
          discovered=true
          write_transaction_receipt || true
          break
        fi
      fi
      bounded_sleep "$mutation_deadline_epoch" "$discovery_interval_seconds" || break
    done
    if [[ "$discovered" != true ]]; then
      fail_after_submission submission_unresolved upload_discovery || true
      return 1
    fi

    while (( $(remaining_seconds "$mutation_deadline_epoch") > 0 )); do
      if list_deployments "$deployment_inventory" "$mutation_deadline_epoch"; then
        deployment_row="$(deployment_record_by_id "$deployment_inventory" \
          "$new_deployment_id" "$upload_message")"
        record_exit=$?
        if (( record_exit == 0 )); then
          IFS=$'\t' read -r deployment_status polled_digest polled_reason \
            <<< "$deployment_row"
          if [[ "$polled_digest" == - ]]; then
            polled_digest=""
          fi
          new_deployment_reason="$polled_reason"
          candidate_observed_status="$deployment_status"
          if [[ -n "$polled_digest" ]]; then
            new_image_digest="$polled_digest"
          fi
          case "$deployment_status" in
            SUCCESS)
              if [[ "$new_image_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
                deployment_terminal_status=SUCCESS
                break
              fi
              ;;
            FAILED|CRASHED|REMOVED|SKIPPED)
              deployment_terminal_status="$deployment_status"
              fail_after_submission "deployment_${deployment_status}" railway_health_gate || true
              return 1
              ;;
          esac
        fi
      fi
      bounded_sleep "$mutation_deadline_epoch" "$poll_interval_seconds" || break
    done
    if [[ "$deployment_terminal_status" != SUCCESS ]]; then
      fail_after_submission deployment_terminal_state_unproven railway_health_gate || true
      return 1
    fi
  fi

  transaction_phase=served_byte_verification
  write_transaction_receipt || true
  if ! bounded_run "$mutation_deadline_epoch" "$command_timeout_seconds" \
    git -C "$repo_root" status --porcelain=v1 --untracked-files=all \
    > "$RAILWAY_CONTROL_DIRECTORY/git-status.txt" \
    || [[ -s "$RAILWAY_CONTROL_DIRECTORY/git-status.txt" ]]; then
    if [[ "$deployment_mode" == uploaded ]]; then
      fail_after_submission dirty_checkout_after_submission served_byte_verification || true
    fi
    return 1
  fi
  final_status="$RAILWAY_CONTROL_DIRECTORY/status-final.json"
  final_topology_ready=false
  while (( $(remaining_seconds "$mutation_deadline_epoch") > 0 )); do
    if capture_topology "$final_status" "$mutation_deadline_epoch"; then
      final_row="$(topology_identity "$final_status" candidate \
        "$mutation_deadline_epoch")"
      topology_exit=$?
      if (( topology_exit == 0 )); then
        IFS=$'\t' read -r final_deployment_id final_image_digest final_reason <<< "$final_row"
        if [[ "$final_deployment_id" == "$new_deployment_id" \
          && "$final_image_digest" == "$new_image_digest" \
          && "$final_reason" == "$new_deployment_reason" ]]; then
          final_topology_ready=true
          break
        fi
      fi
    fi
    bounded_sleep "$mutation_deadline_epoch" "$poll_interval_seconds" || break
  done
  if [[ "$final_topology_ready" != true ]]; then
    if [[ "$deployment_mode" == uploaded ]]; then
      fail_after_submission final_topology_unproven served_byte_verification || true
    fi
    return 1
  fi
  deployment_inventory="$RAILWAY_CONTROL_DIRECTORY/deployments-final.json"
  if ! list_deployments "$deployment_inventory" "$mutation_deadline_epoch" \
    || ! run_post_deploy_verifier "$final_status" "$deployment_inventory"; then
    if [[ "$deployment_mode" == uploaded ]]; then
      fail_after_submission exact_release_verification_failed served_byte_verification || true
    fi
    return 1
  fi
  verification_receipt_sha256="$(sha256sum "$verification_receipt" | awk '{print $1}')" || {
    if [[ "$deployment_mode" == uploaded ]]; then
      fail_after_submission verification_receipt_digest_failed served_byte_verification || true
    fi
    return 1
  }

  transaction_phase=mcp_rights_closure
  write_transaction_receipt || true
  if ! run_exact_mcp_proof; then
    mcp_rights_smoke=failed
    if [[ "$deployment_mode" == uploaded ]]; then
      fail_after_submission mcp_exact_sha_verification_failed mcp_rights_closure || true
    fi
    return 1
  fi
  mcp_rights_smoke=verified

  transaction_phase=complete
  if [[ "$deployment_mode" == recovered_existing ]]; then
    transaction_result=recovered_existing
  else
    transaction_result=deployed
  fi
  finalize_transaction_receipt || return 1
  printf 'Railway publication %s: %s (%s)\n' \
    "$transaction_result" "$PUBLICATION_SHA" "$new_deployment_id"
}

trap 'on_signal TERM' TERM
trap 'on_signal INT' INT
trap 'on_signal HUP' HUP
trap on_exit EXIT

if main; then
  trap - EXIT TERM INT HUP
  exit 0
fi
exit 1
