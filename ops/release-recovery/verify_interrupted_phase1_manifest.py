#!/usr/bin/env python3
"""Fail-closed verifier for the 2026-08-25 interrupted Phase 1 manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "palimpsest-interrupted-phase1-recovery.v1"
INCIDENT_ID = "2026-08-25-interrupted-phase1"
MANIFEST_NAME = "2026-08-25-interrupted-phase1.json"
MANIFEST_REPOSITORY_PATH = f"ops/release-recovery/{MANIFEST_NAME}"
EXPECTED_MANIFEST_SHA256 = (
    "f21ffb99a29902bc849c4c7e0ea0317720f24fa9adcef2068cd0ff40341cf535"
)
MAX_MANIFEST_BYTES = 65_536

_SCHEMA: dict[str, Any] = {
    "authority": {
        "failed_target_commit": str,
        "prior_checkout_commit": str,
        "prior_deployed_commit": str,
    },
    "failed_attempt": {
        "candidate_image": {
            "index_digest": str,
            "platform_manifest_digest": str,
            "revision": str,
        },
        "collectors_may_have_advanced": bool,
        "invalid_gate_artifacts": [
            {
                "name": str,
                "sha256": str,
                "size_bytes": int,
                "valid_receipt": bool,
            }
        ],
        "migration_applied": bool,
        "snapshot_ceiling": {
            "latest_snapshot_id": str,
            "new_snapshot_created": bool,
        },
    },
    "incident_date": str,
    "incident_id": str,
    "observed_safe_boundary": {
        "absent_compose_services": [str],
        "application_containers": [
            {
                "container_id": str,
                "image_index_digest": str,
                "revision": str,
                "service": str,
                "state": str,
            }
        ],
        "compose_environment_sha256": str,
        "compose_scope": {
            "config_files": str,
            "project": str,
            "working_dir": str,
        },
        "controlled_activators": {
            "all_disabled": bool,
            "all_inactive": bool,
        },
        "infrastructure_containers": [
            {
                "container_id": str,
                "image_id": str,
                "service": str,
                "state": str,
            }
        ],
        "installed_bundles": [
            {
                "current_symlink_path": str,
                "manifest_sha256": str,
                "resolved_target_path": str,
                "revision": str,
            }
        ],
        "installed_controller_boundary": {
            "absent_paths": [str],
            "present_files": [{"path": str, "sha256": str}],
        },
        "installed_units": [{"path": str, "sha256": str}],
        "local_application_tag": {
            "index_digest": str,
            "name": str,
            "platform_manifest_digest": str,
            "revision": str,
            "trusted_for_recovery": bool,
        },
        "repository": {
            "checkout_commit": str,
            "deployed_commit": str,
        },
        "running_compose_services": [str],
        "witness_inventory": [{"name": str, "sha256": str, "size_bytes": int}],
    },
    "pre_failure_state": {
        "activators": [
            {
                "active_state": str,
                "unit": str,
                "unit_file_state": str,
            }
        ],
        "application_image": {
            "config_digest": str,
            "index_digest": str,
            "platform_manifest_digest": str,
            "revision": str,
        },
        "compose_writers": [{"presence": str, "running": bool, "service": str}],
    },
    "recovery_target_constraints": {
        "must_be_descendant_of": str,
        "must_be_reviewed": bool,
        "must_contain_manifest_path": str,
    },
    "schema_version": str,
}


class ManifestError(ValueError):
    """The incident manifest is not the reviewed, immutable document."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ManifestError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _validate_shape(value: Any, schema: Any, location: str) -> None:
    if isinstance(schema, dict):
        if type(value) is not dict:
            raise ManifestError(f"{location} must be an object")
        expected = set(schema)
        actual = set(value)
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        if unknown:
            raise ManifestError(f"{location} has unknown fields: {unknown}")
        if missing:
            raise ManifestError(f"{location} is missing fields: {missing}")
        for key, child_schema in schema.items():
            _validate_shape(value[key], child_schema, f"{location}.{key}")
        return

    if isinstance(schema, list):
        if type(value) is not list:
            raise ManifestError(f"{location} must be an array")
        if not value or len(value) > 128:
            raise ManifestError(f"{location} must contain 1 to 128 entries")
        for index, item in enumerate(value):
            _validate_shape(item, schema[0], f"{location}[{index}]")
        return

    if schema is str:
        if type(value) is not str:
            raise ManifestError(f"{location} must be a string")
        if not value or len(value) > 512:
            raise ManifestError(f"{location} has an invalid string length")
        if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
            raise ManifestError(f"{location} must contain printable ASCII only")
        return

    if schema is bool:
        if type(value) is not bool:
            raise ManifestError(f"{location} must be a boolean")
        return

    if schema is int:
        if type(value) is not int or not 0 <= value <= 2_147_483_647:
            raise ManifestError(f"{location} must be a bounded non-negative integer")
        return

    raise AssertionError(f"unsupported verifier schema at {location}")


