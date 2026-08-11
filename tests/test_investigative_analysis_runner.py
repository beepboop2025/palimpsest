from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from core.investigative_candidates import build_candidates, canonical_json_bytes
from ops import investigative_analysis_runner as runner


COMMIT = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    readings = tmp_path / "readings"
    wire = tmp_path / "newswire"
    commit = tmp_path / "deployed-commit"
    _write(readings / "ooni-gfw-latest.json", {"generated_at": "2026-08-11T18:00:00Z"})
    (readings / "ooni-gfw-history.jsonl").write_text(
        '{"generated_at":"2026-08-11T18:00:00Z","value":1}\n', encoding="utf-8"
    )
    _write(
        readings / "board-alarm-latest.json", {"generated_at": "2026-08-11T17:00:00Z"}
    )
    _write(wire / "newswire-latest.json", {"generated_at": "2026-08-11T18:00:00Z"})
    (wire / "newswire-versions.jsonl").write_text(
        '{"version_id":"eventv-000000000000000000000000"}\n', encoding="utf-8"
    )
    commit.write_text(COMMIT + "\n", encoding="ascii")
    return readings, wire, commit


def _complete_fake_container(command: list[str]) -> None:
    volumes = [
        command[index + 1] for index, value in enumerate(command) if value == "--volume"
    ]
    staged = Path(
        next(
            value.split(":", 1)[0]
            for value in volumes
            if value.endswith(":/app/readings:rw")
        )
    )
    candidate_dir = Path(
        next(
            value.split(":", 1)[0]
            for value in volumes
            if value.endswith(":/app/private:rw")
        )
    )
    generated_at = command[command.index("--decision-clock") + 1]
    documents = {
        "vantage-fusion-latest.json": {
            "generated_at": generated_at,
            "ok": False,
            "reason": "no aligned vantages",
        },
        "event-flags-latest.json": {"generated_at": generated_at, "active": []},
        "coverage-guard-latest.json": {
            "generated_at": generated_at,
            "confounded": [],
        },
        "board-alarm-latest.json": {
            "generated_at": generated_at,
            "fdr_selection": {"selected": []},
        },
        "cross-layer-latest.json": {"generated_at": generated_at, "pairs": []},
        "forecast-ledger-latest.json": {"generated_at": generated_at},
        "china-economic-pulse-latest.json": {
            "generated_at": generated_at,
            "economic_state": {"status": "warming_up"},
            "coverage": {"adapter_ready_sources": []},
        },
        "osint-china-latest.json": {"generated_at": generated_at},
        "investigations-latest.json": {"generated_at": generated_at},
    }
    for name, value in documents.items():
        _write(staged / name, value)

    candidate = build_candidates(staged)
    candidate_dir.mkdir(exist_ok=True)
    (candidate_dir / "candidates-latest.json").write_bytes(
        canonical_json_bytes(candidate)
    )
    outputs = []
    for name in runner.DERIVED_LATEST:
        raw = (staged / name).read_bytes()
        outputs.append(
            {
                "path": f"readings/{name}",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    input_commit = command[command.index("--input-commit") + 1]
    _write(
        staged / "analysis-run-manifest.json",
        {
            "schema_version": runner.RUN_SCHEMA,
            "completed_at": generated_at,
            "input_commit": input_commit,
            "decision_clock": generated_at,
            "network_policy": "docker-network-none",
            "publication_policy": "private-review-only",
            "steps": list(runner.RUN_STEPS),
            "candidate_edition_id": candidate["edition_id"],
            "candidate_input_fingerprint": candidate["input_fingerprint"],
            "candidate_count": candidate["n_candidates"],
            "outputs": outputs,
        },
    )


def test_snapshot_excludes_live_derived_output_and_carries_baseline(
    tmp_path: Path,
) -> None:
    readings, wire, _commit = _inputs(tmp_path)
    previous = tmp_path / "previous"
    _write(
        previous / "board-alarm-latest.json", {"generated_at": "2026-08-11T17:30:00Z"}
    )
    destination = tmp_path / "stage" / "readings"

    fingerprint, lineage_fingerprint, manifest, decision_clock = runner.snapshot_inputs(
        readings_dir=readings,
        newswire_dir=wire,
        staging_readings=destination,
        previous_readings=previous,
    )

    assert len(fingerprint) == 64
    assert len(lineage_fingerprint) == 64
    assert decision_clock == "2026-08-11T18:00:00Z"
    assert {row["path"] for row in manifest} == {
        "readings/ooni-gfw-latest.json",
        "readings/ooni-gfw-history.jsonl",
        "readings/newswire-latest.json",
        "readings/newswire-versions.jsonl",
        "readings/board-alarm-latest.json",
    }
    assert (
        json.loads((destination / "board-alarm-latest.json").read_text())[
            "generated_at"
        ]
        == "2026-08-11T17:30:00Z"
    )


def test_snapshot_includes_gfi_and_separates_trigger_from_lineage(
    tmp_path: Path,
) -> None:
    readings, wire, _commit = _inputs(tmp_path)
    _write(readings / "latest.json", {"generated_at": "2026-08-11T17:59:00Z"})
    first_baseline = tmp_path / "baseline-one"
    second_baseline = tmp_path / "baseline-two"
    _write(
        first_baseline / "board-alarm-latest.json",
        {"generated_at": "2026-08-11T17:00:00Z"},
    )
    _write(
        second_baseline / "board-alarm-latest.json",
        {"generated_at": "2026-08-11T17:30:00Z"},
    )

    trigger_one, lineage_one, manifest, _clock = runner.snapshot_inputs(
        readings_dir=readings,
        newswire_dir=wire,
        staging_readings=tmp_path / "stage-one",
        previous_readings=first_baseline,
    )
    trigger_two, lineage_two, _manifest, _clock = runner.snapshot_inputs(
        readings_dir=readings,
        newswire_dir=wire,
        staging_readings=tmp_path / "stage-two",
        previous_readings=second_baseline,
    )

    assert "readings/latest.json" in {row["path"] for row in manifest}
    assert trigger_one == trigger_two
    assert lineage_one != lineage_two


def test_snapshot_rejects_future_source_clock(tmp_path: Path) -> None:
    readings, wire, _commit = _inputs(tmp_path)
    _write(readings / "ooni-gfw-latest.json", {"generated_at": "2999-01-01T00:00:00Z"})

    with pytest.raises(runner.AnalysisRunnerError, match="future decision clock"):
        runner.snapshot_inputs(
            readings_dir=readings,
            newswire_dir=wire,
            staging_readings=tmp_path / "stage",
        )


def test_snapshot_rejects_duplicate_json_and_symlinks(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"x":1,"x":2}\n', encoding="utf-8")
    with pytest.raises(runner.AnalysisRunnerError, match="duplicate"):
        runner._stable_read(bad)

    target = tmp_path / "target.json"
    _write(target, {"x": 1})
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(runner.AnalysisRunnerError, match="cannot stat"):
        runner._stable_read(link)


def test_docker_command_has_hard_network_and_privilege_boundaries(
    tmp_path: Path,
) -> None:
    command = runner.docker_command(
        image_id=IMAGE_ID,
        frozen_readings=tmp_path / "run" / "inputs",
        staged_readings=tmp_path / "run" / "readings",
        candidate_dir=tmp_path / "run" / "private",
        cidfile=tmp_path / "run" / "container.cid",
        input_commit=COMMIT,
        decision_clock="2026-08-11T18:00:00Z",
    )
    assert command[0:3] == ["/usr/bin/docker", "run", "--rm"]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--user") + 1] == "10001:10001"
    assert command[command.index("--entrypoint") + 1] == "/usr/local/bin/python3"
    assert command[command.index("--name") + 1] == runner.CONTAINER_NAME
    assert f"{tmp_path / 'run' / 'inputs'}:/app/frozen:ro" in command
    assert IMAGE_ID in command
    assert "sh" not in command and "bash" not in command
    assert all(
        "/var/lib/palimpsest/readings:/app/readings" not in part for part in command
    )


def test_run_is_idempotent_and_promotes_only_after_container_success(
    tmp_path: Path,
) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"
    calls: list[list[str]] = []
    image_revision = COMMIT

    def execute(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0, f"{image_revision} {IMAGE_ID}\n", ""
            )
        calls.append(command)
        _complete_fake_container(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    first = runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
        execute=execute,
    )
    second = runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
        execute=execute,
    )
    third = runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
        execute=execute,
    )

    assert first["status"] == "completed"
    assert second["status"] == "unchanged"
    assert third["status"] == "unchanged"
    assert len(calls) == 1
    state = json.loads((private / "state.json").read_text())
    assert Path(state["run_path"]).is_dir()
    assert state["image_id"] == IMAGE_ID
    assert not list(runs.glob(".staging-*"))

    commit.write_text("d" * 40 + "\n", encoding="ascii")
    image_revision = "d" * 40
    after_deploy = runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
        execute=execute,
    )
    assert after_deploy["status"] == "completed"
    assert len(calls) == 2


