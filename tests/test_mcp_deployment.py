"""Offline contract tests for the exact-SHA Palimpsest MCP release path."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

import mcp.palimpsest_mcp as server
from scripts import smoke_palimpsest_mcp as smoke


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "ops/mcp-deploy/palimpsest-mcp-deploy-wrapper.sh"
VERIFIER_PATH = ROOT / "ops/mcp-deploy/verify_release.py"
REGISTRY_VERIFIER_PATH = ROOT / "ops/mcp-deploy/verify_registry_release.py"
WORKFLOW = ROOT / ".github/workflows/deploy-mcp.yml"
REGISTRY_WORKFLOW = ROOT / ".github/workflows/registry-publish.yml"
UNIT = ROOT / "ops/systemd/palimpsest-mcp.service"
RUNBOOK = ROOT / "ops/mcp-deploy/README.md"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mcp_release_verifier", VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


def _load_registry_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "mcp_registry_release_verifier", REGISTRY_VERIFIER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


registry_verifier = _load_registry_verifier()


def test_current_candidate_satisfies_release_contract() -> None:
    contract = verifier.verify_candidate(
        ROOT / "mcp/palimpsest_mcp.py",
        ROOT / "server.json",
    )
    assert contract == {
        "version": "1.9.0",
        "server_name": "palimpsest",
        "tools": [
            "get_newsroom",
            "get_signal",
            "gfw_reading",
            "list_signals",
            "query_economic_observations",
            "whats_happening",
        ],
        "prompts": [
            "censorship_briefing",
            "evidence_desk_briefing",
            "gfw_status_check",
            "signal_deep_dive",
        ],
    }


def test_verifier_rejects_manifest_version_drift(tmp_path: Path) -> None:
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    manifest["version"] = "1.9.1"
    path = tmp_path / "server.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="SERVER_VERSION"):
        verifier.verify_candidate(ROOT / "mcp/palimpsest_mcp.py", path)


def test_verifier_rejects_duplicate_manifest_keys(tmp_path: Path) -> None:
    path = tmp_path / "server.json"
    path.write_text('{"name":"a","name":"b","version":"1.9.0"}', encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="duplicate JSON key"):
        verifier.verify_candidate(ROOT / "mcp/palimpsest_mcp.py", path)


def test_verifier_requires_exact_valid_github_signature(tmp_path: Path) -> None:
    target = "a" * 40
    payload = {
        "sha": target,
        "author": {"login": "beepboop2025"},
        "committer": {"login": "web-flow"},
        "parents": [{"sha": "b" * 40}, {"sha": "c" * 40}],
        "commit": {
            "verification": {
                "verified": True,
                "reason": "valid",
                "verified_at": "2026-08-22T12:11:43Z",
            },
        },
    }
    path = tmp_path / "commit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    verifier.verify_github_commit(path, target)

    payload["commit"]["verification"]["verified"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="valid verified signature"):
        verifier.verify_github_commit(path, target)


class _DispatchHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length))
        response = server.dispatch(message)
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_live_smoke_covers_initialize_discovery_and_interconnection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_fetch",
        lambda name: (
            {"situations": [{"event_id": "fixture"}]}
            if name == "china-situation"
            else {}
        ),
    )
    contract = smoke.load_contract(
        ROOT / "mcp/palimpsest_mcp.py",
        ROOT / "server.json",
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _DispatchHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/"
        smoke.validate_url(url, allow_http_loopback=True)
        result = smoke.probe(url, contract, timeout=2)
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()

    assert result["version"] == "1.9.0"
    assert result["tool_count"] == 6
    assert result["prompt_count"] == 4
    assert result["calls"] == ["list_signals", "get_newsroom:interconnection"]


@pytest.mark.parametrize(
    "url",
    [
        "http://api.seiche.info/palimpsest/mcp",
        "file:///etc/passwd",
        f"https://user:secret{chr(64)}api.seiche.info/palimpsest/mcp",
        "https://api.seiche.info/palimpsest/mcp?redirect=elsewhere",
    ],
)
def test_live_smoke_rejects_unsafe_endpoint_urls(url: str) -> None:
    with pytest.raises(smoke.SmokeError):
        smoke.validate_url(url, allow_http_loopback=False)


def test_host_wrapper_is_syntax_valid_and_fail_closed() -> None:
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)
    text = WRAPPER.read_text(encoding="utf-8")
    required = [
        '[[ ! "$original_command" =~ ^deploy\\ ([0-9a-f]{40})$ ]]',
        "merge-base --is-ancestor",
        "fetch.fsckObjects true",
        "github_signature",
        "--github-commit-json",
        'run_as_verify_user "$VERIFY_RELEASE"',
        'run_as_verify_user "$SMOKE"',
        'env -i PATH="$PATH"',
        'expected_blob=$(git --git-dir="$REPOSITORY" rev-parse',
        "flock -n",
        'mv -fT -- "$candidate_tmp" "$TARGET_FILE"',
        'systemctl restart "$SERVICE"',
        '"$SMOKE" --url "$LOCAL_ENDPOINT"',
        "rollback",
        "DEPLOYED_SHA_FILE",
        "require_hardened_runtime",
        'require_service_value "User" "$RUNTIME_USER"',
        'require_service_value "NoNewPrivileges" "yes"',
        "main_pid=$(systemctl show --property=MainPID",
        "exec_main_pid=$(systemctl show --property=ExecMainPID",
        'process_uid=$(ps -o uid= -p "$main_pid"',
    ]
    for needle in required:
        assert needle in text
    assert 'eval "' not in text
    assert "bash -c" not in text
    assert "git checkout" not in text


def test_host_wrapper_pins_all_installed_trust_roots() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    pinned = {
        "EXPECTED_VERIFY_SHA256": ROOT / "ops/mcp-deploy/verify_release.py",
        "EXPECTED_SMOKE_SHA256": ROOT / "scripts/smoke_palimpsest_mcp.py",
        "EXPECTED_UNIT_SHA256": ROOT / "ops/systemd/palimpsest-mcp.service",
    }
    for variable, path in pinned.items():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert f'readonly {variable}="{digest}"' in text


def test_workflow_has_separate_verify_gate_and_public_smoke() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "target_sha:" in text
    assert "git merge-base --is-ancestor" in text
    assert "--github-commit-json" in text
    assert "environment: palimpsest-mcp-production" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "PALIMPSEST_MCP_SSH_HOST_KEY" in text
    assert '"root@$DEPLOY_HOST" "deploy $TARGET_SHA"' in text
    assert "trap cleanup_ssh EXIT" in text
    assert "https://api.seiche.info/palimpsest/mcp" in text
    assert "scripts/smoke_palimpsest_mcp.py" in text
    assert '"view": "interconnection"' in (
        ROOT / "scripts/smoke_palimpsest_mcp.py"
    ).read_text(encoding="utf-8")
    assert "mcp-publisher publish" not in text
    assert "id-token: write" not in text
    assert "cancel-in-progress: false" in text
    assert "palimpsest.mcp-deployment-receipt.v1" in text
    assert "ops/mcp-deploy/verify_registry_release.py" in text
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in text
    assert (
        "palimpsest-mcp-deployment-${{ needs.verify.outputs.target_sha }}-run-"
        "${{ github.run_id }}-attempt-${{ github.run_attempt }}" in text
    )
    assert '"forced_command_deploy": "passed"' in text
    assert '"public_smoke": "passed"' in text


def test_systemd_unit_runs_only_the_controlled_loopback_server() -> None:
    text = UNIT.read_text(encoding="utf-8")
    assert "User=palimpsest-mcp" in text
    assert "ExecStart=/usr/bin/python3 -I /opt/palimpsest-mcp/palimpsest_mcp.py" in text
    assert "NoNewPrivileges=true" in text
    assert "ProtectSystem=strict" in text
    assert "CapabilityBoundingSet=" in text
    assert "EnvironmentFile=" not in text


def test_bootstrap_proves_legacy_runtime_restart_identity_and_hardening() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    legacy_sha = "2a80981815680006f3daf7caf503a125d6299c3c"
    assert legacy_sha in text
    assert 'show "${legacy_sha}:server.json"' in text
    assert text.count('--manifest "$legacy_manifest" --basic') == 2
    assert text.index('--manifest "$legacy_manifest" --basic') < text.index(
        "sudo install -o root -g root -m 0644 ops/systemd/palimpsest-mcp.service"
    )
    assert "systemctl enable --now" not in text
    assert "sudo systemctl enable palimpsest-mcp.service" in text
    assert "sudo systemctl restart palimpsest-mcp.service" in text
    assert "--property=User" in text
    assert "--property=MainPID" in text
    assert "--property=ExecMainPID" in text
    assert 'ps -o uid= -p "$main_pid"' in text
    for property_value in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "PrivateUsers=yes",
        "MemoryDenyWriteExecute=yes",
        "CapabilityBoundingSet=",
    ):
        assert property_value in text


def _write_registry_binding_fixture(
    tmp_path: Path,
) -> tuple[str, str, Path, Path, Path]:
    target_sha = "a" * 40
    run_id = "123456"
    manifest_path = tmp_path / "server.json"
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    run_path = tmp_path / "run.json"
    run_path.write_text(
        json.dumps(
            {
                "id": int(run_id),
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_branch": "main",
                "head_sha": target_sha,
                "path": ".github/workflows/deploy-mcp.yml",
                "repository": {"full_name": "beepboop2025/palimpsest"},
                "head_repository": {"full_name": "beepboop2025/palimpsest"},
                "run_attempt": 2,
            }
        ),
        encoding="utf-8",
    )
    receipt_path = tmp_path / "deployment-receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema": "palimpsest.mcp-deployment-receipt.v1",
                "repository": "beepboop2025/palimpsest",
                "workflow": ".github/workflows/deploy-mcp.yml",
                "workflow_run_id": int(run_id),
                "workflow_run_attempt": 2,
                "target_sha": target_sha,
                "server_version": manifest["version"],
                "public_mcp_url": "https://api.seiche.info/palimpsest/mcp",
                "forced_command_deploy": "passed",
                "public_smoke": "passed",
            }
        ),
        encoding="utf-8",
    )
    return target_sha, run_id, manifest_path, run_path, receipt_path


def test_registry_verifier_binds_exact_successful_deploy_receipt(
    tmp_path: Path,
) -> None:
    target_sha, run_id, manifest, run, receipt = _write_registry_binding_fixture(
        tmp_path
    )
    result = registry_verifier.verify_deployment_binding(
        receipt_path=receipt,
        run_path=run,
        manifest_path=manifest,
        target_sha=target_sha,
        deploy_run_id=run_id,
        repository="beepboop2025/palimpsest",
    )
    assert result == {
        "target_sha": target_sha,
        "server_version": "1.9.0",
        "deploy_run_id": int(run_id),
        "deploy_run_attempt": 2,
    }


@pytest.mark.parametrize(
    ("fixture_name", "field", "value", "message"),
    [
        ("run", "head_sha", "b" * 40, "head SHA"),
        ("run", "conclusion", "failure", "did not succeed"),
        ("receipt", "server_version", "1.8.1", "version drifted"),
        ("receipt", "public_smoke", "failed", "public smoke"),
    ],
)
def test_registry_verifier_rejects_unbound_or_failed_deployments(
    tmp_path: Path,
    fixture_name: str,
    field: str,
    value: str,
    message: str,
) -> None:
    target_sha, run_id, manifest, run, receipt = _write_registry_binding_fixture(
        tmp_path
    )
    path = run if fixture_name == "run" else receipt
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(registry_verifier.RegistryReleaseError, match=message):
        registry_verifier.verify_deployment_binding(
            receipt_path=receipt,
            run_path=run,
            manifest_path=manifest,
            target_sha=target_sha,
            deploy_run_id=run_id,
            repository="beepboop2025/palimpsest",
        )


def test_registry_verifier_requires_exact_active_latest_server_card(
    tmp_path: Path,
) -> None:
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    manifest_path = tmp_path / "server.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    registry_path = tmp_path / "registry.json"
    registry_payload = {
        "server": manifest,
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active",
                "isLatest": True,
            }
        },
    }
    registry_path.write_text(json.dumps(registry_payload), encoding="utf-8")
    result = registry_verifier.verify_published_registry(
        registry_path=registry_path,
        manifest_path=manifest_path,
    )
    assert result["version"] == "1.9.0"
    registry_payload["server"]["version"] = "1.8.1"
    registry_path.write_text(json.dumps(registry_payload), encoding="utf-8")
    with pytest.raises(registry_verifier.RegistryReleaseError, match="differs"):
        registry_verifier.verify_published_registry(
            registry_path=registry_path,
            manifest_path=manifest_path,
        )


def test_registry_workflow_is_sha_receipt_and_post_publish_bound() -> None:
    text = REGISTRY_WORKFLOW.read_text(encoding="utf-8")
    for needle in (
        "target_sha:",
        "deploy_run_id:",
        "ref: ${{ inputs.target_sha }}",
        "persist-credentials: false",
        'test "$GITHUB_REF" = refs/heads/main',
        'test "$(git rev-parse refs/remotes/origin/main)" = "$TARGET_SHA"',
        'test -z "$(git status --porcelain)"',
        "actions: read",
        'gh run download "$DEPLOY_RUN_ID"',
        "-run-${DEPLOY_RUN_ID}-attempt-${run_attempt}",
        "verify_registry_release.py deployment",
        "scripts/smoke_palimpsest_mcp.py",
        '"$publisher" validate',
        '"$publisher" publish',
        "verify_registry_release.py published",
        "REGISTRY_LATEST_URL",
        'MCP_PUBLISHER_VERSION: "1.8.1"',
        'MCP_PUBLISHER_SHA256: "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc"',
    ):
        assert needle in text
    assert "id-token: write" in text
    assert "PALIMPSEST_MCP_DEPLOY_KEY" not in text
