"""Offline contract tests for the host-level freshness watchdog."""

from __future__ import annotations

import argparse
import importlib.util
import json
import stat
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "watchdog" / "palimpsest_freshness_watchdog.py"
SERVICE = ROOT / "ops" / "systemd" / "palimpsest-freshness-watchdog.service"
TIMER = ROOT / "ops" / "systemd" / "palimpsest-freshness-watchdog.timer"
COMPOSE = ROOT / "ops" / "docker" / "docker-compose.prod.yml"
ENV_EXAMPLE = ROOT / "ops" / "docker" / ".env.example"
DEPLOY_GUIDE = ROOT / "ops" / "DEPLOY-HETZNER.md"
SPEC = importlib.util.spec_from_file_location("freshness_watchdog", SCRIPT)
assert SPEC and SPEC.loader
watchdog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watchdog)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _healthy_status() -> dict:
    return {
        "status": "healthy",
        "generated_at": "2026-08-14T12:00:00Z",
        "pipeline": {
            "storage_available": True,
            "sources": {"ddti": {"state": "healthy"}},
        },
        "evidence": {"sources": {"ddti": {"state": "fresh"}}},
        "execution": {
            "storage_available": True,
            "queues": {"default": {"state": "fresh"}},
        },
    }


def _osint(*signals: dict, generated_at: str = "2026-08-14T12:00:00Z") -> dict:
    return {
        "schema_version": "osint-china.v1",
        "generated_at": generated_at,
        "signals": list(signals)
        or [
            {
                "id": "ddti",
                "status": "live",
                "optional": False,
                "source_timestamp": "2026-08-14T11:50:00Z",
                "freshness_deadline": "2026-08-14T13:00:00Z",
                "health": {"collector_status": None, "upstream_status": "ok"},
            }
        ],
    }


def test_node_conditions_are_per_source_and_per_execution_path() -> None:
    status = _healthy_status()
    status["status"] = "degraded"
    status["pipeline"]["sources"]["ddti"]["state"] = "failed"
    status["evidence"]["sources"]["weibo-hotsearch"] = {"state": "stale"}
    status["execution"]["queues"]["collectors"] = {"state": "missing"}

    result = watchdog.evaluate(status, _osint(), now=NOW)

    assert [item["condition"] for item in result["problems"]] == [
        "evidence/weibo-hotsearch",
        "execution/collectors",
        "pipeline/ddti",
    ]


def test_osint_deadlines_catch_configured_optional_but_ignore_disabled_or_absent() -> (
    None
):
    signals = (
        {
            "id": "bleedthrough",
            "status": "live",
            "optional": True,
            "source_timestamp": "2026-08-13T08:00:00Z",
            "freshness_deadline": "2026-08-13T22:00:00Z",
            "health": {"collector_status": None},
        },
        {
            "id": "baike-redaction",
            "status": "stale",
            "optional": True,
            "source_timestamp": "2026-07-30T00:00:00Z",
            "freshness_deadline": "2026-07-31T00:00:00Z",
            "health": {"collector_status": "disabled_no_authorized_access"},
        },
        {
            "id": "undeployed-optional",
            "status": "missing",
            "optional": True,
            "source_timestamp": None,
            "freshness_deadline": None,
            "health": {"collector_status": None},
        },
        {
            "id": "ddti",
            "status": "live",
            "optional": False,
            "source_timestamp": "2026-08-14T11:55:00Z",
            "freshness_deadline": "2026-08-14T13:00:00Z",
            "health": {"collector_status": None},
        },
    )

    result = watchdog.evaluate(_healthy_status(), _osint(*signals), now=NOW)

    assert result["status"] == "degraded"
    assert result["problems"] == [
        {
            "condition": "osint/bleedthrough",
            "scope": "osint",
            "subject": "bleedthrough",
            "state": "stale",
            "required": False,
        }
    ]


def test_stale_rollup_is_detected_from_timestamp_not_serialized_health() -> None:
    result = watchdog.evaluate(
        _healthy_status(),
        _osint(generated_at="2026-08-14T09:00:00Z"),
        now=NOW,
    )
    assert any(
        item["condition"] == "osint/bundle" and item["state"] == "stale"
        for item in result["problems"]
    )


def test_runner_returns_incident_exit_for_stale_local_osint(
    tmp_path: Path, monkeypatch
) -> None:
    osint_path = tmp_path / "osint.json"
    osint_path.write_text(
        json.dumps(_osint(generated_at="2026-08-14T09:00:00Z")),
        encoding="utf-8",
    )
    output = tmp_path / "watchdog" / "status.json"
    state = tmp_path / "watchdog" / "state.json"
    args = argparse.Namespace(
        status_url="http://127.0.0.1:8010/api/v1/node/status",
        osint_path=osint_path,
        output=output,
        state=state,
        bundle_max_age_seconds=7200,
        now="2026-08-14T12:00:00Z",
    )
    monkeypatch.delenv("PALIMPSEST_WATCHDOG_WEBHOOK_URL", raising=False)

    assert watchdog.run(args, status_opener=_Opener(_healthy_status())) == 2
    document = json.loads(output.read_text())
    assert document["status"] == "degraded"
    assert any(
        item["condition"] == "osint/bundle" and item["state"] == "stale"
        for item in document["problems"]
    )


