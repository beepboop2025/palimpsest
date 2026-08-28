"""Contracts for the bounded first-production Newswire activation transaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from core.newswire import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "ops" / "railway" / "run-newswire-prerequisite.sh"
DEPLOY_GUIDE = ROOT / "ops" / "DEPLOY-HETZNER.md"


def _script() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def _python_heredoc_after(marker: str, occurrence: int = 1) -> str:
    script = _script()
    start = -1
    for _ in range(occurrence):
        start = script.index(marker, start + 1)
    source_start = script.index("<<'PY'\n", start) + len("<<'PY'\n")
    source_end = script.index("\nPY", source_start)
    return script[source_start:source_end]


def _bash_function(name: str) -> str:
    script = _script()
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _run_python(source: str, *arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source, *(str(argument) for argument in arguments)],
        text=True,
        capture_output=True,
        check=False,
    )


def _first_activation_block() -> str:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    marker = "### First Newswire prerequisite and Railway activation canary"
    section = guide.index(marker)
    start = guide.index("```bash\n", section) + len("```bash\n")
    end = guide.index("\n```", start)
    return guide[start:end]


def test_first_activation_runbook_is_parseable_and_receipt_chained() -> None:
    block = _first_activation_block()
    parsed = subprocess.run(
        ["/bin/bash", "-n"],
        input=block,
        text=True,
        capture_output=True,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stderr

    base = block.index('EXPECTED_NEWSWIRE_BASE_SHA="$(git rev-parse HEAD)"')
    newswire = block.index("ops/railway/run-newswire-prerequisite.sh", base)
    receipt_parse = block.index('EXPECTED_CANARY_SHA="$(python3', newswire)
    receipt_export = block.index(
        'ACTIVATION_CANARY_RECEIPT="${FIRST_ACTIVATION_EVIDENCE_DIR}',
        receipt_parse,
    )
    canary = block.index(
        "ACTIVATION_CANARY=true FORCE=false ops/railway/run-activation-canary",
        receipt_export,
    )
    canary_parse = block.index('FIRST_CANARY_SOURCE_SHA="$(python3', canary)
    equality = block.index(
        'test "$FIRST_CANARY_SOURCE_SHA" = "$EXPECTED_CANARY_SHA"',
        canary_parse,
    )
    assert base < newswire < receipt_parse < receipt_export < canary < canary_parse
    assert canary_parse < equality
    assert "NEWSWIRE_PREREQUISITE_RECEIPT" in block[receipt_parse:canary]
    assert "palimpsest.newswire-activation-prerequisite.v1" in block
    assert "palimpsest.railway-activation-canary-receipt.v1" in block
    assert "RAILWAY_PUBLICATION_ENABLED=true" not in block


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def test_newswire_prerequisite_is_syntactically_valid_and_fail_closed() -> None:
    script = _script()
    parsed = subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT_PATH)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stderr
    for marker in (
        "RAILWAY_PUBLICATION_ENABLED --body false",
        "RAILWAY_EXCLUSIVE_WRITER_ACK",
        "refusing to delete an unfamiliar Railway writer acknowledgement",
        "restore_newswire_workflow_freeze",
        "private_directory_is_owned_0700",
        "start_new_session=True",
        "os.killpg(process.pid, signal.SIGTERM)",
        "os.killpg(process.pid, signal.SIGKILL)",
        "refusing Newswire activation inside the minute-17 schedule window",
        "a Newswire run is already active",
        "a scheduled Newswire run raced the controlled dispatch",
        "unexpected push Tests run exists for the Action-token commit",
        "palimpsest.newswire-activation-prerequisite.v1",
        "two-hour activation window",
        "os.O_EXCL",
        "NEWSWIRE_TRANSACTION_COMPLETE == 1",
        "begin_newswire_receipt_commit",
        "finish_newswire_receipt_commit",
        "Newswire receipt is committed; post-commit cleanup needs manual attention",
        "Newswire receipt committed; stdout reporting failed",
    ):
        assert marker in script
    assert "gh run cancel" not in script
    assert "RAILWAY_PUBLICATION_ENABLED --body true" not in script
    assert "--ref main" in script


def test_committed_receipt_makes_cleanup_failure_warning_only() -> None:
    cleanup = _bash_function("cleanup_newswire_prerequisite")
    harness = f"""
