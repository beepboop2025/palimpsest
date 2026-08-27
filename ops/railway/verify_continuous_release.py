#!/usr/bin/env python3
"""Verify and receipt an exact Palimpsest Railway publication.

The Railway CLI proves provider-side deployment state.  The public health and
release-manifest endpoints prove served state.  A successful receipt is only
written when those independent observations agree with a clean, current-main
source identity and the exact local release manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

if __package__ in {None, ""}:
    # Direct-file execution sets sys.path[0] to ops/railway rather than the
    # repository root.  The controller intentionally invokes this exact file,
    # so admit only its own resolved checkout before importing shared hardening.
    _REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from core.safe_fetch import FetchError, safe_fetch_response  # noqa: E402

RECEIPT_SCHEMA = "palimpsest.railway-continuous-release-receipt.v1"
RELEASE_SCHEMA = "palimpsest.railway-static-release.v1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_DEPLOYMENT_DOCUMENT_BYTES = 1024 * 1024
MAX_STATUS_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_RELEASE_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_HEALTH_BYTES = 64 * 1024
MAX_GIT_STATUS_BYTES = 1024 * 1024
MAX_DEPLOYMENTS = 100
MAX_RECEIPT_BYTES = 64 * 1024
MAX_ATTEMPTS = 20
MAX_TIMEOUT_SECONDS = 60.0
MAX_RETRY_DELAY_SECONDS = 60.0
MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_FUTURE_SKEW_SECONDS = 10 * 60
MAX_CRITICAL_FILES = 256
MAX_CRITICAL_FILE_BYTES = 32 * 1024 * 1024
MAX_CRITICAL_AGGREGATE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_ENTRIES = 150_000
MAX_BUNDLE_FILES = 100_000
MAX_BUNDLE_FILE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_AGGREGATE_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_PATH_BYTES = 4096
MAX_BUNDLE_DEPTH = 128
ALLOWED_LIVE_ORIGINS = frozenset(
    {
        "https://palimpsest-publication-production.up.railway.app",
        "https://www.palimpsest.info",
    }
)
ALLOWED_DEPLOYMENT_REASONS = frozenset({"deploy", "deploymentRollback"})
EXPECTED_SERVICE_NAME = "palimpsest-publication"


class VerificationError(ValueError):
    """The release evidence did not satisfy the fail-closed contract."""


@dataclass(frozen=True)
class CriticalFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ReleaseIdentity:
    source_commit: str
    tree_sha256: str
    manifest_sha256: str
    built_at: datetime
    critical_files: tuple[CriticalFile, ...]


@dataclass(frozen=True)
class BundleTreeEvidence:
    file_count: int
    total_bytes: int
    tree_sha256: str


@dataclass(frozen=True)
class DeploymentEvidence:
    deployment_id: str
    status: str
    created_at: datetime
    image_digest: str
    reason: str


@dataclass(frozen=True)
class TopologyEvidence:
    project_id: str
    environment_id: str
    service_id: str
    service_name: str
    deployment_id: str
    deployment_reason: str
    source_attached: bool
    cron_schedule: None
    volume_instance_count: int
    volume_mount_count: int


@dataclass(frozen=True)
class HttpPayload:
    status: int
    final_url: str
    body: bytes
    content_type: str
    cache_control: str


Fetcher = Callable[[str, float, int], HttpPayload]
Sleeper = Callable[[float], None]
Clock = Callable[[], datetime]


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _clock_value(clock: Clock, *, label: str) -> datetime:
    try:
        value = clock()
    except Exception as error:
        raise VerificationError(f"{label} is unavailable") from error
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise VerificationError(f"{label} does not include a timezone")
    return value.astimezone(UTC)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise VerificationError(f"non-finite JSON number: {value}")


def _decode_json(payload: bytes, *, label: str, maximum: int) -> Any:
    if not payload or len(payload) > maximum:
        raise VerificationError(f"{label} has an invalid byte size")
    try:
        return json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except VerificationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise VerificationError(f"{label} is not strict JSON") from error


def _read_regular_file(path: Path, *, label: str, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise VerificationError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise VerificationError(f"{label} is not a regular non-symlink file")
    if metadata.st_size > maximum:
        raise VerificationError(f"{label} exceeds its byte ceiling")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise VerificationError(f"{label} could not be read") from error
    if len(payload) != metadata.st_size:
        raise VerificationError(f"{label} changed while it was read")
    return payload


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise VerificationError(f"{field} is not a bounded timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VerificationError(f"{field} is not an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VerificationError(f"{field} does not include a timezone")
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fresh(
    observed: datetime,
    *,
    now: datetime,
    maximum_age_seconds: int,
    future_skew_seconds: int,
    label: str,
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise VerificationError("verification clock does not include a timezone")
    if (
        type(maximum_age_seconds) is not int
        or not 1 <= maximum_age_seconds <= MAX_AGE_SECONDS
    ):
        raise VerificationError("maximum evidence age is outside the safe range")
    if (
        type(future_skew_seconds) is not int
        or not 0 <= future_skew_seconds <= MAX_FUTURE_SKEW_SECONDS
    ):
        raise VerificationError("maximum future skew is outside the safe range")
    now = now.astimezone(UTC)
    if observed > now + timedelta(seconds=future_skew_seconds):
        raise VerificationError(f"{label} is implausibly in the future")
    if now - observed > timedelta(seconds=maximum_age_seconds):
        raise VerificationError(f"{label} is stale")


def _bounded_hex(value: Any, *, length: int, field: str) -> str:
    expression = COMMIT_RE if length == 40 else SHA256_RE
    if not isinstance(value, str) or expression.fullmatch(value) is None:
        raise VerificationError(f"{field} must be {length} lowercase hex characters")
    return value


def _canonical_uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise VerificationError(f"{field} is not a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise VerificationError(f"{field} is not a canonical UUID") from error
    if str(parsed) != value:
        raise VerificationError(f"{field} is not a lowercase canonical UUID")
    return value


def validate_preflight(
    *,
    expected_source_commit: str,
    checkout_source_commit: str,
    current_main_source_commit: str,
    git_status: bytes,
) -> str:
    """Bind the reviewed source to an exact, clean current-main checkout."""

    expected = _bounded_hex(
        expected_source_commit, length=40, field="expected source commit"
    )
    checkout = _bounded_hex(
        checkout_source_commit, length=40, field="checkout source commit"
    )
    current_main = _bounded_hex(
        current_main_source_commit, length=40, field="current-main source commit"
    )
    if expected != checkout:
        raise VerificationError("checkout source commit does not match the expectation")
    if expected != current_main:
        raise VerificationError("expected source commit is no longer current main")
    if len(git_status) > MAX_GIT_STATUS_BYTES:
        raise VerificationError("git status exceeds its byte ceiling")
    if git_status:
        raise VerificationError("release checkout is not clean")
    return expected


def _critical_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1024
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise VerificationError("release manifest critical path is unsafe")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise VerificationError("release manifest critical path is not normalized")
    return value


def _critical_inventory(value: Any) -> tuple[CriticalFile, ...]:
    if type(value) is not dict or not 1 <= len(value) <= MAX_CRITICAL_FILES:
        raise VerificationError("release manifest critical-file inventory is invalid")
    rows: list[CriticalFile] = []
    total_bytes = 0
    for raw_path, raw_evidence in value.items():
        path = _critical_path(raw_path)
        if type(raw_evidence) is not dict or set(raw_evidence) != {"bytes", "sha256"}:
            raise VerificationError(
                "release manifest critical-file evidence is invalid"
            )
        size = raw_evidence["bytes"]
        digest = raw_evidence["sha256"]
        if type(size) is not int or not 0 <= size <= MAX_CRITICAL_FILE_BYTES:
            raise VerificationError("release manifest critical-file size is invalid")
        _bounded_hex(digest, length=64, field="critical-file SHA-256")
        total_bytes += size
        if total_bytes > MAX_CRITICAL_AGGREGATE_BYTES:
            raise VerificationError(
                "release manifest critical files exceed aggregate cap"
            )
        rows.append(CriticalFile(path=path, size=size, sha256=digest))
    return tuple(sorted(rows, key=lambda row: row.path.encode("utf-8")))


def load_release_identity(
    manifest_path: Path,
    *,
    expected_source_commit: str,
    expected_tree_sha256: str,
    expected_manifest_sha256: str,
    now: datetime,
    maximum_age_seconds: int,
    future_skew_seconds: int,
) -> tuple[ReleaseIdentity, bytes]:
    """Validate the local artifact manifest and its independently supplied digest."""

    source_commit = _bounded_hex(
        expected_source_commit, length=40, field="expected source commit"
    )
    tree_sha256 = _bounded_hex(
        expected_tree_sha256, length=64, field="expected tree SHA-256"
    )
    manifest_sha256 = _bounded_hex(
        expected_manifest_sha256, length=64, field="expected manifest SHA-256"
    )
    payload = _read_regular_file(
        manifest_path,
        label="local Railway release manifest",
        maximum=MAX_RELEASE_MANIFEST_BYTES,
    )
    actual_digest = hashlib.sha256(payload).hexdigest()
    if actual_digest != manifest_sha256:
        raise VerificationError("local Railway release manifest digest does not match")
    document = _decode_json(
        payload,
        label="local Railway release manifest",
        maximum=MAX_RELEASE_MANIFEST_BYTES,
    )
    if type(document) is not dict:
        raise VerificationError("local Railway release manifest root is not an object")
    if document.get("schema_version") != RELEASE_SCHEMA:
        raise VerificationError("local Railway release manifest schema is unsupported")
    if document.get("source_commit") != source_commit:
        raise VerificationError("local Railway release source commit does not match")
    if document.get("tree_sha256") != tree_sha256:
        raise VerificationError("local Railway release tree digest does not match")
    if document.get("deployment_source") != "local-git-archive":
        raise VerificationError("local Railway release has an unsafe deployment source")
    if document.get("github_required") is not False:
        raise VerificationError("local Railway release unexpectedly requires GitHub")
    if document.get("state") != "artifact_ready":
        raise VerificationError("local Railway release is not artifact-ready")
    built_at = _timestamp(document.get("built_at"), field="release built_at")
    _fresh(
        built_at,
        now=now,
        maximum_age_seconds=maximum_age_seconds,
        future_skew_seconds=future_skew_seconds,
        label="Railway release manifest",
    )
    critical_files = _critical_inventory(document.get("critical_files"))
    return (
        ReleaseIdentity(
            source_commit=source_commit,
            tree_sha256=tree_sha256,
            manifest_sha256=manifest_sha256,
            built_at=built_at,
            critical_files=critical_files,
        ),
        payload,
    )


def _open_bundle_critical(root_descriptor: int, relative_path: str) -> int:
    """Open one normalized bundle path without following any path-component link."""

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    if not no_follow or not directory_only:
        raise VerificationError("platform cannot enforce sealed-bundle path safety")
    parent_descriptor = os.dup(root_descriptor)
    try:
        segments = relative_path.split("/")
        for segment in segments[:-1]:
            next_descriptor = os.open(
                segment,
                os.O_RDONLY | directory_only | no_follow | close_on_exec,
                dir_fd=parent_descriptor,
            )
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        return os.open(
            segments[-1],
            os.O_RDONLY | no_follow | close_on_exec,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise VerificationError(
            f"sealed bundle critical path is unavailable or unsafe: {relative_path}"
        ) from error
    finally:
        os.close(parent_descriptor)


def _recompute_bundle_tree(root_descriptor: int) -> BundleTreeEvidence:
    """Rebuild the release builder's full-tree identity from pinned descriptors."""

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    if not no_follow or not directory_only:
        raise VerificationError("platform cannot enforce sealed-bundle path safety")
    rows: list[tuple[str, int, str]] = []
    total_bytes = 0
    entry_count = 0

    def walk(directory_descriptor: int, prefix: str, depth: int) -> None:
        nonlocal entry_count, total_bytes
        if depth > MAX_BUNDLE_DEPTH:
            raise VerificationError("sealed bundle directory depth exceeds its cap")
        directory_before = os.fstat(directory_descriptor)
        names: list[str] = []
        with os.scandir(directory_descriptor) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_BUNDLE_ENTRIES:
                    raise VerificationError("sealed bundle entry count exceeds its cap")
                names.append(entry.name)

        for name in names:
            relative = f"{prefix}/{name}" if prefix else name
            try:
                relative_bytes = relative.encode("utf-8")
            except UnicodeError as error:
                raise VerificationError(
                    "sealed bundle contains a path that is not UTF-8"
                ) from error
            if not relative_bytes or len(relative_bytes) > MAX_BUNDLE_PATH_BYTES:
                raise VerificationError("sealed bundle path exceeds its byte cap")
            metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise VerificationError(
                    f"sealed bundle tree contains a symbolic link: {relative}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY | directory_only | no_follow | close_on_exec,
                    dir_fd=directory_descriptor,
                )
                try:
                    opened_child = os.fstat(child_descriptor)
                    if (opened_child.st_dev, opened_child.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise VerificationError(
                            f"sealed bundle directory changed while opened: {relative}"
                        )
                    walk(child_descriptor, relative, depth + 1)
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise VerificationError(
                    f"sealed bundle tree contains a special file: {relative}"
                )
            if relative == "railway-release.json":
                continue
            if metadata.st_size > MAX_BUNDLE_FILE_BYTES:
                raise VerificationError(
                    f"sealed bundle file exceeds its byte cap: {relative}"
                )
            descriptor = os.open(
                name,
                os.O_RDONLY | no_follow | close_on_exec,
                dir_fd=directory_descriptor,
            )
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or (
                    before.st_dev,
                    before.st_ino,
                ) != (metadata.st_dev, metadata.st_ino):
                    raise VerificationError(
                        f"sealed bundle file changed while opened: {relative}"
                    )
                if before.st_size > MAX_BUNDLE_FILE_BYTES:
                    raise VerificationError(
                        f"sealed bundle file exceeds its byte cap: {relative}"
                    )
                digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(descriptor)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise VerificationError(
                        f"sealed bundle file changed while read: {relative}"
                    )
            finally:
                os.close(descriptor)
            rows.append((relative, before.st_size, digest.hexdigest()))
            if len(rows) > MAX_BUNDLE_FILES:
                raise VerificationError("sealed bundle file count exceeds its cap")
            total_bytes += before.st_size
            if total_bytes > MAX_BUNDLE_AGGREGATE_BYTES:
                raise VerificationError(
                    "sealed bundle total bytes exceed aggregate cap"
                )

        directory_after = os.fstat(directory_descriptor)
        if (
            directory_before.st_dev,
            directory_before.st_ino,
            directory_before.st_mtime_ns,
        ) != (
            directory_after.st_dev,
            directory_after.st_ino,
            directory_after.st_mtime_ns,
        ):
            raise VerificationError("sealed bundle directory changed while enumerated")

    try:
        walk(root_descriptor, "", 0)
    except VerificationError:
        raise
    except OSError as error:
        raise VerificationError(
            "sealed bundle tree could not be enumerated safely"
        ) from error
    if not rows:
        raise VerificationError("sealed bundle tree is empty")
    tree = hashlib.sha256()
    for relative, size, digest in sorted(
        rows, key=lambda row: tuple(row[0].split("/"))
    ):
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(size).encode("ascii"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")
    return BundleTreeEvidence(
        file_count=len(rows),
        total_bytes=total_bytes,
        tree_sha256=tree.hexdigest(),
    )


def validate_sealed_bundle(
    bundle_root: Path,
    *,
    expected_source_commit: str,
    expected_tree_sha256: str,
    expected_manifest_sha256: str,
    now: datetime,
    maximum_age_seconds: int,
    future_skew_seconds: int,
) -> ReleaseIdentity:
    """Validate a fresh manifest and every bound critical file before mutation."""

    try:
        root_metadata = bundle_root.lstat()
    except OSError as error:
        raise VerificationError("sealed bundle root is unavailable") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise VerificationError("sealed bundle root is not a non-symlink directory")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    if not no_follow or not directory_only:
        raise VerificationError("platform cannot enforce sealed-bundle path safety")
    try:
        root_descriptor = os.open(
            bundle_root,
            os.O_RDONLY | directory_only | no_follow | close_on_exec,
        )
    except OSError as error:
        raise VerificationError(
            "sealed bundle root could not be opened safely"
        ) from error
    try:
        opened_root = os.fstat(root_descriptor)
        if (opened_root.st_dev, opened_root.st_ino) != (
            root_metadata.st_dev,
            root_metadata.st_ino,
        ):
            raise VerificationError("sealed bundle root changed while it was opened")
        identity, manifest_payload = load_release_identity(
            bundle_root / "railway-release.json",
            expected_source_commit=expected_source_commit,
            expected_tree_sha256=expected_tree_sha256,
            expected_manifest_sha256=expected_manifest_sha256,
            now=now,
            maximum_age_seconds=maximum_age_seconds,
            future_skew_seconds=future_skew_seconds,
        )
        manifest_document = _decode_json(
            manifest_payload,
            label="local Railway release manifest",
            maximum=MAX_RELEASE_MANIFEST_BYTES,
        )
        if type(manifest_document) is not dict:
            raise VerificationError("sealed bundle manifest root is not an object")
        manifest_file_count = manifest_document.get("file_count")
        manifest_total_bytes = manifest_document.get("total_bytes")
        if (
            type(manifest_file_count) is not int
            or not 1 <= manifest_file_count <= MAX_BUNDLE_FILES
        ):
            raise VerificationError("sealed bundle manifest file count is invalid")
        if (
            type(manifest_total_bytes) is not int
            or not 0 <= manifest_total_bytes <= MAX_BUNDLE_AGGREGATE_BYTES
        ):
            raise VerificationError("sealed bundle manifest total bytes is invalid")
        manifest_descriptor = _open_bundle_critical(
            root_descriptor, "railway-release.json"
        )
        try:
            manifest_metadata = os.fstat(manifest_descriptor)
            if (
                not stat.S_ISREG(manifest_metadata.st_mode)
                or manifest_metadata.st_size > MAX_RELEASE_MANIFEST_BYTES
            ):
                raise VerificationError(
                    "sealed bundle manifest is not a bounded regular file"
                )
            bound_manifest = bytearray()
            while chunk := os.read(manifest_descriptor, 1024 * 1024):
                bound_manifest.extend(chunk)
            manifest_after = os.fstat(manifest_descriptor)
            if (
                manifest_metadata.st_dev,
                manifest_metadata.st_ino,
                manifest_metadata.st_size,
                manifest_metadata.st_mtime_ns,
            ) != (
                manifest_after.st_dev,
                manifest_after.st_ino,
                manifest_after.st_size,
                manifest_after.st_mtime_ns,
            ):
                raise VerificationError("sealed bundle manifest changed while read")
            if bytes(bound_manifest) != manifest_payload:
                raise VerificationError(
                    "sealed bundle manifest changed while it was bound"
                )
        finally:
            os.close(manifest_descriptor)
        verified_total_bytes = 0
        for critical in identity.critical_files:
            descriptor = _open_bundle_critical(root_descriptor, critical.path)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise VerificationError(
                        f"sealed bundle critical path is not a regular file: {critical.path}"
                    )
                if before.st_size != critical.size:
                    raise VerificationError(
                        f"sealed bundle critical file size does not match: {critical.path}"
                    )
                verified_total_bytes += before.st_size
                if verified_total_bytes > MAX_CRITICAL_AGGREGATE_BYTES:
                    raise VerificationError(
                        "sealed bundle critical files exceed aggregate cap"
                    )
                digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(descriptor)
                before_identity = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                )
                after_identity = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                )
                if before_identity != after_identity:
                    raise VerificationError(
                        f"sealed bundle critical file changed while read: {critical.path}"
                    )
                if digest.hexdigest() != critical.sha256:
                    raise VerificationError(
                        f"sealed bundle critical file SHA-256 does not match: {critical.path}"
                    )
            finally:
                os.close(descriptor)
        tree = _recompute_bundle_tree(root_descriptor)
        if tree.file_count != manifest_file_count:
            raise VerificationError("sealed bundle file count does not match manifest")
        if tree.total_bytes != manifest_total_bytes:
            raise VerificationError("sealed bundle total bytes do not match manifest")
        if tree.tree_sha256 != identity.tree_sha256:
            raise VerificationError(
                "sealed bundle tree SHA-256 does not match manifest"
            )
        try:
            final_root = bundle_root.lstat()
        except OSError as error:
            raise VerificationError(
                "sealed bundle root changed during validation"
            ) from error
        if (
            stat.S_ISLNK(final_root.st_mode)
            or not stat.S_ISDIR(final_root.st_mode)
            or (final_root.st_dev, final_root.st_ino)
            != (opened_root.st_dev, opened_root.st_ino)
        ):
            raise VerificationError("sealed bundle root changed during validation")
        return identity
    finally:
        os.close(root_descriptor)


