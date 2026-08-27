from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from ops.railway import build_release_manifest as manifest_builder
from ops.railway import verify_continuous_release as verifier

SOURCE = "a" * 40
DEPLOYMENT_ID = "bee09f0a-7898-4942-a762-5bea5c17f58c"
PROJECT_ID = "f7c86128-53a7-458a-a931-6628c6e61fb2"
ENVIRONMENT_ID = "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2"
SERVICE_ID = "86a6f49c-b9dc-4be8-acd1-dd180c693230"
IMAGE_DIGEST = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
COMPLETED = datetime(2026, 8, 27, 6, 2, tzinfo=UTC)
BASE_URL = "https://palimpsest-publication-production.up.railway.app"
PUBLIC_URL = "https://www.palimpsest.info"
CRITICAL_BODIES = {
    "index.html": b"<main>Palimpsest</main>\n",
    "research/report name.pdf": b"%PDF-critical-fixture\n",
}
NONCRITICAL_BODIES = {
    "a.txt": b"sibling\n",
    "a/z.txt": b"nested\n",
    "assets/site.css": b"main { color: #eee; }\n",
    "nested/railway-release.json": b"nested manifest is data\n",
}
BUNDLE_BODIES = {**CRITICAL_BODIES, **NONCRITICAL_BODIES}
# Known output of the canonical builder's component-wise Path ordering.  In
# particular, a/z.txt sorts before its sibling a.txt.
TREE = "a1fa84dc4a9ca6702321310a02f05d121245411c0e45a2c9737bb93bf73e7d56"


