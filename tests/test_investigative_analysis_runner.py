from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.analytical_pieces import build_packet_set, build_template_draft_set
from core.investigative_candidates import build_candidates, canonical_json_bytes
from core.wire_claim_audits import (
    DELIVERY_POLICY as WIRE_DELIVERY_POLICY,
    build_wire_claim_audits,
    canonical_json_bytes as wire_canonical_json_bytes,
)
from ops import investigative_analysis_runner as runner

COMMIT = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _content_id(prefix: str, value: dict, length: int) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value).rstrip(b"\n")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _write_wire_status(
    wire: Path,
    *,
    completed_at: datetime | None = None,
    status: str = "success",
    fresh_sources: int = 1,
) -> None:
    completed_at = (completed_at or datetime.now(timezone.utc)).replace(microsecond=0)
    completed = completed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    attempted = (
        (completed_at - timedelta(seconds=1))
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    latest_raw = (wire / "newswire-latest.json").read_bytes()
    latest = json.loads(latest_raw)
    _write(
        wire / runner.WIRE_STATUS_NAME,
        {
            "schema_version": runner.WIRE_STATUS_SCHEMA,
            "attempted_at": attempted,
            "completed_at": completed,
            "status": status,
            "fresh_sources": fresh_sources,
            "output_generated_at": latest["generated_at"],
            "output_sha256": hashlib.sha256(latest_raw).hexdigest(),
            "failure_class": None if status == "success" else "NoFreshSources",
        },
    )


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
    _write(
        wire / "newswire-latest.json",
        {
            "schema_version": "palimpsest-newswire.v1",
            "generated_at": "2026-08-11T18:00:00Z",
            "events": [],
        },
    )
    _write_wire_status(wire)
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
    frozen = Path(
        next(
            value.split(":", 1)[0]
            for value in volumes
            if value.endswith(":/app/frozen:ro")
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
    for source in frozen.iterdir():
        if source.is_file():
            shutil.copyfile(source, staged / source.name)
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
        "osint-china-latest.json": {
            "schema_version": "osint-china.v1",
            "generated_at": generated_at,
            "signals": [],
        },
        "investigations-latest.json": {"generated_at": generated_at},
    }
    for name, value in documents.items():
        _write(staged / name, value)

    candidate = build_candidates(staged)
    candidate_dir.mkdir(exist_ok=True)
    (candidate_dir / "candidates-latest.json").write_bytes(
        canonical_json_bytes(candidate)
    )
    packets = build_packet_set(candidate)
    drafts = build_template_draft_set(packets)
    (candidate_dir / "analytical-packets-latest.json").write_bytes(
        canonical_json_bytes(packets)
    )
    (candidate_dir / "analytical-drafts-latest.json").write_bytes(
        canonical_json_bytes(drafts)
    )
    audits = build_wire_claim_audits(
        staged,
        decision_clock=datetime.fromisoformat(generated_at.replace("Z", "+00:00")),
    )
    (candidate_dir / "wire-claim-audits-latest.json").write_bytes(
        wire_canonical_json_bytes(audits)
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
            "analytical_packet_edition_id": packets["edition_id"],
            "analytical_packet_count": packets["n_packets"],
            "analytical_draft_edition_id": drafts["edition_id"],
            "analytical_draft_count": drafts["n_drafts"],
            "wire_claim_audit_edition_id": audits["edition_id"],
            "wire_claim_audit_count": audits["n_audits"],
            "wire_claim_audit_brief_eligible_count": sum(
                audit["brief_eligible"] for audit in audits["audits"]
            ),
            "wire_delivery_policy": WIRE_DELIVERY_POLICY,
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


def test_snapshot_rejects_stale_failed_and_unbound_wire_receipts(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    readings, wire, _commit = _inputs(tmp_path / "stale")
    _write_wire_status(wire, completed_at=now - timedelta(minutes=76))
    with pytest.raises(runner.AnalysisRunnerError, match="older than 75 minutes"):
        runner.snapshot_inputs(
            readings_dir=readings,
            newswire_dir=wire,
            staging_readings=tmp_path / "stale-stage",
            now=now,
        )

    readings, wire, _commit = _inputs(tmp_path / "failed")
    _write_wire_status(
        wire, completed_at=now, status="no-fresh-sources", fresh_sources=0
    )
    with pytest.raises(runner.AnalysisRunnerError, match="not a successful fresh run"):
        runner.snapshot_inputs(
            readings_dir=readings,
            newswire_dir=wire,
            staging_readings=tmp_path / "failed-stage",
            now=now,
        )

    readings, wire, _commit = _inputs(tmp_path / "unbound")
    receipt_path = wire / runner.WIRE_STATUS_NAME
    receipt = json.loads(receipt_path.read_text())
    receipt["output_sha256"] = "0" * 64
    _write(receipt_path, receipt)
    with pytest.raises(runner.AnalysisRunnerError, match="does not match"):
        runner.snapshot_inputs(
            readings_dir=readings,
            newswire_dir=wire,
            staging_readings=tmp_path / "unbound-stage",
        )


def test_snapshot_rejects_running_wire_receipt(tmp_path: Path) -> None:
    readings, wire, _commit = _inputs(tmp_path)
    receipt_path = wire / runner.WIRE_STATUS_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.update(
        {
            "completed_at": None,
            "status": "running",
            "fresh_sources": None,
            "output_generated_at": None,
            "output_sha256": None,
            "failure_class": None,
        }
    )
    _write(receipt_path, receipt)

    with pytest.raises(runner.AnalysisRunnerError, match="not a successful fresh run"):
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
    delivery = tmp_path / "delivery"
    wire_projection = delivery / "wire-claim-audits-latest.json"
    assert wire_projection.is_file()
    assert stat.S_IMODE(delivery.stat().st_mode) == 0o711
    assert stat.S_IMODE(wire_projection.stat().st_mode) == 0o644
    assert not (private / "delivery").exists()
    receipt = json.loads((private / runner.ANALYSIS_STATUS_NAME).read_text())
    assert set(receipt) == {
        "schema_version",
        "attempted_at",
        "completed_at",
        "status",
        "decision_clock",
        "input_fingerprint",
        "failure_class",
    }
    assert receipt["schema_version"] == runner.ANALYSIS_STATUS_SCHEMA
    assert receipt["status"] == "unchanged"
    assert receipt["decision_clock"] == "2026-08-11T18:00:00Z"
    assert receipt["failure_class"] is None

    # A failed current wire attempt must stop the unchanged shortcut before a
    # container can be launched or stale success can be reported.
    _write_wire_status(wire, status="no-fresh-sources", fresh_sources=0)
    with pytest.raises(runner.AnalysisRunnerError, match="not a successful fresh run"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=runs,
            private_root=private,
            commit_file=commit,
            execute=execute,
        )
    assert len(calls) == 1
    failed_receipt_raw = (private / runner.ANALYSIS_STATUS_NAME).read_text()
    failed_receipt = json.loads(failed_receipt_raw)
    assert failed_receipt["status"] == "failed"
    assert failed_receipt["failure_class"] == "AnalysisRunnerError"
    assert "not a successful fresh run" not in failed_receipt_raw
    _write_wire_status(wire)

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


def test_valid_v1_state_forces_one_immutable_v3_upgrade(tmp_path: Path) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"
    calls: list[list[str]] = []

    def execute(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
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
    assert first["status"] == "completed"
    v2_state = json.loads((private / "state.json").read_text(encoding="utf-8"))
    old_run = Path(v2_state["run_path"])
    old_manifest_path = old_run / "readings" / "analysis-run-manifest.json"
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    old_manifest["schema_version"] = "palimpsest-investigative-analysis-run.v1"
    old_manifest["steps"] = [
        "vantage_fusion",
        "event_flags",
        "coverage_guard",
        "board_alarm",
        "cross_layer",
        "forecast_ledger",
        "economic_pulse",
        "osint_china",
        "investigations",
        "candidate_edition",
    ]
    for key in (
        "analytical_packet_edition_id",
        "analytical_packet_count",
        "analytical_draft_edition_id",
        "analytical_draft_count",
        "wire_claim_audit_edition_id",
        "wire_claim_audit_count",
        "wire_claim_audit_brief_eligible_count",
        "wire_delivery_policy",
    ):
        old_manifest.pop(key)
    _write(old_manifest_path, old_manifest)
    (old_run / "private" / "analytical-packets-latest.json").unlink()
    (old_run / "private" / "analytical-drafts-latest.json").unlink()
    (old_run / "private" / "wire-claim-audits-latest.json").unlink()
    for key in (
        "analytical_packet_edition_id",
        "analytical_packet_count",
        "analytical_draft_edition_id",
        "analytical_draft_count",
        "wire_claim_audit_edition_id",
        "wire_claim_audit_count",
        "wire_claim_audit_brief_eligible_count",
        "wire_delivery_policy",
    ):
        v2_state.pop(key)
    v2_state["schema_version"] = "palimpsest-investigative-analysis-state.v1"
    _write(private / "state.json", v2_state)
    old_bytes = {
        path.relative_to(old_run): path.read_bytes()
        for path in old_run.rglob("*")
        if path.is_file()
    }

    upgraded = runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
        execute=execute,
    )
    unchanged = runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
        execute=execute,
    )

    assert upgraded["status"] == "completed"
    assert unchanged["status"] == "unchanged"
    assert len(calls) == 2
    current = json.loads((private / "state.json").read_text(encoding="utf-8"))
    assert current["schema_version"] == runner.STATE_SCHEMA_V3
    assert Path(current["run_path"]) != old_run
    assert old_bytes == {
        path.relative_to(old_run): path.read_bytes()
        for path in old_run.rglob("*")
        if path.is_file()
    }


def test_v3_state_cannot_reference_a_v1_run(tmp_path: Path) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"

    def execute(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        _complete_fake_container(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
        execute=execute,
    )
    state = json.loads((private / "state.json").read_text(encoding="utf-8"))
    manifest_path = Path(state["run_path"]) / "readings" / "analysis-run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = runner.RUN_SCHEMA_V1
    manifest["steps"] = list(runner.LEGACY_RUN_STEPS)
    for key in (
        "analytical_packet_edition_id",
        "analytical_packet_count",
        "analytical_draft_edition_id",
        "analytical_draft_count",
        "wire_claim_audit_edition_id",
        "wire_claim_audit_count",
        "wire_claim_audit_brief_eligible_count",
        "wire_delivery_policy",
    ):
        manifest.pop(key)
    _write(manifest_path, manifest)

    with pytest.raises(
        runner.AnalysisRunnerError, match="required schema version"
    ):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=runs,
            private_root=private,
            commit_file=commit,
            execute=execute,
        )


def test_fresh_container_cannot_return_a_legacy_v1_run(tmp_path: Path) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"

    def execute(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        _complete_fake_container(command)
        volumes = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--volume"
        ]
        staged = Path(
            next(value.split(":", 1)[0] for value in volumes if value.endswith(":/app/readings:rw"))
        )
        candidate_dir = Path(
            next(value.split(":", 1)[0] for value in volumes if value.endswith(":/app/private:rw"))
        )
        manifest_path = staged / "analysis-run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = runner.RUN_SCHEMA_V1
        manifest["steps"] = list(runner.LEGACY_RUN_STEPS)
        for key in (
            "analytical_packet_edition_id",
            "analytical_packet_count",
            "analytical_draft_edition_id",
            "analytical_draft_count",
            "wire_claim_audit_edition_id",
            "wire_claim_audit_count",
            "wire_claim_audit_brief_eligible_count",
            "wire_delivery_policy",
        ):
            manifest.pop(key)
        _write(manifest_path, manifest)
        (candidate_dir / "analytical-packets-latest.json").unlink()
        (candidate_dir / "analytical-drafts-latest.json").unlink()
        (candidate_dir / "wire-claim-audits-latest.json").unlink()
        return subprocess.CompletedProcess(command, 0, "ok", "")

    with pytest.raises(runner.AnalysisRunnerError, match="required schema version"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=runs,
            private_root=private,
            commit_file=commit,
            execute=execute,
        )
    assert not list(runs.glob("run-*"))
    assert not (private / "state.json").exists()


def test_unchanged_shortcut_rejects_state_identity_drift(tmp_path: Path) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"

    def execute(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        _complete_fake_container(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
        execute=execute,
    )
    state_path = private / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["analytical_packet_edition_id"] = "packetset-" + "0" * 24
    state["analytical_packet_count"] = 0
    _write(state_path, state)

    with pytest.raises(
        runner.AnalysisRunnerError, match="analytical identity disagrees"
    ):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=runs,
            private_root=private,
            commit_file=commit,
            execute=execute,
        )


def test_host_rejects_fully_valid_forged_candidate_before_promotion(
    tmp_path: Path,
) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"

    def execute(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        _complete_fake_container(command)
        volumes = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--volume"
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
        event_path = staged / "event-flags-latest.json"
        original_event = event_path.read_bytes()
        event = json.loads(original_event)
        event["active"] = ["forged.signal"]
        _write(event_path, event)
        candidate = build_candidates(staged)
        packets = build_packet_set(candidate)
        drafts = build_template_draft_set(packets)
        (candidate_dir / "candidates-latest.json").write_bytes(
            canonical_json_bytes(candidate)
        )
        (candidate_dir / "analytical-packets-latest.json").write_bytes(
            canonical_json_bytes(packets)
        )
        (candidate_dir / "analytical-drafts-latest.json").write_bytes(
            canonical_json_bytes(drafts)
        )
        event_path.write_bytes(original_event)
        manifest_path = staged / "analysis-run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            candidate_edition_id=candidate["edition_id"],
            candidate_input_fingerprint=candidate["input_fingerprint"],
            candidate_count=candidate["n_candidates"],
            analytical_packet_edition_id=packets["edition_id"],
            analytical_packet_count=packets["n_packets"],
            analytical_draft_edition_id=drafts["edition_id"],
            analytical_draft_count=drafts["n_drafts"],
        )
        _write(manifest_path, manifest)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    with pytest.raises(runner.AnalysisRunnerError, match="derive from"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=runs,
            private_root=private,
            commit_file=commit,
            execute=execute,
        )
    assert not list(runs.glob("run-*"))
    assert not (private / "state.json").exists()


def test_host_rejects_valid_but_wrong_analytical_packet_before_promotion(
    tmp_path: Path,
) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"

    def execute(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, f"{COMMIT} {IMAGE_ID}\n", "")
        _complete_fake_container(command)
        candidate_dir = Path(
            next(
                value.split(":", 1)[0]
                for index, value in enumerate(command)
                if value == "--volume"
                for value in [command[index + 1]]
                if value.endswith(":/app/private:rw")
            )
        )
        packet_path = candidate_dir / "analytical-packets-latest.json"
        packets = json.loads(packet_path.read_text(encoding="utf-8"))
        packets["scope"] += " forged"
        packet_payload = {
            key: value for key, value in packets.items() if key != "edition_id"
        }
        packets["edition_id"] = _content_id("packetset", packet_payload, 24)
        drafts = build_template_draft_set(packets)
        packet_path.write_bytes(canonical_json_bytes(packets))
        (candidate_dir / "analytical-drafts-latest.json").write_bytes(
            canonical_json_bytes(drafts)
        )
        volumes = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--volume"
        ]
        staged = Path(
            next(
                value.split(":", 1)[0]
                for value in volumes
                if value.endswith(":/app/readings:rw")
            )
        )
        manifest_path = staged / "analysis-run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            analytical_packet_edition_id=packets["edition_id"],
            analytical_packet_count=packets["n_packets"],
            analytical_draft_edition_id=drafts["edition_id"],
            analytical_draft_count=drafts["n_drafts"],
        )
        _write(manifest_path, manifest)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    with pytest.raises(runner.AnalysisRunnerError, match="derive from"):
        runner.run_once(
            readings_dir=readings,
            newswire_dir=wire,
            runs_dir=runs,
            private_root=private,
            commit_file=commit,
            execute=execute,
        )
    assert not list(runs.glob("run-*"))
    assert not (private / "state.json").exists()


def test_production_path_uses_broker_for_every_privileged_lifecycle_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings, wire, commit = _inputs(tmp_path)
    runs = tmp_path / "runs"
    private = tmp_path / "private"
    runs.mkdir()
    operations: list[str] = []

    monkeypatch.setattr(runner, "DEFAULT_READINGS", readings)
    monkeypatch.setattr(runner, "DEFAULT_NEWSWIRE", wire)
    monkeypatch.setattr(runner, "DEFAULT_RUNS", runs)
    monkeypatch.setattr(runner, "DEFAULT_PRIVATE", private)
    monkeypatch.setattr(runner, "DEFAULT_COMMIT_FILE", commit)

    def broker(request: dict) -> dict:
        operation = request["operation"]
        operations.append(operation)
        if operation == "identity":
            return {"ok": True, "input_commit": COMMIT, "image_id": IMAGE_ID}
        if operation == "reconcile":
            return {"ok": True}
        if operation == "prepare":
            stage_name = ".staging-0123456789abcdef"
            stage = runs / stage_name
            for name in ("inputs", "readings", "private"):
                (stage / name).mkdir(parents=True, exist_ok=True)
            return {"ok": True, "stage_name": stage_name}
        if operation == "run":
            stage = runs / request["stage_name"]
            command = runner.docker_command(
                image_id=IMAGE_ID,
                frozen_readings=stage / "inputs",
                staged_readings=stage / "readings",
                candidate_dir=stage / "private",
                cidfile=stage / "container.cid",
                input_commit=COMMIT,
                decision_clock=request["decision_clock"],
            )
            _complete_fake_container(command)
            return {
                "ok": True,
                "returncode": 0,
                "stdout_tail": "ok",
                "stderr_tail": "",
                "timed_out": False,
            }
        if operation == "promote":
            (runs / request["stage_name"]).replace(runs / request["final_name"])
            return {"ok": True, "final_name": request["final_name"]}
        if operation == "prune":
            return {"ok": True, "removed": 0}
        if operation == "cleanup":
            shutil.rmtree(runs / request["stage_name"])
            return {"ok": True}
        raise AssertionError(f"unexpected broker operation: {operation}")

    monkeypatch.setattr(runner, "_call_broker", broker)

    real_unlink = Path.unlink

    def reject_analysis_uid_cid_cleanup(path: Path, *args, **kwargs) -> None:
        if path.name == "container.cid":
            raise PermissionError(
                "UID 10001 cannot unlink the broker-owned CID receipt"
            )
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_analysis_uid_cid_cleanup)

    first = runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
    )
    second = runner.run_once(
        readings_dir=readings,
        newswire_dir=wire,
        runs_dir=runs,
        private_root=private,
        commit_file=commit,
    )

    assert first["status"] == "completed"
    assert second["status"] == "unchanged"
    assert operations == [
        "identity",
        "reconcile",
        "prepare",
        "run",
        "promote",
        "prune",
        "identity",
        "reconcile",
        "prepare",
        "cleanup",
    ]


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
    receipt_raw = (private / runner.ANALYSIS_STATUS_NAME).read_text()
    receipt = json.loads(receipt_raw)
    assert receipt["status"] == "failed"
    assert receipt["failure_class"] == "AnalysisRunnerError"
    assert "boom" not in receipt_raw


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
    prior_status = b'{"status":"active-run-sentinel"}\n'
    (private / runner.ANALYSIS_STATUS_NAME).write_bytes(prior_status)

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
    assert (private / runner.ANALYSIS_STATUS_NAME).read_bytes() == prior_status


