"""The manual OSINT producer exposes exact outcomes without faking Phase 2."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml"
SCHEMA_VERSION = "palimpsest.osint-manual-outcome.v1"
OUTCOME_FIELDS = {
    "acquisition_base_sha",
    "base_sha",
    "candidate_changed",
    "candidate_sha",
    "current_main_sha",
    "event",
    "expected_deploy_sha",
    "head_sha",
    "output_parents",
    "output_sha",
    "publication_commit",
    "push_exit_code",
    "push_outcome",
    "recorded_at",
    "release_nonce",
    "repository",
    "result",
    "retry_candidate_changed",
    "retry_candidate_sha",
    "retry_exit_code",
    "retry_outcome",
    "run_attempt",
    "run_id",
    "schema_version",
    "synchronized_candidate_changed",
    "workflow",
    "workflow_name",
}
LEGACY_FIELDS = {
    "expected_deploy_sha",
    "head_sha",
    "publication_commit",
    "release_nonce",
    "run_attempt",
    "run_id",
    "schema_version",
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(*, step_id: str | None = None, name: str | None = None) -> dict:
    for step in _workflow()["jobs"]["publish"]["steps"]:
        if step_id is not None and step.get("id") == step_id:
            return step
        if name is not None and step.get("name") == name:
            return step
    raise AssertionError(f"missing workflow step: {step_id or name}")


def _heredoc(step_id: str, marker: str) -> str:
    run = _step(step_id=step_id)["run"]
    start = run.index(marker) + len(marker)
    end = run.index("\nPY\n", start)
    return run[start:end]


def _outcome_source() -> str:
    return _heredoc("manual_outcome", "python - \"$receipt_path\" <<'PY'\n")


def _legacy_source() -> str:
    return _heredoc(
        "phase2_compatibility",
        'python - "$legacy_path" "$publication_commit" <<\'PY\'\n',
    )


def _base_env() -> dict[str, str]:
    base = "a" * 40
    return {
        **os.environ,
        "ACQUISITION_BASE_SHA": base,
        "CANDIDATE_CHANGED": "false",
        "CURRENT_MAIN_SHA": base,
        "EXPECTED_DEPLOY_SHA": base,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "beepboop2025/palimpsest",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "789012",
        "GITHUB_SHA": base,
        "INITIAL_CANDIDATE_PARENT_SHA": "",
        "INITIAL_CANDIDATE_SHA": "",
        "INITIAL_PUSH_EXIT_CODE": "",
        "INITIAL_PUSH_OUTCOME": "skipped",
        "ORIGINAL_CANDIDATE_SHA": base,
        "RACE_CANDIDATE_CHANGED": "",
        "RELEASE_NONCE": "b" * 32,
        "RETRY_CANDIDATE_PARENT_SHA": "",
        "RETRY_CANDIDATE_SHA": "",
        "RETRY_PUSH_EXIT_CODE": "",
        "RETRY_PUSH_OUTCOME": "skipped",
        "SYNCHRONIZED_CANDIDATE_CHANGED": "",
    }


def _run_outcome(
    receipt_path: Path, **updates: str
) -> tuple[subprocess.CompletedProcess[str], dict | None]:
    env = _base_env()
    env.update(updates)
    result = subprocess.run(
        [sys.executable, "-", str(receipt_path)],
        input=_outcome_source(),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    receipt = None
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    return result, receipt


def _canonical_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def test_exact_no_change_is_a_canonical_terminal_outcome(tmp_path: Path) -> None:
    path = tmp_path / "osint-manual-outcome.json"
    result, receipt = _run_outcome(path)

    assert result.returncode == 0, result.stderr
    assert receipt is not None
    assert set(receipt) == OUTCOME_FIELDS
    assert receipt["schema_version"] == SCHEMA_VERSION
    assert receipt["result"] == "no_change"
    assert receipt["head_sha"] == receipt["expected_deploy_sha"]
    assert receipt["expected_deploy_sha"] == receipt["acquisition_base_sha"]
    assert receipt["acquisition_base_sha"] == receipt["base_sha"]
    assert receipt["base_sha"] == receipt["candidate_sha"]
    assert receipt["candidate_sha"] == receipt["output_sha"]
    assert receipt["output_sha"] == receipt["publication_commit"]
    assert receipt["publication_commit"] == receipt["current_main_sha"]
    assert receipt["output_parents"] == []
    assert receipt["push_exit_code"] is receipt["retry_exit_code"] is None
    assert receipt["push_outcome"] == receipt["retry_outcome"] == "skipped"
    assert path.read_bytes() == _canonical_bytes(receipt)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("exit_code", "step_outcome"), [("0", "success"), ("76", "failure")]
)
def test_initial_publication_proves_one_exact_child(
    tmp_path: Path, exit_code: str, step_outcome: str
) -> None:
    base = "a" * 40
    output = "c" * 40
    result, receipt = _run_outcome(
        tmp_path / "osint-manual-outcome.json",
        CANDIDATE_CHANGED="true",
        CURRENT_MAIN_SHA=output,
        INITIAL_CANDIDATE_PARENT_SHA=base,
        INITIAL_CANDIDATE_SHA=output,
        INITIAL_PUSH_EXIT_CODE=exit_code,
        INITIAL_PUSH_OUTCOME=step_outcome,
        ORIGINAL_CANDIDATE_SHA=output,
    )

    assert result.returncode == 0, result.stderr
    assert receipt is not None
    assert receipt["result"] == "committed"
    assert receipt["base_sha"] == base
    assert receipt["output_parents"] == [base]
    assert receipt["candidate_sha"] == output
    assert receipt["output_sha"] == receipt["publication_commit"] == output
    assert receipt["current_main_sha"] == output
    assert receipt["push_exit_code"] == int(exit_code)


@pytest.mark.parametrize(
    ("retry_code", "retry_outcome"), [("0", "success"), ("76", "failure")]
)
def test_race_retry_preserves_both_bases_and_exact_output(
    tmp_path: Path, retry_code: str, retry_outcome: str
) -> None:
    first_base = "a" * 40
    first_candidate = "b" * 40
    retry_base = "c" * 40
    output = "d" * 40
    result, receipt = _run_outcome(
        tmp_path / "osint-manual-outcome.json",
        CANDIDATE_CHANGED="true",
        CURRENT_MAIN_SHA=output,
        INITIAL_CANDIDATE_PARENT_SHA=first_base,
        INITIAL_CANDIDATE_SHA=first_candidate,
        INITIAL_PUSH_EXIT_CODE="75",
        INITIAL_PUSH_OUTCOME="failure",
        ORIGINAL_CANDIDATE_SHA=first_candidate,
        RACE_CANDIDATE_CHANGED="true",
        RETRY_CANDIDATE_PARENT_SHA=retry_base,
        RETRY_CANDIDATE_SHA=output,
        RETRY_PUSH_EXIT_CODE=retry_code,
        RETRY_PUSH_OUTCOME=retry_outcome,
    )

    assert result.returncode == 0, result.stderr
    assert receipt is not None
    assert receipt["result"] == "committed"
    assert receipt["acquisition_base_sha"] == first_base
    assert receipt["base_sha"] == retry_base
    assert receipt["candidate_sha"] == first_candidate
    assert receipt["retry_candidate_sha"] == output
    assert receipt["output_sha"] == receipt["publication_commit"] == output
    assert receipt["output_parents"] == [retry_base]


@pytest.mark.parametrize(
    "updates",
    [
        {"CURRENT_MAIN_SHA": "c" * 40},
        {
            "CANDIDATE_CHANGED": "true",
            "CURRENT_MAIN_SHA": "d" * 40,
            "INITIAL_CANDIDATE_PARENT_SHA": "a" * 40,
            "INITIAL_CANDIDATE_SHA": "c" * 40,
            "INITIAL_PUSH_EXIT_CODE": "0",
            "INITIAL_PUSH_OUTCOME": "success",
            "ORIGINAL_CANDIDATE_SHA": "c" * 40,
        },
    ],
)
def test_concurrent_main_advance_is_recorded_but_not_safe(
    tmp_path: Path, updates: dict[str, str]
) -> None:
    result, receipt = _run_outcome(tmp_path / "osint-manual-outcome.json", **updates)

    assert result.returncode == 0, result.stderr
    assert receipt is not None
    assert receipt["result"] == "concurrent_main_advance"


def test_ambiguous_failed_publication_refuses_to_emit(tmp_path: Path) -> None:
    path = tmp_path / "osint-manual-outcome.json"
    result, receipt = _run_outcome(
        path,
        CANDIDATE_CHANGED="true",
        INITIAL_CANDIDATE_PARENT_SHA="a" * 40,
        INITIAL_CANDIDATE_SHA="b" * 40,
        INITIAL_PUSH_EXIT_CODE="1",
        INITIAL_PUSH_OUTCOME="failure",
        ORIGINAL_CANDIDATE_SHA="b" * 40,
    )

    assert result.returncode != 0
    assert "ambiguous failed OSINT publication" in result.stderr
    assert receipt is None


def test_step_outcome_and_exit_code_must_agree(tmp_path: Path) -> None:
    result, receipt = _run_outcome(
        tmp_path / "osint-manual-outcome.json",
        CANDIDATE_CHANGED="true",
        INITIAL_CANDIDATE_PARENT_SHA="a" * 40,
        INITIAL_CANDIDATE_SHA="b" * 40,
        INITIAL_PUSH_EXIT_CODE="76",
        INITIAL_PUSH_OUTCOME="success",
        ORIGINAL_CANDIDATE_SHA="b" * 40,
    )

    assert result.returncode != 0
    assert "does not match its step outcome" in result.stderr
    assert receipt is None


def test_outcome_writer_is_exclusive_and_preserves_first_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "osint-manual-outcome.json"
    first, receipt = _run_outcome(path)
    original = path.read_bytes()
    second, _ = _run_outcome(path, CURRENT_MAIN_SHA="c" * 40)

    assert first.returncode == 0
    assert receipt is not None
    assert second.returncode != 0
    assert path.read_bytes() == original


def test_new_outcome_artifact_is_run_attempt_unique_and_authoritative() -> None:
    artifact = _step(step_id="manual_outcome_artifact")

    assert artifact["with"] == {
        "name": "osint-manual-outcome-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "${{ steps.manual_outcome.outputs.path }}",
        "if-no-files-found": "error",
        "retention-days": 90,
        "compression-level": 0,
    }
    assert "always()" in artifact["if"]
    assert "steps.manual_outcome.outcome == 'success'" in artifact["if"]


def test_legacy_phase2_receipt_stays_exact_and_first_attempt_committed_only(
    tmp_path: Path,
) -> None:
    step = _step(step_id="phase2_compatibility")
    artifact = _step(step_id="phase2_compatibility_artifact")
    publication = "c" * 40
    path = tmp_path / "osint-release-run.json"
    env = _base_env()
    result = subprocess.run(
        [sys.executable, "-", str(path), publication],
        input=_legacy_source(),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert set(receipt) == LEGACY_FIELDS
    assert receipt == {
        "expected_deploy_sha": "a" * 40,
        "head_sha": "a" * 40,
        "publication_commit": publication,
        "release_nonce": "b" * 32,
        "run_attempt": 1,
        "run_id": 789012,
        "schema_version": "palimpsest-osint-release-run.v1",
    }
    assert path.read_bytes() == _canonical_bytes(receipt)
    assert "github.run_attempt == 1" in step["if"]
    assert "steps.manual_outcome.outputs.result == 'committed'" in step["if"]
    assert artifact["with"] == {
        "name": "palimpsest-osint-release-${{ github.run_id }}",
        "path": "${{ steps.phase2_compatibility.outputs.path }}",
        "if-no-files-found": "error",
        "retention-days": 14,
    }


def test_no_change_never_creates_legacy_phase2_authority() -> None:
    initial_push = _step(step_id="push_attempt")
    retry_push = _step(step_id="retry_push")
    legacy = _step(step_id="phase2_compatibility")
    final = _step(name="Enforce an unambiguous manual OSINT outcome")

    assert (
        "steps.synchronized_candidate.outputs.changed != 'false'" in initial_push["if"]
    )
    assert "steps.race_candidate.outputs.changed == 'true'" in retry_push["if"]
    assert "outputs.result == 'committed'" in legacy["if"]
    assert "no_change)" in final["run"]
    assert (
        "LEGACY_RECEIPT_STEP"
        not in final["run"].split("no_change)", 1)[1].split("committed)", 1)[0]
    )


def test_dispatch_retry_and_terminal_enforcement_are_receipt_gated() -> None:
    retry_push = _step(step_id="retry_push")
    retry_contract = _step(name="Retry the exact contract event without rebuilding")
    retry_race_contract = _step(name="Retry the race-safe exact contract event")
    enforce_push = _step(name="Enforce a successful OSINT publication attempt")
    enforce_manual = _step(name="Enforce an unambiguous manual OSINT outcome")

    assert retry_push["continue-on-error"] is True
    assert "steps.manual_outcome.outputs.result == 'committed'" in retry_contract["if"]
    assert "steps.manual_outcome_artifact.outcome == 'success'" in retry_contract["if"]
    assert (
        "steps.manual_outcome.outputs.result == 'committed'"
        in retry_race_contract["if"]
    )
    assert (
        "steps.manual_outcome_artifact.outcome == 'success'"
        in (retry_race_contract["if"])
    )
    assert "0:|76:|75:0|75:76" in enforce_push["run"]
    assert "no_change)" in enforce_manual["run"]
    assert "committed)" in enforce_manual["run"]
    assert "not activation-safe" in enforce_manual["run"]


def test_outcome_contains_no_secret_shaped_fields(tmp_path: Path) -> None:
    result, receipt = _run_outcome(tmp_path / "osint-manual-outcome.json")

    assert result.returncode == 0
    assert receipt is not None
    encoded = json.dumps(receipt, sort_keys=True).lower()
    for forbidden in ("authorization", "credential", "password", "secret", "token"):
        assert forbidden not in encoded


def test_embedded_receipt_programs_compile() -> None:
    compile(_outcome_source(), str(WORKFLOW), "exec")
    compile(_legacy_source(), str(WORKFLOW), "exec")