def _deployment_candidates(document: Any) -> list[Any]:
    if isinstance(document, list):
        candidates = document
    elif type(document) is dict and set(document) == {"deployments"}:
        candidates = document["deployments"]
    elif type(document) is dict:
        candidates = [document]
    else:
        raise VerificationError("Railway deployment JSON root has an invalid shape")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_DEPLOYMENTS:
        raise VerificationError("Railway deployment inventory has an invalid size")
    return candidates


def parse_deployment_evidence(
    payload: bytes,
    *,
    expected_deployment_id: str,
    expected_image_digest: str | None,
    now: datetime,
    maximum_age_seconds: int,
    future_skew_seconds: int,
) -> DeploymentEvidence:
    """Extract only allowlisted evidence from Railway CLI deployment JSON."""

    deployment_id = _canonical_uuid(
        expected_deployment_id, field="expected deployment ID"
    )
    if (
        expected_image_digest is not None
        and IMAGE_DIGEST_RE.fullmatch(expected_image_digest) is None
    ):
        raise VerificationError("expected image digest is invalid")
    document = _decode_json(
        payload,
        label="Railway deployment JSON",
        maximum=MAX_DEPLOYMENT_DOCUMENT_BYTES,
    )
    matches: list[dict[str, Any]] = []
    for candidate in _deployment_candidates(document):
        if type(candidate) is not dict:
            raise VerificationError(
                "Railway deployment inventory contains a non-object"
            )
        if candidate.get("id") == deployment_id:
            matches.append(candidate)
    if len(matches) != 1:
        raise VerificationError(
            "Railway deployment inventory does not contain one exact deployment ID"
        )
    deployment = matches[0]
    if deployment.get("status") != "SUCCESS":
        raise VerificationError("Railway deployment did not finish with SUCCESS")
    created_at = _timestamp(
        deployment.get("createdAt"), field="Railway deployment createdAt"
    )
    _fresh(
        created_at,
        now=now,
        maximum_age_seconds=maximum_age_seconds,
        future_skew_seconds=future_skew_seconds,
        label="Railway deployment",
    )
    metadata = deployment.get("meta")
    if type(metadata) is not dict:
        raise VerificationError("Railway deployment metadata is missing")
    if metadata.get("buildOnly") is not False:
        raise VerificationError("Railway deployment is build-only or ambiguous")
    reason = metadata.get("reason")
    if reason not in ALLOWED_DEPLOYMENT_REASONS:
        raise VerificationError(
            "Railway deployment reason is not an allowed release state"
        )
    image_digest = metadata.get("imageDigest")
    if (
        not isinstance(image_digest, str)
        or IMAGE_DIGEST_RE.fullmatch(image_digest) is None
    ):
        raise VerificationError("Railway deployment image digest is invalid")
    if expected_image_digest is not None and image_digest != expected_image_digest:
        raise VerificationError("Railway deployment image digest does not match")
    return DeploymentEvidence(
        deployment_id=deployment_id,
        status="SUCCESS",
        created_at=created_at,
        image_digest=image_digest,
        reason=reason,
    )


