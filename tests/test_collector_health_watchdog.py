"""The recovery watchdog retries dead pipelines without manufacturing live data."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets

from jsonschema import Draft202012Validator, ValidationError
import pytest
import yaml

from scripts import collector_health_watchdog as watchdog


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "collector-health-watchdog.yml"
RECEIPT_SCHEMA = ROOT / "protocol" / "collector-health-watchdog-receipt-v1.schema.json"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
STRICT_NEWSWIRE_VALIDATOR = watchdog._validate_newswire_contract
STRICT_SITUATION_VALIDATOR = watchdog._validate_situation_contract


def _canonical_bytes(value: dict) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _receipt_schema() -> dict:
    return json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))


def _zero_action_receipt() -> dict:
    sha = "a" * 40
    plan = {
        "bundle_generated_at": "2026-08-13T11:30:00Z",
        "bundle_stale": False,
        "dispatch": [],
        "escalations": [],
        "generated_at": "2026-08-13T12:00:00Z",
        "problems": [],
        "schema_version": "collector-watchdog-plan.v2",
    }
    return {
        "actor": "beepboop2025",
        "checkout_sha": sha,
        "dispatch_step_outcome": "success",
        "dispatches": [],
        "event": "workflow_dispatch",
        "event_sha": sha,
        "final_main_sha": sha,
        "observed_at": "2026-08-13T12:00:00Z",
        "observed_main_sha": sha,
        "plan": plan,
        "plan_sha256": hashlib.sha256(_canonical_bytes(plan)).hexdigest(),
        "repository": "beepboop2025/palimpsest",
        "run_attempt": 1,
        "run_id": 123,
        "schema_version": "palimpsest.collector-health-watchdog-receipt.v1",
        "status": "success",
        "workflow": ".github/workflows/collector-health-watchdog.yml",
        "workflow_name": "Recover stale collector publications",
    }


@pytest.fixture(autouse=True)
def _compact_publication_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep clock/lineage unit fixtures small; strict contracts have dedicated tests."""

    def validate_wire(document: dict) -> None:
        if document.get("schema_version") != "palimpsest-newswire.v1":
            raise ValueError("bad compact wire schema")

    def validate_situation(document: dict) -> None:
        if document.get("schema_version") != "palimpsest-china-situation.v1":
            raise ValueError("bad compact situation schema")

    monkeypatch.setattr(watchdog, "_validate_newswire_contract", validate_wire)
    monkeypatch.setattr(watchdog, "_validate_situation_contract", validate_situation)


def _signal(
    signal_id: str,
    status: str,
    *,
    deadline: str = "2026-08-13T11:00:00Z",
    optional: bool = False,
) -> dict:
    return {
        "id": signal_id,
        "status": status,
        "optional": optional,
        "freshness_deadline": deadline,
        "health": {"collector_status": None},
    }


def _document(*signals: dict, generated_at: str = "2026-08-13T11:30:00Z") -> dict:
    return {
        "schema_version": "osint-china.v1",
        "generated_at": generated_at,
        "signals": list(signals),
    }


def _wire(generated_at: str = "2026-08-13T11:30:00Z", *, successes: int = 1) -> dict:
    return {
        "schema_version": "palimpsest-newswire.v1",
        "generated_at": generated_at,
        "coverage": {"counts": {"success": successes}},
    }


def _situation(
    wire: dict,
    generated_at: str = "2026-08-13T11:40:00Z",
) -> dict:
    return {
        "schema_version": "palimpsest-china-situation.v1",
        "generated_at": generated_at,
        "inputs": {
            "newswire_generated_at": wire["generated_at"],
            "newswire_sha256": hashlib.sha256(
                watchdog._canonical_json_bytes(wire)
            ).hexdigest(),
        },
    }


def _healthy_osint() -> dict:
    return _document(
        _signal(
            "silence-index",
            "live",
            deadline="2026-08-13T13:00:00Z",
        )
    )


def test_stale_silence_and_nemesis_get_reviewed_recovery_workflows() -> None:
    plan = watchdog.plan_recoveries(
        _document(
            _signal("silence-index", "stale"),
            _signal("nemesis", "stale", optional=True),
        ),
        NOW,
    )
    assert plan["dispatch"] == [
        "silence-index-refresh.yml",
        "osint-china-v2-refresh.yml",
    ]


