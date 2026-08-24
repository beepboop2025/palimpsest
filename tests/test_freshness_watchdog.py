"""Offline contract tests for the host-level freshness watchdog."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest


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


def _newswire(*, generated_at: str = "2026-08-14T12:00:00Z") -> dict:
    return {
        "schema_version": "palimpsest-newswire.v1",
        "generated_at": generated_at,
        "n_items": 0,
        "n_events": 0,
        "items": [],
        "events": [],
    }


def _canonical_bytes(document: dict) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _situation(
    newswire: dict, *, generated_at: str = "2026-08-14T12:00:00Z"
) -> dict:
    return {
        "schema_version": "palimpsest-china-situation.v1",
        "generated_at": generated_at,
        "inputs": {
            "newswire_generated_at": newswire["generated_at"],
            "newswire_sha256": hashlib.sha256(_canonical_bytes(newswire)).hexdigest(),
        },
        "situations": [],
    }


def _publications(*, generated_at: str = "2026-08-14T12:00:00Z") -> tuple[dict, dict]:
    newswire = _newswire(generated_at=generated_at)
    return newswire, _situation(newswire)


def test_node_conditions_are_per_source_and_per_execution_path() -> None:
    status = _healthy_status()
    status["status"] = "degraded"
    status["pipeline"]["sources"]["ddti"]["state"] = "failed"
    status["evidence"]["sources"]["weibo-hotsearch"] = {"state": "stale"}
    status["execution"]["queues"]["collectors"] = {"state": "missing"}

    result = watchdog.evaluate(status, _osint(), *_publications(), now=NOW)

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

    result = watchdog.evaluate(
        _healthy_status(), _osint(*signals), *_publications(), now=NOW
    )

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
        *_publications(),
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

    assert (
        watchdog.run(
            args,
            status_opener=_Opener(_healthy_status()),
            publication_opener=_PublicationOpener(*_publications()),
        )
        == 2
    )
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
    def __init__(self, payload: dict | bytes, *, url: str = "", status: int = 200):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.url = url
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status


class _Opener:
    def __init__(self, payload: dict):
        self.payload = payload

    def open(self, _request, timeout: int):
        assert timeout == 5
        return _Response(self.payload)


class _PublicationOpener:
    def __init__(
        self,
        newswire: dict,
        situation: dict,
        *,
        final_urls: dict[str, str] | None = None,
    ):
        self.payloads = {
            watchdog.PUBLIC_NEWSWIRE_URL: newswire,
            watchdog.PUBLIC_SITUATION_URL: situation,
        }
        self.final_urls = final_urls or {}
        self.requests = []

    def open(self, request, timeout: int):
        assert timeout == watchdog.PUBLICATION_TIMEOUT_SECONDS
        self.requests.append(request)
        url = request.full_url
        base_url = url.split("?", 1)[0]
        return _Response(
            self.payloads[base_url],
            url=self.final_urls.get(base_url, url),
        )


class _WebhookOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout: int):
        assert timeout == 10
        self.requests.append(request)
        return _Response(b"", url=request.full_url)


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

    assert (
        watchdog.run(
            args,
            status_opener=_Opener(_healthy_status()),
            publication_opener=_PublicationOpener(*_publications()),
        )
        == 0
    )
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

    publications = _PublicationOpener(*_publications())
    assert (
        watchdog.run(
            args,
            status_opener=_Opener(degraded),
            publication_opener=publications,
        )
        == 2
    )
    assert json.loads(output.read_text())["transition"]["opened_count"] == 1
    assert (
        watchdog.run(
            args,
            status_opener=_Opener(degraded),
            publication_opener=publications,
        )
        == 2
    )
    assert json.loads(output.read_text())["transition"]["opened_count"] == 0
    assert json.loads(state.read_text())["conditions"] == {"pipeline/ddti": "failed"}


def test_fresh_situation_cannot_hide_its_stale_embedded_newswire() -> None:
    newswire = _newswire(generated_at="2026-08-14T09:00:00Z")
    situation = _situation(newswire, generated_at="2026-08-14T12:00:00Z")

    result = watchdog.evaluate(
        _healthy_status(), _osint(), newswire, situation, now=NOW
    )

    assert {
        item["condition"]: item["state"] for item in result["problems"]
    } == {
        "publication/china-situation": "stale",
        "publication/newswire": "stale",
    }


@pytest.mark.parametrize("lineage_field", ["newswire_generated_at", "newswire_sha256"])
def test_situation_lineage_must_match_the_exact_canonical_newswire(
    lineage_field: str,
) -> None:
    newswire, situation = _publications()
    situation["inputs"][lineage_field] = (
        "2026-08-14T11:59:00Z"
        if lineage_field == "newswire_generated_at"
        else "0" * 64
    )

    result = watchdog.evaluate(
        _healthy_status(), _osint(), newswire, situation, now=NOW
    )

    assert result["problems"] == [
        {
            "condition": "publication/china-situation",
            "scope": "publication",
            "subject": "china-situation",
            "state": "corrupt",
            "required": True,
        }
    ]


def test_publication_conditions_use_the_existing_transition_latch(
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
    stale_wire = _newswire(generated_at="2026-08-14T09:00:00Z")
    stale_publications = _PublicationOpener(stale_wire, _situation(stale_wire))
    monkeypatch.delenv("PALIMPSEST_WATCHDOG_WEBHOOK_URL", raising=False)

    for expected_opened in (2, 0):
        assert (
            watchdog.run(
                args,
                status_opener=_Opener(_healthy_status()),
                publication_opener=stale_publications,
            )
            == 2
        )
        assert json.loads(output.read_text())["transition"]["opened_count"] == expected_opened

    assert json.loads(state.read_text())["conditions"] == {
        "publication/china-situation": "stale",
        "publication/newswire": "stale",
    }


def test_publication_transition_uses_the_existing_webhook_once(
    tmp_path: Path, monkeypatch
) -> None:
    osint_path = tmp_path / "osint.json"
    osint_path.write_text(json.dumps(_osint()), encoding="utf-8")
    args = argparse.Namespace(
        status_url="http://127.0.0.1:8010/api/v1/node/status",
        osint_path=osint_path,
        output=tmp_path / "watchdog" / "status.json",
        state=tmp_path / "watchdog" / "state.json",
        bundle_max_age_seconds=7200,
        now="2026-08-14T12:00:00Z",
    )
    stale_wire = _newswire(generated_at="2026-08-14T09:00:00Z")
    publications = _PublicationOpener(stale_wire, _situation(stale_wire))
    webhook = _WebhookOpener()
    monkeypatch.setenv(
        "PALIMPSEST_WATCHDOG_WEBHOOK_URL",
        "https://alerts.example.invalid/operator-hook",
    )
    monkeypatch.setattr(watchdog, "_webhook_is_public_https", lambda _url: True)

    for _attempt in range(2):
        assert (
            watchdog.run(
                args,
                status_opener=_Opener(_healthy_status()),
                publication_opener=publications,
                webhook_opener=webhook,
            )
            == 2
        )

    assert len(webhook.requests) == 1
    alert = json.loads(webhook.requests[0].data)
    assert alert["opened"] == [
        {"condition": "publication/china-situation", "state": "stale"},
        {"condition": "publication/newswire", "state": "stale"},
    ]


def test_publication_fetch_is_fixed_no_cache_json_and_bounded() -> None:
    opener = _PublicationOpener(*_publications())

    watchdog._fetch_public_json(
        watchdog.PUBLIC_NEWSWIRE_URL, observed_at=NOW, opener=opener
    )
    watchdog._fetch_public_json(
        watchdog.PUBLIC_SITUATION_URL, observed_at=NOW, opener=opener
    )

    request_urls = [request.full_url for request in opener.requests]
    assert [url.split("?", 1)[0] for url in request_urls] == [
        watchdog.PUBLIC_NEWSWIRE_URL,
        watchdog.PUBLIC_SITUATION_URL,
    ]
    assert all("?watchdog=" in url for url in request_urls)
    assert request_urls[0].split("?", 1)[1] == request_urls[1].split("?", 1)[1]
    for request in opener.requests:
        assert request.method == "GET"
        assert request.get_header("Accept") == "application/json"
        assert request.get_header("Cache-control") == "no-cache"
        assert request.get_header("Pragma") == "no-cache"
    assert watchdog.MAX_PUBLICATION_INPUT_BYTES == 12 * 1024 * 1024


def test_publication_fetch_refuses_unknown_authorities_and_redirects() -> None:
    opener = _PublicationOpener(
        *_publications(),
        final_urls={watchdog.PUBLIC_NEWSWIRE_URL: "https://example.com/diverted.json"},
    )

    with pytest.raises(watchdog.WatchdogError, match="not allowlisted"):
        watchdog._fetch_public_json("https://example.com/newswire.json", opener=opener)
    assert opener.requests == []

    with pytest.raises(watchdog.WatchdogError, match="redirect was refused"):
        watchdog._fetch_public_json(watchdog.PUBLIC_NEWSWIRE_URL, opener=opener)


def test_publication_fetch_stops_at_its_byte_ceiling() -> None:
    class OversizeResponse(_Response):
        def read(self, limit: int) -> bytes:
            return b"x" * limit

    class OversizeOpener:
        def open(self, request, timeout: int):
            assert timeout == watchdog.PUBLICATION_TIMEOUT_SECONDS
            return OversizeResponse(b"", url=request.full_url)

    with pytest.raises(watchdog.WatchdogError, match="exceeds its byte ceiling"):
        watchdog._fetch_public_json(
            watchdog.PUBLIC_NEWSWIRE_URL,
            opener=OversizeOpener(),
        )


def test_publication_watchdog_is_strictly_alert_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8").casefold()

    assert "api.github.com" not in source
    assert "workflow_dispatch" not in source
    assert "subprocess" not in source


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
