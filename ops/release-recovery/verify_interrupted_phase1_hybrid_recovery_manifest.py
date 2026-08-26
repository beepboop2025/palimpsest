#!/usr/bin/env python3
"""Fail-closed verifier for the interrupted Phase 1 hybrid recovery record."""

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
INCIDENT_ID = "2026-08-26-interrupted-phase1-hybrid-recovery"
MANIFEST_NAME = f"{INCIDENT_ID}.json"
MANIFEST_REPOSITORY_PATH = f"ops/release-recovery/{MANIFEST_NAME}"
EXPECTED_MANIFEST_SHA256 = (
    "8ebbec1471a60f6112c521a2783efd3fda1d5c5fea352c087f31f62dd9d153af"
)

CHECKPOINT_COMMIT = "927e0a8b5c82a008f3ffa08a5f5518b8efa8bffd"
PARTIAL_RUNTIME_COMMIT = "15edd4fe13103e68da53c651a15c7c0aa1aed4a3"
LEGACY_BEAT_COMMIT = "7d05ecca47b20d8cf092a513a0db0390435f363f"
RESTORE_PROFILE_SHA256 = (
    "705aaf3b5e52cc400fcb51957f0f2cf6167869e86b4622637b01ad512641c08a"
)
FAILED_ATTEMPT_SHA256 = (
    "9dc5735ee6705a75d3757617e384cf0985dbb86497bb56b577c94e2a21522ad7"
)
SAFE_BOUNDARY_SHA256 = (
    "f8720d97ec64f1245368ba716cf5136dfff31cc30b119eacf899815affdbd756"
)

PREDECESSOR_INCIDENT_ID = "2026-08-25-common-crawl-bind-alias-retry"
PREDECESSOR_MANIFEST_PATH = (
    "ops/release-recovery/2026-08-25-common-crawl-bind-alias-retry.json"
)
PREDECESSOR_MANIFEST_SHA256 = (
    "62dd4970775c4acc840649f4531c50f73dc73906ad816d7bf45c49e1f323d834"
)
CANONICAL_PREPARED_PATH = (
    "/var/lib/palimpsest-release/recovery/"
    "2026-08-25-common-crawl-bind-alias-retry.prepared.json"
)
PREDECESSOR_COMPLETION_PATH = (
    "/var/lib/palimpsest-release/recovery/"
    "2026-08-25-common-crawl-bind-alias-retry.complete.json"
)

EARLIER_ATTEMPT_PATH = (
    "/var/lib/palimpsest-release/recovery/"
    "2026-08-25-common-crawl-bind-alias-retry."
    "attempt-20260826T060005Z.2e0b16ef28eb960c0d9c8b82c7d83451665f0e21."
    "prepared.json"
)
EARLIER_ATTEMPT_SHA256 = (
    "498b4b53679ae5a963752b1684b7399b27471f4958cbcbefccc5f1cd5b622d17"
)
EARLIER_ATTEMPT_TARGET = "2e0b16ef28eb960c0d9c8b82c7d83451665f0e21"
EARLIER_ATTEMPT_TRANSACTION = "ab4c79121d12837feb67976fda8d29e0"

LATEST_ATTEMPT_PATH = (
    "/var/lib/palimpsest-release/recovery/"
    "2026-08-25-common-crawl-bind-alias-retry."
    "attempt-20260826T063159Z.15edd4fe13103e68da53c651a15c7c0aa1aed4a3."
    "prepared.json"
)
LATEST_ATTEMPT_SHA256 = (
    "56f687021b54f1fe7acba2dc9cb5e98e4ce857ec0169ac0dff99257630ab8751"
)
LATEST_ATTEMPT_TRANSACTION = "6c45a54311cd2e4d125944149bb2a2b1"

API_INCIDENT_ID = "2026-08-25-api-readiness-retry"
API_MANIFEST_PATH = "ops/release-recovery/2026-08-25-api-readiness-retry.json"
API_MANIFEST_SHA256 = (
    "6a3a393a7f9ebdfb6fb38cf984db4f4558b3af9fa7cc973683116c274d9d3218"
)
API_PREPARED_PATH = (
    "/var/lib/palimpsest-release/recovery/"
    "2026-08-25-api-readiness-retry.prepared.json"
)
API_PREPARED_SHA256 = (
    "1699c22c16241f971b344b93e972f6358aae974352dccbac7cfe61114467b561"
)
API_COMPLETION_PATH = (
    "/var/lib/palimpsest-release/recovery/"
    "2026-08-25-api-readiness-retry.complete.json"
)
API_TRANSACTION_ID = "81459025a36873031dba693c229baa7c"

