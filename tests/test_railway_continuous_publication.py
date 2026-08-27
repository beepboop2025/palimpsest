from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / ".github/workflows/railway-publication-controller.yml"
TESTS_WORKFLOW = ROOT / ".github/workflows/tests.yml"
TRANSACTION = ROOT / "ops/railway/deploy-continuous-release.sh"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_hourly_controller_is_level_triggered_disabled_and_anti_downgrade() -> None:
    text = CONTROLLER.read_text(encoding="utf-8")
    workflow = _workflow(CONTROLLER)
    dispatch = workflow["jobs"]["dispatch"]

    assert 'cron: "13 * * * *"' in text
    assert dispatch["if"] == "${{ vars.RAILWAY_PUBLICATION_ENABLED == 'true' }}"
    assert workflow["permissions"] == {"contents": "write"}
    assert workflow["concurrency"] == {
        "group": "railway-publication-controller",
        "cancel-in-progress": False,
    }
    assert "railway up" not in text
    assert 'git cat-file -e "${live_sha}^{commit}"' in text
    assert 'git merge-base --is-ancestor "$live_sha" "$main_sha"' in text
    assert 'test "$remote_main" = "$PUBLICATION_SHA"' in text
    assert "deploy_railway: true" in text
    assert 'scope: "complete"' in text
    assert "--argjson controller_run_id" in text
    assert "--argjson controller_run_attempt" in text
    assert (
        "PROVIDER_MANIFEST_URL: "
        "https://palimpsest-publication-production.up.railway.app/railway-release.json"
        in text
    )
    assert (
        "PUBLIC_MANIFEST_URL: https://www.palimpsest.info/railway-release.json" in text
    )
    assert 'fetch_manifest "$PROVIDER_MANIFEST_URL" "$provider_manifest"' in text
    assert 'fetch_manifest "$PUBLIC_MANIFEST_URL" "$public_manifest"' in text
    assert 'cmp --silent "$provider_manifest" "$public_manifest"' in text
    assert "--max-filesize 4194304" in text
    assert "--location" not in text


def test_dispatch_identity_gate_enforces_both_closed_payload_schemas(
    tmp_path: Path,
) -> None:
    workflow = _workflow(TESTS_WORKFLOW)
    identity = next(
        step
        for step in workflow["jobs"]["contract"]["steps"]
        if step.get("name") == "Resolve and validate the publication identity"
    )
    script = identity["run"]
    revision = "a" * 40
    railway_payload = {
        "sha": revision,
        "scope": "complete",
        "deploy_railway": True,
        "controller_run_id": 123,
        "controller_run_attempt": 1,
        "requested_at": "2026-08-27T10:00:00Z",
        "controller_artifact_id": 456,
        "controller_artifact_digest": "b" * 64,
        "controller_request_sha256": "c" * 64,
    }

    cases = (
        ({"sha": revision, "scope": "complete"}, True),
        (railway_payload, True),
        (
            {
                **railway_payload,
                "deploy_railway": "true",
            },
            False,
        ),
        (
            {
                **railway_payload,
                "scope": "source",
            },
            False,
        ),
        (
            {
                **railway_payload,
                "controller_run_id": 0,
            },
            False,
        ),
        ({"sha": revision, "scope": "complete", "extra": "drift"}, False),
    )

    for index, (payload, accepted) in enumerate(cases):
        event_path = tmp_path / f"event-{index}.json"
        output_path = tmp_path / f"output-{index}.txt"
        event_path.write_text(json.dumps({"client_payload": payload}), encoding="utf-8")
        completed = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env={
                "GITHUB_EVENT_NAME": "repository_dispatch",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_OUTPUT": str(output_path),
                "GITHUB_SHA": revision,
                "PUBLICATION_SHA": str(payload.get("sha", "")),
                "PUBLICATION_SCOPE": str(payload.get("scope", "")),
                "GITHUB_REPOSITORY": "palimpsest-info/palimpsest",
            },
        )
        assert (completed.returncode == 0) is accepted, (
            payload,
            completed.stdout,
            completed.stderr,
        )


