"""Deployment contract for the bounded, fixed-vantage BLEEDTHROUGH service.

These tests are offline.  They verify the unit's least-privilege boundary, exercise
the shell with fake curl/flock/python executables, and fault-inject atomic writers.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

import scripts.bleedthrough_curate as curate
import scripts.bleedthrough_fetch_prefixes as fetch_prefixes
import scripts.bleedthrough_pull as pull


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ops/systemd/palimpsest-bleedthrough.service"
TIMER = ROOT / "ops/systemd/palimpsest-bleedthrough.timer"
ENV_EXAMPLE = ROOT / "ops/bleedthrough/bleedthrough.env.example"
PROBER = ROOT / "ops/bleedthrough_prober.sh"
RUNBOOK = ROOT / "ops/bleedthrough/README.md"


def test_systemd_service_is_fixed_user_least_privilege_and_state_separated():
    unit = SERVICE.read_text(encoding="utf-8")

    assert "User=palimpsest" in unit and "Group=palimpsest" in unit
    assert "WorkingDirectory=/home/palimpsest/palimpsest" in unit
    assert "EnvironmentFile=/etc/palimpsest/bleedthrough.env" in unit
    assert (
        "ConditionFileIsExecutable="
        "/home/palimpsest/palimpsest/ops/bleedthrough_prober.sh"
    ) in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "ReadOnlyPaths=/home/palimpsest/palimpsest" in unit
    assert (
        "ReadWritePaths=/var/lib/palimpsest/bleedthrough /var/lib/palimpsest/readings"
    ) in unit
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
    assert "TimeoutStartSec=3h" in unit


def test_timer_runs_every_six_hours_with_nonzero_random_offset():
    timer = TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 00,06,12,18:00:00 UTC" in timer
    assert "RandomizedDelaySec=45m" in timer
    assert "FixedRandomDelay=true" in timer
    assert "Persistent=true" in timer


def test_environment_records_fixed_box_consent_kill_switch_and_durable_paths():
    env = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "BLEEDTHROUGH_LIVE=1" in env
    assert "BLEEDTHROUGH_ALLOW_BOX=1" in env
    assert "BLEEDTHROUGH_VANTAGE_COUNTRY=DE" in env
    assert "PALIMPSEST_KILLFILE=/var/lib/palimpsest/readings/state/STOP" in env
    for variable in (
        "BLEEDTHROUGH_PREFIXES",
        "BLEEDTHROUGH_TARGETS",
        "BLEEDTHROUGH_STORE",
        "BLEEDTHROUGH_OUT",
        "BLEEDTHROUGH_HIST",
        "BLEEDTHROUGH_LOCKFILE",
    ):
        assert f"{variable}=/var/lib/palimpsest/" in env


def test_install_preflights_and_preserves_access_for_the_shared_identity():
    runbook = RUNBOOK.read_text(encoding="utf-8")

    preflight = runbook.index("--ensure-identity")
    access_grant = runbook.index(
        "setfacl -R -m u:palimpsest-analysis:rwX /var/lib/palimpsest/readings"
    )
    assert preflight < access_grant
    assert "-exec setfacl -m d:u:palimpsest-analysis:rwx {} +" in runbook
    assert "u:10001:" not in runbook
    assert "world-write" in runbook


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_prober_routes_all_mutable_paths_outside_checkout(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "python-calls.log"
    state = tmp_path / "state"

    _executable(fake_bin / "curl", "#!/bin/sh\necho 203.0.113.7\n")
    _executable(fake_bin / "flock", "#!/bin/sh\nexit 0\n")
    _executable(
        fake_bin / "python3",
        "#!/bin/sh\n"
        "printf '%s|%s|%s|%s|%s\\n' \"$BLEEDTHROUGH_PREFIXES\" "
        '"$BLEEDTHROUGH_TARGETS" "$BLEEDTHROUGH_OUT" '
        '"$BLEEDTHROUGH_HIST" "$BLEEDTHROUGH_STORE" >> "$CALL_LOG"\n',
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "CALL_LOG": str(calls),
        "PALIMPSEST_STATE_ROOT": str(state),
        "BLEEDTHROUGH_PYTHON": str(fake_bin / "python3"),
        "BLEEDTHROUGH_LIVE": "1",
        "BLEEDTHROUGH_ALLOW_BOX": "1",
    }

    result = subprocess.run(
        ["bash", str(PROBER)],
        env=env,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    rows = calls.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
    for row in rows:
        paths = [Path(value) for value in row.split("|")]
        assert all(path.is_relative_to(state) for path in paths)
        assert all(not path.is_relative_to(ROOT) for path in paths)
    assert (state / "bleedthrough/round.lock").exists()


def test_known_hetzner_box_still_requires_separate_allow_flag(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "curl", "#!/bin/sh\necho 167.233.225.54\n")
    _executable(fake_bin / "flock", "#!/bin/sh\nexit 0\n")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "PALIMPSEST_STATE_ROOT": str(tmp_path / "state"),
        "BLEEDTHROUGH_LIVE": "1",
    }

    result = subprocess.run(
        ["bash", str(PROBER)],
        env=env,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 2
    assert "BLEEDTHROUGH_ALLOW_BOX=1" in result.stdout


@pytest.mark.parametrize("module", [fetch_prefixes, curate])
def test_private_atomic_json_replace_failure_preserves_last_good(
    module, tmp_path, monkeypatch
):
    destination = tmp_path / "state.json"
    destination.write_text('{"last":"good"}\n', encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("injected rename failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected rename failure"):
        module._atomic_write_json(str(destination), {"next": True})

    assert destination.read_text(encoding="utf-8") == '{"last":"good"}\n'
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_public_atomic_replace_failure_preserves_last_good(tmp_path, monkeypatch):
    destination = tmp_path / "bleedthrough-latest.json"
    destination.write_bytes(b'{"last":"good"}\n')

    def fail_replace(_source, _destination):
        raise OSError("injected rename failure")

    monkeypatch.setattr(pull.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected rename failure"):
        pull._atomic_write_json(str(destination), {"next": True})

    assert destination.read_bytes() == b'{"last":"good"}\n'
    assert list(tmp_path.glob(".bleedthrough-latest.json.*.tmp")) == []


def test_history_refuses_to_extend_a_torn_record(tmp_path):
    history = tmp_path / "bleedthrough-history.jsonl"
    history.write_bytes(b'{"incomplete":true}')

    with pytest.raises(ValueError, match="truncated history"):
        pull._atomic_append_jsonl(str(history), {"next": True})

    assert history.read_bytes() == b'{"incomplete":true}'


def test_prober_shell_is_syntactically_valid():
    subprocess.run(["bash", "-n", str(PROBER)], check=True)