def _require_semantics(document: dict[str, Any]) -> None:
    authority = document["authority"]
    failed_target = authority["failed_target_commit"]
    prior_checkout = authority["prior_checkout_commit"]
    prior_deployed = authority["prior_deployed_commit"]
    constraints = document["recovery_target_constraints"]
    boundary = document["observed_safe_boundary"]

    if document["schema_version"] != SCHEMA_VERSION:
        raise ManifestError("unexpected manifest schema version")
    if (
        document["incident_id"] != INCIDENT_ID
        or document["incident_date"] != "2026-08-25"
    ):
        raise ManifestError("unexpected incident identity")
    if constraints != {
        "must_be_descendant_of": failed_target,
        "must_be_reviewed": True,
        "must_contain_manifest_path": MANIFEST_REPOSITORY_PATH,
    }:
        raise ManifestError("recovery target constraints are not fail-closed")
    if boundary["repository"] != {
        "checkout_commit": prior_checkout,
        "deployed_commit": prior_deployed,
    }:
        raise ManifestError("observed repository boundary contradicts authority")

    attempt = document["failed_attempt"]
    if attempt["migration_applied"] is not False:
        raise ManifestError("manifest must record that migration was not applied")
    if attempt["collectors_may_have_advanced"] is not True:
        raise ManifestError("manifest must preserve the collector-advance uncertainty")
    if attempt["snapshot_ceiling"]["new_snapshot_created"] is not False:
        raise ManifestError("manifest must not claim a new pre-change snapshot")
    if any(
        item["valid_receipt"] is not False for item in attempt["invalid_gate_artifacts"]
    ):
        raise ManifestError("invalid gate output must never be treated as a receipt")

    activators = document["pre_failure_state"]["activators"]
    if len(activators) != 12 or len({item["unit"] for item in activators}) != 12:
        raise ManifestError(
            "pre-failure activator inventory must contain 12 unique units"
        )
    disabled = [item for item in activators if item["unit_file_state"] == "disabled"]
    if disabled != [
        {
            "active_state": "inactive",
            "unit": "palimpsest-node-offsite-backup.timer",
            "unit_file_state": "disabled",
        }
    ]:
        raise ManifestError("pre-failure activator exception is invalid")
    if any(
        item["unit_file_state"] != "enabled" or item["active_state"] != "active"
        for item in activators
        if item not in disabled
    ):
        raise ManifestError("pre-failure enabled activators must all have been active")

    writers = document["pre_failure_state"]["compose_writers"]
    writer_names = [item["service"] for item in writers]
    if writer_names != [
        "beat",
        "worker",
        "worker-collectors",
        "worker-warehouse",
        "worker-velocity",
    ]:
        raise ManifestError("pre-failure writer inventory is invalid")
    if writers[-1] != {
        "presence": "absent",
        "running": False,
        "service": "worker-velocity",
    } or any(
        item["presence"] != "present" or item["running"] is not True
        for item in writers[:-1]
    ):
        raise ManifestError("pre-failure writer states are invalid")

    if boundary["controlled_activators"] != {
        "all_disabled": True,
        "all_inactive": True,
    }:
        raise ManifestError("observed boundary must remain quiesced")
    if boundary["running_compose_services"] != ["api", "postgres", "redis"]:
        raise ManifestError("observed running Compose set is invalid")
    if boundary["absent_compose_services"] != ["worker-velocity"]:
        raise ManifestError("observed absent Compose set is invalid")
    if boundary["compose_environment_sha256"] != (
        "2ce97c2f94ce93336b592e1ddee78cfdbec1e8b19d35b39faab6ac069d332c95"
    ):
        raise ManifestError("Compose environment digest is invalid")
    if boundary["compose_scope"] != {
        "config_files": (
            "/home/palimpsest/palimpsest/ops/docker/docker-compose.prod.yml"
        ),
        "project": "palimpsest",
        "working_dir": "/home/palimpsest/palimpsest/ops/docker",
    }:
        raise ManifestError("Compose project scope is invalid")
    if boundary["local_application_tag"]["trusted_for_recovery"] is not False:
        raise ManifestError("rebuilt local tag must remain untrusted for recovery")

    if boundary["infrastructure_containers"] != [
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
    ]:
        raise ManifestError("infrastructure container boundary is invalid")

    expected_bundle_paths = [
        "/usr/local/libexec/palimpsest-analysis/current",
        "/usr/local/libexec/palimpsest-network-lane/current",
        "/usr/local/libexec/palimpsest-common-crawl/current",
        "/usr/local/libexec/palimpsest-public-osint-sync/current",
        "/usr/local/libexec/palimpsest-node-offsite/current",
    ]
    installed_bundles = boundary["installed_bundles"]
    if [item["current_symlink_path"] for item in installed_bundles] != (
        expected_bundle_paths
    ) or any(item["revision"] != prior_deployed for item in installed_bundles):
        raise ManifestError("installed bundle boundary is not the prior deployment")
    if any(
        item["resolved_target_path"]
        != f"{item['current_symlink_path'].removesuffix('/current')}/{item['revision']}"
        for item in installed_bundles
    ):
        raise ManifestError("installed bundle resolved-target relation is invalid")
    expected_bundle_manifests = {
        "/usr/local/libexec/palimpsest-analysis/current": (
            "8864db5c14dc8d834b9eac51be5adb3fb19381fa240c30c38159f1305bca542a"
        ),
        "/usr/local/libexec/palimpsest-network-lane/current": (
            "0783cf1c90b4b3ae399b580e5df0e6568fb588b042605d110ee585d7b6169d66"
        ),
        "/usr/local/libexec/palimpsest-common-crawl/current": (
            "9556ba2245bda43a70aec4a3149e72748da60f9d3d19293b1b1a8c3eadb18138"
        ),
        "/usr/local/libexec/palimpsest-public-osint-sync/current": (
            "1868dc67d5f0d2ced080476d95fd21da69a75191ef0977dcd712f43888748a92"
        ),
        "/usr/local/libexec/palimpsest-node-offsite/current": (
            "8877cfc0cef4928a43fdf22129fa706746e9717307d22266523c00b69bb2c87d"
        ),
    }
    if {
        item["current_symlink_path"]: item["manifest_sha256"]
        for item in installed_bundles
    } != expected_bundle_manifests:
        raise ManifestError("installed bundle manifest hashes are invalid")
    if boundary["installed_controller_boundary"] != {
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
    }:
        raise ManifestError("installed controller boundary is not the audited state")

    for field in (
        "prior_checkout_commit",
        "prior_deployed_commit",
        "failed_target_commit",
    ):
        value = authority[field]
        if len(value) != 40 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ManifestError(
                f"authority.{field} must be a lowercase full Git commit"
            )