def _edges(value: Any, *, field: str, maximum: int = 100) -> list[dict[str, Any]]:
    if type(value) is not dict or set(value) != {"edges"}:
        raise VerificationError(f"Railway status {field} edge inventory is invalid")
    edges = value.get("edges")
    if not isinstance(edges, list) or len(edges) > maximum:
        raise VerificationError(f"Railway status {field} edge inventory is invalid")
    if any(
        type(edge) is not dict or type(edge.get("node")) is not dict for edge in edges
    ):
        raise VerificationError(f"Railway status {field} contains an invalid edge")
    return edges


def _one_node(
    edges: list[dict[str, Any]], *, key: str, expected: str, field: str
) -> dict[str, Any]:
    matches = [edge["node"] for edge in edges if edge["node"].get(key) == expected]
    if len(matches) != 1:
        raise VerificationError(f"Railway status does not contain one exact {field}")
    return matches[0]


def extract_latest_status_deployment(
    payload: bytes,
    *,
    expected_environment_id: str,
    expected_service_id: str,
    now: datetime,
    maximum_age_seconds: int,
    future_skew_seconds: int,
) -> DeploymentEvidence:
    """Extract one fresh successful latest deployment from pinned status topology."""

    environment_id = _canonical_uuid(
        expected_environment_id, field="expected environment ID"
    )
    service_id = _canonical_uuid(expected_service_id, field="expected service ID")
    document = _decode_json(
        payload,
        label="Railway status JSON",
        maximum=MAX_STATUS_DOCUMENT_BYTES,
    )
    if type(document) is not dict:
        raise VerificationError("Railway status JSON root is not an object")
    environments = _edges(document.get("environments"), field="environments")
    environment = _one_node(
        environments,
        key="id",
        expected=environment_id,
        field="environment ID",
    )
    if (
        environment.get("deletedAt") is not None
        or environment.get("canAccess") is not True
    ):
        raise VerificationError("Railway status environment is deleted or inaccessible")
    service_instances = _edges(
        environment.get("serviceInstances"), field="service instances"
    )
    instance = _one_node(
        service_instances,
        key="serviceId",
        expected=service_id,
        field="environment service ID",
    )
    if instance.get("environmentId") != environment_id:
        raise VerificationError("Railway status service environment ID does not match")
    latest = instance.get("latestDeployment")
    if type(latest) is not dict:
        raise VerificationError("Railway status latest deployment is missing")
    deployment_id = _canonical_uuid(
        latest.get("id"), field="Railway status latest deployment ID"
    )
    if latest.get("status") != "SUCCESS":
        raise VerificationError("Railway status latest deployment is not successful")
    if latest.get("deploymentStopped") is not False:
        raise VerificationError(
            "Railway status latest deployment is stopped or ambiguous"
        )
    instances = latest.get("instances")
    if (
        not isinstance(instances, list)
        or len(instances) != 1
        or any(
            type(row) is not dict or row.get("status") != "RUNNING" for row in instances
        )
    ):
        raise VerificationError(
            "Railway status latest deployment does not have exactly one running instance"
        )
    created_at = _timestamp(
        latest.get("createdAt"), field="Railway status latest deployment createdAt"
    )
    _fresh(
        created_at,
        now=now,
        maximum_age_seconds=maximum_age_seconds,
        future_skew_seconds=future_skew_seconds,
        label="Railway status latest deployment",
    )
    metadata = latest.get("meta")
    if type(metadata) is not dict:
        raise VerificationError("Railway status latest deployment metadata is missing")
    if metadata.get("buildOnly") is not False:
        raise VerificationError("Railway status latest deployment is build-only")
    reason = metadata.get("reason")
    if reason not in ALLOWED_DEPLOYMENT_REASONS:
        raise VerificationError(
            "Railway status latest deployment reason is not an allowed release state"
        )
    image_digest = metadata.get("imageDigest")
    if (
        not isinstance(image_digest, str)
        or IMAGE_DIGEST_RE.fullmatch(image_digest) is None
    ):
        raise VerificationError("Railway status latest image digest is invalid")
    return DeploymentEvidence(
        deployment_id=deployment_id,
        status="SUCCESS",
        created_at=created_at,
        image_digest=image_digest,
        reason=reason,
    )