NEWSWIRE_TRANSACTION_COMPLETE=1
NEWSWIRE_TMP_DIR=/private/tmp/newswire-committed-test
restore_newswire_workflow_freeze() {{ return 0; }}
private_directory_is_owned_0700() {{ return 0; }}
rm() {{ return 1; }}
clear_railway_writer_authority_on_failure() {{
  printf 'authority cleanup must not run after receipt commit\\n' >&2
  return 1
}}
{cleanup}
cleanup_newswire_prerequisite 0
"""
    result = subprocess.run(
        ["/bin/bash"],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "post-commit cleanup needs manual attention" in result.stderr
    assert "authority cleanup must not run" not in result.stderr


def test_committed_receipt_stdout_reporting_is_non_authoritative() -> None:
    script = _script()
    committed = script.index("NEWSWIRE_TRANSACTION_COMPLETE=1")
    first_report = script.index(
        "if ! printf 'NEWSWIRE_PUBLICATION_SHA=%s\\n'", committed
    )
    second_report = script.index(
        "if ! printf 'NEWSWIRE_PREREQUISITE_RECEIPT=%s\\n'", first_report
    )
    terminal_success = script.index("exit 0", second_report)
    assert committed < first_report < second_report < terminal_success


def test_term_during_receipt_commit_cannot_split_receipt_and_flag(
    tmp_path: Path,
) -> None:
    begin = _bash_function("begin_newswire_receipt_commit")
    restore = _bash_function("restore_newswire_receipt_signal_handlers")
    finish = _bash_function("finish_newswire_receipt_commit")
    receipt = tmp_path / "committed.json"
    harness = f"""