ORIGINAL_INCIDENT_ID = "2026-08-25-interrupted-phase1"
ORIGINAL_MANIFEST_PATH = "ops/release-recovery/2026-08-25-interrupted-phase1.json"
ORIGINAL_MANIFEST_SHA256 = (
    "f21ffb99a29902bc849c4c7e0ea0317720f24fa9adcef2068cd0ff40341cf535"
)
ORIGINAL_PREPARED_PATH = (
    "/var/lib/palimpsest-release/recovery/"
    "2026-08-25-interrupted-phase1.prepared.json"
)
ORIGINAL_PREPARED_SHA256 = (
    "e9f506a44e19f78ecb094bd13c5d7c29f62f894174a5213de67b402b42a74f66"
)
ORIGINAL_COMPLETION_PATH = (
    "/var/lib/palimpsest-release/recovery/"
    "2026-08-25-interrupted-phase1.complete.json"
)
ORIGINAL_TRANSACTION_ID = "ff12146621a04cd507df19cb0665b32f"

RELEASE_ROOT = Path("/var/lib/palimpsest-release")
RECOVERY_DIRECTORY = RELEASE_ROOT / "recovery"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 64 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TRANSACTION = re.compile(r"^[0-9a-f]{32}$")


class ManifestError(ValueError):
    """The recovery manifest or its continuation is not reviewed authority."""


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
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _require_keys(value: object, expected: set[str], label: str) -> None:
    if type(value) is not dict:
        raise ManifestError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise ManifestError(
            f"{label} fields differ: missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _attempt_binding(
    path: str, digest: str, target: str, transaction: str
) -> dict[str, Any]:
    return {
        "gid": 0,
        "link_count": 1,
        "mode": "0400",
        "path": path,
        "recovery_controller_commit": target,
        "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
        "sha256": digest,
        "status": "prepared",
        "target_commit": target,
        "transaction_id": transaction,
        "uid": 0,
    }


def _expected_attempt_bindings() -> list[dict[str, Any]]:
    return [
        _attempt_binding(
            EARLIER_ATTEMPT_PATH,
            EARLIER_ATTEMPT_SHA256,
            EARLIER_ATTEMPT_TARGET,
            EARLIER_ATTEMPT_TRANSACTION,
        ),
        _attempt_binding(
            LATEST_ATTEMPT_PATH,
            LATEST_ATTEMPT_SHA256,
            PARTIAL_RUNTIME_COMMIT,
            LATEST_ATTEMPT_TRANSACTION,
        ),
    ]


def _expected_applications() -> list[dict[str, Any]]:
    return [
        {
            "container_id": (
                "bfff6f10241a0daaac38e348737773be5e97384b6af2aabff6c9b63c1edbf406"
            ),
            "exit_code": 0,
            "health": "healthy",
            "image_index_digest": (
                "sha256:823ef51dc924a98d30a734a83dcd2cde78ab80948fb1d423705640c5a12e51ec"
            ),
            "revision": PARTIAL_RUNTIME_COMMIT,
            "service": "api",
            "state": "running",
        },
        {
            "container_id": (
                "0229866f1aa03054427b733ac17f2cceb873411c7810b025b6a094a4e9ed7358"
            ),
            "exit_code": 0,
            "health": "none",
            "image_index_digest": (
                "sha256:c798010776a5070efbb9f54163f191e71cecc26a08f1ec3d77b99995a419ea19"
            ),
            "revision": LEGACY_BEAT_COMMIT,
            "service": "beat",
            "state": "exited",
        },
        {
            "container_id": (
                "81d5b2d1470cd3c78c35f5dc9a65e3a81dcbf2dd5ea5cb95790888c3e3524003"
            ),
            "exit_code": 0,
            "health": "none",
            "image_index_digest": (
                "sha256:823ef51dc924a98d30a734a83dcd2cde78ab80948fb1d423705640c5a12e51ec"
            ),
            "revision": PARTIAL_RUNTIME_COMMIT,
            "service": "migrate",
            "state": "exited",
        },
        {
            "container_id": (
                "15da490f3da5aae9eb96a8cc9a19b17391150a7bf79d634349c569916e52be12"
            ),
            "exit_code": 0,
            "health": "unhealthy",
            "image_index_digest": (
                "sha256:afa1a175597ea22ea3f6482eff074386f2da9fe26d51cd4cde113bb6c645487d"
            ),
            "revision": CHECKPOINT_COMMIT,
            "service": "worker",
            "state": "exited",
        },
        {
            "container_id": (
                "a7a57e1606d4bd909a6bf1eed4c9ef939b1f9c303aade2ce432ec9c1433ecdcf"
            ),
            "exit_code": 0,
            "health": "unhealthy",
            "image_index_digest": (
                "sha256:afa1a175597ea22ea3f6482eff074386f2da9fe26d51cd4cde113bb6c645487d"
            ),
            "revision": CHECKPOINT_COMMIT,
            "service": "worker-collectors",
            "state": "exited",
        },
        {
            "container_id": (
                "f96ded0f69fc9d35f4a05813090aa592f56f41f627f4c4ed97ed0a5216e37373"
            ),
            "exit_code": 0,
            "health": "unhealthy",
            "image_index_digest": (
                "sha256:afa1a175597ea22ea3f6482eff074386f2da9fe26d51cd4cde113bb6c645487d"
            ),
            "revision": CHECKPOINT_COMMIT,
            "service": "worker-warehouse",
            "state": "exited",
        },
    ]


