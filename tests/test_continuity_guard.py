"""Contracts for the independent direct-publication continuity guard."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "ops/railway/palimpsest-continuity-guard"


def _load_guard():
    loader = SourceFileLoader("palimpsest_continuity_guard", str(GUARD_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _state(
    unit: str,
    *,
    active: str = "active",
    enablement: str = "enabled",
) -> object:
    return guard.TimerState(
        unit=unit,
        load_state="loaded",
        unit_file_state=enablement,
        active_state=active,
    )


def _canonical(document: dict[str, object]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _hold(now: datetime) -> dict[str, object]:
    def clock(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    return {
        "schema_version": "palimpsest.continuity-maintenance-hold.v1",
        "status": "active",
        "transaction_id": "a" * 32,
        "reason_code": "reviewed-release",
        "created_at": clock(now - timedelta(minutes=5)),
        "expires_at": clock(now + timedelta(minutes=55)),
        "controller_commit": "b" * 40,
        "pre_state": {
            unit: {
                "load_state": "loaded",
                "unit_file_state": "enabled",
                "active_state": "active",
                "fragment_sha256": "c" * 64,
            }
            for unit in guard.TIMERS
        },
        "restore_profile_sha256": "d" * 64,
    }


def test_guard_inventory_is_the_complete_direct_publication_lane() -> None:
    assert guard.TIMERS == (
        "palimpsest-evidence-wire.timer",
        "palimpsest-measurement-refresh.timer",
        "palimpsest-railway-publish.timer",
        "palimpsest-direct-watchdog.timer",
    )
    assert os.access(GUARD_PATH, os.X_OK)


def test_only_enabled_inactive_timers_are_repair_candidates() -> None:
    states = [
        _state(guard.TIMERS[0], active="inactive"),
        _state(guard.TIMERS[1]),
        _state(guard.TIMERS[2], active="failed", enablement="disabled"),
        _state(guard.TIMERS[3], active="activating"),
    ]

    assert guard.repair_candidates(states, blockers=[]) == [guard.TIMERS[0]]
    assert guard.repair_candidates(states, blockers=["DATA HOLD"]) == []


def test_maintenance_hold_is_canonical_root_owned_and_bounded() -> None:
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    document = _hold(now)
    raw = _canonical(document)

    assert guard.validate_maintenance_hold(
        raw, now=now, owner_uid=0, owner_gid=0, mode=0o100600
    ) == document
    with pytest.raises(guard.GuardError, match="ownership or mode"):
        guard.validate_maintenance_hold(
            raw, now=now, owner_uid=501, owner_gid=0, mode=0o100600
        )
    with pytest.raises(guard.GuardError, match="not canonical"):
        guard.validate_maintenance_hold(
            json.dumps(document).encode(),
            now=now,
            owner_uid=0,
            owner_gid=0,
            mode=0o100600,
        )
    document["expires_at"] = (
        now + timedelta(seconds=guard.MAX_HOLD_SECONDS + 1)
    ).isoformat().replace("+00:00", "Z")
    with pytest.raises(guard.GuardError, match="bounded lease"):
        guard.validate_maintenance_hold(
            _canonical(document),
            now=now,
            owner_uid=0,
            owner_gid=0,
            mode=0o100600,
        )


def test_guard_repairs_an_accidentally_stopped_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = [_state(unit) for unit in guard.TIMERS]
    states[1] = _state(guard.TIMERS[1], active="inactive")
    commands: list[tuple[str, ...]] = []
    written: list[dict[str, object]] = []

    monkeypatch.setattr(guard, "_timer_states", lambda: states)
    monkeypatch.setattr(guard, "_blockers", lambda _now: [])

    def command(*arguments: str) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(guard, "_systemctl", command)
    monkeypatch.setattr(guard, "_property", lambda _unit, _name: "active")
    monkeypatch.setattr(guard, "_write_status", written.append)

    assert guard.main() == 0
    assert commands == [
        ("reset-failed", guard.TIMERS[1]),
        ("start", guard.TIMERS[1]),
    ]
    assert written[0]["status"] == "repaired"
    assert written[0]["repairs"] == [guard.TIMERS[1]]


def test_guard_abstains_instead_of_repairing_across_a_safety_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = [_state(unit) for unit in guard.TIMERS]
    states[0] = _state(guard.TIMERS[0], active="inactive")
    written: list[dict[str, object]] = []

    monkeypatch.setattr(guard, "_timer_states", lambda: states)
    monkeypatch.setattr(guard, "_blockers", lambda _now: ["pending-candidate"])
    monkeypatch.setattr(
        guard,
        "_systemctl",
        lambda *_arguments: (_ for _ in ()).throw(AssertionError("must abstain")),
    )
    monkeypatch.setattr(guard, "_write_status", written.append)

    assert guard.main() == 0
    assert written[0]["status"] == "abstained"
    assert written[0]["repairs"] == []


def test_disabled_steady_state_is_alerted_but_not_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = [_state(unit) for unit in guard.TIMERS]
    states[2] = _state(guard.TIMERS[2], active="inactive", enablement="disabled")
    written: list[dict[str, object]] = []

    monkeypatch.setattr(guard, "_timer_states", lambda: states)
    monkeypatch.setattr(guard, "_blockers", lambda _now: [])
    monkeypatch.setattr(
        guard,
        "_systemctl",
        lambda *_arguments: (_ for _ in ()).throw(AssertionError("must not repair")),
    )
    monkeypatch.setattr(guard, "_write_status", written.append)

    assert guard.main() == 1
    assert written[0]["status"] == "degraded"
    assert written[0]["repairs"] == []
    assert written[0]["problems"] == [
        f"steady-state timer is disabled: {guard.TIMERS[2]}"
    ]


def test_guard_units_are_independent_hardened_and_not_manually_stoppable() -> None:
    service = (
        ROOT / "ops/systemd/palimpsest-continuity-guard.service"
    ).read_text(encoding="utf-8")
    timer = (
        ROOT / "ops/systemd/palimpsest-continuity-guard.timer"
    ).read_text(encoding="utf-8")
    runbook = (
        ROOT / "docs/HETZNER-RAILWAY-CONTINUOUS-PUBLICATION.md"
    ).read_text(encoding="utf-8")

    assert "User=root" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "StateDirectory=palimpsest-continuity" in service
    assert "RefuseManualStop=yes" in timer
    assert "OnUnitActiveSec=2m" in timer
    assert "Persistent=true" in timer
    assert "disable-before-stop/start-before-enable" in runbook
    assert "palimpsest-continuity-guard.timer" not in (
        ROOT / "ops/DEPLOY-HETZNER.md"
    ).read_text(encoding="utf-8").split("RELEASE_ACTIVATORS=(", 1)[1].split(
        ")", 1
    )[0]
