"""Functional lineage proofs for the direct Railway publication watchdog."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import time
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parent.parent
WATCHDOG_PATH = ROOT / "ops" / "railway" / "palimpsest-direct-watchdog"
ROTATION_HELPER_PATH = ROOT / "ops" / "railway" / "rotate-direct-publication-base"
LEGACY_INSTALLED_KEYS = {
    "publisher_service_sha256",
    "publisher_sha256",
    "reconciler_sha256",
    "transition_helper_sha256",
    "watchdog_service_sha256",
    "watchdog_sha256",
    "watchdog_timer_sha256",
}
SUCCESSOR_INSTALLED_KEYS = LEGACY_INSTALLED_KEYS | {
    "continuity_guard_service_sha256",
    "continuity_guard_sha256",
    "continuity_guard_timer_sha256",
    "event_analysis_publish_dropin_sha256",
    "event_analysis_service_sha256",
    "event_analysis_wrapper_sha256",
    "rotation_helper_sha256",
}


def _load_watchdog() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "palimpsest_direct_watchdog_test", str(WATCHDOG_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


watchdog = _load_watchdog()


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


V1_RECEIPT = b"""{
  "schema_version": "palimpsest.hetzner-railway-publication.v1",
  "status": "verified",
  "recorded_at": "2026-08-30T09:12:08Z",
  "input_sha256": "7fef2522dfdb1a0f6697f292b22a266e381073e5a9efeede38b2dc68a549413e",
  "wire_generated_at": "2026-08-30T08:45:26Z",
  "base_sha": "b22d809bca5ca8aed8255e8a89a06a88dc9cbcb9",
  "release_sha": "ae5ecacd2e151d15af3fe06a7cd1219aa51573e7",
  "railway": {
    "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
    "status": "SUCCESS"
  },
  "origins": {
    "provider": "https://palimpsest-publication-production.up.railway.app",
    "public": "https://www.palimpsest.info"
  },
  "github_actions_used": false
}
"""


@dataclass
class Fixture:
    config: Any
    receipt: dict[str, Any]
    receipt_path: Path
    pin_path: Path
    bundle_path: Path
    metadata_path: Path
    manifest_path: Path
    manifest_raw: bytes
    host_sha: str
    base_sha: str
    release_sha: str

    def validate(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return watchdog.validate_lineage(
            self.config,
            fetch_manifest=lambda _origin: self.manifest_raw,
        )

    def rewrite_receipt(self) -> None:
        _write(self.receipt_path, _json_bytes(self.receipt), 0o600)


@pytest.fixture
def lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    predecessor_manifest = {
        "schema_version": "palimpsest.railway-static-release.v1",
        "state": "artifact_ready",
        "source_commit": watchdog.INCIDENT_RELEASE_SHA,
        "tree_sha256": "8" * 64,
        "deployment_source": "local-git-archive",
        "github_required": False,
        "file_count": 101,
        "total_bytes": 202,
    }
    predecessor_manifest_raw = _json_bytes(predecessor_manifest)
    monkeypatch.setattr(
        watchdog,
        "INCIDENT_MANIFEST_SHA256",
        _sha(predecessor_manifest_raw),
    )
    monkeypatch.setattr(watchdog, "INCIDENT_TREE_SHA256", "8" * 64)

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Palimpsest Test")
    _git(repository, "config", "user.email", "test@palimpsest.info")

    (repository / "host.txt").write_text("host deployment\n", encoding="utf-8")
    _git(repository, "add", "host.txt")
    _git(repository, "commit", "--quiet", "-m", "host deployment")
    host_sha = _git(repository, "rev-parse", "HEAD")

    (repository / "base.txt").write_text("publication base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "--quiet", "-m", "publication base")
    base_sha = _git(repository, "rev-parse", "HEAD")

    (repository / "release.txt").write_text("generated edition\n", encoding="utf-8")
    _git(repository, "add", "release.txt")
    _git(repository, "commit", "--quiet", "-m", "generated release")
    release_sha = _git(repository, "rev-parse", "HEAD")
    release_ref = f"refs/palimpsest/releases/{release_sha}"
    _git(repository, "update-ref", release_ref, release_sha)

    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    control_root = tmp_path / "control"
    control_root.mkdir(mode=0o750)
    bundle_root = state_root / "release-bundles"
    bundle_root.mkdir(mode=0o700)
    bundle_path = bundle_root / f"{release_sha}.bundle"
    _git(repository, "bundle", "create", str(bundle_path), release_ref, f"^{base_sha}")
    bundle_path.chmod(0o600)
    bundle_raw = bundle_path.read_bytes()
    bundle_digest = _sha(bundle_raw)

    metadata_path = bundle_root / f"{release_sha}.json"
    metadata = {
        "schema_version": watchdog.BUNDLE_SCHEMA,
        "status": "verified",
        "created_at": "2026-08-30T12:00:00Z",
        "path": str(bundle_path),
        "sha256": bundle_digest,
        "bytes": len(bundle_raw),
        "base_sha": base_sha,
        "release_sha": release_sha,
    }
    metadata_raw = _json_bytes(metadata)
    _write(metadata_path, metadata_raw, 0o600)

    installed_root = tmp_path / "installed"
    installed_root.mkdir()
    installed: dict[str, tuple[Path, int]] = {
        "continuity_guard_sha256": (
            installed_root / "continuity-guard",
            0o755,
        ),
        "continuity_guard_service_sha256": (
            installed_root / "continuity-guard.service",
            0o644,
        ),
        "continuity_guard_timer_sha256": (
            installed_root / "continuity-guard.timer",
            0o644,
        ),
        "event_analysis_wrapper_sha256": (
            installed_root / "event-analysis",
            0o755,
        ),
        "event_analysis_service_sha256": (
            installed_root / "event-analysis.service",
            0o644,
        ),
        "event_analysis_publish_dropin_sha256": (
            installed_root / "event-analysis-publish.conf",
            0o644,
        ),
        "publisher_sha256": (installed_root / "publisher", 0o755),
        "transition_helper_sha256": (installed_root / "transition", 0o755),
        "rotation_helper_sha256": (installed_root / "rotation", 0o755),
        "reconciler_sha256": (installed_root / "reconciler", 0o755),
        "watchdog_sha256": (installed_root / "watchdog", 0o755),
        "publisher_service_sha256": (
            installed_root / "publisher.service",
            0o644,
        ),
        "watchdog_service_sha256": (installed_root / "watchdog.service", 0o644),
        "watchdog_timer_sha256": (installed_root / "watchdog.timer", 0o644),
    }
    installed_digests: dict[str, str] = {}
    for key, (path, mode) in installed.items():
        raw = f"{key}\n".encode()
        _write(path, raw, mode)
        installed_digests[key] = _sha(raw)

    pin_path = tmp_path / "railway-publication-base.json"
    pin = {
        "incident_id": watchdog.PIN_INCIDENT,
        "installed": {
            key: value
            for key, value in installed_digests.items()
            if key in LEGACY_INSTALLED_KEYS
        },
        "origins": {
            "provider": watchdog.DEFAULT_PROVIDER_ORIGIN,
            "public": watchdog.DEFAULT_PUBLIC_ORIGIN,
        },
        "previous": {
            "canonical_head": watchdog.INCIDENT_BASE_SHA,
            "deployed_commit": watchdog.INCIDENT_BASE_SHA,
            "live_manifest_sha256": watchdog.INCIDENT_MANIFEST_SHA256,
            "live_release_sha": watchdog.INCIDENT_RELEASE_SHA,
            "live_tree_sha256": watchdog.INCIDENT_TREE_SHA256,
            "publication_input_sha256": watchdog.INCIDENT_INPUT_SHA256,
            "publication_receipt_sha256": watchdog.INCIDENT_RECEIPT_SHA256,
            "railway_deployment_id": watchdog.INCIDENT_DEPLOYMENT_ID,
        },
        "recorded_at": "2026-08-30T11:55:00Z",
        "schema_version": watchdog.PIN_SCHEMA,
        "status": "verified",
        "target": {"base_sha": base_sha, "public_main_sha": base_sha},
    }
    pin_raw = _json_bytes(pin)
    monkeypatch.setattr(watchdog, "INCIDENT_PIN_SHA256", _sha(pin_raw))
    monkeypatch.setattr(watchdog, "INCIDENT_PIN_TARGET_SHA", base_sha)
    _write(pin_path, pin_raw, 0o640)

    deployed_commit = tmp_path / "deployed-commit"
    _write(deployed_commit, f"{host_sha}\n".encode(), 0o644)

    archive_root = state_root / "receipts"
    archive_root.mkdir(mode=0o700)
    assert _sha(V1_RECEIPT) == watchdog.INCIDENT_RECEIPT_SHA256
    v1_archive = archive_root / f"{watchdog.INCIDENT_RECEIPT_SHA256}.json"
    _write(v1_archive, V1_RECEIPT, 0o600)

    manifest = {
        "schema_version": "palimpsest.railway-static-release.v1",
        "state": "artifact_ready",
        "source_commit": release_sha,
        "built_at": "2026-08-30T12:00:00Z",
        "deployment_source": "local-git-archive",
        "github_required": False,
        "tree_sha256": "3" * 64,
        "file_count": 123,
        "total_bytes": 456789,
        "critical_files": {"index.html": {"bytes": 10, "sha256": "7" * 64}},
    }
    manifest_raw = _json_bytes(manifest)
    manifest_root = state_root / "release-manifests"
    manifest_root.mkdir(mode=0o700)
    manifest_path = manifest_root / f"{release_sha}.json"
    _write(manifest_path, manifest_raw, 0o600)
    release_manifest = {
        "bytes": len(manifest_raw),
        "file_count": manifest["file_count"],
        "path": str(manifest_path),
        "sha256": _sha(manifest_raw),
        "total_bytes": manifest["total_bytes"],
        "tree_sha256": manifest["tree_sha256"],
    }
    publication_base = {
        "path": str(pin_path),
        "kind": "verified_transition",
        "sha256": _sha(pin_raw),
        "target_sha": base_sha,
    }
    receipt_bundle = {
        "schema_version": watchdog.BUNDLE_SCHEMA,
        "path": str(bundle_path),
        "sha256": bundle_digest,
        "bytes": len(bundle_raw),
        "base_sha": base_sha,
        "release_sha": release_sha,
        "metadata_path": str(metadata_path),
        "metadata_sha256": _sha(metadata_raw),
    }
    predecessor = {
        "receipt_sha256": watchdog.INCIDENT_RECEIPT_SHA256,
        "archive_path": str(v1_archive),
        "schema_version": watchdog.V1_SCHEMA,
        "base_sha": watchdog.INCIDENT_BASE_SHA,
        "release_sha": watchdog.INCIDENT_RELEASE_SHA,
        "deployment_id": watchdog.INCIDENT_DEPLOYMENT_ID,
        "input_sha256": watchdog.INCIDENT_INPUT_SHA256,
        "wire_generated_at": "2026-08-30T08:45:26Z",
        "manifest_sha256": watchdog.INCIDENT_MANIFEST_SHA256,
        "tree_sha256": watchdog.INCIDENT_TREE_SHA256,
    }

    project_id = "11111111-1111-4111-8111-111111111111"
    environment_id = "22222222-2222-4222-8222-222222222222"
    service_id = "33333333-3333-4333-8333-333333333333"
    created_at = "2026-08-30T09:00:00Z"
    image_digest = f"sha256:{'9' * 64}"
    evidence_root = state_root / "predecessors"
    evidence_root.mkdir(mode=0o700)
    release_evidence = evidence_root / release_sha
    release_evidence.mkdir(mode=0o700)
    provider_path = release_evidence / "provider-railway-release.json"
    public_path = release_evidence / "public-railway-release.json"
    _write(provider_path, predecessor_manifest_raw, 0o600)
    _write(public_path, predecessor_manifest_raw, 0o600)
    topology_document = {
        "id": project_id,
        "services": {
            "edges": [{"node": {"id": service_id, "name": "palimpsest-publication"}}]
        },
        "environments": {
            "edges": [
                {
                    "node": {
                        "id": environment_id,
                        "serviceInstances": {
                            "edges": [
                                {
                                    "node": {
                                        "serviceId": service_id,
                                        "latestDeployment": {
                                            "id": watchdog.INCIDENT_DEPLOYMENT_ID,
                                            "status": "SUCCESS",
                                            "createdAt": created_at,
                                            "meta": {
                                                "imageDigest": image_digest,
                                                "reason": "deploy",
                                            },
                                        },
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }
    topology_raw = _json_bytes(topology_document)
    topology_path = release_evidence / "railway-status.json"
    _write(topology_path, topology_raw, 0o600)
    rollback_evidence = {
        "schema_version": watchdog.ROLLBACK_EVIDENCE_SCHEMA,
        "captured_at": "2026-08-30T11:59:00Z",
        "provider_manifest": {
            "path": str(provider_path),
            "sha256": _sha(predecessor_manifest_raw),
            "bytes": len(predecessor_manifest_raw),
        },
        "public_manifest": {
            "path": str(public_path),
            "sha256": _sha(predecessor_manifest_raw),
            "bytes": len(predecessor_manifest_raw),
        },
        "topology": {
            "path": str(topology_path),
            "sha256": _sha(topology_raw),
            "bytes": len(topology_raw),
            "project_id": project_id,
            "environment_id": environment_id,
            "service_id": service_id,
            "deployment_id": watchdog.INCIDENT_DEPLOYMENT_ID,
            "image_digest": image_digest,
            "reason": "deploy",
            "created_at": created_at,
        },
    }
    input_sha256 = "4" * 64
    submission_id = "5" * 32
    message = (
        f"palimpsest-hetzner-{release_sha[:12]}-{input_sha256[:12]}-{submission_id}"
    )
    candidate_document = {
        "schema_version": watchdog.CANDIDATE_SCHEMA,
        "status": "mutation_unresolved",
        "prepared_at": "2026-08-30T12:01:00Z",
        "message": message,
        "input_sha256": input_sha256,
        "wire_generated_at": "2026-08-30T12:00:00Z",
        "host_deployed_sha": host_sha,
        "base_sha": base_sha,
        "publication_base": {
            key: publication_base[key] for key in ("path", "kind", "sha256")
        },
        "release_sha": release_sha,
        "release_bundle": {
            key: receipt_bundle[key]
            for key in (
                "path",
                "sha256",
                "bytes",
                "metadata_path",
                "metadata_sha256",
            )
        },
        "release_manifest": release_manifest,
        "predecessor": predecessor,
        "rollback_evidence": rollback_evidence,
        "submission_id": submission_id,
    }
    candidate_raw = _json_bytes(candidate_document)
    candidate_digest = _sha(candidate_raw)
    candidate_root = state_root / "candidates"
    candidate_root.mkdir(mode=0o700)
    candidate_path = candidate_root / f"{candidate_digest}.json"
    _write(candidate_path, candidate_raw, 0o600)

    receipt = {
        "schema_version": watchdog.V2_SCHEMA,
        "status": "verified",
        "recorded_at": "2026-08-30T12:05:00Z",
        "input_sha256": input_sha256,
        "wire_generated_at": "2026-08-30T12:00:00Z",
        "host_deployed_sha": host_sha,
        "base_sha": base_sha,
        "publication_base": publication_base,
        "release_sha": release_sha,
        "release_bundle": receipt_bundle,
        "predecessor": predecessor,
        "candidate": {
            "archive_path": str(candidate_path),
            "journal_sha256": candidate_digest,
            "message": message,
        },
        "live_manifest": release_manifest,
        "railway": {
            "deployment_id": "12345678-1234-4123-8123-123456789abc",
            "status": "SUCCESS",
        },
        "origins": {
            "provider": watchdog.DEFAULT_PROVIDER_ORIGIN,
            "public": watchdog.DEFAULT_PUBLIC_ORIGIN,
        },
        "github_actions_used": False,
    }
    receipt_path = state_root / "latest-success.json"
    _write(receipt_path, _json_bytes(receipt), 0o600)

    uid = os.getuid()
    gid = os.getgid()
    config = watchdog.LineageConfig(
        state_root=state_root,
        control_root=control_root,
        source_repository=repository,
        publication_receipt=receipt_path,
        deployed_commit=deployed_commit,
        base_pin=pin_path,
        pending_candidate=state_root / "pending-candidate.json",
        data_hold=tmp_path / "railway-publication-data-hold.json",
        installed_publisher=installed["publisher_sha256"][0],
        installed_continuity_guard=installed["continuity_guard_sha256"][0],
        installed_continuity_guard_service=installed["continuity_guard_service_sha256"][
            0
        ],
        installed_continuity_guard_timer=installed["continuity_guard_timer_sha256"][0],
        installed_event_analysis_wrapper=installed["event_analysis_wrapper_sha256"][0],
        installed_event_analysis_service=installed["event_analysis_service_sha256"][0],
        installed_event_analysis_publish_dropin=installed[
            "event_analysis_publish_dropin_sha256"
        ][0],
        installed_transition_helper=installed["transition_helper_sha256"][0],
        installed_rotation_helper=installed["rotation_helper_sha256"][0],
        installed_reconciler=installed["reconciler_sha256"][0],
        installed_watchdog=installed["watchdog_sha256"][0],
        installed_publisher_service=installed["publisher_service_sha256"][0],
        installed_watchdog_service=installed["watchdog_service_sha256"][0],
        installed_watchdog_timer=installed["watchdog_timer_sha256"][0],
        provider_origin=watchdog.DEFAULT_PROVIDER_ORIGIN,
        public_origin=watchdog.DEFAULT_PUBLIC_ORIGIN,
        state_uid=uid,
        state_gid=gid,
        pin_uid=uid,
        pin_gid=gid,
        installed_uid=uid,
        installed_gid=gid,
    )
    return Fixture(
        config=config,
        receipt=receipt,
        receipt_path=receipt_path,
        pin_path=pin_path,
        bundle_path=bundle_path,
        metadata_path=metadata_path,
        manifest_path=manifest_path,
        manifest_raw=manifest_raw,
        host_sha=host_sha,
        base_sha=base_sha,
        release_sha=release_sha,
    )


def test_v2_allows_publication_base_to_differ_from_host_deployment(
    lineage: Fixture,
) -> None:
    assert watchdog.MAX_PIN_GENERATIONS == 256
    proof, candidate = lineage.validate()

    assert lineage.host_sha != lineage.base_sha
    assert proof["host_deployed_sha"] == lineage.host_sha
    assert proof["publication_base_sha"] == lineage.base_sha
    assert proof["release_sha"] == lineage.release_sha
    assert candidate == {"status": "none"}


def test_successor_pin_authenticates_archived_pin_receipt_and_rotation_record(
    lineage: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = json.loads(lineage.pin_path.read_bytes())
    anchor["previous"]["canonical_head"] = lineage.host_sha
    anchor["previous"]["deployed_commit"] = lineage.host_sha
    anchor_raw = _json_bytes(anchor)
    anchor_digest = _sha(anchor_raw)
    monkeypatch.setattr(watchdog, "INCIDENT_BASE_SHA", lineage.host_sha)
    monkeypatch.setattr(watchdog, "INCIDENT_PIN_SHA256", anchor_digest)
    monkeypatch.setattr(watchdog, "INCIDENT_PIN_TARGET_SHA", lineage.base_sha)

    history = lineage.config.control_root / "base-rotation-history"
    for relative in (
        "",
        "pins",
        "receipts",
        "manifests",
        "manifests/provider",
        "manifests/public",
        "topologies",
        "rotations",
    ):
        path = history / relative if relative else history
        path.mkdir(mode=0o750)
        path.chmod(0o750)
    anchor_path = history / "pins" / f"{anchor_digest}.json"
    _write(anchor_path, anchor_raw, 0o640)

    predecessor_receipt = json.loads(json.dumps(lineage.receipt))
    predecessor_receipt["publication_base"]["sha256"] = anchor_digest
    predecessor_raw = _json_bytes(predecessor_receipt)
    predecessor_digest = _sha(predecessor_raw)
    predecessor_path = history / "receipts" / f"{predecessor_digest}.json"
    _write(predecessor_path, predecessor_raw, 0o640)

    manifest_digest = _sha(lineage.manifest_raw)
    provider_path = history / "manifests/provider" / f"{manifest_digest}.json"
    public_path = history / "manifests/public" / f"{manifest_digest}.json"
    _write(provider_path, lineage.manifest_raw, 0o640)
    _write(public_path, lineage.manifest_raw, 0o640)

    project_id = "11111111-1111-4111-8111-111111111111"
    environment_id = "22222222-2222-4222-8222-222222222222"
    service_id = "33333333-3333-4333-8333-333333333333"
    deployment_id = predecessor_receipt["railway"]["deployment_id"]
    created_at = "2026-08-31T12:00:00Z"
    image_digest = f"sha256:{'9' * 64}"
    topology = {
        "id": project_id,
        "services": {
            "edges": [{"node": {"id": service_id, "name": "palimpsest-publication"}}]
        },
        "environments": {
            "edges": [
                {
                    "node": {
                        "id": environment_id,
                        "serviceInstances": {
                            "edges": [
                                {
                                    "node": {
                                        "serviceId": service_id,
                                        "latestDeployment": {
                                            "id": deployment_id,
                                            "status": "SUCCESS",
                                            "createdAt": created_at,
                                            "meta": {
                                                "imageDigest": image_digest,
                                                "reason": "deploy",
                                            },
                                        },
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }
    topology_raw = _json_bytes(topology)
    topology_digest = _sha(topology_raw)
    topology_path = history / "topologies" / f"{topology_digest}.json"
    _write(topology_path, topology_raw, 0o640)

    installed_paths = {
        "continuity_guard_sha256": lineage.config.installed_continuity_guard,
        "continuity_guard_service_sha256": (
            lineage.config.installed_continuity_guard_service
        ),
        "continuity_guard_timer_sha256": (
            lineage.config.installed_continuity_guard_timer
        ),
        "event_analysis_wrapper_sha256": (
            lineage.config.installed_event_analysis_wrapper
        ),
        "event_analysis_service_sha256": (
            lineage.config.installed_event_analysis_service
        ),
        "event_analysis_publish_dropin_sha256": (
            lineage.config.installed_event_analysis_publish_dropin
        ),
        "publisher_sha256": lineage.config.installed_publisher,
        "transition_helper_sha256": lineage.config.installed_transition_helper,
        "rotation_helper_sha256": lineage.config.installed_rotation_helper,
        "reconciler_sha256": lineage.config.installed_reconciler,
        "watchdog_sha256": lineage.config.installed_watchdog,
        "publisher_service_sha256": lineage.config.installed_publisher_service,
        "watchdog_service_sha256": lineage.config.installed_watchdog_service,
        "watchdog_timer_sha256": lineage.config.installed_watchdog_timer,
    }
    assert set(installed_paths) == SUCCESSOR_INSTALLED_KEYS
    installed = {key: _sha(path.read_bytes()) for key, path in installed_paths.items()}
    target_sha = lineage.release_sha
    _git(
        lineage.config.source_repository,
        "update-ref",
        "refs/remotes/origin/main",
        target_sha,
    )
    receipt_proof = {
        "base_sha": predecessor_receipt["base_sha"],
        "deployment_id": deployment_id,
        "host_deployed_sha": predecessor_receipt["host_deployed_sha"],
        "input_sha256": predecessor_receipt["input_sha256"],
        "manifest_sha256": predecessor_receipt["live_manifest"]["sha256"],
        "path": str(predecessor_path),
        "publication_base_sha256": anchor_digest,
        "release_sha": predecessor_receipt["release_sha"],
        "schema_version": predecessor_receipt["schema_version"],
        "sha256": predecessor_digest,
        "tree_sha256": predecessor_receipt["live_manifest"]["tree_sha256"],
        "wire_generated_at": predecessor_receipt["wire_generated_at"],
    }
    rotation_path = (
        history / "rotations" / (f"1-{target_sha}-{predecessor_digest}.json")
    )
    successor = {
        "anchor": {
            "path": str(anchor_path),
            "schema_version": watchdog.PIN_SCHEMA,
            "sha256": anchor_digest,
            "target_sha": lineage.base_sha,
        },
        "generation": 1,
        "host": {
            "canonical_head": lineage.host_sha,
            "deployed_commit": lineage.host_sha,
        },
        "installed": installed,
        "live": {
            "file_count": json.loads(lineage.manifest_raw)["file_count"],
            "provider_manifest": {
                "bytes": len(lineage.manifest_raw),
                "path": str(provider_path),
                "sha256": manifest_digest,
            },
            "public_manifest": {
                "bytes": len(lineage.manifest_raw),
                "path": str(public_path),
                "sha256": manifest_digest,
            },
            "release_sha": predecessor_receipt["release_sha"],
            "total_bytes": json.loads(lineage.manifest_raw)["total_bytes"],
            "tree_sha256": predecessor_receipt["live_manifest"]["tree_sha256"],
        },
        "origins": {
            "provider": lineage.config.provider_origin,
            "public": lineage.config.public_origin,
        },
        "predecessor": {
            "pin": {
                "generation": 0,
                "path": str(anchor_path),
                "schema_version": watchdog.PIN_SCHEMA,
                "sha256": anchor_digest,
                "target_sha": lineage.base_sha,
            },
            "publication_receipt": receipt_proof,
        },
        "railway": {
            "created_at": created_at,
            "deployment_id": deployment_id,
            "environment_id": environment_id,
            "image_digest": image_digest,
            "project_id": project_id,
            "reason": "deploy",
            "service_id": service_id,
            "topology": {
                "bytes": len(topology_raw),
                "path": str(topology_path),
                "sha256": topology_digest,
            },
        },
        "recorded_at": "2026-08-31T12:05:00Z",
        "rotation_record_path": str(rotation_path),
        "schema_version": watchdog.SUCCESSOR_PIN_SCHEMA,
        "status": "verified",
        "target": {"base_sha": target_sha, "public_main_sha": target_sha},
    }
    successor_raw = _json_bytes(successor)
    rotation_record = {
        "generation": 1,
        "next_pin": successor,
        "next_pin_sha256": _sha(successor_raw),
        "predecessor_pin_sha256": anchor_digest,
        "predecessor_receipt_sha256": predecessor_digest,
        "recorded_at": successor["recorded_at"],
        "schema_version": watchdog.ROTATION_SCHEMA,
        "status": "verified",
        "target_base_sha": target_sha,
    }
    _write(rotation_path, _json_bytes(rotation_record), 0o640)
    _write(lineage.pin_path, successor_raw, 0o640)
    monkeypatch.setattr(
        watchdog,
        "_validate_v2_receipt",
        lambda *_args, **_kwargs: None,
    )

    pin, raw, authenticated = watchdog._validate_pin(lineage.config)

    assert raw == successor_raw
    assert pin["target"]["base_sha"] == target_sha
    assert set(authenticated) == {anchor_digest, _sha(successor_raw)}


def test_deep_successor_corruption_is_rejected_recursively(
    lineage: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_raw = lineage.pin_path.read_bytes()
    anchor = json.loads(anchor_raw)
    anchor_digest = _sha(anchor_raw)
    anchor_target = anchor["target"]["base_sha"]
    host = {
        "canonical_head": anchor["previous"]["canonical_head"],
        "deployed_commit": anchor["previous"]["deployed_commit"],
    }
    history = lineage.config.control_root / "base-rotation-history"
    forged_anchor_digest = "f" * 64

    def successor(
        *,
        generation: int,
        target: str,
        predecessor_generation: int,
        predecessor_digest: str,
        predecessor_schema: str,
        predecessor_target: str,
    ) -> dict[str, Any]:
        return {
            "anchor": {
                "path": str(history / "pins" / f"{anchor_digest}.json"),
                "schema_version": watchdog.PIN_SCHEMA,
                "sha256": anchor_digest,
                "target_sha": anchor_target,
            },
            "generation": generation,
            "host": host,
            "installed": {key: "1" * 64 for key in SUCCESSOR_INSTALLED_KEYS},
            "live": {},
            "origins": {
                "provider": lineage.config.provider_origin,
                "public": lineage.config.public_origin,
            },
            "predecessor": {
                "pin": {
                    "generation": predecessor_generation,
                    "path": str(history / "pins" / f"{predecessor_digest}.json"),
                    "schema_version": predecessor_schema,
                    "sha256": predecessor_digest,
                    "target_sha": predecessor_target,
                },
                "publication_receipt": {},
            },
            "railway": {},
            "recorded_at": "2026-08-31T12:05:00Z",
            "rotation_record_path": str(
                history / "rotations" / f"{generation}-{target}-proof.json"
            ),
            "schema_version": watchdog.SUCCESSOR_PIN_SCHEMA,
            "status": "verified",
            "target": {"base_sha": target, "public_main_sha": target},
        }

    generation_one = successor(
        generation=1,
        target="d" * 40,
        predecessor_generation=0,
        predecessor_digest=forged_anchor_digest,
        predecessor_schema=watchdog.PIN_SCHEMA,
        predecessor_target=anchor_target,
    )
    generation_one_raw = _json_bytes(generation_one)
    generation_one_digest = _sha(generation_one_raw)
    generation_two = successor(
        generation=2,
        target="e" * 40,
        predecessor_generation=1,
        predecessor_digest=generation_one_digest,
        predecessor_schema=watchdog.SUCCESSOR_PIN_SCHEMA,
        predecessor_target=generation_one["target"]["base_sha"],
    )
    generation_two_raw = _json_bytes(generation_two)

    artifacts = {
        anchor_digest: anchor_raw,
        forged_anchor_digest: anchor_raw,
        generation_one_digest: generation_one_raw,
    }

    def read_artifact(
        _config: Any,
        *,
        digest: str,
        **_kwargs: Any,
    ) -> bytes:
        return artifacts[digest]

    monkeypatch.setattr(watchdog, "_read_rotation_artifact", read_artifact)

    with pytest.raises(
        watchdog.WatchdogError,
        match="first successor does not extend the incident pin",
    ):
        watchdog._validate_successor_pin_document(
            lineage.config,
            generation_two,
            generation_two_raw,
            history_root=history,
            active=False,
            seen=set(),
        )


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(
            lambda fixture: fixture.receipt_path.write_bytes(
                b'{"schema_version":"one","schema_version":"two"}\n'
            ),
            id="duplicate-receipt-key",
        ),
        pytest.param(
            lambda fixture: fixture.bundle_path.write_bytes(
                fixture.bundle_path.read_bytes() + b"corruption"
            ),
            id="corrupt-bundle-bytes",
        ),
        pytest.param(
            lambda fixture: fixture.metadata_path.write_bytes(b'{"status":"bad"}\n'),
            id="corrupt-bundle-metadata",
        ),
        pytest.param(
            lambda fixture: fixture.manifest_path.write_bytes(
                fixture.manifest_path.read_bytes() + b"corruption"
            ),
            id="corrupt-release-manifest-anchor",
        ),
        pytest.param(
            lambda fixture: fixture.pin_path.write_bytes(
                fixture.pin_path.read_bytes() + b" "
            ),
            id="changed-root-pin",
        ),
        pytest.param(
            lambda fixture: Path(
                fixture.receipt["candidate"]["archive_path"]
            ).write_bytes(
                Path(fixture.receipt["candidate"]["archive_path"]).read_bytes()
                + b"corruption"
            ),
            id="corrupt-candidate-archive",
        ),
    ],
)
def test_malformed_receipt_or_corrupt_durable_proof_fails_closed(
    lineage: Fixture,
    corrupt: Callable[[Fixture], None],
) -> None:
    corrupt(lineage)

    with pytest.raises(watchdog.WatchdogError):
        lineage.validate()


def test_corrupt_rollback_evidence_fails_closed(lineage: Fixture) -> None:
    candidate_path = Path(lineage.receipt["candidate"]["archive_path"])
    candidate = json.loads(candidate_path.read_bytes())
    provider_path = Path(candidate["rollback_evidence"]["provider_manifest"]["path"])
    provider_path.write_bytes(provider_path.read_bytes() + b"corruption")

    with pytest.raises(watchdog.WatchdogError, match="provider rollback manifest"):
        lineage.validate()


def test_unresolved_candidate_journal_is_reported(lineage: Fixture) -> None:
    archived = Path(lineage.receipt["candidate"]["archive_path"])
    candidate = json.loads(archived.read_bytes())
    candidate["input_sha256"] = "6" * 64
    candidate["message"] = (
        f"palimpsest-hetzner-{lineage.release_sha[:12]}-{'6' * 12}-"
        f"{candidate['submission_id']}"
    )
    _write(
        lineage.config.pending_candidate,
        _json_bytes(candidate),
        0o600,
    )

    _proof, candidate_status = lineage.validate()

    assert candidate_status["status"] == "mutation_unresolved"
    assert candidate_status["data_hold"] is True
    assert candidate_status["release_sha"] == lineage.release_sha


def test_consumed_pending_candidate_remains_data_hold_until_cleanup(
    lineage: Fixture,
) -> None:
    archived = Path(lineage.receipt["candidate"]["archive_path"])
    _write(lineage.config.pending_candidate, archived.read_bytes(), 0o600)

    _proof, candidate_status = lineage.validate()

    assert candidate_status["status"] == "consumed_pending_cleanup"
    assert candidate_status["data_hold"] is True
    assert watchdog._outcome_status(problems=[], data_hold=True) == "DATA HOLD"


def test_candidate_submission_identity_is_full_entropy_and_message_bound(
    lineage: Fixture,
) -> None:
    archived = Path(lineage.receipt["candidate"]["archive_path"])
    candidate = json.loads(archived.read_bytes())
    candidate["submission_id"] = "short"
    candidate["message"] = (
        f"palimpsest-hetzner-{lineage.release_sha[:12]}-"
        f"{candidate['input_sha256'][:12]}-short"
    )
    _write(lineage.config.pending_candidate, _json_bytes(candidate), 0o600)

    with pytest.raises(watchdog.WatchdogError, match="submission ID"):
        lineage.validate()


def test_outcome_status_never_cosmetically_hides_data_hold() -> None:
    problem = [{"check": "publication/candidate", "detail": "unresolved"}]

    assert watchdog._outcome_status(problems=[], data_hold=False) == "healthy"
    assert watchdog._outcome_status(problems=problem, data_hold=False) == "degraded"
    assert watchdog._outcome_status(problems=problem, data_hold=True) == "DATA HOLD"


def _write_data_hold(
    lineage: Fixture,
    *,
    attempted: bool,
) -> tuple[dict[str, Any], Path | None]:
    candidate_path = Path(lineage.receipt["candidate"]["archive_path"])
    candidate_raw = candidate_path.read_bytes()
    candidate = json.loads(candidate_raw)
    candidate_digest = _sha(candidate_raw)
    topology = candidate["rollback_evidence"]["topology"]
    predecessor = candidate["predecessor"]
    attempt_path: Path | None = None
    attempt_digest: str | None = None
    if attempted:
        attempt_root = lineage.config.state_root / "rollback-attempts"
        attempt_root.mkdir(mode=0o700)
        attempt_path = attempt_root / f"{candidate_digest}.json"
        topology_root = lineage.config.state_root / "rollback-attempt-topologies"
        topology_root.mkdir(mode=0o700)
        topology_path = topology_root / f"{candidate_digest}.json"
        topology_document = {
            "id": topology["project_id"],
            "services": {
                "edges": [
                    {
                        "node": {
                            "id": topology["service_id"],
                            "name": "palimpsest-publication",
                        }
                    }
                ]
            },
            "environments": {
                "edges": [
                    {
                        "node": {
                            "id": topology["environment_id"],
                            "serviceInstances": {
                                "edges": [
                                    {
                                        "node": {
                                            "serviceId": topology["service_id"],
                                            "latestDeployment": {
                                                "id": lineage.receipt["railway"][
                                                    "deployment_id"
                                                ],
                                                "status": "SUCCESS",
                                                "createdAt": "2026-08-30T12:02:00Z",
                                                "meta": {
                                                    "imageDigest": f"sha256:{'6' * 64}",
                                                    "reason": "deploy",
                                                },
                                            },
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            },
        }
        topology_raw = _json_bytes(topology_document)
        _write(topology_path, topology_raw, 0o600)
        attempt = {
            "candidate_deployment_id": lineage.receipt["railway"]["deployment_id"],
            "candidate_journal_sha256": candidate_digest,
            "candidate_topology_path": str(topology_path),
            "created_at": "2026-08-30T12:06:00.123456Z",
            "predecessor_deployment_id": predecessor["deployment_id"],
            "schema_version": watchdog.ROLLBACK_ATTEMPT_SCHEMA,
            "status": "mutation_may_execute",
            "topology_sha256": _sha(topology_raw),
        }
        attempt_raw = _json_bytes(attempt)
        _write(attempt_path, attempt_raw, 0o600)
        attempt_digest = _sha(attempt_raw)

    hold = {
        "candidate": {
            "archive_path": str(candidate_path),
            "journal_sha256": candidate_digest,
            "message": candidate["message"],
            "release_sha": candidate["release_sha"],
        },
        "predecessor": {
            "deployment_id": predecessor["deployment_id"],
            "image_digest": topology["image_digest"],
            "manifest_sha256": predecessor["manifest_sha256"],
            "receipt_sha256": predecessor["receipt_sha256"],
            "topology_sha256": topology["sha256"],
        },
        "reason_code": "rollback_restore_unproven",
        "recorded_at": "2026-08-30T12:07:00Z",
        "rollback": {
            "attempt_path": str(attempt_path) if attempt_path is not None else None,
            "attempt_sha256": attempt_digest,
            "attempted": attempted,
        },
        "schema_version": watchdog.DATA_HOLD_SCHEMA,
        "status": "DATA HOLD",
    }
    _write(lineage.config.data_hold, _json_bytes(hold), 0o640)
    return hold, attempt_path


@pytest.mark.parametrize("attempted", [False, True])
def test_root_data_hold_is_strict_and_surfaces_rollback_state(
    lineage: Fixture,
    attempted: bool,
) -> None:
    _write_data_hold(lineage, attempted=attempted)

    status = watchdog.validate_data_hold(lineage.config)

    assert status["status"] == "DATA HOLD"
    assert status["reason_code"] == "rollback_restore_unproven"
    assert status["candidate_release_sha"] == lineage.release_sha
    assert status["rollback_attempted"] is attempted


def test_root_data_hold_rejects_duplicate_keys(lineage: Fixture) -> None:
    _write_data_hold(lineage, attempted=False)
    _write(
        lineage.config.data_hold,
        b'{"schema_version":"one","schema_version":"two"}\n',
        0o640,
    )

    with pytest.raises(watchdog.WatchdogError, match="duplicate key"):
        watchdog.validate_data_hold(lineage.config)


def test_root_data_hold_rejects_noncanonical_rollback_attempt_path(
    lineage: Fixture,
) -> None:
    hold, _attempt_path = _write_data_hold(lineage, attempted=True)
    hold["rollback"]["attempt_path"] = str(
        lineage.config.state_root / "rollback-attempts" / "other.json"
    )
    _write(lineage.config.data_hold, _json_bytes(hold), 0o640)

    with pytest.raises(watchdog.WatchdogError, match="path is not canonical"):
        watchdog.validate_data_hold(lineage.config)


def test_root_data_hold_cannot_hide_an_existing_rollback_attempt(
    lineage: Fixture,
) -> None:
    hold, _attempt_path = _write_data_hold(lineage, attempted=True)
    hold["rollback"] = {
        "attempt_path": None,
        "attempt_sha256": None,
        "attempted": False,
    }
    _write(lineage.config.data_hold, _json_bytes(hold), 0o640)

    with pytest.raises(
        watchdog.WatchdogError,
        match="durable artifact exists",
    ):
        watchdog.validate_data_hold(lineage.config)


def test_root_data_hold_rejects_changed_rollback_attempt_topology(
    lineage: Fixture,
) -> None:
    hold, attempt_path = _write_data_hold(lineage, attempted=True)
    assert attempt_path is not None
    attempt = json.loads(attempt_path.read_bytes())
    topology_path = Path(attempt["candidate_topology_path"])
    topology = json.loads(topology_path.read_bytes())
    topology["environments"]["edges"][0]["node"]["serviceInstances"]["edges"][0][
        "node"
    ]["latestDeployment"]["id"] = watchdog.INCIDENT_DEPLOYMENT_ID
    topology_raw = _json_bytes(topology)
    _write(topology_path, topology_raw, 0o600)
    attempt["topology_sha256"] = _sha(topology_raw)
    attempt_raw = _json_bytes(attempt)
    _write(attempt_path, attempt_raw, 0o600)
    hold["rollback"]["attempt_sha256"] = _sha(attempt_raw)
    _write(lineage.config.data_hold, _json_bytes(hold), 0o640)

    with pytest.raises(
        watchdog.WatchdogError,
        match="topology identity is invalid",
    ):
        watchdog.validate_data_hold(lineage.config)


def test_root_data_hold_rejects_corrupt_rollback_attempt_topology(
    lineage: Fixture,
) -> None:
    _hold, attempt_path = _write_data_hold(lineage, attempted=True)
    assert attempt_path is not None
    attempt = json.loads(attempt_path.read_bytes())
    topology_path = Path(attempt["candidate_topology_path"])
    _write(topology_path, b'{"id":"corrupt"}\n', 0o600)

    with pytest.raises(
        watchdog.WatchdogError,
        match="topology digest changed",
    ):
        watchdog.validate_data_hold(lineage.config)


def test_main_reports_valid_root_hold_as_explicit_data_hold(
    lineage: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold, _attempt_path = _write_data_hold(lineage, attempted=False)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(watchdog, "default_lineage_config", lambda: lineage.config)
    monkeypatch.setattr(watchdog, "_load_prior_chain_checkpoint", lambda: None)
    monkeypatch.setattr(
        watchdog,
        "load_json",
        lambda _path: {
            "events": [{}],
            "generated_at": "2026-08-30T12:00:00Z",
            "recorded_at": "2026-08-30T12:00:00Z",
            "succeeded": 1,
        },
    )
    monkeypatch.setattr(watchdog, "check_age", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(
        watchdog,
        "validate_lineage",
        lambda *_args, **_kwargs: (
            {"chain_checkpoint": None},
            {"status": "none"},
        ),
    )
    monkeypatch.setattr(
        watchdog,
        "validate_data_hold",
        lambda _config: {
            "status": "DATA HOLD",
            "reason_code": hold["reason_code"],
            "candidate_release_sha": lineage.release_sha,
        },
    )
    monkeypatch.setattr(watchdog, "command", lambda *_args: "active")
    monkeypatch.setattr(watchdog, "unit_property", lambda *_args: "success")
    monkeypatch.setattr(
        watchdog,
        "fetch_json_url",
        lambda _url: {
            "schema_version": "palimpsest.regional-analysis.v1",
            "revision_id": "revision",
            "coverage": {"event_count": 1, "source_count": 1},
            "wire": {"generated_at": "2026-08-30T12:00:00Z"},
        },
    )
    monkeypatch.setattr(watchdog, "_write_status", captured.update)

    assert watchdog.main([]) == 2
    assert captured["status"] == "DATA HOLD"
    assert captured["data_hold"] is True
    assert captured["checks"]["data_hold"]["reason_code"] == hold["reason_code"]
    assert any(
        problem["check"] == "publication/data_hold"
        and hold["reason_code"] in problem["detail"]
        for problem in captured["problems"]
    )


def test_provider_and_public_manifests_must_be_byte_identical(
    lineage: Fixture,
) -> None:
    def fetch(origin: str) -> bytes:
        if origin == lineage.config.provider_origin:
            return lineage.manifest_raw
        return lineage.manifest_raw + b" "

    with pytest.raises(
        watchdog.WatchdogError,
        match="provider and public live manifests are not byte-identical",
    ):
        watchdog.validate_lineage(lineage.config, fetch_manifest=fetch)


def test_byte_identical_live_manifests_must_match_pre_mutation_anchor(
    lineage: Fixture,
) -> None:
    changed = json.loads(lineage.manifest_raw)
    changed["total_bytes"] += 1
    changed_raw = _json_bytes(changed)

    with pytest.raises(
        watchdog.WatchdogError,
        match="pre-mutation anchor",
    ):
        watchdog.validate_lineage(
            lineage.config,
            fetch_manifest=lambda _origin: changed_raw,
        )


def _predecessor(
    lineage: Fixture,
    raw: bytes,
    receipt: dict[str, Any],
    archive_path: Path,
) -> dict[str, Any]:
    railway = receipt["railway"]
    if receipt["schema_version"] == watchdog.V1_SCHEMA:
        manifest_sha256 = watchdog.INCIDENT_MANIFEST_SHA256
        tree_sha256 = watchdog.INCIDENT_TREE_SHA256
    else:
        manifest_sha256 = receipt["live_manifest"]["sha256"]
        tree_sha256 = receipt["live_manifest"]["tree_sha256"]
    return {
        "receipt_sha256": _sha(raw),
        "archive_path": str(archive_path),
        "schema_version": receipt["schema_version"],
        "base_sha": receipt["base_sha"],
        "release_sha": receipt["release_sha"],
        "deployment_id": railway["deployment_id"],
        "input_sha256": receipt["input_sha256"],
        "wire_generated_at": receipt["wire_generated_at"],
        "manifest_sha256": manifest_sha256,
        "tree_sha256": tree_sha256,
    }


def test_receipt_chain_scales_past_129_generations_and_checkpoints(
    lineage: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_root = lineage.config.state_root / "receipts"
    previous_raw = V1_RECEIPT
    previous_receipt = json.loads(V1_RECEIPT)
    previous_path = archive_root / f"{_sha(previous_raw)}.json"
    latest_raw = b""
    latest_receipt: dict[str, Any] = {}

    for generation in range(1, 141):
        latest_receipt = json.loads(json.dumps(lineage.receipt))
        latest_receipt["input_sha256"] = f"{generation:064x}"
        latest_receipt["predecessor"] = _predecessor(
            lineage,
            previous_raw,
            previous_receipt,
            previous_path,
        )
        latest_raw = _json_bytes(latest_receipt)
        if generation < 140:
            previous_path = archive_root / f"{_sha(latest_raw)}.json"
            _write(previous_path, latest_raw, 0o600)
            previous_raw = latest_raw
            previous_receipt = latest_receipt

    _write(lineage.receipt_path, latest_raw, 0o600)
    bundle_modes: list[bool] = []

    def validate_bundle(
        _config: Any,
        _bundle: dict[str, Any],
        *,
        base_sha: str,
        release_sha: str,
        verify_git: bool,
    ) -> None:
        assert base_sha == lineage.base_sha
        assert release_sha == lineage.release_sha
        bundle_modes.append(verify_git)

    monkeypatch.setattr(watchdog, "_validate_bundle", validate_bundle)
    monkeypatch.setattr(
        watchdog,
        "_validate_candidate_archive",
        lambda *_args, **_kwargs: None,
    )
    started = time.monotonic()
    proof, _candidate = lineage.validate()
    bootstrap_seconds = time.monotonic() - started
    checkpoint = proof["chain_checkpoint"]

    assert checkpoint["generation_count"] == 140
    assert checkpoint["validation_mode"] == "bootstrap"
    assert bundle_modes.count(True) == 1
    assert bundle_modes.count(False) == 139
    assert bootstrap_seconds < 5.0

    latest_path = archive_root / f"{_sha(latest_raw)}.json"
    _write(latest_path, latest_raw, 0o600)
    next_receipt = json.loads(json.dumps(latest_receipt))
    next_receipt["input_sha256"] = f"{141:064x}"
    next_receipt["predecessor"] = _predecessor(
        lineage,
        latest_raw,
        latest_receipt,
        latest_path,
    )
    lineage.receipt = next_receipt
    lineage.rewrite_receipt()
    bundle_modes.clear()

    extension, _candidate = watchdog.validate_lineage(
        lineage.config,
        fetch_manifest=lambda _origin: lineage.manifest_raw,
        chain_checkpoint=checkpoint,
    )

    next_checkpoint = extension["chain_checkpoint"]
    assert next_checkpoint["generation_count"] == 141
    assert next_checkpoint["validation_mode"] == "checkpoint_extension"
    assert bundle_modes == [True, False]

    bundle_modes.clear()
    same_head, _candidate = watchdog.validate_lineage(
        lineage.config,
        fetch_manifest=lambda _origin: lineage.manifest_raw,
        chain_checkpoint=next_checkpoint,
    )
    assert same_head["chain_checkpoint"]["generation_count"] == 141
    assert same_head["chain_checkpoint"]["validation_mode"] == "checkpoint_same_head"
    assert bundle_modes == [True]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("head_publication_base_sha256", "a" * 64, "different anchor"),
        ("head_receipt_sha256", "b" * 64, "does not extend"),
        ("head_release_sha", "c" * 40, "head release changed"),
    ],
)
def test_forged_or_stale_chain_checkpoint_fails_closed(
    lineage: Fixture,
    field: str,
    value: str,
    message: str,
) -> None:
    proof, _candidate = lineage.validate()
    checkpoint = dict(proof["chain_checkpoint"])
    checkpoint[field] = value

    with pytest.raises(watchdog.WatchdogError, match=message):
        watchdog.validate_lineage(
            lineage.config,
            fetch_manifest=lambda _origin: lineage.manifest_raw,
            chain_checkpoint=checkpoint,
        )


def test_prior_status_round_trips_the_strict_chain_checkpoint(
    lineage: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    proof, _candidate = lineage.validate()
    checkpoint = proof["chain_checkpoint"]
    output = tmp_path / "watchdog-status.json"
    _write(
        output,
        _json_bytes(
            {
                "schema_version": "palimpsest.direct-watchdog.v2",
                "checks": {"publication": {"chain_checkpoint": checkpoint}},
            }
        ),
        0o600,
    )
    monkeypatch.setattr(watchdog, "OUTPUT_PATH", output)

    assert watchdog._load_prior_chain_checkpoint() == checkpoint


def test_checkpoint_extends_across_authenticated_pin_digest_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_pin_sha256 = "1" * 64
    new_pin_sha256 = "2" * 64
    old_release = "3" * 40
    new_release = "4" * 40
    old_receipt = {
        "predecessor": {"kind": "unused"},
        "publication_base": {"sha256": old_pin_sha256},
        "release_sha": old_release,
        "schema_version": watchdog.V2_SCHEMA,
    }
    old_raw = _json_bytes(old_receipt)
    current_receipt = {
        "predecessor": {"kind": "old-head"},
        "publication_base": {"sha256": new_pin_sha256},
        "release_sha": new_release,
        "schema_version": watchdog.V2_SCHEMA,
    }
    current_raw = _json_bytes(current_receipt)
    validated_pin_digests: list[str] = []

    def read_predecessor(
        _config: Any,
        predecessor: dict[str, Any],
        *,
        pin: dict[str, Any],
    ) -> tuple[str, bytes, dict[str, Any]]:
        assert predecessor == {"kind": "old-head"}
        assert pin == {"name": "new"}
        return _sha(old_raw), old_raw, old_receipt

    def validate_receipt(
        _config: Any,
        receipt: dict[str, Any],
        *,
        pin: dict[str, Any],
        pin_sha256: str,
        current: bool,
        verify_bundle_git: bool,
    ) -> None:
        assert receipt is old_receipt
        assert pin == {"name": "old"}
        assert current is False
        assert verify_bundle_git is False
        validated_pin_digests.append(pin_sha256)

    monkeypatch.setattr(watchdog, "_read_predecessor_archive", read_predecessor)
    monkeypatch.setattr(watchdog, "_validate_v2_receipt", validate_receipt)
    checkpoint = {
        "anchor_receipt_sha256": watchdog.INCIDENT_RECEIPT_SHA256,
        "generation_count": 7,
        "head_receipt_sha256": _sha(old_raw),
        "head_release_sha": old_release,
        "pin_sha256": old_pin_sha256,
        "schema_version": watchdog.LEGACY_CHAIN_CHECKPOINT_SCHEMA,
        "validation_mode": "checkpoint_same_head",
        "verified_at": "2026-08-31T12:00:00Z",
    }

    next_checkpoint = watchdog._validate_receipt_chain(
        object(),
        current_raw,
        current_receipt,
        authenticated_pins={
            old_pin_sha256: {"name": "old"},
            new_pin_sha256: {"name": "new"},
        },
        checkpoint=checkpoint,
    )

    assert next_checkpoint["schema_version"] == watchdog.CHAIN_CHECKPOINT_SCHEMA
    assert next_checkpoint["generation_count"] == 8
    assert next_checkpoint["head_publication_base_sha256"] == new_pin_sha256
    assert next_checkpoint["validation_mode"] == "checkpoint_extension"
    assert validated_pin_digests == [old_pin_sha256]


def test_rotation_interlock_keeps_timers_live_and_one_lock_covers_install_to_pin() -> (
    None
):
    source = ROTATION_HELPER_PATH.read_text(encoding="utf-8")
    rotation = source[source.index("def perform_rotation") :]

    lock = rotation.index("_acquire_lock(")
    intent = rotation.index("_persist_rotation_intent(")
    install = rotation.index("_install_target_artifacts(")
    replace_pin = rotation.index("_atomic_replace_pin(")

    assert lock < intent < install < replace_pin
    assert "systemctl stop" not in rotation
    assert "systemctl disable" not in rotation
    assert "maintenance" not in rotation.lower()
    assert "daemon-reload" in source


def test_tracked_service_exposes_only_the_base_repository_from_home() -> None:
    service = (
        ROOT / "ops" / "systemd" / "palimpsest-direct-watchdog.service"
    ).read_text(encoding="utf-8")
    timer = (ROOT / "ops" / "systemd" / "palimpsest-direct-watchdog.timer").read_text(
        encoding="utf-8"
    )

    assert "ProtectHome=tmpfs" in service
    assert "BindReadOnlyPaths=/home/palimpsest/palimpsest" in service
    assert "ReadOnlyPaths=-/etc/palimpsest/railway-publication-base.json" in service
    assert (
        "ReadOnlyPaths=-/etc/palimpsest/railway-publication-data-hold.json" in service
    )
    assert "MemoryDenyWriteExecute=true" in service
    assert "OnCalendar=*:0/5" in timer
    assert "Persistent=true" in timer