def _expected_bundles() -> list[dict[str, str]]:
    rows = (
        (
            "palimpsest-analysis",
            PARTIAL_RUNTIME_COMMIT,
            "aad5da0caa5a93bd4ea306e8926e8c2a6a6eb93778ad2a2a0a9796e36865fb37",
        ),
        (
            "palimpsest-network-lane",
            PARTIAL_RUNTIME_COMMIT,
            "fe8c81cd6874d385fd928d9cc68f2baa8152288672a30b2fa12fcc6dbc42ffa4",
        ),
        (
            "palimpsest-common-crawl",
            PARTIAL_RUNTIME_COMMIT,
            "c32d850e2c64e4ee292fe79f121f7ff1e6463d552fafee58aec8436f09f493f8",
        ),
        (
            "palimpsest-public-osint-sync",
            CHECKPOINT_COMMIT,
            "d36f1b88f1d3a98da7dba62d49951f98c989681de8af0cf2660f276ea89a11a8",
        ),
        (
            "palimpsest-node-offsite",
            PARTIAL_RUNTIME_COMMIT,
            "7e494b29c346a2d281a12968f65d8f9d2e71d6c3cf3d07409a9334055203f636",
        ),
    )
    return [
        {
            "current_symlink_path": f"/usr/local/libexec/{name}/current",
            "manifest_sha256": digest,
            "resolved_target_path": f"/usr/local/libexec/{name}/{revision}",
            "revision": revision,
        }
        for name, revision, digest in rows
    ]