def test_browser_time_aging_can_recover_a_signal_serialized_as_live() -> None:
    plan = watchdog.plan_recoveries(_document(_signal("silence-index", "live")), NOW)
    assert plan["dispatch"] == ["silence-index-refresh.yml"]
    assert plan["problems"][0]["status"] == "stale"


def test_semantic_degradation_and_unconfigured_optional_sources_do_not_thrash() -> None:
    plan = watchdog.plan_recoveries(
        _document(
            _signal(
                "believability",
                "degraded",
                deadline="2026-09-01T00:00:00Z",
                optional=True,
            ),
            _signal("nemesis", "missing", deadline=None, optional=True),
        ),
        NOW,
    )
    assert plan["dispatch"] == []
    assert plan["problems"] == [
        {
            "signal_id": "nemesis",
            "status": "missing",
            "optional": True,
            "workflow": None,
            "action": "optional source is not configured; no automatic retry",
        }
    ]


def test_old_command_bundle_is_refreshed_before_embedded_states_are_trusted() -> None:
    plan = watchdog.plan_recoveries(
        _document(
            _signal("silence-index", "stale"),
            generated_at="2026-08-13T08:00:00Z",
        ),
        NOW,
    )
    assert plan["bundle_stale"] is True
    assert plan["dispatch"] == ["osint-china-v2-refresh.yml"]
    assert [row["signal_id"] for row in plan["problems"]] == ["osint-china"]


def test_fresh_matching_publication_heads_need_no_recovery() -> None:
    wire = _wire()
    plan = watchdog.build_watchdog_plan(
        _healthy_osint(), wire, _situation(wire), now=NOW
    )

    assert plan["dispatch"] == []
    assert plan["escalations"] == []
    assert plan["problems"] == []
    assert plan["schema_version"] == "collector-watchdog-plan.v2"


def test_stale_wire_is_not_hidden_by_a_fresh_situation_rebuild() -> None:
    wire = _wire("2026-08-13T08:30:00Z")
    plan = watchdog.build_watchdog_plan(
        _healthy_osint(),
        wire,
        _situation(wire, "2026-08-13T11:50:00Z"),
        now=NOW,
    )

    assert plan["dispatch"] == ["newswire-refresh.yml"]
    assert plan["escalations"] == []
    assert plan["problems"][0]["signal_id"] == "publication/newswire"
    assert plan["problems"][0]["status"] == "stale"


def test_fresh_wire_and_stale_situation_dispatch_only_once() -> None:
    wire = _wire()
    plan = watchdog.build_watchdog_plan(
        _healthy_osint(),
        wire,
        _situation(wire, "2026-08-13T08:30:00Z"),
        now=NOW,
    )

    assert plan["dispatch"] == ["newswire-refresh.yml"]
    assert plan["escalations"] == []
    assert plan["problems"][0]["signal_id"] == "publication/china-situation"


def test_publication_lineage_mismatch_recovers_and_escalates_immediately() -> None:
    wire = _wire()
    situation = _situation(wire)
    situation["inputs"]["newswire_sha256"] = "0" * 64

    plan = watchdog.build_watchdog_plan(_healthy_osint(), wire, situation, now=NOW)

    assert plan["dispatch"] == ["newswire-refresh.yml"]
    assert plan["escalations"] == ["publication/china-situation"]
    assert plan["problems"][0]["status"] == "lineage-mismatch"


def test_persistent_publication_staleness_is_an_escalation() -> None:
    wire = _wire("2026-08-13T05:00:00Z")
    situation = _situation(wire, "2026-08-13T05:10:00Z")

    plan = watchdog.build_watchdog_plan(_healthy_osint(), wire, situation, now=NOW)

    assert plan["dispatch"] == ["newswire-refresh.yml"]
    assert plan["escalations"] == [
        "publication/newswire",
        "publication/china-situation",
    ]


