"""Safety and attempt-receipt contract for the Hetzner evidence-wire job."""

import fcntl
import hashlib
import json
from pathlib import Path

import pytest

from scripts import newswire_pull as pull

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "ops/systemd/palimpsest-evidence-wire.service"
TIMER = ROOT / "ops/systemd/palimpsest-evidence-wire.timer"


def test_node_newswire_is_bounded_unprivileged_and_state_separated() -> None:
    unit = SERVICE.read_text(encoding="utf-8")

    assert "User=palimpsest" in unit
    assert "Type=oneshot" in unit
    assert "TimeoutStartSec=10m" in unit
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadOnlyPaths=/home/palimpsest/palimpsest" in unit
    assert "ReadWritePaths=/var/lib/palimpsest/newswire" in unit
    assert "NoExecPaths=/var/lib/palimpsest/newswire" in unit
    assert "--workers 6" in unit
    assert "--output /var/lib/palimpsest/newswire/newswire-latest.json" in unit
    assert "--ledger /var/lib/palimpsest/newswire/newswire-versions.jsonl" in unit
    assert "--status /var/lib/palimpsest/newswire/newswire-status.json" in unit
    assert "--lock /var/lib/palimpsest/newswire/newswire.lock" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=2m" in unit
    assert "StartLimitIntervalSec=10m" in unit
    assert "StartLimitBurst=3" in unit


def test_node_newswire_has_a_non_overlapping_half_hour_timer() -> None:
    timer = TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*:0/30" in timer
    assert "RandomizedDelaySec=5m" in timer
    assert "FixedRandomDelay=true" in timer
    assert "Persistent=true" in timer
    assert "Unit=palimpsest-evidence-wire.service" in timer


def test_attempt_is_marked_running_before_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    document = {
        "generated_at": "2026-08-13T00:00:00Z",
        "n_items": 0,
        "n_events": 0,
        "events": [],
        "coverage": {
            "registry_sources": 1,
            "successful_sources": 1,
            "counts": {"success": 1},
            "status": "healthy",
        },
    }
    observed_running: dict[str, object] = {}

    monkeypatch.setattr(pull, "load_source_registry", lambda _path: object())

    def collect(*_args, **_kwargs):
        observed_running.update(json.loads(status.read_text(encoding="utf-8")))
        return document

    monkeypatch.setattr(pull, "collect_newswire", collect)

    assert (
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
            ]
        )
        == 0
    )

    assert set(observed_running) == {
        "schema_version",
        "attempted_at",
        "completed_at",
        "status",
        "fresh_sources",
        "output_generated_at",
        "output_sha256",
        "failure_class",
    }
    assert observed_running["schema_version"] == pull.STATUS_SCHEMA
    assert observed_running["status"] == "running"
    assert observed_running["completed_at"] is None
    assert observed_running["fresh_sources"] is None
    assert observed_running["output_generated_at"] is None
    assert observed_running["output_sha256"] is None
    assert observed_running["failure_class"] is None

    terminal = json.loads(status.read_text(encoding="utf-8"))
    assert terminal["attempted_at"] == observed_running["attempted_at"]
    assert terminal["status"] == "success"
    assert terminal["completed_at"] is not None


def test_complete_attempt_runs_under_persistent_exclusive_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "newswire.lock"
    events: list[tuple[int, int]] = []

    def locked_attempt(_args) -> int:
        contender = lock.open("rb")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            contender.close()
        return 17

    monkeypatch.setattr(pull, "_main_locked", locked_attempt)

    assert pull.main(["--lock", str(lock)]) == 17
    metadata = lock.stat()
    assert metadata.st_nlink == 1
    assert metadata.st_mode & 0o777 == 0o600
    with lock.open("rb") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        events.append((contender.fileno(), fcntl.LOCK_SH))
    assert events


def test_locked_attempt_reconciles_only_owned_atomic_temporaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    lock = tmp_path / "newswire.lock"
    stale = [
        tmp_path / ".newswire-latest.json.abc123_4",
        tmp_path / ".newswire-versions.jsonl.1234abcd",
        tmp_path / ".newswire-status.json.a1b2c3d4",
    ]
    unrelated = tmp_path / ".not-a-newswire-temporary.abc123_4"
    for path in (*stale, unrelated):
        path.write_text("abandoned", encoding="utf-8")

    def attempt(_args) -> int:
        assert all(not path.exists() for path in stale)
        assert unrelated.read_text(encoding="utf-8") == "abandoned"
        return 0

    monkeypatch.setattr(pull, "_main_locked", attempt)

    assert (
        pull.main(
            [
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--lock",
                str(lock),
            ]
        )
        == 0
    )


