from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from ops import investigative_analysis_broker as broker_module

COMMIT = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64


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

        def __init__(self, _command, **_kwargs):
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
