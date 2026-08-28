from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "ops" / "railway" / "run-activation-canary"
SHA = "1" * 40
TREE_SHA256 = "2" * 64
DEPLOYMENT_ID = "11111111-1111-4111-8111-111111111111"
IMAGE_DIGEST = f"sha256:{'4' * 64}"
REPOSITORY = "beepboop2025/palimpsest"
PROVIDER_ORIGIN = "https://palimpsest-publication-production.up.railway.app"
PUBLIC_ORIGIN = "https://www.palimpsest.info"
ENVIRONMENT_NAME = "palimpsest-railway-production"
ENVIRONMENT_NUMERIC_ID = 20705508397
ACK = "palimpsest-github-environment-v1"
CONTROLLER_RUN_ID = 101
RELEASE_RUN_ID = 202
NEWSWIRE_CANONICAL_SHA256 = "a" * 64
SITUATION_CANONICAL_SHA256 = "b" * 64
NEWSWIRE_RAW_SHA256 = "c" * 64
SITUATION_RAW_SHA256 = "d" * 64
BASE_SHA = "0" * 40
FRESHNESS_PATH = "readings/publication-freshness-attestation-latest.json"
CONTROLLER_REQUEST_ARTIFACT_ID = 601
CONTROLLER_OUTCOME_ARTIFACT_ID = 602
RELEASE_ARTIFACT_ID = 603
REQUESTED_AT = "2026-08-27T11:59:00Z"


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _bind_test_interpreter(
    namespace: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> str:
    expected = str(Path(sys.executable).resolve(strict=True))

    def resolve_exact(name: str) -> str:
        actual = str(Path(name).resolve(strict=True))
        assert actual == expected
        return expected

    trusted = namespace["_trusted_executable"]
    monkeypatch.setitem(trusted.__globals__, "_trusted_executable", resolve_exact)
    return expected


def _zip_artifact(path: Path, files: dict[str, bytes]) -> dict[str, object]:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 27, 12, 0, 0))
            info.external_attr = 0o100600 << 16
            archive.writestr(info, payload)
    raw = path.read_bytes()
    return {
        "digest": hashlib.sha256(raw).hexdigest(),
        "path": str(path),
        "size": len(raw),
    }


