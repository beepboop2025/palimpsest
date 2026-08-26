from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / "ops" / "release-recovery"
MANIFEST = RECOVERY / "2026-08-25-common-crawl-bind-alias-retry.json"
VERIFIER = RECOVERY / "verify_common_crawl_bind_alias_retry_manifest.py"
API_MANIFEST = RECOVERY / "2026-08-25-api-readiness-retry.json"
ORIGINAL_MANIFEST = RECOVERY / "2026-08-25-interrupted-phase1.json"
EXPECTED_MANIFEST_SHA256 = (
    "62dd4970775c4acc840649f4531c50f73dc73906ad816d7bf45c49e1f323d834"
)


def _verify(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_pretty(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )


def _write_compact(path: Path, document: object) -> str:
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    path.write_bytes(payload)
    path.chmod(0o400)
    return hashlib.sha256(payload).hexdigest()


def _load_verifier_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "common_crawl_bind_alias_retry_verifier", VERIFIER
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewed_common_crawl_retry_manifest_passes_exact_verifier() -> None:
    result = _verify(MANIFEST)

    assert hashlib.sha256(MANIFEST.read_bytes()).hexdigest() == (
        EXPECTED_MANIFEST_SHA256
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == (
        "validated Common Crawl bind-alias retry manifest: "
        f"{EXPECTED_MANIFEST_SHA256}\n"
    )


def test_verifier_remains_self_contained_when_extracted_to_tmp(
    tmp_path: Path,
) -> None:
    extracted = tmp_path / VERIFIER.name
    shutil.copyfile(VERIFIER, extracted)

    result = subprocess.run(
        [sys.executable, str(extracted), str(MANIFEST)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith(f"{EXPECTED_MANIFEST_SHA256}\n")


def test_verifier_rejects_digest_tamper_against_reviewed_bytes(
    tmp_path: Path,
) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["failed_attempt"]["common_crawl_mount_identity"]["source_identity"][
        "inode"
    ] += 1
    candidate = tmp_path / MANIFEST.name
    _write_pretty(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "retry manifest SHA-256 does not match reviewed bytes" in result.stderr


def test_reblessed_full_boundary_tamper_still_fails_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["observed_safe_boundary"]["witness_inventory"][0]["size_bytes"] += 1
    candidate = tmp_path / MANIFEST.name
    _write_pretty(candidate, document)
    verifier = _load_verifier_module()
    monkeypatch.setattr(
        verifier,
        "EXPECTED_MANIFEST_SHA256",
        hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )

    with pytest.raises(
        verifier.ManifestError, match="full observed safe-boundary digest is invalid"
    ):
        verifier.validate_manifest(candidate)


def test_reblessed_mount_alias_identity_tamper_fails_even_with_new_boundary_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["failed_attempt"]["common_crawl_mount_identity"]["source_identity"][
        "device"
    ] += 1
    candidate = tmp_path / MANIFEST.name
    _write_pretty(candidate, document)
    verifier = _load_verifier_module()
    monkeypatch.setattr(
        verifier,
        "EXPECTED_MANIFEST_SHA256",
        hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )

    with pytest.raises(
        verifier.ManifestError,
        match="Common Crawl same-inode mount failure facts are invalid",
    ):
        verifier.validate_manifest(candidate)


def test_reblessed_dynamic_instance_tamper_fails_exact_host_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["observed_safe_boundary"]["dynamic_release_instances"][0][
        "active_state"
    ] = "inactive"
    candidate = tmp_path / MANIFEST.name
    _write_pretty(candidate, document)
    verifier = _load_verifier_module()
    monkeypatch.setattr(
        verifier,
        "EXPECTED_MANIFEST_SHA256",
        hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        verifier,
        "SAFE_BOUNDARY_SHA256",
        verifier._canonical_digest(document["observed_safe_boundary"]),
    )

    with pytest.raises(
        verifier.ManifestError,
        match="recorded dynamic release instance boundary is invalid",
    ):
        verifier.validate_manifest(candidate)


def test_reblessed_renderer_absence_tamper_fails_exact_host_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["observed_safe_boundary"]["absent_compose_services"] = ["worker-velocity"]
    candidate = tmp_path / MANIFEST.name
    _write_pretty(candidate, document)
    verifier = _load_verifier_module()
    monkeypatch.setattr(
        verifier,
        "EXPECTED_MANIFEST_SHA256",
        hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        verifier,
        "SAFE_BOUNDARY_SHA256",
        verifier._canonical_digest(document["observed_safe_boundary"]),
    )

    with pytest.raises(verifier.ManifestError, match="absent Compose service set"):
        verifier.validate_manifest(candidate)


def test_reblessed_accidental_activation_tamper_fails_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["failed_attempt"]["post_failure_diagnostic"]["activation_cause"] = (
        "intentional activation"
    )
    candidate = tmp_path / MANIFEST.name
    _write_pretty(candidate, document)
    verifier = _load_verifier_module()
    monkeypatch.setattr(
        verifier,
        "EXPECTED_MANIFEST_SHA256",
        hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )

    with pytest.raises(
        verifier.ManifestError, match="post-failure diagnostic projection is invalid"
    ):
        verifier.validate_manifest(candidate)


def _api_receipt(manifest_sha256: str) -> dict[str, object]:
    return {
        "broker_queue_sha256": (
            "57cba36db8a74f1091b3478b831c833a6325023d57a8c4aa33190112e483f42b"
        ),
        "compose_environment_sha256": (
            "2ce97c2f94ce93336b592e1ddee78cfdbec1e8b19d35b39faab6ac069d332c95"
        ),
        "failed_target_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
        "hybrid_fingerprint_sha256": (
            "c4c5544d11c476911658d50ef54a9a81f43796372cff4937cc3e55cdff5948ed"
        ),
        "incident_id": "2026-08-25-api-readiness-retry",
        "manifest_sha256": manifest_sha256,
        "minimum_recovery_ancestor": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
        "prepared_at": "2026-08-25T21:17:47.899450Z",
        "prior_checkout_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
        "prior_deployed_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
        "recovery_controller_commit": "913a6aa64e705bd5d2b2f6f022a91e07389999e0",
        "restore_profile_sha256": (
            "705aaf3b5e52cc400fcb51957f0f2cf6167869e86b4622637b01ad512641c08a"
        ),
        "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
        "status": "prepared",
        "target_commit": "913a6aa64e705bd5d2b2f6f022a91e07389999e0",
        "transaction_id": "81459025a36873031dba693c229baa7c",
    }


def _original_receipt() -> dict[str, object]:
    return {
        "broker_queue_sha256": (
            "57cba36db8a74f1091b3478b831c833a6325023d57a8c4aa33190112e483f42b"
        ),
        "compose_environment_sha256": (
            "2ce97c2f94ce93336b592e1ddee78cfdbec1e8b19d35b39faab6ac069d332c95"
        ),
        "failed_target_commit": "138a9eb323857ba91944fc04d0ccfabb653e7f24",
        "hybrid_fingerprint_sha256": (
            "19551d94176f03148b052f68467ce9b626995940f3d8bcff495d27d46c0ade78"
        ),
        "incident_id": "2026-08-25-interrupted-phase1",
        "manifest_sha256": (
            "f21ffb99a29902bc849c4c7e0ea0317720f24fa9adcef2068cd0ff40341cf535"
        ),
        "minimum_recovery_ancestor": "8b48162a13f719a4500c2297a337655d91dbb28e",
        "prepared_at": "2026-08-25T17:17:52.050629Z",
        "prior_checkout_commit": "7d05ecca47b20d8cf092a513a0db0390435f363f",
        "prior_deployed_commit": "95ea01d1a394fe219d64d3dce6b105296bce309a",
        "recovery_controller_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
        "restore_profile_sha256": (
            "705aaf3b5e52cc400fcb51957f0f2cf6167869e86b4622637b01ad512641c08a"
        ),
        "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
        "status": "prepared",
        "target_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
        "transaction_id": "ff12146621a04cd507df19cb0665b32f",
    }


def _host_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, dict[str, object], Path, Path, Path, Path]:
    verifier = _load_verifier_module()
    release_root = tmp_path / "palimpsest-release"
    recovery = release_root / "recovery"
    recovery.mkdir(parents=True)
    release_root.chmod(0o700)
    recovery.chmod(0o700)
    uid = os.getuid()
    gid = os.getgid()
    monkeypatch.setattr(verifier, "RELEASE_ROOT", release_root)
    monkeypatch.setattr(verifier, "RECOVERY_DIRECTORY", recovery)
    monkeypatch.setattr(verifier, "CONTINUATION_UID", uid)
    monkeypatch.setattr(verifier, "CONTINUATION_GID", gid)

    repository_root = tmp_path / "repository"
    manifests = repository_root / "ops" / "release-recovery"
    manifests.mkdir(parents=True)
    shutil.copyfile(ORIGINAL_MANIFEST, manifests / ORIGINAL_MANIFEST.name)

    original_prepared = recovery / "original.prepared.json"
    original_complete = recovery / "original.complete.json"
    original_sha = _write_compact(original_prepared, _original_receipt())
    assert original_sha == verifier.ORIGINAL_PREPARED_SHA256
    monkeypatch.setattr(verifier, "ORIGINAL_PREPARED_PATH", str(original_prepared))
    monkeypatch.setattr(verifier, "ORIGINAL_COMPLETION_PATH", str(original_complete))

    api_document = json.loads(API_MANIFEST.read_text(encoding="utf-8"))
    api_document["continuation"]["predecessor_prepared_receipt"].update(
        {"gid": gid, "path": str(original_prepared), "uid": uid}
    )
    api_document["continuation"]["predecessor_completion_receipt"]["path"] = str(
        original_complete
    )
    api_path = manifests / API_MANIFEST.name
    _write_pretty(api_path, api_document)
    api_manifest_sha = hashlib.sha256(api_path.read_bytes()).hexdigest()
    monkeypatch.setattr(verifier, "PREDECESSOR_MANIFEST_SHA256", api_manifest_sha)

    api_prepared = recovery / "api.prepared.json"
    api_complete = recovery / "api.complete.json"
    api_prepared_sha = _write_compact(api_prepared, _api_receipt(api_manifest_sha))
    monkeypatch.setattr(verifier, "PREDECESSOR_PREPARED_PATH", str(api_prepared))
    monkeypatch.setattr(verifier, "PREDECESSOR_PREPARED_SHA256", api_prepared_sha)
    monkeypatch.setattr(verifier, "PREDECESSOR_COMPLETION_PATH", str(api_complete))

    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["continuation"]["predecessor_manifest"]["sha256"] = api_manifest_sha
    document["continuation"]["predecessor_prepared_receipt"].update(
        {
            "gid": gid,
            "path": str(api_prepared),
            "sha256": api_prepared_sha,
            "uid": uid,
        }
    )
    document["continuation"]["predecessor_completion_receipt"]["path"] = str(
        api_complete
    )
    return (
        verifier,
        document,
        repository_root,
        api_prepared,
        api_complete,
        api_path,
    )


def test_host_continuation_recursively_verifies_both_incidents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier, document, repository_root, api_prepared, _, _ = _host_chain(
        tmp_path, monkeypatch
    )

    api_digest, original_digest = verifier.verify_host_continuation(
        document, repository_root
    )

    assert api_digest == hashlib.sha256(api_prepared.read_bytes()).hexdigest()
    assert original_digest == verifier.ORIGINAL_PREPARED_SHA256


@pytest.mark.parametrize(
    "attack",
    [
        "completion",
        "receipt",
        "manifest",
        "original_completion",
        "original_receipt",
        "original_manifest",
    ],
)
def test_host_continuation_rejects_completion_or_chain_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attack: str
) -> None:
    verifier, document, repository_root, api_prepared, api_complete, api_path = (
        _host_chain(tmp_path, monkeypatch)
    )
    if attack == "completion":
        api_complete.write_text("{}\n", encoding="ascii")
    elif attack == "receipt":
        api_prepared.chmod(0o600)
        api_prepared.write_bytes(
            api_prepared.read_bytes().replace(b"prepared", b"invalid!")
        )
        api_prepared.chmod(0o400)
    elif attack == "manifest":
        api_path.write_bytes(api_path.read_bytes().replace(b"healthy", b"altered", 1))
    elif attack == "original_completion":
        Path(verifier.ORIGINAL_COMPLETION_PATH).write_text("{}\n", encoding="ascii")
    elif attack == "original_receipt":
        original_prepared = Path(verifier.ORIGINAL_PREPARED_PATH)
        original_prepared.chmod(0o600)
        original_prepared.write_bytes(
            original_prepared.read_bytes().replace(b"prepared", b"invalid!")
        )
        original_prepared.chmod(0o400)
    else:
        original_manifest = repository_root / verifier.ORIGINAL_MANIFEST_PATH
        original_manifest.write_bytes(
            original_manifest.read_bytes().replace(b"running", b"altered", 1)
        )

    with pytest.raises(verifier.ManifestError):
        verifier.verify_host_continuation(document, repository_root)
