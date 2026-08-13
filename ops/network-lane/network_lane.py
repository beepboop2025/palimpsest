#!/usr/bin/env python3
"""Serialize Palimpsest's heavy network jobs and preserve crash evidence.

The wrapper deliberately exposes only two production network jobs: the fixed
BLEEDTHROUGH prober and Common Crawl's pinned ``cc-downloader`` manifest flow,
plus a root-only offline adoption operation for an existing mirror.  It never
evaluates a shell command.  An internal command-taking function exists so the
lock/signal contract can be tested with inert child processes.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import gzip
import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


EXIT_TEMPFAIL = 75
EXIT_CONFIG = 78
EXIT_IOERR = 74
EXIT_SOFTWARE = 70
RECEIPT_SCHEMA = "palimpsest-network-lane-receipt/v1"
RECONCILIATION_SCHEMA = "palimpsest-network-lane-reconciliation/v1"
INVENTORY_SCHEMA = "palimpsest-common-crawl-path-size-inventory/v1"
DEFAULT_STATE_DIR = Path("/var/lib/palimpsest/network-lane")
DEFAULT_VOLUME_PARENT = Path("/mnt")
BUNDLE_REVISION_PATH = Path(__file__).resolve().parent / "REVISION"
DOWNLOADER_PATH = Path("/usr/local/bin/cc-downloader")
DOWNLOADER_VERSION = "1.0.1"
BLEEDTHROUGH_PROBER = (
    Path(__file__).resolve().parent / "ops/bleedthrough_prober.sh"
)
MIN_MIRROR_QUIET_SECONDS = 900
MIN_MIRROR_FREE_BYTES = 256 * 1024 * 1024 * 1024
MIN_THREADS = 1
MAX_THREADS = 10
MIN_RETRIES = 1
MAX_RETRIES = 1000
MIN_OPERATOR_REASON_CHARS = 8
MAX_OPERATOR_REASON_CHARS = 500
MAX_JSON_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 16 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_OBJECTS = 100_000
FILE_MODE = 0o660
DIRECTORY_MODE = 0o750
SIGNAL_GRACE_SECONDS = 10.0

CRAWL_RE = re.compile(r"CC-MAIN-[0-9]{4}-[0-9]{2}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
INVOCATION_RE = re.compile(r"[0-9a-f]{32}\Z")
REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")


class LaneError(RuntimeError):
    """Base error for a refused or invalid lane operation."""


class LaneTemporaryError(LaneError):
    """Retryable refusal: lock busy, quiet window, or orphan marker."""


class LaneConfigurationError(LaneError):
    """Static configuration or input validation failed."""


@dataclass(frozen=True)
class LanePaths:
    root: Path
    lock: Path
    dataset_lock: Path
    active: Path
    mirror_completed: Path
    receipts: Path

    @classmethod
    def from_root(cls, root: Path | str) -> LanePaths:
        validated = _validate_directory(
            Path(root), label="network-lane state directory", reject_world_write=True
        )
        state = _validate_directory(
            validated / "state",
            label="network-lane mutable state directory",
            reject_world_write=True,
        )
        receipts = validated / "receipts"
        receipts = _validate_directory(
            receipts, label="network-lane receipt directory", reject_world_write=True
        )
        if validated.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise LaneConfigurationError(
                "network-lane state directory must not be group/world-writable"
            )
        return cls(
            root=validated,
            lock=validated / "lane.lock",
            dataset_lock=validated / "dataset.lock",
            active=state / "active.json",
            mirror_completed=state / "mirror-completed.json",
            receipts=receipts,
        )


@dataclass(frozen=True)
class MirrorPlan:
    crawl: str
    config_path: Path
    volume_root: Path
    manifest_path: Path
    mirror_root: Path
    threads: int
    retries: int
    downloader_sha256: str


@dataclass(frozen=True)
class ChildOutcome:
    exit_status: int
    received_signal: int | None
    spawn_error: str | None = None
    process_group_cleanup_required: bool = False


@dataclass(frozen=True)
class CompletionMetadata:
    fields: Mapping[str, Any]
    failure_status: int | None = None


def _validate_directory(path: Path, *, label: str, reject_world_write: bool) -> Path:
    if not path.is_absolute():
        raise LaneConfigurationError(f"{label} must be absolute")
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        after = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise LaneConfigurationError(f"cannot validate {label}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise LaneConfigurationError(f"{label} must be a real directory")
    if resolved != path:
        raise LaneConfigurationError(f"{label} must contain no symlink components")
    if (before.st_dev, before.st_ino, before.st_mode) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
    ):
        raise LaneConfigurationError(f"{label} changed during validation")
    if reject_world_write and before.st_mode & stat.S_IWOTH:
        raise LaneConfigurationError(f"{label} must not be world-writable")
    return resolved


def _secure_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    expected_uid: int | None = None,
    executable: bool = False,
    reject_group_write: bool = True,
) -> tuple[int, os.stat_result]:
    if not path.is_absolute():
        raise LaneConfigurationError(f"{label} must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LaneConfigurationError(f"cannot resolve {label}: {exc}") from exc
    if resolved != path:
        raise LaneConfigurationError(f"{label} must contain no symlink components")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LaneConfigurationError(f"cannot open {label}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LaneConfigurationError(
                f"{label} must be a singly linked regular file"
            )
        if info.st_size <= 0 or info.st_size > max_bytes:
            raise LaneConfigurationError(f"{label} has an invalid size")
        forbidden_write = stat.S_IWOTH
        if reject_group_write:
            forbidden_write |= stat.S_IWGRP
        if info.st_mode & forbidden_write:
            scope = "group/world" if reject_group_write else "world"
            raise LaneConfigurationError(f"{label} must not be {scope}-writable")
        if expected_uid is not None and info.st_uid != expected_uid:
            raise LaneConfigurationError(f"{label} has the wrong owner")
        if executable and not info.st_mode & stat.S_IXUSR:
            raise LaneConfigurationError(f"{label} is not executable")
        return fd, info
    except Exception:
        os.close(fd)
        raise


def _read_fd(fd: int, *, max_bytes: int, label: str) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    body = bytearray()
    while True:
        chunk = os.read(fd, min(64 * 1024, max_bytes + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > max_bytes:
            raise LaneConfigurationError(f"{label} exceeds its size bound")
    return bytes(body)


def _strict_json_bytes(body: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = body.decode("utf-8")
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LaneConfigurationError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise LaneConfigurationError(f"{label} must be a JSON object")
    return parsed


def load_mirror_plan(
    config_path: Path | str,
    crawl: str,
    *,
    expected_config_uid: int = 0,
    expected_volume_root: Path | None = None,
    allowed_volume_parent: Path = DEFAULT_VOLUME_PARENT,
    require_non_root_volume: bool = True,
    require_production_config_path: bool = True,
) -> MirrorPlan:
    """Load a root-owned closed-shape mirror plan without evaluating a shell."""

    if CRAWL_RE.fullmatch(crawl) is None:
        raise LaneConfigurationError("crawl must match CC-MAIN-YYYY-WW")
    config = Path(config_path)
    expected = Path("/etc/palimpsest/common-crawl-mirror") / f"{crawl}.json"
    if require_production_config_path and config != expected:
        raise LaneConfigurationError(f"config path must be exactly {expected}")
    fd, _ = _secure_regular_file(
        config,
        label="mirror config",
        max_bytes=MAX_CONFIG_BYTES,
        expected_uid=expected_config_uid,
    )
    try:
        raw = _strict_json_bytes(
            _read_fd(fd, max_bytes=MAX_CONFIG_BYTES, label="mirror config"),
            label="mirror config",
        )
    finally:
        os.close(fd)

    expected_keys = {
        "schema_version",
        "crawl",
        "volume_root",
        "manifest_path",
        "mirror_root",
        "threads",
        "retries",
        "downloader_sha256",
    }
    if set(raw) != expected_keys or raw.get("schema_version") != 1:
        raise LaneConfigurationError("mirror config has an unsupported shape")
    if raw.get("crawl") != crawl:
        raise LaneConfigurationError(
            "mirror config crawl does not match the unit instance"
        )
    if not isinstance(raw.get("threads"), int) or isinstance(raw.get("threads"), bool):
        raise LaneConfigurationError("threads must be an integer")
    if not MIN_THREADS <= raw["threads"] <= MAX_THREADS:
        raise LaneConfigurationError("threads is outside the reviewed 1..10 range")
    if not isinstance(raw.get("retries"), int) or isinstance(raw.get("retries"), bool):
        raise LaneConfigurationError("retries must be an integer")
    if not MIN_RETRIES <= raw["retries"] <= MAX_RETRIES:
        raise LaneConfigurationError("retries is outside the reviewed 1..1000 range")
    if (
        not isinstance(raw.get("downloader_sha256"), str)
        or SHA256_RE.fullmatch(raw["downloader_sha256"]) is None
    ):
        raise LaneConfigurationError("downloader_sha256 must be lowercase SHA-256")
    if (
        not isinstance(raw.get("volume_root"), str)
        or not isinstance(raw.get("manifest_path"), str)
        or not isinstance(raw.get("mirror_root"), str)
    ):
        raise LaneConfigurationError("mirror paths must be strings")

    volume_root = _validate_directory(
        Path(raw["volume_root"]),
        label="Common Crawl volume root",
        reject_world_write=True,
    )
    try:
        volume_relative = volume_root.relative_to(allowed_volume_parent)
    except ValueError as exc:
        raise LaneConfigurationError(
            f"volume_root must be below {allowed_volume_parent}"
        ) from exc
    if volume_relative == Path("."):
        raise LaneConfigurationError("volume_root must identify a dedicated mount")
    if expected_volume_root is not None and volume_root != expected_volume_root:
        raise LaneConfigurationError("volume_root does not match the expected mount")
    if require_non_root_volume and volume_root.stat().st_dev == Path("/").stat().st_dev:
        raise LaneConfigurationError(
            "Common Crawl volume root is on the root filesystem"
        )
    mirror_root = _validate_directory(
        Path(raw["mirror_root"]),
        label="Common Crawl mirror root",
        reject_world_write=True,
    )
    try:
        relative_mirror = mirror_root.relative_to(volume_root)
    except ValueError as exc:
        raise LaneConfigurationError("mirror_root must be below volume_root") from exc
    if relative_mirror == Path("."):
        raise LaneConfigurationError("mirror_root must be a dedicated descendant")
    if mirror_root.stat().st_dev != volume_root.stat().st_dev:
        raise LaneConfigurationError("mirror_root must use the volume-root filesystem")
    manifest_path = Path(raw["manifest_path"])
    expected_manifest = mirror_root / "cc-index-table.paths.gz"
    if manifest_path != expected_manifest:
        raise LaneConfigurationError(
            f"manifest_path must be exactly {expected_manifest}"
        )

    return MirrorPlan(
        crawl=crawl,
        config_path=config,
        volume_root=volume_root,
        manifest_path=manifest_path,
        mirror_root=mirror_root,
        threads=raw["threads"],
        retries=raw["retries"],
        downloader_sha256=raw["downloader_sha256"],
    )


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _inspect_mirror_config_binding(
    plan: MirrorPlan, *, expected_config_uid: int = 0
) -> dict[str, Any]:
    """Re-open the validated plan and bind its exact bytes to a receipt."""

    descriptor, _ = _secure_regular_file(
        plan.config_path,
        label="mirror config",
        max_bytes=MAX_CONFIG_BYTES,
        expected_uid=expected_config_uid,
    )
    try:
        body = _read_fd(
            descriptor, max_bytes=MAX_CONFIG_BYTES, label="mirror config"
        )
        raw = _strict_json_bytes(body, label="mirror config")
        digest = hashlib.sha256(body).hexdigest()
    finally:
        os.close(descriptor)

    expected = {
        "schema_version": 1,
        "crawl": plan.crawl,
        "volume_root": str(plan.volume_root),
        "manifest_path": str(plan.manifest_path),
        "mirror_root": str(plan.mirror_root),
        "threads": plan.threads,
        "retries": plan.retries,
        "downloader_sha256": plan.downloader_sha256,
    }
    if raw != expected:
        raise LaneConfigurationError(
            "mirror config changed after its plan was validated"
        )
    return {
        "schema_version": 1,
        "path": str(plan.config_path),
        "sha256": digest,
    }


def _inspect_manifest_with_paths(
    plan: MirrorPlan, *, expected_manifest_uid: int = 0
) -> tuple[dict[str, Any], tuple[str, ...]]:
    manifest_parent = _validate_directory(
        plan.manifest_path.parent,
        label="Common Crawl manifest directory",
        reject_world_write=True,
    )
    parent_info = manifest_parent.stat()
    if parent_info.st_uid != expected_manifest_uid or parent_info.st_mode & (
        stat.S_IWGRP | stat.S_IWOTH
    ):
        raise LaneConfigurationError(
            "Common Crawl manifest directory must be owner-only writable"
        )
    fd, _ = _secure_regular_file(
        plan.manifest_path,
        label="Common Crawl path manifest",
        max_bytes=MAX_MANIFEST_BYTES,
        expected_uid=expected_manifest_uid,
    )
    try:
        digest = _sha256_fd(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        pattern = re.compile(
            rf"cc-index/table/cc-main/warc/crawl={re.escape(plan.crawl)}/"
            r"subset=warc/part-[A-Za-z0-9._-]+\.(?:gz|zstd)\.parquet\Z"
        )
        total_bytes = 0
        paths: set[str] = set()
        with os.fdopen(os.dup(fd), "rb", closefd=True) as raw:
            with gzip.GzipFile(fileobj=raw, mode="rb") as zipped:
                for encoded in zipped:
                    total_bytes += len(encoded)
                    if total_bytes > MAX_MANIFEST_UNCOMPRESSED_BYTES:
                        raise LaneConfigurationError(
                            "path manifest expands beyond 64 MiB"
                        )
                    if len(paths) >= MAX_MANIFEST_OBJECTS:
                        raise LaneConfigurationError(
                            "path manifest has too many objects"
                        )
                    try:
                        item = encoded.rstrip(b"\r\n").decode("ascii")
                    except UnicodeDecodeError as exc:
                        raise LaneConfigurationError(
                            "path manifest contains a non-ASCII object path"
                        ) from exc
                    if not item or pattern.fullmatch(item) is None:
                        raise LaneConfigurationError(
                            "path manifest contains an out-of-scope object"
                        )
                    if item in paths:
                        raise LaneConfigurationError("path manifest repeats an object")
                    paths.add(item)
    except (OSError, EOFError) as exc:
        raise LaneConfigurationError(f"cannot validate path manifest: {exc}") from exc
    finally:
        os.close(fd)
    if not paths:
        raise LaneConfigurationError("path manifest is empty")
    return (
        {
            "path": str(plan.manifest_path),
            "sha256": digest,
            "object_count": len(paths),
        },
        tuple(sorted(paths)),
    )


def inspect_manifest(
    plan: MirrorPlan, *, expected_manifest_uid: int = 0
) -> dict[str, Any]:
    receipt, _ = _inspect_manifest_with_paths(
        plan, expected_manifest_uid=expected_manifest_uid
    )
    return receipt


def _inspect_parquet_file(path: Path) -> tuple[int | None, str | None]:
    try:
        before = path.lstat()
    except OSError as exc:
        return None, f"lstat:{type(exc).__name__}"
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return None, "not-a-real-regular-file"
    if before.st_nlink != 1:
        return None, "hard-linked-file"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        return None, f"open:{type(exc).__name__}"
    try:
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            return None, "file-changed-during-validation"
        if after.st_size < 8:
            return after.st_size, "too-small-for-parquet"
        first = os.pread(descriptor, 4, 0)
        last = os.pread(descriptor, 4, after.st_size - 4)
        if first != b"PAR1" or last != b"PAR1":
            return after.st_size, "missing-parquet-magic"
        return after.st_size, None
    finally:
        os.close(descriptor)


def inspect_mirror_inventory(
    plan: MirrorPlan, expected_objects: Sequence[str]
) -> dict[str, Any]:
    """Validate one crawl subtree and seal a canonical path+size inventory."""

    expected = set(expected_objects)
    errors: list[str] = []
    observed: set[str] = set()
    sizes: dict[str, int] = {}
    parquet_magic_validated: set[str] = set()
    crawl_root = (
        plan.mirror_root
        / "cc-index/table/cc-main/warc"
        / f"crawl={plan.crawl}"
        / "subset=warc"
    )
    try:
        validated_root = _validate_directory(
            crawl_root,
            label="mirrored crawl output directory",
            reject_world_write=True,
        )

        def raise_walk_error(error: OSError) -> None:
            raise error

        for directory, directory_names, file_names in os.walk(
            validated_root, topdown=True, onerror=raise_walk_error, followlinks=False
        ):
            current = Path(directory)
            safe_directories: list[str] = []
            for name in sorted(directory_names):
                candidate = current / name
                try:
                    information = candidate.lstat()
                except OSError as exc:
                    errors.append(f"directory-lstat:{type(exc).__name__}")
                    continue
                if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(
                    information.st_mode
                ):
                    errors.append("non-directory-or-symlink-in-crawl-tree")
                    continue
                errors.append("unexpected-directory-in-crawl-tree")
                safe_directories.append(name)
            directory_names[:] = safe_directories
            for name in sorted(file_names):
                candidate = current / name
                try:
                    relative = candidate.relative_to(plan.mirror_root).as_posix()
                except ValueError:
                    errors.append("output-path-escaped-mirror-root")
                    continue
                observed.add(relative)
                if len(observed) > MAX_MANIFEST_OBJECTS:
                    errors.append("crawl-output-object-limit-exceeded")
                    break
                size, error = _inspect_parquet_file(candidate)
                if size is not None:
                    sizes[relative] = size
                if error is not None:
                    errors.append(error)
                else:
                    parquet_magic_validated.add(relative)
            if len(observed) > MAX_MANIFEST_OBJECTS:
                break
    except LaneConfigurationError as exc:
        errors.append(f"crawl-root:{exc}")
    except OSError as exc:
        errors.append(f"walk:{type(exc).__name__}")

    missing = sorted(expected - observed)
    extra = sorted(observed - expected)
    if missing:
        errors.append(f"missing-manifest-objects:{len(missing)}")
    if extra:
        errors.append(f"extra-crawl-objects:{len(extra)}")
    inventory_sha256: str | None = None
    if len(sizes) == len(observed):
        digest = hashlib.sha256()
        for relative in sorted(sizes):
            digest.update(f"{relative}\t{sizes[relative]}\n".encode("utf-8"))
        inventory_sha256 = digest.hexdigest()
    valid = not errors and observed == expected and len(sizes) == len(observed)
    return {
        "schema_version": INVENTORY_SCHEMA,
        "valid": valid,
        "canonicalization": "UTF-8 POSIX relative path, TAB, decimal bytes, LF; sorted",
        "inventory_sha256": inventory_sha256,
        "expected_object_count": len(expected),
        "observed_object_count": len(observed),
        "observed_total_bytes": sum(sizes.values()),
        "parquet_magic_validated_count": len(parquet_magic_validated),
        "missing_object_count": len(missing),
        "extra_object_count": len(extra),
        "errors": errors[:20],
        "integrity_limit": (
            "inventory binds canonical paths and sizes and checks PAR1 framing; "
            "it does not hash complete Parquet object contents"
        ),
    }


def verify_completed_mirror(
    plan: MirrorPlan,
    *,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    expected_manifest_uid: int = 0,
) -> dict[str, Any]:
    """Recompute and bind a mirror's manifest, inventory, and success receipt.

    The caller must hold :func:`exclusive_dataset` until it has finished using
    the files.  Recomputing under that lock turns the mirror completion marker
    into a race-free local read boundary rather than trusting downloader exit.
    """

    paths = LanePaths.from_root(state_dir)
    # A later mirror may have mutated these same paths before it was killed.
    # Its orphan marker therefore invalidates every older success stamp even
    # when path, size, and PAR1 framing happen to remain unchanged.
    _refuse_orphan(paths)
    stamp = _read_state_json(paths.mirror_completed)
    if stamp is None:
        raise LaneConfigurationError("no Common Crawl mirror completion exists")
    if (
        stamp.get("schema_version") != RECEIPT_SCHEMA
        or stamp.get("job_kind") != "common-crawl-mirror"
        or stamp.get("state") != "completed"
        or stamp.get("exit_status") != 0
        or stamp.get("crawl") != plan.crawl
    ):
        raise LaneConfigurationError(
            "latest Common Crawl mirror is not a successful matching crawl"
        )

    manifest, expected_objects = _inspect_manifest_with_paths(
        plan, expected_manifest_uid=expected_manifest_uid
    )
    stamped_manifest = stamp.get("manifest")
    if not isinstance(stamped_manifest, dict) or any(
        stamped_manifest.get(field) != manifest[field]
        for field in ("path", "sha256", "object_count")
    ):
        raise LaneConfigurationError(
            "mirror completion does not match the current path manifest"
        )

    inventory = inspect_mirror_inventory(plan, expected_objects)
    stamped_inventory = stamp.get("output_inventory")
    inventory_fields = (
        "schema_version",
        "valid",
        "inventory_sha256",
        "expected_object_count",
        "observed_object_count",
        "observed_total_bytes",
        "parquet_magic_validated_count",
        "missing_object_count",
        "extra_object_count",
    )
    if (
        not inventory["valid"]
        or not isinstance(stamped_inventory, dict)
        or stamped_inventory.get("valid") is not True
        or any(
            stamped_inventory.get(field) != inventory[field]
            for field in inventory_fields
        )
    ):
        raise LaneConfigurationError(
            "mirror output no longer matches its successful inventory receipt"
        )

    receipt_path_value = stamp.get("receipt_path")
    receipt_sha256 = stamp.get("receipt_sha256")
    if (
        not isinstance(receipt_path_value, str)
        or not isinstance(receipt_sha256, str)
        or SHA256_RE.fullmatch(receipt_sha256) is None
    ):
        raise LaneConfigurationError("mirror completion receipt binding is invalid")
    receipt_path = Path(receipt_path_value)
    if receipt_path.parent != paths.receipts:
        raise LaneConfigurationError("mirror receipt path escaped the receipt directory")
    descriptor, _ = _secure_regular_file(
        receipt_path,
        label="Common Crawl mirror receipt",
        max_bytes=MAX_JSON_BYTES,
        expected_uid=None,
        reject_group_write=False,
    )
    try:
        actual_receipt_sha256 = _sha256_fd(descriptor)
    finally:
        os.close(descriptor)
    if actual_receipt_sha256 != receipt_sha256:
        raise LaneConfigurationError("mirror receipt hash no longer matches")
    receipt_document = _read_state_json(receipt_path)
    if receipt_document is None or any(
        receipt_document.get(field) != stamp.get(field)
        for field in (
            "schema_version",
            "state",
            "job_kind",
            "crawl",
            "exit_status",
            "completed_unix_ns",
            "network_lane_revision",
            "manifest",
            "output_inventory",
        )
    ):
        raise LaneConfigurationError("mirror stamp does not match its bound receipt")

    return {
        "crawl": plan.crawl,
        "completed_at": stamp.get("completed_at"),
        "completed_unix_ns": stamp.get("completed_unix_ns"),
        "network_lane_revision": stamp.get("network_lane_revision"),
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha256,
        "manifest": manifest,
        "output_inventory": inventory,
    }


def inspect_downloader(
    expected_sha256: str,
    *,
    path: Path = DOWNLOADER_PATH,
    expected_uid: int = 0,
) -> dict[str, Any]:
    fd, _ = _secure_regular_file(
        path,
        label="cc-downloader",
        max_bytes=256 * 1024 * 1024,
        expected_uid=expected_uid,
        executable=True,
    )
    try:
        digest = _sha256_fd(fd)
    finally:
        os.close(fd)
    if digest != expected_sha256:
        raise LaneConfigurationError(
            "cc-downloader does not match the configured SHA-256"
        )
    try:
        version = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=10,
            shell=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaneConfigurationError(f"cannot identify cc-downloader: {exc}") from exc
    output = version.stdout.strip()
    if (
        version.returncode != 0
        or re.fullmatch(rf"cc-downloader\s+{re.escape(DOWNLOADER_VERSION)}", output)
        is None
    ):
        raise LaneConfigurationError(
            f"cc-downloader must report exact version {DOWNLOADER_VERSION}"
        )
    return {"path": str(path), "sha256": digest, "version": DOWNLOADER_VERSION}


def inspect_runtime_revision(
    *, path: Path = BUNDLE_REVISION_PATH, expected_uid: int = 0
) -> str:
    descriptor, _ = _secure_regular_file(
        path,
        label="network-lane bundle revision",
        max_bytes=128,
        expected_uid=expected_uid,
    )
    try:
        try:
            revision = _read_fd(
                descriptor, max_bytes=128, label="network-lane bundle revision"
            ).decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise LaneConfigurationError(
                "network-lane bundle revision is not ASCII"
            ) from exc
    finally:
        os.close(descriptor)
    if REVISION_RE.fullmatch(revision) is None:
        raise LaneConfigurationError(
            "network-lane bundle revision must be 40 lowercase hex"
        )
    return revision


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    body = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    if len(body) > MAX_JSON_BYTES:
        raise LaneConfigurationError("network-lane receipt exceeds 64 KiB")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(temporary, flags, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        view = memoryview(body)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_state_json(path: Path, *, orphan: bool = False) -> dict[str, Any] | None:
    try:
        fd, _ = _secure_regular_file(
            path,
            label=path.name,
            max_bytes=MAX_JSON_BYTES,
            expected_uid=None,
            reject_group_write=False,
        )
    except LaneConfigurationError as exc:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        if orphan:
            raise LaneTemporaryError(f"orphan marker is unreadable: {exc}") from exc
        raise LaneTemporaryError(f"completion stamp is invalid: {exc}") from exc
    try:
        body = _read_fd(fd, max_bytes=MAX_JSON_BYTES, label=path.name)
        parsed = _strict_json_bytes(body, label=path.name)
    except LaneConfigurationError as exc:
        if orphan:
            raise LaneTemporaryError(f"orphan marker is malformed: {exc}") from exc
        raise LaneTemporaryError(f"completion stamp is malformed: {exc}") from exc
    finally:
        os.close(fd)
    return parsed


@contextmanager
def _exclusive_root_owned_lock(
    paths: LanePaths, lock_path: Path, *, busy_message: str
):
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags)
        info = os.fstat(fd)
        path_info = lock_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LaneTemporaryError("shared lock is not a safe regular file")
        if (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise LaneTemporaryError("shared lock changed during validation")
        if info.st_uid != paths.root.stat().st_uid or info.st_mode & stat.S_IWOTH:
            raise LaneTemporaryError("shared lock ownership or mode is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        if "fd" in locals():
            os.close(fd)
        raise LaneTemporaryError(busy_message) from exc
    except LaneTemporaryError:
        if "fd" in locals():
            os.close(fd)
        raise
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise LaneTemporaryError(busy_message) from exc
        raise
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _exclusive_lane(paths: LanePaths):
    return _exclusive_root_owned_lock(
        paths, paths.lock, busy_message="heavy network lane is busy"
    )


def _exclusive_dataset(paths: LanePaths):
    return _exclusive_root_owned_lock(
        paths,
        paths.dataset_lock,
        busy_message="Common Crawl dataset is busy",
    )


@contextmanager
def exclusive_dataset(state_dir: Path | str = DEFAULT_STATE_DIR):
    """Hold the durable Common Crawl data lock for one complete local reader."""

    paths = LanePaths.from_root(state_dir)
    with _exclusive_dataset(paths):
        yield


def _timestamp(now_ns: int) -> str:
    return (
        datetime.fromtimestamp(now_ns / 1_000_000_000, tz=UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _normalize_operator_reason(reason: str, *, operation: str) -> str:
    if not isinstance(reason, str):
        raise LaneConfigurationError(f"{operation} reason must be text")
    normalized = " ".join(reason.split())
    if not MIN_OPERATOR_REASON_CHARS <= len(normalized) <= MAX_OPERATOR_REASON_CHARS:
        raise LaneConfigurationError(
            f"{operation} reason must be "
            f"{MIN_OPERATOR_REASON_CHARS}..{MAX_OPERATOR_REASON_CHARS} characters"
        )
    return normalized


def _refuse_orphan(paths: LanePaths) -> None:
    marker = _read_state_json(paths.active, orphan=True)
    if marker is not None:
        invocation = marker.get("invocation_id", "unknown")
        raise LaneTemporaryError(
            "orphan active marker blocks the network lane; "
            f"reconcile invocation {invocation} explicitly"
        )


def _require_mirror_quiet(paths: LanePaths, *, now_ns: int, quiet_seconds: int) -> None:
    if quiet_seconds < MIN_MIRROR_QUIET_SECONDS:
        raise LaneConfigurationError(
            "mirror quiet window cannot be less than 900 seconds"
        )
    stamp = _read_state_json(paths.mirror_completed)
    if stamp is None:
        return
    completed_ns = stamp.get("completed_unix_ns")
    if (
        stamp.get("schema_version") != RECEIPT_SCHEMA
        or stamp.get("job_kind") != "common-crawl-mirror"
        or not isinstance(completed_ns, int)
        or isinstance(completed_ns, bool)
        or completed_ns < 0
    ):
        raise LaneTemporaryError("mirror completion stamp has an unsupported shape")
    if now_ns < completed_ns:
        raise LaneTemporaryError("mirror completion stamp is in the future")
    if now_ns - completed_ns < quiet_seconds * 1_000_000_000:
        raise LaneTemporaryError(
            "mirror completion is inside the 15-minute quiet window"
        )


def _normalize_child_status(returncode: int) -> int:
    if returncode >= 0:
        return min(returncode, 255)
    return min(128 + abs(returncode), 255)


def _run_child(
    command: Sequence[str],
    *,
    signal_grace_seconds: float = SIGNAL_GRACE_SECONDS,
    handle_signals: bool = True,
) -> ChildOutcome:
    if not command or any(
        not isinstance(item, str) or not item or "\0" in item for item in command
    ):
        raise LaneConfigurationError("child argv is invalid")
    child: subprocess.Popen[bytes] | None = None
    received_signal: int | None = None
    signal_received_at: float | None = None
    previous_handlers: dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        nonlocal received_signal, signal_received_at
        if received_signal is None:
            received_signal = signum
            signal_received_at = time.monotonic()
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    if handle_signals:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, forward)
    try:
        try:
            child = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            return ChildOutcome(127, received_signal, type(exc).__name__)
        if received_signal is not None and child.poll() is None:
            forward(received_signal, None)
        while child.poll() is None:
            if (
                signal_received_at is not None
                and time.monotonic() - signal_received_at >= signal_grace_seconds
            ):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.05)
        cleanup_required = False
        cleanup_started: float | None = None
        kill_sent_at: float | None = None
        sent_kill = False
        while True:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            if cleanup_started is None:
                cleanup_required = True
                cleanup_started = time.monotonic()
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    break
            elif (
                not sent_kill
                and time.monotonic() - cleanup_started >= signal_grace_seconds
            ):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    break
                sent_kill = True
                kill_sent_at = time.monotonic()
            elif (
                sent_kill
                and kill_sent_at is not None
                and time.monotonic() - kill_sent_at >= signal_grace_seconds
            ):
                raise LaneTemporaryError(
                    "child process group survived SIGKILL; "
                    "orphan marker retained for reconciliation"
                )
            time.sleep(0.05)
        exit_status = _normalize_child_status(child.returncode)
        if received_signal is not None and exit_status == 0:
            exit_status = min(128 + received_signal, 255)
        if cleanup_required and exit_status == 0:
            exit_status = EXIT_SOFTWARE
        return ChildOutcome(
            exit_status,
            received_signal,
            None,
            cleanup_required,
        )
    finally:
        if handle_signals:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)


def execute_guarded_job(
    *,
    state_dir: Path | str,
    job_kind: str,
    command: Sequence[str],
    metadata_factory: Callable[[], Mapping[str, Any]] | None = None,
    completion_metadata_factory: (
        Callable[[ChildOutcome], Mapping[str, Any] | CompletionMetadata] | None
    ) = None,
    mirror_quiet_seconds: int | None = None,
    now_ns: Callable[[], int] = time.time_ns,
    signal_grace_seconds: float = SIGNAL_GRACE_SECONDS,
    handle_signals: bool = True,
) -> int:
    """Run one trusted argv while holding the shared lock for its whole life."""

    if job_kind not in {"common-crawl-mirror", "bleedthrough"}:
        raise LaneConfigurationError("unsupported network-lane job kind")
    paths = LanePaths.from_root(state_dir)
    with ExitStack() as locks:
        locks.enter_context(_exclusive_lane(paths))
        if job_kind == "common-crawl-mirror":
            # Readers take this second durable lock without taking the network
            # lane. Holding it through validation and stamp publication makes
            # the mirror handoff atomic from a guarded reader's perspective.
            locks.enter_context(_exclusive_dataset(paths))
        _refuse_orphan(paths)
        if mirror_quiet_seconds is not None:
            _require_mirror_quiet(
                paths, now_ns=now_ns(), quiet_seconds=mirror_quiet_seconds
            )
        metadata = dict(metadata_factory() if metadata_factory is not None else {})
        reserved = {
            "schema_version",
            "state",
            "job_kind",
            "invocation_id",
            "wrapper_pid",
            "started_at",
            "started_unix_ns",
            "completed_at",
            "completed_unix_ns",
            "exit_status",
            "received_signal",
            "spawn_error",
            "process_group_cleanup_required",
        }
        if set(metadata) & reserved:
            raise LaneConfigurationError(
                "job metadata attempts to replace receipt fields"
            )
        invocation_id = uuid.uuid4().hex
        started_ns = now_ns()
        active = {
            "schema_version": RECEIPT_SCHEMA,
            "state": "active",
            "job_kind": job_kind,
            "invocation_id": invocation_id,
            "wrapper_pid": os.getpid(),
            "started_at": _timestamp(started_ns),
            "started_unix_ns": started_ns,
            **metadata,
        }
        _atomic_write_json(paths.active, active)
        outcome = _run_child(
            command,
            signal_grace_seconds=signal_grace_seconds,
            handle_signals=handle_signals,
        )
        completed_ns = now_ns()
        completion_result = (
            completion_metadata_factory(outcome)
            if completion_metadata_factory is not None
            else {}
        )
        completion_failure_status: int | None = None
        if isinstance(completion_result, CompletionMetadata):
            completion_metadata = dict(completion_result.fields)
            completion_failure_status = completion_result.failure_status
        else:
            completion_metadata = dict(completion_result)
        if completion_failure_status is not None:
            if not 1 <= completion_failure_status <= 255:
                raise LaneConfigurationError(
                    "completion failure status must be in the 1..255 range"
                )
            if outcome.exit_status == 0:
                outcome = ChildOutcome(
                    completion_failure_status,
                    outcome.received_signal,
                    "completion-validation-failed",
                    outcome.process_group_cleanup_required,
                )
        if set(completion_metadata) & reserved:
            raise LaneConfigurationError(
                "completion metadata attempts to replace receipt fields"
            )
        completed = {
            **active,
            "state": (
                "completed"
                if outcome.exit_status == 0
                and not outcome.process_group_cleanup_required
                else "failed"
            ),
            "completed_at": _timestamp(completed_ns),
            "completed_unix_ns": completed_ns,
            "exit_status": outcome.exit_status,
            "received_signal": outcome.received_signal,
            "spawn_error": outcome.spawn_error,
            "process_group_cleanup_required": (
                outcome.process_group_cleanup_required
            ),
            **completion_metadata,
        }
        receipt_path = paths.receipts / f"{job_kind}-{invocation_id}.json"
        _atomic_write_json(receipt_path, completed)
        if job_kind == "common-crawl-mirror":
            stamp = {
                **completed,
                "receipt_path": str(receipt_path),
                "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            }
            _atomic_write_json(paths.mirror_completed, stamp)
        paths.active.unlink()
        _fsync_directory(paths.active.parent)
        return outcome.exit_status


def adopt_mirror(
    plan: MirrorPlan,
    *,
    reason: str,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    downloader_path: Path = DOWNLOADER_PATH,
    expected_config_uid: int = 0,
    expected_downloader_uid: int = 0,
    expected_manifest_uid: int = 0,
    revision_path: Path = BUNDLE_REVISION_PATH,
    expected_revision_uid: int = 0,
    expected_volume_root: Path | None = None,
    allowed_volume_parent: Path = DEFAULT_VOLUME_PARENT,
    require_non_root_volume: bool = True,
    require_production_config_path: bool = True,
    now_ns: Callable[[], int] = time.time_ns,
) -> int:
    """Validate and receipt an existing mirror without running a download."""

    if os.geteuid() != 0:
        raise LaneConfigurationError("mirror adoption must run as root")
    normalized_reason = _normalize_operator_reason(reason, operation="adoption")
    paths = LanePaths.from_root(state_dir)
    with ExitStack() as locks:
        # Preserve the live-mirror order: no reader or heavy network job can
        # overlap the complete validation and publication boundary.
        locks.enter_context(_exclusive_lane(paths))
        locks.enter_context(_exclusive_dataset(paths))
        _refuse_orphan(paths)
        started_ns = now_ns()

        validated_plan = load_mirror_plan(
            plan.config_path,
            plan.crawl,
            expected_config_uid=expected_config_uid,
            expected_volume_root=expected_volume_root,
            allowed_volume_parent=allowed_volume_parent,
            require_non_root_volume=require_non_root_volume,
            require_production_config_path=require_production_config_path,
        )
        if validated_plan != plan:
            raise LaneConfigurationError(
                "mirror plan no longer matches its root-owned config"
            )
        config_receipt = _inspect_mirror_config_binding(
            validated_plan, expected_config_uid=expected_config_uid
        )
        manifest_receipt, expected_objects = _inspect_manifest_with_paths(
            validated_plan, expected_manifest_uid=expected_manifest_uid
        )
        inventory = inspect_mirror_inventory(validated_plan, expected_objects)
        if not inventory["valid"]:
            details = ", ".join(inventory.get("errors", [])[:5])
            suffix = f": {details}" if details else ""
            raise LaneConfigurationError(
                "cannot adopt mirror with an invalid output inventory" + suffix
            )
        tool_receipt = inspect_downloader(
            validated_plan.downloader_sha256,
            path=downloader_path,
            expected_uid=expected_downloader_uid,
        )
        revision = inspect_runtime_revision(
            path=revision_path, expected_uid=expected_revision_uid
        )

        completed_ns = now_ns()
        invocation_id = uuid.uuid4().hex
        completed = {
            "schema_version": RECEIPT_SCHEMA,
            "state": "completed",
            "job_kind": "common-crawl-mirror",
            "invocation_id": invocation_id,
            "wrapper_pid": os.getpid(),
            "started_at": _timestamp(started_ns),
            "started_unix_ns": started_ns,
            "completed_at": _timestamp(completed_ns),
            "completed_unix_ns": completed_ns,
            "exit_status": 0,
            "received_signal": None,
            "spawn_error": None,
            "process_group_cleanup_required": False,
            "crawl": validated_plan.crawl,
            "config_path": str(validated_plan.config_path),
            "config": config_receipt,
            "volume_root": str(validated_plan.volume_root),
            "mirror_root": str(validated_plan.mirror_root),
            "threads": validated_plan.threads,
            "retries": validated_plan.retries,
            "network_lane_revision": revision,
            "tool": tool_receipt,
            "manifest": manifest_receipt,
            "output_inventory": inventory,
            "adoption": {
                "adopted": True,
                "mode": "offline-existing-mirror",
                "reason": normalized_reason,
                "operator_uid": os.geteuid(),
                "download_command_executed": False,
            },
            "integrity_limit": (
                "offline adoption validates the root-owned manifest, config, "
                "tool and revision plus exact path-and-size inventory and PAR1 "
                "framing; it does not prove transfer provenance or hash complete "
                "Parquet object contents"
            ),
        }
        receipt_path = paths.receipts / (
            f"common-crawl-mirror-{invocation_id}.json"
        )
        _atomic_write_json(receipt_path, completed)
        stamp = {
            **completed,
            "receipt_path": str(receipt_path),
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        }
        _atomic_write_json(paths.mirror_completed, stamp)
        return 0


def run_mirror(
    plan: MirrorPlan,
    *,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    downloader_path: Path = DOWNLOADER_PATH,
    expected_downloader_uid: int = 0,
    expected_manifest_uid: int = 0,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    minimum_free_bytes: int = MIN_MIRROR_FREE_BYTES,
    revision_path: Path = BUNDLE_REVISION_PATH,
    expected_revision_uid: int = 0,
) -> int:
    command = [
        str(downloader_path),
        "download",
        "--threads",
        str(plan.threads),
        "--retries",
        str(plan.retries),
        str(plan.manifest_path),
        str(plan.mirror_root),
    ]

    disk_receipt: dict[str, int] = {}
    expected_objects: tuple[str, ...] = ()

    def metadata() -> Mapping[str, Any]:
        nonlocal expected_objects
        free_before = int(disk_usage(plan.volume_root).free)
        if minimum_free_bytes < MIN_MIRROR_FREE_BYTES:
            raise LaneConfigurationError(
                "mirror free-space floor cannot be less than 256 GiB"
            )
        if free_before < minimum_free_bytes:
            raise LaneConfigurationError(
                "Common Crawl volume has less than the required 256 GiB free"
            )
        disk_receipt["disk_free_bytes_before"] = free_before
        manifest_receipt, expected_objects = _inspect_manifest_with_paths(
            plan, expected_manifest_uid=expected_manifest_uid
        )
        return {
            "crawl": plan.crawl,
            "config_path": str(plan.config_path),
            "volume_root": str(plan.volume_root),
            "mirror_root": str(plan.mirror_root),
            "threads": plan.threads,
            "retries": plan.retries,
            "disk_free_bytes_before": free_before,
            "minimum_free_bytes": minimum_free_bytes,
            "network_lane_revision": inspect_runtime_revision(
                path=revision_path, expected_uid=expected_revision_uid
            ),
            "tool": inspect_downloader(
                plan.downloader_sha256,
                path=downloader_path,
                expected_uid=expected_downloader_uid,
            ),
            "manifest": manifest_receipt,
            "integrity_limit": (
                "cc-downloader/path-manifest validates scope and transfer completion; "
                "per-object content hashes are not supplied by this upstream flow"
            ),
        }

    def completion_metadata(outcome: ChildOutcome) -> CompletionMetadata:
        if "disk_free_bytes_before" not in disk_receipt:
            raise LaneConfigurationError("mirror disk preflight receipt is missing")
        fields: dict[str, Any] = {}
        try:
            fields["disk_free_bytes_after"] = int(disk_usage(plan.volume_root).free)
        except OSError as exc:
            fields["disk_free_bytes_after"] = None
            fields["disk_free_bytes_after_error"] = type(exc).__name__
        inventory = inspect_mirror_inventory(plan, expected_objects)
        fields["output_inventory"] = inventory
        failure_status = None
        if not inventory["valid"] and outcome.exit_status == 0:
            failure_status = EXIT_CONFIG
            fields["completion_validation_error"] = "output-inventory-mismatch"
        return CompletionMetadata(fields=fields, failure_status=failure_status)

    return execute_guarded_job(
        state_dir=state_dir,
        job_kind="common-crawl-mirror",
        command=command,
        metadata_factory=metadata,
        completion_metadata_factory=completion_metadata,
    )


def run_bleedthrough(
    *,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    prober_path: Path = BLEEDTHROUGH_PROBER,
    quiet_seconds: int = MIN_MIRROR_QUIET_SECONDS,
    revision_path: Path = BUNDLE_REVISION_PATH,
    expected_revision_uid: int = 0,
) -> int:
    descriptor, _ = _secure_regular_file(
        prober_path,
        label="BLEEDTHROUGH prober",
        max_bytes=1024 * 1024,
        expected_uid=expected_revision_uid,
        executable=True,
    )
    try:
        prober_sha256 = _sha256_fd(descriptor)
    finally:
        os.close(descriptor)
    return execute_guarded_job(
        state_dir=state_dir,
        job_kind="bleedthrough",
        command=[str(prober_path)],
        metadata_factory=lambda: {
            "network_lane_revision": inspect_runtime_revision(
                path=revision_path, expected_uid=expected_revision_uid
            ),
            "prober": {
                "path": str(prober_path),
                "sha256": prober_sha256,
            },
        },
        mirror_quiet_seconds=quiet_seconds,
    )


def reconcile_orphan(
    *,
    state_dir: Path | str,
    expected_invocation_id: str,
    reason: str,
    now_ns: Callable[[], int] = time.time_ns,
) -> dict[str, Any]:
    """Archive one exact orphan; a mirror orphan restarts the 15-minute clock."""

    if INVOCATION_RE.fullmatch(expected_invocation_id) is None:
        raise LaneConfigurationError("expected invocation id must be 32 lowercase hex")
    normalized_reason = _normalize_operator_reason(
        reason, operation="reconciliation"
    )
    paths = LanePaths.from_root(state_dir)
    with ExitStack() as locks:
        locks.enter_context(_exclusive_lane(paths))
        marker = _read_state_json(paths.active, orphan=True)
        if marker is None:
            raise LaneConfigurationError("there is no orphan active marker")
        if marker.get("invocation_id") != expected_invocation_id:
            raise LaneTemporaryError("active marker changed; refusing reconciliation")
        if marker.get("job_kind") not in {"common-crawl-mirror", "bleedthrough"}:
            raise LaneTemporaryError("active marker has an unknown job kind")
        if marker["job_kind"] == "common-crawl-mirror":
            # Canonical order matches a live mirror: network lane first, then
            # dataset. A filter reader can never have its snapshot invalidated
            # by reconciliation while it is using the guarded files.
            locks.enter_context(_exclusive_dataset(paths))
        reconciled_ns = now_ns()
        receipt = {
            "schema_version": RECONCILIATION_SCHEMA,
            "state": "orphan-reconciled",
            "invocation_id": expected_invocation_id,
            "job_kind": marker["job_kind"],
            "reconciled_at": _timestamp(reconciled_ns),
            "reconciled_unix_ns": reconciled_ns,
            "reason": normalized_reason,
            "original_active_marker": marker,
        }
        archive = paths.receipts / f"reconciled-{expected_invocation_id}.json"
        _atomic_write_json(archive, receipt)
        if marker["job_kind"] == "common-crawl-mirror":
            quiet_stamp = {
                **marker,
                "schema_version": RECEIPT_SCHEMA,
                "state": "orphan-reconciled",
                "completed_at": _timestamp(reconciled_ns),
                "completed_unix_ns": reconciled_ns,
                "exit_status": None,
                "received_signal": None,
                "spawn_error": "orphan-reconciled",
                "receipt_path": str(archive),
                "receipt_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
            _atomic_write_json(paths.mirror_completed, quiet_stamp)
        paths.active.unlink()
        _fsync_directory(paths.active.parent)
        return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    commands = parser.add_subparsers(dest="command", required=True)

    mirror = commands.add_parser(
        "mirror", help="run one reviewed cc-index-table mirror"
    )
    mirror.add_argument("--crawl", required=True)
    mirror.add_argument("--config", type=Path, required=True)

    adopt = commands.add_parser(
        "adopt-mirror",
        help="validate and receipt an already populated mirror without downloading",
    )
    adopt.add_argument("--crawl", required=True)
    adopt.add_argument("--config", type=Path, required=True)
    adopt.add_argument("--reason", required=True)

    bleed = commands.add_parser(
        "bleedthrough", help="run the fixed BLEEDTHROUGH prober"
    )
    bleed.add_argument("--quiet-seconds", type=int, default=MIN_MIRROR_QUIET_SECONDS)

    reconcile = commands.add_parser(
        "reconcile", help="archive one exact orphan marker after operator review"
    )
    reconcile.add_argument("--expected-invocation-id", required=True)
    reconcile.add_argument("--reason", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "mirror":
            plan = load_mirror_plan(args.config, args.crawl)
            return run_mirror(plan, state_dir=args.state_dir)
        if args.command == "adopt-mirror":
            if os.geteuid() != 0:
                raise LaneConfigurationError("mirror adoption must run as root")
            plan = load_mirror_plan(args.config, args.crawl)
            return adopt_mirror(
                plan,
                state_dir=args.state_dir,
                reason=args.reason,
            )
        if args.command == "bleedthrough":
            return run_bleedthrough(
                state_dir=args.state_dir, quiet_seconds=args.quiet_seconds
            )
        if os.geteuid() != 0:
            raise LaneConfigurationError("orphan reconciliation must run as root")
        reconcile_orphan(
            state_dir=args.state_dir,
            expected_invocation_id=args.expected_invocation_id,
            reason=args.reason,
        )
        return 0
    except LaneTemporaryError as exc:
        print(f"network lane temporarily refused: {exc}", file=sys.stderr)
        return EXIT_TEMPFAIL
    except LaneConfigurationError as exc:
        print(f"network lane configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except OSError as exc:
        print(f"network lane I/O error: {exc}", file=sys.stderr)
        return EXIT_IOERR


if __name__ == "__main__":
    raise SystemExit(main())
