from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ops import investigative_analysis_broker as broker_module

COMMIT = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64


def test_root_entrypoint_imports_its_verified_bundle_under_isolated_python(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "core").mkdir(parents=True)
    for source, target in (
        (Path(broker_module.__file__), bundle / "investigative_analysis_broker.py"),
        (
            Path(broker_module.__file__).parents[1]
            / "core"
            / "investigative_container_contract.py",
            bundle / "core" / "investigative_container_contract.py",
        ),
        (
            Path(broker_module.__file__).parents[1] / "core" / "__init__.py",
            bundle / "core" / "__init__.py",
        ),
    ):
        shutil.copy2(source, target)

    completed = subprocess.run(
        [sys.executable, "-I", str(bundle / "investigative_analysis_broker.py")],
        input=b"",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert json.loads(completed.stdout)["ok"] is False
    assert b"ModuleNotFoundError" not in completed.stderr


def _broker(tmp_path: Path) -> broker_module.AnalysisBroker:
    return broker_module.AnalysisBroker(
        bundle_root=tmp_path / "bundle",
        runs_dir=tmp_path / "runs",
        commit_file=tmp_path / "deployed-commit",
    )


def test_request_contract_rejects_extra_fields_before_side_effect(
    tmp_path: Path,
) -> None:
    instance = _broker(tmp_path)

    with pytest.raises(broker_module.BrokerError, match="fields are not exact"):
        instance.dispatch(
            {
                "schema_version": broker_module.BROKER_SCHEMA,
                "operation": "identity",
                "image": "caller-controlled:latest",
            }
        )
    with pytest.raises(broker_module.BrokerError, match="unsupported"):
        instance.dispatch(
            {
                "schema_version": broker_module.BROKER_SCHEMA,
                "operation": "shell",
            }
        )


def test_request_reader_rejects_duplicates_and_oversize() -> None:
    duplicate = io.BytesIO(
        b'{"schema_version":"x","operation":"identity","operation":"run"}'
    )
    with pytest.raises(broker_module.BrokerError, match="duplicate JSON key"):
        broker_module._read_request(duplicate)

    with pytest.raises(broker_module.BrokerError, match="oversized"):
        broker_module._read_request(
            io.BytesIO(b"{" + b" " * broker_module.MAX_REQUEST_BYTES + b"}")
        )


def test_prepare_removes_partial_stage_when_ownership_transition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _broker(tmp_path)
    instance.runs_dir.mkdir()
    monkeypatch.setattr(instance, "_require_runs_root", lambda: None)
    monkeypatch.setattr(broker_module.secrets, "token_hex", lambda _size: "1" * 16)

    def reject_chown(*_args, **_kwargs) -> None:
        raise PermissionError("CAP_CHOWN unavailable")

    monkeypatch.setattr(broker_module.os, "chown", reject_chown)

    with pytest.raises(PermissionError, match="CAP_CHOWN unavailable"):
        instance.dispatch(
            {
                "schema_version": broker_module.BROKER_SCHEMA,
                "operation": "prepare",
            }
        )

    assert not list(instance.runs_dir.iterdir())


def test_run_operation_builds_only_the_fixed_networkless_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _broker(tmp_path)
    stage = tmp_path / "runs" / ".staging-0123456789abcdef"
    for name in ("inputs", "readings", "private"):
        (stage / name).mkdir(parents=True, exist_ok=True)
    captured: list[list[str]] = []

    monkeypatch.setattr(instance, "_identity", lambda: (COMMIT, IMAGE_ID))
    monkeypatch.setattr(instance, "_require_stage_inventory", lambda _stage: None)

    class FakeProcess:
        def __init__(self, command, **kwargs):
            captured.append(command)
            cidfile = Path(command[command.index("--cidfile") + 1])
            cidfile.write_text("c" * 64 + "\n", encoding="ascii")
            kwargs["stdout"].write(b"ok\n")
            self.returncode = 0

        def wait(self, timeout):
            assert timeout == broker_module.CONTAINER_TIMEOUT_SECONDS
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)

    result = instance._run_container(
        stage=stage,
        input_commit=COMMIT,
        decision_clock="2026-08-11T18:00:00Z",
    )

    assert result["returncode"] == 0
    command = captured[0]
    assert command[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--name") + 1] == "palimpsest-investigative-analysis"
    assert command[command.index("--user") + 1] == "10001:10001"
    assert f"{stage / 'inputs'}:/app/frozen:ro" in command
    assert f"{stage / 'readings'}:/app/readings:rw" in command
    assert f"{stage / 'private'}:/app/private:rw" in command
    assert "sh" not in command and "bash" not in command
    assert not (stage / "container.cid").exists()


def test_failed_run_preserves_cid_receipt_for_brokered_stage_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _broker(tmp_path)
    stage = tmp_path / "runs" / ".staging-0123456789abcdef"
    for name in ("inputs", "readings", "private"):
        (stage / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(instance, "_identity", lambda: (COMMIT, IMAGE_ID))
    monkeypatch.setattr(instance, "_require_stage_inventory", lambda _stage: None)

    class FailedProcess:
        def __init__(self, command, **_kwargs):
            cidfile = Path(command[command.index("--cidfile") + 1])
            cidfile.write_text("d" * 64 + "\n", encoding="ascii")
            self.returncode = 9

        def wait(self, timeout):
            assert timeout == broker_module.CONTAINER_TIMEOUT_SECONDS
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FailedProcess)

    result = instance._run_container(
        stage=stage,
        input_commit=COMMIT,
        decision_clock="2026-08-11T18:00:00Z",
    )

    assert result["returncode"] == 9
    assert (stage / "container.cid").read_text(encoding="ascii") == "d" * 64 + "\n"


def test_timeout_removes_only_the_fixed_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _broker(tmp_path)
    stage = tmp_path / "stage"
    for name in ("inputs", "readings", "private"):
        (stage / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(instance, "_identity", lambda: (COMMIT, IMAGE_ID))
    monkeypatch.setattr(instance, "_require_stage_inventory", lambda _stage: None)
    removed: list[bool] = []
    monkeypatch.setattr(
        instance,
        "_remove_container",
        lambda *, allow_absent: removed.append(allow_absent),
    )

    class TimeoutProcess:
        calls = 0

        def __init__(self, command, **_kwargs):
            Path(command[command.index("--cidfile") + 1]).write_text(
                "c" * 64 + "\n", encoding="ascii"
            )
            self.returncode = 137

        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("docker", timeout)
            return self.returncode

        def kill(self):
            raise AssertionError("container removal should unblock the Docker client")

    monkeypatch.setattr(subprocess, "Popen", TimeoutProcess)

    result = instance._run_container(
        stage=stage,
        input_commit=COMMIT,
        decision_clock="2026-08-11T18:00:00Z",
    )

    assert result["timed_out"] is True
    assert result["returncode"] == 137
    assert removed == [False]
    assert (stage / "container.cid").read_text(encoding="ascii") == "c" * 64 + "\n"