def test_container_cannot_mutate_frozen_evidence_or_rewrite_stale_outputs(
    tmp_path: Path,
) -> None:
    readings, wire, commit = _inputs(tmp_path)

    def mutate_input(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        frozen = Path(
            next(
                value.split(":", 1)[0]
                for index, value in enumerate(command)
                if value == "--volume"
                for value in [command[index + 1]]
                if value.endswith(":/app/frozen:ro")
            )
        )
        _write(
            frozen / "ooni-gfw-latest.json",
            {"generated_at": "2026-08-11T18:00:00Z", "mutated": True},
        )
        _complete_fake_container(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    with pytest.raises(runner.AnalysisRunnerError, match="mutated frozen input"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=tmp_path / "runs-mutated",
            private_root=tmp_path / "private-mutated",
            commit_file=commit,
            execute=mutate_input,
        )

    def stale_output(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        _complete_fake_container(command)
        staged = Path(
            next(
                value.split(":", 1)[0]
                for index, value in enumerate(command)
                if value == "--volume"
                for value in [command[index + 1]]
                if value.endswith(":/app/readings:rw")
            )
        )
        manifest = json.loads((staged / "analysis-run-manifest.json").read_text())
        _write(
            staged / "event-flags-latest.json",
            {"generated_at": "2020-01-01T00:00:00Z", "active": []},
        )
        raw = (staged / "event-flags-latest.json").read_bytes()
        receipt = next(
            row
            for row in manifest["outputs"]
            if row["path"].endswith("event-flags-latest.json")
        )
        receipt.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        _write(staged / "analysis-run-manifest.json", manifest)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    with pytest.raises(runner.AnalysisRunnerError, match="decision clock"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=tmp_path / "runs-stale",
            private_root=tmp_path / "private-stale",
            commit_file=commit,
            execute=stale_output,
        )


def test_failed_container_leaves_no_promoted_run_or_state(tmp_path: Path) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"

    def fail(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        return subprocess.CompletedProcess(command, 9, "", "boom")

    with pytest.raises(runner.AnalysisRunnerError, match="status 9"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=runs,
            private_root=private,
            commit_file=commit,
            execute=fail,
        )
    assert not (private / "state.json").exists()
    assert not list(runs.glob("run-*"))
    assert not list(runs.glob(".staging-*"))


def test_runner_refuses_an_image_built_from_a_different_commit(tmp_path: Path) -> None:
    readings, wire, commit = _inputs(tmp_path)

    def stale_image(command, **_kwargs):
        assert command[1:3] == ["image", "inspect"]
        return subprocess.CompletedProcess(command, 0, f"{'e' * 40} {IMAGE_ID}\n", "")

    with pytest.raises(runner.AnalysisRunnerError, match="not built from"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=tmp_path / "runs",
            private_root=tmp_path / "private",
            commit_file=commit,
            execute=stale_image,
        )

    assert not (tmp_path / "runs").exists()


def test_timeout_force_removes_the_exact_started_container(tmp_path: Path) -> None:
    readings, wire, commit = _inputs(tmp_path)
    removed: list[str] = []

    def timeout(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        if command[1:3] == ["rm", "--force"]:
            removed.append(command[-1])
            return subprocess.CompletedProcess(command, 0, "", "")
        cidfile = Path(command[command.index("--cidfile") + 1])
        cidfile.write_text("c" * 64 + "\n", encoding="ascii")
        raise subprocess.TimeoutExpired(command, 1200)

    with pytest.raises(runner.AnalysisRunnerError, match="20 minute"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=tmp_path / "runs",
            private_root=tmp_path / "private",
            commit_file=commit,
            execute=timeout,
        )

    assert removed == [runner.CONTAINER_NAME]
    assert not list((tmp_path / "runs").glob(".staging-*"))


def test_post_commit_ledger_failure_repairs_without_reanalysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"
    container_runs = 0

    def execute(command, **_kwargs):
        nonlocal container_runs
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        container_runs += 1
        _complete_fake_container(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    real_publish = runner.publish_private_candidates

    def fail_publish(*_args, **_kwargs):
        raise OSError("injected ledger failure after state commit")

    monkeypatch.setattr(runner, "publish_private_candidates", fail_publish)
    with pytest.raises(OSError, match="injected ledger failure"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=runs,
            private_root=private,
            commit_file=commit,
            execute=execute,
        )

    assert (private / "state.json").is_file()
    assert len(list(runs.glob("run-*"))) == 1
    assert not (private / "ledger" / "candidates-latest.json").exists()

    monkeypatch.setattr(runner, "publish_private_candidates", real_publish)
    repaired = runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
        execute=execute,
    )

    assert repaired["status"] == "unchanged"
    assert container_runs == 1
    assert (private / "ledger" / "candidates-latest.json").is_file()
    assert (private / "ledger" / "candidate-versions.jsonl").is_file()


def test_node_wide_lease_rejects_an_overlapping_manual_run(tmp_path: Path) -> None:
    readings, wire, commit = _inputs(tmp_path)
    private = tmp_path / "private"
    private.mkdir()

    with runner._exclusive_lock(private / "cascade.lock"):
        with pytest.raises(runner.AnalysisRunnerError, match="already running"):
            runner.run_once(
                readings_dir=readings,
                newswire_dir=wire,
                runs_dir=tmp_path / "runs",
                private_root=private,
                commit_file=commit,
            )

    assert (private / "cascade.lock").stat().st_mode & 0o777 == 0o600


def test_systemd_contract_keeps_analysis_private_and_recurring() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (
        root / "ops/systemd/palimpsest-investigative-analysis.service"
    ).read_text()
    timer = (root / "ops/systemd/palimpsest-investigative-analysis.timer").read_text()
    source = (root / "ops/investigative_analysis_runner.py").read_text()

    assert "/usr/local/libexec/palimpsest-analysis" in service
    assert "/var/lib/palimpsest/readings /var/lib/palimpsest/newswire" in service
    assert (
        "ReadWritePaths=/var/lib/palimpsest-analysis/runs "
        "/var/lib/palimpsest-analysis/private" in service
    )
    assert "User=10001" in service and "Group=10001" in service
    assert "SupplementaryGroups=docker" in service
    assert "TimeoutStartSec=35m" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "OnCalendar=*:15,45" in timer
    assert '"--network"' in source and '"none"' in source
    assert 'private_root / "cascade.lock"' in source
    assert "private-review-only" in source
