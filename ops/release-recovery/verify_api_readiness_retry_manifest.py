#!/usr/bin/env python3
"""Fail-closed verifier for the API-readiness interrupted-Phase-1 retry."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "palimpsest-interrupted-phase1-recovery.v2"
INCIDENT_ID = "2026-08-25-api-readiness-retry"
MANIFEST_NAME = "2026-08-25-api-readiness-retry.json"
MANIFEST_REPOSITORY_PATH = f"ops/release-recovery/{MANIFEST_NAME}"
EXPECTED_MANIFEST_SHA256 = (
    "6a3a393a7f9ebdfb6fb38cf984db4f4558b3af9fa7cc973683116c274d9d3218"
)
TARGET_COMMIT = "1ae25399c7b36dca60e316cc966ea7d9636ec62b"
PREDECESSOR_INCIDENT_ID = "2026-08-25-interrupted-phase1"
PREDECESSOR_MANIFEST_PATH = "ops/release-recovery/2026-08-25-interrupted-phase1.json"
PREDECESSOR_MANIFEST_SHA256 = (
    "f21ffb99a29902bc849c4c7e0ea0317720f24fa9adcef2068cd0ff40341cf535"
)
PREDECESSOR_PREPARED_PATH = (
    "/var/lib/palimpsest-release/recovery/2026-08-25-interrupted-phase1.prepared.json"
)
PREDECESSOR_PREPARED_SHA256 = (
    "e9f506a44e19f78ecb094bd13c5d7c29f62f894174a5213de67b402b42a74f66"
)
PREDECESSOR_COMPLETION_PATH = (
    "/var/lib/palimpsest-release/recovery/2026-08-25-interrupted-phase1.complete.json"
)
PREDECESSOR_TRANSACTION_ID = "ff12146621a04cd507df19cb0665b32f"
RESTORE_PROFILE_SHA256 = (
    "705aaf3b5e52cc400fcb51957f0f2cf6167869e86b4622637b01ad512641c08a"
)
MAX_MANIFEST_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 64 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """The manifest or its bound continuation is not the reviewed authority."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ManifestError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(item: str) -> None:
    raise ManifestError(f"non-finite JSON value: {item}")


def _load_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot decode {label}: {error}") from error
    if type(value) is not dict:
        raise ManifestError(f"{label} must be a JSON object")
    return value


