#!/usr/bin/env python3
"""Fail-closed verifier for the Common Crawl bind-alias release retry."""

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
INCIDENT_ID = "2026-08-25-common-crawl-bind-alias-retry"
MANIFEST_NAME = f"{INCIDENT_ID}.json"
MANIFEST_REPOSITORY_PATH = f"ops/release-recovery/{MANIFEST_NAME}"
EXPECTED_MANIFEST_SHA256 = (
    "62dd4970775c4acc840649f4531c50f73dc73906ad816d7bf45c49e1f323d834"
)
TARGET_COMMIT = "913a6aa64e705bd5d2b2f6f022a91e07389999e0"
RESTORE_PROFILE_SHA256 = (
    "705aaf3b5e52cc400fcb51957f0f2cf6167869e86b4622637b01ad512641c08a"
)
SAFE_BOUNDARY_SHA256 = (
    "ea2956a5d083da4f2e55cdee7330675b28fc2514d6028640d23fb5f129a8c954"
)

PREDECESSOR_INCIDENT_ID = "2026-08-25-api-readiness-retry"
PREDECESSOR_MANIFEST_PATH = "ops/release-recovery/2026-08-25-api-readiness-retry.json"
PREDECESSOR_MANIFEST_SHA256 = (
    "6a3a393a7f9ebdfb6fb38cf984db4f4558b3af9fa7cc973683116c274d9d3218"
)
PREDECESSOR_PREPARED_PATH = (
    "/var/lib/palimpsest-release/recovery/2026-08-25-api-readiness-retry.prepared.json"
)
PREDECESSOR_PREPARED_SHA256 = (
    "1699c22c16241f971b344b93e972f6358aae974352dccbac7cfe61114467b561"
)
PREDECESSOR_COMPLETION_PATH = (
    "/var/lib/palimpsest-release/recovery/2026-08-25-api-readiness-retry.complete.json"
)
PREDECESSOR_TRANSACTION_ID = "81459025a36873031dba693c229baa7c"

ORIGINAL_INCIDENT_ID = "2026-08-25-interrupted-phase1"
ORIGINAL_MANIFEST_PATH = "ops/release-recovery/2026-08-25-interrupted-phase1.json"
ORIGINAL_MANIFEST_SHA256 = (
    "f21ffb99a29902bc849c4c7e0ea0317720f24fa9adcef2068cd0ff40341cf535"
)
ORIGINAL_PREPARED_PATH = (
    "/var/lib/palimpsest-release/recovery/2026-08-25-interrupted-phase1.prepared.json"
)
ORIGINAL_PREPARED_SHA256 = (
    "e9f506a44e19f78ecb094bd13c5d7c29f62f894174a5213de67b402b42a74f66"
)
ORIGINAL_COMPLETION_PATH = (
    "/var/lib/palimpsest-release/recovery/2026-08-25-interrupted-phase1.complete.json"
)
ORIGINAL_TRANSACTION_ID = "ff12146621a04cd507df19cb0665b32f"

DYNAMIC_RELEASE_INSTANCE_UNITS = (
    "palimpsest-investigative-broker@580-4016203-10001.service",
    "palimpsest-investigative-broker@581-4021663-10001.service",
    "palimpsest-investigative-broker@582-4026947-10001.service",
    "palimpsest-investigative-broker@583-4099297-10001.service",
    "palimpsest-investigative-broker@584-4106088-10001.service",
    "palimpsest-investigative-broker@585-4111243-10001.service",
    "palimpsest-investigative-broker@586-4186255-10001.service",
    "palimpsest-investigative-broker@587-4193386-10001.service",
    "palimpsest-investigative-broker@588-4559-10001.service",
    "palimpsest-investigative-broker@589-70881-10001.service",
    "palimpsest-investigative-broker@590-75857-10001.service",
    "palimpsest-investigative-broker@591-80778-10001.service",
    "palimpsest-investigative-broker@592-143327-10001.service",
    "palimpsest-investigative-broker@593-149774-10001.service",
    "palimpsest-investigative-broker@594-155056-10001.service",
    "palimpsest-investigative-broker@595-216237-10001.service",
    "palimpsest-investigative-broker@596-221118-10001.service",
    "palimpsest-investigative-broker@597-226125-10001.service",
    "palimpsest-investigative-broker@598-358278-10001.service",
    "palimpsest-investigative-broker@599-363134-10001.service",
    "palimpsest-investigative-broker@600-368678-10001.service",
    "palimpsest-investigative-broker@601-431584-10001.service",
    "palimpsest-investigative-broker@602-436582-10001.service",
    "palimpsest-investigative-broker@603-442171-10001.service",
    "palimpsest-investigative-broker@604-3267185-10001.service",
    "palimpsest-investigative-broker@605-3272661-10001.service",
    "palimpsest-investigative-broker@606-3277521-10001.service",
    "palimpsest-investigative-broker@607-3314743-10001.service",
    "palimpsest-investigative-broker@608-3320490-10001.service",
    "palimpsest-investigative-broker@609-3328346-10001.service",
)

