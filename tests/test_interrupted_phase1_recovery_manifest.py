from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ops" / "release-recovery" / "2026-08-25-interrupted-phase1.json"
VERIFIER = ROOT / "ops" / "release-recovery" / "verify_interrupted_phase1_manifest.py"
EXPECTED_MANIFEST_SHA256 = (
    "f21ffb99a29902bc849c4c7e0ea0317720f24fa9adcef2068cd0ff40341cf535"
)


def _verify(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_canonical(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )


def _load_verifier_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "recovery_manifest_verifier", VERIFIER
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewed_interrupted_phase1_manifest_passes_strict_verifier() -> None:
    result = _verify(MANIFEST)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == (
        f"validated interrupted Phase 1 recovery manifest: {EXPECTED_MANIFEST_SHA256}\n"
    )


def test_manifest_binds_authority_and_forward_recovery_constraint() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert document["authority"] == {
        "failed_target_commit": "138a9eb323857ba91944fc04d0ccfabb653e7f24",
        "prior_checkout_commit": "7d05ecca47b20d8cf092a513a0db0390435f363f",
        "prior_deployed_commit": "95ea01d1a394fe219d64d3dce6b105296bce309a",
    }
    assert document["recovery_target_constraints"] == {
        "must_be_descendant_of": "138a9eb323857ba91944fc04d0ccfabb653e7f24",
        "must_be_reviewed": True,
        "must_contain_manifest_path": (
            "ops/release-recovery/2026-08-25-interrupted-phase1.json"
        ),
    }


def test_manifest_preserves_the_hybrid_image_boundary() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    boundary = document["observed_safe_boundary"]
    containers = {item["service"]: item for item in boundary["application_containers"]}

    assert containers["api"]["state"] == "running"
    assert containers["api"]["image_index_digest"] == (
        "sha256:c798010776a5070efbb9f54163f191e71cecc26a08f1ec3d77b99995a419ea19"
    )
    for service in ("worker", "worker-collectors", "worker-warehouse"):
        assert containers[service]["state"] == "exited"
        assert containers[service]["image_index_digest"] == (
            "sha256:e5eb3cc44e2e129031c7fa781e3f8dabc46152a8593afb9d926a24a786e1eb3e"
        )
    assert boundary["local_application_tag"] == {
        "index_digest": (
            "sha256:919104857c30a9a939413e114f5b8d99edd9c2d10828646c2d7b0b2d49f8398a"
        ),
        "name": "palimpsest/app:local",
        "platform_manifest_digest": (
            "sha256:8df7761092abf82f2e6da8448453c042556afd42549276fe62be02e6919f381b"
        ),
        "revision": "7d05ecca47b20d8cf092a513a0db0390435f363f",
        "trusted_for_recovery": False,
    }


def test_manifest_binds_the_unadvanced_installed_bundle_boundary() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    boundary = document["observed_safe_boundary"]

    assert boundary["installed_bundles"] == [
        {
            "current_symlink_path": path,
            "manifest_sha256": manifest_sha256,
            "resolved_target_path": path.removesuffix("/current")
            + "/95ea01d1a394fe219d64d3dce6b105296bce309a",
            "revision": "95ea01d1a394fe219d64d3dce6b105296bce309a",
        }
        for path, manifest_sha256 in (
            (
                "/usr/local/libexec/palimpsest-analysis/current",
                "8864db5c14dc8d834b9eac51be5adb3fb19381fa240c30c38159f1305bca542a",
            ),
            (
                "/usr/local/libexec/palimpsest-network-lane/current",
                "0783cf1c90b4b3ae399b580e5df0e6568fb588b042605d110ee585d7b6169d66",
            ),
            (
                "/usr/local/libexec/palimpsest-common-crawl/current",
                "9556ba2245bda43a70aec4a3149e72748da60f9d3d19293b1b1a8c3eadb18138",
            ),
            (
                "/usr/local/libexec/palimpsest-public-osint-sync/current",
                "1868dc67d5f0d2ced080476d95fd21da69a75191ef0977dcd712f43888748a92",
            ),
            (
                "/usr/local/libexec/palimpsest-node-offsite/current",
                "8877cfc0cef4928a43fdf22129fa706746e9717307d22266523c00b69bb2c87d",
            ),
        )
    ]
    assert boundary["installed_controller_boundary"] == {
        "absent_paths": [
            "/opt/palimpsest/ops/release/observer_release_gate.py",
            "/opt/palimpsest/ops/release/celery_release_gate.py",
            "/opt/palimpsest/ops/release/recover_deployment_snapshots.py",
            "/etc/palimpsest/observer-release-policy.json",
            "/opt/palimpsest/ops/watchdog/palimpsest_freshness_watchdog.py",
        ],
        "present_files": [
            {
                "path": "/opt/palimpsest/ops/witness/palimpsest_witness.py",
                "sha256": (
                    "ea60a918e2eb4e74dce214f2b65284a7a23e3af8932c2dd554d88834d27d2afa"
                ),
            }
        ],
    }


def test_manifest_binds_stateful_infrastructure_containers() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert document["observed_safe_boundary"]["compose_environment_sha256"] == (
        "2ce97c2f94ce93336b592e1ddee78cfdbec1e8b19d35b39faab6ac069d332c95"
    )
    assert document["observed_safe_boundary"]["infrastructure_containers"] == [
        {
            "container_id": (
                "b75c70b96bdb02cd6db7470520b08dd468f0e1c64a77ca32f26a49e922addee3"
            ),
            "image_id": (
                "sha256:e013e867e712fec275706a6c51c966f0bb0c93cfa8f51000f85a15f9865a28cb"
            ),
            "service": "postgres",
            "state": "running",
        },
        {
            "container_id": (
                "011a5e9d66dd481e42c574aee4475d3f4889eecce377679b988bedc492738716"
            ),
            "image_id": (
                "sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"
            ),
            "service": "redis",
            "state": "running",
        },
    ]


def test_manifest_binds_exact_compose_project_scope() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert document["observed_safe_boundary"]["compose_scope"] == {
        "config_files": (
            "/home/palimpsest/palimpsest/ops/docker/docker-compose.prod.yml"
        ),
        "project": "palimpsest",
        "working_dir": "/home/palimpsest/palimpsest/ops/docker",
    }


def test_verifier_rejects_changed_compose_project_scope(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["observed_safe_boundary"]["compose_scope"]["project"] = "shadow"
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "Compose project scope is invalid" in result.stderr


def test_verifier_rejects_reordered_infrastructure_containers(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["observed_safe_boundary"]["infrastructure_containers"].reverse()
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "infrastructure container boundary is invalid" in result.stderr


def test_verifier_rejects_partially_advanced_bundle_revision(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["observed_safe_boundary"]["installed_bundles"][0]["revision"] = (
        "138a9eb323857ba91944fc04d0ccfabb653e7f24"
    )
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "installed bundle boundary is not the prior deployment" in result.stderr


def test_verifier_rejects_wrong_bundle_resolved_target(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["observed_safe_boundary"]["installed_bundles"][0][
        "resolved_target_path"
    ] = "/usr/local/libexec/palimpsest-analysis/unreviewed"
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "installed bundle resolved-target relation is invalid" in result.stderr


def test_verifier_rejects_unknown_nested_fields(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["failed_attempt"]["snapshot_ceiling"]["operator_override"] = True
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "unknown fields: ['operator_override']" in result.stderr


def test_verifier_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    raw = MANIFEST.read_text(encoding="utf-8")
    marker = '  "incident_id": "2026-08-25-interrupted-phase1",\n'
    candidate = tmp_path / MANIFEST.name
    candidate.write_text(raw.replace(marker, marker + marker, 1), encoding="utf-8")

    result = _verify(candidate)

    assert result.returncode == 1
    assert "duplicate JSON key: incident_id" in result.stderr


def test_verifier_rejects_changed_incident_evidence(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["failed_attempt"]["invalid_gate_artifacts"][0]["sha256"] = "0" * 64
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "manifest SHA-256 does not match the reviewed bytes" in result.stderr


def test_verifier_rejects_noncanonical_json_bytes(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    candidate = tmp_path / MANIFEST.name
    candidate.write_text(json.dumps(document), encoding="utf-8")

    result = _verify(candidate)

    assert result.returncode == 1
    assert "canonical sorted, indented JSON bytes" in result.stderr


def test_verifier_rejects_manifest_symlink(tmp_path: Path) -> None:
    candidate = tmp_path / MANIFEST.name
    candidate.symlink_to(MANIFEST)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "cannot open manifest without following symlinks" in result.stderr


def test_verifier_rejects_manifest_with_multiple_hard_links(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(MANIFEST.read_bytes())
    candidate = tmp_path / MANIFEST.name
    os.link(source, candidate)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "manifest must have exactly one hard link" in result.stderr


def test_verifier_rejects_trailing_growth_during_descriptor_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / MANIFEST.name
    candidate.write_bytes(MANIFEST.read_bytes())
    verifier = _load_verifier_module()
    real_read = os.read
    appended = False

    def append_after_first_read(descriptor: int, count: int) -> bytes:
        nonlocal appended
        chunk = real_read(descriptor, count)
        if chunk and not appended:
            with candidate.open("ab") as handle:
                handle.write(b"x")
            appended = True
        return chunk

    monkeypatch.setattr(verifier.os, "read", append_after_first_read)

    with pytest.raises(verifier.ManifestError, match="trailing growth"):
        verifier.validate_manifest(candidate)