def _read_regular_nofollow(path: Path, maximum: int, label: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ManifestError(
            f"cannot open {label} without following symlinks: {error}"
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ManifestError(f"{label} is not a single-link regular file")
        if not 0 < before.st_size <= maximum:
            raise ManifestError(f"{label} size is outside the accepted bound")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum:
                raise ManifestError(f"{label} exceeds its byte ceiling")
        after = os.fstat(descriptor)
        identity = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(payload) != before.st_size or any(
            getattr(before, field) != getattr(after, field) for field in identity
        ):
            raise ManifestError(f"{label} changed while being read")
        return bytes(payload)
    except OSError as error:
        raise ManifestError(f"cannot read {label}: {error}") from error
    finally:
        os.close(descriptor)


def _canonical_pretty(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")


def _canonical_compact(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ManifestError(
            f"{label} fields differ: missing={sorted(expected - set(value))}, "
            f"unknown={sorted(set(value) - expected)}"
        )


def _require_semantics(document: dict[str, Any]) -> None:
    _require_keys(
        document,
        {
            "authority",
            "continuation",
            "failed_attempt",
            "incident_date",
            "incident_id",
            "observed_safe_boundary",
            "pre_failure_state",
            "recovery_target_constraints",
            "schema_version",
        },
        "manifest",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ManifestError("unexpected schema version")
    if (
        document["incident_id"] != INCIDENT_ID
        or document["incident_date"] != "2026-08-25"
    ):
        raise ManifestError("unexpected incident identity")

    authority = document["authority"]
    if authority != {
        "failed_target_commit": TARGET_COMMIT,
        "prior_checkout_commit": TARGET_COMMIT,
        "prior_deployed_commit": TARGET_COMMIT,
    }:
        raise ManifestError("retry authority is not the exact 1ae25399 checkpoint")

    constraints = document["recovery_target_constraints"]
    if constraints != {
        "must_be_descendant_of": TARGET_COMMIT,
        "must_be_reviewed": True,
        "must_contain_manifest_path": MANIFEST_REPOSITORY_PATH,
    }:
        raise ManifestError("recovery target constraints are not fail-closed")

    continuation = document["continuation"]
    _require_keys(
        continuation,
        {
            "predecessor_completion_receipt",
            "predecessor_incident_id",
            "predecessor_manifest",
            "predecessor_prepared_receipt",
            "predecessor_restore_profile_sha256",
        },
        "continuation",
    )
    if continuation["predecessor_incident_id"] != PREDECESSOR_INCIDENT_ID:
        raise ManifestError("predecessor incident is invalid")
    if continuation["predecessor_manifest"] != {
        "path": PREDECESSOR_MANIFEST_PATH,
        "sha256": PREDECESSOR_MANIFEST_SHA256,
    }:
        raise ManifestError("predecessor manifest binding is invalid")
    if continuation["predecessor_completion_receipt"] != {
        "expected_absent": True,
        "path": PREDECESSOR_COMPLETION_PATH,
    }:
        raise ManifestError("predecessor completion absence binding is invalid")
    prepared_binding = continuation["predecessor_prepared_receipt"]
    if prepared_binding != {
        "gid": 0,
        "link_count": 1,
        "mode": "0400",
        "path": PREDECESSOR_PREPARED_PATH,
        "recovery_controller_commit": TARGET_COMMIT,
        "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
        "sha256": PREDECESSOR_PREPARED_SHA256,
        "status": "prepared",
        "target_commit": TARGET_COMMIT,
        "transaction_id": PREDECESSOR_TRANSACTION_ID,
        "uid": 0,
    }:
        raise ManifestError("predecessor prepared-receipt binding is invalid")
    if continuation["predecessor_restore_profile_sha256"] != RESTORE_PROFILE_SHA256:
        raise ManifestError("predecessor restoration profile binding is invalid")

    failed = document["failed_attempt"]
    _require_keys(
        failed,
        {
            "api_readiness",
            "candidate_image",
            "collectors_may_have_advanced",
            "migration_applied",
            "migration_exit_code",
            "migration_result",
            "phase1_handoff_created",
            "recovery_backup_reason",
            "release_receipts_created",
            "snapshot_ceiling",
        },
        "failed_attempt",
    )
    if (
        failed["migration_applied"] is not True
        or failed["migration_exit_code"] != 0
        or failed["collectors_may_have_advanced"] is not True
        or failed["phase1_handoff_created"] is not False
        or failed["release_receipts_created"] is not False
        or failed["recovery_backup_reason"] != "api-readiness-retry-fresh-target-backup"
    ):
        raise ManifestError("failed-attempt recovery facts are invalid")

    boundary = document["observed_safe_boundary"]
    _require_keys(
        boundary,
        {
            "absent_compose_services",
            "application_containers",
            "compose_environment_sha256",
            "compose_scope",
            "controlled_activators",
            "dynamic_release_instances",
            "infrastructure_containers",
            "installed_bundles",
            "installed_controller_boundary",
            "installed_units",
            "local_application_tag",
            "release_services",
            "repository",
            "running_compose_services",
            "witness_inventory",
        },
        "observed_safe_boundary",
    )
    if boundary["repository"] != {
        "checkout_commit": TARGET_COMMIT,
        "deployed_commit": TARGET_COMMIT,
    }:
        raise ManifestError("repository checkpoint contradicts authority")
    if boundary["compose_environment_sha256"] != (
        "2ce97c2f94ce93336b592e1ddee78cfdbec1e8b19d35b39faab6ac069d332c95"
    ) or boundary["compose_scope"] != {
        "config_files": "/home/palimpsest/palimpsest/ops/docker/docker-compose.prod.yml",
        "project": "palimpsest",
        "working_dir": "/home/palimpsest/palimpsest/ops/docker",
    }:
        raise ManifestError("Compose environment boundary is invalid")
    if boundary["running_compose_services"] != ["api", "postgres", "redis"]:
        raise ManifestError("running Compose service set is invalid")
    if boundary["absent_compose_services"] != ["worker-velocity"]:
        raise ManifestError("absent Compose service set is invalid")
    if boundary["dynamic_release_instances"] != []:
        raise ManifestError("dynamic release instances must remain absent")

    applications = boundary["application_containers"]
    if [item.get("service") for item in applications] != [
        "api",
        "beat",
        "migrate",
        "worker",
        "worker-collectors",
        "worker-warehouse",
    ]:
        raise ManifestError("application container inventory is invalid")
    if any(
        _CONTAINER.fullmatch(str(item.get("container_id", ""))) is None
        for item in applications
    ):
        raise ManifestError("application container ID is invalid")
    application_by_service = {item["service"]: item for item in applications}
    if application_by_service["api"] != {
        "container_id": "edb71ca5489f51ffeca48dac1c8df89e7c85039f50cde426b83be2d222d216e2",
        "exit_code": 0,
        "health": "healthy",
        "image_index_digest": "sha256:d1b3b82b3ab77be6a81851af035134a84280a9c90e868d8344c48d837ea00471",
        "revision": TARGET_COMMIT,
        "service": "api",
        "state": "running",
    }:
        raise ManifestError("post-failure API boundary is invalid")
    if any(item["state"] != "exited" for item in applications[1:]):
        raise ManifestError("every non-API application container must be exited")
    if failed["api_readiness"] != {
        "container_id": application_by_service["api"]["container_id"],
        "failed_probe_path": "/api/v1/node/status",
        "failure_message": "C1 API did not become ready after Compose restart",
        "health": "healthy",
        "post_failure_observed_healthy": True,
        "revision": TARGET_COMMIT,
        "state": "running",
    }:
        raise ManifestError("API-readiness failure projection is invalid")
    migration = failed["migration_result"]
    if migration.get("container_id") != application_by_service["migrate"][
        "container_id"
    ] or migration != {
        "container_id": "6f2bd46198f653a97a66b363d1714ed06429e289f92d0d890db495e3ded8d2d5",
        "exit_code": 0,
        "finished_at": "2026-08-25T17:22:58.978261422Z",
        "image_id": "sha256:d1b3b82b3ab77be6a81851af035134a84280a9c90e868d8344c48d837ea00471",
        "revision": TARGET_COMMIT,
        "started_at": "2026-08-25T17:22:55.11527012Z",
        "state": "exited",
    }:
        raise ManifestError("migration result projection is invalid")

    image = failed["candidate_image"]
    local_image = boundary["local_application_tag"]
    for key in (
        "config_digest",
        "index_digest",
        "platform_manifest_digest",
        "revision",
    ):
        if image.get(key) != local_image.get(key):
            raise ManifestError(f"candidate/local image projection differs: {key}")
    if (
        local_image.get("trusted_for_recovery") is not False
        or local_image.get("revision") != TARGET_COMMIT
    ):
        raise ManifestError("mutable local tag is incorrectly trusted")

    infrastructure = boundary["infrastructure_containers"]
    if [item.get("service") for item in infrastructure] != ["postgres", "redis"] or any(
        item.get("state") != "running" or item.get("health") != "healthy"
        for item in infrastructure
    ):
        raise ManifestError("infrastructure container boundary is invalid")

    activators = boundary["controlled_activators"]
    inventory = activators.get("inventory") if isinstance(activators, dict) else None
    if (
        activators.get("all_disabled") is not True
        or activators.get("all_inactive") is not True
        or not isinstance(inventory, list)
        or len(inventory) != 12
        or len({item.get("unit") for item in inventory}) != 12
        or any(
            item.get("unit_file_state") != "disabled"
            or item.get("active_state") != "inactive"
            or item.get("load_state") != "loaded"
            or item.get("fragment_path") != f"/etc/systemd/system/{item.get('unit')}"
            for item in inventory
        )
    ):
        raise ManifestError("controlled activator checkpoint is invalid")

    services = boundary["release_services"]
    if (
        len(services) != 12
        or len({item.get("unit") for item in services}) != 12
        or any(
            item.get("load_state") != "loaded"
            or item.get("active_state") not in {"inactive", "failed"}
            or item.get("fragment_path") != f"/etc/systemd/system/{item.get('unit')}"
            for item in services
        )
    ):
        raise ManifestError("release service checkpoint is invalid")

    controllers = boundary["installed_controller_boundary"]
    if (
        controllers.get("absent_paths") != []
        or len(controllers.get("present_files", [])) != 6
    ):
        raise ManifestError("controller checkpoint counts are invalid")
    if any(
        not item.get("path", "").startswith(("/etc/palimpsest/", "/opt/palimpsest/"))
        or _SHA256.fullmatch(str(item.get("sha256", ""))) is None
        or item.get("stat") not in {"0:0:444:1", "0:0:755:1"}
        for item in controllers["present_files"]
    ):
        raise ManifestError("installed controller checkpoint is invalid")

    units = boundary["installed_units"]
    unit_paths = [item.get("path") for item in units]
    if (
        len(units) != 25
        or unit_paths != sorted(unit_paths)
        or len(set(unit_paths)) != 25
        or any(
            not str(item.get("path", "")).startswith("/etc/systemd/system/")
            or _SHA256.fullmatch(str(item.get("sha256", ""))) is None
            or item.get("stat") != "0:0:644:1"
            for item in units
        )
    ):
        raise ManifestError("installed unit checkpoint is invalid")

    bundles = boundary["installed_bundles"]
    if len(bundles) != 5 or any(
        item.get("revision") != TARGET_COMMIT
        or item.get("resolved_target_path")
        != f"{item.get('current_symlink_path', '').removesuffix('/current')}/{TARGET_COMMIT}"
        or _SHA256.fullmatch(str(item.get("manifest_sha256", ""))) is None
        for item in bundles
    ):
        raise ManifestError("installed bundle checkpoint is invalid")

    witness = boundary["witness_inventory"]
    if [item.get("name") for item in witness] != [
        "erasure-ledger.witness.jsonl",
        "eval-registry.witness.jsonl",
        "public-freshness-state.json",
    ] or any(
        _SHA256.fullmatch(str(item.get("sha256", ""))) is None
        or type(item.get("size_bytes")) is not int
        or item["size_bytes"] <= 0
        for item in witness
    ):
        raise ManifestError("witness checkpoint is invalid")

    snapshot = failed["snapshot_ceiling"]
    verification = snapshot.get("verification")
    if (
        snapshot.get("latest_snapshot_id") != "20260825T172123Z"
        or snapshot.get("new_snapshot_created") is not True
        or not isinstance(verification, dict)
        or verification.get("schema") != "palimpsest-node-backup-verification.v1"
        or verification.get("status") != "verified"
        or verification.get("snapshot") != snapshot["latest_snapshot_id"]
        or verification.get("counts", {}).get("snapshot_files") != 6
        or verification.get("counts", {}).get("checksum_entries") != 5
        or verification.get("counts", {}).get("witness_history_records", 0) <= 0
        or set(verification.get("digests", {}))
        != {
            "MANIFEST.txt",
            "artifacts.list",
            "artifacts.tar.gz",
            "postgres.dump",
            "postgres.list",
        }
        or any(
            _SHA256.fullmatch(str(value)) is None
            for value in verification.get("digests", {}).values()
        )
    ):
        raise ManifestError("verified checkpoint snapshot projection is invalid")

    pre_failure = document["pre_failure_state"]
    if _canonical_digest(pre_failure) != RESTORE_PROFILE_SHA256:
        raise ManifestError("original restoration profile was not preserved exactly")
    pre_activators = pre_failure.get("activators", [])
    pre_writers = pre_failure.get("compose_writers", [])
    if (
        len(pre_activators) != 12
        or sum(item.get("active_state") == "active" for item in pre_activators) != 11
    ):
        raise ManifestError("original activator restoration intent is invalid")
    if pre_activators[2] != {
        "active_state": "inactive",
        "unit": "palimpsest-node-offsite-backup.timer",
        "unit_file_state": "disabled",
    }:
        raise ManifestError("original node-offsite exception is invalid")
    if (
        [item.get("service") for item in pre_writers]
        != [
            "beat",
            "worker",
            "worker-collectors",
            "worker-warehouse",
            "worker-velocity",
        ]
        or any(item.get("running") is not True for item in pre_writers[:4])
        or pre_writers[-1]
        != {
            "presence": "absent",
            "running": False,
            "service": "worker-velocity",
        }
    ):
        raise ManifestError("original writer restoration intent is invalid")


def validate_manifest(path: Path) -> tuple[str, dict[str, Any]]:
    raw = _read_regular_nofollow(path, MAX_MANIFEST_BYTES, "retry manifest")
    document = _load_json(raw, "retry manifest")
    if raw != _canonical_pretty(document):
        raise ManifestError("retry manifest must use canonical sorted, indented JSON")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_MANIFEST_SHA256:
        raise ManifestError("retry manifest SHA-256 does not match reviewed bytes")
    _require_semantics(document)
    return digest, document


def _validate_parent_directories(path: Path) -> None:
    for directory in (Path("/var/lib/palimpsest-release"), path.parent):
        try:
            metadata = os.lstat(directory)
        except OSError as error:
            raise ManifestError(
                f"cannot inspect continuation directory {directory}: {error}"
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ManifestError(f"continuation directory is unsafe: {directory}")


def verify_host_continuation(document: dict[str, Any], repository_root: Path) -> str:
    continuation = document["continuation"]
    binding = continuation["predecessor_prepared_receipt"]
    prepared_path = Path(binding["path"])
    if (
        prepared_path != Path(PREDECESSOR_PREPARED_PATH)
        or not prepared_path.is_absolute()
    ):
        raise ManifestError(
            "prepared receipt path is not the exact bound absolute path"
        )
    _validate_parent_directories(prepared_path)
    raw = _read_regular_nofollow(
        prepared_path, MAX_RECEIPT_BYTES, "predecessor prepared receipt"
    )
    metadata = os.lstat(prepared_path)
    if (
        metadata.st_uid != binding["uid"]
        or metadata.st_gid != binding["gid"]
        or stat.S_IMODE(metadata.st_mode) != int(binding["mode"], 8)
        or metadata.st_nlink != binding["link_count"]
    ):
        raise ManifestError("predecessor prepared receipt ownership or mode is invalid")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != binding["sha256"] or digest != PREDECESSOR_PREPARED_SHA256:
        raise ManifestError("predecessor prepared receipt SHA-256 is invalid")
    prepared = _load_json(raw, "predecessor prepared receipt")
    if raw != _canonical_compact(prepared):
        raise ManifestError("predecessor prepared receipt is not canonical JSON")
    _require_keys(
        prepared,
        {
            "broker_queue_sha256",
            "compose_environment_sha256",
            "failed_target_commit",
            "hybrid_fingerprint_sha256",
            "incident_id",
            "manifest_sha256",
            "minimum_recovery_ancestor",
            "prepared_at",
            "prior_checkout_commit",
            "prior_deployed_commit",
            "recovery_controller_commit",
            "restore_profile_sha256",
            "schema_version",
            "status",
            "target_commit",
            "transaction_id",
        },
        "predecessor prepared receipt",
    )
    exact = {
        "broker_queue_sha256": "57cba36db8a74f1091b3478b831c833a6325023d57a8c4aa33190112e483f42b",
        "compose_environment_sha256": "2ce97c2f94ce93336b592e1ddee78cfdbec1e8b19d35b39faab6ac069d332c95",
        "failed_target_commit": "138a9eb323857ba91944fc04d0ccfabb653e7f24",
        "hybrid_fingerprint_sha256": "19551d94176f03148b052f68467ce9b626995940f3d8bcff495d27d46c0ade78",
        "incident_id": PREDECESSOR_INCIDENT_ID,
        "manifest_sha256": PREDECESSOR_MANIFEST_SHA256,
        "minimum_recovery_ancestor": "8b48162a13f719a4500c2297a337655d91dbb28e",
        "prior_checkout_commit": "7d05ecca47b20d8cf092a513a0db0390435f363f",
        "prior_deployed_commit": "95ea01d1a394fe219d64d3dce6b105296bce309a",
        "recovery_controller_commit": TARGET_COMMIT,
        "restore_profile_sha256": RESTORE_PROFILE_SHA256,
        "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
        "status": "prepared",
        "target_commit": TARGET_COMMIT,
        "transaction_id": PREDECESSOR_TRANSACTION_ID,
    }
    if any(prepared.get(key) != value for key, value in exact.items()):
        raise ManifestError("predecessor prepared receipt authority is invalid")
    try:
        prepared_at = datetime.datetime.fromisoformat(
            str(prepared["prepared_at"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ManifestError("predecessor prepared_at is invalid") from error
    if prepared_at.utcoffset() != datetime.timedelta(0):
        raise ManifestError("predecessor prepared_at is not UTC")

    completion_path = Path(continuation["predecessor_completion_receipt"]["path"])
    if completion_path != Path(PREDECESSOR_COMPLETION_PATH):
        raise ManifestError("predecessor completion path is not exact")
    try:
        os.lstat(completion_path)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ManifestError(
            f"cannot prove predecessor completion absence: {error}"
        ) from error
    else:
        raise ManifestError("predecessor completion receipt exists or is a symlink")

    old_manifest_path = repository_root / PREDECESSOR_MANIFEST_PATH
    old_raw = _read_regular_nofollow(
        old_manifest_path, MAX_MANIFEST_BYTES, "predecessor manifest"
    )
    if hashlib.sha256(old_raw).hexdigest() != PREDECESSOR_MANIFEST_SHA256:
        raise ManifestError("predecessor manifest bytes are invalid")
    old_manifest = _load_json(old_raw, "predecessor manifest")
    if old_raw != _canonical_pretty(old_manifest):
        raise ManifestError("predecessor manifest is not canonical JSON")
    if (
        old_manifest.get("incident_id") != PREDECESSOR_INCIDENT_ID
        or old_manifest.get("schema_version")
        != "palimpsest-interrupted-phase1-recovery.v1"
        or _canonical_digest(old_manifest.get("pre_failure_state"))
        != RESTORE_PROFILE_SHA256
        or old_manifest.get("pre_failure_state") != document["pre_failure_state"]
    ):
        raise ManifestError("predecessor manifest continuation semantics are invalid")
    return digest


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name(MANIFEST_NAME),
        help="retry manifest path (defaults to the reviewed sibling JSON)",
    )
    parser.add_argument(
        "--verify-host-continuation",
        action="store_true",
        help="also verify the bound root-owned predecessor receipt and absence",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path.cwd(),
        help="repository root used to verify the predecessor manifest",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    receipt_digest: str | None = None
    try:
        digest, document = validate_manifest(args.manifest)
        if args.verify_host_continuation:
            receipt_digest = verify_host_continuation(
                document, args.repository_root.resolve(strict=True)
            )
    except (ManifestError, OSError) as error:
        print(f"invalid API readiness retry manifest: {error}", file=sys.stderr)
        return 1
    if args.verify_host_continuation:
        assert receipt_digest is not None
        print(
            "validated API readiness retry host continuation: "
            f"manifest={digest} prepared={receipt_digest}"
        )
    else:
        print(f"validated API readiness retry manifest: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
