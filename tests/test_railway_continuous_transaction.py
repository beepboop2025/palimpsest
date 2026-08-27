from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TRANSACTION_SCRIPT = REPOSITORY_ROOT / "ops/railway/deploy-continuous-release.sh"
PUBLICATION_SHA = "b" * 40
PREVIOUS_SHA = "a" * 40
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
ENVIRONMENT_ID = "22222222-2222-4222-8222-222222222222"
SERVICE_ID = "33333333-3333-4333-8333-333333333333"
PREVIOUS_DEPLOYMENT_ID = "44444444-4444-4444-8444-444444444444"
CANDIDATE_DEPLOYMENT_ID = "55555555-5555-4555-8555-555555555555"
UNRELATED_DEPLOYMENT_ID = "66666666-6666-4666-8666-666666666666"
ROLLBACK_DEPLOYMENT_ID = "77777777-7777-4777-8777-777777777777"
PREVIOUS_DIGEST = f"sha256:{'c' * 64}"
CANDIDATE_DIGEST = f"sha256:{'d' * 64}"
UNRELATED_DIGEST = f"sha256:{'e' * 64}"
RIGHTS_PAYLOAD = b'{"rights":"ok"}\n'


def _write_executable(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o700)


def _manifest(source: str, tree: str) -> bytes:
    document = {
        "schema_version": "palimpsest.railway-static-release.v1",
        "source_commit": source,
        "built_at": "2026-08-27T00:00:00Z",
        "deployment_source": "local-git-archive",
        "github_required": False,
        "state": "artifact_ready",
        "file_count": 1,
        "total_bytes": 8,
        "tree_sha256": tree,
        "critical_files": {
            "payload.txt": {
                "bytes": 8,
                "sha256": hashlib.sha256(b"payload\n").hexdigest(),
            }
        },
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


@dataclass
class RunResult:
    process: subprocess.CompletedProcess[str]
    root: Path
    state: Path
    evidence: Path

    @property
    def receipt(self) -> dict[str, object]:
        return json.loads(
            (self.evidence / "railway-continuous-transaction.json").read_text(
                encoding="utf-8"
            )
        )

    def count(self, name: str) -> int:
        path = self.state / name
        return int(path.read_text(encoding="utf-8")) if path.exists() else 0


class TransactionHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.bin = root / "bin"
        self.state = root / "state"
        self.release = root / "release"
        self.control = root / "control"
        self.evidence = root / "evidence"
        self.previous_manifest = root / "previous.json"
        self.event = root / "event.json"
        self.repo.mkdir()
        self.bin.mkdir()
        self.state.mkdir()
        self.previous_manifest.write_bytes(_manifest(PREVIOUS_SHA, "c" * 64))
        self.event.write_text(
            json.dumps(
                {
                    "client_payload": {
                        "sha": PUBLICATION_SHA,
                        "scope": "complete",
                        "deploy_railway": True,
                        "controller_run_id": 731994934,
                        "controller_run_attempt": 1,
                        "requested_at": "2026-08-27T00:00:00Z",
                        "controller_artifact_id": 99,
                        "controller_artifact_digest": "f" * 64,
                        "controller_request_sha256": "9" * 64,
                        "should_not_escape": "private-control-field",
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.state / "clock-calls").write_text("0\n", encoding="utf-8")
        self._install_repo()
        self._install_commands()

    def _install_repo(self) -> None:
        destination = self.repo / "ops/railway/deploy-continuous-release.sh"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(TRANSACTION_SCRIPT.read_bytes())
        destination.chmod(0o700)

        _write_executable(
            self.repo / "ops/railway/build-static-bundle.sh",
            """#!/usr/bin/env bash
set -euo pipefail
root="$2"
mkdir -p "$root"
printf 'payload\n' > "$root/payload.txt"
python3 - "$root" "$1" <<'PY'
import hashlib, json, sys
from datetime import UTC, datetime
from pathlib import Path
root = Path(sys.argv[1])
payload = (root / "payload.txt").read_bytes()
document = {
    "schema_version": "palimpsest.railway-static-release.v1",
    "source_commit": sys.argv[2],
    "built_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "deployment_source": "local-git-archive",
    "github_required": False,
    "state": "artifact_ready",
    "file_count": 1,
    "total_bytes": len(payload),
    "tree_sha256": "b" * 64,
    "critical_files": {"payload.txt": {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}},
}
(root / "railway-release.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\\n")
PY
printf '{"rights":"ok"}\n' > "${root}.pages-rights-release-receipt.json"
""",
        )
        (self.repo / "ops/railway/build_release_manifest.py").write_text(
            """import json, os
def build_manifest(root, source, built_at):
    document = json.loads((root / "railway-release.json").read_text())
    if os.environ.get("FAKE_SCENARIO") == "tree_mismatch":
        document["file_count"] += 1
    return document
""",
            encoding="utf-8",
        )
        _write_executable(
            self.repo / "ops/railway/verify_continuous_release.py",
            """#!/usr/bin/env python3
import argparse, hashlib, json, os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

@dataclass
class Evidence:
    deployment_id: str
    image_digest: str
    reason: str

@dataclass
class Identity:
    tree_sha256: str

def latest_deployment(document, **kwargs):
    project_id = kwargs.get("expected_project_id")
    if project_id is not None and document.get("id") != project_id:
        raise ValueError("project mismatch")
    environments = (document.get("environments") or {}).get("edges")
    matches = [
        edge["node"] for edge in environments
        if edge["node"].get("id") == kwargs["expected_environment_id"]
    ]
    if len(matches) != 1:
        raise ValueError("environment mismatch")
    instances = (matches[0].get("serviceInstances") or {}).get("edges")
    matches = [
        edge["node"] for edge in instances
        if edge["node"].get("serviceId") == kwargs["expected_service_id"]
    ]
    if len(matches) != 1:
        raise ValueError("service mismatch")
    latest = matches[0].get("latestDeployment")
    if not isinstance(latest, dict):
        raise ValueError("latest deployment missing")
    return latest

def extract_latest_status_deployment(payload, **kwargs):
    document = json.loads(payload)
    latest = latest_deployment(document, **kwargs)
    if latest.get("status") != "SUCCESS":
        raise ValueError("latest deployment is not active")
    metadata = latest.get("meta")
    if not isinstance(metadata, dict):
        raise ValueError("deployment metadata missing")
    if metadata.get("reason") not in {"deploy", "deploymentRollback"}:
        raise ValueError("missing deployment reason")
    digest = metadata.get("imageDigest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError("missing image digest")
    created_at = datetime.fromisoformat(latest["createdAt"].replace("Z", "+00:00"))
    now = kwargs.get("now", datetime.now(UTC))
    if (now - created_at.astimezone(UTC)).total_seconds() > kwargs.get(
        "maximum_age_seconds", 86400
    ):
        raise ValueError("deployment too old")
    return Evidence(latest["id"], digest, metadata["reason"])

def parse_status_topology(payload, **kwargs):
    document = json.loads(payload)
    latest = latest_deployment(document, **kwargs)
    metadata = latest.get("meta") or {}
    if latest["id"] != kwargs["expected_deployment_id"]:
        raise ValueError("deployment mismatch")
    if metadata.get("imageDigest") != kwargs["expected_image_digest"]:
        raise ValueError("digest mismatch")
    if metadata.get("reason") != kwargs["expected_deployment_reason"]:
        raise ValueError("reason mismatch")
    return object()

def validate_sealed_bundle(root, **kwargs):
    if os.environ.get("FAKE_SCENARIO") == "stale_manifest":
        raise ValueError("stale manifest")
    if os.environ.get("FAKE_SCENARIO") == "tree_mismatch":
        raise ValueError("full tree mismatch")
    path = Path(root) / "railway-release.json"
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != kwargs["expected_manifest_sha256"]:
        raise ValueError("manifest digest mismatch")
    document = json.loads(payload)
    critical = document["critical_files"]["payload.txt"]
    body = (Path(root) / "payload.txt").read_bytes()
    if len(body) != critical["bytes"] or hashlib.sha256(body).hexdigest() != critical["sha256"]:
        raise ValueError("critical byte mismatch")
    return Identity(document["tree_sha256"])

def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--receipt", required=True)
    args, _ = parser.parse_known_args()
    if os.environ.get("FAKE_SCENARIO") == "verifier_fail":
        raise SystemExit(1)
    Path(args.receipt).write_text('{"verified":true}\\n')
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
""",
        )
        _write_executable(
            self.repo / "scripts/smoke_palimpsest_mcp.py",
            """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_STATE"], "mcp-argv.json").write_text(json.dumps(sys.argv[1:]))
Path(os.environ["FAKE_STATE"], "mcp-attempted").touch()
scenario = os.environ.get("FAKE_SCENARIO", "")
if scenario == "mcp_fail_slow_clock":
    calls_path = Path(os.environ["FAKE_STATE"], "clock-calls")
    current_calls = int(calls_path.read_text())
    normal_budget = (
        int(os.environ["PALIMPSEST_RAILWAY_TRANSACTION_SECONDS"])
        - int(os.environ["PALIMPSEST_RAILWAY_ROLLBACK_RESERVE_SECONDS"])
        - 1
    )
    target_calls = normal_budget * int(os.environ["FAKE_CLOCK_DIVISOR"])
    calls_path.write_text(f"{max(current_calls, target_calls)}\\n")
if "--bootstrap-deny" in sys.argv or "--expected-publication-sha" not in sys.argv:
    raise SystemExit(12)
if "mcp_fail" in scenario:
    raise SystemExit(13)
print('{"mcp":"exact"}')
""",
        )
        (self.repo / "mcp").mkdir()
        (self.repo / "mcp/palimpsest_mcp.py").write_text(
            "# fixture\n", encoding="utf-8"
        )
        (self.repo / "server.json").write_text("{}\n", encoding="utf-8")

    def _install_commands(self) -> None:
        _write_executable(
            self.bin / "python-handoff",
            """#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_SCENARIO:-}" == mcp_fail_signal_post_recovery \
  && "${TX_PHASE:-}" == rollback_reconciliation \
  && "${TX_RESULT:-}" == failed_reconciling \
  && "${TX_ROLLBACK_RESULT:-}" == passed \
  && ! -e "$FAKE_STATE/handoff-entered" ]]; then
  touch "$FAKE_STATE/handoff-entered"
  while [[ ! -e "$FAKE_STATE/handoff-release" ]]; do
    sleep 0.01
  done
fi
exec "$FAKE_REAL_PYTHON" "$@"
""",
        )
        _write_executable(
            self.bin / "date",
            """#!/usr/bin/env bash
set -euo pipefail
calls=$(cat "$FAKE_STATE/clock-calls")
printf '%s\n' "$((1000 + calls / ${FAKE_CLOCK_DIVISOR:-40}))"
printf '%s\n' "$((calls + 1))" > "$FAKE_STATE/clock-calls"
""",
        )
        _write_executable(
            self.bin / "git",
            """#!/usr/bin/env bash
set -euo pipefail
args="$*"
case "$args" in
  *" fetch "*|*" fetch --"*) exit 0 ;;
  *"rev-parse HEAD"*) printf '%s\n' "$PUBLICATION_SHA" ;;
  *"rev-parse refs/remotes/origin/main"*) printf '%s\n' "$PUBLICATION_SHA" ;;
  *"status --porcelain"*) exit 0 ;;
  *"cat-file -e"*)
    test "${FAKE_SCENARIO:-}" != ancestry_missing
    ;;
  *"merge-base --is-ancestor"*)
    test "${FAKE_SCENARIO:-}" != ancestry_fail
    ;;
  *) exit 2 ;;
esac
""",
        )
        _write_executable(
            self.bin / "curl",
            """#!/usr/bin/env bash
set -euo pipefail
destination=""
while (($#)); do
  if [[ "$1" == --output ]]; then destination="$2"; shift 2; else shift; fi
done
test -n "$destination"
source="$FAKE_PREVIOUS_MANIFEST"
if [[ -e "$FAKE_STATE/submitted" && ! -e "$FAKE_STATE/rolled-back" ]]; then
  case "${FAKE_SCENARIO:-success}" in
    preserved_mcp_fail|undiscovered|list_fail|late|crashed) ;;
    cancel_race_success)
      [[ ! -e "$FAKE_STATE/cancel-race" ]] || source="$FAKE_CANDIDATE_MANIFEST" ;;
    mcp_fail_predecessor_restored)
      [[ -e "$FAKE_STATE/mcp-attempted" ]] || source="$FAKE_CANDIDATE_MANIFEST" ;;
    *) source="$FAKE_CANDIDATE_MANIFEST" ;;
  esac
fi
if [[ -e "$FAKE_STATE/rolled-back" \
  && "${FAKE_SCENARIO:-}" == mcp_fail_rollback_bytes_mismatch ]]; then
  source="$FAKE_CANDIDATE_MANIFEST"
fi
cp "$source" "$destination"
""",
        )
        _write_executable(
            self.bin / "railway",
            f"""#!/usr/bin/env bash
set -euo pipefail
increment() {{
  local path="$FAKE_STATE/$1" value=0
  [[ ! -e "$path" ]] || value=$(cat "$path")
  printf '%s\n' "$((value + 1))" > "$path"
}}
command_name="${{1:-}}"
shift || true
if [[ "$command_name" == api ]]; then
  query="${{1:-}}"
  if [[ "$query" == *PalimpsestProjectToken* ]]; then
    printf '%s\n' '{{"data":{{"projectToken":{{"projectId":"{PROJECT_ID}","environmentId":"{ENVIRONMENT_ID}"}}}}}}'
  elif [[ "$query" == *PalimpsestRollbackTarget* ]]; then
    increment rollback-query-count
    if [[ "${{FAKE_SCENARIO:-}}" == mcp_fail_signal_reconciliation ]]; then
      touch "$FAKE_STATE/rollback-reconciling"
      sleep 30
    fi
    printf '%s\n' '{{"data":{{"deployment":{{"id":"{PREVIOUS_DEPLOYMENT_ID}","canRollback":true}}}}}}'
  elif [[ "$query" == *PalimpsestCancel* ]]; then
    [[ "$query" == 'mutation PalimpsestCancel($id: String!) {{ deploymentCancel(id: $id) }}' ]] || exit 8
    [[ " $* " == *" id={CANDIDATE_DEPLOYMENT_ID} "* ]] || exit 8
    increment cancel-count
    if [[ "${{FAKE_SCENARIO:-}}" == cancel_race_success ]]; then
      touch "$FAKE_STATE/cancel-race"
    else
      touch "$FAKE_STATE/canceled"
    fi
    printf '%s\n' '{{"data":{{"deploymentCancel":true}}}}'
  elif [[ "$query" == *PalimpsestRollback* ]]; then
    increment rollback-count
    touch "$FAKE_STATE/rolled-back"
    printf '%s\n' '{{"data":{{"deploymentRollback":true}}}}'
    if [[ "${{FAKE_SCENARIO:-}}" == mcp_fail_rollback_api_error_success ]]; then
      exit 17
    fi
  else
    exit 4
  fi
  exit 0
fi
if [[ "$command_name" == up ]]; then
  increment upload-count
  message=""
  while (($#)); do
    if [[ "$1" == --message ]]; then message="$2"; shift 2; else shift; fi
  done
  printf '%s' "$message" > "$FAKE_STATE/message"
  touch "$FAKE_STATE/submitted"
  if [[ "${{FAKE_SCENARIO:-}}" == signal_hang ]]; then sleep 30; fi
  if [[ "${{FAKE_SCENARIO:-}}" == ambiguous_up ]]; then
    printf '%s\n' '{{"accepted":true}}'
    exit 0
  fi
  reported_id="{CANDIDATE_DEPLOYMENT_ID}"
  if [[ "${{FAKE_SCENARIO:-}}" == up_id_mismatch ]]; then
    reported_id="{UNRELATED_DEPLOYMENT_ID}"
  fi
  printf '{{"deploymentId":"%s","logsUrl":"https://railway.com/project/{PROJECT_ID}/service/{SERVICE_ID}"}}\n' "$reported_id"
  if [[ "${{FAKE_SCENARIO:-}}" == up_error_success ]]; then exit 17; fi
  exit 0
fi
if [[ "$command_name" == status ]]; then
  if [[ -e "$FAKE_STATE/submitted" && "${{FAKE_SCENARIO:-}}" == mcp_fail_unavailable ]]; then
    exit 19
  fi
  id="{PREVIOUS_DEPLOYMENT_ID}" digest="{PREVIOUS_DIGEST}" reason=deploy status=SUCCESS
  created_at=2026-08-27T00:00:00Z
  if [[ "${{FAKE_SCENARIO:-}}" == old_predecessor && ! -e "$FAKE_STATE/submitted" ]]; then
    created_at=2026-01-01T00:00:00Z
  fi
  if [[ "${{FAKE_SCENARIO:-}}" == missing_reason ]]; then reason=""; fi
  if [[ -e "$FAKE_STATE/submitted" && ! -e "$FAKE_STATE/rolled-back" ]]; then
    case "${{FAKE_SCENARIO:-success}}" in
      undiscovered|list_fail|late|crashed) ;;
      cancel_race_success)
        if [[ -e "$FAKE_STATE/cancel-race" ]]; then
          id="{CANDIDATE_DEPLOYMENT_ID}"; digest="{CANDIDATE_DIGEST}"
        fi ;;
      mcp_fail_predecessor_restored)
        if [[ ! -e "$FAKE_STATE/mcp-attempted" ]]; then
          id="{CANDIDATE_DEPLOYMENT_ID}"; digest="{CANDIDATE_DIGEST}"
        fi ;;
      mcp_fail_unrelated)
        id="{UNRELATED_DEPLOYMENT_ID}"; digest="{UNRELATED_DIGEST}" ;;
      *) id="{CANDIDATE_DEPLOYMENT_ID}"; digest="{CANDIDATE_DIGEST}" ;;
    esac
  fi
  if [[ -e "$FAKE_STATE/rolled-back" ]]; then
    id="{ROLLBACK_DEPLOYMENT_ID}"
    digest="{PREVIOUS_DIGEST}"
    reason=deploymentRollback
    created_at=2026-08-27T00:00:00Z
  fi
  printf '{{"id":"{PROJECT_ID}","environments":{{"edges":[{{"node":{{"id":"{ENVIRONMENT_ID}","serviceInstances":{{"edges":[{{"node":{{"serviceId":"{SERVICE_ID}","latestDeployment":{{"id":"%s","status":"%s","createdAt":"%s","meta":{{"imageDigest":"%s","reason":"%s"}}}}}}}}]}}}}}}]}}}}\n' \
    "$id" "$status" "$created_at" "$digest" "$reason"
  exit 0
fi
if [[ "$command_name" == deployment && "${{1:-}}" == list ]]; then
  if [[ "${{FAKE_SCENARIO:-}}" == list_fail ]]; then exit 23; fi
  if [[ "${{FAKE_SCENARIO:-}}" == undiscovered ]]; then printf '[]\n'; exit 0; fi
  message=$(cat "$FAKE_STATE/message")
  status=SUCCESS digest="{CANDIDATE_DIGEST}" reason=deploy
  case "${{FAKE_SCENARIO:-success}}" in
    late)
      if [[ -e "$FAKE_STATE/canceled" ]]; then status=REMOVED; else status=BUILDING; fi ;;
    cancel_race_success)
      if [[ -e "$FAKE_STATE/cancel-race" ]]; then status=SUCCESS; else status=BUILDING; fi ;;
    crashed) status=CRASHED ;;
    success_no_digest) digest="" ;;
    missing_candidate_reason) reason="" ;;
    sleeping) status=SLEEPING ;;
  esac
  printf '[{{"id":"{CANDIDATE_DEPLOYMENT_ID}","status":"%s","createdAt":"2026-08-27T00:00:00Z","meta":{{"cliMessage":"%s","imageDigest":"%s","reason":"%s","buildOnly":false}}}}]\n' \
    "$status" "$message" "$digest" "$reason"
  exit 0
fi
exit 3
""",
        )

    def environment(
        self,
        scenario: str,
        *,
        transaction_seconds: int = 68,
        reserve_seconds: int = 64,
        minimum_mutation_seconds: int = 2,
        clock_divisor: int = 30,
        exclusive_writer_ack: str | None = "palimpsest-github-environment-v1",
    ) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("RAILWAY_EXCLUSIVE_WRITER_ACK", None)
        environment.update(
            {
                "PATH": f"{self.bin}:{environment['PATH']}",
                "PUBLICATION_SHA": PUBLICATION_SHA,
                "RIGHTS_ADMISSION_EPOCH": "1787788800",
                "PAGES_RIGHTS_RECEIPT_SHA256": hashlib.sha256(
                    RIGHTS_PAYLOAD
                ).hexdigest(),
                "RAILWAY_TOKEN": "project-token-fixture",
                "RAILWAY_PROJECT_ID": PROJECT_ID,
                "RAILWAY_ENVIRONMENT_ID": ENVIRONMENT_ID,
                "RAILWAY_SERVICE_ID": SERVICE_ID,
                "RAILWAY_PROVIDER_ORIGIN": "https://palimpsest-publication-production.up.railway.app",
                "RAILWAY_PUBLIC_ORIGIN": "https://www.palimpsest.info",
                "RAILWAY_RELEASE_ROOT": str(self.release),
                "RAILWAY_CONTROL_DIRECTORY": str(self.control),
                "RAILWAY_EVIDENCE_DIRECTORY": str(self.evidence),
                "PALIMPSEST_RAILWAY_PYTHON": sys.executable,
                "PALIMPSEST_RAILWAY_TRANSACTION_SECONDS": str(transaction_seconds),
                "PALIMPSEST_RAILWAY_ROLLBACK_RESERVE_SECONDS": str(reserve_seconds),
                "PALIMPSEST_RAILWAY_MINIMUM_MUTATION_SECONDS": str(
                    minimum_mutation_seconds
                ),
                "PALIMPSEST_RAILWAY_COMMAND_TIMEOUT_SECONDS": "2",
                "PALIMPSEST_RAILWAY_VERIFIER_TIMEOUT_SECONDS": "2",
                "PALIMPSEST_RAILWAY_MCP_TIMEOUT_SECONDS": "2",
                "PALIMPSEST_RAILWAY_ROLLBACK_COMMAND_TIMEOUT_SECONDS": "1",
                "PALIMPSEST_RAILWAY_ROLLBACK_CANCEL_TIMEOUT_SECONDS": "1",
                "PALIMPSEST_RAILWAY_ROLLBACK_RESTORE_TIMEOUT_SECONDS": "1",
                "PALIMPSEST_RAILWAY_PREDECESSOR_MAX_AGE_SECONDS": "31536000",
                "PALIMPSEST_RAILWAY_CANDIDATE_MAX_AGE_SECONDS": "86400",
                "PALIMPSEST_RAILWAY_DISCOVERY_INTERVAL_SECONDS": "0",
                "PALIMPSEST_RAILWAY_POLL_INTERVAL_SECONDS": "0",
                "FAKE_SCENARIO": scenario,
                "FAKE_STATE": str(self.state),
                "FAKE_PREVIOUS_MANIFEST": str(self.previous_manifest),
                "FAKE_CANDIDATE_MANIFEST": str(self.release / "railway-release.json"),
                "FAKE_CLOCK_DIVISOR": str(clock_divisor),
                "FAKE_REAL_PYTHON": sys.executable,
                "GITHUB_RUN_ID": "731994934",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_REPOSITORY": "mrinalwadhwa/Palimpsest",
                "GITHUB_EVENT_PATH": str(self.event),
            }
        )
        if exclusive_writer_ack is not None:
            environment["RAILWAY_EXCLUSIVE_WRITER_ACK"] = exclusive_writer_ack
        if scenario == "mcp_fail_signal_post_recovery":
            environment["PALIMPSEST_RAILWAY_PYTHON"] = str(self.bin / "python-handoff")
        return environment

    def run(
        self,
        scenario: str,
        *,
        transaction_seconds: int = 68,
        reserve_seconds: int = 64,
        minimum_mutation_seconds: int = 2,
        clock_divisor: int = 30,
        exclusive_writer_ack: str | None = "palimpsest-github-environment-v1",
    ) -> RunResult:
        environment = self.environment(
            scenario,
            transaction_seconds=transaction_seconds,
            reserve_seconds=reserve_seconds,
            minimum_mutation_seconds=minimum_mutation_seconds,
            clock_divisor=clock_divisor,
            exclusive_writer_ack=exclusive_writer_ack,
        )
        process = subprocess.run(
            ["bash", str(self.repo / "ops/railway/deploy-continuous-release.sh")],
            cwd=self.repo,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return RunResult(process, self.root, self.state, self.evidence)


@pytest.fixture
def harness(tmp_path: Path) -> TransactionHarness:
    return TransactionHarness(tmp_path)


def test_success_is_one_exact_submission_with_strict_post_mcp(
    harness: TransactionHarness,
) -> None:
    result = harness.run("success")

    assert result.process.returncode == 0, result.process.stderr
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 0
    receipt = result.receipt
    assert receipt["status"] == "deployed"
    assert receipt["railway"]["submission_state"] == "active"
    assert receipt["railway"]["new_deployment_reason"] == "deploy"
    assert (
        receipt["railway"]["upload_reported_deployment_id"] == CANDIDATE_DEPLOYMENT_ID
    )
    assert receipt["railway"]["new_deployment_id"] == CANDIDATE_DEPLOYMENT_ID
    mcp_argv = json.loads((result.state / "mcp-argv.json").read_text())
    assert "--bootstrap-deny" not in mcp_argv
    assert mcp_argv[mcp_argv.index("--expected-publication-sha") + 1] == PUBLICATION_SHA
    evidence_names = {path.name for path in result.evidence.iterdir()}
    assert "candidate-railway-release.json" in evidence_names
    assert "candidate-pages-rights-release-receipt.json" in evidence_names
    assert "previous-release-identity.json" in evidence_names
    assert "controller-provenance.json" in evidence_names
    assert not any(name.startswith("status-") for name in evidence_names)
    provenance = json.loads(
        (result.evidence / "controller-provenance.json").read_text()
    )
    assert set(provenance["dispatch"]) == {
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
    assert provenance["dispatch"]["controller_artifact_id"] == 99
    assert "should_not_escape" not in provenance["dispatch"]
    evidence_payload = b"\n".join(
        path.read_bytes() for path in result.evidence.iterdir() if path.is_file()
    )
    assert b"RAILWAY_TOKEN" not in evidence_payload
    assert b"project-token-fixture" not in evidence_payload


def test_ambiguous_up_output_is_reconciled_by_unique_message(
    harness: TransactionHarness,
) -> None:
    result = harness.run("ambiguous_up")

    assert result.process.returncode == 0, result.process.stderr
    assert result.count("upload-count") == 1
    assert result.receipt["railway"]["upload_reported_deployment_id"] is None
    assert result.receipt["railway"]["new_deployment_id"] == CANDIDATE_DEPLOYMENT_ID
    assert result.receipt["status"] == "deployed"


def test_cli_error_after_acceptance_is_reconciled_without_second_upload(
    harness: TransactionHarness,
) -> None:
    result = harness.run("up_error_success")

    assert result.process.returncode == 0, result.process.stderr
    assert result.count("upload-count") == 1
    assert result.receipt["railway"]["upload_exit_code"] == 17
    assert (
        result.receipt["railway"]["upload_reported_deployment_id"]
        == CANDIDATE_DEPLOYMENT_ID
    )
    assert result.receipt["status"] == "deployed"


def test_cli_deployment_id_disagreement_fails_closed_without_second_upload(
    harness: TransactionHarness,
) -> None:
    result = harness.run("up_id_mismatch")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("cancel-count") == 0
    assert result.count("rollback-count") == 0
    assert (
        result.receipt["railway"]["upload_reported_deployment_id"]
        == UNRELATED_DEPLOYMENT_ID
    )
    assert result.receipt["railway"]["new_deployment_id"] is None
    assert result.receipt["railway"]["candidate_observed_status"] == (
        "IDENTITY_CONFLICT"
    )
    assert result.receipt["rollback"]["result"] == (
        "refused_candidate_identity_unknown"
    )


def test_missing_exclusive_writer_ack_performs_zero_uploads(
    harness: TransactionHarness,
) -> None:
    result = harness.run("success", exclusive_writer_ack=None)

    assert result.process.returncode != 0
    assert result.count("upload-count") == 0
    assert not result.evidence.exists()
    assert "missing required continuous-release setting" in result.process.stderr


@pytest.mark.parametrize(
    "scenario",
    [
        "missing_reason",
        "ancestry_fail",
        "ancestry_missing",
        "stale_manifest",
        "tree_mismatch",
    ],
)
def test_pre_mutation_authority_failure_performs_zero_uploads(
    harness: TransactionHarness, scenario: str
) -> None:
    result = harness.run(scenario)

    assert result.process.returncode != 0
    assert result.count("upload-count") == 0
    assert result.count("rollback-count") == 0
    assert result.receipt["railway"]["submission_state"] == "not_started"


@pytest.mark.parametrize("scenario", ["undiscovered", "list_fail"])
def test_unidentified_submission_never_claims_the_previous_release_is_preserved(
    harness: TransactionHarness, scenario: str
) -> None:
    result = harness.run(scenario)

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 0
    assert result.receipt["railway"]["submission_state"] == "terminal_failed"
    assert result.receipt["rollback"]["result"] == (
        "refused_candidate_identity_unknown"
    )


def test_sleeping_candidate_is_refused_without_cancel_or_rollback(
    harness: TransactionHarness,
) -> None:
    result = harness.run("sleeping")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("cancel-count") == 0
    assert result.count("rollback-count") == 0
    assert result.receipt["railway"]["candidate_observed_status"] == "SLEEPING"
    assert result.receipt["rollback"]["result"] == "refused_candidate_sleeping"
    assert result.receipt["rollback"]["restored_deployment_id"] is None
    assert result.receipt["status"] == "failed_rollback_refused"


def test_nonterminal_candidate_is_canceled_then_predecessor_is_proven(
    harness: TransactionHarness,
) -> None:
    result = harness.run("late")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("cancel-count") == 1
    assert result.count("rollback-count") == 0
    assert result.receipt["rollback"]["result"] == (
        "terminal_candidate_predecessor_proven"
    )
    assert result.receipt["status"] == (
        "failed_candidate_terminal_previous_release_proven"
    )


def test_cancel_race_to_success_rolls_back_the_exact_candidate(
    harness: TransactionHarness,
) -> None:
    result = harness.run("cancel_race_success")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("cancel-count") == 1
    assert result.count("rollback-count") == 1
    assert result.receipt["rollback"]["result"] == "passed"
    assert result.receipt["status"] == "failed_rolled_back"


def test_terminal_failed_candidate_proves_predecessor_topology_and_bytes(
    harness: TransactionHarness,
) -> None:
    result = harness.run("crashed")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 0
    assert result.receipt["rollback"]["result"] == (
        "already_preserved_terminal_candidate"
    )
    assert result.receipt["status"] == "failed_previous_release_preserved"


def test_success_without_list_digest_fails_and_rolls_back_owned_candidate(
    harness: TransactionHarness,
) -> None:
    result = harness.run("success_no_digest")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 1
    assert result.receipt["rollback"]["result"] == "passed"


def test_missing_candidate_reason_never_becomes_an_active_identity(
    harness: TransactionHarness,
) -> None:
    result = harness.run("missing_candidate_reason")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 0
    assert result.receipt["railway"]["submission_state"] == "terminal_failed"
    assert result.receipt["rollback"]["result"] == (
        "refused_candidate_identity_unknown"
    )


@pytest.mark.parametrize("scenario", ["verifier_fail", "mcp_fail"])
def test_post_activation_proof_failure_rolls_back_exact_candidate(
    harness: TransactionHarness, scenario: str
) -> None:
    result = harness.run(scenario)

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 1
    assert result.receipt["status"] == "failed_rolled_back"
    assert result.receipt["rollback"]["result"] == "passed"
    assert (
        result.receipt["rollback"]["restored_deployment_id"] == ROLLBACK_DEPLOYMENT_ID
    )
    assert result.receipt["rollback"]["restored_image_digest"] == PREVIOUS_DIGEST
    assert result.receipt["rollback"]["restored_reason"] == "deploymentRollback"


def test_ambiguous_rollback_response_is_reconciled_without_second_mutation(
    harness: TransactionHarness,
) -> None:
    result = harness.run("mcp_fail_rollback_api_error_success")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 1
    assert result.receipt["rollback"]["result"] == (
        "passed_mutation_response_unconfirmed"
    )
    assert result.receipt["status"] == (
        "failed_rolled_back_mutation_response_unconfirmed"
    )


def test_rollback_topology_without_previous_bytes_is_not_called_restored(
    harness: TransactionHarness,
) -> None:
    result = harness.run("mcp_fail_rollback_bytes_mismatch")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 1
    assert result.receipt["rollback"]["result"] == "failed"
    assert result.receipt["status"] == "failed_rollback_failed"
    assert result.receipt["rollback"]["restored_deployment_id"] is None
    assert result.receipt["rollback"]["restored_image_digest"] is None
    assert result.receipt["rollback"]["restored_reason"] is None


def test_success_candidate_with_old_served_bytes_still_rolls_back(
    harness: TransactionHarness,
) -> None:
    result = harness.run("preserved_mcp_fail")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 1
    assert result.receipt["rollback"]["result"] == "passed"
    assert result.receipt["status"] == "failed_rolled_back"


def test_exact_predecessor_topology_then_dual_bytes_needs_no_rollback_mutation(
    harness: TransactionHarness,
) -> None:
    result = harness.run("mcp_fail_predecessor_restored")

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 0
    assert result.receipt["rollback"]["result"] == "already_preserved"
    assert result.receipt["status"] == "failed_previous_release_preserved"


@pytest.mark.parametrize(
    ("scenario", "expected_rollback"),
    [
        ("mcp_fail_unrelated", "refused_unrelated_latest"),
        ("mcp_fail_unavailable", "refused_latest_state_unavailable"),
    ],
)
def test_exclusive_writer_double_check_refuses_unowned_state(
    harness: TransactionHarness,
    scenario: str,
    expected_rollback: str,
) -> None:
    result = harness.run(scenario)

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 0
    assert result.receipt["rollback"]["result"] == expected_rollback
    assert result.receipt["status"] == "failed_rollback_refused"


def test_global_deadline_reserve_prevents_mutation(harness: TransactionHarness) -> None:
    result = harness.run(
        "success",
        transaction_seconds=67,
        reserve_seconds=64,
        minimum_mutation_seconds=4,
        clock_divisor=200,
    )

    assert result.process.returncode != 0
    assert result.count("upload-count") == 0
    assert result.receipt["railway"]["submission_state"] == "not_started"


def test_rollback_completes_inside_reserved_time_under_advancing_clock(
    harness: TransactionHarness,
) -> None:
    result = harness.run(
        "mcp_fail_slow_clock",
        transaction_seconds=100,
        reserve_seconds=64,
        minimum_mutation_seconds=2,
        clock_divisor=8,
    )

    assert result.process.returncode != 0
    assert result.count("upload-count") == 1
    assert result.count("rollback-count") == 1
    assert result.receipt["rollback"]["result"] == "passed"
    assert result.receipt["status"] == "failed_rolled_back"


def test_old_predecessor_is_accepted_under_separate_bounded_age_policy(
    harness: TransactionHarness,
) -> None:
    result = harness.run("old_predecessor")

    assert result.process.returncode == 0, result.process.stderr
    assert result.count("upload-count") == 1
    assert result.receipt["status"] == "deployed"


def test_term_after_submission_is_reconciled_without_resubmission(
    tmp_path: Path,
) -> None:
    harness = TransactionHarness(tmp_path)
    environment = harness.environment("signal_hang")
    process = subprocess.Popen(
        ["bash", str(harness.repo / "ops/railway/deploy-continuous-release.sh")],
        cwd=harness.repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while not (harness.state / "submitted").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (harness.state / "submitted").exists()
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=10)

    assert process.returncode != 0
    assert int((harness.state / "upload-count").read_text()) == 1
    assert int((harness.state / "rollback-count").read_text()) == 1
    receipt = json.loads(
        (harness.evidence / "railway-continuous-transaction.json").read_text()
    )
    assert receipt["railway"]["submission_state"] == "terminal_failed"
    assert receipt["rollback"]["result"] == "passed"
    assert receipt["status"] == "failed_rolled_back"
    assert receipt["signal"] == "TERM"


def test_term_during_post_recovery_handoff_preserves_completed_recovery(
    tmp_path: Path,
) -> None:
    harness = TransactionHarness(tmp_path)
    environment = harness.environment("mcp_fail_signal_post_recovery")
    process = subprocess.Popen(
        ["bash", str(harness.repo / "ops/railway/deploy-continuous-release.sh")],
        cwd=harness.repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    marker = harness.state / "handoff-entered"
    release = harness.state / "handoff-release"
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        assert marker.exists()
        process.send_signal(signal.SIGTERM)
        release.touch()
        process.communicate(timeout=10)
    finally:
        release.touch(exist_ok=True)
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)

    assert process.returncode != 0
    assert int((harness.state / "upload-count").read_text()) == 1
    assert int((harness.state / "rollback-query-count").read_text()) == 1
    assert int((harness.state / "rollback-count").read_text()) == 1
    receipt = json.loads(
        (harness.evidence / "railway-continuous-transaction.json").read_text()
    )
    assert receipt["railway"]["submission_state"] == "terminal_failed"
    assert receipt["rollback"]["result"] == "passed"
    assert receipt["rollback"]["restored_deployment_id"] == ROLLBACK_DEPLOYMENT_ID
    assert receipt["status"] == "failed_rolled_back"
    assert receipt["signal"] == "TERM"


def test_term_during_reconciliation_does_not_recurse_into_second_recovery(
    tmp_path: Path,
) -> None:
    harness = TransactionHarness(tmp_path)
    environment = harness.environment("mcp_fail_signal_reconciliation")
    process = subprocess.Popen(
        ["bash", str(harness.repo / "ops/railway/deploy-continuous-release.sh")],
        cwd=harness.repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    marker = harness.state / "rollback-reconciling"
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=10)

    assert process.returncode != 0
    assert int((harness.state / "upload-count").read_text()) == 1
    assert int((harness.state / "rollback-query-count").read_text()) == 1
    assert not (harness.state / "rollback-count").exists()
    receipt = json.loads(
        (harness.evidence / "railway-continuous-transaction.json").read_text()
    )
    assert receipt["railway"]["submission_state"] == "terminal_failed"
    assert receipt["status"] == "failed_rollback_interrupted"
    assert receipt["signal"] == "TERM"