def test_railway_job_is_protected_serialized_pinned_and_exact() -> None:
    workflow = _workflow(TESTS_WORKFLOW)
    jobs = workflow["jobs"]
    railway = jobs["deploy-and-verify-railway"]

    assert set(railway["needs"]) == {"contract", "pages-artifact", "deploy-pages"}
    for gate in (
        "needs.contract.result == 'success'",
        "needs.pages-artifact.result == 'success'",
        "needs.deploy-pages.result == 'success'",
        "needs.contract.outputs.scope == 'complete'",
        "github.event_name == 'repository_dispatch'",
        "github.event.client_payload.deploy_railway == true",
        "vars.RAILWAY_PUBLICATION_ENABLED == 'true'",
    ):
        assert gate in railway["if"]
    assert railway["concurrency"] == {
        "group": "palimpsest-railway-production",
        "cancel-in-progress": False,
    }
    assert railway["environment"] == {
        "name": "palimpsest-railway-production",
        "url": "https://www.palimpsest.info",
    }
    assert railway["permissions"] == {"contents": "read"}
    assert railway["env"] == {
        "RAILWAY_PROJECT_ID": "f7c86128-53a7-458a-a931-6628c6e61fb2",
        "RAILWAY_ENVIRONMENT_ID": "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2",
        "RAILWAY_SERVICE_ID": "86a6f49c-b9dc-4be8-acd1-dd180c693230",
        "RAILWAY_PROVIDER_ORIGIN": (
            "https://palimpsest-publication-production.up.railway.app"
        ),
        "RAILWAY_PUBLIC_ORIGIN": "https://www.palimpsest.info",
        "RAILWAY_EXCLUSIVE_WRITER_ACK": ("${{ vars.RAILWAY_EXCLUSIVE_WRITER_ACK }}"),
    }

    steps = railway["steps"]
    checkout = steps[0]
    assert checkout["with"] == {
        "ref": "${{ needs.contract.outputs.revision }}",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    credential = next(step for step in steps if step.get("id") == "railway_credential")
    assert credential["env"] == {
        "RAILWAY_TOKEN": "${{ secrets.PALIMPSEST_RAILWAY_TOKEN }}"
    }
    install_cli = next(
        step
        for step in steps
        if step.get("name") == "Install the checksum-pinned Railway CLI"
    )
    assert install_cli["env"]["RAILWAY_CLI_VERSION"] == "5.44.1"
    assert install_cli["env"]["RAILWAY_CLI_SHA256"] == (
        "d3197e0868576e90fc1926795014c398fdfd8a8b006be07eb3e8e5215ee0a3ca"
    )
    assert "sha256sum --check --strict" in install_cli["run"]
    assert "--max-filesize 8388608" in install_cli["run"]
    assert "curl |" not in install_cli["run"]
    transaction = next(
        step for step in steps if step.get("id") == "railway_transaction"
    )
    assert transaction["run"] == "bash ops/railway/deploy-continuous-release.sh"
    assert transaction["env"]["RAILWAY_TOKEN"] == (
        "${{ secrets.PALIMPSEST_RAILWAY_TOKEN }}"
    )
    evidence = next(
        step
        for step in steps
        if step.get("name") == "Upload immutable Railway transaction evidence"
    )
    assert evidence["if"] == (
        "${{ always() && steps.railway_credential.outcome == 'success' }}"
    )
    assert evidence["with"]["retention-days"] == 90
    assert evidence["with"]["compression-level"] == 0
    assert evidence["with"]["if-no-files-found"] == "error"

    top_level_cancel = workflow["concurrency"]["cancel-in-progress"]
    assert "deploy_railway == true" in top_level_cancel
    assert top_level_cancel.startswith("${{ !(")
    final_rights = jobs["verify-live-rights-closure"]
    assert "github.event.client_payload.deploy_railway != true" in final_rights["if"]
    assert "vars.RAILWAY_PUBLICATION_ENABLED != 'true'" in final_rights["if"]


def test_predeployment_mcp_probe_defers_only_new_sha_identity() -> None:
    workflow = _workflow(TESTS_WORKFLOW)
    mcp = workflow["jobs"]["mcp-deployment-admission"]
    smoke = next(
        step
        for step in mcp["steps"]
        if step.get("name") == "Re-probe the deployed public MCP release"
    )
    assert smoke["env"]["DEPLOY_RAILWAY"] == (
        "${{ github.event.client_payload.deploy_railway || false }}"
    )
    assert 'if [ "$DEPLOY_RAILWAY" != true ]; then' in smoke["run"]
    assert (
        'publication_args=(--expected-publication-sha "$PUBLICATION_SHA")'
        in smoke["run"]
    )
    assert "--bootstrap-deny" in smoke["run"]


def test_transaction_orders_authority_gates_and_has_bounded_rollback() -> None:
    text = TRANSACTION.read_text(encoding="utf-8")
    assert (
        subprocess.run(
            ["bash", "-n", str(TRANSACTION)], check=False, capture_output=True
        ).returncode
        == 0
    )

    token_scope = text.index("transaction_phase=token_scope")
    topology = text.index("transaction_phase=topology_preflight")
    bundle = text.index("transaction_phase=bundle")
    mutation = text.index("transaction_phase=mutation_preflight")
    upload = text.index("transaction_phase=upload")
    live_verify = text.index("transaction_phase=served_byte_verification")
    mcp_verify = text.index("transaction_phase=mcp_rights_closure")
    assert (
        token_scope < topology < bundle < mutation < upload < live_verify < mcp_verify
    )

    assert "projectToken { projectId environmentId }" in text
    assert "--max-filesize 2097152" in text
    assert text.count("assert_current_main") == 3  # definition plus two calls
    assert '[[ "$mutation_deployment_id" == "$previous_deployment_id" \\' in text
    assert '&& "$mutation_image_digest" == "$previous_image_digest" \\' in text
    assert (
        'cmp --silent "$previous_provider_manifest" "$mutation_provider_manifest"'
        in text
    )
    assert (
        'cmp --silent "$previous_public_manifest" "$mutation_public_manifest"' in text
    )
    assert text.count("railway up \\") == 1
    assert "for upload_attempt in 1 2" not in text
    assert '--message "$upload_message"' in text
    assert "deployment_record_by_message" in text
    assert "deployment_record_by_id" in text
    assert "FAILED|CRASHED|REMOVED|SKIPPED" in text
    assert "submission_state=submitted_unknown" in text
    assert "submission_state=active" in text
    assert "fail_after_submission submission_unresolved" in text
    assert "verify_continuous_release.py" in text
    assert '--public-base-url "$RAILWAY_PUBLIC_ORIGIN"' in text
    assert '--expected-publication-sha "$PUBLICATION_SHA"' in text
    assert "--bootstrap-deny" not in text
    assert "deploymentRollback(id: $id)" in text
    assert "canRollback" in text
    assert text.count('if [[ "$deployment_mode" == uploaded ]]; then') >= 2
    assert 'rollback_target_deployment_id="$previous_deployment_id"' in text
    assert "rollback_in_progress=false" in text
    assert '[[ "$reconciliation_started" != true ]] || return 1' in text
    assert 'elif [[ "$reconciliation_started" == true ]]; then' in text
    assert "finalize_transaction_receipt()" in text
    assert "transaction_finalizing=true" in text
    assert "rollback_result=refused_unrelated_latest" in text
    assert "rollback_result=refused_candidate_identity_unknown" in text
    assert "rollback_result=refused_candidate_sleeping" in text
    assert 'document.get("errors") not in (None, [])' in text
    assert "verify_rollback_until_deadline rollback" in text
    assert (
        'rollback_reserve_seconds="${PALIMPSEST_RAILWAY_ROLLBACK_RESERVE_SECONDS:-900}"'
        in text
    )
    assert "rollback_required_reserve=$((" in text
    assert "mutation_deadline_epoch" in text
    assert "trap 'on_signal TERM' TERM" in text
    assert "railway down" not in text
    assert "RAILWAY_TOKEN" not in (
        text[text.index("document = {") : text.index("payload = (json.dumps")]
    )


@pytest.mark.parametrize(
    "forbidden",
    ("RAILWAY_API_TOKEN", "HETZNER_SSH_KEY", "DATABASE_URL", "REDIS_URL"),
)
def test_controller_and_transaction_do_not_request_cross_plane_secrets(
    forbidden: str,
) -> None:
    combined = CONTROLLER.read_text(encoding="utf-8") + TRANSACTION.read_text(
        encoding="utf-8"
    )
    assert forbidden not in combined