def test_systemd_contract_keeps_analysis_private_and_recurring() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (
        root / "ops/systemd/palimpsest-investigative-analysis.service"
    ).read_text()
    timer = (root / "ops/systemd/palimpsest-investigative-analysis.timer").read_text()
    source = (root / "ops/investigative_analysis_runner.py").read_text()
    container_contract = (root / "core/investigative_container_contract.py").read_text()

    assert "/usr/local/libexec/palimpsest-analysis" in service
    assert "/var/lib/palimpsest/readings /var/lib/palimpsest/newswire" in service
    assert [
        line for line in service.splitlines() if line.startswith("ReadWritePaths=")
    ] == [
        "ReadWritePaths=/var/lib/palimpsest-analysis/runs "
        "/var/lib/palimpsest-analysis/private "
        "/var/lib/palimpsest-analysis/delivery"
    ]
    assert "User=10001" in service and "Group=10001" in service
    assert "SupplementaryGroups=docker" not in service
    assert "Requires=palimpsest-investigative-broker.socket" in service
    assert "TimeoutStartSec=35m" in service
    assert "Restart=on-failure" in service
    assert "RestartSec=2m" in service
    assert "StartLimitIntervalSec=10m" in service
    assert "StartLimitBurst=3" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "OnCalendar=*:15,45" in timer
    assert '"--network"' in container_contract and '"none"' in container_contract
    assert "DEFAULT_BROKER_SOCKET" in source
    assert "execute: Callable[..., CompletedProcess] | None = None" in source
    assert 'private_root / "cascade.lock"' in source
    assert "private-review-only" in source
