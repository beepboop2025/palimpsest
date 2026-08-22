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
WORKFLOW = ROOT / ".github/workflows/deploy-mcp.yml"
UNIT = ROOT / "ops/systemd/palimpsest-mcp.service"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mcp_release_verifier", VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


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
            "author": {
                "name": "Palimpsest Maintainer",
                "email": "mrinallovesbhature@gmail.com",
            },
            "committer": {"name": "GitHub", "email": "noreply@github.com"},
            "verification": {
                "verified": True,
                "reason": "valid",
                "verified_at": "2026-08-22T12:11:43Z",
            },
        },
    }
    path = tmp_path / "commit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    verifier.verify_github_commit(path, target, "mrinallovesbhature@gmail.com")

    payload["commit"]["verification"]["verified"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="valid verified signature"):
        verifier.verify_github_commit(path, target, "mrinallovesbhature@gmail.com")


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
        "https://user:secret@api.seiche.info/palimpsest/mcp",
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
        "env -i PATH=\"$PATH\"",
        'expected_blob=$(git --git-dir="$REPOSITORY" rev-parse',
        "flock -n",
        "mv -fT -- \"$candidate_tmp\" \"$TARGET_FILE\"",
        'systemctl restart "$SERVICE"',
        '"$SMOKE" --url "$LOCAL_ENDPOINT"',
        "rollback",
        "DEPLOYED_SHA_FILE",
    ]
    for needle in required:
        assert needle in text
    assert "eval \"" not in text
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


def test_systemd_unit_runs_only_the_controlled_loopback_server() -> None:
    text = UNIT.read_text(encoding="utf-8")
    assert "User=palimpsest-mcp" in text
    assert "ExecStart=/usr/bin/python3 -I /opt/palimpsest-mcp/palimpsest_mcp.py" in text
    assert "NoNewPrivileges=true" in text
    assert "ProtectSystem=strict" in text
    assert "CapabilityBoundingSet=" in text
    assert "EnvironmentFile=" not in text