def test_transition_keeps_existing_incident_while_opening_new_source() -> None:
    opened, resolved = watchdog._transition(
        {"pipeline/ddti": "failed", "evidence/weibo-hotsearch": "stale"},
        {"pipeline/ddti": "failed"},
    )
    assert opened == [{"condition": "evidence/weibo-hotsearch", "state": "stale"}]
    assert resolved == []


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


class _Opener:
    def __init__(self, payload: dict):
        self.payload = payload

    def open(self, _request, timeout: int):
        assert timeout == 5
        return _Response(self.payload)


def test_runner_atomically_writes_secret_free_status_and_private_latch(
    tmp_path: Path, monkeypatch
) -> None:
    osint_path = tmp_path / "osint.json"
    osint_path.write_text(json.dumps(_osint()), encoding="utf-8")
    output = tmp_path / "watchdog" / "status.json"
    state = tmp_path / "watchdog" / "state.json"
    args = argparse.Namespace(
        status_url="http://127.0.0.1:8010/api/v1/node/status",
        osint_path=osint_path,
        output=output,
        state=state,
        bundle_max_age_seconds=7200,
        now="2026-08-14T12:00:00Z",
    )
    monkeypatch.delenv("PALIMPSEST_WATCHDOG_WEBHOOK_URL", raising=False)

    assert watchdog.run(args, status_opener=_Opener(_healthy_status())) == 0
    document = json.loads(output.read_text())
    assert document["status"] == "healthy"
    assert document["problems"] == []
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert not list(output.parent.glob(".*.json.*"))


def test_degraded_log_only_runner_latches_condition_without_reopening(
    tmp_path: Path, monkeypatch
) -> None:
    osint_path = tmp_path / "osint.json"
    osint_path.write_text(json.dumps(_osint()), encoding="utf-8")
    output = tmp_path / "watchdog" / "status.json"
    state = tmp_path / "watchdog" / "state.json"
    degraded = _healthy_status()
    degraded["status"] = "degraded"
    degraded["pipeline"]["sources"]["ddti"]["state"] = "failed"
    args = argparse.Namespace(
        status_url="http://127.0.0.1:8010/api/v1/node/status",
        osint_path=osint_path,
        output=output,
        state=state,
        bundle_max_age_seconds=7200,
        now="2026-08-14T12:00:00Z",
    )
    monkeypatch.delenv("PALIMPSEST_WATCHDOG_WEBHOOK_URL", raising=False)

    assert watchdog.run(args, status_opener=_Opener(degraded)) == 2
    assert json.loads(output.read_text())["transition"]["opened_count"] == 1
    assert watchdog.run(args, status_opener=_Opener(degraded)) == 2
    assert json.loads(output.read_text())["transition"]["opened_count"] == 0
    assert json.loads(state.read_text())["conditions"] == {"pipeline/ddti": "failed"}


def test_systemd_lane_is_independent_and_cannot_write_readings() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    assert "StateDirectory=palimpsest-watchdog" in service
    assert "ProtectSystem=strict" in service
    assert "NoNewPrivileges=true" in service
    assert "CapabilityBoundingSet=" in service
    assert "ReadOnlyPaths=-/var/lib/palimpsest/readings" in service
    assert "ReadWritePaths=/var/lib/palimpsest/readings" not in service
    assert "celery" not in service.casefold()
    assert "OnCalendar=*:0/5" in timer
    assert "Persistent=true" in timer
    assert "from core" not in script
    assert "import redis" not in script
    assert "sqlalchemy" not in script.casefold()


def test_watchdog_default_matches_the_production_compose_host_port() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    endpoint = "http://127.0.0.1:8010/api/v1/node/status"
    assert watchdog.DEFAULT_STATUS_URL == endpoint
    assert f"PALIMPSEST_LOCAL_STATUS_URL={endpoint}" in service
    assert "127.0.0.1:${PALIMPSEST_API_PORT:-8010}:8000" in compose
    assert "PALIMPSEST_API_PORT=8010" in env_example


def test_deployment_installs_and_verifies_both_watchdog_units_after_api_probe() -> None:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    probe = guide.index("http://127.0.0.1:8010/api/v1/node/status")
    install_service = guide.index(
        "sudo install -m 0644 ops/systemd/palimpsest-freshness-watchdog.service"
    )
    verify = guide.index("sudo systemd-analyze verify", install_service)
    restore = guide.index("restore_activator_enablement() {", verify)

    assert install_service < verify < probe < restore
    verification = guide[verify:restore]
    assert "/etc/systemd/system/palimpsest-freshness-watchdog.service" in verification
    assert "/etc/systemd/system/palimpsest-freshness-watchdog.timer" in verification
    assert "/etc/systemd/system/palimpsest-witness.service" in verification
    assert "/etc/systemd/system/palimpsest-witness.timer" in verification
    assert "InvocationID" in guide[verify:restore]
    assert "ExecMainStatus" in guide[verify:restore]