def _read_manifest_bytes(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ManifestError(
            f"cannot open manifest without following symlinks: {error}"
        ) from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ManifestError("manifest descriptor must reference a regular file")
        if metadata.st_nlink != 1:
            raise ManifestError("manifest must have exactly one hard link")
        if not 0 < metadata.st_size <= MAX_MANIFEST_BYTES:
            raise ManifestError("manifest size is outside the accepted bounds")

        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = MAX_MANIFEST_BYTES + 1 - total
            if remaining <= 0:
                raise ManifestError("manifest grew beyond the accepted size bound")
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > metadata.st_size:
                raise ManifestError("manifest changed size or has trailing growth")

        final_metadata = os.fstat(descriptor)
        initial_identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        final_identity = (
            final_metadata.st_dev,
            final_metadata.st_ino,
            final_metadata.st_mode,
            final_metadata.st_nlink,
            final_metadata.st_size,
            final_metadata.st_mtime_ns,
            final_metadata.st_ctime_ns,
        )
        if total != metadata.st_size or final_identity != initial_identity:
            raise ManifestError("manifest descriptor changed while it was being read")
        return b"".join(chunks)
    except OSError as error:
        raise ManifestError(f"cannot read manifest descriptor: {error}") from error
    finally:
        os.close(descriptor)


def validate_manifest(path: Path) -> str:
    """Validate *path* and return the SHA-256 of its exact reviewed bytes."""

    try:
        raw = _read_manifest_bytes(path)
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(
            f"cannot read strict UTF-8 JSON manifest: {error}"
        ) from error
    if type(document) is not dict:
        raise ManifestError("manifest root must be an object")

    _validate_shape(document, _SCHEMA, "manifest")
    _require_semantics(document)

    canonical = (
        json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")
    if raw != canonical:
        raise ManifestError("manifest must use canonical sorted, indented JSON bytes")

    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_MANIFEST_SHA256:
        raise ManifestError(
            "incident manifest SHA-256 does not match the reviewed bytes"
        )
    return digest


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name(MANIFEST_NAME),
        help="manifest path (defaults to the reviewed sibling JSON file)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        digest = validate_manifest(args.manifest)
    except ManifestError as error:
        print(
            f"invalid interrupted Phase 1 recovery manifest: {error}", file=sys.stderr
        )
        return 1
    print(f"validated interrupted Phase 1 recovery manifest: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