def _manifest(*, built_at: str = "2026-08-27T05:16:18Z") -> tuple[dict, bytes]:
    document = {
        "schema_version": verifier.RELEASE_SCHEMA,
        "source_commit": SOURCE,
        "built_at": built_at,
        "deployment_source": "local-git-archive",
        "github_required": False,
        "state": "artifact_ready",
        "file_count": len(BUNDLE_BODIES),
        "total_bytes": sum(len(body) for body in BUNDLE_BODIES.values()),
        "tree_sha256": TREE,
        "critical_files": {
            path: {
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            for path, body in CRITICAL_BODIES.items()
        },
    }
    return document, (json.dumps(document, sort_keys=True) + "\n").encode()


def _deployment() -> dict:
    return {
        "id": DEPLOYMENT_ID,
        "status": "SUCCESS",
        "createdAt": "2026-08-27T05:26:46.517Z",
        "meta": {
            "buildOnly": False,
            "reason": "deploy",
            "imageDigest": IMAGE_DIGEST,
            "cliMessage": "safe message",
            "futureSecret": "must-not-enter-receipt",
        },
    }


def _latest_deployment() -> dict:
    return {
        "id": DEPLOYMENT_ID,
        "status": "SUCCESS",
        "createdAt": "2026-08-27T05:26:46.517Z",
        "deploymentStopped": False,
        "instances": [{"id": "instance", "status": "RUNNING"}],
        "meta": {
            "buildOnly": False,
            "reason": "deploy",
            "imageDigest": IMAGE_DIGEST,
            "volumeMounts": [],
            "serviceManifest": {
                "build": {
                    "builder": "DOCKERFILE",
                    "dockerfilePath": "ops/railway/Dockerfile.static",
                },
                "deploy": {
                    "cronSchedule": None,
                    "healthcheckPath": "/healthz",
                    "numReplicas": 1,
                    "requiredMountPath": None,
                },
            },
        },
    }


def _status() -> dict:
    return {
        "id": PROJECT_ID,
        "services": {
            "edges": [{"node": {"id": SERVICE_ID, "name": "palimpsest-publication"}}]
        },
        "environments": {
            "edges": [
                {
                    "node": {
                        "id": ENVIRONMENT_ID,
                        "canAccess": True,
                        "deletedAt": None,
                        "volumeInstances": {"edges": []},
                        "serviceInstances": {
                            "edges": [
                                {
                                    "node": {
                                        "environmentId": ENVIRONMENT_ID,
                                        "serviceId": SERVICE_ID,
                                        "serviceName": "palimpsest-publication",
                                        "source": {"image": None, "repo": None},
                                        "cronSchedule": None,
                                        "nextCronRunAt": None,
                                        "latestDeployment": _latest_deployment(),
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
        "workspace": {"futureToken": "must-not-enter-receipt"},
    }


class FakeLive:
    def __init__(self, manifest: bytes, *, wrong_health_attempts: int = 0) -> None:
        self.manifest = manifest
        self.wrong_health_attempts = wrong_health_attempts
        self.health_calls = 0
        self.calls: list[tuple[str, float, int]] = []

    def __call__(self, url: str, timeout: float, maximum: int) -> verifier.HttpPayload:
        self.calls.append((url, timeout, maximum))
        if "/healthz?" in url:
            self.health_calls += 1
            source = (
                "f" * 40 if self.health_calls <= self.wrong_health_attempts else SOURCE
            )
            body = json.dumps(
                {
                    "status": "ready",
                    "service": "palimpsest-publication",
                    "topology": "static-only",
                    "mcp_available_here": False,
                    "source_commit": source,
                    "tree_sha256": TREE,
                }
            ).encode()
        elif "/railway-release.json?" in url:
            body = self.manifest
        else:
            path = unquote(urlsplit(url).path.lstrip("/"))
            body = CRITICAL_BODIES[path]
        return verifier.HttpPayload(
            status=200,
            final_url=url,
            body=body,
            content_type="application/json; charset=utf-8",
            cache_control="no-store",
        )


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, bytes]:
    _document, manifest = _manifest()
    git_status = tmp_path / "git-status.txt"
    release_manifest = tmp_path / "railway-release.json"
    deployment_json = tmp_path / "deployment.json"
    status_json = tmp_path / "status.json"
    git_status.write_bytes(b"")
    release_manifest.write_bytes(manifest)
    deployment_json.write_text(json.dumps([{"id": "unrelated"}, _deployment()]))
    status_json.write_text(json.dumps(_status()))
    return git_status, release_manifest, deployment_json, status_json, manifest


def _sealed_bundle(
    tmp_path: Path,
    *,
    name: str = "bundle",
    built_at: str = "2026-08-27T05:16:18Z",
) -> tuple[Path, bytes]:
    root = tmp_path / name
    root.mkdir()
    for relative, body in BUNDLE_BODIES.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
    _document, manifest = _manifest(built_at=built_at)
    (root / "railway-release.json").write_bytes(manifest)
    return root, manifest


def _validate_bundle(
    root: Path,
    manifest: bytes,
    *,
    now: datetime = NOW,
    maximum_age_seconds: int = 86400,
) -> verifier.ReleaseIdentity:
    return verifier.validate_sealed_bundle(
        root,
        expected_source_commit=SOURCE,
        expected_tree_sha256=TREE,
        expected_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        now=now,
        maximum_age_seconds=maximum_age_seconds,
        future_skew_seconds=120,
    )


def test_verify_and_write_is_offline_atomic_canonical_and_secret_free(
    tmp_path: Path,
) -> None:
    git_status, release_manifest, deployment_json, status_json, manifest = (
        _write_inputs(tmp_path)
    )
    fetcher = FakeLive(manifest)
    receipt_path = tmp_path / "receipt.json"

    receipt, digest = verifier.verify_and_write(
        expected_source_commit=SOURCE,
        checkout_source_commit=SOURCE,
        current_main_source_commit=SOURCE,
        git_status_path=git_status,
        release_manifest_path=release_manifest,
        expected_tree_sha256=TREE,
        expected_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        deployment_json_path=deployment_json,
        status_json_path=status_json,
        expected_deployment_id=DEPLOYMENT_ID,
        expected_image_digest=None,
        expected_project_id=PROJECT_ID,
        expected_environment_id=ENVIRONMENT_ID,
        expected_service_id=SERVICE_ID,
        live_base_url=BASE_URL,
        public_base_url=PUBLIC_URL,
        receipt_path=receipt_path,
        now=NOW,
        attempts=3,
        retry_delay_seconds=0,
        timeout_seconds=7,
        maximum_deployment_age_seconds=7200,
        maximum_release_age_seconds=86400,
        future_skew_seconds=120,
        fetcher=fetcher,
        sleeper=lambda _seconds: pytest.fail("successful verification must not sleep"),
        clock=lambda: COMPLETED,
    )

    payload = receipt_path.read_bytes()
    assert payload == verifier.canonical_json(receipt)
    assert hashlib.sha256(payload).hexdigest() == digest
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert b"must-not-enter-receipt" not in payload
    assert b"cliMessage" not in payload
    assert receipt["preflight"]["worktree_clean"] is True
    assert receipt["verified_at"] == "2026-08-27T06:02:00Z"
    assert receipt["deployment"]["image_digest"] == IMAGE_DIGEST
    assert receipt["topology"] == {
        "project_id": PROJECT_ID,
        "environment_id": ENVIRONMENT_ID,
        "service_id": SERVICE_ID,
        "service_name": "palimpsest-publication",
        "latest_deployment_id": DEPLOYMENT_ID,
        "latest_deployment_reason": "deploy",
        "source_attached": False,
        "cron_schedule": None,
        "volume_instance_count": 0,
        "volume_mount_count": 0,
        "service_manifest": {
            "builder": "DOCKERFILE",
            "dockerfile_path": "ops/railway/Dockerfile.static",
            "healthcheck_path": "/healthz",
            "num_replicas": 1,
        },
    }
    assert receipt["live"]["public_origin_verified"] is True
    assert receipt["live"]["manifest_byte_identical"] is True
    assert receipt["live"]["critical_inventory_byte_identical"] is True
    assert len(fetcher.calls) == 8
    assert all(call[1] == 7 for call in fetcher.calls)

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "protocol/railway-continuous-release-receipt-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)
    assert sum("research/report%20name.pdf?" in call[0] for call in fetcher.calls) == 2


def test_verify_and_write_rechecks_deployment_freshness_at_completion(
    tmp_path: Path,
) -> None:
    git_status, release_manifest, deployment_json, status_json, manifest = (
        _write_inputs(tmp_path)
    )
    receipt_path = tmp_path / "receipt.json"

    with pytest.raises(
        verifier.VerificationError,
        match="deployment at verification completion is stale",
    ):
        verifier.verify_and_write(
            expected_source_commit=SOURCE,
            checkout_source_commit=SOURCE,
            current_main_source_commit=SOURCE,
            git_status_path=git_status,
            release_manifest_path=release_manifest,
            expected_tree_sha256=TREE,
            expected_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            deployment_json_path=deployment_json,
            status_json_path=status_json,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=None,
            expected_project_id=PROJECT_ID,
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            live_base_url=BASE_URL,
            public_base_url=None,
            receipt_path=receipt_path,
            now=NOW,
            attempts=1,
            retry_delay_seconds=0,
            timeout_seconds=7,
            maximum_deployment_age_seconds=3600,
            maximum_release_age_seconds=86400,
            future_skew_seconds=120,
            fetcher=FakeLive(manifest),
            sleeper=lambda _seconds: None,
            clock=lambda: datetime(2026, 8, 27, 7, 0, tzinfo=UTC),
        )
    assert not receipt_path.exists()


@pytest.mark.parametrize(
    ("checkout", "current_main", "status", "message"),
    [
        ("d" * 40, SOURCE, b"", "checkout source commit"),
        (SOURCE, "e" * 40, b"", "no longer current main"),
        (SOURCE, SOURCE, b" M index.html\n", "not clean"),
    ],
)
def test_preflight_fails_closed_on_identity_or_cleanliness_drift(
    checkout: str, current_main: str, status: bytes, message: str
) -> None:
    with pytest.raises(verifier.VerificationError, match=message):
        verifier.validate_preflight(
            expected_source_commit=SOURCE,
            checkout_source_commit=checkout,
            current_main_source_commit=current_main,
            git_status=status,
        )


def test_sealed_bundle_validates_manifest_freshness_and_every_critical_file(
    tmp_path: Path,
) -> None:
    root, manifest = _sealed_bundle(tmp_path)
    identity = _validate_bundle(root, manifest)

    assert identity.source_commit == SOURCE
    assert identity.tree_sha256 == TREE
    assert identity.manifest_sha256 == hashlib.sha256(manifest).hexdigest()
    assert [row.path for row in identity.critical_files] == sorted(CRITICAL_BODIES)


def test_manifest_builder_excludes_only_the_root_release_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "builder-root"
    nested = root / "nested/railway-release.json"
    nested.parent.mkdir(parents=True)
    (root / "railway-release.json").write_bytes(b"root manifest is excluded\n")
    nested.write_bytes(b"nested manifest is content\n")
    (root / "index.html").write_bytes(b"index\n")
    monkeypatch.setattr(manifest_builder, "CRITICAL_PATHS", ())

    first = manifest_builder.build_manifest(root, SOURCE, "2026-08-27T05:16:18Z")
    assert first["file_count"] == 2
    assert first["total_bytes"] == len(b"nested manifest is content\n") + len(
        b"index\n"
    )

    nested.write_bytes(b"X" + b"nested manifest is content\n"[1:])
    second = manifest_builder.build_manifest(root, SOURCE, "2026-08-27T05:16:18Z")
    assert second["file_count"] == first["file_count"]
    assert second["total_bytes"] == first["total_bytes"]
    assert second["tree_sha256"] != first["tree_sha256"]


def test_sealed_bundle_binds_nested_release_manifest_names(tmp_path: Path) -> None:
    tampered_root, tampered_manifest = _sealed_bundle(
        tmp_path, name="nested-manifest-tamper"
    )
    nested = tampered_root / "nested/railway-release.json"
    original = NONCRITICAL_BODIES["nested/railway-release.json"]
    nested.write_bytes(b"X" + original[1:])
    with pytest.raises(verifier.VerificationError, match="tree SHA-256 does not match"):
        _validate_bundle(tampered_root, tampered_manifest)

    extra_root, extra_manifest = _sealed_bundle(tmp_path, name="nested-manifest-extra")
    extra = extra_root / "another/railway-release.json"
    extra.parent.mkdir()
    extra.write_bytes(b"must be counted\n")
    with pytest.raises(verifier.VerificationError, match="file count does not match"):
        _validate_bundle(extra_root, extra_manifest)


def test_sealed_bundle_recomputes_full_tree_and_rejects_noncritical_drift(
    tmp_path: Path,
) -> None:
    tampered_root, tampered_manifest = _sealed_bundle(tmp_path, name="tree-tamper")
    noncritical = tampered_root / "assets/site.css"
    original = NONCRITICAL_BODIES["assets/site.css"]
    noncritical.write_bytes(b"X" + original[1:])
    with pytest.raises(verifier.VerificationError, match="tree SHA-256 does not match"):
        _validate_bundle(tampered_root, tampered_manifest)

    resized_root, resized_manifest = _sealed_bundle(tmp_path, name="tree-resize")
    (resized_root / "assets/site.css").write_bytes(original + b"x")
    with pytest.raises(verifier.VerificationError, match="total bytes do not match"):
        _validate_bundle(resized_root, resized_manifest)

    extra_root, extra_manifest = _sealed_bundle(tmp_path, name="extra-file")
    (extra_root / "undeclared-noncritical.txt").write_bytes(b"extra\n")
    with pytest.raises(verifier.VerificationError, match="file count does not match"):
        _validate_bundle(extra_root, extra_manifest)


def test_sealed_bundle_rejects_size_and_same_size_hash_tampering(
    tmp_path: Path,
) -> None:
    hash_root, hash_manifest = _sealed_bundle(tmp_path, name="hash-tamper")
    original = CRITICAL_BODIES["index.html"]
    (hash_root / "index.html").write_bytes(b"X" + original[1:])
    with pytest.raises(verifier.VerificationError, match="SHA-256 does not match"):
        _validate_bundle(hash_root, hash_manifest)

    size_root, size_manifest = _sealed_bundle(tmp_path, name="size-tamper")
    (size_root / "index.html").write_bytes(original[:-1])
    with pytest.raises(verifier.VerificationError, match="size does not match"):
        _validate_bundle(size_root, size_manifest)


def test_sealed_bundle_rejects_final_and_intermediate_symlinks(tmp_path: Path) -> None:
    final_root, final_manifest = _sealed_bundle(tmp_path, name="final-link")
    outside_file = tmp_path / "outside.pdf"
    outside_file.write_bytes(CRITICAL_BODIES["research/report name.pdf"])
    final_path = final_root / "research/report name.pdf"
    final_path.unlink()
    final_path.symlink_to(outside_file)
    with pytest.raises(verifier.VerificationError, match="unavailable or unsafe"):
        _validate_bundle(final_root, final_manifest)

    middle_root, middle_manifest = _sealed_bundle(tmp_path, name="middle-link")
    outside_directory = tmp_path / "outside-research"
    outside_directory.mkdir()
    (outside_directory / "report name.pdf").write_bytes(
        CRITICAL_BODIES["research/report name.pdf"]
    )
    middle_file = middle_root / "research/report name.pdf"
    middle_file.unlink()
    middle_file.parent.rmdir()
    middle_file.parent.symlink_to(outside_directory, target_is_directory=True)
    with pytest.raises(verifier.VerificationError, match="unavailable or unsafe"):
        _validate_bundle(middle_root, middle_manifest)


def test_sealed_bundle_rejects_root_link_directory_target_and_unsafe_manifest_path(
    tmp_path: Path,
) -> None:
    root, manifest = _sealed_bundle(tmp_path, name="root-targets")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(root, target_is_directory=True)
    with pytest.raises(verifier.VerificationError, match="root is not"):
        _validate_bundle(root_link, manifest)

    directory_root, directory_manifest = _sealed_bundle(tmp_path, name="directory")
    directory_file = directory_root / "index.html"
    directory_file.unlink()
    directory_file.mkdir()
    with pytest.raises(verifier.VerificationError, match="not a regular file"):
        _validate_bundle(directory_root, directory_manifest)

    path_root, _path_manifest = _sealed_bundle(tmp_path, name="unsafe-path")
    document, _payload = _manifest()
    document["critical_files"] = {
        "research/../outside": {
            "bytes": 1,
            "sha256": hashlib.sha256(b"x").hexdigest(),
        }
    }
    unsafe_manifest = (json.dumps(document, sort_keys=True) + "\n").encode()
    (path_root / "railway-release.json").write_bytes(unsafe_manifest)
    with pytest.raises(verifier.VerificationError, match="not normalized"):
        _validate_bundle(path_root, unsafe_manifest)


def test_sealed_bundle_rejects_stale_manifest_and_aggregate_over_cap(
    tmp_path: Path, monkeypatch
) -> None:
    stale_root, stale_manifest = _sealed_bundle(
        tmp_path,
        name="stale",
        built_at="2026-08-20T00:00:00Z",
    )
    with pytest.raises(verifier.VerificationError, match="manifest is stale"):
        _validate_bundle(stale_root, stale_manifest, maximum_age_seconds=3600)

    capped_root, capped_manifest = _sealed_bundle(tmp_path, name="capped")
    monkeypatch.setattr(verifier, "MAX_CRITICAL_AGGREGATE_BYTES", 10)
    with pytest.raises(verifier.VerificationError, match="aggregate cap"):
        _validate_bundle(capped_root, capped_manifest)


def test_sealed_bundle_rejects_root_replacement_during_validation(
    tmp_path: Path, monkeypatch
) -> None:
    root, manifest = _sealed_bundle(tmp_path)
    original_load = verifier.load_release_identity

    def replace_root(*args, **kwargs):
        result = original_load(*args, **kwargs)
        root.rename(tmp_path / "original-bundle")
        root.mkdir()
        return result

    monkeypatch.setattr(verifier, "load_release_identity", replace_root)
    with pytest.raises(verifier.VerificationError, match="root changed"):
        _validate_bundle(root, manifest)


def test_deployment_must_be_exact_successful_fresh_and_not_build_only() -> None:
    document = _deployment()
    document["createdAt"] = "2026-08-20T00:00:00Z"
    with pytest.raises(verifier.VerificationError, match="stale"):
        verifier.parse_deployment_evidence(
            json.dumps(document).encode(),
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=IMAGE_DIGEST,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )

    document = _deployment()
    document["meta"]["buildOnly"] = True
    with pytest.raises(verifier.VerificationError, match="build-only"):
        verifier.parse_deployment_evidence(
            json.dumps(document).encode(),
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=IMAGE_DIGEST,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )


def test_documented_rollback_reason_is_bound_across_provider_evidence() -> None:
    deployment = _deployment()
    deployment["meta"]["reason"] = "deploymentRollback"
    evidence = verifier.parse_deployment_evidence(
        json.dumps(deployment).encode(),
        expected_deployment_id=DEPLOYMENT_ID,
        expected_image_digest=IMAGE_DIGEST,
        now=NOW,
        maximum_age_seconds=3600,
        future_skew_seconds=60,
    )
    assert evidence.reason == "deploymentRollback"

    status = _status()
    latest = status["environments"]["edges"][0]["node"]["serviceInstances"]["edges"][0][
        "node"
    ]["latestDeployment"]
    latest["meta"]["reason"] = "deploymentRollback"
    topology = verifier.parse_status_topology(
        json.dumps(status).encode(),
        expected_project_id=PROJECT_ID,
        expected_environment_id=ENVIRONMENT_ID,
        expected_service_id=SERVICE_ID,
        expected_deployment_id=DEPLOYMENT_ID,
        expected_image_digest=IMAGE_DIGEST,
        expected_deployment_reason=evidence.reason,
    )
    assert topology.deployment_reason == "deploymentRollback"

    deployment["meta"]["reason"] = "redeployFromUnknownState"
    with pytest.raises(verifier.VerificationError, match="allowed release state"):
        verifier.parse_deployment_evidence(
            json.dumps(deployment).encode(),
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=IMAGE_DIGEST,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )


def test_deployment_json_rejects_duplicate_keys_and_duplicate_identity() -> None:
    with pytest.raises(verifier.VerificationError, match="duplicate JSON key"):
        verifier.parse_deployment_evidence(
            b'{"id":"one","id":"two"}',
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=None,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )

    duplicate = json.dumps([_deployment(), _deployment()]).encode()
    with pytest.raises(verifier.VerificationError, match="one exact deployment ID"):
        verifier.parse_deployment_evidence(
            duplicate,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=None,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )


def test_latest_status_deployment_extracts_real_shaped_pinned_identity() -> None:
    evidence = verifier.extract_latest_status_deployment(
        json.dumps(_status()).encode(),
        expected_environment_id=ENVIRONMENT_ID,
        expected_service_id=SERVICE_ID,
        now=NOW,
        maximum_age_seconds=3600,
        future_skew_seconds=60,
    )

    assert evidence == verifier.DeploymentEvidence(
        deployment_id=DEPLOYMENT_ID,
        status="SUCCESS",
        created_at=datetime(2026, 8, 27, 5, 26, 46, 517000, tzinfo=UTC),
        image_digest=IMAGE_DIGEST,
        reason="deploy",
    )
    topology = verifier.parse_status_topology(
        json.dumps(_status()).encode(),
        expected_project_id=PROJECT_ID,
        expected_environment_id=ENVIRONMENT_ID,
        expected_service_id=SERVICE_ID,
        expected_deployment_id=evidence.deployment_id,
        expected_image_digest=evidence.image_digest,
        expected_deployment_reason=evidence.reason,
    )
    assert topology.deployment_id == evidence.deployment_id


@pytest.mark.parametrize(
    "instances",
    [
        pytest.param([], id="zero-instances"),
        pytest.param(
            [
                {"id": "instance-one", "status": "RUNNING"},
                {"id": "instance-two", "status": "RUNNING"},
            ],
            id="two-instances",
        ),
    ],
)
def test_status_extraction_and_topology_require_exactly_one_running_instance(
    instances: list[dict[str, str]],
) -> None:
    document = _status()
    latest = document["environments"]["edges"][0]["node"]["serviceInstances"]["edges"][
        0
    ]["node"]["latestDeployment"]
    latest["instances"] = instances
    payload = json.dumps(document).encode()

    with pytest.raises(
        verifier.VerificationError, match="exactly one running instance"
    ):
        verifier.extract_latest_status_deployment(
            payload,
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )
    with pytest.raises(
        verifier.VerificationError, match="exactly one running instance"
    ):
        verifier.parse_status_topology(
            payload,
            expected_project_id=PROJECT_ID,
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=IMAGE_DIGEST,
            expected_deployment_reason="deploy",
        )


def test_latest_status_deployment_accepts_only_documented_rollback_reason() -> None:
    document = _status()
    latest = document["environments"]["edges"][0]["node"]["serviceInstances"]["edges"][
        0
    ]["node"]["latestDeployment"]
    latest["meta"]["reason"] = "deploymentRollback"
    evidence = verifier.extract_latest_status_deployment(
        json.dumps(document).encode(),
        expected_environment_id=ENVIRONMENT_ID,
        expected_service_id=SERVICE_ID,
        now=NOW,
        maximum_age_seconds=3600,
        future_skew_seconds=60,
    )
    assert evidence.reason == "deploymentRollback"

    latest["meta"]["reason"] = "unknownRollback"
    with pytest.raises(verifier.VerificationError, match="allowed release state"):
        verifier.extract_latest_status_deployment(
            json.dumps(document).encode(),
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )


def test_latest_status_deployment_rejects_duplicate_malformed_and_oversized_json() -> (
    None
):
    duplicate = b'{"environments":{"edges":[]},"environments":{"edges":[]}}'
    with pytest.raises(verifier.VerificationError, match="duplicate JSON key"):
        verifier.extract_latest_status_deployment(
            duplicate,
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )

    for payload in (
        b'{"environments":',
        b"x" * (verifier.MAX_STATUS_DOCUMENT_BYTES + 1),
    ):
        with pytest.raises(verifier.VerificationError):
            verifier.extract_latest_status_deployment(
                payload,
                expected_environment_id=ENVIRONMENT_ID,
                expected_service_id=SERVICE_ID,
                now=NOW,
                maximum_age_seconds=3600,
                future_skew_seconds=60,
            )


def test_latest_status_deployment_rejects_stale_and_non_singular_topology() -> None:
    stale = _status()
    latest = stale["environments"]["edges"][0]["node"]["serviceInstances"]["edges"][0][
        "node"
    ]["latestDeployment"]
    latest["createdAt"] = "2026-08-20T00:00:00Z"
    with pytest.raises(verifier.VerificationError, match="is stale"):
        verifier.extract_latest_status_deployment(
            json.dumps(stale).encode(),
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )

    duplicated = _status()
    environment_edges = duplicated["environments"]["edges"]
    environment_edges.append(json.loads(json.dumps(environment_edges[0])))
    with pytest.raises(verifier.VerificationError, match="one exact environment ID"):
        verifier.extract_latest_status_deployment(
            json.dumps(duplicated).encode(),
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )

    duplicated = _status()
    instance_edges = duplicated["environments"]["edges"][0]["node"]["serviceInstances"][
        "edges"
    ]
    instance_edges.append(json.loads(json.dumps(instance_edges[0])))
    with pytest.raises(
        verifier.VerificationError, match="one exact environment service ID"
    ):
        verifier.extract_latest_status_deployment(
            json.dumps(duplicated).encode(),
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", "FAILED", "not successful"),
        ("deploymentStopped", True, "stopped or ambiguous"),
        ("createdAt", None, "bounded timestamp"),
    ],
)
def test_latest_status_deployment_rejects_malformed_latest_state(
    field: str, value: object, message: str
) -> None:
    document = _status()
    latest = document["environments"]["edges"][0]["node"]["serviceInstances"]["edges"][
        0
    ]["node"]["latestDeployment"]
    latest[field] = value
    with pytest.raises(verifier.VerificationError, match=message):
        verifier.extract_latest_status_deployment(
            json.dumps(document).encode(),
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", "not-a-deployment-id", "canonical UUID"),
        ("imageDigest", "sha256:short", "image digest is invalid"),
    ],
)
def test_latest_status_deployment_rejects_malformed_bound_identity(
    field: str, value: str, message: str
) -> None:
    document = _status()
    latest = document["environments"]["edges"][0]["node"]["serviceInstances"]["edges"][
        0
    ]["node"]["latestDeployment"]
    if field == "id":
        latest[field] = value
    else:
        latest["meta"][field] = value
    with pytest.raises(verifier.VerificationError, match=message):
        verifier.extract_latest_status_deployment(
            json.dumps(document).encode(),
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            now=NOW,
            maximum_age_seconds=3600,
            future_skew_seconds=60,
        )


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("source", "repo"), "owner/repository", "attached source"),
        (
            ("latestDeployment", "meta", "volumeMounts"),
            [{"mountPath": "/data"}],
            "mounts",
        ),
        (
            ("latestDeployment", "meta", "serviceManifest", "build", "builder"),
            "NIXPACKS",
            "builder",
        ),
        (
            (
                "latestDeployment",
                "meta",
                "serviceManifest",
                "build",
                "dockerfilePath",
            ),
            "Dockerfile",
            "Dockerfile path",
        ),
        (
            (
                "latestDeployment",
                "meta",
                "serviceManifest",
                "deploy",
                "healthcheckPath",
            ),
            "/ready",
            "healthcheck path",
        ),
        (
            (
                "latestDeployment",
                "meta",
                "serviceManifest",
                "deploy",
                "numReplicas",
            ),
            2,
            "replica count",
        ),
    ],
)
def test_status_topology_fails_closed_on_runtime_drift(
    path: tuple[str, ...], value: object, message: str
) -> None:
    document = _status()
    instance = document["environments"]["edges"][0]["node"]["serviceInstances"][
        "edges"
    ][0]["node"]
    target = instance
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(verifier.VerificationError, match=message):
        verifier.parse_status_topology(
            json.dumps(document).encode(),
            expected_project_id=PROJECT_ID,
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=IMAGE_DIGEST,
            expected_deployment_reason="deploy",
        )


def test_status_topology_rejects_volume_and_accepts_cli_null_source_shapes() -> None:
    document = _status()
    environment = document["environments"]["edges"][0]["node"]
    environment["volumeInstances"]["edges"] = [{"node": {"id": "volume"}}]
    with pytest.raises(verifier.VerificationError, match="has volumes"):
        verifier.parse_status_topology(
            json.dumps(document).encode(),
            expected_project_id=PROJECT_ID,
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=IMAGE_DIGEST,
            expected_deployment_reason="deploy",
        )

    document = _status()
    instance = document["environments"]["edges"][0]["node"]["serviceInstances"][
        "edges"
    ][0]["node"]
    instance["source"] = None
    evidence = verifier.parse_status_topology(
        json.dumps(document).encode(),
        expected_project_id=PROJECT_ID,
        expected_environment_id=ENVIRONMENT_ID,
        expected_service_id=SERVICE_ID,
        expected_deployment_id=DEPLOYMENT_ID,
        expected_image_digest=IMAGE_DIGEST,
        expected_deployment_reason="deploy",
    )
    assert evidence.source_attached is False


def test_status_topology_requires_the_receipt_schema_service_name() -> None:
    document = _status()
    document["services"]["edges"][0]["node"]["name"] = "renamed-publication"
    instance = document["environments"]["edges"][0]["node"]["serviceInstances"][
        "edges"
    ][0]["node"]
    instance["serviceName"] = "renamed-publication"

    with pytest.raises(verifier.VerificationError, match="service name does not match"):
        verifier.parse_status_topology(
            json.dumps(document).encode(),
            expected_project_id=PROJECT_ID,
            expected_environment_id=ENVIRONMENT_ID,
            expected_service_id=SERVICE_ID,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_image_digest=IMAGE_DIGEST,
            expected_deployment_reason="deploy",
        )


def test_live_verification_retries_propagation_mismatch_without_network() -> None:
    _document, manifest = _manifest()
    identity = verifier.ReleaseIdentity(
        source_commit=SOURCE,
        tree_sha256=TREE,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        built_at=datetime(2026, 8, 27, 5, 16, 18, tzinfo=UTC),
        critical_files=tuple(
            verifier.CriticalFile(
                path=path,
                size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
            for path, body in sorted(CRITICAL_BODIES.items())
        ),
    )
    fetcher = FakeLive(manifest, wrong_health_attempts=1)
    sleeps: list[float] = []

    live, attempts_used = verifier.verify_live_release(
        base_url=BASE_URL,
        identity=identity,
        now=NOW,
        maximum_release_age_seconds=86400,
        future_skew_seconds=120,
        attempts=3,
        retry_delay_seconds=0.25,
        timeout_seconds=4,
        fetcher=fetcher,
        sleeper=sleeps.append,
    )

    assert attempts_used == 2
    assert sleeps == [0.25]
    assert live["release_manifest"]["manifest_sha256"] == identity.manifest_sha256
    assert len(fetcher.calls) == 5


def test_live_verification_rechecks_freshness_with_each_attempt_clock() -> None:
    _document, manifest = _manifest()
    identity = verifier.ReleaseIdentity(
        source_commit=SOURCE,
        tree_sha256=TREE,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        built_at=datetime(2026, 8, 27, 5, 16, 18, tzinfo=UTC),
        critical_files=tuple(
            verifier.CriticalFile(
                path=path,
                size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
            for path, body in sorted(CRITICAL_BODIES.items())
        ),
    )
    attempt_clocks = iter([NOW, datetime(2026, 8, 27, 8, 0, tzinfo=UTC)])

    with pytest.raises(verifier.VerificationError, match="manifest is stale"):
        verifier.verify_live_release(
            base_url=BASE_URL,
            identity=identity,
            now=NOW,
            maximum_release_age_seconds=3600,
            future_skew_seconds=120,
            attempts=2,
            retry_delay_seconds=0,
            timeout_seconds=4,
            fetcher=FakeLive(manifest, wrong_health_attempts=1),
            sleeper=lambda _seconds: None,
            clock=lambda: next(attempt_clocks),
        )


def test_live_verification_rejects_stale_manifest_and_cacheable_response() -> None:
    _document, manifest = _manifest(built_at="2026-08-20T00:00:00Z")
    identity = verifier.ReleaseIdentity(
        source_commit=SOURCE,
        tree_sha256=TREE,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        built_at=datetime(2026, 8, 20, tzinfo=UTC),
        critical_files=tuple(
            verifier.CriticalFile(
                path=path,
                size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
            for path, body in sorted(CRITICAL_BODIES.items())
        ),
    )
    with pytest.raises(verifier.VerificationError, match="stale"):
        verifier.verify_live_release(
            base_url=BASE_URL,
            identity=identity,
            now=NOW,
            maximum_release_age_seconds=3600,
            future_skew_seconds=120,
            attempts=1,
            retry_delay_seconds=0,
            timeout_seconds=4,
            fetcher=FakeLive(manifest),
            sleeper=lambda _seconds: None,
        )


def test_critical_file_inventory_and_served_bytes_fail_closed() -> None:
    with pytest.raises(verifier.VerificationError, match="not normalized"):
        verifier._critical_inventory(
            {
                "research/../secret": {
                    "bytes": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
            }
        )

    _document, manifest = _manifest()
    identity = verifier.ReleaseIdentity(
        source_commit=SOURCE,
        tree_sha256=TREE,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        built_at=datetime(2026, 8, 27, 5, 16, 18, tzinfo=UTC),
        critical_files=tuple(
            verifier.CriticalFile(
                path=path,
                size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
            for path, body in sorted(CRITICAL_BODIES.items())
        ),
    )

    class AlteredCritical(FakeLive):
        def __call__(
            self, url: str, timeout: float, maximum: int
        ) -> verifier.HttpPayload:
            result = super().__call__(url, timeout, maximum)
            if "research/report%20name.pdf?" in url:
                altered = b"X" + result.body[1:]
                return verifier.HttpPayload(
                    status=result.status,
                    final_url=result.final_url,
                    body=altered,
                    content_type=result.content_type,
                    cache_control=result.cache_control,
                )
            return result

    with pytest.raises(verifier.VerificationError, match="SHA-256 does not match"):
        verifier.verify_live_release(
            base_url=BASE_URL,
            identity=identity,
            now=NOW,
            maximum_release_age_seconds=86400,
            future_skew_seconds=120,
            attempts=1,
            retry_delay_seconds=0,
            timeout_seconds=4,
            fetcher=AlteredCritical(manifest),
            sleeper=lambda _seconds: None,
        )


def test_live_verification_rejects_cacheable_response() -> None:
    class Cacheable(FakeLive):
        def __call__(
            self, url: str, timeout: float, maximum: int
        ) -> verifier.HttpPayload:
            result = super().__call__(url, timeout, maximum)
            return verifier.HttpPayload(
                status=result.status,
                final_url=result.final_url,
                body=result.body,
                content_type=result.content_type,
                cache_control="max-age=300",
            )

    _document, fresh_manifest = _manifest()
    fresh_identity = verifier.ReleaseIdentity(
        source_commit=SOURCE,
        tree_sha256=TREE,
        manifest_sha256=hashlib.sha256(fresh_manifest).hexdigest(),
        built_at=datetime(2026, 8, 27, 5, 16, 18, tzinfo=UTC),
        critical_files=tuple(
            verifier.CriticalFile(
                path=path,
                size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
            for path, body in sorted(CRITICAL_BODIES.items())
        ),
    )
    with pytest.raises(verifier.VerificationError, match="stale caching"):
        verifier.verify_live_release(
            base_url=BASE_URL,
            identity=fresh_identity,
            now=NOW,
            maximum_release_age_seconds=86400,
            future_skew_seconds=120,
            attempts=1,
            retry_delay_seconds=0,
            timeout_seconds=4,
            fetcher=Cacheable(fresh_manifest),
            sleeper=lambda _seconds: None,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://probe.palimpsest.info",
        "https://" + "user" + ":" + "secret" + "@" + "probe.palimpsest.info",
        "https://probe.palimpsest.info/path",
        "https://probe.palimpsest.info?token=secret",
    ],
)
def test_live_origin_rejects_unsafe_or_secret_bearing_urls(url: str) -> None:
    with pytest.raises(
        verifier.VerificationError, match="credential-free HTTPS origin"
    ):
        verifier.normalize_base_url(url)


def test_live_origin_accepts_only_the_two_reviewed_exact_origins() -> None:
    assert verifier.normalize_base_url(BASE_URL) == BASE_URL
    assert verifier.normalize_base_url(PUBLIC_URL) == PUBLIC_URL
    with pytest.raises(verifier.VerificationError, match="origin allowlist"):
        verifier.normalize_base_url("https://probe.palimpsest.info")


def test_direct_file_cli_imports_shared_hardening_from_any_working_directory(
    tmp_path: Path,
) -> None:
    script = Path(verifier.__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "Verify an exact Railway deployment" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr


def test_fetch_adapter_enforces_safe_transport_contract(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def safe_response(url: str, **kwargs):
        calls.append((url, kwargs))
        kwargs["url_policy"](url)
        return SimpleNamespace(
            status=200,
            url=url,
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
            },
        )

    monkeypatch.setattr(verifier, "safe_fetch_response", safe_response)
    url = f"{BASE_URL}/healthz?release_verify=bounded"
    response = verifier.fetch_json(url, 7.5, 4096)

    assert response.status == 200
    assert response.final_url == url
    assert calls == [
        (
            url,
            {
                "max_bytes": 4096,
                "timeout": 7.5,
                "max_redirects": 0,
                "url_policy": verifier._live_url_policy,
                "headers": {
                    "Accept": "*/*",
                    "Cache-Control": "no-cache",
                    "User-Agent": "palimpsest-railway-release-verifier/1",
                },
            },
        )
    ]
    with pytest.raises(verifier.FetchError, match="escaped"):
        verifier._live_url_policy("https://attacker.example/railway-release.json")


def test_fetch_adapter_suppresses_transport_details(monkeypatch) -> None:
    def refused(*_args, **_kwargs):
        raise verifier.FetchError("secret-bearing transport detail")

    monkeypatch.setattr(verifier, "safe_fetch_response", refused)
    with pytest.raises(verifier.VerificationError) as failure:
        verifier.fetch_json(f"{PUBLIC_URL}/healthz", 2, 1024)
    assert str(failure.value) == "live JSON endpoint could not be reached safely"
    assert "secret-bearing" not in str(failure.value)