NEWSWIRE_TRANSACTION_COMPLETE=0
cleanup_newswire_prerequisite() {{ exit 99; }}
{begin}
{restore}
{finish}
begin_newswire_receipt_commit
printf '{{}}\\n' > {receipt!s}
kill -TERM $$
finish_newswire_receipt_commit
trap - HUP INT TERM
test "$NEWSWIRE_TRANSACTION_COMPLETE" = 1
test -f {receipt!s}
"""
    result = subprocess.run(
        ["/bin/bash"],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_newswire_enable_dispatch_disable_precedes_run_acceptance() -> None:
    script = _script()
    base_push = script.index('BASE_PUSH_RUN_ID="$(python3')
    snapshot_contract = script.index('>"$CONTRACT_RUNS_BEFORE"', base_push)
    arm = script.index("NEWSWIRE_WORKFLOW_RESTORE_DISABLED=1", snapshot_contract)
    enable = script.index('workflow enable "$NEWSWIRE_WORKFLOW"', arm)
    active = script.index('test "$(newswire_workflow_state)" = active', enable)
    dispatch = script.index('workflow run "$NEWSWIRE_WORKFLOW"', active)
    disable = script.index("restore_newswire_workflow_freeze", dispatch)
    frozen = script.index(
        'test "$(newswire_workflow_state)" = disabled_manually', disable
    )
    select = script.index('NEWSWIRE_RUN_ID="$(python3', frozen)
    watch = script.index('run watch "$NEWSWIRE_RUN_ID"', select)
    late_race_recheck = script.index(
        "Newswire run set changed after controlled selection", watch
    )
    descendant = script.index(
        'NEWSWIRE_PUBLICATION_SHA="$(bounded_gh', late_race_recheck
    )
    contract_select = script.index('PUBLICATION_CONTRACT_RUN_ID="$(python3', descendant)
    contract_watch = script.index(
        'run watch "$PUBLICATION_CONTRACT_RUN_ID"', contract_select
    )
    job_proof = script.index('"Package exact complete Pages edition": "success"')
    raw_validation = script.index(
        'python3 - "$NEWSWIRE_JSON" "$SITUATION_JSON"', job_proof
    )
    receipt = script.index(
        '"schema_version": "palimpsest.newswire-activation-prerequisite.v1"',
        raw_validation,
    )
    assert (
        base_push
        < snapshot_contract
        < arm
        < enable
        < active
        < dispatch
        < disable
        < frozen
        < select
        < watch
        < late_race_recheck
        < descendant
        < contract_select
        < contract_watch
        < job_proof
        < raw_validation
        < receipt
    )


def test_newswire_selector_rejects_schedule_races_and_multiple_runs(
    tmp_path: Path,
) -> None:
    source = _python_heredoc_after('NEWSWIRE_RUN_ID="$(python3')
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    base_sha = "a" * 40
    before = [
        {
            "databaseId": 10,
            "event": "schedule",
            "headSha": "0" * 40,
            "workflowName": "Refresh evidence wire",
        }
    ]
    _write_json(before_path, before)
    manual = {
        "databaseId": 11,
        "event": "workflow_dispatch",
        "headSha": base_sha,
        "workflowName": "Refresh evidence wire",
    }
    _write_json(after_path, [*before, manual])
    valid = _run_python(source, before_path, after_path, base_sha)
    assert valid.returncode == 0, valid.stderr
    assert valid.stdout.strip() == "11"

    schedule = {
        "databaseId": 12,
        "event": "schedule",
        "headSha": base_sha,
        "workflowName": "Refresh evidence wire",
    }
    _write_json(after_path, [*before, manual, schedule])
    raced = _run_python(source, before_path, after_path, base_sha)
    assert raced.returncode != 0
    assert "scheduled Newswire run raced" in raced.stderr

    second_manual = {**manual, "databaseId": 13}
    _write_json(after_path, [*before, manual, second_manual])
    duplicate = _run_python(source, before_path, after_path, base_sha)
    assert duplicate.returncode != 0
    assert "more than one new Newswire run" in duplicate.stderr


def test_publication_contract_selector_is_exact(tmp_path: Path) -> None:
    source = _python_heredoc_after('PUBLICATION_CONTRACT_RUN_ID="$(python3')
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    publication_sha = "b" * 40
    before = [
        {
            "databaseId": 20,
            "event": "repository_dispatch",
            "headSha": "0" * 40,
            "workflowName": "Tests",
        }
    ]
    _write_json(before_path, before)
    expected = {
        "databaseId": 21,
        "event": "repository_dispatch",
        "headSha": publication_sha,
        "workflowName": "Tests",
    }
    unrelated = {
        "databaseId": 22,
        "event": "repository_dispatch",
        "headSha": "c" * 40,
        "workflowName": "Tests",
    }
    _write_json(after_path, [*before, expected, unrelated])
    valid = _run_python(source, before_path, after_path, publication_sha)
    assert valid.returncode == 0, valid.stderr
    assert valid.stdout.strip() == "21"

    _write_json(after_path, [*before, expected, {**expected, "databaseId": 23}])
    duplicate = _run_python(source, before_path, after_path, publication_sha)
    assert duplicate.returncode != 0
    assert "more than one publication contract" in duplicate.stderr


def test_newswire_commit_proof_requires_direct_bot_child_inside_run(
    tmp_path: Path,
) -> None:
    source = _python_heredoc_after('python3 - "$NEWSWIRE_COMMIT_JSON"')
    commit_path = tmp_path / "commit.json"
    run_path = tmp_path / "run.json"
    proof_path = tmp_path / "proof.json"
    metadata_paths = [tmp_path / f"metadata-{index}.json" for index in range(4)]
    base_sha = "a" * 40
    publication_sha = "b" * 40
    stamp = datetime.now(timezone.utc).replace(microsecond=0)
    stamp_text = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    commit = {
        "sha": publication_sha,
        "parents": [{"sha": base_sha}],
        "commit": {
            "message": f"data: evidence wire refresh ({stamp_text}) [skip pytest]",
            "author": {
                "name": "palimpsest-bot",
                "email": "bot@palimpsest.info",
                "date": stamp_text,
            },
            "committer": {
                "name": "palimpsest-bot",
                "email": "bot@palimpsest.info",
                "date": stamp_text,
            },
        },
    }
    run = {
        "startedAt": (stamp - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updatedAt": (stamp + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_json(commit_path, commit)
    _write_json(run_path, run)
    for index, path in enumerate(metadata_paths):
        _write_json(path, {"type": "file", "sha": f"{index + 1:x}" * 40, "size": 100})
    valid = _run_python(
        source,
        commit_path,
        run_path,
        *metadata_paths,
        proof_path,
        base_sha,
        publication_sha,
    )
    assert valid.returncode == 0, valid.stderr
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert (
        proof["blobs"]["newswire"]["before_sha"]
        != proof["blobs"]["newswire"]["after_sha"]
    )

    commit["parents"] = [{"sha": "c" * 40}]
    _write_json(commit_path, commit)
    wrong_parent = _run_python(
        source,
        commit_path,
        run_path,
        *metadata_paths,
        proof_path,
        base_sha,
        publication_sha,
    )
    assert wrong_parent.returncode != 0
    assert "one direct child" in wrong_parent.stderr

    commit["parents"] = [{"sha": base_sha}]
    commit["commit"]["author"]["email"] = "someone@example.com"
    _write_json(commit_path, commit)
    wrong_author = _run_python(
        source,
        commit_path,
        run_path,
        *metadata_paths,
        proof_path,
        base_sha,
        publication_sha,
    )
    assert wrong_author.returncode != 0
    assert "publication bot" in wrong_author.stderr


def test_acquisition_artifact_is_run_bound_and_archive_digest_checked(
    tmp_path: Path,
) -> None:
    inventory_source = _python_heredoc_after('python3 - "$NEWSWIRE_ARTIFACTS_JSON"')
    zip_source = _python_heredoc_after('python3 - "$NEWSWIRE_ARTIFACT_ZIP"')
    inventory_path = tmp_path / "artifacts.json"
    proof_path = tmp_path / "artifact-proof.json"
    archive_path = tmp_path / "artifact.zip"
    latest_path = tmp_path / "latest.json"
    versions_path = tmp_path / "versions.jsonl"
    base_sha = "d" * 40
    run_id = 123
    attempt = 1
    name = f"newswire-acquisition-{base_sha}-{run_id}-{attempt}"
    artifact = {
        "id": 456,
        "name": name,
        "expired": False,
        "size_in_bytes": 2048,
        "digest": "sha256:" + "0" * 64,
        "workflow_run": {"id": run_id, "head_branch": "main", "head_sha": base_sha},
    }
    _write_json(inventory_path, {"artifacts": [artifact]})
    valid_inventory = _run_python(
        inventory_source,
        inventory_path,
        proof_path,
        base_sha,
        run_id,
        attempt,
    )
    assert valid_inventory.returncode == 0, valid_inventory.stderr

    artifact["expired"] = True
    _write_json(inventory_path, {"artifacts": [artifact]})
    expired = _run_python(
        inventory_source,
        inventory_path,
        proof_path,
        base_sha,
        run_id,
        attempt,
    )
    assert expired.returncode != 0
    assert "artifact identity is invalid" in expired.stderr

    with zipfile.ZipFile(archive_path, "w") as bundle:
        bundle.writestr("readings/newswire-latest.json", b"{}\n")
        bundle.writestr("readings/newswire-versions.jsonl", b"{}\n")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    valid_zip = _run_python(
        zip_source,
        archive_path,
        digest,
        latest_path,
        versions_path,
    )
    assert valid_zip.returncode == 0, valid_zip.stderr
    assert latest_path.read_bytes() == b"{}\n"
    assert versions_path.read_bytes() == b"{}\n"

    bad_digest = _run_python(
        zip_source,
        archive_path,
        "f" * 64,
        latest_path,
        versions_path,
    )
    assert bad_digest.returncode != 0
    assert "archive digest is invalid" in bad_digest.stderr


def test_raw_git_documents_are_validated_fresh_and_lineage_bound(
    tmp_path: Path,
) -> None:
    source = _python_heredoc_after('python3 - "$NEWSWIRE_JSON" "$SITUATION_JSON"')
    newswire_path = tmp_path / "newswire.json"
    situation_path = tmp_path / "situation.json"
    receipt_path = tmp_path / "receipt.json"
    commit_proof_path = tmp_path / "commit-proof.json"
    artifact_proof_path = tmp_path / "artifact-proof.json"
    newswire = json.loads(
        (ROOT / "readings" / "newswire-latest.json").read_text(encoding="utf-8")
    )
    situation = json.loads(
        (ROOT / "readings" / "china-situation-latest.json").read_text(encoding="utf-8")
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    original_generated = datetime.strptime(
        newswire["generated_at"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    fresh_offset_hours = int((now - original_generated).total_seconds() // 3600)
    fresh = original_generated + timedelta(hours=fresh_offset_hours)
    fresh_text = fresh.strftime("%Y-%m-%dT%H:%M:%SZ")
    newswire["generated_at"] = fresh_text
    newswire["window"]["to"] = fresh_text
    newswire["window"]["hours"] += fresh_offset_hours
    for item in newswire["items"]:
        item["collected_at"] = fresh_text
    situation["generated_at"] = fresh_text
    situation["inputs"]["newswire_generated_at"] = fresh_text
    situation["inputs"]["newswire_sha256"] = hashlib.sha256(
        canonical_json_bytes(newswire)
    ).hexdigest()
    _write_json(newswire_path, newswire)
    _write_json(situation_path, situation)
    _write_json(
        commit_proof_path,
        {
            "blobs": {
                "newswire": {"before_sha": "1" * 40, "after_sha": "2" * 40},
                "situation": {"before_sha": "3" * 40, "after_sha": "4" * 40},
            },
            "commit_at": fresh_text,
            "run_completed_at": fresh_text,
            "run_started_at": fresh_text,
        },
    )
    _write_json(
        artifact_proof_path,
        {
            "digest": "5" * 64,
            "id": 103,
            "name": f"newswire-acquisition-{'d' * 40}-101-1",
            "size_in_bytes": 2048,
        },
    )

    arguments = (
        newswire_path,
        situation_path,
        receipt_path,
        "beepboop2025/palimpsest",
        "d" * 40,
        100,
        101,
        1,
        "e" * 40,
        102,
        1,
        commit_proof_path,
        artifact_proof_path,
    )
    valid = _run_python(source, *arguments)
    assert valid.returncode == 0, valid.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["base_sha"] == "d" * 40
    assert receipt["publication_sha"] == "e" * 40
    assert (
        receipt["newswire"]["canonical_sha256"]
        == situation["inputs"]["newswire_sha256"]
    )
    assert (
        receipt["situation"]["canonical_sha256"]
        == hashlib.sha256(canonical_json_bytes(situation)).hexdigest()
    )
    assert receipt["situation"]["inputs"] == {
        "newswire_canonical_sha256": receipt["newswire"]["canonical_sha256"],
        "newswire_generated_at": receipt["newswire"]["generated_at"],
    }
    assert receipt["hourly_publication_enabled"] is False
    assert receipt["workflow_state"] == "disabled_manually"

    situation["inputs"]["newswire_sha256"] = "0" * 64
    _write_json(situation_path, situation)
    receipt_path.unlink()
    tampered = _run_python(source, *arguments)
    assert tampered.returncode != 0
    assert "digest does not match" in tampered.stderr

    stale = fresh - timedelta(hours=3)
    stale_text = stale.strftime("%Y-%m-%dT%H:%M:%SZ")
    newswire["generated_at"] = stale_text
    newswire["window"]["to"] = stale_text
    newswire["window"]["hours"] -= 3
    for item in newswire["items"]:
        item["collected_at"] = stale_text
    situation["generated_at"] = stale_text
    situation["inputs"]["newswire_generated_at"] = stale_text
    situation["inputs"]["newswire_sha256"] = hashlib.sha256(
        canonical_json_bytes(newswire)
    ).hexdigest()
    _write_json(newswire_path, newswire)
    _write_json(situation_path, situation)
    stale_result = _run_python(source, *arguments)
    assert stale_result.returncode != 0
    assert "two-hour activation window" in stale_result.stderr
