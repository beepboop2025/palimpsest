"""Manual Newswire restores leave one strict, immutable publication outcome."""

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
WORKFLOW = ROOT / ".github" / "workflows" / "newswire-refresh.yml"
SCHEMA_VERSION = "palimpsest.newswire-manual-outcome.v1"
ROOT_FIELDS = {
    "acquisition_base_sha",
    "base_sha",
    "candidate_changed",
    "candidate_sha",
    "current_main_sha",
    "event",
    "head_sha",
    "output_parents",
    "output_sha",
    "push_outcome",
    "recorded_at",
    "repository",
    "result",
    "retry_candidate_changed",
    "retry_candidate_sha",
    "retry_outcome",
    "run_attempt",
    "run_id",
    "schema_version",
    "synchronized_candidate_changed",
    "workflow",
    "workflow_name",
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


def _receipt_source() -> str:
    run = _step(step_id="manual_outcome")["run"]
    marker = "python - \"$receipt_path\" <<'PY'\n"
    start = run.index(marker) + len(marker)
    end = run.index("\nPY\n", start)
    return run[start:end]


def _base_env() -> dict[str, str]:
    base = "a" * 40
    return {
        **os.environ,
        "ACQUISITION_BASE_SHA": base,
        "CANDIDATE_CHANGED": "false",
        "CURRENT_MAIN_SHA": base,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "beepboop2025/palimpsest",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "123456",
        "INITIAL_CANDIDATE_PARENT_SHA": "",
        "INITIAL_CANDIDATE_SHA": "",
        "INITIAL_PUSH_OUTCOME": "skipped",
        "NEWSWIRE_HEAD_SHA": base,
        "ORIGINAL_CANDIDATE_SHA": base,
        "RACE_CANDIDATE_CHANGED": "",
        "RETRY_CANDIDATE_PARENT_SHA": "",
        "RETRY_CANDIDATE_SHA": "",
        "RETRY_PUSH_OUTCOME": "skipped",
        "SYNCHRONIZED_CANDIDATE_CHANGED": "",
    }


def _run_receipt(
    receipt_path: Path, **updates: str
) -> tuple[subprocess.CompletedProcess[str], dict | None]:
    env = _base_env()
    env.update(updates)
    result = subprocess.run(
        [sys.executable, "-", str(receipt_path)],
        input=_receipt_source(),
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


def test_no_change_receipt_is_closed_canonical_and_durable(tmp_path: Path) -> None:
    receipt_path = tmp_path / "newswire-manual-outcome.json"
    result, receipt = _run_receipt(receipt_path)

    assert result.returncode == 0, result.stderr
    assert receipt is not None
    assert set(receipt) == ROOT_FIELDS
    assert receipt["schema_version"] == SCHEMA_VERSION
    assert receipt["result"] == "no_change"
    assert receipt["head_sha"] == receipt["acquisition_base_sha"]
    assert receipt["base_sha"] == receipt["candidate_sha"]
    assert receipt["candidate_sha"] == receipt["output_sha"]
    assert receipt["output_sha"] == receipt["current_main_sha"]
    assert receipt["output_parents"] == []
    assert receipt["push_outcome"] == receipt["retry_outcome"] == "skipped"
    assert receipt_path.read_bytes() == _canonical_bytes(receipt)
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600


def test_initial_push_receipt_proves_one_exact_child(tmp_path: Path) -> None:
    base = "a" * 40
    output = "b" * 40
    result, receipt = _run_receipt(
        tmp_path / "newswire-manual-outcome.json",
        CANDIDATE_CHANGED="true",
        CURRENT_MAIN_SHA=output,
        INITIAL_CANDIDATE_PARENT_SHA=base,
        INITIAL_CANDIDATE_SHA=output,
        INITIAL_PUSH_OUTCOME="success",
        ORIGINAL_CANDIDATE_SHA=output,
    )

    assert result.returncode == 0, result.stderr
    assert receipt is not None
    assert receipt["result"] == "committed"
    assert receipt["base_sha"] == base
    assert receipt["candidate_sha"] == output
    assert receipt["output_sha"] == receipt["current_main_sha"] == output
    assert receipt["output_parents"] == [base]
    assert receipt["push_outcome"] == "success"
    assert receipt["retry_outcome"] == "skipped"


def test_retry_receipt_preserves_acquisition_and_actual_publication_bases(
    tmp_path: Path,
) -> None:
    acquisition_base = "a" * 40
    first_candidate = "b" * 40
    retry_base = "c" * 40
    output = "d" * 40
    result, receipt = _run_receipt(
        tmp_path / "newswire-manual-outcome.json",
        ACQUISITION_BASE_SHA=acquisition_base,
        CANDIDATE_CHANGED="true",
        CURRENT_MAIN_SHA=output,
        INITIAL_CANDIDATE_PARENT_SHA=acquisition_base,
        INITIAL_CANDIDATE_SHA=first_candidate,
        INITIAL_PUSH_OUTCOME="failure",
        ORIGINAL_CANDIDATE_SHA=first_candidate,
        RACE_CANDIDATE_CHANGED="true",
        RETRY_CANDIDATE_PARENT_SHA=retry_base,
        RETRY_CANDIDATE_SHA=output,
        RETRY_PUSH_OUTCOME="success",
    )

    assert result.returncode == 0, result.stderr
    assert receipt is not None
    assert receipt["result"] == "committed"
    assert receipt["acquisition_base_sha"] == acquisition_base
    assert receipt["base_sha"] == retry_base
    assert receipt["candidate_sha"] == first_candidate
    assert receipt["retry_candidate_sha"] == output
    assert receipt["output_sha"] == output
    assert receipt["output_parents"] == [retry_base]


@pytest.mark.parametrize(
    ("updates", "expected_output"),
    [
        ({"CURRENT_MAIN_SHA": "c" * 40}, None),
        (
            {
                "CANDIDATE_CHANGED": "true",
                "CURRENT_MAIN_SHA": "c" * 40,
                "INITIAL_CANDIDATE_PARENT_SHA": "a" * 40,
                "INITIAL_CANDIDATE_SHA": "b" * 40,
                "INITIAL_PUSH_OUTCOME": "success",
                "ORIGINAL_CANDIDATE_SHA": "b" * 40,
            },
            "b" * 40,
        ),
    ],
)
def test_main_races_are_truthfully_terminal_but_not_activation_safe(
    tmp_path: Path, updates: dict[str, str], expected_output: str | None
) -> None:
    result, receipt = _run_receipt(tmp_path / "newswire-manual-outcome.json", **updates)

    assert result.returncode == 0, result.stderr
    assert receipt is not None
    assert receipt["result"] == "concurrent_main_advance"
    assert receipt["output_sha"] == expected_output


def test_failed_push_without_observable_advance_refuses_to_lie(tmp_path: Path) -> None:
    receipt_path = tmp_path / "newswire-manual-outcome.json"
    result, receipt = _run_receipt(
        receipt_path,
        CANDIDATE_CHANGED="true",
        INITIAL_CANDIDATE_PARENT_SHA="a" * 40,
        INITIAL_CANDIDATE_SHA="b" * 40,
        INITIAL_PUSH_OUTCOME="failure",
        ORIGINAL_CANDIDATE_SHA="b" * 40,
        RETRY_PUSH_OUTCOME="failure",
    )

    assert result.returncode != 0
    assert "ambiguous failed publication" in result.stderr
    assert receipt is None


def test_receipt_is_exclusive_and_never_overwrites_terminal_evidence(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "newswire-manual-outcome.json"
    first, receipt = _run_receipt(receipt_path)
    original = receipt_path.read_bytes()
    second, _ = _run_receipt(receipt_path, CURRENT_MAIN_SHA="c" * 40)

    assert first.returncode == 0
    assert receipt is not None
    assert second.returncode != 0
    assert receipt_path.read_bytes() == original


def test_receipt_has_no_secret_shaped_fields(tmp_path: Path) -> None:
    result, receipt = _run_receipt(tmp_path / "newswire-manual-outcome.json")

    assert result.returncode == 0
    assert receipt is not None
    encoded = json.dumps(receipt, sort_keys=True).lower()
    for forbidden in ("authorization", "credential", "password", "secret", "token"):
        assert forbidden not in encoded


def test_workflow_preserves_one_unique_run_attempt_artifact() -> None:
    artifact = _step(step_id="manual_outcome_artifact")

    assert artifact["uses"] == (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    )
    assert artifact["with"] == {
        "name": "newswire-manual-outcome-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "${{ steps.manual_outcome.outputs.path }}",
        "if-no-files-found": "error",
        "retention-days": 90,
        "compression-level": 0,
    }
    assert "always()" in artifact["if"]
    assert "steps.manual_outcome.outcome == 'success'" in artifact["if"]


def test_dispatch_and_terminal_enforcement_fail_closed() -> None:
    retry = _step(step_id="retry_push")
    dispatch = _step(name="Dispatch the exact publication contract")
    enforce_push = _step(name="Enforce a successful publication push")
    enforce_manual = _step(name="Enforce an unambiguous manual publication outcome")

    assert retry["continue-on-error"] is True
    assert "steps.manual_outcome.outputs.result == 'committed'" in dispatch["if"]
    assert "steps.manual_outcome_artifact.outcome == 'success'" in dispatch["if"]
    assert (
        "neither the initial nor retry publication push succeeded"
        in enforce_push["run"]
    )
    assert "committed|no_change" in enforce_manual["run"]
    assert "OUTCOME_ARTIFACT_STEP" in enforce_manual["env"]


def test_embedded_receipt_program_compiles() -> None:
    compile(_receipt_source(), str(WORKFLOW), "exec")