def test_locked_attempt_rejects_unsafe_matching_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("must survive", encoding="utf-8")
    unsafe = tmp_path / ".newswire-latest.json.abc123_4"
    unsafe.symlink_to(outside)
    monkeypatch.setattr(
        pull,
        "_main_locked",
        lambda _args: pytest.fail("unsafe temporary reached the collector"),
    )

    with pytest.raises(ValueError, match="temporary artifact is unsafe"):
        pull.main(
            [
                "--output",
                str(tmp_path / "newswire-latest.json"),
                "--ledger",
                str(tmp_path / "newswire-versions.jsonl"),
                "--status",
                str(tmp_path / "newswire-status.json"),
                "--lock",
                str(tmp_path / "newswire.lock"),
            ]
        )
    assert unsafe.is_symlink()
    assert outside.read_text(encoding="utf-8") == "must survive"


def test_zero_fresh_sources_preserves_last_good_and_records_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    prior_output = b'{"generated_at":"2026-08-13T00:00:00Z","last_good":true}\n'
    prior_ledger = b'{"last_good":true}\n'
    output.write_bytes(prior_output)
    ledger.write_bytes(prior_ledger)

    monkeypatch.setattr(
        pull,
        "_load_previous",
        lambda _path: {"generated_at": "2026-08-13T00:00:00Z"},
    )
    monkeypatch.setattr(pull, "_load_ledger", lambda _path: [])
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: object())
    monkeypatch.setattr(
        pull,
        "collect_newswire",
        lambda *_args, **_kwargs: {"coverage": {"counts": {"success": 0}}},
    )

    result = pull.main(
        [
            "--config",
            str(tmp_path / "registry.json"),
            "--output",
            str(output),
            "--ledger",
            str(ledger),
            "--status",
            str(status),
        ]
    )

    assert result == 2
    assert output.read_bytes() == prior_output
    assert ledger.read_bytes() == prior_ledger
    receipt = json.loads(status.read_text())
    assert set(receipt) == {
        "schema_version",
        "attempted_at",
        "completed_at",
        "status",
        "fresh_sources",
        "output_generated_at",
        "output_sha256",
        "failure_class",
    }
    assert receipt["schema_version"] == pull.STATUS_SCHEMA
    assert receipt["status"] == "no-fresh-sources"
    assert receipt["fresh_sources"] == 0
    assert receipt["output_generated_at"] == "2026-08-13T00:00:00Z"
    assert receipt["output_sha256"] == hashlib.sha256(prior_output).hexdigest()
    assert receipt["failure_class"] == "NoFreshSources"
    assert not list(tmp_path.glob(".newswire-status.json.*"))


def test_success_receipt_is_bound_to_the_atomically_published_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    document = {
        "generated_at": "2026-08-13T00:00:00Z",
        "n_items": 0,
        "n_events": 0,
        "events": [],
        "coverage": {
            "registry_sources": 1,
            "successful_sources": 1,
            "counts": {"success": 1},
            "status": "healthy",
        },
    }
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: object())
    monkeypatch.setattr(pull, "collect_newswire", lambda *_args, **_kwargs: document)

    assert (
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
            ]
        )
        == 0
    )

    receipt = json.loads(status.read_text())
    assert receipt["status"] == "success"
    assert receipt["fresh_sources"] == 1
    assert receipt["output_generated_at"] == document["generated_at"]
    assert receipt["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert receipt["failure_class"] is None


def test_unexpected_wire_failure_receipt_does_not_leak_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = tmp_path / "newswire-status.json"
    monkeypatch.setattr(
        pull,
        "load_source_registry",
        lambda _path: (_ for _ in ()).throw(RuntimeError("sensitive upstream detail")),
    )

    with pytest.raises(RuntimeError, match="sensitive upstream detail"):
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(tmp_path / "latest.json"),
                "--ledger",
                str(tmp_path / "ledger.jsonl"),
                "--status",
                str(status),
            ]
        )

    raw = status.read_text()
    receipt = json.loads(raw)
    assert receipt["status"] == "failed"
    assert receipt["failure_class"] == "RuntimeError"
    assert "sensitive upstream detail" not in raw
