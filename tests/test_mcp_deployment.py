"""Offline contract tests for the exact-SHA Palimpsest MCP release path."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
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
GITHUB_SIGNING_KEY = ROOT / "ops/mcp-deploy/github-web-flow-signing-key.asc"
GITHUB_COMMIT_FIXTURE_SHA = "ad52601de621edfd7a9b8fd221fb030a0cfab273"


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


def _github_commit_fixture() -> dict[str, object]:
    """Recreate API evidence from one exact authentic reachable GitHub merge."""
    commit_sha = GITHUB_COMMIT_FIXTURE_SHA
    raw_commit = subprocess.run(
        ["git", "cat-file", "commit", commit_sha],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    raw_headers, message = raw_commit.split("\n\n", 1)
    payload_headers: list[str] = []
    signature_lines: list[str] = []
    reading_signature = False
    for line in raw_headers.splitlines():
        if line.startswith("gpgsig "):
            assert not signature_lines
            reading_signature = True
            signature_lines.append(line.removeprefix("gpgsig "))
        elif reading_signature and line.startswith(" "):
            signature_lines.append(line[1:])
        else:
            reading_signature = False
            payload_headers.append(line)
    while signature_lines and not signature_lines[-1]:
        signature_lines.pop()
    assert signature_lines
    assert signature_lines[0] == "-----BEGIN PGP SIGNATURE-----"
    parents = [
        {"sha": line.removeprefix("parent ")}
        for line in payload_headers
        if line.startswith("parent ")
    ]
    return {
        "sha": commit_sha,
        "author": {"login": "beepboop2025"},
        "committer": {"login": "web-flow"},
        "parents": parents,
        "commit": {
            "verification": {
                "verified": True,
                "reason": "valid",
                "verified_at": "2026-08-24T20:28:24Z",
                "payload": "\n".join(payload_headers)
                + "\n\n"
                + message.removesuffix("\n"),
                "signature": "\n".join(signature_lines) + "\n",
            }
        },
    }


def test_current_candidate_satisfies_release_contract() -> None:
    contract = verifier.verify_candidate(
        ROOT / "mcp/palimpsest_mcp.py",
        ROOT / "server.json",
    )
    assert contract == {
        "version": "1.9.2",
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
        "resources": ["palimpsest://china-economic/publication-rights"],
    }


def test_verifier_rejects_manifest_version_drift(tmp_path: Path) -> None:
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    manifest["version"] = "1.9.3"
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
    gpgv = shutil.which("gpgv")
    assert gpgv is not None, "gpgv is part of the release-controller contract"
    payload = _github_commit_fixture()
    target_sha = payload["sha"]
    assert isinstance(target_sha, str)
    path = tmp_path / "commit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    verifier.verify_github_commit(
        path,
        target_sha,
        GITHUB_SIGNING_KEY,
        gpgv_path=Path(gpgv).resolve(),
    )

    payload["commit"]["verification"]["verified"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="valid verified signature"):
        verifier.verify_github_commit(
            path,
            target_sha,
            GITHUB_SIGNING_KEY,
            gpgv_path=Path(gpgv).resolve(),
        )


def test_verifier_rejects_forged_runner_provenance(tmp_path: Path) -> None:
    forged = {
        "sha": "a" * 40,
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
    path = tmp_path / "forged.json"
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="no signed payload"):
        verifier.verify_github_commit(path, "a" * 40, GITHUB_SIGNING_KEY)


def test_verifier_binds_signed_payload_to_target_sha(tmp_path: Path) -> None:
    payload = _github_commit_fixture()
    target_sha = payload["sha"]
    assert isinstance(target_sha, str)
    signed_payload = payload["commit"]["verification"]["payload"]
    assert isinstance(signed_payload, str)
    payload["commit"]["verification"]["payload"] = signed_payload + "!"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="reconstruct the target SHA"):
        verifier.verify_github_commit(
            path,
            target_sha,
            GITHUB_SIGNING_KEY,
        )


def test_verifier_rejects_unpinned_signed_author(tmp_path: Path) -> None:
    payload = _github_commit_fixture()
    target_sha = payload["sha"]
    assert isinstance(target_sha, str)
    signed_payload = payload["commit"]["verification"]["payload"]
    assert isinstance(signed_payload, str)
    author_line = next(
        line for line in signed_payload.splitlines() if line.startswith("author ")
    )
    timestamp, timezone = author_line.rsplit(" ", 2)[-2:]
    payload["commit"]["verification"]["payload"] = signed_payload.replace(
        author_line,
        f"author Example Maintainer <release@example.com> {timestamp} {timezone}",
        1,
    )
    path = tmp_path / "wrong-author.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="pinned release principal"):
        verifier.verify_github_commit(
            path,
            target_sha,
            GITHUB_SIGNING_KEY,
        )


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


def _verified_rights_payload() -> dict[str, object]:
    return {
        "schema_version": server.ECON_RIGHTS_MCP_SCHEMA,
        "status": "restricted",
        "availability": "unavailable",
        "evidence_class": "restricted",
        "publication_allowed": False,
        "reason": "Reviewed policy denies this value family.",
        "mcp_checked_at": "2026-08-26T00:00:01Z",
        "publication_sha": "2" * 40,
        "rights_evaluated_at": "2026-08-26T00:00:00Z",
        "status_artifact": {
            "url": server.ECON_RIGHTS_STATUS_URL,
            "schema_url": server.ECON_RIGHTS_SCHEMA_URL,
            "integrity": "verified",
            "sha256": "a" * 64,
        },
        "policy": {
            "path": server.ECON_RIGHTS_POLICY_PATH,
            "schema_version": server.ECON_RIGHTS_POLICY_SCHEMA,
            "policy_scope": server.ECON_RIGHTS_POLICY_SCOPE,
            "default_decision": "deny",
            "sha256": server.ECON_RIGHTS_POLICY_SHA256,
            "bytes": server.ECON_RIGHTS_POLICY_BYTES,
            "rechecked_at": "2026-08-26T00:00:01Z",
        },
        "counts": {
            "input_records": server.ECON_RIGHTS_EXPECTED_INPUT_RECORDS,
            "allowed_records": server.ECON_RIGHTS_EXPECTED_ALLOWED_RECORDS,
            "restricted_records": server.ECON_RIGHTS_EXPECTED_RESTRICTED_RECORDS,
            "published_records": 0,
            "quarantined_artifacts": server.ECON_RIGHTS_EXPECTED_QUARANTINED_ARTIFACTS,
        },
        "source_decisions": [
            {
                "source_id": "cfets_benchmarks", "decision": "deny",
                "availability": "restricted", "values_allowed": False,
                "seiche_export_allowed": False, "published_records": 0,
            },
            {
                "source_id": "chinamoney", "decision": "deny",
                "availability": "restricted", "values_allowed": False,
                "seiche_export_allowed": False, "published_records": 0,
            },
            {
                "source_id": "world_bank_wdi", "decision": "allow",
                "availability": "unavailable", "values_allowed": True,
                "seiche_export_allowed": True, "input_records": 0,
                "published_records": 0,
            },
        ],
        "quarantined_paths": sorted(
            server.SIGNALS[name][0].lstrip("/")
            for name in server.ECON_RIGHTS_AFFECTED_SIGNALS
        ),
        "no_partial_rows": True,
        "limitations": list(server._ECON_RIGHTS_LIMITATIONS),
    }


def test_live_smoke_covers_native_rights_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "economic_rights_status", _verified_rights_payload)
    monkeypatch.setattr(
        server, "_fetch", lambda name: (_ for _ in ()).throw(AssertionError(name))
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
        result = smoke.probe(
            url,
            contract,
            timeout=2,
            allow_http_loopback=True,
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()

    assert result["version"] == contract["version"]
    assert result["tool_count"] == 6
    assert result["prompt_count"] == 4
    assert result["resource_count"] == 1
    assert result["calls"] == [
        "resources/read:china-economic-publication-rights",
        "list_signals",
        "query_economic_observations:rights-status",
        *[
            f"get_signal:{name}:restricted"
            for name in sorted(server.ECON_RIGHTS_AFFECTED_SIGNALS)
        ],
        *[
            f"get_newsroom:{view}:restricted"
            for view in sorted(server.ECON_RIGHTS_AFFECTED_NEWSROOM_VIEWS)
        ],
        "whats_happening:rights-restricted",
    ]


def test_live_smoke_accepts_only_monotonic_denied_coverage_growth() -> None:
    contract = smoke.load_contract(
        ROOT / "mcp/palimpsest_mcp.py",
        ROOT / "server.json",
    )
    payload = _verified_rights_payload()
    payload["counts"] = {
        **payload["counts"],
        "input_records": server.ECON_RIGHTS_EXPECTED_INPUT_RECORDS + 7,
        "restricted_records": server.ECON_RIGHTS_EXPECTED_RESTRICTED_RECORDS + 7,
        "quarantined_artifacts": 24_541,
    }
    payload["quarantined_paths"] = sorted({
        *payload["quarantined_paths"],
        *(
            f"news/wire/repository-scale-{index:05d}.json"
            for index in range(24_541)
        ),
    })
    payload["counts"]["quarantined_artifacts"] = len(
        payload["quarantined_paths"]
    )

    smoke._validate_rights_payload(payload, contract, require_verified=True)

    payload["counts"]["allowed_records"] = 1
    with pytest.raises(smoke.SmokeError, match="reviewed release"):
        smoke._validate_rights_payload(payload, contract, require_verified=True)


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


def test_live_smoke_requires_explicit_loopback_permission() -> None:
    with pytest.raises(smoke.SmokeError, match="explicit loopback"):
        smoke.post_json(
            "http://127.0.0.1:8793/",
            {"jsonrpc": "2.0"},
            timeout=1,
        )


def test_live_smoke_rejects_private_https_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smoke.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                smoke.socket.AF_INET,
                smoke.socket.SOCK_STREAM,
                smoke.socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ],
    )
    with pytest.raises(smoke.SmokeError, match="non-public"):
        smoke.post_json(
            "https://api.seiche.info/palimpsest/mcp",
            {"jsonrpc": "2.0"},
            timeout=1,
        )


def test_live_smoke_rejects_nonfinite_or_oversized_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smoke,
        "_post_public_https",
        lambda *_args, **_kwargs: pytest.fail("invalid request reached transport"),
    )
    with pytest.raises(smoke.SmokeError, match="strict JSON"):
        smoke.post_json("https://api.seiche.info/", {"value": float("nan")}, 1)
    with pytest.raises(smoke.SmokeError, match="request exceeds"):
        smoke.post_json(
            "https://api.seiche.info/",
            {"value": "x" * smoke.MAX_REQUEST_BYTES},
            1,
        )


def test_host_wrapper_is_syntax_valid_and_fail_closed() -> None:
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)
    text = WRAPPER.read_text(encoding="utf-8")
    required = [
        '[[ ! "$original_command" =~ ^deploy\\ ([0-9a-f]{40})$ ]]',
        '[[ "$current_main" = "$target_sha" ]]',
        "target is not the exact origin/main tip",
        "fetch.fsckObjects true",
        "github_signature",
        "--github-commit-json",
        "--github-signing-key",
        'readonly GPGV="/usr/bin/gpgv"',
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
        '"previous_runtime_sha256": previous_runtime_digest',
        '"previous_runtime_backup": previous_runtime_backup',
        '"previous_runtime_source_sha": previous_runtime_source or None',
        'readonly LEGACY_SOURCE_SHA="2a80981815680006f3daf7caf503a125d6299c3c"',
        'readonly EXPECTED_LEGACY_RUNTIME_SHA256="47d419e81ff048771acab14895a9b1e27868d7bbe14874e5cd8c1c94acfc4ed4"',
        "markerless runtime is not the pinned bootstrap legacy release",
        '"schema_version": 2',
        "target already has an immutable receipt",
        'readonly INCIDENT_STATE_FILE="${STATE_DIR}/incident-degraded.json"',
        'restore_state_file "$previous_incident_state" "$INCIDENT_STATE_FILE"',
        'rm -f -- "$INCIDENT_STATE_FILE"',
        "incident state does not bind the live degraded runtime",
    ]
    for needle in required:
        assert needle in text
    assert text.count('[[ "$current_main" = "$target_sha" ]]') == 2
    assert 'eval "' not in text
    assert "bash -c" not in text
    assert "git checkout" not in text
    assert "merge-base --is-ancestor" not in text


def test_host_wrapper_pins_all_installed_trust_roots() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    pinned = {
        "EXPECTED_VERIFY_SHA256": ROOT / "ops/mcp-deploy/verify_release.py",
        "EXPECTED_GITHUB_SIGNING_KEY_SHA256": GITHUB_SIGNING_KEY,
        "EXPECTED_SMOKE_SHA256": ROOT / "scripts/smoke_palimpsest_mcp.py",
        "EXPECTED_UNIT_SHA256": ROOT / "ops/systemd/palimpsest-mcp.service",
    }
    for variable, path in pinned.items():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert f'readonly {variable}="{digest}"' in text


def test_host_wrapper_accepts_only_bounded_runner_provenance() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert "api.github.com" not in text
    assert "Authorization:" not in text
    assert "GH_TOKEN" not in text
    assert "GITHUB_TOKEN" not in text
    assert "GITHUB_PROVENANCE_MAX_BYTES=262144" in text
    assert 'head --bytes="$((GITHUB_PROVENANCE_MAX_BYTES + 1))"' in text
    assert "could not read authenticated GitHub provenance" in text
    assert "authenticated GitHub provenance is empty" in text
    assert "authenticated GitHub provenance exceeds the 256 KiB cap" in text
    assert 'receive_github_provenance "$api_json"' in text
    assert text.index('receive_github_provenance "$api_json"') < text.index(
        'run_as_verify_user "$VERIFY_RELEASE"'
    )


def test_host_wrapper_bounded_provenance_reader(tmp_path: Path) -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    start = text.index("receive_github_provenance() {")
    function_source = text[start : text.index("\n}\n", start) + 3]
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    timeout_shim = shim_dir / "timeout"
    timeout_shim.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'test "$1" = --kill-after=5s\n'
        "shift\n"
        'test "$1" = 20s\n'
        "shift\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    timeout_shim.chmod(0o755)
    script = textwrap.dedent(
        f"""
        set -Eeuo pipefail
        PATH="$1:$PATH"
        shift
        readonly GITHUB_PROVENANCE_MAX_BYTES=262144
        fail() {{ printf '%s\n' "$*" >&2; exit 1; }}
        {function_source}
        receive_github_provenance "$1"
        """
    )

    valid_output = tmp_path / "valid.json"
    valid = subprocess.run(
        ["bash", "-c", script, "bounded-reader", str(shim_dir), str(valid_output)],
        input=b'{"sha":"fixture"}\n',
        capture_output=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr.decode()
    assert valid_output.read_bytes() == b'{"sha":"fixture"}\n'
    assert valid_output.stat().st_mode & 0o777 == 0o444

    for label, payload, error in (
        ("empty", b"", "authenticated GitHub provenance is empty"),
        (
            "oversize",
            b"x" * 262145,
            "authenticated GitHub provenance exceeds the 256 KiB cap",
        ),
    ):
        result = subprocess.run(
            [
                "bash",
                "-c",
                script,
                "bounded-reader",
                str(shim_dir),
                str(tmp_path / f"{label}.json"),
            ],
            input=payload,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0
        assert error in result.stderr.decode()


def test_workflow_has_separate_verify_gate_and_public_smoke() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "target_sha:" in text
    assert "git merge-base --is-ancestor" not in text
    assert 'test "$GITHUB_REF" = refs/heads/main' in text
    assert (
        text.count('test "$(git rev-parse refs/remotes/origin/main)" = "$TARGET_SHA"')
        == 2
    )
    assert "--github-commit-json" in text
    assert "commits/${TARGET_SHA}?per_page=1" in text
    assert text.count("persist-credentials: false") == 2
    assert "--github-signing-key ops/mcp-deploy/github-web-flow-signing-key.asc" in text
    assert text.count("GH_TOKEN: ${{ github.token }}") == 2
    assert text.count('--header "Authorization: Bearer $GH_TOKEN"') == 2
    provenance_step_start = text.index("- name: Fetch authenticated commit provenance")
    configure_step_start = text.index("- name: Configure pinned SSH identity")
    deploy_step_start = text.index("- name: Deploy exact SHA through forced command")
    assert provenance_step_start < configure_step_start < deploy_step_start
    provenance_step = text[provenance_step_start:configure_step_start]
    deploy_step = text[deploy_step_start:]
    assert "GH_TOKEN: ${{ github.token }}" not in deploy_step
    assert '--header "Authorization: Bearer $GH_TOKEN"' not in deploy_step
    assert "env -u GH_TOKEN -u GITHUB_TOKEN" in deploy_step
    assert '"deploy $TARGET_SHA" <"$github_json"' in deploy_step
    assert 'test -s "$github_json"' in provenance_step
    assert "palimpsest-mcp-provenance/github-commit.json" in deploy_step
    assert "environment: palimpsest-mcp-production" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "PALIMPSEST_MCP_SSH_HOST_KEY" in text
    assert '"root@$DEPLOY_HOST" "deploy $TARGET_SHA"' in text
    assert "trap cleanup_ssh EXIT" in text
    assert "https://api.seiche.info/palimpsest/mcp" in text
    assert "scripts/smoke_palimpsest_mcp.py" in text
    assert "--rights-bootstrap-preflight" in text
    assert "--bootstrap-deny" in text
    assert "--expected-publication-sha" in text
    smoke_text = (ROOT / "scripts/smoke_palimpsest_mcp.py").read_text(
        encoding="utf-8"
    )
    assert "_EXPECTED_AFFECTED_SIGNALS" in smoke_text
    assert "_EXPECTED_AFFECTED_VIEWS" in smoke_text
    assert "resources/read" in smoke_text
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
    assert "MemoryHigh=384M" in text
    assert "MemoryMax=512M" in text
    assert "TasksMax=64" in text
    assert "LimitNOFILE=1024" in text


def test_bootstrap_proves_legacy_runtime_restart_identity_and_hardening() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    legacy_sha = "2a80981815680006f3daf7caf503a125d6299c3c"
    assert legacy_sha in text
    assert 'show "${legacy_sha}:server.json"' in text
    assert text.count('--manifest "$legacy_manifest" --basic') == 3
    assert text.index('--manifest "$legacy_manifest" --basic') < text.index(
        "sudo install -o root -g root -m 0644 ops/systemd/palimpsest-mcp.service"
    )
    assert "bootstrap-backups/pre-controller.XXXXXX" in text
    assert "expected_legacy_runtime_sha256" in text
    assert "expected_legacy_unit_sha256" in text
    assert 'sudo tee "$bootstrap_backup/SHA256SUMS"' in text
    assert "finish_bootstrap()" in text
    assert "mutation_started=1" in text
    assert "bootstrap_committed=1" in text
    assert "restoring the captured legacy runtime and unit" in text
    assert "Do not reuse a workstation" in text
    assert "`ssh-keyscan`" in text
    assert "alone is not identity verification" in text
    assert text.count('"$bootstrap_backup/palimpsest_mcp.py"') >= 2
    assert text.count('"$bootstrap_backup/palimpsest-mcp.service"') >= 2
    assert "systemctl enable --now" not in text
    assert "sudo systemctl enable palimpsest-mcp.service" in text
    assert "sudo systemctl restart palimpsest-mcp.service" in text
    assert "--property=User" in text
    assert "--property=MainPID" in text
    assert "--property=ExecMainPID" in text
    assert 'ps -o uid= -p "$main_pid"' in text
    assert "legacy_enablement=$(sudo systemctl is-enabled" in text
    assert 'sudo tee "$bootstrap_backup/SERVICE_ENABLEMENT"' in text
    assert "sudo systemctl disable palimpsest-mcp.service" in text
    assert '"$legacy_enablement" || rollback_failed=1' in text
    assert "bootstrap rollback did not restore every captured invariant" in text
    assert "rm -f --" in text
    assert '"$deploy_key_dir/palimpsest-mcp-deploy"' in text
    assert '"$deploy_key_dir/palimpsest-mcp-deploy.pub"' in text
    assert "ops/mcp-deploy/github-web-flow-signing-key.asc" in text
    assert "968479A1AFF927E37D1A566BB5690EEEBB952194" in text
    assert "unset deploy_key_dir" in text
    assert "trap cleanup_deploy_key EXIT HUP INT TERM" in text
    assert "trap - EXIT HUP INT TERM" in text
    for property_value in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "PrivateUsers=yes",
        "MemoryDenyWriteExecute=yes",
        "CapabilityBoundingSet=",
    ):
        assert property_value in text


def test_controller_upgrade_precedes_forced_deployment() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    section = text.index("## Updating the installed controller trust bundle")
    release = text.index("## Release transaction", section)
    upgrade = text[section:release]
    normalized = " ".join(upgrade.split())
    assert "separate root-admin transaction" in upgrade
    assert "/var/lib/palimpsest-mcp-deploy/deploy.lock" in upgrade
    assert "smoke_palimpsest_mcp.py" in upgrade
    assert "palimpsest-mcp.service" in upgrade
    assert "preserve the installed wrapper, verifier, smoke client" in upgrade
    assert "record that it was absent" in normalized
    for marker in (
        "git rev-parse",
        "git hash-object",
        "bash -n",
        "systemd-analyze verify",
        "pinned verifier, signing-key, smoke-client, and systemd-unit",
        "root ownership, single-link regular-file type, exact modes, and digests",
        "NeedDaemonReload=no",
        "current runtime digest, and deployed marker",
        "without changing `/opt/palimpsest-mcp/palimpsest_mcp.py`",
        "exact `FragmentPath`",
        "has no drop-ins",
        "expected process user/group",
        "runtime digest and deployed marker did not change",
        "restore its prior active state",
        "drop-in absence, process identity, hardening, runtime digest, and deployed marker",
    ):
        assert marker in normalized
    signing_key = upgrade.index("signing key `0444`")
    verifier = upgrade.index("verifier `0755`", signing_key)
    smoke = upgrade.index("smoke client `0755`", verifier)
    unit = upgrade.index("systemd unit `0644`", smoke)
    reload = upgrade.index("systemctl daemon-reload", unit)
    restart = upgrade.index("restart the currently deployed MCP runtime", reload)
    basic_smoke = upgrade.index("with `--basic`", restart)
    wrapper = upgrade.index("wrapper\n`0755` last", basic_smoke)
    assert (
        signing_key < verifier < smoke < unit < reload < restart < basic_smoke < wrapper
    )
    rollback = upgrade.index("restore every captured preimage", wrapper)
    remove_key = upgrade.index("remove a newly introduced key", rollback)
    rollback_reload = upgrade.index("systemctl daemon-reload", remove_key)
    rollback_restart = upgrade.index("restart the prior runtime", rollback_reload)
    rollback_smoke = upgrade.index("re-run the prior basic smoke", rollback_restart)
    assert wrapper < rollback < remove_key < rollback_reload
    assert rollback_reload < rollback_restart < rollback_smoke
    assert "still holding the lock" in upgrade[rollback:rollback_reload]
    assert "Preserve the backup and transaction evidence" in normalized
    assert "report a rollback failure explicitly" in normalized
    assert "Only after this transaction succeeds" in normalized


def test_release_runbook_freezes_writers_through_exact_pages_publish() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    freeze = text.index('gh workflow disable "$workflow_id"')
    deploy = text.index("gh workflow run deploy-mcp.yml")
    publish = text.index("gh workflow run registry-publish.yml")
    complete_dispatch = text.index("-f event_type=publication_contract")
    served_bytes = text.index("pages-served")
    restore = text.index('gh workflow enable "$workflow_id"')
    assert freeze < deploy < publish < complete_dispatch < served_bytes < restore
    assert "scheduled-workflows.tsv" in text
    assert "reuse that gate's" in text
    assert "original preservation manifest" in text
    assert 'build_schedule_manifest "$premerge_schedule_paths"' in text
    assert '"$schedule_manifest" "$premerge_schedule_count"' in text
    assert "expected_state workflow_file" in text
    assert 'status == "queued" or .status == "in_progress"' in text
    assert 'test "$(git rev-parse origin/main)" = "$frozen_main"' in text
    assert 'gh workflow run deploy-mcp.yml --repo "$repo" --ref main' in text
    assert (
        '--repo "$repo" --ref main'
        in text[text.index("gh workflow run registry-publish.yml") :]
    )
    assert "a new frozen tip and a new signed merge" in text
    assert "snapshot_workflow_runs()" in text
    assert "wait_for_one_new_run()" in text
    assert "deploy-runs-before.txt" in text
    assert "registry-runs-before.txt" in text
    assert "ambiguous new %s runs" in text
    assert "actions/runs/$deploy_run_id" in text
    assert "actions/runs/$registry_run_id" in text
    assert "tests-repository-dispatch-before.txt" in text
    assert "client_payload[scope]" in text
    assert "actions/runs/$tests_run_id" in text
    assert "Admit exact deployed MCP release before Pages" in text
    assert "Deploy exact complete Pages edition" in text
    assert "readings/audit/readings-ledger-recovery-20260824.json" in text
    assert 'test "$matched" = 1' in text
    refresh_main = (
        "git fetch --force --no-tags origin \\\n"
        "  '+refs/heads/main:refs/remotes/origin/main'"
    )
    assert text.count(refresh_main) >= 3
    final_refresh = text.rindex(refresh_main, 0, restore)
    final_tip_check = text.index(
        'test "$(git rev-parse origin/main)" = "$target_sha"', final_refresh
    )
    assert served_bytes < final_refresh < final_tip_check < restore
    assert '.path | split(\\"@\\")[0]' in text
    assert "## Rollback after a completed release" in text
    assert "never edit `deployed-sha`" in text
    assert "monotonically higher server version" in text


def test_release_runbook_pins_china_schedule_transition() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    workflows = ROOT / ".github/workflows"
    scheduled_paths = sorted(
        path.relative_to(ROOT).as_posix()
        for path in workflows.iterdir()
        if path.suffix in {".yml", ".yaml"}
        if "\n  schedule:" in path.read_text(encoding="utf-8")
    )

    assert len(scheduled_paths) == 34
    assert ".github/workflows/china-econ-refresh.yml" not in scheduled_paths
    assert ".github/workflows/codeql.yml" in scheduled_paths
    assert 'premerge_schedule_paths="$release_gate_dir/' in text
    assert 'postmerge_schedule_paths="$release_gate_dir/' in text
    assert 'scheduled_paths_at "$frozen_main"' in text
    assert 'validate_schedule_transition "$frozen_main" "$target_sha"' in text
    assert 'build_schedule_manifest "$premerge_schedule_paths"' in text
    assert "35:34" in text
    assert "34:34" in text
    assert "LC_ALL=C comm -23" in text
    assert "LC_ALL=C comm -13" in text
    assert ".github/workflows/*.yml|.github/workflows/*.yaml" in text
    assert ".github/workflows/china-econ-refresh.yml" in text
    assert ".github/workflows/osint-china-refresh.yml" in text
    assert ".github/workflows/osint-china-v2-refresh.yml" in text
    assert "original 35 intentions" in text
    assert "exposes its reviewed" in text
    assert "manual dispatch" in text
    assert "cannot recreate the" in text
    assert "removed schedule" in text
    assert "34-to-34" in text
    assert 'workflow_replacements="$release_gate_dir/' in text
    assert "replacement_live_runs=$(gh api --paginate" in text
    assert 'test -z "$replacement_live_runs"' in text
    assert 'test "$expected_state" = disabled_manually' in text
    assert 'test "$resolved_id" != "$captured_id"' in text


def test_release_runbook_executes_exact_tree_schedule_transitions(
    tmp_path: Path,
) -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    marker = "# BEGIN exact-tree scheduled path extractor"
    start = text.index(marker) + len(marker)
    end = text.index("# END exact-tree scheduled path extractor", start)
    function_source = text[start:end].strip()
    repository = tmp_path / "repository"
    workflows = repository / ".github/workflows"
    workflows.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)

    for number in range(32):
        (workflows / f"writer-{number:02d}.yml").write_text(
            f"name: writer-{number:02d}\non:\n  schedule:\n    - cron: '0 0 * * *'\n",
            encoding="utf-8",
        )
    yaml_writer = workflows / "writer-yaml.yaml"
    yaml_writer.write_text(
        "name: yaml writer\non:\n  schedule:\n    - cron: '1 0 * * *'\n",
        encoding="utf-8",
    )
    china = workflows / "china-econ-refresh.yml"
    china.write_text(
        "name: China econ\non:\n"
        "  schedule:\n    - cron: '41 */6 * * *'\n"
        "  workflow_dispatch: {}\n",
        encoding="utf-8",
    )
    old_osint = workflows / "osint-china-refresh.yml"
    old_osint.write_text(
        "name: OSINT China\non:\n  schedule:\n    - cron: '7 * * * *'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".github/workflows"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Palimpsest Test",
            "-c",
            "user.email=test@palimpsest.info",
            "commit",
            "-qm",
            "premerge tree",
        ],
        cwd=repository,
        check=True,
    )
    premerge_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()

    china.write_text(
        "name: China econ\non:\n  workflow_dispatch: {}\n",
        encoding="utf-8",
    )
    old_osint.unlink()
    (workflows / "osint-china-v2-refresh.yml").write_text(
        "name: OSINT China v2\non:\n  schedule:\n    - cron: '7 * * * *'\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "-A", ".github/workflows"], cwd=repository, check=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Palimpsest Test",
            "-c",
            "user.email=test@palimpsest.info",
            "commit",
            "-qm",
            "one-time transition",
        ],
        cwd=repository,
        check=True,
    )
    transition_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()

    (repository / "README.md").write_text("steady state\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Palimpsest Test",
            "-c",
            "user.email=test@palimpsest.info",
            "commit",
            "-qm",
            "steady state",
        ],
        cwd=repository,
        check=True,
    )
    steady_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()

    def validate_transition(
        premerge: str,
        target: str,
        stem: str,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        premerge_paths = tmp_path / f"{stem}-pre.txt"
        postmerge_paths = tmp_path / f"{stem}-post.txt"
        replacements = tmp_path / f"{stem}-replacements.tsv"
        return subprocess.run(
            [
                "bash",
                "-c",
                (
                    f"set -euo pipefail\n{function_source}\n"
                    'scheduled_paths_at "$1" >"$3"\n'
                    'validate_schedule_transition "$1" "$2" "$3" "$4" "$5"'
                ),
                "scheduled-path-test",
                premerge,
                target,
                str(premerge_paths),
                str(postmerge_paths),
                str(replacements),
            ],
            cwd=repository,
            check=check,
            capture_output=True,
            text=True,
        )

    assert (
        validate_transition(premerge_tree, transition_tree, "one-time").returncode == 0
    )
    assert validate_transition(transition_tree, steady_tree, "steady").returncode == 0
    premerge_paths = (
        (tmp_path / "one-time-pre.txt").read_text(encoding="utf-8").splitlines()
    )
    postmerge_paths = (
        (tmp_path / "one-time-post.txt").read_text(encoding="utf-8").splitlines()
    )
    replacements = (tmp_path / "one-time-replacements.tsv").read_text(encoding="utf-8")
    assert len(premerge_paths) == 35
    assert len(postmerge_paths) == 34
    assert ".github/workflows/writer-yaml.yaml" in premerge_paths
    assert all(":" not in path for path in premerge_paths + postmerge_paths)
    assert replacements == (
        ".github/workflows/osint-china-refresh.yml\t"
        ".github/workflows/osint-china-v2-refresh.yml\n"
    )

    (workflows / "writer-00.yml").write_text(
        "name: writer-00\non:\n  workflow_dispatch: {}\n",
        encoding="utf-8",
    )
    (workflows / "replacement.yml").write_text(
        "name: replacement\non:\n  schedule:\n    - cron: '5 0 * * *'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".github/workflows"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Palimpsest Test",
            "-c",
            "user.email=test@palimpsest.info",
            "commit",
            "-qm",
            "invalid set substitution",
        ],
        cwd=repository,
        check=True,
    )
    changed_set_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    assert (
        validate_transition(
            steady_tree,
            changed_set_tree,
            "changed-set",
            check=False,
        ).returncode
        != 0
    )


def test_release_runbook_manifest_join_is_an_exact_bijection(
    tmp_path: Path,
) -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    marker = "# BEGIN exact workflow manifest join"
    start = text.index(marker) + len(marker)
    end = text.index("# END exact workflow manifest join", start)
    function_source = text[start:end].strip()
    paths = [f".github/workflows/writer-{number:02d}.yml" for number in range(34)]
    paths_file = tmp_path / "paths.txt"
    inventory_file = tmp_path / "inventory.json"
    manifest_file = tmp_path / "manifest.tsv"
    paths_file.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")

    def build_manifest(
        inventory: list[dict[str, object]],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        inventory_file.write_text(json.dumps(inventory), encoding="utf-8")
        return subprocess.run(
            [
                "bash",
                "-c",
                (
                    f"set -euo pipefail\n{function_source}\n"
                    'build_schedule_manifest "$1" "$2" "$3" 34'
                ),
                "manifest-join-test",
                str(paths_file),
                str(inventory_file),
                str(manifest_file),
            ],
            check=check,
            capture_output=True,
            text=True,
        )

    inventory = [
        {"id": number + 1, "state": "active", "path": path}
        for number, path in enumerate(paths)
    ]
    assert build_manifest(inventory).returncode == 0
    rows = [row.split("\t") for row in manifest_file.read_text().splitlines()]
    assert len(rows) == 34
    assert sorted(row[2] for row in rows) == paths
    assert len({row[0] for row in rows}) == 34

    duplicate_path = [*inventory, {"id": 99, "state": "active", "path": paths[0]}]
    assert build_manifest(duplicate_path, check=False).returncode != 0
    assert build_manifest(inventory[:-1], check=False).returncode != 0
    duplicate_id = [dict(row) for row in inventory]
    duplicate_id[-1]["id"] = duplicate_id[0]["id"]
    assert build_manifest(duplicate_id, check=False).returncode != 0


def test_emergency_rollback_is_receipt_bound_atomic_and_syntax_valid() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    heading = text.index("## Rollback after a completed release")
    start = text.index("```bash\n", heading) + len("```bash\n")
    end = text.index("\n```", start)
    source = text[start:end]
    subprocess.run(["bash", "-n", "-c", source], check=True)
    for needle in (
        'receipt.get("schema_version") != 2',
        'receipt.get("previous_runtime_sha256")',
        'receipt.get("previous_runtime_backup")',
        'receipt.get("previous_runtime_source_sha")',
        'test "$source_previous_digest" = "$previous_digest"',
        'test "$(sudo sha256sum "$backup_file"',
        'sudo mv -fT "$restore_tmp" "$target_file"',
        "finish_emergency_restore()",
        '"$incident_dir/released-runtime.py"',
        "palimpsest.mcp-emergency-rollback-receipt.v1",
        '"state": "incident-degraded"',
        "sudo grep -Eq '^[0-9a-f]{40}$' \"$marker_file\"",
        'released_receipt_digest=$(sudo sha256sum "$release_receipt"',
        'readonly incident_state_file="$state_dir/incident-degraded.json"',
        'sudo mv -fT "$incident_state_tmp" "$incident_state_file"',
        'sudo rm -f -- "$incident_state_file"',
        "incident_state_promoted=1",
        "/usr/bin/flock -n /var/lib/palimpsest-mcp-deploy/deploy.lock",
        "/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin",
        "PALIMPSEST_EMERGENCY_ROLLBACK",
    ):
        assert needle in source


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
    expected_version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    assert result == {
        "target_sha": target_sha,
        "server_version": expected_version,
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
    assert result["version"] == manifest["version"]
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
        "--max-filesize 1048576",
        'MCP_PUBLISHER_VERSION: "1.8.1"',
        'MCP_PUBLISHER_SHA256: "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc"',
        "REGISTRY_VERSIONS_URL",
        "?include_deleted=true",
        "Preflight the exact immutable Registry version",
        "already_published=true",
        "publication_mode=recovered-existing",
        "immutable Registry version exists with different server content",
        "palimpsest.mcp-registry-publication-receipt.v2",
        '"publication_mode"',
        '"registry_response_sha256"',
        "registry-latest.json",
        "registry-receipt.json",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "palimpsest-mcp-registry-${{ inputs.target_sha }}-run-",
        "retention-days: 90",
        "compression-level: 0",
    ):
        assert needle in text
    assert "id-token: write" in text
    assert "PALIMPSEST_MCP_DEPLOY_KEY" not in text
    assert (
        text.count('test "$(git rev-parse refs/remotes/origin/main)" = "$TARGET_SHA"')
        == 2
    )
    assert (
        text.count("if: steps.registry_preflight.outputs.already_published != 'true'")
        == 2
    )


def test_registry_preflight_recovers_only_the_exact_active_version(
    tmp_path: Path,
) -> None:
    workflow = REGISTRY_WORKFLOW.read_text(encoding="utf-8")
    marker = "python3 - \"$version_json\" <<'PY'\n"
    start = workflow.index(marker) + len(marker)
    end = workflow.index("\n          PY", start)
    source = textwrap.dedent(workflow[start:end])
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    (tmp_path / "server.json").write_text(json.dumps(manifest), encoding="utf-8")
    registry = {
        "server": manifest,
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active",
                "isLatest": True,
                "publishedAt": "2026-08-24T07:00:00Z",
            }
        },
    }
    registry_path = tmp_path / "registry-version.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    subprocess.run(
        [sys.executable, "-c", source, str(registry_path)],
        cwd=tmp_path,
        check=True,
    )

    # A prior publish may be visible at its exact immutable endpoint before the
    # eventually consistent `latest` endpoint converges. The final poll below
    # remains responsible for requiring isLatest=true.
    registry["_meta"]["io.modelcontextprotocol.registry/official"]["isLatest"] = False
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    subprocess.run(
        [sys.executable, "-c", source, str(registry_path)],
        cwd=tmp_path,
        check=True,
    )

    registry["server"] = {**manifest, "description": "equivocated immutable card"}
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            [sys.executable, "-c", source, str(registry_path)],
            cwd=tmp_path,
            check=True,
        )


def test_registry_receipt_script_binds_exact_verified_response(tmp_path: Path) -> None:
    workflow = REGISTRY_WORKFLOW.read_text(encoding="utf-8")
    marker = (
        'python3 - "$registry_json" "$receipt_dir/registry-receipt.json" <<\'PY\'\n'
    )
    start = workflow.index(marker) + len(marker)
    end = workflow.index("\n          PY", start)
    source = textwrap.dedent(workflow[start:end])

    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    (tmp_path / "server.json").write_text(json.dumps(manifest), encoding="utf-8")
    registry = {
        "server": manifest,
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active",
                "isLatest": True,
                "publishedAt": "2026-08-24T07:00:00Z",
            }
        },
    }
    registry_path = tmp_path / "registry-latest.json"
    registry_raw = json.dumps(registry, separators=(",", ":")).encode()
    registry_path.write_bytes(registry_raw)
    receipt_path = tmp_path / "registry-receipt.json"
    target_sha = "a" * 40
    subprocess.run(
        [sys.executable, "-c", source, str(registry_path), str(receipt_path)],
        cwd=tmp_path,
        env={
            **os.environ,
            "GITHUB_REPOSITORY": "beepboop2025/palimpsest",
            "GITHUB_RUN_ID": "987654",
            "GITHUB_RUN_ATTEMPT": "2",
            "TARGET_SHA": target_sha,
            "DEPLOY_RUN_ID": "123456",
            "REGISTRY_LATEST_URL": (
                "https://registry.modelcontextprotocol.io/v0.1/servers/"
                "io.github.beepboop2025%2Fpalimpsest/versions/latest"
            ),
            "PUBLICATION_MODE": "recovered-existing",
        },
        check=True,
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt == {
        "schema": "palimpsest.mcp-registry-publication-receipt.v2",
        "repository": "beepboop2025/palimpsest",
        "workflow": ".github/workflows/registry-publish.yml",
        "workflow_run_id": 987654,
        "workflow_run_attempt": 2,
        "target_sha": target_sha,
        "server_name": "io.github.beepboop2025/palimpsest",
        "server_version": manifest["version"],
        "deploy_run_id": 123456,
        "publication_mode": "recovered-existing",
        "registry_latest_url": (
            "https://registry.modelcontextprotocol.io/v0.1/servers/"
            "io.github.beepboop2025%2Fpalimpsest/versions/latest"
        ),
        "registry_response_sha256": hashlib.sha256(registry_raw).hexdigest(),
        "official_status": "active",
        "official_is_latest": True,
        "published_at": "2026-08-24T07:00:00Z",
    }

    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            [sys.executable, "-c", source, str(registry_path), str(receipt_path)],
            cwd=tmp_path,
            env={
                **os.environ,
                "GITHUB_REPOSITORY": "beepboop2025/palimpsest",
                "GITHUB_RUN_ID": "987654",
                "GITHUB_RUN_ATTEMPT": "2",
                "TARGET_SHA": target_sha,
                "DEPLOY_RUN_ID": "123456",
                "REGISTRY_LATEST_URL": (
                    "https://registry.modelcontextprotocol.io/v0.1/servers/"
                    "io.github.beepboop2025%2Fpalimpsest/versions/latest"
                ),
                "PUBLICATION_MODE": "unreviewed-mode",
            },
            check=True,
        )