def _expected_snapshot() -> dict[str, Any]:
    return {
        "latest_snapshot_id": "20260826T110720Z",
        "new_snapshot_created": True,
        "verification": {
            "counts": {
                "artifact_directories": 2513,
                "artifact_files": 12463,
                "artifact_members": 14976,
                "checksum_entries": 5,
                "snapshot_files": 6,
                "witness_history_records": 2253,
            },
            "digests": {
                "MANIFEST.txt": (
                    "1a0615635a87e4cd63acbc24ff1dad19495a29c633bf0dfde367160d76e1b952"
                ),
                "artifacts.list": (
                    "89ee6f2117f714ff4805b4aba97230f0bfaaa818916865ab295beb287722ce1f"
                ),
                "artifacts.tar.gz": (
                    "3b09661afc9465aa9b8b03830152b506414a3b89bac3430e9db38a6d7f99dad8"
                ),
                "postgres.dump": (
                    "d4f9ff1da534ad6a9f118fc92d02c933551882a7087829e45f4282a2bace1361"
                ),
                "postgres.list": (
                    "9225cbae2b75c325a8d7903fd5089299be87090f9b8c64764ebd8f8c141c673b"
                ),
            },
            "schema": "palimpsest-node-backup-verification.v1",
            "snapshot": "20260826T110720Z",
            "status": "verified",
        },
        "verification_receipt": {
            "path": (
                "/tmp/palimpsest-v4-snapshot-bridge.n17fXy/"
                "v4-backup-verification.json"
            ),
            "sha256": (
                "c9446ca105d20e2f8310c8bcc43c87249954fa12ed047c43164f7776cf6e4be3"
            ),
        },
    }


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
        or document["incident_date"] != "2026-08-26"
    ):
        raise ManifestError("unexpected incident identity")

    authority = document["authority"]
    if authority != {
        "failed_target_commit": CHECKPOINT_COMMIT,
        "prior_checkout_commit": CHECKPOINT_COMMIT,
        "prior_deployed_commit": CHECKPOINT_COMMIT,
    }:
        raise ManifestError("hybrid recovery authority is invalid")
    if any(_COMMIT.fullmatch(str(value)) is None for value in authority.values()):
        raise ManifestError("hybrid recovery authority contains a malformed commit")

    if document["recovery_target_constraints"] != {
        "must_be_descendant_of": CHECKPOINT_COMMIT,
        "must_be_reviewed": True,
        "must_contain_manifest_path": MANIFEST_REPOSITORY_PATH,
        "must_not_normalize_observed_hybrid": True,
    }:
        raise ManifestError("recovery target constraints are not fail-closed")

    continuation = document["continuation"]
    _require_keys(
        continuation,
        {
            "canonical_prepared_receipt",
            "predecessor_completion_receipt",
            "predecessor_incident_id",
            "predecessor_manifest",
            "predecessor_prepared_attempts",
            "predecessor_prepared_receipt",
            "predecessor_restore_profile_sha256",
        },
        "continuation",
    )
    attempts = _expected_attempt_bindings()
    if continuation["predecessor_incident_id"] != PREDECESSOR_INCIDENT_ID:
        raise ManifestError("predecessor incident is invalid")
    if continuation["predecessor_manifest"] != {
        "path": PREDECESSOR_MANIFEST_PATH,
        "sha256": PREDECESSOR_MANIFEST_SHA256,
    }:
        raise ManifestError("predecessor manifest binding is invalid")
    if continuation["predecessor_prepared_attempts"] != attempts:
        raise ManifestError("archived prepared-attempt bindings are invalid")
    if continuation["predecessor_prepared_receipt"] != attempts[-1]:
        raise ManifestError("latest prepared-attempt projection is invalid")
    if continuation["canonical_prepared_receipt"] != {
        "expected_absent": True,
        "path": CANONICAL_PREPARED_PATH,
    }:
        raise ManifestError("canonical prepared-receipt absence binding is invalid")
    if continuation["predecessor_completion_receipt"] != {
        "expected_absent": True,
        "path": PREDECESSOR_COMPLETION_PATH,
    }:
        raise ManifestError("predecessor completion absence binding is invalid")
    if continuation["predecessor_restore_profile_sha256"] != (
        RESTORE_PROFILE_SHA256
    ):
        raise ManifestError("predecessor restoration profile binding is invalid")

    failed = document["failed_attempt"]
    _require_keys(
        failed,
        {
            "advanced_components",
            "backup_bridge_receipts",
            "candidate_freshness_failure",
            "candidate_image",
            "failed_operation",
            "failed_target_snapshot",
            "failure_evidence",
            "last_emitted_stage",
            "migration_applied",
            "migration_exit_code",
            "migration_result",
            "phase1_handoff_created",
            "phase2_started",
            "phase3_binding_created",
            "prechange_snapshot_id",
            "recovery_backup_reason",
            "release_receipts_created",
            "release_transaction_id",
            "snapshot_ceiling",
            "systemd_invocation_id",
            "timeline",
        },
        "failed attempt",
    )
    if (
        failed["migration_applied"] is not False
        or failed["migration_exit_code"] != 0
        or failed["phase1_handoff_created"] is not False
        or failed["phase2_started"] is not False
        or failed["phase3_binding_created"] is not False
        or failed["release_receipts_created"] is not False
        or failed["release_transaction_id"] is not None
        or failed["last_emitted_stage"] != "fail-safe-armed"
        or failed["systemd_invocation_id"]
        != "6867901ad1c54b62b038fb14b2233391"
        or failed["failed_operation"]
        != "start_and_verify_oneshot palimpsest-public-osint-sync.service"
        or failed["recovery_backup_reason"]
        != "interrupted-phase1-hybrid-recovery-fresh-target-backup"
    ):
        raise ManifestError("failed-attempt phase facts are invalid")
    if failed["advanced_components"] != {
        "analysis_bundle": False,
        "api": False,
        "candidate_image": True,
        "checkout": True,
        "common_crawl_bundle": False,
        "deployed_marker": True,
        "migrate": False,
        "network_lane_bundle": False,
        "node_offsite_bundle": False,
        "public_osint_bundle": True,
        "workers": True,
    }:
        raise ManifestError("failed-target advance projection is invalid")
    if failed["candidate_freshness_failure"] != {
        "authoritative_bytes_equal": True,
        "candidate_age_seconds": 7409,
        "failure_code": "generation-stale",
        "maximum_age_seconds": 7200,
    }:
        raise ManifestError("generation-stale failure projection is invalid")
    if failed["candidate_image"] != {
        "config_digest": (
            "sha256:cced45c7964d656e92959b19f0dc11c2addf58fd95d2c3f3adcade92b8231b4b"
        ),
        "index_digest": (
            "sha256:afa1a175597ea22ea3f6482eff074386f2da9fe26d51cd4cde113bb6c645487d"
        ),
        "platform_manifest_digest": (
            "sha256:f3b9f942917b3143a606f83d51e17f6a0d4ffdd202434f623cec8451a232f1c5"
        ),
        "revision": CHECKPOINT_COMMIT,
    }:
        raise ManifestError("retained candidate image authority is invalid")
    migration = failed["migration_result"]
    if migration != {
        "applies_to_failed_target": False,
        "container_id": (
            "81d5b2d1470cd3c78c35f5dc9a65e3a81dcbf2dd5ea5cb95790888c3e3524003"
        ),
        "exit_code": 0,
        "finished_at": "2026-08-26T06:37:04.164130974Z",
        "image_id": (
            "sha256:823ef51dc924a98d30a734a83dcd2cde78ab80948fb1d423705640c5a12e51ec"
        ),
        "revision": PARTIAL_RUNTIME_COMMIT,
        "started_at": "2026-08-26T06:37:00.255604339Z",
        "state": "exited",
    }:
        raise ManifestError("surviving prior migration result is invalid")
    if failed["prechange_snapshot_id"] != "20260826T071623Z":
        raise ManifestError("prechange snapshot binding is invalid")
    if failed["failed_target_snapshot"] != {
        "sha256sums_sha256": (
            "1c7411b342bd97c41151e480ab279b59d85cd9194a091c869e10c3a8fc22c401"
        ),
        "snapshot_id": "20260826T072010Z",
        "verification_receipt_sha256": (
            "e6497272630c9cd53990d97ee9798afe7036c3114782c749036e1f1770caf00e"
        ),
    }:
        raise ManifestError("failed-target snapshot binding is invalid")
    if failed["failure_evidence"] != {
        "journal_sha256": (
            "cbf9c877912d5f973520592bd74791c1c10a158c4b07f86b1a9d7518cca26d0f"
        ),
        "last_failure_sha256": (
            "c58a98528c63215f87b89a8650c64de551f43120e12ee3984e37794aee3c75f1"
        ),
    }:
        raise ManifestError("failed-target evidence binding is invalid")
    if failed["timeline"] != {
        "checkout_advanced_at": "2026-08-26T07:17:50Z",
        "dispatched_at": "2026-08-26T07:14:45.661Z",
        "fail_safe_completed_at": "2026-08-26T07:21:36.362Z",
        "generation_stale_at": "2026-08-26T07:21:29.037045Z",
        "public_osint_started_at": "2026-08-26T07:21:26.902284Z",
    }:
        raise ManifestError("failed-target timeline binding is invalid")

    expected_receipts = [
        {
            "kind": "pre_worker_start_empty",
            "path": (
                "/tmp/palimpsest-v4-snapshot-bridge.n17fXy/"
                "broker-empty-before-start.json"
            ),
            "schema_version": "palimpsest-celery-broker-release-gate.v1",
            "sha256": (
                "c4048e40d3d29789d503af4fed030a9faa90584bb5052dc2278c577ee5c96f43"
            ),
            "status": "empty",
        },
        {
            "kind": "temporary_workers_fenced",
            "path": (
                "/tmp/palimpsest-v4-snapshot-bridge.n17fXy/"
                "celery-v4-backup-fenced.json"
            ),
            "schema_version": "palimpsest-celery-release-gate.v1",
            "sha256": (
                "7c40f40327d690f5da795b949454f426ced403f5c80d85436d9f979a638469d6"
            ),
            "status": "fenced",
        },
        {
            "kind": "post_worker_stop_empty",
            "path": (
                "/tmp/palimpsest-v4-snapshot-bridge.n17fXy/"
                "broker-empty-after-stop.json"
            ),
            "schema_version": "palimpsest-celery-broker-release-gate.v1",
            "sha256": (
                "31212cf76d842fcf7fc5f10299d7819aac44b66487b4b5ba99034ea413f2f4e8"
            ),
            "status": "empty",
        },
    ]
    if failed["backup_bridge_receipts"] != expected_receipts:
        raise ManifestError("backup bridge receipt bindings are invalid")
    if failed["snapshot_ceiling"] != _expected_snapshot():
        raise ManifestError("verified checkpoint snapshot projection is invalid")
    if _canonical_digest(failed) != FAILED_ATTEMPT_SHA256:
        raise ManifestError("full failed-attempt digest is invalid")

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
        "observed safe boundary",
    )
    if boundary["repository"] != {
        "checkout_commit": CHECKPOINT_COMMIT,
        "deployed_commit": CHECKPOINT_COMMIT,
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

    applications = boundary["application_containers"]
    expected_applications = _expected_applications()
    if type(applications) is not list or [
        item.get("service") for item in applications if type(item) is dict
    ] != [item["service"] for item in expected_applications]:
        raise ManifestError("application container inventory is invalid")
    if any(
        type(item) is not dict
        or _CONTAINER.fullmatch(str(item.get("container_id", ""))) is None
        or _IMAGE_DIGEST.fullmatch(str(item.get("image_index_digest", ""))) is None
        for item in applications
    ):
        raise ManifestError("application container identifiers are malformed")
    allowed_revisions = {
        CHECKPOINT_COMMIT,
        PARTIAL_RUNTIME_COMMIT,
        LEGACY_BEAT_COMMIT,
    }
    if any(item.get("revision") not in allowed_revisions for item in applications):
        raise ManifestError("application container revision is unsupported")
    by_service = {item["service"]: item for item in applications}
    if (
        by_service["api"].get("revision") != PARTIAL_RUNTIME_COMMIT
        or by_service["migrate"].get("revision") != PARTIAL_RUNTIME_COMMIT
        or any(
            by_service[service].get("revision") != CHECKPOINT_COMMIT
            for service in ("worker", "worker-collectors", "worker-warehouse")
        )
        or by_service["api"].get("state") != "running"
        or any(
            by_service[service].get("state") != "exited"
            for service in (
                "beat",
                "migrate",
                "worker",
                "worker-collectors",
                "worker-warehouse",
            )
        )
    ):
        raise ManifestError("required heterogeneous application boundary was normalized")
    if applications != expected_applications:
        raise ManifestError("application container identity drifted")

    bundles = boundary["installed_bundles"]
    if type(bundles) is not list or any(
        type(item) is not dict
        or item.get("revision") not in {CHECKPOINT_COMMIT, PARTIAL_RUNTIME_COMMIT}
        for item in bundles
    ):
        raise ManifestError("installed bundle revision is unsupported")
    if len({item["revision"] for item in bundles}) != 2:
        raise ManifestError("required heterogeneous bundle boundary was normalized")
    if bundles != _expected_bundles():
        raise ManifestError("installed bundle identity drifted")

    local_image = boundary["local_application_tag"]
    if local_image != {
        "config_digest": (
            "sha256:cced45c7964d656e92959b19f0dc11c2addf58fd95d2c3f3adcade92b8231b4b"
        ),
        "embedded_compose_labels": {
            "com.docker.compose.project": "palimpsest",
            "com.docker.compose.service": "beat",
            "com.docker.compose.version": "5.3.0",
        },
        "index_digest": (
            "sha256:afa1a175597ea22ea3f6482eff074386f2da9fe26d51cd4cde113bb6c645487d"
        ),
        "labels_are_runtime_authority": False,
        "name": "palimpsest/app:local",
        "platform_manifest_digest": (
            "sha256:f3b9f942917b3143a606f83d51e17f6a0d4ffdd202434f623cec8451a232f1c5"
        ),
        "revision": CHECKPOINT_COMMIT,
        "trusted_for_recovery": False,
    }:
        raise ManifestError("mutable local-image label hazard binding is invalid")

    infrastructure = boundary["infrastructure_containers"]
    if infrastructure != [
        {
            "container_id": (
                "b75c70b96bdb02cd6db7470520b08dd468f0e1c64a77ca32f26a49e922addee3"
            ),
            "health": "healthy",
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
            "health": "healthy",
            "image_id": (
                "sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"
            ),
            "service": "redis",
            "state": "running",
        },
    ]:
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
            for item in inventory
        )
    ):
        raise ManifestError("controlled activator checkpoint is invalid")
    if len(boundary["dynamic_release_instances"]) != 30:
        raise ManifestError("dynamic release instance inventory is incomplete")
    if len(boundary["release_services"]) != 12:
        raise ManifestError("release service inventory is incomplete")
    if len(boundary["installed_units"]) != 25:
        raise ManifestError("installed unit inventory is incomplete")
    controllers = boundary["installed_controller_boundary"]
    if (
        controllers.get("absent_paths") != []
        or len(controllers.get("present_files", [])) != 6
    ):
        raise ManifestError("installed controller inventory is incomplete")
    if len(boundary["witness_inventory"]) != 3:
        raise ManifestError("witness inventory is incomplete")
    if _canonical_digest(boundary) != SAFE_BOUNDARY_SHA256:
        raise ManifestError("full observed safe-boundary digest is invalid")

    pre_failure = document["pre_failure_state"]
    if _canonical_digest(pre_failure) != RESTORE_PROFILE_SHA256:
        raise ManifestError("original restoration profile was not preserved exactly")
    if (
        len(pre_failure.get("activators", [])) != 12
        or len(pre_failure.get("compose_writers", [])) != 5
    ):
        raise ManifestError("original restoration profile is incomplete")