def test_corrupt_or_future_publication_heads_escalate_without_aborting() -> None:
    future_wire = _wire("2026-08-13T12:06:00Z")
    plan = watchdog.build_watchdog_plan(
        _healthy_osint(),
        future_wire,
        None,
        now=NOW,
        situation_unreadable=True,
    )

    assert plan["dispatch"] == ["newswire-refresh.yml"]
    assert plan["escalations"] == [
        "publication/newswire",
        "publication/china-situation",
    ]
    assert [row["status"] for row in plan["problems"]] == [
        "future-clock",
        "corrupt",
    ]


def test_bad_schema_and_empty_coverage_are_immediate_escalations() -> None:
    bad_schema = _wire()
    bad_schema["schema_version"] = "palimpsest-newswire.v0"
    schema_plan = watchdog.build_watchdog_plan(
        _healthy_osint(), bad_schema, _situation(_wire()), now=NOW
    )
    assert schema_plan["problems"][0]["status"] == "corrupt"
    assert schema_plan["escalations"] == ["publication/newswire"]

    empty_wire = _wire(successes=0)
    empty_plan = watchdog.build_watchdog_plan(
        _healthy_osint(), empty_wire, _situation(empty_wire), now=NOW
    )
    assert empty_plan["problems"][0]["status"] == "empty-coverage"
    assert empty_plan["escalations"] == ["publication/newswire"]


def test_checked_in_publication_heads_pass_the_full_contracts() -> None:
    wire = json.loads(
        (ROOT / "readings" / "newswire-latest.json").read_text(encoding="utf-8")
    )
    situation = json.loads(
        (ROOT / "readings" / "china-situation-latest.json").read_text(encoding="utf-8")
    )

    STRICT_NEWSWIRE_VALIDATOR(wire)
    STRICT_SITUATION_VALIDATOR(situation)


def test_full_contract_rejection_is_an_immediate_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(_document: dict) -> None:
        raise watchdog.newswire_model.NewswireError("invalid nested event")

    monkeypatch.setattr(watchdog, "_validate_newswire_contract", reject)
    wire = _wire()
    plan = watchdog.build_watchdog_plan(
        _healthy_osint(), wire, _situation(wire), now=NOW
    )

    assert plan["problems"][0]["status"] == "corrupt"
    assert plan["escalations"] == ["publication/newswire"]


def test_corrupt_collector_bundle_cannot_suppress_publication_recovery() -> None:
    wire = _wire("2026-08-13T05:00:00Z")
    plan = watchdog.build_watchdog_plan(
        {"schema_version": "attacker-controlled.v1"},
        wire,
        _situation(wire, "2026-08-13T05:10:00Z"),
        now=NOW,
    )

    assert plan["dispatch"][:2] == [
        "newswire-refresh.yml",
        "osint-china-v2-refresh.yml",
    ]
    assert [problem["signal_id"] for problem in plan["problems"]] == [
        "publication/newswire",
        "publication/china-situation",
        "osint-china",
    ]