def _manifest(attestation: bytes) -> bytes:
    document = {
        "built_at": "2026-08-27T12:00:00Z",
        "critical_files": {
            FRESHNESS_PATH: {
                "bytes": len(attestation),
                "sha256": hashlib.sha256(attestation).hexdigest(),
            }
        },
        "deployment_source": "local-git-archive",
        "github_required": False,
        "schema_version": "palimpsest.railway-static-release.v1",
        "source_commit": SHA,
        "state": "artifact_ready",
        "tree_sha256": TREE_SHA256,
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _clock(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _newswire_prerequisite(
    *,
    publication_sha: str = SHA,
    stale: bool = False,
    bad_linkage: bool = False,
) -> bytes:
    now = datetime.now(UTC).replace(microsecond=0)
    if stale:
        now -= timedelta(hours=4)
    newswire_at = _clock(now - timedelta(minutes=5))
    situation_at = _clock(now - timedelta(minutes=4))
    document = {
        "base_push_run_id": 300,
        "base_sha": BASE_SHA,
        "created_at": _clock(now),
        "hourly_publication_enabled": False,
        "newswire": {
            "acquisition_artifact": {
                "digest": "e" * 64,
                "id": 700,
                "name": f"newswire-acquisition-{BASE_SHA}-301-1",
                "size_in_bytes": 4096,
            },
            "canonical_sha256": NEWSWIRE_CANONICAL_SHA256,
            "commit": {
                "blobs": {
                    "newswire": {"after_sha": "1" * 40, "before_sha": "2" * 40},
                    "situation": {"after_sha": "3" * 40, "before_sha": "4" * 40},
                },
                "commit_at": _clock(now - timedelta(minutes=3)),
                "run_completed_at": _clock(now - timedelta(minutes=2)),
                "run_started_at": _clock(now - timedelta(minutes=8)),
            },
            "generated_at": newswire_at,
            "raw_sha256": NEWSWIRE_RAW_SHA256,
            "run_attempt": 1,
            "run_id": 301,
        },
        "publication_contract": {"run_attempt": 1, "run_id": 302},
        "publication_sha": publication_sha,
        "repository": REPOSITORY,
        "schema_version": "palimpsest.newswire-activation-prerequisite.v1",
        "situation": {
            "canonical_sha256": SITUATION_CANONICAL_SHA256,
            "generated_at": situation_at,
            "inputs": {
                "newswire_canonical_sha256": (
                    "f" * 64 if bad_linkage else NEWSWIRE_CANONICAL_SHA256
                ),
                "newswire_generated_at": newswire_at,
            },
            "raw_sha256": SITUATION_RAW_SHA256,
        },
        "workflow_state": "disabled_manually",
    }
    return _canonical(document)


def _freshness_attestation(prerequisite: bytes) -> bytes:
    proof = json.loads(prerequisite)
    newswire = proof["newswire"]
    situation = proof["situation"]
    document = {
        "artifacts": {
            "china_situation": {
                "canonical_sha256": situation["canonical_sha256"],
                "generated_at": situation["generated_at"],
                "inputs": dict(situation["inputs"]),
                "path": "readings/china-situation-latest.json",
                "schema_version": "palimpsest-china-situation.v1",
            },
            "newswire": {
                "canonical_sha256": newswire["canonical_sha256"],
                "generated_at": newswire["generated_at"],
                "path": "readings/newswire-latest.json",
                "schema_version": "palimpsest-newswire.v1",
            },
        },
        "attested_at": proof["created_at"],
        "limitations": [
            "Metadata only; quarantined source artifacts are not republished here.",
            "No source values, observations, or per-record identifiers are included.",
            "This attestation conveys no observation or publication authority.",
            "Unavailable or restricted evidence is not a directional signal.",
        ],
        "mode": "rights-suppressed",
        "publication_allowed": False,
        "publication_sha": proof["publication_sha"],
        "rights_status": {
            "bytes": 1024,
            "path": "readings/china-publication-rights-latest.json",
            "sha256": "9" * 64,
        },
        "schema_version": "palimpsest.publication-freshness-attestation.v1",
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _live_origin(origin: str, manifest_sha256: str) -> dict[str, object]:
    return {
        "base_url": origin,
        "critical_files": {
            "all_manifest_entries_verified": True,
            "inventory_sha256": "5" * 64,
            "verified_count": 0,
            "verified_total_bytes": 0,
        },
        "health": {
            "cache_control": "no-store",
            "http_status": 200,
            "path": "/healthz",
            "source_commit": SHA,
            "tree_sha256": TREE_SHA256,
        },
        "release_manifest": {
            "built_at": "2026-08-27T12:00:00Z",
            "cache_control": "no-store",
            "http_status": 200,
            "manifest_sha256": manifest_sha256,
            "path": "/railway-release.json",
            "source_commit": SHA,
            "tree_sha256": TREE_SHA256,
        },
    }


def _write_artifact(
    directory: Path,
    manifest: bytes,
    *,
    controller_artifact_digest: str,
    controller_request_sha256: str,
    tamper: str = "",
) -> None:
    directory.mkdir(mode=0o700)
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    pages_receipt = _canonical({"status": "verified"})
    verification = {
        "deployment": {
            "created_at": "2026-08-27T12:01:00Z",
            "deployment_id": DEPLOYMENT_ID,
            "image_digest": IMAGE_DIGEST,
            "reason": f"palimpsest-continuous-{SHA}-run-{RELEASE_RUN_ID}-attempt-1",
            "status": "SUCCESS",
        },
        "live": {
            "critical_inventory_byte_identical": True,
            "manifest_byte_identical": True,
            "provider_origin": _live_origin(PROVIDER_ORIGIN, manifest_sha256),
            "public_origin": _live_origin(PUBLIC_ORIGIN, manifest_sha256),
            "public_origin_verified": True,
        },
        "preflight": {
            "checkout_source_commit": SHA,
            "current_main_source_commit": SHA,
            "expected_source_commit": SHA,
            "worktree_clean": True,
        },
        "release": {
            "built_at": "2026-08-27T12:00:00Z",
            "manifest_sha256": manifest_sha256,
            "schema_version": "palimpsest.railway-static-release.v1",
            "source_commit": SHA,
            "tree_sha256": TREE_SHA256,
        },
        "schema_version": "palimpsest.railway-continuous-release-receipt.v1",
        "status": "verified",
        "topology": {
            "cron_schedule": None,
            "environment_id": "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2",
            "latest_deployment_id": DEPLOYMENT_ID,
            "latest_deployment_reason": (
                f"palimpsest-continuous-{SHA}-run-{RELEASE_RUN_ID}-attempt-1"
            ),
            "project_id": "f7c86128-53a7-458a-a931-6628c6e61fb2",
            "service_id": "86a6f49c-b9dc-4be8-acd1-dd180c693230",
            "service_manifest": {},
            "service_name": "palimpsest-publication",
            "source_attached": False,
            "volume_instance_count": 0,
            "volume_mount_count": 0,
        },
        "verification_policy": {
            "attempts_limit": 8,
            "attempts_used": {"provider_origin": 1, "public_origin": 1},
            "maximum_deployment_age_seconds": 7200,
            "maximum_future_skew_seconds": 120,
            "maximum_release_age_seconds": 86400,
            "request_timeout_seconds": 10,
            "retry_delay_seconds": 5,
        },
        "verified_at": "2026-08-27T12:02:00Z",
    }
    verification_raw = _canonical(verification)
    transaction = {
        "candidate": {
            "manifest_sha256": (
                "f" * 64 if tamper == "candidate_manifest" else manifest_sha256
            ),
            "tree_sha256": ("e" * 64 if tamper == "candidate_tree" else TREE_SHA256),
        },
        "deadline": {
            "mutation_deadline_epoch": 1787832600,
            "started_epoch": 1787832000,
            "transaction_deadline_epoch": 1787835000,
        },
        "failure_reason": None,
        "pages_rights_receipt_sha256": hashlib.sha256(pages_receipt).hexdigest(),
        "phase": "complete",
        "previous_release": {
            "manifest_sha256": "6" * 64,
            "source_commit": "0" * 40,
            "tree_sha256": "7" * 64,
        },
        "publication_sha": "9" * 40 if tamper == "publication_sha" else SHA,
        "railway": {
            "candidate_observed_status": "SUCCESS",
            "deployment_mode": "uploaded",
            "environment_id": "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2",
            "exclusive_writer_ack": ACK,
            "new_deployment_id": DEPLOYMENT_ID,
            "new_deployment_reason": (
                f"palimpsest-continuous-{SHA}-run-{RELEASE_RUN_ID}-attempt-1"
            ),
            "new_image_digest": IMAGE_DIGEST,
            "previous_deployment_id": "22222222-2222-4222-8222-222222222222",
            "previous_deployment_reason": "previous",
            "previous_image_digest": f"sha256:{'8' * 64}",
            "project_id": "f7c86128-53a7-458a-a931-6628c6e61fb2",
            "service_id": "86a6f49c-b9dc-4be8-acd1-dd180c693230",
            "submission_state": "active",
            "terminal_status": "SUCCESS",
            "upload_exit_code": 0,
            "upload_message": (
                f"palimpsest-continuous-{SHA}-run-{RELEASE_RUN_ID}-attempt-1"
            ),
            "upload_reported_deployment_id": DEPLOYMENT_ID,
        },
        "recorded_at": "2026-08-27T12:02:00Z",
        "repository": REPOSITORY,
        "rights_admission_epoch": 1787832000,
        "rollback": {
            "restored_deployment_id": None,
            "restored_image_digest": None,
            "restored_reason": None,
            "result": "not_required",
            "target_deployment_id": None,
        },
        "run_attempt": "1",
        "run_id": str(RELEASE_RUN_ID),
        "schema_version": "palimpsest.railway-continuous-transaction.v1",
        "signal": None,
        "status": "deployed",
        "verification": {
            "mcp_rights_smoke": "verified",
            "receipt_sha256": hashlib.sha256(verification_raw).hexdigest(),
        },
        "workflow": f"{REPOSITORY}/.github/workflows/tests.yml@refs/heads/main",
    }
    (directory / "railway-continuous-verification.json").write_bytes(verification_raw)
    (directory / "railway-continuous-transaction.json").write_bytes(
        _canonical(transaction)
    )
    (directory / "candidate-railway-release.json").write_bytes(manifest)
    (directory / "candidate-pages-rights-release-receipt.json").write_bytes(
        pages_receipt
    )
    (directory / "previous-release-identity.json").write_bytes(
        _canonical({"source_commit": BASE_SHA})
    )
    (directory / "pages-mcp-rights-live-receipt.json").write_bytes(
        _canonical({"status": "verified"})
    )
    provenance = {
        "dispatch": {
            "controller_artifact_digest": controller_artifact_digest,
            "controller_artifact_id": CONTROLLER_REQUEST_ARTIFACT_ID,
            "controller_request_sha256": controller_request_sha256,
            "controller_run_attempt": 1,
            "controller_run_id": CONTROLLER_RUN_ID,
            "deploy_railway": True,
            "requested_at": REQUESTED_AT,
            "scope": "complete",
            "sha": SHA,
        },
        "event_name": "repository_dispatch",
        "repository": REPOSITORY,
        "schema_version": "palimpsest.railway-controller-provenance.v1",
        "workflow_ref": f"{REPOSITORY}/.github/workflows/tests.yml@refs/heads/main",
        "workflow_run_attempt": "1",
        "workflow_run_id": str(RELEASE_RUN_ID),
    }
    if tamper == "swapped_provenance":
        provenance["dispatch"]["controller_run_id"] = CONTROLLER_RUN_ID + 99
    (directory / "controller-provenance.json").write_bytes(_canonical(provenance))


FAKE_GH = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
fixture_root = Path(__file__).resolve().parent.parent
state_path = fixture_root / "state.json"
log_path = fixture_root / "gh.log"
state = json.loads(state_path.read_text())
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args, separators=(",", ":")) + "\n")

failure_match = state.get("fail_match", "")
if failure_match and failure_match in " ".join(args):
    raise SystemExit(int(state.get("fail_status", 42)))

sha = "1" * 40
controller_id = 101
release_id = 202

def save():
    state_path.write_text(json.dumps(state, sort_keys=True))

def emit(value):
    if isinstance(value, (dict, list)):
        print(json.dumps(value, separators=(",", ":")))
    else:
        print(value)

if args[:2] == ["auth", "status"]:
    raise SystemExit(0)

if args[:3] == ["config", "get", "http_unix_socket"]:
    if state.get("http_unix_socket"):
        emit(state["http_unix_socket"])
    raise SystemExit(0)

if args[:2] == ["variable", "get"]:
    if args[2] == "RAILWAY_PUBLICATION_ENABLED":
        emit(state["hourly"])
        raise SystemExit(0)
    if args[2] == "RAILWAY_EXCLUSIVE_WRITER_ACK":
        if not state["ack_present"]:
            raise SystemExit(1)
        emit(state["ack_value"])
        raise SystemExit(0)

if args[:2] == ["variable", "set"]:
    state["hourly"] = args[args.index("--body") + 1]
    save()
    raise SystemExit(0)

if args[:2] == ["variable", "delete"]:
    state["ack_present"] = False
    save()
    raise SystemExit(0)

if args[:2] == ["variable", "list"]:
    fields = args[args.index("--json") + 1]
    if not state["ack_present"]:
        emit([])
    elif fields == "name,value":
        emit([{"name": "RAILWAY_EXCLUSIVE_WRITER_ACK", "value": state["ack_value"]}])
    else:
        emit([{"name": "RAILWAY_EXCLUSIVE_WRITER_ACK"}])
    raise SystemExit(0)

if args[:2] == ["workflow", "run"]:
    state["dispatched"] = True
    save()
    raise SystemExit(0)

if args[:2] == ["run", "list"]:
    workflow = args[args.index("--workflow") + 1]
    if not state["dispatched"]:
        emit([])
    elif workflow == "railway-publication-controller.yml":
        emit([{"databaseId": controller_id, "event": "workflow_dispatch", "headSha": sha}])
    elif state["controller_result"] == "no_change":
        emit([])
    else:
        emit([{
            "databaseId": release_id,
            "event": "repository_dispatch",
            "headSha": sha,
            "workflowName": "Tests",
        }])
    raise SystemExit(0)

if args[:2] == ["run", "view"]:
    run_id = int(args[2])
    fields = args[args.index("--json") + 1]
    if fields == "jobs":
        emit({"jobs": [
            {"name": "contract", "conclusion": "success"},
            {"name": "Package exact complete Pages edition", "conclusion": "success"},
            {"name": "Deploy exact complete Pages edition", "conclusion": "success"},
            {"name": "Deploy and prove exact Railway publication", "conclusion": "success"},
            {"name": "Verify exact Pages and native MCP rights closure", "conclusion": "skipped"},
            {"name": "pytest", "conclusion": "success"},
        ]})
        raise SystemExit(0)
    if run_id == controller_id:
        emit({
            "databaseId": controller_id,
            "event": "workflow_dispatch",
            "workflowName": "Queue exact Railway publication",
            "headBranch": "main",
            "headSha": sha,
            "status": "completed",
            "conclusion": "success",
        })
    else:
        emit({
            "databaseId": release_id,
            "event": "repository_dispatch",
            "workflowName": "Tests",
            "headBranch": "main",
            "headSha": sha,
            "status": "completed" if state["approved"] else "waiting",
            "conclusion": "success" if state["approved"] else "",
        })
    raise SystemExit(0)

if args and args[0] == "api":
    endpoint = next((item for item in args if item.startswith("repos/")), "")
    if endpoint.endswith("/commits/main"):
        emit(sha)
        raise SystemExit(0)
    if "/actions/runs/" in endpoint and endpoint.endswith("/artifacts?per_page=100"):
        run_id = int(endpoint.split("/actions/runs/", 1)[1].split("/", 1)[0])
        artifacts = []
        for item in state["artifacts"]:
            if item["run_id"] == run_id:
                artifacts.append({
                    "digest": "sha256:" + item["digest"],
                    "expired": False,
                    "id": item["id"],
                    "name": item["name"],
                    "size_in_bytes": item["size"],
                    "workflow_run": {"id": run_id},
                })
        emit({"artifacts": artifacts, "total_count": len(artifacts)})
        raise SystemExit(0)
    if "/actions/artifacts/" in endpoint and endpoint.endswith("/zip"):
        artifact_id = int(endpoint.split("/actions/artifacts/", 1)[1].split("/", 1)[0])
        item = next(entry for entry in state["artifacts"] if entry["id"] == artifact_id)
        sys.stdout.buffer.write(Path(item["path"]).read_bytes())
        raise SystemExit(0)
    if endpoint.endswith(f"/actions/runs/{controller_id}") or endpoint.endswith(
        f"/actions/runs/{release_id}"
    ):
        emit(1)
        raise SystemExit(0)
    if endpoint.endswith("/environments/palimpsest-railway-production"):
        reviewer_id = 999 if state.get("environment_drift") else 215868371
        emit({
            "can_admins_bypass": state.get("can_admins_bypass", False),
            "deployment_branch_policy": {
                "custom_branch_policies": True,
                "protected_branches": False,
            },
            "id": 20705508397,
            "name": "palimpsest-railway-production",
            "protection_rules": [{
                "id": 63841171,
                "prevent_self_review": False,
                "reviewers": [{
                    "reviewer": {"id": reviewer_id, "login": "beepboop2025"},
                    "type": "User",
                }],
                "type": "required_reviewers",
            }, {
                "id": 999 if state.get("branch_rule_drift") else 63841172,
                "type": "branch_policy",
            }],
        })
        raise SystemExit(0)
    if endpoint.endswith(
        "/environments/palimpsest-railway-production/deployment-branch-policies?per_page=100"
    ):
        emit({
            "branch_policies": [{"id": 58388360, "name": "main", "type": "branch"}],
            "total_count": 1,
        })
        raise SystemExit(0)
    if endpoint.endswith(
        "/environments/palimpsest-railway-production/secrets?per_page=100"
    ):
        emit({"secrets": [{"name": "PALIMPSEST_RAILWAY_TOKEN"}], "total_count": 1})
        raise SystemExit(0)
    if endpoint.endswith(
        "/environments/palimpsest-railway-production/variables?per_page=100"
    ):
        variables = [{
            "name": "RAILWAY_EXCLUSIVE_WRITER_ACK",
            "value": "palimpsest-github-environment-v1",
        }]
        if state.get("environment_variable_drift"):
            variables.append({"name": "EXTRA_AUTHORITY", "value": "unexpected"})
        emit({"total_count": len(variables), "variables": variables})
        raise SystemExit(0)
    if endpoint.endswith(f"/actions/runs/{release_id}/pending_deployments"):
        if "POST" in args:
            state["approved"] = True
            save()
            raise SystemExit(0)
        emit([{
            "current_user_can_approve": True,
            "environment": {
                "id": 20705508397,
                "name": "palimpsest-railway-production",
                "node_id": "EN_kwDOExample",
            },
            "reviewers": [],
            "wait_timer": 0,
        }])
        raise SystemExit(0)

raise SystemExit(97)
"""


FAKE_CURL = r"""#!/usr/bin/env python3
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
fixture_root = Path(__file__).resolve().parent.parent
destination = Path(args[args.index("--output") + 1])
source = (
    fixture_root / "freshness-attestation.json"
    if "publication-freshness-attestation-latest.json" in args[-1]
    else fixture_root / "manifest.json"
)
shutil.copy2(source, destination)
with (fixture_root / "curl.log").open("a", encoding="utf-8") as handle:
    handle.write(args[-1] + "\n")
print("200", end="")
"""


def _prepare_harness(
    tmp_path: Path,
    *,
    tamper: str = "",
    ack_value: str = ACK,
    controller_result: str = "dispatched",
    controller_live_sha: str = BASE_SHA,
    can_admins_bypass: bool = False,
    branch_rule_drift: bool = False,
    environment_variable_drift: bool = False,
    environment_drift: bool = False,
    prerequisite_publication_sha: str = SHA,
    stale_prerequisite: bool = False,
    bad_prerequisite_linkage: bool = False,
) -> dict[str, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(mode=0o700)
    gh = fake_bin / "gh"
    curl = fake_bin / "curl"
    gh.write_text(FAKE_GH)
    curl.write_text(FAKE_CURL)
    gh.chmod(0o700)
    curl.chmod(0o700)
    prerequisite = tmp_path / "newswire-prerequisite.json"
    prerequisite.write_bytes(
        _newswire_prerequisite(
            publication_sha=prerequisite_publication_sha,
            stale=stale_prerequisite,
            bad_linkage=bad_prerequisite_linkage,
        )
    )
    prerequisite.chmod(0o600)
    attestation = tmp_path / "freshness-attestation.json"
    attestation.write_bytes(_freshness_attestation(prerequisite.read_bytes()))
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(_manifest(attestation.read_bytes()))
    request = {
        "activation_canary": True,
        "controller_repository": REPOSITORY,
        "controller_run_attempt": 1,
        "controller_run_id": CONTROLLER_RUN_ID,
        "controller_workflow_path": ".github/workflows/railway-publication-controller.yml",
        "deploy_railway": True,
        "requested_at": REQUESTED_AT,
        "schema_version": "palimpsest.railway-publication-request.v2",
        "scope": "complete",
        "sha": SHA,
    }
    request_raw = _canonical(request)
    request_archive = _zip_artifact(
        tmp_path / "controller-request.zip",
        {"railway-publication-request.json": request_raw},
    )
    outcome = {
        "activation_canary": True,
        "controller_run_attempt": 1,
        "controller_run_id": CONTROLLER_RUN_ID,
        "event": "workflow_dispatch",
        "force": False,
        "gate_enabled": False,
        "head_sha": SHA,
        "live_sha": SHA if controller_result == "no_change" else controller_live_sha,
        "main_sha": SHA,
        "recorded_at": "2026-08-27T12:00:00Z",
        "repository": REPOSITORY,
        "request_artifact_digest": (
            request_archive["digest"] if controller_result == "dispatched" else None
        ),
        "request_artifact_id": (
            CONTROLLER_REQUEST_ARTIFACT_ID
            if controller_result == "dispatched"
            else None
        ),
        "request_artifact_name": (
            f"railway-publication-request-{CONTROLLER_RUN_ID}-1"
            if controller_result == "dispatched"
            else None
        ),
        "request_artifact_size": (
            request_archive["size"] if controller_result == "dispatched" else None
        ),
        "request_sha256": (
            hashlib.sha256(request_raw).hexdigest()
            if controller_result == "dispatched"
            else None
        ),
        "requested_at": REQUESTED_AT if controller_result == "dispatched" else None,
        "result": controller_result,
        "schema_version": "palimpsest.railway-publication-controller-outcome.v1",
        "workflow": ".github/workflows/railway-publication-controller.yml",
        "workflow_name": "Queue exact Railway publication",
    }
    outcome_archive = _zip_artifact(
        tmp_path / "controller-outcome.zip",
        {"railway-publication-controller-outcome.json": _canonical(outcome)},
    )
    artifact = tmp_path / "artifact"
    _write_artifact(
        artifact,
        manifest.read_bytes(),
        controller_artifact_digest=str(request_archive["digest"]),
        controller_request_sha256=hashlib.sha256(request_raw).hexdigest(),
        tamper=tamper,
    )
    release_archive = _zip_artifact(
        tmp_path / "railway-release.zip",
        {item.name: item.read_bytes() for item in artifact.iterdir()},
    )
    if tamper == "archive_digest":
        release_archive["digest"] = "f" * 64
    artifacts = [
        {
            "digest": outcome_archive["digest"],
            "id": CONTROLLER_OUTCOME_ARTIFACT_ID,
            "name": f"railway-publication-controller-outcome-{CONTROLLER_RUN_ID}-1",
            "path": outcome_archive["path"],
            "run_id": CONTROLLER_RUN_ID,
            "size": outcome_archive["size"],
        }
    ]
    if controller_result == "dispatched":
        artifacts.extend(
            [
                {
                    "digest": request_archive["digest"],
                    "id": CONTROLLER_REQUEST_ARTIFACT_ID,
                    "name": f"railway-publication-request-{CONTROLLER_RUN_ID}-1",
                    "path": request_archive["path"],
                    "run_id": CONTROLLER_RUN_ID,
                    "size": request_archive["size"],
                },
                {
                    "digest": release_archive["digest"],
                    "id": RELEASE_ARTIFACT_ID,
                    "name": (
                        f"railway-continuous-release-{SHA}-run-{RELEASE_RUN_ID}"
                        "-attempt-1"
                    ),
                    "path": release_archive["path"],
                    "run_id": RELEASE_RUN_ID,
                    "size": release_archive["size"],
                },
            ]
        )
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "ack_present": True,
                "ack_value": ack_value,
                "approved": False,
                "artifacts": artifacts,
                "controller_result": controller_result,
                "can_admins_bypass": can_admins_bypass,
                "branch_rule_drift": branch_rule_drift,
                "dispatched": False,
                "environment_drift": environment_drift,
                "environment_variable_drift": environment_variable_drift,
                "hourly": "false",
            },
            sort_keys=True,
        )
    )
    log = tmp_path / "gh.log"
    curl_log = tmp_path / "curl.log"
    log.touch()
    curl_log.touch()
    return {
        "artifact": artifact,
        "attestation": attestation,
        "curl_log": curl_log,
        "fake_bin": fake_bin,
        "log": log,
        "manifest": manifest,
        "prerequisite": prerequisite,
        "receipt": tmp_path / "activation-canary-receipt.json",
        "state": state,
    }


def _run_helper(
    harness: dict[str, Path],
    *,
    typed_sha: str = SHA,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "ACTIVATION_CANARY_RECEIPT": str(harness["receipt"]),
            "EXPECTED_CANARY_SHA": SHA,
            "FAKE_ARTIFACT_DIR": str(harness["artifact"]),
            "FAKE_ATTESTATION": str(harness["attestation"]),
            "FAKE_CURL_LOG": str(harness["curl_log"]),
            "FAKE_GH_LOG": str(harness["log"]),
            "FAKE_GH_STATE": str(harness["state"]),
            "FAKE_MANIFEST": str(harness["manifest"]),
            "FAKE_SHA": SHA,
            "NEWSWIRE_PREREQUISITE_RECEIPT": str(harness["prerequisite"]),
            "PATH": f"{harness['fake_bin']}:{environment['PATH']}",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if extra_environment:
        if "FAKE_GH_FAIL_MATCH" in extra_environment:
            state = json.loads(harness["state"].read_text())
            state["fail_match"] = extra_environment["FAKE_GH_FAIL_MATCH"]
            state["fail_status"] = int(
                extra_environment.get("FAKE_GH_FAIL_STATUS", "42")
            )
            harness["state"].write_text(json.dumps(state, sort_keys=True))
        environment.update(extra_environment)
    return subprocess.run(
        [str(HELPER)],
        input=f"{typed_sha}\n",
        text=True,
        capture_output=True,
        check=False,
        env=environment,
        timeout=30,
    )


def _log(harness: dict[str, Path]) -> list[list[str]]:
    return [json.loads(line) for line in harness["log"].read_text().splitlines()]


def test_activation_canary_executes_exact_two_run_transaction(tmp_path: Path) -> None:
    harness = _prepare_harness(tmp_path)
    result = _run_helper(harness)

    assert result.returncode == 0, result.stderr
    raw = harness["receipt"].read_bytes()
    receipt = json.loads(raw)
    assert raw == _canonical(receipt)
    assert stat.S_IMODE(harness["receipt"].stat().st_mode) == 0o400
    assert receipt["schema_version"] == (
        "palimpsest.railway-activation-canary-receipt.v1"
    )
    assert receipt["source_commit"] == SHA
    assert receipt["controller"]["run_attempt"] == 1
    assert receipt["controller"]["run_id"] == 101
    assert receipt["controller"]["result"] == "dispatched"
    assert (
        receipt["controller"]["request_sha256"]
        == hashlib.sha256(
            _canonical(
                {
                    "activation_canary": True,
                    "controller_repository": REPOSITORY,
                    "controller_run_attempt": 1,
                    "controller_run_id": CONTROLLER_RUN_ID,
                    "controller_workflow_path": ".github/workflows/railway-publication-controller.yml",
                    "deploy_railway": True,
                    "requested_at": REQUESTED_AT,
                    "schema_version": "palimpsest.railway-publication-request.v2",
                    "scope": "complete",
                    "sha": SHA,
                }
            )
        ).hexdigest()
    )
    assert receipt["release"] == {"run_attempt": 1, "run_id": 202}
    assert receipt["policy"] == {
        "approval_budget_seconds": 5400,
        "activation_canary": True,
        "completion_budget_seconds": 4200,
        "force": False,
        "hourly_publication_enabled": False,
    }
    assert receipt["newswire_prerequisite"] == {
        "newswire_canonical_sha256": NEWSWIRE_CANONICAL_SHA256,
        "newswire_generated_at": json.loads(harness["prerequisite"].read_bytes())[
            "newswire"
        ]["generated_at"],
        "publication_sha": SHA,
        "receipt_sha256": hashlib.sha256(
            harness["prerequisite"].read_bytes()
        ).hexdigest(),
        "situation_canonical_sha256": SITUATION_CANONICAL_SHA256,
        "situation_generated_at": json.loads(harness["prerequisite"].read_bytes())[
            "situation"
        ]["generated_at"],
    }
    assert (
        receipt["live"]["freshness_attestation_sha256"]
        == hashlib.sha256(harness["attestation"].read_bytes()).hexdigest()
    )
    assert "secret_value" not in raw.decode().lower()
    assert "gd_pat_" not in raw.decode().lower()

    calls = _log(harness)
    workflow_run = next(
        index for index, call in enumerate(calls) if call[:2] == ["workflow", "run"]
    )
    controller_snapshot = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ["run", "list"] and "railway-publication-controller.yml" in call
    )
    release_snapshot = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ["run", "list"] and "tests.yml" in call
    )
    assert controller_snapshot < workflow_run
    assert release_snapshot < workflow_run
    assert calls[workflow_run][-4:] == [
        "-f",
        "activation_canary=true",
        "-f",
        "force=false",
    ]

    approval = next(
        index
        for index, call in enumerate(calls)
        if call[:3] == ["api", "--method", "POST"]
    )
    pending_reads = [
        index
        for index, call in enumerate(calls)
        if call[0] == "api"
        and call[-1].endswith("/pending_deployments")
        and "POST" not in call
    ]
    assert len(pending_reads) == 2
    assert pending_reads[-1] < approval
    assert any(
        pending_reads[-1] < index < approval
        and call[:3] == ["variable", "get", "RAILWAY_PUBLICATION_ENABLED"]
        for index, call in enumerate(calls)
    )
    assert any(
        pending_reads[-1] < index < approval
        and call[:3] == ["variable", "get", "RAILWAY_EXCLUSIVE_WRITER_ACK"]
        for index, call in enumerate(calls)
    )
    assert any(
        pending_reads[-1] < index < approval
        and call[:3] == ["run", "view", str(RELEASE_RUN_ID)]
        for index, call in enumerate(calls)
    )
    assert any(
        pending_reads[-1] < index < approval
        and call[0] == "api"
        and any(item.endswith("/commits/main") for item in call)
        for index, call in enumerate(calls)
    )
    assert not any(call[:2] == ["run", "cancel"] for call in calls)
    assert json.loads(harness["state"].read_text())["hourly"] == "false"
    assert len(harness["curl_log"].read_text().splitlines()) == 4


def test_wrong_typed_sha_closes_authority_and_reports_release(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path)
    result = _run_helper(harness, typed_sha="0" * 40)

    assert result.returncode == 1
    assert not harness["receipt"].exists()
    state = json.loads(harness["state"].read_text())
    assert state["hourly"] == "false"
    assert state["ack_present"] is False
    assert "Reconcile downstream Railway release run 202" in result.stderr
    calls = _log(harness)
    assert any(
        call[:3] == ["variable", "set", "RAILWAY_PUBLICATION_ENABLED"] for call in calls
    )
    assert any(
        call[:3] == ["variable", "delete", "RAILWAY_EXCLUSIVE_WRITER_ACK"]
        for call in calls
    )
    assert not any("POST" in call for call in calls)
    assert not any(call[:2] == ["run", "cancel"] for call in calls)


@pytest.mark.parametrize(
    "tamper", ["publication_sha", "candidate_manifest", "candidate_tree"]
)
def test_tampered_immutable_transaction_receipt_fails_closed(
    tmp_path: Path, tamper: str
) -> None:
    harness = _prepare_harness(tmp_path, tamper=tamper)
    result = _run_helper(harness)

    assert result.returncode == 1
    assert "receipt is not exact" in result.stderr
    assert not harness["receipt"].exists()
    state = json.loads(harness["state"].read_text())
    assert state["ack_present"] is False
    assert state["ack_value"] == ACK
    assert state["approved"] is True
    assert state["dispatched"] is True
    assert state["hourly"] == "false"


def test_controller_no_change_proves_live_bytes_without_downstream_run(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path, controller_result="no_change")
    result = _run_helper(harness)

    assert result.returncode == 0, result.stderr
    receipt = json.loads(harness["receipt"].read_bytes())
    assert receipt["controller"]["result"] == "no_change"
    assert receipt["release"] is None
    assert receipt["jobs"] is None
    assert receipt["artifact_evidence"] is None
    calls = _log(harness)
    assert not any("pending_deployments" in " ".join(call) for call in calls)
    assert not any(call[:3] == ["api", "--method", "POST"] for call in calls)
    assert len(harness["curl_log"].read_text().splitlines()) == 6


def test_swapped_same_sha_controller_provenance_is_rejected(tmp_path: Path) -> None:
    harness = _prepare_harness(tmp_path, tamper="swapped_provenance")
    result = _run_helper(harness)

    assert result.returncode == 1
    assert "provenance is not causal authority" in result.stderr
    assert not harness["receipt"].exists()
    assert json.loads(harness["state"].read_text())["ack_present"] is False


def test_artifact_archive_bytes_must_match_github_digest(tmp_path: Path) -> None:
    harness = _prepare_harness(tmp_path, tamper="archive_digest")
    result = _run_helper(harness)

    assert result.returncode == 1
    assert "archive bytes do not match GitHub metadata" in result.stderr
    assert not harness["receipt"].exists()


def test_dispatched_controller_cannot_claim_live_already_equals_main(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path, controller_live_sha=SHA)
    result = _run_helper(harness)

    assert result.returncode == 1
    assert "dispatched outcome is not exact" in result.stderr
    assert not harness["receipt"].exists()


def test_environment_policy_drift_blocks_protected_approval(tmp_path: Path) -> None:
    harness = _prepare_harness(tmp_path, environment_drift=True)
    result = _run_helper(harness)

    assert result.returncode == 1
    assert "reviewer identity is not exact" in result.stderr
    assert not harness["receipt"].exists()
    state = json.loads(harness["state"].read_text())
    assert state["approved"] is False
    assert state["ack_present"] is False


def test_environment_admin_bypass_drift_blocks_protected_approval(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path, can_admins_bypass=True)
    result = _run_helper(harness)

    assert result.returncode == 1
    assert "environment identity is not exact" in result.stderr
    assert not harness["receipt"].exists()
    state = json.loads(harness["state"].read_text())
    assert state["approved"] is False
    assert state["ack_present"] is False


def test_environment_branch_rule_drift_blocks_protected_approval(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path, branch_rule_drift=True)
    result = _run_helper(harness)

    assert result.returncode == 1
    assert "branch rule is not exact" in result.stderr
    assert not harness["receipt"].exists()


def test_environment_variable_shadowing_blocks_protected_approval(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path, environment_variable_drift=True)
    result = _run_helper(harness)

    assert result.returncode == 1
    assert "variable inventory is not exact" in result.stderr
    assert not harness["receipt"].exists()


def test_newswire_prerequisite_rejects_writable_parent_and_initial_symlink(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(HELPER))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "receipt.json"
    target.write_bytes(_newswire_prerequisite())
    target.chmod(0o600)
    private.chmod(0o770)
    with pytest.raises(namespace["CanaryError"], match="directory is not private"):
        namespace["_load_newswire_prerequisite"](
            target, repository=REPOSITORY, expected_sha=SHA
        )
    private.chmod(0o700)
    link = private / "receipt-link.json"
    link.symlink_to(target)
    with pytest.raises(namespace["CanaryError"], match="opened safely"):
        namespace["_load_newswire_prerequisite"](
            link, repository=REPOSITORY, expected_sha=SHA
        )


def test_newswire_descriptor_read_is_not_redirected_by_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = runpy.run_path(str(HELPER))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    receipt = private / "receipt.json"
    receipt.write_bytes(_newswire_prerequisite())
    receipt.chmod(0o600)
    replacement = private / "replacement.json"
    replacement.write_bytes(_newswire_prerequisite(publication_sha="f" * 40))
    replacement.chmod(0o600)
    original_open = namespace["os"].open
    swapped = False

    def swapping_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == receipt and not swapped:
            swapped = True
            receipt.unlink()
            receipt.symlink_to(replacement)
        return descriptor

    monkeypatch.setattr(namespace["os"], "open", swapping_open)
    with pytest.raises(namespace["CanaryError"], match="private regular file"):
        namespace["_load_newswire_prerequisite"](
            receipt, repository=REPOSITORY, expected_sha=SHA
        )
    assert receipt.is_symlink()


def test_command_environment_drops_ambient_transport_and_loader_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(HELPER))
    hostile = {
        "CURL_CA_BUNDLE": "/tmp/forged-ca",
        "SSL_CERT_FILE": "/tmp/forged-cert",
        "SSL_CERT_DIR": "/tmp/forged-dir",
        "CURL_SSL_BACKEND": "forged",
        "LD_PRELOAD": "/tmp/forged.so",
        "DYLD_INSERT_LIBRARIES": "/tmp/forged.dylib",
        "GH_CONFIG_DIR": "/tmp/forged-gh",
        "XDG_CONFIG_HOME": "/tmp/forged-xdg",
        "HTTPS_PROXY": "http://forged.invalid",
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)
    gh_environment = namespace["_command_environment"]("/usr/bin/gh")
    curl_environment = namespace["_command_environment"]("/usr/bin/curl")
    assert not set(hostile).intersection(gh_environment)
    assert not set(hostile).intersection(curl_environment)
    assert gh_environment["GH_HOST"] == "github.com"
    assert "HOME" not in curl_environment


def test_github_unix_socket_override_stops_before_dispatch(tmp_path: Path) -> None:
    harness = _prepare_harness(tmp_path)
    state = json.loads(harness["state"].read_text())
    state["http_unix_socket"] = "/tmp/forged-github.sock"
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_helper(harness)

    assert result.returncode == 1
    assert "http_unix_socket transport is not empty" in result.stderr
    assert not any(call[:2] == ["workflow", "run"] for call in _log(harness))
    state = json.loads(harness["state"].read_text())
    assert state["hourly"] == "false"
    assert state["ack_present"] is False


def test_failure_cleanup_preserves_original_command_exit_status(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path)
    result = _run_helper(
        harness,
        extra_environment={
            "FAKE_GH_FAIL_MATCH": "run view 101",
            "FAKE_GH_FAIL_STATUS": "42",
        },
    )

    assert result.returncode == 42
    state = json.loads(harness["state"].read_text())
    assert state["hourly"] == "false"
    assert state["ack_present"] is False


def test_failure_cleanup_never_deletes_an_unfamiliar_acknowledgement(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path, ack_value="unfamiliar-writer")
    result = _run_helper(harness)

    assert result.returncode == 1
    assert "refusing to delete an unfamiliar" in result.stderr
    assert json.loads(harness["state"].read_text())["ack_present"] is True
    assert not any(call[:2] == ["variable", "delete"] for call in _log(harness))


@pytest.mark.parametrize(
    "failure",
    ["missing", "stale", "wrong_sha", "bad_linkage", "public_mode"],
)
def test_invalid_newswire_prerequisite_clears_armed_authority(
    tmp_path: Path, failure: str
) -> None:
    harness = _prepare_harness(
        tmp_path,
        prerequisite_publication_sha="f" * 40 if failure == "wrong_sha" else SHA,
        stale_prerequisite=failure == "stale",
        bad_prerequisite_linkage=failure == "bad_linkage",
    )
    if failure == "missing":
        harness["prerequisite"].unlink()
    elif failure == "public_mode":
        harness["prerequisite"].chmod(0o644)

    result = _run_helper(harness)

    assert result.returncode == 2
    state = json.loads(harness["state"].read_text())
    assert state["hourly"] == "false"
    assert state["ack_present"] is False
    calls = _log(harness)
    assert any(
        call[:3] == ["variable", "set", "RAILWAY_PUBLICATION_ENABLED"] for call in calls
    )
    assert any(
        call[:3] == ["variable", "delete", "RAILWAY_EXCLUSIVE_WRITER_ACK"]
        for call in calls
    )
    assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_process_group_timeout_terminates_a_spawned_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(HELPER))
    interpreter = _bind_test_interpreter(namespace, monkeypatch)
    child_started = tmp_path / "child-started"
    child_terminated = tmp_path / "child-terminated"
    child = tmp_path / "child.py"
    child.write_text(
        """
import os
import signal
import sys
import time
from pathlib import Path

started = Path(sys.argv[1])
terminated = Path(sys.argv[2])

def stop(_signum, _frame):
    terminated.write_text(str(os.getpid()))
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
started.write_text(str(os.getpid()))
while True:
    time.sleep(1)
""".lstrip()
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        """
import subprocess
import sys
import time

subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]])
while True:
    time.sleep(1)