def validate_manifest(path: Path) -> tuple[str, dict[str, Any]]:
    raw = _read_regular_nofollow(path, MAX_MANIFEST_BYTES, "hybrid manifest")
    document = _load_json(raw, "hybrid manifest")
    if raw != _canonical_pretty(document):
        raise ManifestError("hybrid manifest must use canonical sorted, indented JSON")
    _require_semantics(document)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_MANIFEST_SHA256:
        raise ManifestError("hybrid manifest SHA-256 does not match reviewed bytes")
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
            or metadata.st_uid != 0
            or metadata.st_gid != 0
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
    raw = _read_regular_nofollow(
        repository_root / relative, MAX_MANIFEST_BYTES, label
    )
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ManifestError(f"{label} bytes are invalid")
    document = _load_json(raw, label)
    if raw != _canonical_pretty(document):
        raise ManifestError(f"{label} is not canonical JSON")
    return document


def _attempt_receipt_authority(
    *, target: str, transaction: str
) -> dict[str, str]:
    return {
        "broker_queue_sha256": (
            "57cba36db8a74f1091b3478b831c833a6325023d57a8c4aa33190112e483f42b"
        ),
        "compose_environment_sha256": (
            "2ce97c2f94ce93336b592e1ddee78cfdbec1e8b19d35b39faab6ac069d332c95"
        ),
        "failed_target_commit": CHECKPOINT_COMMIT,
        "hybrid_fingerprint_sha256": (
            "ea2956a5d083da4f2e55cdee7330675b28fc2514d6028640d23fb5f129a8c954"
        ),
        "incident_id": PREDECESSOR_INCIDENT_ID,
        "manifest_sha256": PREDECESSOR_MANIFEST_SHA256,
        "minimum_recovery_ancestor": CHECKPOINT_COMMIT,
        "prior_checkout_commit": CHECKPOINT_COMMIT,
        "prior_deployed_commit": CHECKPOINT_COMMIT,
        "recovery_controller_commit": target,
        "restore_profile_sha256": RESTORE_PROFILE_SHA256,
        "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
        "status": "prepared",
        "target_commit": target,
        "transaction_id": transaction,
    }


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

    attempts = continuation["predecessor_prepared_attempts"]
    _verify_prepared_receipt(
        attempts[0],
        expected_path=EARLIER_ATTEMPT_PATH,
        expected_sha256=EARLIER_ATTEMPT_SHA256,
        expected_authority=_attempt_receipt_authority(
            target=EARLIER_ATTEMPT_TARGET,
            transaction=EARLIER_ATTEMPT_TRANSACTION,
        ),
        label="earlier archived prepared receipt",
    )
    latest_digest = _verify_prepared_receipt(
        continuation["predecessor_prepared_receipt"],
        expected_path=LATEST_ATTEMPT_PATH,
        expected_sha256=LATEST_ATTEMPT_SHA256,
        expected_authority=_attempt_receipt_authority(
            target=PARTIAL_RUNTIME_COMMIT,
            transaction=LATEST_ATTEMPT_TRANSACTION,
        ),
        label="latest archived prepared receipt",
    )
    _prove_absent(
        continuation["canonical_prepared_receipt"]["path"],
        CANONICAL_PREPARED_PATH,
        "canonical prepared receipt",
    )
    _prove_absent(
        continuation["predecessor_completion_receipt"]["path"],
        PREDECESSOR_COMPLETION_PATH,
        "predecessor completion receipt",
    )

    predecessor_continuation = predecessor.get("continuation")
    if type(predecessor_continuation) is not dict:
        raise ManifestError("predecessor retry continuation is invalid")
    api = _load_bound_manifest(
        repository_root,
        predecessor_continuation["predecessor_manifest"],
        expected_path=API_MANIFEST_PATH,
        expected_sha256=API_MANIFEST_SHA256,
        label="API readiness retry manifest",
    )
    if (
        api.get("incident_id") != API_INCIDENT_ID
        or api.get("schema_version") != SCHEMA_VERSION
        or api.get("pre_failure_state") != document["pre_failure_state"]
    ):
        raise ManifestError("API readiness continuation semantics are invalid")
    api_digest = _verify_prepared_receipt(
        predecessor_continuation["predecessor_prepared_receipt"],
        expected_path=API_PREPARED_PATH,
        expected_sha256=API_PREPARED_SHA256,
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
            "incident_id": API_INCIDENT_ID,
            "manifest_sha256": API_MANIFEST_SHA256,
            "minimum_recovery_ancestor": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
            "prior_checkout_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
            "prior_deployed_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
            "recovery_controller_commit": CHECKPOINT_COMMIT,
            "restore_profile_sha256": RESTORE_PROFILE_SHA256,
            "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
            "status": "prepared",
            "target_commit": CHECKPOINT_COMMIT,
            "transaction_id": API_TRANSACTION_ID,
        },
        label="API readiness prepared receipt",
    )
    _prove_absent(
        predecessor_continuation["predecessor_completion_receipt"]["path"],
        API_COMPLETION_PATH,
        "API readiness completion receipt",
    )

    api_continuation = api.get("continuation")
    if type(api_continuation) is not dict:
        raise ManifestError("original recovery continuation is invalid")
    original = _load_bound_manifest(
        repository_root,
        api_continuation["predecessor_manifest"],
        expected_path=ORIGINAL_MANIFEST_PATH,
        expected_sha256=ORIGINAL_MANIFEST_SHA256,
        label="original recovery manifest",
    )
    if (
        original.get("incident_id") != ORIGINAL_INCIDENT_ID
        or original.get("schema_version")
        != "palimpsest-interrupted-phase1-recovery.v1"
        or original.get("pre_failure_state") != document["pre_failure_state"]
    ):
        raise ManifestError("original recovery continuation semantics are invalid")
    _verify_prepared_receipt(
        api_continuation["predecessor_prepared_receipt"],
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
            "minimum_recovery_ancestor": "8b48162a13f719a4500c2297a337655d91dbb28e",
            "prior_checkout_commit": "7d05ecca47b20d8cf092a513a0db0390435f363f",
            "prior_deployed_commit": "95ea01d1a394fe219d64d3dce6b105296bce309a",
            "recovery_controller_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
            "restore_profile_sha256": RESTORE_PROFILE_SHA256,
            "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
            "status": "prepared",
            "target_commit": "1ae25399c7b36dca60e316cc966ea7d9636ec62b",
            "transaction_id": ORIGINAL_TRANSACTION_ID,
        },
        label="original prepared receipt",
    )
    _prove_absent(
        api_continuation["predecessor_completion_receipt"]["path"],
        ORIGINAL_COMPLETION_PATH,
        "original completion receipt",
    )
    return latest_digest, api_digest


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name(MANIFEST_NAME),
        help="hybrid manifest path (defaults to the reviewed sibling JSON)",
    )
    parser.add_argument(
        "--verify-host-continuation",
        action="store_true",
        help="verify archived attempts, predecessor receipts, and receipt absences",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path.cwd(),
        help="repository root used to verify the predecessor manifest chain",
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
        print(f"invalid interrupted Phase 1 hybrid manifest: {error}", file=sys.stderr)
        return 1
    if args.verify_host_continuation:
        assert prepared_digests is not None
        print(
            "validated interrupted Phase 1 hybrid host continuation: "
            f"manifest={digest} prepared={prepared_digests[0]} "
            f"predecessor_prepared={prepared_digests[1]}"
        )
    else:
        print(f"validated interrupted Phase 1 hybrid manifest: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