def test_cli_recovers_from_an_unreadable_collector_bundle(
    tmp_path: Path, capsys
) -> None:
    collector_path = tmp_path / "osint.json"
    wire_path = tmp_path / "wire.json"
    situation_path = tmp_path / "situation.json"
    collector_path.write_text("{", encoding="utf-8")
    wire = _wire()
    wire_path.write_text(json.dumps(wire), encoding="utf-8")
    situation_path.write_text(json.dumps(_situation(wire)), encoding="utf-8")

    assert (
        watchdog.main(
            [
                "--input",
                str(collector_path),
                "--newswire-input",
                str(wire_path),
                "--situation-input",
                str(situation_path),
                "--now",
                "2026-08-13T12:00:00Z",
                "--format",
                "workflows",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == "osint-china-v2-refresh.yml\n"


def test_shared_producers_are_deduplicated_and_dispatches_are_bounded() -> None:
    signals = [
        _signal("board-alarm", "stale"),
        _signal("coverage-guard", "stale"),
        _signal("forecast-ledger", "stale"),
        _signal("cross-layer", "stale"),
        _signal("ddti", "stale"),
        _signal("gdelt", "stale"),
        _signal("weibo-hotsearch", "stale"),
        _signal("silence-index", "stale"),
    ]
    plan = watchdog.plan_recoveries(_document(*signals), NOW)
    assert len(plan["dispatch"]) == watchdog.MAX_DISPATCHES
    assert plan["dispatch"].count("board-alarm-refresh.yml") == 1


def test_publication_recovery_has_priority_inside_the_dispatch_bound() -> None:
    wire = _wire("2026-08-13T08:30:00Z")
    collector = _document(
        _signal("board-alarm", "stale"),
        _signal("ddti", "stale"),
        _signal("gdelt", "stale"),
        _signal("weibo-hotsearch", "stale"),
        _signal("silence-index", "stale"),
    )

    plan = watchdog.build_watchdog_plan(collector, wire, _situation(wire), now=NOW)

    assert plan["dispatch"][0] == "newswire-refresh.yml"
    assert len(plan["dispatch"]) == watchdog.MAX_DISPATCHES
    assert len(plan["dispatch"]) == len(set(plan["dispatch"]))


def test_every_recovery_target_exists_and_accepts_manual_dispatch() -> None:
    workflow_root = ROOT / ".github" / "workflows"
    for workflow_name in set(watchdog.RECOVERY_WORKFLOWS.values()):
        workflow = yaml.safe_load(
            (workflow_root / workflow_name).read_text(encoding="utf-8")
        )
        assert "workflow_dispatch" in workflow[True], workflow_name
    newswire = yaml.safe_load(
        (workflow_root / "newswire-refresh.yml").read_text(encoding="utf-8")
    )
    assert "workflow_dispatch" in newswire[True]


def test_watchdog_workflow_has_only_narrow_read_and_dispatch_permissions() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read", "actions": "write"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["jobs"]["recover"]["timeout-minutes"] == 10
    assert "workflow_run" in workflow[True]
    assert workflow["jobs"]["recover"]["if"].endswith(
        "github.event.workflow_run.conclusion == 'success'"
    )
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "gh workflow run" in source
    assert "ref: ${{ github.sha }}" in source
    assert "persist-credentials: false" in source
    assert "watchdog_now=" in source
    assert source.count('--now "$watchdog_now"') == 2
    assert "--format json" in source
    assert "--format summary" in source
    assert "WATCHDOG_TRIGGER: ${{ github.event_name }}" in source
    assert "if: always()" in source
    assert (
        "collector-health-watchdog-receipt-${{ github.run_id }}-${{ github.run_attempt }}"
        in source
    )
    assert "retention-days: 90" in source
    assert "compression-level: 0" in source
    assert "Refresh evidence wire" not in source
    assert "git push" not in source
    assert "issues: write" not in source
    assert "cancel-in-progress: true" not in source


def test_watchdog_respects_live_workflow_activation_authority() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    state_query = '"repos/$GITHUB_REPOSITORY/actions/workflows/$workflow"'
    failed_query = "if ! workflow_state=$(gh api"
    exact_active_gate = 'if [ "$workflow_state" != "active" ]; then'
    active_run_query = "active=$(gh run list"
    dispatch = 'gh workflow run "$workflow" --repo "$GITHUB_REPOSITORY"'

    assert state_query in source
    assert "--jq '.state'" in source
    assert failed_query in source
    assert exact_active_gate in source
    assert source.index(failed_query) < source.index(exact_active_gate)
    assert source.index(exact_active_gate) < source.index(active_run_query)
    assert source.index(active_run_query) < source.index(dispatch)
    assert "respecting its operator-controlled freeze" in source
    assert '"failed_state_query"' in source


def test_cli_emits_only_allowlisted_workflow_names(tmp_path: Path, capsys) -> None:
    source = tmp_path / "osint.json"
    source.write_text(
        json.dumps(_document(_signal("silence-index", "stale"))),
        encoding="utf-8",
    )
    wire = _wire()
    wire_path = tmp_path / "wire.json"
    situation_path = tmp_path / "situation.json"
    wire_path.write_text(json.dumps(wire), encoding="utf-8")
    situation_path.write_text(json.dumps(_situation(wire)), encoding="utf-8")
    assert (
        watchdog.main(
            [
                "--input",
                str(source),
                "--newswire-input",
                str(wire_path),
                "--situation-input",
                str(situation_path),
                "--now",
                "2026-08-13T12:00:00Z",
                "--format",
                "workflows",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == "silence-index-refresh.yml\n"


def test_cli_escalation_output_is_bounded_and_still_returns_zero(
    tmp_path: Path, capsys
) -> None:
    osint_path = tmp_path / "osint.json"
    wire_path = tmp_path / "wire.json"
    situation_path = tmp_path / "situation.json"
    wire = _wire()
    situation = _situation(wire)
    situation["inputs"]["newswire_generated_at"] = "2026-08-13T10:00:00Z"
    osint_path.write_text(json.dumps(_healthy_osint()), encoding="utf-8")
    wire_path.write_text(json.dumps(wire), encoding="utf-8")
    situation_path.write_text(json.dumps(situation), encoding="utf-8")

    assert (
        watchdog.main(
            [
                "--input",
                str(osint_path),
                "--newswire-input",
                str(wire_path),
                "--situation-input",
                str(situation_path),
                "--now",
                "2026-08-13T12:00:00Z",
                "--format",
                "escalations",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == "publication/china-situation\n"


def test_watchdog_dispatches_osint_with_its_required_exact_inputs() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    dispatch_script = next(
        step["run"]
        for step in workflow["jobs"]["recover"]["steps"]
        if step.get("id") == "dispatch"
    )
    osint = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml").read_text(
            encoding="utf-8"
        )
    )
    required = {
        name
        for name, value in osint[True]["workflow_dispatch"]["inputs"].items()
        if value.get("required") is True
    }

    assert required == {"expected_deploy_sha", "release_nonce"}
    assert 'if [ "$workflow" = "osint-china-v2-refresh.yml" ]; then' in dispatch_script
    assert '-f expected_deploy_sha="$dispatch_sha"' in dispatch_script
    assert '-f release_nonce="$release_nonce"' in dispatch_script
    assert dispatch_script.count("gh workflow run") == 2
    non_osint_branch = dispatch_script.split("else\n", 1)[1]
    assert 'gh workflow run "$workflow" --repo "$GITHUB_REPOSITORY"' in non_osint_branch
    assert " -f " not in non_osint_branch.split("fi\n", 1)[0]


def test_watchdog_osint_sha_drift_is_fail_closed_on_both_sides() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    dispatch_script = next(
        step["run"]
        for step in workflow["jobs"]["recover"]["steps"]
        if step.get("id") == "dispatch"
    )

    pre_read = "if ! dispatch_sha=$(read_main_sha); then"
    pre_drift = 'if [ "$dispatch_sha" != "$before_sha" ]; then'
    osint_branch = 'if [ "$workflow" = "osint-china-v2-refresh.yml" ]; then'
    post_read = "if ! after_sha=$(read_main_sha); then"
    post_drift = 'if [ "$after_sha" != "$dispatch_sha" ]; then'
    assert dispatch_script.index(pre_read) < dispatch_script.index(pre_drift)
    assert dispatch_script.index(pre_drift) < dispatch_script.index(osint_branch)
    assert dispatch_script.index(osint_branch) < dispatch_script.index(post_read)
    assert dispatch_script.index(post_read) < dispatch_script.index(post_drift)
    assert dispatch_script.count('"failed_ref_drift"') == 2
    assert 'test "$observed_main_sha" = "$checkout_sha"' in next(
        step["run"]
        for step in workflow["jobs"]["recover"]["steps"]
        if step.get("id") == "plan"
    )


def test_watchdog_release_nonces_are_lowercase_exact_and_unique() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets.token_hex(16)" in source
    assert '[[ "$release_nonce" =~ ^[0-9a-f]{32}$ ]]' in source

    generated = {secrets.token_hex(16) for _ in range(128)}
    assert len(generated) == 128
    assert all(re.fullmatch(r"[0-9a-f]{32}", item) for item in generated)


def test_watchdog_receipt_allowlist_matches_the_planner_exactly() -> None:
    schema = _receipt_schema()
    schema_allowlist = set(schema["$defs"]["workflow"]["enum"])
    planner_allowlist = set(watchdog.RECOVERY_WORKFLOWS.values()) | {
        "newswire-refresh.yml"
    }
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    plan_script = next(
        step["run"]
        for step in workflow["jobs"]["recover"]["steps"]
        if step.get("id") == "plan"
    )
    inline_allowlist = set(
        re.findall(r'^\s+"([a-z0-9-]+\.yml)",$', plan_script, re.MULTILINE)
    )
    dispatch_script = next(
        step["run"]
        for step in workflow["jobs"]["recover"]["steps"]
        if step.get("id") == "dispatch"
    )

    assert schema_allowlist == planner_allowlist
    assert inline_allowlist == planner_allowlist
    assert all(name in dispatch_script for name in planner_allowlist)


def test_watchdog_receipt_schema_is_closed_and_accepts_canonical_zero_action() -> None:
    schema = _receipt_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    receipt = _zero_action_receipt()
    validator.validate(receipt)
    payload = _canonical_bytes(receipt)

    assert payload.endswith(b"\n")
    assert json.loads(payload) == receipt
    assert _canonical_bytes(json.loads(payload)) == payload
    assert receipt["plan"]["dispatch"] == []
    assert receipt["dispatches"] == []
    assert receipt["status"] == "success"
    assert (
        receipt["plan_sha256"]
        == hashlib.sha256(_canonical_bytes(receipt["plan"])).hexdigest()
    )

    poisoned = {**receipt, "credential": "must-not-exist"}
    with pytest.raises(ValidationError):
        validator.validate(poisoned)


def test_watchdog_receipt_accepts_only_exact_osint_arguments() -> None:
    schema = _receipt_schema()
    validator = Draft202012Validator(schema)
    receipt = _zero_action_receipt()
    sha = receipt["checkout_sha"]
    receipt["plan"]["dispatch"] = ["osint-china-v2-refresh.yml"]
    receipt["plan_sha256"] = hashlib.sha256(
        _canonical_bytes(receipt["plan"])
    ).hexdigest()
    receipt["dispatches"] = [
        {
            "active_runs": 0,
            "dispatch_args": {
                "inputs": {
                    "expected_deploy_sha": sha,
                    "release_nonce": "b" * 32,
                },
                "ref": "main",
            },
            "main_sha_after_dispatch": sha,
            "main_sha_before_dispatch": sha,
            "observed_at": receipt["observed_at"],
            "outcome": "dispatched",
            "workflow": "osint-china-v2-refresh.yml",
            "workflow_state": "active",
        }
    ]
    validator.validate(receipt)

    bad_nonce = json.loads(json.dumps(receipt))
    bad_nonce["dispatches"][0]["dispatch_args"]["inputs"]["release_nonce"] = "B" * 32
    with pytest.raises(ValidationError):
        validator.validate(bad_nonce)

    missing_sha = json.loads(json.dumps(receipt))
    del missing_sha["dispatches"][0]["dispatch_args"]["inputs"]["expected_deploy_sha"]
    with pytest.raises(ValidationError):
        validator.validate(missing_sha)


def test_watchdog_receipt_artifact_is_exact_and_secret_free() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow[True]["workflow_dispatch"] == {}
    upload = next(
        step
        for step in workflow["jobs"]["recover"]["steps"]
        if step.get("id") == "receipt_artifact"
    )
    assert upload["with"] == {
        "name": "collector-health-watchdog-receipt-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "${{ runner.temp }}/collector-health-watchdog-receipt.json",
        "if-no-files-found": "error",
        "retention-days": 90,
        "compression-level": 0,
    }

    forbidden = re.compile(r"(?:authorization|credential|password|secret|token)", re.I)

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert not [key for key in keys(_zero_action_receipt()) if forbidden.search(key)]
    receipt_script = next(
        step["run"]
        for step in workflow["jobs"]["recover"]["steps"]
        if step.get("id") == "receipt"
    )
    assert "watchdog receipt contains a secret field" in receipt_script
    assert "allow_nan=False" in receipt_script
    assert 'separators=(",", ":")' in receipt_script
    assert "sort_keys=True" in receipt_script
    assert "os.O_EXCL" in receipt_script
    assert "O_NOFOLLOW" in receipt_script
    assert receipt_script.count("os.fsync(") == 2
    assert "output.write_bytes" not in receipt_script