RELEASE_ROOT = Path("/var/lib/palimpsest-release")
RECOVERY_DIRECTORY = RELEASE_ROOT / "recovery"
CONTINUATION_UID = 0
CONTINUATION_GID = 0
MAX_MANIFEST_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 64 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TRANSACTION = re.compile(r"^[0-9a-f]{32}$")


class ManifestError(ValueError):
    """The manifest or its bound continuation is not reviewed authority."""


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


def _require_keys(value: object, expected: set[str], label: str) -> None:
    if type(value) is not dict:
        raise ManifestError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise ManifestError(
            f"{label} fields differ: missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
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
        raise ManifestError("retry authority is not the exact failed target")
    if any(_COMMIT.fullmatch(str(value)) is None for value in authority.values()):
        raise ManifestError("retry authority contains a malformed commit")

    if document["recovery_target_constraints"] != {
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
    if continuation["predecessor_prepared_receipt"] != {
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
    if continuation["predecessor_restore_profile_sha256"] != (RESTORE_PROFILE_SHA256):
        raise ManifestError("predecessor restoration profile binding is invalid")

    failed = document["failed_attempt"]
    _require_keys(
        failed,
        {
            "candidate_image",
            "collectors_may_have_advanced",
            "common_crawl_import_started",
            "common_crawl_mount_identity",
            "migration_applied",
            "migration_exit_code",
            "migration_result",
            "phase1_handoff_created",
            "phase2_started",
            "phase3_binding_created",
            "post_failure_diagnostic",
            "recovery_backup_reason",
            "release_receipts_created",
            "snapshot_ceiling",
        },
        "failed attempt",
    )
    if (
        failed["migration_applied"] is not True
        or failed["migration_exit_code"] != 0
        or failed["collectors_may_have_advanced"] is not True
        or failed["common_crawl_import_started"] is not False
        or failed["phase1_handoff_created"] is not False
        or failed["phase2_started"] is not False
        or failed["phase3_binding_created"] is not False
        or failed["release_receipts_created"] is not False
        or failed["recovery_backup_reason"]
        != "common-crawl-bind-alias-retry-fresh-target-backup"
    ):
        raise ManifestError("failed-attempt recovery facts are invalid")

    mount = failed["common_crawl_mount_identity"]
    expected_mount = {
        "container_id": (
            "113d06ed615d4774dfa64a0f85f259b95ba6fc2d90de41fde13db7376a23c72a"
        ),
        "destination": "/app/common-crawl-derived",
        "expected_source": (
            "/mnt/HC_Volume_106588294/palimpsest/warehouse/common-crawl/derived"
        ),
        "failure_message": (
            "collector Common Crawl mount source path differed from the "
            "configured warehouse path"
        ),
        "feature_sha256": (
            "71f7089a826e42bc5fb6dc3893bf12ae08a3940583b1adc23754f0edbc44ad9f"
        ),
        "mount_type": "bind",
        "observed_source": "/var/lib/palimpsest/common-crawl/derived",
        "read_only": True,
        "source_identity": {
            "device": 2064,
            "gid": 10001,
            "inode": 62128631,
            "mode": "0700",
            "uid": 10001,
        },
    }
    if mount != expected_mount:
        raise ManifestError("Common Crawl same-inode mount failure facts are invalid")
    if mount["expected_source"] == mount["observed_source"]:
        raise ManifestError("Common Crawl aliases must remain distinct path spellings")
    if (
        _CONTAINER.fullmatch(mount["container_id"]) is None
        or _SHA256.fullmatch(mount["feature_sha256"]) is None
        or mount["source_identity"]["device"] <= 0
        or mount["source_identity"]["inode"] <= 0
    ):
        raise ManifestError("Common Crawl mount identity is malformed")

    candidate = failed["candidate_image"]
    if candidate != {
        "config_digest": (
            "sha256:211997b27d95a5221f461df999d64445bc982f53e5780f81aebff71f2977c5b9"
        ),
        "index_digest": (
            "sha256:3053782ef36db915ffd0bd2209da02d429fe5487e3875a1c260678cbf0690acd"
        ),
        "platform_manifest_digest": (
            "sha256:099ccc4be1bc7be4009c2fd1f98f8237fab4b4c06084975fa8f3f363b726b7ec"
        ),
        "revision": TARGET_COMMIT,
    }:
        raise ManifestError("candidate image authority is invalid")
    migration = failed["migration_result"]
    if migration != {
        "container_id": (
            "d2738ba0d227613dd4d126522074954074239bf2cdfb2ed7be1da30a4aa02c9e"
        ),
        "exit_code": 0,
        "finished_at": "2026-08-25T21:22:40.434400461Z",
        "image_id": candidate["index_digest"],
        "revision": TARGET_COMMIT,
        "started_at": "2026-08-25T21:22:36.38872758Z",
        "state": "exited",
    }:
        raise ManifestError("migration result projection is invalid")
    diagnostic = failed["post_failure_diagnostic"]
    if diagnostic != {
        "activation_cause": "accidental activation by a diagnostic watcher command",
        "collector_container_id": mount["container_id"],
        "exit_code": 0,
        "finished_at": "2026-08-25T21:25:27.690221376Z",
        "restored_to_quiescent": True,
        "started_at": "2026-08-25T21:24:11.877145308Z",
    }:
        raise ManifestError("post-failure diagnostic projection is invalid")

    snapshot = failed["snapshot_ceiling"]
    verification = snapshot.get("verification") if type(snapshot) is dict else None
    if (
        snapshot.get("latest_snapshot_id") != "20260825T212106Z"
        or snapshot.get("new_snapshot_created") is not True
        or type(verification) is not dict
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

    boundary = document["observed_safe_boundary"]
    if _canonical_digest(boundary) != SAFE_BOUNDARY_SHA256:
        raise ManifestError("full observed safe-boundary digest is invalid")
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
        "observed safe boundary",
    )
    if boundary["repository"] != {
        "checkout_commit": TARGET_COMMIT,
        "deployed_commit": TARGET_COMMIT,
    }:
        raise ManifestError("repository checkpoint contradicts authority")
    if boundary["compose_environment_sha256"] != (
        "2ce97c2f94ce93336b592e1ddee78cfdbec1e8b19d35b39faab6ac069d332c95"
    ) or boundary["compose_scope"] != {
        "config_files": (
            "/home/palimpsest/palimpsest/ops/docker/docker-compose.prod.yml"
        ),
        "project": "palimpsest",
        "working_dir": "/home/palimpsest/palimpsest/ops/docker",
    }:
        raise ManifestError("Compose environment boundary is invalid")
    if boundary["running_compose_services"] != ["api", "postgres", "redis"]:
        raise ManifestError("running Compose service set is invalid")
    if boundary["absent_compose_services"] != [
        "censorwatch-render-gateway",
        "worker-velocity",
    ]:
        raise ManifestError("absent Compose service set is invalid")
    expected_dynamic_instances = [
        {
            "active_state": "failed",
            "fragment_path": (
                "/etc/systemd/system/palimpsest-investigative-broker@.service"
            ),
            "load_state": "loaded",
            "sub_state": "failed",
            "unit": unit,
        }
        for unit in DYNAMIC_RELEASE_INSTANCE_UNITS
    ]
    if boundary["dynamic_release_instances"] != expected_dynamic_instances:
        raise ManifestError("recorded dynamic release instance boundary is invalid")

    applications = boundary["application_containers"]
    if [item.get("service") for item in applications] != [
        "api",
        "beat",
        "migrate",
        "worker",
        "worker-collectors",
        "worker-warehouse",
    ] or any(
        _CONTAINER.fullmatch(str(item.get("container_id", ""))) is None
        for item in applications
    ):
        raise ManifestError("application container inventory is invalid")
    application_by_service = {item["service"]: item for item in applications}
    if (
        application_by_service["api"]["state"] != "running"
        or application_by_service["api"]["health"] != "healthy"
        or application_by_service["api"]["revision"] != TARGET_COMMIT
        or any(item["state"] != "exited" for item in applications[1:])
        or any(
            application_by_service[service]["revision"] != TARGET_COMMIT
            for service in (
                "migrate",
                "worker",
                "worker-collectors",
                "worker-warehouse",
            )
        )
    ):
        raise ManifestError("application quiescence boundary is invalid")
    if application_by_service["migrate"]["container_id"] != migration["container_id"]:
        raise ManifestError("migration container is not bound to the safe boundary")
    if (
        application_by_service["worker-collectors"]["container_id"]
        != mount["container_id"]
    ):
        raise ManifestError("collector container is not bound to mount evidence")

    local_image = boundary["local_application_tag"]
    for key in (
        "config_digest",
        "index_digest",
        "platform_manifest_digest",
        "revision",
    ):
        if local_image.get(key) != candidate[key]:
            raise ManifestError(f"candidate/local image projection differs: {key}")
    if local_image.get("trusted_for_recovery") is not False:
        raise ManifestError("mutable local tag is incorrectly trusted")

    infrastructure = boundary["infrastructure_containers"]
    if [item.get("service") for item in infrastructure] != ["postgres", "redis"] or any(
        item.get("state") != "running" or item.get("health") != "healthy"
        for item in infrastructure
    ):
        raise ManifestError("infrastructure container boundary is invalid")

    activators = boundary["controlled_activators"]
    inventory = activators.get("inventory") if type(activators) is dict else None
    if (
        activators.get("all_disabled") is not True
        or activators.get("all_inactive") is not True
        or type(inventory) is not list
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
        or any(
            not item.get("path", "").startswith(
                ("/etc/palimpsest/", "/opt/palimpsest/")
            )
            or _SHA256.fullmatch(str(item.get("sha256", ""))) is None
            or item.get("stat") not in {"0:0:444:1", "0:0:755:1"}
            for item in controllers.get("present_files", [])
        )
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

    pre_failure = document["pre_failure_state"]
    if _canonical_digest(pre_failure) != RESTORE_PROFILE_SHA256:
        raise ManifestError("original restoration profile was not preserved exactly")
    pre_activators = pre_failure.get("activators", [])
    pre_writers = pre_failure.get("compose_writers", [])
    if (
        len(pre_activators) != 12
        or sum(item.get("active_state") == "active" for item in pre_activators) != 11
        or pre_activators[2]
        != {
            "active_state": "inactive",
            "unit": "palimpsest-node-offsite-backup.timer",
            "unit_file_state": "disabled",
        }
    ):
        raise ManifestError("original activator restoration intent is invalid")
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
    if path.parent != RECOVERY_DIRECTORY:
        raise ManifestError(f"continuation path has an unexpected parent: {path}")
    for directory in (RELEASE_ROOT, RECOVERY_DIRECTORY):
        try:
            metadata = os.lstat(directory)
        except OSError as error:
            raise ManifestError(
                f"cannot inspect continuation directory {directory}: {error}"
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != CONTINUATION_UID
            or metadata.st_gid != CONTINUATION_GID
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ManifestError(f"continuation directory is unsafe: {directory}")


_RECEIPT_KEYS = {
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
}


def _verify_prepared_receipt(
    binding: dict[str, Any],
    *,
    expected_path: str,
    expected_sha256: str,
    expected_authority: dict[str, str],
    label: str,
) -> str:
    prepared_path = Path(binding.get("path", ""))
    if prepared_path != Path(expected_path) or not prepared_path.is_absolute():
        raise ManifestError(f"{label} path is not the exact bound absolute path")
    _validate_parent_directories(prepared_path)
    raw = _read_regular_nofollow(prepared_path, MAX_RECEIPT_BYTES, label)
    metadata = os.lstat(prepared_path)
    try:
        expected_mode = int(str(binding["mode"]), 8)
    except (KeyError, TypeError, ValueError) as error:
        raise ManifestError(f"{label} mode binding is invalid") from error
    if (
        metadata.st_uid != binding.get("uid")
        or metadata.st_gid != binding.get("gid")
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or metadata.st_nlink != binding.get("link_count")
        or expected_mode != 0o400
    ):
        raise ManifestError(f"{label} ownership or mode is invalid")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != binding.get("sha256") or digest != expected_sha256:
        raise ManifestError(f"{label} SHA-256 is invalid")
    prepared = _load_json(raw, label)
    if raw != _canonical_compact(prepared):
        raise ManifestError(f"{label} is not canonical JSON")
    _require_keys(prepared, _RECEIPT_KEYS, label)
    for key, value in expected_authority.items():
        if prepared.get(key) != value:
            raise ManifestError(f"{label} authority is invalid: {key}")
    if any(
        _SHA256.fullmatch(str(prepared[key])) is None
        for key in (
            "broker_queue_sha256",
            "compose_environment_sha256",
            "hybrid_fingerprint_sha256",
            "manifest_sha256",
            "restore_profile_sha256",
        )
    ) or any(
        _COMMIT.fullmatch(str(prepared[key])) is None
        for key in (
            "failed_target_commit",
            "minimum_recovery_ancestor",
            "prior_checkout_commit",
            "prior_deployed_commit",
            "recovery_controller_commit",
            "target_commit",
        )
    ):
        raise ManifestError(f"{label} authority contains malformed digests")
    if _TRANSACTION.fullmatch(str(prepared["transaction_id"])) is None:
        raise ManifestError(f"{label} transaction ID is malformed")
    try:
        prepared_at = datetime.datetime.fromisoformat(
            str(prepared["prepared_at"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ManifestError(f"{label} prepared_at is invalid") from error
    if prepared_at.utcoffset() != datetime.timedelta(0):
        raise ManifestError(f"{label} prepared_at is not UTC")
    return digest


def _prove_absent(path_value: str, expected_path: str, label: str) -> None:
    path = Path(path_value)
    if path != Path(expected_path) or not path.is_absolute():
        raise ManifestError(f"{label} path is not exact")
    _validate_parent_directories(path)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ManifestError(f"cannot prove {label} absence: {error}") from error
    raise ManifestError(f"{label} exists or is a symlink")


def _load_bound_manifest(
    repository_root: Path,
    binding: dict[str, Any],
    *,
    expected_path: str,
    expected_sha256: str,
    label: str,
) -> dict[str, Any]:
    if binding != {"path": expected_path, "sha256": expected_sha256}:
        raise ManifestError(f"{label} binding is invalid")
    relative = Path(expected_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ManifestError(f"{label} repository path is unsafe")
    path = repository_root / relative
    raw = _read_regular_nofollow(path, MAX_MANIFEST_BYTES, label)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ManifestError(f"{label} bytes are invalid")
    document = _load_json(raw, label)
    if raw != _canonical_pretty(document):
        raise ManifestError(f"{label} is not canonical JSON")
    return document


def verify_host_continuation(
    document: dict[str, Any], repository_root: Path
) -> tuple[str, str]:
    continuation = document["continuation"]
    predecessor = _load_bound_manifest(
        repository_root,
        continuation["predecessor_manifest"],
        expected_path=PREDECESSOR_MANIFEST_PATH,
        expected_sha256=PREDECESSOR_MANIFEST_SHA256,
        label="predecessor retry manifest",
    )
    if (
        predecessor.get("incident_id") != PREDECESSOR_INCIDENT_ID
        or predecessor.get("schema_version") != SCHEMA_VERSION
        or _canonical_digest(predecessor.get("pre_failure_state"))
        != RESTORE_PROFILE_SHA256
        or predecessor.get("pre_failure_state") != document["pre_failure_state"]
    ):
        raise ManifestError("predecessor retry continuation semantics are invalid")

    predecessor_continuation = predecessor.get("continuation")
    _require_keys(
        predecessor_continuation,
        {
            "predecessor_completion_receipt",
            "predecessor_incident_id",
            "predecessor_manifest",
            "predecessor_prepared_receipt",
            "predecessor_restore_profile_sha256",
        },
        "predecessor retry continuation",
    )
    if predecessor_continuation["predecessor_incident_id"] != ORIGINAL_INCIDENT_ID:
        raise ManifestError("original predecessor incident is invalid")
    if predecessor_continuation["predecessor_restore_profile_sha256"] != (
        RESTORE_PROFILE_SHA256
    ):
        raise ManifestError("original restoration profile binding is invalid")

    original = _load_bound_manifest(
        repository_root,
        predecessor_continuation["predecessor_manifest"],
        expected_path=ORIGINAL_MANIFEST_PATH,
        expected_sha256=ORIGINAL_MANIFEST_SHA256,
        label="original recovery manifest",
    )
    if (
        original.get("incident_id") != ORIGINAL_INCIDENT_ID
        or original.get("schema_version") != "palimpsest-interrupted-phase1-recovery.v1"
        or _canonical_digest(original.get("pre_failure_state"))
        != RESTORE_PROFILE_SHA256
        or original.get("pre_failure_state") != predecessor["pre_failure_state"]
        or original.get("pre_failure_state") != document["pre_failure_state"]
    ):
        raise ManifestError("original recovery continuation semantics are invalid")

    predecessor_binding = continuation["predecessor_prepared_receipt"]
    predecessor_digest = _verify_prepared_receipt(
        predecessor_binding,
        expected_path=PREDECESSOR_PREPARED_PATH,
        expected_sha256=PREDECESSOR_PREPARED_SHA256,
        expected_authority={
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
            "incident_id": PREDECESSOR_INCIDENT_ID,
            "manifest_sha256": PREDECESSOR_MANIFEST_SHA256,
            "minimum_recovery_ancestor": ("1ae25399c7b36dca60e316cc966ea7d9636ec62b"),
            "prior_checkout_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
            "prior_deployed_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
            "recovery_controller_commit": TARGET_COMMIT,
            "restore_profile_sha256": RESTORE_PROFILE_SHA256,
            "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
            "status": "prepared",
            "target_commit": TARGET_COMMIT,
            "transaction_id": PREDECESSOR_TRANSACTION_ID,
        },
        label="predecessor prepared receipt",
    )

    original_binding = predecessor_continuation["predecessor_prepared_receipt"]
    if original_binding != {
        "gid": CONTINUATION_GID,
        "link_count": 1,
        "mode": "0400",
        "path": ORIGINAL_PREPARED_PATH,
        "recovery_controller_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
        "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
        "sha256": ORIGINAL_PREPARED_SHA256,
        "status": "prepared",
        "target_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
        "transaction_id": ORIGINAL_TRANSACTION_ID,
        "uid": CONTINUATION_UID,
    }:
        raise ManifestError("original prepared-receipt binding is invalid")
    original_digest = _verify_prepared_receipt(
        original_binding,
        expected_path=ORIGINAL_PREPARED_PATH,
        expected_sha256=ORIGINAL_PREPARED_SHA256,
        expected_authority={
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
            "incident_id": ORIGINAL_INCIDENT_ID,
            "manifest_sha256": ORIGINAL_MANIFEST_SHA256,
            "minimum_recovery_ancestor": ("8b48162a13f719a4500c2297a337655d91dbb28e"),
            "prior_checkout_commit": "7d05ecca47b20d8cf092a513a0db0390435f363f",
            "prior_deployed_commit": "95ea01d1a394fe219d64d3dce6b105296bce309a",
            "recovery_controller_commit": ("1ae25399c7b36dca60e316cc966ea7d9636ec62b"),
            "restore_profile_sha256": RESTORE_PROFILE_SHA256,
            "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
            "status": "prepared",
            "target_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
            "transaction_id": ORIGINAL_TRANSACTION_ID,
        },
        label="original prepared receipt",
    )

    predecessor_completion = continuation["predecessor_completion_receipt"]
    if predecessor_completion.get("expected_absent") is not True:
        raise ManifestError("predecessor completion must be bound absent")
    _prove_absent(
        predecessor_completion.get("path", ""),
        PREDECESSOR_COMPLETION_PATH,
        "predecessor completion receipt",
    )
    original_completion = predecessor_continuation["predecessor_completion_receipt"]
    if original_completion != {
        "expected_absent": True,
        "path": ORIGINAL_COMPLETION_PATH,
    }:
        raise ManifestError("original completion absence binding is invalid")
    _prove_absent(
        original_completion["path"],
        ORIGINAL_COMPLETION_PATH,
        "original completion receipt",
    )
    return predecessor_digest, original_digest


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
        help="verify both immutable predecessor receipts and manifest chain",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path.cwd(),
        help="repository root used to verify both predecessor manifests",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    prepared_digests: tuple[str, str] | None = None
    try:
        digest, document = validate_manifest(args.manifest)
        if args.verify_host_continuation:
            prepared_digests = verify_host_continuation(
                document, args.repository_root.resolve(strict=True)
            )
    except (ManifestError, OSError) as error:
        print(
            f"invalid Common Crawl bind-alias retry manifest: {error}", file=sys.stderr
        )
        return 1
    if args.verify_host_continuation:
        assert prepared_digests is not None
        print(
            "validated Common Crawl bind-alias retry host continuation: "
            f"manifest={digest} prepared={prepared_digests[0]} "
            f"original_prepared={prepared_digests[1]}"
        )
    else:
        print(f"validated Common Crawl bind-alias retry manifest: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
