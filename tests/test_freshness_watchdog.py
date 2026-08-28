"""Offline contract tests for the host-level freshness watchdog."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import stage_pages_rights


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
PUBLICATION_SHA = "a" * 40


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


def _situation(newswire: dict, *, generated_at: str = "2026-08-14T12:00:00Z") -> dict:
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


def _identity(document: dict) -> dict:
    payload = _canonical_bytes(document)
    return {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def _restricted_publications(
    *,
    generated_at: str = "2026-08-14T12:00:00Z",
    attested_at: str = "2026-08-14T12:00:00Z",
    built_at: str = "2026-08-14T12:00:00Z",
) -> tuple[dict, dict, dict, dict, dict]:
    policy = {
        "path": "config/china_econ_source_policy.json",
        "schema_version": "palimpsest.china-economic-source-policy.v1",
        "policy_scope": "china_economic_values_and_seiche_export",
        "default_decision": "deny",
        "sha256": "1" * 64,
        "bytes": 123,
    }
    limitations = [
        "Metadata only; quarantined source artifacts are not republished here.",
        "No source values, observations, or per-record identifiers are included.",
        "This attestation conveys no observation or publication authority.",
        "Unavailable or restricted evidence is not a directional signal.",
    ]
    master = {
        "schema_version": "palimpsest-restricted-publication.v1",
        "publication_sha": PUBLICATION_SHA,
        "rights_evaluated_at": attested_at,
        "status": "restricted",
        "availability": "unavailable",
        "publication_allowed": False,
        "reason": "Current source policy requires a metadata-only endpoint.",
        "artifact": {
            "path": watchdog.PUBLIC_RIGHTS_STATUS_PATH,
            "media_type": "application/json",
        },
        "policy": dict(policy),
        "counts": {
            "input_records": 2,
            "allowed_records": 0,
            "restricted_records": 2,
            "published_records": 0,
            "quarantined_artifacts": 2,
        },
        "source_decisions": [
            {
                "source_id": "cfets_benchmarks",
                "decision": "deny",
                "configured_decision": "deny",
                "availability": "restricted",
                "values_allowed": False,
                "seiche_export_allowed": False,
                "license": None,
                "license_url": None,
                "rights_evidence_url": "https://www.shibor.org/english/svcmds/",
                "attribution": "China Foreign Exchange Trade System",
                "reviewed_at": "2026-08-14T00:00:00Z",
                "expires_at": "2027-08-14T00:00:00Z",
                "reason": "Publication is denied without redistribution authority.",
                "decision_sha256": "5" * 64,
                "input_records": 2,
                "published_records": 0,
            }
        ],
        "quarantined_paths": sorted(
            [watchdog.PUBLIC_NEWSWIRE_PATH, watchdog.PUBLIC_SITUATION_PATH]
        ),
        "limitations": list(limitations),
    }
    master_identity = _identity(master)

    def stub(path: str) -> dict:
        return {
            "schema_version": "palimpsest-restricted-publication-endpoint.v1",
            "publication_sha": PUBLICATION_SHA,
            "rights_evaluated_at": attested_at,
            "status": "restricted",
            "availability": "unavailable",
            "publication_allowed": False,
            "reason": "Current source policy requires a metadata-only endpoint.",
            "artifact": {"path": path, "media_type": "application/json"},
            "policy": dict(policy),
            "master_status": {
                "path": f"/{watchdog.PUBLIC_RIGHTS_STATUS_PATH}",
                **master_identity,
            },
            "counts": {
                "input_records": 2,
                "restricted_records": 2,
                "published_records": 0,
            },
            "limitations": list(limitations),
        }

    newswire_stub = stub(watchdog.PUBLIC_NEWSWIRE_PATH)
    situation_stub = stub(watchdog.PUBLIC_SITUATION_PATH)
    attestation = {
        "schema_version": "palimpsest.publication-freshness-attestation.v1",
        "publication_sha": PUBLICATION_SHA,
        "attested_at": attested_at,
        "mode": "rights-suppressed",
        "publication_allowed": False,
        "artifacts": {
            "newswire": {
                "path": watchdog.PUBLIC_NEWSWIRE_PATH,
                "schema_version": "palimpsest-newswire.v1",
                "generated_at": generated_at,
                "canonical_sha256": "2" * 64,
            },
            "china_situation": {
                "path": watchdog.PUBLIC_SITUATION_PATH,
                "schema_version": "palimpsest-china-situation.v1",
                "generated_at": generated_at,
                "canonical_sha256": "3" * 64,
                "inputs": {
                    "newswire_generated_at": generated_at,
                    "newswire_canonical_sha256": "2" * 64,
                },
            },
        },
        "rights_status": {
            "path": watchdog.PUBLIC_RIGHTS_STATUS_PATH,
            **master_identity,
        },
        "limitations": list(limitations),
    }
    critical_documents = {
        watchdog.PUBLIC_NEWSWIRE_PATH: newswire_stub,
        watchdog.PUBLIC_SITUATION_PATH: situation_stub,
        watchdog.PUBLIC_ATTESTATION_PATH: attestation,
        watchdog.PUBLIC_RIGHTS_STATUS_PATH: master,
    }
    critical_files = {
        path: _identity(document) for path, document in critical_documents.items()
    }
    manifest = {
        "schema_version": "palimpsest.railway-static-release.v1",
        "source_commit": PUBLICATION_SHA,
        "built_at": built_at,
        "deployment_source": "local-git-archive",
        "github_required": False,
        "state": "artifact_ready",
        "file_count": len(critical_files),
        "total_bytes": sum(row["bytes"] for row in critical_files.values()),
        "tree_sha256": "4" * 64,
        "critical_files": critical_files,
    }
    return newswire_stub, situation_stub, attestation, master, manifest


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
        self.payload = (
            payload if isinstance(payload, bytes) else _canonical_bytes(payload)
        )
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
        attestation: dict | None = None,
        rights_status: dict | None = None,
        release_manifest: dict | None = None,
        *,
        final_urls: dict[str, str] | None = None,
    ):
        self.payloads = {
            watchdog.PUBLIC_NEWSWIRE_URL: newswire,
            watchdog.PUBLIC_SITUATION_URL: situation,
        }
        if attestation is not None:
            self.payloads[watchdog.PUBLIC_ATTESTATION_URL] = attestation
        if rights_status is not None:
            self.payloads[watchdog.PUBLIC_RIGHTS_STATUS_URL] = rights_status
        if release_manifest is not None:
            self.payloads[watchdog.PUBLIC_RELEASE_MANIFEST_URL] = release_manifest
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


def test_runner_verifies_all_served_restricted_identities(
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
    publications = _PublicationOpener(*_restricted_publications())
    monkeypatch.delenv("PALIMPSEST_WATCHDOG_WEBHOOK_URL", raising=False)

    assert (
        watchdog.run(
            args,
            status_opener=_Opener(_healthy_status()),
            publication_opener=publications,
        )
        == 0
    )
    document = json.loads(output.read_text())
    assert document["publication"]["mode"] == "rights-suppressed"
    assert document["publication"]["publication_sha"] == PUBLICATION_SHA
    assert len(publications.requests) == 5


def test_production_runner_refuses_legacy_full_publication_mode(
    tmp_path: Path, monkeypatch
) -> None:
    osint_path = tmp_path / "osint.json"
    osint_path.write_text(json.dumps(_osint()), encoding="utf-8")
    output = tmp_path / "watchdog" / "status.json"
    args = argparse.Namespace(
        status_url="http://127.0.0.1:8010/api/v1/node/status",
        osint_path=osint_path,
        output=output,
        state=tmp_path / "watchdog" / "state.json",
        bundle_max_age_seconds=7200,
        required_publication_mode="rights-suppressed",
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
    assert document["publication"]["mode"] == "full"
    assert {item["condition"]: item["state"] for item in document["problems"]} == {
        "publication/rights-mode": "restricted-required"
    }


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

    assert {item["condition"]: item["state"] for item in result["problems"]} == {
        "publication/china-situation": "stale",
        "publication/newswire": "stale",
    }


def test_fresh_rights_suppressed_publication_is_exactly_bound() -> None:
    result = watchdog.evaluate(
        _healthy_status(), _osint(), *_restricted_publications(), now=NOW
    )

    assert result["status"] == "healthy"
    assert result["problems"] == []
    assert result["publication"] == {
        "mode": "rights-suppressed",
        "publication_sha": PUBLICATION_SHA,
        "newswire_generated_at": "2026-08-14T12:00:00Z",
        "china_situation_generated_at": "2026-08-14T12:00:00Z",
        "attestation": _identity(_restricted_publications()[2]),
        "release_manifest": {
            "source_commit": PUBLICATION_SHA,
            "tree_sha256": "4" * 64,
            **_identity(_restricted_publications()[4]),
        },
    }


def test_actual_rights_stager_output_is_accepted_with_served_byte_identities(
    tmp_path: Path,
) -> None:
    """Keep the producer and watchdog contracts joined by a real staged bundle."""

    policy_path = tmp_path / stage_pages_rights.POLICY_RELATIVE_PATH
    policy_path.parent.mkdir(parents=True)
    shutil.copy2(ROOT / stage_pages_rights.POLICY_RELATIVE_PATH, policy_path)
    readings = tmp_path / "readings"
    readings.mkdir()
    (readings / "china-econ-observations.jsonl").write_text(
        json.dumps(
            {
                "source_id": "cfets_benchmarks",
                "series_id": "cn.cfets.synthetic",
                "value": 987654.321,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    for relative in (
        stage_pages_rights.NEWSWIRE_RELATIVE_PATH,
        stage_pages_rights.CHINA_SITUATION_RELATIVE_PATH,
    ):
        (tmp_path / relative).write_bytes((ROOT / relative).read_bytes())
    newswire = json.loads((tmp_path / watchdog.PUBLIC_NEWSWIRE_PATH).read_bytes())
    generated_at = newswire["generated_at"]
    observed_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    stage_pages_rights.stage_pages_tree(
        tmp_path,
        publication_sha=PUBLICATION_SHA,
        evaluated_at=observed_at,
        admission_at=observed_at,
    )

    def fetched(relative: str) -> watchdog._FetchedDocument:
        raw = (tmp_path / relative).read_bytes()
        return watchdog._FetchedDocument(json.loads(raw), raw)

    staged_newswire = fetched(watchdog.PUBLIC_NEWSWIRE_PATH)
    staged_situation = fetched(watchdog.PUBLIC_SITUATION_PATH)
    attestation = fetched(watchdog.PUBLIC_ATTESTATION_PATH)
    rights_status = fetched(watchdog.PUBLIC_RIGHTS_STATUS_PATH)
    critical_documents = {
        watchdog.PUBLIC_NEWSWIRE_PATH: staged_newswire,
        watchdog.PUBLIC_SITUATION_PATH: staged_situation,
        watchdog.PUBLIC_ATTESTATION_PATH: attestation,
        watchdog.PUBLIC_RIGHTS_STATUS_PATH: rights_status,
    }
    critical_files = {
        relative: {
            "sha256": document.served_sha256,
            "bytes": document.served_bytes,
        }
        for relative, document in critical_documents.items()
    }
    release_manifest = {
        "schema_version": watchdog.RELEASE_MANIFEST_SCHEMA,
        "source_commit": PUBLICATION_SHA,
        "built_at": generated_at,
        "deployment_source": "local-git-archive",
        "github_required": False,
        "state": "artifact_ready",
        "file_count": len(critical_files),
        "total_bytes": sum(row["bytes"] for row in critical_files.values()),
        "tree_sha256": "4" * 64,
        "critical_files": critical_files,
    }

    node_status = _healthy_status()
    node_status["generated_at"] = generated_at
    osint = _osint(generated_at=generated_at)
    for signal in osint["signals"]:
        if signal.get("freshness_deadline") is not None:
            signal["freshness_deadline"] = generated_at
    result = watchdog.evaluate(
        node_status,
        osint,
        staged_newswire,
        staged_situation,
        attestation,
        rights_status,
        release_manifest,
        now=observed_at,
    )

    assert result["status"] == "healthy"
    assert result["problems"] == []
    assert result["publication"]["mode"] == "rights-suppressed"
    assert result["publication"]["publication_sha"] == PUBLICATION_SHA


def test_stale_attested_evidence_stays_stale_despite_fresh_rights_and_build() -> None:
    publications = _restricted_publications(generated_at="2026-08-14T09:00:00Z")

    result = watchdog.evaluate(_healthy_status(), _osint(), *publications, now=NOW)

    assert {item["condition"]: item["state"] for item in result["problems"]} == {
        "publication/china-situation": "stale",
        "publication/newswire": "stale",
    }
    assert result["publication"]["mode"] == "rights-suppressed"


def test_rights_and_build_clocks_are_not_evidence_freshness_clocks() -> None:
    publications = _restricted_publications(
        generated_at="2026-08-14T12:00:00Z",
        attested_at="2026-08-13T00:00:00Z",
        built_at="2026-08-13T00:00:00Z",
    )

    result = watchdog.evaluate(_healthy_status(), _osint(), *publications, now=NOW)

    assert result["status"] == "healthy"
    assert result["problems"] == []


@pytest.mark.parametrize(
    "lineage_field",
    ["newswire_generated_at", "newswire_canonical_sha256"],
)
def test_restricted_situation_lineage_must_match_attested_newswire(
    lineage_field: str,
) -> None:
    publications = list(_restricted_publications())
    inputs = publications[2]["artifacts"]["china_situation"]["inputs"]
    inputs[lineage_field] = (
        "2026-08-14T11:59:00Z" if lineage_field.endswith("generated_at") else "0" * 64
    )

    result = watchdog.evaluate(_healthy_status(), _osint(), *publications, now=NOW)

    assert {item["condition"]: item["state"] for item in result["problems"]} == {
        "publication/china-situation": "corrupt",
        "publication/newswire": "corrupt",
    }
    assert result["publication"]["mode"] == "unknown"


@pytest.mark.parametrize(
    "mismatch",
    [
        "endpoint-path",
        "master-identity",
        "policy-identity",
        "source-commit",
        "critical-file",
    ],
)
def test_restricted_publication_refuses_any_cross_document_mismatch(
    mismatch: str,
) -> None:
    publications = list(_restricted_publications())
    newswire, situation, attestation, _master, manifest = publications
    if mismatch == "endpoint-path":
        newswire["artifact"]["path"] = watchdog.PUBLIC_SITUATION_PATH
    elif mismatch == "master-identity":
        attestation["rights_status"]["sha256"] = "0" * 64
    elif mismatch == "policy-identity":
        situation["policy"]["sha256"] = "0" * 64
    elif mismatch == "source-commit":
        manifest["source_commit"] = "b" * 40
    elif mismatch == "critical-file":
        manifest["critical_files"][watchdog.PUBLIC_NEWSWIRE_PATH]["sha256"] = "0" * 64

    result = watchdog.evaluate(_healthy_status(), _osint(), *publications, now=NOW)

    assert {item["condition"]: item["state"] for item in result["problems"]} == {
        "publication/china-situation": "corrupt",
        "publication/newswire": "corrupt",
    }
    assert result["publication"]["publication_sha"] is None


def test_restricted_publication_refuses_nested_rights_metadata_smuggling() -> None:
    publications = list(_restricted_publications())
    master = publications[3]
    master["source_decisions"][0]["private_values"] = {"signals": [1, 2, 3]}
    master_identity = _identity(master)
    for stub in publications[:2]:
        stub["master_status"].update(master_identity)
    publications[2]["rights_status"].update(master_identity)
    manifest = publications[4]
    for path, document in (
        (watchdog.PUBLIC_NEWSWIRE_PATH, publications[0]),
        (watchdog.PUBLIC_SITUATION_PATH, publications[1]),
        (watchdog.PUBLIC_ATTESTATION_PATH, publications[2]),
        (watchdog.PUBLIC_RIGHTS_STATUS_PATH, master),
    ):
        manifest["critical_files"][path] = _identity(document)
    manifest["total_bytes"] = sum(
        row["bytes"] for row in manifest["critical_files"].values()
    )

    result = watchdog.evaluate(_healthy_status(), _osint(), *publications, now=NOW)

    assert {item["condition"]: item["state"] for item in result["problems"]} == {
        "publication/china-situation": "corrupt",
        "publication/newswire": "corrupt",
    }
    assert result["publication"]["mode"] == "unknown"


def test_publication_bundle_fetches_all_fixed_documents_concurrently(
    monkeypatch,
) -> None:
    barrier = threading.Barrier(len(watchdog.PUBLICATION_ENDPOINTS))
    seen: list[str] = []

    def fetch(url: str, *, observed_at, opener):
        assert observed_at == NOW
        assert opener == "bounded-opener"
        seen.append(url)
        barrier.wait(timeout=1)
        return watchdog._FetchedDocument({"url": url}, b"{}\n")

    monkeypatch.setattr(watchdog, "_fetch_public_json", fetch)

    documents = watchdog._fetch_publication_documents(
        observed_at=NOW,
        opener="bounded-opener",
    )

    assert len(documents) == len(watchdog.PUBLICATION_ENDPOINTS)
    assert set(seen) == {url for _name, url in watchdog.PUBLICATION_ENDPOINTS}


def test_watchdog_network_budget_fits_the_systemd_deadline() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    timeout_match = re.search(r"^TimeoutStartSec=(\d+)s$", service, re.MULTILINE)
    assert timeout_match is not None
    systemd_timeout = int(timeout_match.group(1))
    worst_bounded_path = (
        watchdog.LOCAL_STATUS_TIMEOUT_SECONDS
        + watchdog.PUBLICATION_TIMEOUT_SECONDS
        + watchdog.WEBHOOK_TIMEOUT_SECONDS
    )
    assert systemd_timeout - worst_bounded_path >= 10


def test_mixed_original_and_rights_suppressed_mode_fails_closed() -> None:
    publications = list(_restricted_publications())
    publications[0] = _newswire()

    result = watchdog.evaluate(_healthy_status(), _osint(), *publications, now=NOW)

    assert {item["condition"]: item["state"] for item in result["problems"]} == {
        "publication/china-situation": "corrupt",
        "publication/newswire": "corrupt",
    }
    assert result["publication"]["mode"] == "unknown"


@pytest.mark.parametrize("lineage_field", ["newswire_generated_at", "newswire_sha256"])
def test_situation_lineage_must_match_the_exact_canonical_newswire(
    lineage_field: str,
) -> None:
    newswire, situation = _publications()
    situation["inputs"][lineage_field] = (
        "2026-08-14T11:59:00Z" if lineage_field == "newswire_generated_at" else "0" * 64
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
        assert (
            json.loads(output.read_text())["transition"]["opened_count"]
            == expected_opened
        )

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
    opener = _PublicationOpener(*_restricted_publications())
    urls = [
        watchdog.PUBLIC_NEWSWIRE_URL,
        watchdog.PUBLIC_SITUATION_URL,
        watchdog.PUBLIC_ATTESTATION_URL,
        watchdog.PUBLIC_RIGHTS_STATUS_URL,
        watchdog.PUBLIC_RELEASE_MANIFEST_URL,
    ]
    for url in urls:
        watchdog._fetch_public_json(url, observed_at=NOW, opener=opener)

    request_urls = [request.full_url for request in opener.requests]
    assert [url.split("?", 1)[0] for url in request_urls] == urls
    assert all(url.startswith("https://www.palimpsest.info/") for url in urls)
    assert all("?watchdog=" in url for url in request_urls)
    assert len({url.split("?", 1)[1] for url in request_urls}) == 1
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

    with pytest.raises(watchdog.WatchdogError, match="not allowlisted"):
        watchdog._fetch_public_json(
            "https://palimpsest.info/readings/newswire-latest.json", opener=opener
        )
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
    assert "ReadOnlyPaths=-/home/palimpsest/palimpsest" in service
    assert "ProtectSystem=strict" in service
    assert "NoNewPrivileges=true" in service
    assert "CapabilityBoundingSet=" in service
    assert "ReadOnlyPaths=-/var/lib/palimpsest/readings" in service
    assert "ReadWritePaths=/var/lib/palimpsest/readings" not in service
    assert "celery" not in service.casefold()
    assert "--required-publication-mode rights-suppressed" in service
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
    assert f"--status-url {endpoint}" in service
    assert "ExecStart=/usr/bin/python3 /opt/palimpsest/ops/watchdog/" in service
    assert "--bundle-max-age-seconds 21600" in service
    assert "127.0.0.1:${PALIMPSEST_API_PORT:-8010}:8000" in compose
    assert "PALIMPSEST_API_PORT=8010" in env_example


def test_deployment_installs_and_verifies_both_watchdog_units_after_api_probe() -> None:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    install_service = guide.index(
        'sudo install -o root -g root -m 0644 "$WATCHDOG_CONTROLLER_SERVICE"'
    )
    verify = guide.index("sudo systemd-analyze verify", install_service)
    timeout = guide.index("--connect-timeout 1 --max-time 5", verify)
    probe = guide.index("http://127.0.0.1:8010/readyz", timeout)
    restore = guide.index("restore_activator_enablement() {", verify)

    assert install_service < verify < timeout < probe < restore
    verification = guide[install_service:restore]
    assert "/etc/systemd/system/palimpsest-freshness-watchdog.service" in verification
    assert "/etc/systemd/system/palimpsest-freshness-watchdog.timer" in verification
    assert "/etc/systemd/system/palimpsest-witness.service" in verification
    assert "/etc/systemd/system/palimpsest-witness.timer" in verification
    assert 'sudo cmp -s "$WATCHDOG_PREFLIGHT_SCRIPT"' in verification
    assert 'sudo cmp -s "$WATCHDOG_CONTROLLER_SERVICE"' in verification
    assert "NeedDaemonReload" in verification
    assert "InvocationID" in guide[verify:restore]
    assert "ExecMainStatus" in guide[verify:restore]