def parse_status_topology(
    payload: bytes,
    *,
    expected_project_id: str,
    expected_environment_id: str,
    expected_service_id: str,
    expected_deployment_id: str,
    expected_image_digest: str,
    expected_deployment_reason: str,
) -> TopologyEvidence:
    """Validate the Railway project topology and effective service manifest."""

    project_id = _canonical_uuid(expected_project_id, field="expected project ID")
    environment_id = _canonical_uuid(
        expected_environment_id, field="expected environment ID"
    )
    service_id = _canonical_uuid(expected_service_id, field="expected service ID")
    deployment_id = _canonical_uuid(
        expected_deployment_id, field="expected deployment ID"
    )
    if IMAGE_DIGEST_RE.fullmatch(expected_image_digest) is None:
        raise VerificationError("expected image digest is invalid")
    if expected_deployment_reason not in ALLOWED_DEPLOYMENT_REASONS:
        raise VerificationError("expected deployment reason is invalid")
    document = _decode_json(
        payload,
        label="Railway status JSON",
        maximum=MAX_STATUS_DOCUMENT_BYTES,
    )
    if type(document) is not dict:
        raise VerificationError("Railway status JSON root is not an object")
    if document.get("id") != project_id:
        raise VerificationError("Railway status project ID does not match")

    project_services = _edges(document.get("services"), field="project services")
    project_service = _one_node(
        project_services,
        key="id",
        expected=service_id,
        field="project service ID",
    )
    service_name = project_service.get("name")
    if service_name != EXPECTED_SERVICE_NAME:
        raise VerificationError("Railway status service name does not match required")

    environments = _edges(document.get("environments"), field="environments")
    environment = _one_node(
        environments,
        key="id",
        expected=environment_id,
        field="environment ID",
    )
    if (
        environment.get("deletedAt") is not None
        or environment.get("canAccess") is not True
    ):
        raise VerificationError("Railway status environment is deleted or inaccessible")
    volume_instances = _edges(
        environment.get("volumeInstances"), field="volume instances"
    )
    if volume_instances:
        raise VerificationError("Railway status environment unexpectedly has volumes")

    service_instances = _edges(
        environment.get("serviceInstances"), field="service instances"
    )
    instance = _one_node(
        service_instances,
        key="serviceId",
        expected=service_id,
        field="environment service ID",
    )
    if instance.get("environmentId") != environment_id:
        raise VerificationError("Railway status service environment ID does not match")
    if instance.get("serviceName") != service_name:
        raise VerificationError("Railway status service name does not match")

    if "source" not in instance:
        raise VerificationError("Railway status service source field is missing")
    source = instance["source"]
    if source is not None and source != {"image": None, "repo": None}:
        raise VerificationError("Railway status service has an attached source")
    if "cronSchedule" not in instance or instance["cronSchedule"] is not None:
        raise VerificationError(
            "Railway status service unexpectedly has a cron schedule"
        )
    if "nextCronRunAt" not in instance or instance["nextCronRunAt"] is not None:
        raise VerificationError("Railway status service has an ambiguous next cron run")

    latest = instance.get("latestDeployment")
    if type(latest) is not dict:
        raise VerificationError("Railway status latest deployment is missing")
    if latest.get("id") != deployment_id or latest.get("status") != "SUCCESS":
        raise VerificationError(
            "Railway status latest deployment identity does not match"
        )
    if latest.get("deploymentStopped") is not False:
        raise VerificationError(
            "Railway status latest deployment is stopped or ambiguous"
        )
    instances = latest.get("instances")
    if (
        not isinstance(instances, list)
        or len(instances) != 1
        or any(
            type(row) is not dict or row.get("status") != "RUNNING" for row in instances
        )
    ):
        raise VerificationError(
            "Railway status latest deployment does not have exactly one running instance"
        )

    metadata = latest.get("meta")
    if type(metadata) is not dict:
        raise VerificationError("Railway status latest deployment metadata is missing")
    if (
        metadata.get("buildOnly") is not False
        or metadata.get("reason") != expected_deployment_reason
    ):
        raise VerificationError("Railway status latest deployment mode is invalid")
    if metadata.get("imageDigest") != expected_image_digest:
        raise VerificationError("Railway status latest image digest does not match")
    volume_mounts = metadata.get("volumeMounts")
    if volume_mounts != []:
        raise VerificationError(
            "Railway status latest deployment unexpectedly has mounts"
        )

    service_manifest = metadata.get("serviceManifest")
    if type(service_manifest) is not dict:
        raise VerificationError("Railway status latest service manifest is missing")
    build = service_manifest.get("build")
    deploy = service_manifest.get("deploy")
    if type(build) is not dict or type(deploy) is not dict:
        raise VerificationError("Railway status latest service manifest is invalid")
    if build.get("builder") != "DOCKERFILE":
        raise VerificationError("Railway status builder is not DOCKERFILE")
    if build.get("dockerfilePath") != "ops/railway/Dockerfile.static":
        raise VerificationError("Railway status Dockerfile path does not match")
    if deploy.get("healthcheckPath") != "/healthz":
        raise VerificationError("Railway status healthcheck path does not match")
    if type(deploy.get("numReplicas")) is not int or deploy.get("numReplicas") != 1:
        raise VerificationError("Railway status replica count does not match")
    if "cronSchedule" not in deploy or deploy["cronSchedule"] is not None:
        raise VerificationError(
            "Railway status deployment unexpectedly has a cron schedule"
        )
    if "requiredMountPath" not in deploy or deploy["requiredMountPath"] is not None:
        raise VerificationError(
            "Railway status deployment unexpectedly requires a mount"
        )

    return TopologyEvidence(
        project_id=project_id,
        environment_id=environment_id,
        service_id=service_id,
        service_name=service_name,
        deployment_id=deployment_id,
        deployment_reason=expected_deployment_reason,
        source_attached=False,
        cron_schedule=None,
        volume_instance_count=0,
        volume_mount_count=0,
    )


def normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise VerificationError("live base URL is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise VerificationError("live base URL must be a credential-free HTTPS origin")
    try:
        port = parsed.port
    except ValueError as error:
        raise VerificationError("live base URL port is invalid") from error
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if not host or any(character.isspace() for character in host):
        raise VerificationError("live base URL hostname is invalid")
    authority = f"{host}:{port}" if port is not None else host
    normalized = f"https://{authority}"
    if normalized not in ALLOWED_LIVE_ORIGINS:
        raise VerificationError(
            "live base URL is outside the reviewed origin allowlist"
        )
    return normalized


def _live_url_policy(url: str) -> None:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in ALLOWED_LIVE_ORIGINS:
        raise FetchError("Railway verifier URL escaped the reviewed origin allowlist")


def fetch_json(url: str, timeout: float, maximum_bytes: int) -> HttpPayload:
    """Fetch one bounded resource through the pinned-IP public transport."""

    try:
        response = safe_fetch_response(
            url,
            max_bytes=maximum_bytes,
            timeout=timeout,
            max_redirects=0,
            url_policy=_live_url_policy,
            headers={
                "Accept": "*/*",
                "Cache-Control": "no-cache",
                "User-Agent": "palimpsest-railway-release-verifier/1",
            },
        )
    except FetchError as error:
        raise VerificationError(
            "live JSON endpoint could not be reached safely"
        ) from error
    headers = {name.lower(): value for name, value in response.headers.items()}
    return HttpPayload(
        status=response.status,
        final_url=response.url,
        body=response.body,
        content_type=headers.get("content-type", ""),
        cache_control=headers.get("cache-control", ""),
    )


def _require_json_response(
    response: HttpPayload, *, requested_url: str, label: str
) -> None:
    if response.status != 200:
        raise VerificationError(f"{label} did not return HTTP 200")
    if response.final_url != requested_url:
        raise VerificationError(f"{label} redirected away from the exact origin")
    media_type = response.content_type.partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise VerificationError(f"{label} did not return application/json")
    cache_tokens = {
        token.strip().lower()
        for token in response.cache_control.split(",")
        if token.strip()
    }
    if "no-store" not in cache_tokens:
        raise VerificationError(f"{label} is not protected from stale caching")


def _quoted_critical_path(path: str) -> str:
    return "/".join(quote(segment, safe="-._~") for segment in path.split("/"))


def _critical_inventory_digest(rows: tuple[CriticalFile, ...]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(row.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(row.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _verify_live_once(
    *,
    base_url: str,
    identity: ReleaseIdentity,
    now: datetime,
    maximum_release_age_seconds: int,
    future_skew_seconds: int,
    timeout_seconds: float,
    fetcher: Fetcher,
) -> dict[str, Any]:
    nonce = quote(f"{identity.source_commit[:12]}-{int(now.timestamp())}", safe="")
    health_url = f"{base_url}/healthz?release_verify={nonce}"
    manifest_url = f"{base_url}/railway-release.json?release_verify={nonce}"
    health_response = fetcher(health_url, timeout_seconds, MAX_HEALTH_BYTES)
    _require_json_response(
        health_response, requested_url=health_url, label="live health endpoint"
    )
    health = _decode_json(
        health_response.body, label="live health response", maximum=MAX_HEALTH_BYTES
    )
    if type(health) is not dict:
        raise VerificationError("live health response root is not an object")
    expected_health = {
        "status": "ready",
        "service": "palimpsest-publication",
        "topology": "static-only",
        "mcp_available_here": False,
        "source_commit": identity.source_commit,
        "tree_sha256": identity.tree_sha256,
    }
    for key, expected in expected_health.items():
        if health.get(key) != expected:
            raise VerificationError(f"live health {key} does not match")

    manifest_response = fetcher(
        manifest_url, timeout_seconds, MAX_RELEASE_MANIFEST_BYTES
    )
    _require_json_response(
        manifest_response,
        requested_url=manifest_url,
        label="live release manifest endpoint",
    )
    served_manifest_sha256 = hashlib.sha256(manifest_response.body).hexdigest()
    if served_manifest_sha256 != identity.manifest_sha256:
        raise VerificationError("live release manifest bytes do not match")
    manifest = _decode_json(
        manifest_response.body,
        label="live Railway release manifest",
        maximum=MAX_RELEASE_MANIFEST_BYTES,
    )
    if type(manifest) is not dict:
        raise VerificationError("live Railway release manifest root is not an object")
    expected_manifest = {
        "schema_version": RELEASE_SCHEMA,
        "source_commit": identity.source_commit,
        "tree_sha256": identity.tree_sha256,
        "deployment_source": "local-git-archive",
        "github_required": False,
        "state": "artifact_ready",
    }
    for key, expected in expected_manifest.items():
        if manifest.get(key) != expected:
            raise VerificationError(f"live release manifest {key} does not match")
    built_at = _timestamp(manifest.get("built_at"), field="live release built_at")
    if built_at != identity.built_at:
        raise VerificationError(
            "live release built_at does not match the local manifest"
        )
    _fresh(
        built_at,
        now=now,
        maximum_age_seconds=maximum_release_age_seconds,
        future_skew_seconds=future_skew_seconds,
        label="live Railway release manifest",
    )
    verified_bytes = 0
    for critical in identity.critical_files:
        encoded_path = _quoted_critical_path(critical.path)
        critical_url = f"{base_url}/{encoded_path}?release_verify={nonce}"
        response = fetcher(
            critical_url,
            timeout_seconds,
            min(critical.size, MAX_CRITICAL_FILE_BYTES) + 1,
        )
        if response.status != 200:
            raise VerificationError(
                f"live critical file did not return HTTP 200: {critical.path}"
            )
        if response.final_url != critical_url:
            raise VerificationError(
                f"live critical file redirected away from the exact origin: {critical.path}"
            )
        if len(response.body) != critical.size:
            raise VerificationError(
                f"live critical file size does not match: {critical.path}"
            )
        if hashlib.sha256(response.body).hexdigest() != critical.sha256:
            raise VerificationError(
                f"live critical file SHA-256 does not match: {critical.path}"
            )
        verified_bytes += critical.size
        if verified_bytes > MAX_CRITICAL_AGGREGATE_BYTES:
            raise VerificationError("live critical files exceed aggregate cap")
    return {
        "base_url": base_url,
        "health": {
            "path": "/healthz",
            "http_status": 200,
            "cache_control": "no-store",
            "source_commit": identity.source_commit,
            "tree_sha256": identity.tree_sha256,
        },
        "release_manifest": {
            "path": "/railway-release.json",
            "http_status": 200,
            "cache_control": "no-store",
            "source_commit": identity.source_commit,
            "tree_sha256": identity.tree_sha256,
            "manifest_sha256": identity.manifest_sha256,
            "built_at": _utc_text(identity.built_at),
        },
        "critical_files": {
            "all_manifest_entries_verified": True,
            "verified_count": len(identity.critical_files),
            "verified_total_bytes": verified_bytes,
            "inventory_sha256": _critical_inventory_digest(identity.critical_files),
        },
    }


def verify_live_release(
    *,
    base_url: str,
    identity: ReleaseIdentity,
    now: datetime,
    maximum_release_age_seconds: int,
    future_skew_seconds: int,
    attempts: int,
    retry_delay_seconds: float,
    timeout_seconds: float,
    fetcher: Fetcher = fetch_json,
    sleeper: Sleeper = time.sleep,
    clock: Clock | None = None,
) -> tuple[dict[str, Any], int]:
    if not 1 <= attempts <= MAX_ATTEMPTS:
        raise VerificationError("attempt count is outside the safe range")
    if not 0 <= retry_delay_seconds <= MAX_RETRY_DELAY_SECONDS:
        raise VerificationError("retry delay is outside the safe range")
    if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise VerificationError("request timeout is outside the safe range")
    origin = normalize_base_url(base_url)
    last_error: VerificationError | None = None
    for attempt in range(1, attempts + 1):
        try:
            attempt_now = (
                now
                if clock is None
                else _clock_value(clock, label="live verification clock")
            )
            return (
                _verify_live_once(
                    base_url=origin,
                    identity=identity,
                    now=attempt_now,
                    maximum_release_age_seconds=maximum_release_age_seconds,
                    future_skew_seconds=future_skew_seconds,
                    timeout_seconds=timeout_seconds,
                    fetcher=fetcher,
                ),
                attempt,
            )
        except VerificationError as error:
            last_error = error
            if attempt < attempts:
                sleeper(retry_delay_seconds)
    assert last_error is not None
    raise VerificationError(
        f"live release verification failed after {attempts} attempt(s): {last_error}"
    ) from last_error


def build_receipt(
    *,
    identity: ReleaseIdentity,
    deployment: DeploymentEvidence,
    topology: TopologyEvidence,
    live: dict[str, Any],
    verified_at: datetime,
    attempts_used: dict[str, int | None],
    attempts_limit: int,
    retry_delay_seconds: float,
    timeout_seconds: float,
    maximum_deployment_age_seconds: int,
    maximum_release_age_seconds: int,
    future_skew_seconds: int,
) -> dict[str, Any]:
    """Build an allowlisted receipt; no raw CLI or HTTP metadata is retained."""

    return {
        "schema_version": RECEIPT_SCHEMA,
        "status": "verified",
        "verified_at": _utc_text(verified_at),
        "preflight": {
            "expected_source_commit": identity.source_commit,
            "checkout_source_commit": identity.source_commit,
            "current_main_source_commit": identity.source_commit,
            "worktree_clean": True,
        },
        "release": {
            "schema_version": RELEASE_SCHEMA,
            "source_commit": identity.source_commit,
            "tree_sha256": identity.tree_sha256,
            "manifest_sha256": identity.manifest_sha256,
            "built_at": _utc_text(identity.built_at),
        },
        "deployment": {
            "deployment_id": deployment.deployment_id,
            "status": deployment.status,
            "created_at": _utc_text(deployment.created_at),
            "image_digest": deployment.image_digest,
            "reason": deployment.reason,
        },
        "topology": {
            "project_id": topology.project_id,
            "environment_id": topology.environment_id,
            "service_id": topology.service_id,
            "service_name": topology.service_name,
            "latest_deployment_id": topology.deployment_id,
            "latest_deployment_reason": topology.deployment_reason,
            "source_attached": topology.source_attached,
            "cron_schedule": topology.cron_schedule,
            "volume_instance_count": topology.volume_instance_count,
            "volume_mount_count": topology.volume_mount_count,
            "service_manifest": {
                "builder": "DOCKERFILE",
                "dockerfile_path": "ops/railway/Dockerfile.static",
                "healthcheck_path": "/healthz",
                "num_replicas": 1,
            },
        },
        "live": live,
        "verification_policy": {
            "attempts_used": attempts_used,
            "attempts_limit": attempts_limit,
            "retry_delay_seconds": retry_delay_seconds,
            "request_timeout_seconds": timeout_seconds,
            "maximum_deployment_age_seconds": maximum_deployment_age_seconds,
            "maximum_release_age_seconds": maximum_release_age_seconds,
            "maximum_future_skew_seconds": future_skew_seconds,
        },
    }


def canonical_json(document: dict[str, Any]) -> bytes:
    try:
        payload = (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        raise VerificationError("receipt is not canonical JSON") from error
    if len(payload) > MAX_RECEIPT_BYTES:
        raise VerificationError("receipt exceeds its byte ceiling")
    return payload


def write_atomic_receipt(path: Path, document: dict[str, Any]) -> str:
    payload = canonical_json(document)
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise VerificationError("receipt directory is unavailable") from error
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
        parent_metadata.st_mode
    ):
        raise VerificationError("receipt directory is not a non-symlink directory")
    try:
        destination_metadata = path.lstat()
    except FileNotFoundError:
        destination_metadata = None
    except OSError as error:
        raise VerificationError("receipt destination could not be inspected") from error
    if destination_metadata is not None and (
        stat.S_ISLNK(destination_metadata.st_mode)
        or not stat.S_ISREG(destination_metadata.st_mode)
    ):
        raise VerificationError("receipt destination is not a regular file")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise VerificationError("receipt could not be written atomically") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(payload).hexdigest()


def verify_and_write(
    *,
    expected_source_commit: str,
    checkout_source_commit: str,
    current_main_source_commit: str,
    git_status_path: Path,
    release_manifest_path: Path,
    expected_tree_sha256: str,
    expected_manifest_sha256: str,
    deployment_json_path: Path,
    status_json_path: Path,
    expected_deployment_id: str,
    expected_image_digest: str | None,
    expected_project_id: str,
    expected_environment_id: str,
    expected_service_id: str,
    live_base_url: str,
    public_base_url: str | None,
    receipt_path: Path,
    now: datetime,
    attempts: int,
    retry_delay_seconds: float,
    timeout_seconds: float,
    maximum_deployment_age_seconds: int,
    maximum_release_age_seconds: int,
    future_skew_seconds: int,
    fetcher: Fetcher = fetch_json,
    sleeper: Sleeper = time.sleep,
    clock: Clock = _system_utc_now,
) -> tuple[dict[str, Any], str]:
    git_status = _read_regular_file(
        git_status_path,
        label="git status evidence",
        maximum=MAX_GIT_STATUS_BYTES,
    )
    source_commit = validate_preflight(
        expected_source_commit=expected_source_commit,
        checkout_source_commit=checkout_source_commit,
        current_main_source_commit=current_main_source_commit,
        git_status=git_status,
    )
    identity, _manifest_payload = load_release_identity(
        release_manifest_path,
        expected_source_commit=source_commit,
        expected_tree_sha256=expected_tree_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
        now=now,
        maximum_age_seconds=maximum_release_age_seconds,
        future_skew_seconds=future_skew_seconds,
    )
    deployment_payload = _read_regular_file(
        deployment_json_path,
        label="Railway deployment JSON",
        maximum=MAX_DEPLOYMENT_DOCUMENT_BYTES,
    )
    deployment = parse_deployment_evidence(
        deployment_payload,
        expected_deployment_id=expected_deployment_id,
        expected_image_digest=expected_image_digest,
        now=now,
        maximum_age_seconds=maximum_deployment_age_seconds,
        future_skew_seconds=future_skew_seconds,
    )
    status_payload = _read_regular_file(
        status_json_path,
        label="Railway status JSON",
        maximum=MAX_STATUS_DOCUMENT_BYTES,
    )
    topology = parse_status_topology(
        status_payload,
        expected_project_id=expected_project_id,
        expected_environment_id=expected_environment_id,
        expected_service_id=expected_service_id,
        expected_deployment_id=deployment.deployment_id,
        expected_image_digest=deployment.image_digest,
        expected_deployment_reason=deployment.reason,
    )
    provider_origin = normalize_base_url(live_base_url)
    public_origin = (
        normalize_base_url(public_base_url) if public_base_url is not None else None
    )
    if public_origin == provider_origin:
        raise VerificationError("public base URL must differ from the provider origin")
    provider_live, provider_attempts = verify_live_release(
        base_url=provider_origin,
        identity=identity,
        now=now,
        maximum_release_age_seconds=maximum_release_age_seconds,
        future_skew_seconds=future_skew_seconds,
        attempts=attempts,
        retry_delay_seconds=retry_delay_seconds,
        timeout_seconds=timeout_seconds,
        fetcher=fetcher,
        sleeper=sleeper,
        clock=clock,
    )
    public_live: dict[str, Any] | None = None
    public_attempts: int | None = None
    if public_origin is not None:
        public_live, public_attempts = verify_live_release(
            base_url=public_origin,
            identity=identity,
            now=now,
            maximum_release_age_seconds=maximum_release_age_seconds,
            future_skew_seconds=future_skew_seconds,
            attempts=attempts,
            retry_delay_seconds=retry_delay_seconds,
            timeout_seconds=timeout_seconds,
            fetcher=fetcher,
            sleeper=sleeper,
            clock=clock,
        )
    live = {
        "provider_origin": provider_live,
        "public_origin": public_live,
        "public_origin_verified": public_live is not None,
        "manifest_byte_identical": (
            public_live["release_manifest"]["manifest_sha256"]
            == provider_live["release_manifest"]["manifest_sha256"]
            if public_live is not None
            else None
        ),
        "critical_inventory_byte_identical": (
            public_live["critical_files"]["inventory_sha256"]
            == provider_live["critical_files"]["inventory_sha256"]
            if public_live is not None
            else None
        ),
    }
    attempts_used = {
        "provider_origin": provider_attempts,
        "public_origin": public_attempts,
    }
    completed_at = _clock_value(clock, label="verification completion clock")
    if now.tzinfo is None or now.utcoffset() is None:
        raise VerificationError("verification start clock does not include a timezone")
    if completed_at < now.astimezone(UTC):
        raise VerificationError("verification completion clock moved backwards")
    _fresh(
        identity.built_at,
        now=completed_at,
        maximum_age_seconds=maximum_release_age_seconds,
        future_skew_seconds=future_skew_seconds,
        label="Railway release manifest at verification completion",
    )
    _fresh(
        deployment.created_at,
        now=completed_at,
        maximum_age_seconds=maximum_deployment_age_seconds,
        future_skew_seconds=future_skew_seconds,
        label="Railway deployment at verification completion",
    )
    receipt = build_receipt(
        identity=identity,
        deployment=deployment,
        topology=topology,
        live=live,
        verified_at=completed_at,
        attempts_used=attempts_used,
        attempts_limit=attempts,
        retry_delay_seconds=retry_delay_seconds,
        timeout_seconds=timeout_seconds,
        maximum_deployment_age_seconds=maximum_deployment_age_seconds,
        maximum_release_age_seconds=maximum_release_age_seconds,
        future_skew_seconds=future_skew_seconds,
    )
    digest = write_atomic_receipt(receipt_path, receipt)
    return receipt, digest


def _bounded_int(name: str, minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from error
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


def _bounded_float(name: str, minimum: float, maximum: float) -> Callable[[str], float]:
    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{name} must be numeric") from error
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum:g} and {maximum:g}"
            )
        return parsed

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify an exact Railway deployment and write a secret-free receipt."
    )
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--checkout-source-commit", required=True)
    parser.add_argument("--current-main-source-commit", required=True)
    parser.add_argument("--git-status-file", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--expected-tree-sha256", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--deployment-json", type=Path, required=True)
    parser.add_argument("--status-json", type=Path, required=True)
    parser.add_argument("--expected-deployment-id", required=True)
    parser.add_argument("--expected-image-digest")
    parser.add_argument("--expected-project-id", required=True)
    parser.add_argument("--expected-environment-id", required=True)
    parser.add_argument("--expected-service-id", required=True)
    parser.add_argument("--live-base-url", required=True)
    parser.add_argument("--public-base-url")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--attempts", type=_bounded_int("attempts", 1, MAX_ATTEMPTS), default=8
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=_bounded_float("retry delay", 0.0, MAX_RETRY_DELAY_SECONDS),
        default=5.0,
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=_bounded_float("request timeout", 0.1, MAX_TIMEOUT_SECONDS),
        default=10.0,
    )
    parser.add_argument(
        "--max-deployment-age-seconds",
        type=_bounded_int("deployment age", 1, MAX_AGE_SECONDS),
        default=7200,
    )
    parser.add_argument(
        "--max-release-age-seconds",
        type=_bounded_int("release age", 1, MAX_AGE_SECONDS),
        default=86400,
    )
    parser.add_argument(
        "--max-future-skew-seconds",
        type=_bounded_int("future skew", 0, MAX_FUTURE_SKEW_SECONDS),
        default=120,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = datetime.now(UTC)
    try:
        _receipt, digest = verify_and_write(
            expected_source_commit=args.expected_source_commit,
            checkout_source_commit=args.checkout_source_commit,
            current_main_source_commit=args.current_main_source_commit,
            git_status_path=args.git_status_file,
            release_manifest_path=args.release_manifest,
            expected_tree_sha256=args.expected_tree_sha256,
            expected_manifest_sha256=args.expected_manifest_sha256,
            deployment_json_path=args.deployment_json,
            status_json_path=args.status_json,
            expected_deployment_id=args.expected_deployment_id,
            expected_image_digest=args.expected_image_digest,
            expected_project_id=args.expected_project_id,
            expected_environment_id=args.expected_environment_id,
            expected_service_id=args.expected_service_id,
            live_base_url=args.live_base_url,
            public_base_url=args.public_base_url,
            receipt_path=args.receipt,
            now=now,
            attempts=args.attempts,
            retry_delay_seconds=args.retry_delay_seconds,
            timeout_seconds=args.request_timeout_seconds,
            maximum_deployment_age_seconds=args.max_deployment_age_seconds,
            maximum_release_age_seconds=args.max_release_age_seconds,
            future_skew_seconds=args.max_future_skew_seconds,
        )
    except VerificationError as error:
        print(f"Railway release verification failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "receipt": str(args.receipt),
                "receipt_sha256": digest,
                "status": "verified",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