""".lstrip()
    )

    started_at = time.monotonic()
    with pytest.raises(namespace["CommandError"]) as captured:
        namespace["_bounded_command"](
            (
                interpreter,
                str(parent),
                str(child),
                str(child_started),
                str(child_terminated),
            ),
            timeout=0.5,
            label="descendant fixture",
        )
    elapsed = time.monotonic() - started_at

    assert captured.value.returncode == 124
    assert elapsed < 3
    assert child_started.exists()
    assert child_terminated.exists()


def test_freshness_attestation_is_exactly_bound_to_prerequisite(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path)
    namespace = runpy.run_path(str(HELPER))
    prerequisite = namespace["_load_newswire_prerequisite"](
        harness["prerequisite"], repository=REPOSITORY, expected_sha=SHA
    )
    raw = harness["attestation"].read_bytes()
    expected_digest = hashlib.sha256(raw).hexdigest()

    assert (
        namespace["_validate_freshness_attestation"](
            raw, expected_sha=SHA, newswire=prerequisite
        )
        == expected_digest
    )

    forged = json.loads(raw)
    forged["artifacts"]["china_situation"]["canonical_sha256"] = "0" * 64
    forged_raw = (json.dumps(forged, indent=2, sort_keys=True) + "\n").encode()
    with pytest.raises(namespace["CanaryError"]):
        namespace["_validate_freshness_attestation"](
            forged_raw, expected_sha=SHA, newswire=prerequisite
        )


def test_live_manifest_must_seal_freshness_attestation_path_size_and_digest(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path)
    namespace = runpy.run_path(str(HELPER))
    attestation = harness["attestation"].read_bytes()
    manifest = json.loads(harness["manifest"].read_bytes())
    manifest["critical_files"][FRESHNESS_PATH]["bytes"] += 1
    forged_raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()

    with pytest.raises(namespace["CanaryError"]):
        namespace["_validate_live_manifest"](
            forged_raw,
            label="fixture",
            expected_sha=SHA,
            expected_manifest_sha256=hashlib.sha256(forged_raw).hexdigest(),
            freshness_raw=attestation,
        )


def _configure_unit_main(
    namespace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    harness: dict[str, Path],
    private_directory: Path,
) -> list[tuple[str, int | None]]:
    monkeypatch.setenv("EXPECTED_CANARY_SHA", SHA)
    monkeypatch.setenv("ACTIVATION_CANARY_RECEIPT", str(harness["receipt"]))
    monkeypatch.setenv("NEWSWIRE_PREREQUISITE_RECEIPT", str(harness["prerequisite"]))
    monkeypatch.delenv("ACTIVATION_CANARY", raising=False)
    monkeypatch.delenv("FORCE", raising=False)
    monkeypatch.setattr(sys, "argv", [str(HELPER)])
    monkeypatch.setattr(
        namespace["tempfile"], "mkdtemp", lambda **_kwargs: str(private_directory)
    )
    cleanup_calls: list[tuple[str, int | None]] = []

    def fake_execute(**arguments):
        arguments["run_state"].release_run_id = RELEASE_RUN_ID
        return {"schema_version": "test-receipt"}, RELEASE_RUN_ID

    def fake_cleanup(repository: str, release_run_id: int | None) -> bool:
        cleanup_calls.append((repository, release_run_id))
        return True

    namespace["main"].__globals__["_execute"] = fake_execute
    namespace["main"].__globals__["_failure_cleanup"] = fake_cleanup
    return cleanup_calls


def test_rmtree_failure_happens_before_receipt_commit_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _prepare_harness(tmp_path)
    namespace = runpy.run_path(str(HELPER))
    private_directory = tmp_path / "private-canary"
    private_directory.mkdir(mode=0o700)
    cleanup_calls = _configure_unit_main(
        namespace, monkeypatch, harness, private_directory
    )
    original_rmtree = namespace["shutil"].rmtree

    def fail_rmtree(_path: Path) -> None:
        raise OSError("injected rmtree failure")

    monkeypatch.setattr(namespace["shutil"], "rmtree", fail_rmtree)
    write_calls: list[Path] = []

    def forbidden_write(path: Path, *_args, **_kwargs) -> str:
        write_calls.append(path)
        raise AssertionError("receipt write must follow successful cleanup")

    namespace["main"].__globals__["_write_final_receipt"] = forbidden_write
    try:
        status = namespace["main"]()
    finally:
        original_rmtree(private_directory)

    assert status == 1
    assert write_calls == []
    assert not harness["receipt"].exists()
    assert cleanup_calls == [(REPOSITORY, RELEASE_RUN_ID)]


def test_broken_stdout_after_receipt_commit_is_warning_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _prepare_harness(tmp_path)
    namespace = runpy.run_path(str(HELPER))
    private_directory = tmp_path / "private-canary"
    private_directory.mkdir(mode=0o700)
    cleanup_calls = _configure_unit_main(
        namespace, monkeypatch, harness, private_directory
    )

    class BrokenStdout:
        def write(self, _value: str) -> int:
            raise BrokenPipeError("injected stdout failure")

        def flush(self) -> None:
            raise BrokenPipeError("injected stdout failure")

        def fileno(self) -> int:
            raise OSError("no descriptor")

    monkeypatch.setattr(sys, "stdout", BrokenStdout())
    status = namespace["main"]()

    assert status == 0
    assert harness["receipt"].is_file()
    assert json.loads(harness["receipt"].read_bytes()) == {
        "schema_version": "test-receipt"
    }
    assert cleanup_calls == []


def test_helper_static_authority_contract() -> None:
    source = HELPER.read_text()
    assert "APPROVAL_BUDGET_SECONDS = 5400" in source
    assert "COMPLETION_BUDGET_SECONDS = 4200" in source
    assert "PROMPT_TIMEOUT_SECONDS = 300" in source
    assert '"activation_canary=true"' in source
    assert '"force=false"' in source
    assert "time.monotonic() + budget_seconds" in source
    approval = source.index("Deadline.start(APPROVAL_BUDGET_SECONDS)")
    approval_post = source.index('"POST",', approval)
    completion = source.index("Deadline.start(COMPLETION_BUDGET_SECONDS)")
    downstream_wait = source.index("_wait_for_success(", completion)
    assert approval < approval_post < completion < downstream_wait
    assert "start_new_session=True" in source
    assert "os.killpg(process_group_id, signal.SIGTERM)" in source
    assert "os.killpg(process_group_id, signal.SIGKILL)" in source
    assert "NEWSWIRE_PREREQUISITE_RECEIPT" in source
    assert "publication-freshness-attestation-latest.json" in source
    assert "cleanup did not cancel it" in source
    assert '"run",\n                "cancel"' not in source
    compile(source, str(HELPER), "exec")


@pytest.mark.parametrize(
    ("activation_canary", "force"),
    [("false", "false"), ("true", "true"), ("1", "0")],
)
def test_helper_rejects_any_policy_other_than_exact_canary(
    tmp_path: Path, activation_canary: str, force: str
) -> None:
    harness = _prepare_harness(tmp_path)
    result = _run_helper(
        harness,
        extra_environment={"ACTIVATION_CANARY": activation_canary, "FORCE": force},
    )

    assert result.returncode == 2
    assert "accepts only ACTIVATION_CANARY=true and FORCE=false" in result.stderr
    assert _log(harness) == []
