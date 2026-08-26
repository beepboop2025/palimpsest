#!/usr/bin/env python3
"""Fail-closed verification for one Palimpsest node-backup snapshot."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
from typing import BinaryIO


SCHEMA = "palimpsest-node-backup-verification.v1"
SNAPSHOT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z")
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9._+-]{1,255}")
CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)")
COMPOSE_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")

BASE_HASHED_FILES = (
    "postgres.dump",
    "postgres.list",
    "artifacts.tar.gz",
    "artifacts.list",
    "MANIFEST.txt",
)
CENSORWATCH_HASHED_FILES = (
    "censorwatch-postgres.dump",
    "censorwatch-postgres.list",
    "censorwatch-redis.tar.gz",
    "censorwatch-redis.list",
)
HASHED_FILES_BY_MODE = {
    "absent": BASE_HASHED_FILES,
    "included": (
        *BASE_HASHED_FILES[:-1],
        *CENSORWATCH_HASHED_FILES,
        "MANIFEST.txt",
    ),
}
SNAPSHOT_FILES_BY_MODE = {
    mode: frozenset({"SHA256SUMS", *hashed_files})
    for mode, hashed_files in HASHED_FILES_BY_MODE.items()
}
ALL_SNAPSHOT_FILES = frozenset().union(*SNAPSHOT_FILES_BY_MODE.values())
# Compatibility alias for callers that build the always-valid absent fixture.
SNAPSHOT_FILES = SNAPSHOT_FILES_BY_MODE["absent"]
HASHED_FILES = BASE_HASHED_FILES
MANIFEST_FIELDS = frozenset(
    {
        "format_version",
        "snapshot_id",
        "created_at_utc",
        "host",
        "compose_project",
        "postgres_version",
        "artifact_roots",
        "censorwatch_mode",
        "censorwatch_postgres_version",
        "censorwatch_redis_version",
        "censorwatch_writer_fence",
        "contents",
    }
)
ARTIFACT_ROOTS = ("readings", "data", "newswire", "analysis", "witness")
ARTIFACT_ROOT_SET = frozenset(ARTIFACT_ROOTS)
CONTENTS_BY_MODE = {
    mode: ",".join(name for name in hashed_files if name != "MANIFEST.txt")
    for mode, hashed_files in HASHED_FILES_BY_MODE.items()
}
CENSORWATCH_WRITER_FENCE = (
    "beat-velocity-data,beat-velocity-control,worker-velocity,worker-velocity-control"
)
WITNESS_UID = 1001
WITNESS_GID = 1001
WITNESS_HISTORY_FILES = frozenset(
    {
        "witness/erasure-ledger.witness.jsonl",
        "witness/eval-registry.witness.jsonl",
    }
)
WITNESS_FRESHNESS_FILE = "witness/public-freshness-state.json"
WITNESS_MEMBERS = frozenset(
    {"witness/", *WITNESS_HISTORY_FILES, WITNESS_FRESHNESS_FILE}
)
WITNESS_FRESHNESS_SCHEMA = "palimpsest-public-freshness-state.v1"
LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}")
WITNESS_CONDITION = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}")
WITNESS_STATE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

MAX_MANIFEST_BYTES = 64 * 1024
MAX_CHECKSUM_BYTES = 8 * 1024
MAX_ARTIFACT_LIST_BYTES = 256 * 1024 * 1024
MAX_ARTIFACT_MEMBERS = 1_100_000
MAX_ARTIFACT_DEPTH = 65
MAX_ARTIFACT_LIST_LINE_BYTES = 4098
MAX_REDIS_LIST_BYTES = 16 * 1024 * 1024
MAX_REDIS_MEMBERS = 100_000
MAX_REDIS_UNCOMPRESSED_BYTES = 384 * 1024 * 1024
MAX_REDIS_MEMBER_BYTES = 384 * 1024 * 1024
MAX_REDIS_MANIFEST_BYTES = 1024 * 1024
MAX_WITNESS_HISTORY_BYTES = 64 * 1024 * 1024
MAX_WITNESS_HISTORY_RECORDS = 1_000_000
MAX_WITNESS_HISTORY_LINE_BYTES = 4096
MAX_WITNESS_FRESHNESS_BYTES = 64 * 1024
READ_CHUNK_BYTES = 1024 * 1024
OUTER_SCHEMA = "palimpsest-node-backup-outer-archive.v1"
PACK_SCHEMA = "palimpsest-node-backup-pack.v1"
VERSION_VALUE = re.compile(r"[0-9][A-Za-z0-9.+_-]{0,63}")
REDIS_AOF_FILE = re.compile(
    r"appendonly\.aof(?:\.manifest|\.[0-9]+\.(?:base\.rdb|incr\.aof))"
)


class VerificationError(RuntimeError):
    """The snapshot does not satisfy the node-backup restore contract."""


def _metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _validate_snapshot_id(value: str) -> None:
    if not SNAPSHOT_ID.fullmatch(value):
        raise VerificationError("snapshot id is malformed")
    try:
        datetime.strptime(value, "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise VerificationError("snapshot id is not a real UTC timestamp") from exc


def _validate_directory_metadata(
    metadata: os.stat_result,
    *,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise VerificationError("snapshot path is not a real directory")
    if not scratch_restore and (
        metadata.st_uid != expected_uid
        or metadata.st_gid != expected_gid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise VerificationError("snapshot directory ownership or mode is invalid")


def _validate_file_metadata(
    name: str,
    metadata: os.stat_result,
    *,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise VerificationError(
            f"snapshot entry is not a single-link regular file: {name}"
        )
    if not scratch_restore and (
        metadata.st_uid != expected_uid
        or metadata.st_gid != expected_gid
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise VerificationError(f"snapshot file ownership or mode is invalid: {name}")


def _open_snapshot_directory(
    path: Path,
    *,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, os.stat_result]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise VerificationError("host lacks required no-follow directory support")
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise VerificationError("snapshot directory cannot be opened safely") from exc
    descriptor_metadata = os.fstat(descriptor)
    try:
        _validate_directory_metadata(
            descriptor_metadata,
            scratch_restore=scratch_restore,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        if not _same_identity(path_metadata, descriptor_metadata):
            raise VerificationError("snapshot directory identity changed")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, descriptor_metadata


def _inspect_snapshot_files(
    directory_descriptor: int,
    *,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> dict[str, os.stat_result]:
    try:
        names = os.listdir(directory_descriptor)
    except OSError as exc:
        raise VerificationError("snapshot inventory cannot be read") from exc
    name_set = frozenset(names)
    if (
        len(names) != len(name_set)
        or name_set not in SNAPSHOT_FILES_BY_MODE.values()
    ):
        raise VerificationError("snapshot file inventory is not exact")

    metadata_by_name: dict[str, os.stat_result] = {}
    for name in sorted(name_set):
        try:
            metadata = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise VerificationError(
                f"snapshot entry cannot be inspected: {name}"
            ) from exc
        _validate_file_metadata(
            name,
            metadata,
            scratch_restore=scratch_restore,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        metadata_by_name[name] = metadata
    return metadata_by_name


def _open_regular_file(
    directory_descriptor: int,
    name: str,
    expected_metadata: os.stat_result,
    *,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise VerificationError(
            f"snapshot file cannot be opened safely: {name}"
        ) from exc
    opened_metadata = os.fstat(descriptor)
    try:
        _validate_file_metadata(
            name,
            opened_metadata,
            scratch_restore=scratch_restore,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        path_metadata = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not (
            _same_identity(expected_metadata, opened_metadata)
            and _same_identity(opened_metadata, path_metadata)
        ):
            raise VerificationError(f"snapshot file identity changed: {name}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened_metadata


def _validate_file_unchanged(
    directory_descriptor: int,
    name: str,
    descriptor: int,
    opened_metadata: os.stat_result,
) -> None:
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise VerificationError(
            f"snapshot file identity cannot be rechecked: {name}"
        ) from exc
    if _metadata_signature(descriptor_metadata) != _metadata_signature(
        opened_metadata
    ) or _metadata_signature(path_metadata) != _metadata_signature(opened_metadata):
        raise VerificationError(f"snapshot file changed during verification: {name}")


def _read_regular_file(
    directory_descriptor: int,
    name: str,
    expected_metadata: os.stat_result,
    *,
    maximum_bytes: int,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> bytes:
    descriptor, opened_metadata = _open_regular_file(
        directory_descriptor,
        name,
        expected_metadata,
        scratch_restore=scratch_restore,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    payload = bytearray()
    try:
        while True:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, maximum_bytes + 1))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                raise VerificationError(f"snapshot metadata file is too large: {name}")
        _validate_file_unchanged(
            directory_descriptor,
            name,
            descriptor,
            opened_metadata,
        )
    finally:
        os.close(descriptor)
    return bytes(payload)


def _sha256_regular_file(
    directory_descriptor: int,
    name: str,
    expected_metadata: os.stat_result,
    *,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> str:
    descriptor, opened_metadata = _open_regular_file(
        directory_descriptor,
        name,
        expected_metadata,
        scratch_restore=scratch_restore,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, READ_CHUNK_BYTES):
            digest.update(chunk)
        _validate_file_unchanged(
            directory_descriptor,
            name,
            descriptor,
            opened_metadata,
        )
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _decode_strict_text(payload: bytes, *, label: str) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"{label} is not valid UTF-8") from exc
    if not text.endswith("\n") or "\r" in text or "\x00" in text:
        raise VerificationError(f"{label} is not canonical line-oriented text")
    return text


def _parse_manifest(payload: bytes, *, snapshot_id: str) -> dict[str, str]:
    text = _decode_strict_text(payload, label="manifest")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            raise VerificationError("manifest line is malformed")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[a-z_]+", key) or key in fields or not value:
            raise VerificationError("manifest fields are malformed or duplicated")
        if value != value.strip() or not value.isprintable():
            raise VerificationError("manifest contains an unsafe value")
        fields[key] = value

    if set(fields) != MANIFEST_FIELDS:
        raise VerificationError("manifest field inventory is not exact")
    if fields["format_version"] != "5":
        raise VerificationError("manifest format version is not 5")
    if fields["snapshot_id"] != snapshot_id:
        raise VerificationError("manifest snapshot id does not match")
    if fields["artifact_roots"] != ",".join(ARTIFACT_ROOTS):
        raise VerificationError("manifest artifact roots are not exact")
    mode = fields["censorwatch_mode"]
    if mode not in SNAPSHOT_FILES_BY_MODE:
        raise VerificationError("manifest CensorWatch mode is not exact")
    if fields["contents"] != CONTENTS_BY_MODE[mode]:
        raise VerificationError("manifest contents are not exact")
    if mode == "absent":
        if (
            fields["censorwatch_postgres_version"] != "absent"
            or fields["censorwatch_redis_version"] != "absent"
            or fields["censorwatch_writer_fence"] != "not-applicable"
        ):
            raise VerificationError("absent CensorWatch manifest is inconsistent")
    elif (
        VERSION_VALUE.fullmatch(fields["censorwatch_postgres_version"]) is None
        or VERSION_VALUE.fullmatch(fields["censorwatch_redis_version"]) is None
        or not fields["censorwatch_postgres_version"].startswith("16.")
        or not fields["censorwatch_redis_version"].startswith("7.")
        or fields["censorwatch_writer_fence"] != CENSORWATCH_WRITER_FENCE
    ):
        raise VerificationError("included CensorWatch manifest is inconsistent")
    if not HOST.fullmatch(fields["host"]):
        raise VerificationError("manifest host is malformed")
    if not COMPOSE_PROJECT.fullmatch(fields["compose_project"]):
        raise VerificationError("manifest Compose project is malformed")
    try:
        datetime.strptime(fields["created_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise VerificationError("manifest creation time is malformed") from exc
    return fields


def _parse_checksums(
    payload: bytes,
    *,
    hashed_files: tuple[str, ...],
) -> dict[str, str]:
    text = _decode_strict_text(payload, label="checksum manifest")
    checksums: dict[str, str] = {}
    observed_names: list[str] = []
    for line in text.splitlines():
        match = CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise VerificationError("checksum manifest line is malformed")
        digest, name = match.groups()
        if name in checksums:
            raise VerificationError("checksum manifest contains a duplicate entry")
        checksums[name] = digest
        observed_names.append(name)
    if tuple(observed_names) != hashed_files:
        raise VerificationError("checksum manifest inventory is not exact")
    return checksums


def _read_artifact_listing(
    directory_descriptor: int,
    expected_metadata: os.stat_result,
    *,
    expected_digest: str,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> tuple[str, ...]:
    name = "artifacts.list"
    descriptor, opened_metadata = _open_regular_file(
        directory_descriptor,
        name,
        expected_metadata,
        scratch_restore=scratch_restore,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    digest = hashlib.sha256()
    lines: list[str] = []
    total_bytes = 0
    source: BinaryIO | None = None
    try:
        source = os.fdopen(descriptor, "rb", closefd=False)
        while True:
            line = source.readline(MAX_ARTIFACT_LIST_LINE_BYTES)
            if not line:
                break
            total_bytes += len(line)
            if total_bytes > MAX_ARTIFACT_LIST_BYTES:
                raise VerificationError("artifact listing exceeds its size bound")
            digest.update(line)
            if not line.endswith(b"\n") or b"\r" in line or b"\x00" in line:
                raise VerificationError("artifact listing is not canonical text")
            try:
                decoded = line[:-1].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise VerificationError("artifact listing is not valid UTF-8") from exc
            if not decoded:
                raise VerificationError("artifact listing contains an empty entry")
            lines.append(decoded)
            if len(lines) > MAX_ARTIFACT_MEMBERS:
                raise VerificationError("artifact listing exceeds its entry bound")
        _validate_file_unchanged(
            directory_descriptor,
            name,
            descriptor,
            opened_metadata,
        )
    finally:
        if source is not None:
            source.close()
        os.close(descriptor)
    if not lines:
        raise VerificationError("artifact listing is empty")
    if digest.hexdigest() != expected_digest:
        raise VerificationError("artifact listing fails its checksum")
    return tuple(lines)


def _canonical_artifact_member(member: tarfile.TarInfo) -> str:
    name = member.name.rstrip("/")
    if (
        not name
        or member.name.startswith("/")
        or "\\" in member.name
        or "\x00" in member.name
        or "//" in member.name
    ):
        raise VerificationError("artifact archive contains an unsafe path")
    parts = name.split("/")
    if (
        len(parts) > MAX_ARTIFACT_DEPTH
        or parts[0] not in ARTIFACT_ROOT_SET
        or any(
            component in {"", ".", ".."} or SAFE_COMPONENT.fullmatch(component) is None
            for component in parts
        )
    ):
        raise VerificationError("artifact archive contains an unsafe path")
    if member.isdir():
        return f"{name}/"
    if not member.isreg() or member.issparse():
        raise VerificationError("artifact archive contains a link or special member")
    return name


def _strict_json_object(payload: bytes, *, label: str) -> dict[str, object]:
    if not payload.endswith(b"\n") or b"\r" in payload or b"\0" in payload:
        raise VerificationError(f"{label} is not canonical UTF-8 JSON")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise VerificationError(f"{label} contains duplicate JSON fields")
            value[key] = item
        return value

    try:
        decoded = json.loads(payload, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise VerificationError(f"{label} is not a JSON object")
    return decoded


def _validate_witness_history_payload(payload: bytes) -> int:
    if (
        not payload
        or len(payload) > MAX_WITNESS_HISTORY_BYTES
        or not payload.endswith(b"\n")
        or b"\r" in payload
        or b"\0" in payload
    ):
        raise VerificationError("witness history is empty, oversized, or non-canonical")
    lines = payload.splitlines(keepends=True)
    if len(lines) > MAX_WITNESS_HISTORY_RECORDS:
        raise VerificationError("witness history exceeds its record bound")
    for line in lines:
        if len(line) > MAX_WITNESS_HISTORY_LINE_BYTES:
            raise VerificationError("witness history contains an overlong record")
        record = _strict_json_object(line, label="witness history record")
        if set(record) != {"ts", "n", "head", "root", "alerts"}:
            raise VerificationError("witness history record fields are not exact")
        timestamp = record["ts"]
        if not isinstance(timestamp, str) or len(timestamp) > 64:
            raise VerificationError("witness history timestamp is malformed")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise VerificationError("witness history timestamp is malformed") from exc
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            raise VerificationError("witness history timestamp lacks a timezone")
        for field in ("n", "alerts"):
            value = record[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VerificationError(
                    f"witness history {field} is not a non-negative integer"
                )
        for field in ("head", "root"):
            value = record[field]
            if not isinstance(value, str) or LOWER_HEX_64.fullmatch(value) is None:
                raise VerificationError(f"witness history {field} is malformed")
    return len(lines)


def _validate_witness_freshness_payload(payload: bytes) -> None:
    if not payload or len(payload) > MAX_WITNESS_FRESHNESS_BYTES:
        raise VerificationError("witness freshness state is empty or oversized")
    document = _strict_json_object(payload, label="witness freshness state")
    if (
        set(document) != {"schema_version", "conditions"}
        or document.get("schema_version") != WITNESS_FRESHNESS_SCHEMA
        or not isinstance(document.get("conditions"), dict)
        or len(document["conditions"]) > 128
    ):
        raise VerificationError("witness freshness state contract is invalid")
    for condition, state_name in document["conditions"].items():
        if (
            not isinstance(condition, str)
            or WITNESS_CONDITION.fullmatch(condition) is None
            or not isinstance(state_name, str)
            or WITNESS_STATE.fullmatch(state_name) is None
        ):
            raise VerificationError("witness freshness condition is malformed")


def _inspect_witness_archive_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    display_name: str,
) -> int:
    """Validate one witness member and return its history-record contribution."""

    mode = stat.S_IMODE(member.mode)
    if member.uid != WITNESS_UID or member.gid != WITNESS_GID:
        raise VerificationError("witness archive ownership metadata is invalid")
    if display_name == "witness/":
        if not member.isdir() or mode not in {0o700, 0o750, 0o755}:
            raise VerificationError("witness archive directory mode is invalid")
        return 0
    if not member.isreg() or member.issparse():
        raise VerificationError("witness archive contains a non-regular state file")
    if display_name in WITNESS_HISTORY_FILES:
        if mode not in {0o600, 0o640, 0o644}:
            raise VerificationError("witness archive history mode is invalid")
        if member.size <= 0 or member.size > MAX_WITNESS_HISTORY_BYTES:
            raise VerificationError("witness archive history size is invalid")
    elif display_name == WITNESS_FRESHNESS_FILE:
        if (
            mode != 0o600
            or member.size <= 0
            or member.size > MAX_WITNESS_FRESHNESS_BYTES
        ):
            raise VerificationError("witness freshness archive metadata is invalid")
    else:
        raise VerificationError("witness archive inventory is not exact")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise VerificationError("witness archive payload cannot be read")
    payload = extracted.read(member.size + 1)
    if len(payload) != member.size:
        raise VerificationError("witness archive payload is truncated")
    if display_name in WITNESS_HISTORY_FILES:
        return _validate_witness_history_payload(payload)
    _validate_witness_freshness_payload(payload)
    return 0


def _inspect_artifact_archive(
    directory_descriptor: int,
    expected_metadata: os.stat_result,
    *,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> tuple[tuple[str, ...], int, int, int]:
    name = "artifacts.tar.gz"
    descriptor, opened_metadata = _open_regular_file(
        directory_descriptor,
        name,
        expected_metadata,
        scratch_restore=scratch_restore,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    source: BinaryIO | None = None
    members: list[str] = []
    seen: set[str] = set()
    root_directories: set[str] = set()
    witness_members: set[str] = set()
    witness_history_records = 0
    files = 0
    directories = 0
    try:
        source = os.fdopen(descriptor, "rb", closefd=False)
        try:
            with tarfile.open(fileobj=source, mode="r:gz") as archive:
                if archive.pax_headers:
                    raise VerificationError(
                        "artifact archive contains global PAX metadata"
                    )
                for member in archive:
                    if member.pax_headers:
                        raise VerificationError(
                            "artifact archive contains PAX metadata"
                        )
                    display_name = _canonical_artifact_member(member)
                    if display_name in seen:
                        raise VerificationError(
                            "artifact archive contains a duplicate member"
                        )
                    seen.add(display_name)
                    members.append(display_name)
                    if display_name == "witness/" or display_name.startswith(
                        "witness/"
                    ):
                        witness_members.add(display_name)
                        witness_history_records += _inspect_witness_archive_member(
                            archive,
                            member,
                            display_name,
                        )
                    if len(members) > MAX_ARTIFACT_MEMBERS:
                        raise VerificationError(
                            "artifact archive exceeds its entry bound"
                        )
                    if member.isdir():
                        directories += 1
                        if member.name.rstrip("/") in ARTIFACT_ROOT_SET:
                            root_directories.add(member.name.rstrip("/"))
                    else:
                        files += 1
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise VerificationError(
                "artifact archive is corrupt or unreadable"
            ) from exc
        _validate_file_unchanged(
            directory_descriptor,
            name,
            descriptor,
            opened_metadata,
        )
    finally:
        if source is not None:
            source.close()
        os.close(descriptor)
    if not members or root_directories != ARTIFACT_ROOT_SET:
        raise VerificationError("artifact archive root inventory is not exact")
    if witness_members != WITNESS_MEMBERS:
        raise VerificationError("witness archive inventory is not exact")
    return tuple(members), files, directories, witness_history_records


def _read_open_descriptor(
    descriptor: int,
    name: str,
    opened_metadata: os.stat_result,
    *,
    maximum_bytes: int,
) -> bytes:
    payload = bytearray()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, min(READ_CHUNK_BYTES, maximum_bytes + 1))
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > maximum_bytes:
            raise VerificationError(f"snapshot metadata file is too large: {name}")
    if _metadata_signature(os.fstat(descriptor)) != _metadata_signature(
        opened_metadata
    ):
        raise VerificationError(f"snapshot file changed during verification: {name}")
    return bytes(payload)


def _sha256_open_descriptor(
    descriptor: int,
    name: str,
    opened_metadata: os.stat_result,
) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, READ_CHUNK_BYTES):
        digest.update(chunk)
    if _metadata_signature(os.fstat(descriptor)) != _metadata_signature(
        opened_metadata
    ):
        raise VerificationError(f"snapshot file changed during verification: {name}")
    return digest.hexdigest()


def _parse_artifact_listing_payload(
    payload: bytes,
    *,
    expected_digest: str,
) -> tuple[str, ...]:
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise VerificationError("artifact listing fails its checksum")
    text = _decode_strict_text(payload, label="artifact listing")
    lines = tuple(text.splitlines())
    if not lines or any(not line for line in lines):
        raise VerificationError("artifact listing is empty or contains an empty entry")
    if len(lines) > MAX_ARTIFACT_MEMBERS:
        raise VerificationError("artifact listing exceeds its entry bound")
    if any(len(line.encode("utf-8")) >= MAX_ARTIFACT_LIST_LINE_BYTES for line in lines):
        raise VerificationError("artifact listing contains an overlong entry")
    return lines


def _inspect_artifact_open_descriptor(
    descriptor: int,
    opened_metadata: os.stat_result,
) -> tuple[tuple[str, ...], int, int, int]:
    source = os.fdopen(os.dup(descriptor), "rb")
    members: list[str] = []
    seen: set[str] = set()
    root_directories: set[str] = set()
    witness_members: set[str] = set()
    witness_history_records = 0
    files = 0
    directories = 0
    try:
        source.seek(0)
        try:
            with tarfile.open(fileobj=source, mode="r:gz") as archive:
                for member in archive:
                    display_name = _canonical_artifact_member(member)
                    if display_name in seen:
                        raise VerificationError(
                            "artifact archive contains a duplicate member"
                        )
                    seen.add(display_name)
                    members.append(display_name)
                    if display_name == "witness/" or display_name.startswith(
                        "witness/"
                    ):
                        witness_members.add(display_name)
                        witness_history_records += _inspect_witness_archive_member(
                            archive,
                            member,
                            display_name,
                        )
                    if len(members) > MAX_ARTIFACT_MEMBERS:
                        raise VerificationError(
                            "artifact archive exceeds its entry bound"
                        )
                    if member.isdir():
                        directories += 1
                        root_name = member.name.rstrip("/")
                        if root_name in ARTIFACT_ROOT_SET:
                            root_directories.add(root_name)
                    else:
                        files += 1
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise VerificationError(
                "artifact archive is corrupt or unreadable"
            ) from exc
    finally:
        source.close()
    if _metadata_signature(os.fstat(descriptor)) != _metadata_signature(
        opened_metadata
    ):
        raise VerificationError("artifact archive changed during inspection")
    if not members or root_directories != ARTIFACT_ROOT_SET:
        raise VerificationError("artifact archive root inventory is not exact")
    if witness_members != WITNESS_MEMBERS:
        raise VerificationError("witness archive inventory is not exact")
    return tuple(members), files, directories, witness_history_records


def _parse_redis_listing_payload(
    payload: bytes,
    *,
    expected_digest: str,
) -> tuple[str, ...]:
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise VerificationError("CensorWatch Redis listing fails its checksum")
    text = _decode_strict_text(payload, label="CensorWatch Redis listing")
    lines = tuple(text.splitlines())
    if (
        not lines
        or len(lines) > MAX_REDIS_MEMBERS
        or any(not line or len(line.encode("utf-8")) >= MAX_ARTIFACT_LIST_LINE_BYTES for line in lines)
    ):
        raise VerificationError("CensorWatch Redis listing is invalid")
    return lines


def _canonical_redis_member(member: tarfile.TarInfo) -> str:
    name = member.name.rstrip("/")
    if (
        not name
        or member.name.startswith("/")
        or "\\" in member.name
        or "\x00" in member.name
        or "//" in member.name
    ):
        raise VerificationError("CensorWatch Redis archive contains an unsafe path")
    parts = name.split("/")
    if (
        len(parts) > 3
        or parts[0] != "redis"
        or any(
            component in {"", ".", ".."}
            or SAFE_COMPONENT.fullmatch(component) is None
            for component in parts
        )
    ):
        raise VerificationError("CensorWatch Redis archive contains an unsafe path")
    if member.isdir():
        if name not in {"redis", "redis/appendonlydir"}:
            raise VerificationError(
                "CensorWatch Redis archive directory inventory is not exact"
            )
        return f"{name}/"
    if not member.isreg() or member.issparse():
        raise VerificationError(
            "CensorWatch Redis archive contains a link or special member"
        )
    relative = name.removeprefix("redis/")
    if relative != "dump.rdb" and not (
        relative.startswith("appendonlydir/")
        and REDIS_AOF_FILE.fullmatch(relative.removeprefix("appendonlydir/"))
    ):
        raise VerificationError(
            "CensorWatch Redis archive contains non-state or secret-bearing bytes"
        )
    if member.size < 0 or member.size > MAX_REDIS_MEMBER_BYTES:
        raise VerificationError("CensorWatch Redis archive member is oversized")
    return name


def _validate_redis_aof_manifest(
    payload: bytes,
    *,
    archived_files: set[str],
) -> None:
    text = _decode_strict_text(payload, label="CensorWatch Redis AOF manifest")
    referenced: set[str] = set()
    for line in text.splitlines():
        match = re.fullmatch(
            r"file (appendonly\.aof\.[0-9]+\.(?:base\.rdb|incr\.aof)) "
            r"seq [0-9]+ type [bi]",
            line,
        )
        if match is None or match.group(1) in referenced:
            raise VerificationError("CensorWatch Redis AOF manifest is malformed")
        referenced.add(match.group(1))
    archived_aof = {
        name.removeprefix("redis/appendonlydir/")
        for name in archived_files
        if name.startswith("redis/appendonlydir/")
        and name != "redis/appendonlydir/appendonly.aof.manifest"
    }
    if not referenced or referenced != archived_aof:
        raise VerificationError(
            "CensorWatch Redis AOF manifest does not match the cold archive"
        )


def _inspect_redis_open_descriptor(
    descriptor: int,
    opened_metadata: os.stat_result,
) -> tuple[tuple[str, ...], int]:
    source = os.fdopen(os.dup(descriptor), "rb")
    members: list[str] = []
    seen: set[str] = set()
    archived_files: set[str] = set()
    aof_manifest: bytes | None = None
    total_bytes = 0
    try:
        source.seek(0)
        try:
            with tarfile.open(fileobj=source, mode="r:gz") as archive:
                if archive.pax_headers:
                    raise VerificationError(
                        "CensorWatch Redis archive contains global PAX metadata"
                    )
                for member in archive:
                    if member.pax_headers:
                        raise VerificationError(
                            "CensorWatch Redis archive contains PAX metadata"
                        )
                    display_name = _canonical_redis_member(member)
                    if display_name in seen:
                        raise VerificationError(
                            "CensorWatch Redis archive contains a duplicate member"
                        )
                    seen.add(display_name)
                    members.append(display_name)
                    if len(members) > MAX_REDIS_MEMBERS:
                        raise VerificationError(
                            "CensorWatch Redis archive exceeds its entry bound"
                        )
                    if not member.isdir():
                        archived_files.add(display_name)
                        total_bytes += member.size
                        if total_bytes > MAX_REDIS_UNCOMPRESSED_BYTES:
                            raise VerificationError(
                                "CensorWatch Redis archive exceeds its byte bound"
                            )
                        if display_name == (
                            "redis/appendonlydir/appendonly.aof.manifest"
                        ):
                            if member.size > MAX_REDIS_MANIFEST_BYTES:
                                raise VerificationError(
                                    "CensorWatch Redis AOF manifest is oversized"
                                )
                            extracted = archive.extractfile(member)
                            if extracted is None:
                                raise VerificationError(
                                    "CensorWatch Redis AOF manifest cannot be read"
                                )
                            aof_manifest = extracted.read(member.size + 1)
                            if len(aof_manifest) != member.size:
                                raise VerificationError(
                                    "CensorWatch Redis AOF manifest is truncated"
                                )
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise VerificationError(
                "CensorWatch Redis archive is corrupt or unreadable"
            ) from exc
    finally:
        source.close()
    if _metadata_signature(os.fstat(descriptor)) != _metadata_signature(
        opened_metadata
    ):
        raise VerificationError("CensorWatch Redis archive changed during inspection")
    if (
        "redis/" not in seen
        or "redis/appendonlydir/" not in seen
        or aof_manifest is None
        or not archived_files
    ):
        raise VerificationError("CensorWatch Redis archive inventory is incomplete")
    _validate_redis_aof_manifest(aof_manifest, archived_files=archived_files)
    return tuple(members), total_bytes


def _require_postgres_dump(
    descriptor: int,
    metadata: os.stat_result,
    *,
    label: str,
) -> None:
    if metadata.st_size <= 5 or os.pread(descriptor, 5, 0) != b"PGDMP":
        raise VerificationError(f"{label} is empty or lacks custom-format framing")


def _open_all_snapshot_files(
    directory_descriptor: int,
    metadata_by_name: dict[str, os.stat_result],
    *,
    scratch_restore: bool,
    expected_uid: int,
    expected_gid: int,
) -> tuple[dict[str, int], dict[str, os.stat_result]]:
    descriptors: dict[str, int] = {}
    opened_metadata: dict[str, os.stat_result] = {}
    try:
        for name in sorted(metadata_by_name):
            descriptor, metadata = _open_regular_file(
                directory_descriptor,
                name,
                metadata_by_name[name],
                scratch_restore=scratch_restore,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
            )
            descriptors[name] = descriptor
            opened_metadata[name] = metadata
    except Exception:
        for descriptor in descriptors.values():
            os.close(descriptor)
        raise
    return descriptors, opened_metadata


def _verify_open_snapshot(
    directory_descriptor: int,
    directory_metadata: os.stat_result,
    file_descriptors: dict[str, int],
    opened_metadata: dict[str, os.stat_result],
    *,
    snapshot_id: str,
) -> dict[str, object]:
    manifest_payload = _read_open_descriptor(
        file_descriptors["MANIFEST.txt"],
        "MANIFEST.txt",
        opened_metadata["MANIFEST.txt"],
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    manifest = _parse_manifest(manifest_payload, snapshot_id=snapshot_id)
    censorwatch_mode = manifest["censorwatch_mode"]
    snapshot_files = SNAPSHOT_FILES_BY_MODE[censorwatch_mode]
    hashed_files = HASHED_FILES_BY_MODE[censorwatch_mode]
    if set(file_descriptors) != snapshot_files or set(opened_metadata) != snapshot_files:
        raise VerificationError(
            "snapshot inventory does not match the declared CensorWatch mode"
        )
    checksum_payload = _read_open_descriptor(
        file_descriptors["SHA256SUMS"],
        "SHA256SUMS",
        opened_metadata["SHA256SUMS"],
        maximum_bytes=MAX_CHECKSUM_BYTES,
    )
    expected_digests = _parse_checksums(
        checksum_payload,
        hashed_files=hashed_files,
    )

    actual_digests: dict[str, str] = {}
    for name in hashed_files:
        actual = _sha256_open_descriptor(
            file_descriptors[name], name, opened_metadata[name]
        )
        if actual != expected_digests[name]:
            raise VerificationError(f"snapshot file fails its checksum: {name}")
        actual_digests[name] = actual

    _require_postgres_dump(
        file_descriptors["postgres.dump"],
        opened_metadata["postgres.dump"],
        label="PostgreSQL dump",
    )
    if opened_metadata["postgres.list"].st_size <= 0:
        raise VerificationError("PostgreSQL listing is empty")

    redis_members: tuple[str, ...] = ()
    redis_uncompressed_bytes = 0
    if censorwatch_mode == "included":
        _require_postgres_dump(
            file_descriptors["censorwatch-postgres.dump"],
            opened_metadata["censorwatch-postgres.dump"],
            label="CensorWatch PostgreSQL dump",
        )
        if opened_metadata["censorwatch-postgres.list"].st_size <= 0:
            raise VerificationError("CensorWatch PostgreSQL listing is empty")
        redis_listing_payload = _read_open_descriptor(
            file_descriptors["censorwatch-redis.list"],
            "censorwatch-redis.list",
            opened_metadata["censorwatch-redis.list"],
            maximum_bytes=MAX_REDIS_LIST_BYTES,
        )
        redis_listing = _parse_redis_listing_payload(
            redis_listing_payload,
            expected_digest=actual_digests["censorwatch-redis.list"],
        )
        redis_members, redis_uncompressed_bytes = _inspect_redis_open_descriptor(
            file_descriptors["censorwatch-redis.tar.gz"],
            opened_metadata["censorwatch-redis.tar.gz"],
        )
        redis_digest_after_inspection = _sha256_open_descriptor(
            file_descriptors["censorwatch-redis.tar.gz"],
            "censorwatch-redis.tar.gz",
            opened_metadata["censorwatch-redis.tar.gz"],
        )
        if redis_digest_after_inspection != actual_digests[
            "censorwatch-redis.tar.gz"
        ]:
            raise VerificationError(
                "CensorWatch Redis archive changed during inspection"
            )
        if redis_listing != redis_members:
            raise VerificationError(
                "CensorWatch Redis archive does not match its listing"
            )

    artifact_listing_payload = _read_open_descriptor(
        file_descriptors["artifacts.list"],
        "artifacts.list",
        opened_metadata["artifacts.list"],
        maximum_bytes=MAX_ARTIFACT_LIST_BYTES,
    )
    artifact_listing = _parse_artifact_listing_payload(
        artifact_listing_payload,
        expected_digest=actual_digests["artifacts.list"],
    )
    (
        archive_members,
        artifact_files,
        artifact_directories,
        witness_history_records,
    ) = _inspect_artifact_open_descriptor(
        file_descriptors["artifacts.tar.gz"],
        opened_metadata["artifacts.tar.gz"],
    )
    archive_digest_after_inspection = _sha256_open_descriptor(
        file_descriptors["artifacts.tar.gz"],
        "artifacts.tar.gz",
        opened_metadata["artifacts.tar.gz"],
    )
    if archive_digest_after_inspection != actual_digests["artifacts.tar.gz"]:
        raise VerificationError("artifact archive changed during inspection")
    if artifact_listing != archive_members:
        raise VerificationError("artifact archive does not match artifacts.list")

    for name in sorted(snapshot_files):
        _validate_file_unchanged(
            directory_descriptor,
            name,
            file_descriptors[name],
            opened_metadata[name],
        )
    current_directory_metadata = os.fstat(directory_descriptor)
    if _metadata_signature(current_directory_metadata) != _metadata_signature(
        directory_metadata
    ):
        raise VerificationError("snapshot directory changed during verification")
    if set(os.listdir(directory_descriptor)) != snapshot_files:
        raise VerificationError("snapshot file inventory changed during verification")

    return {
        "censorwatch": {
            "mode": censorwatch_mode,
            "postgres_dump_bytes": (
                opened_metadata["censorwatch-postgres.dump"].st_size
                if censorwatch_mode == "included"
                else 0
            ),
            "redis_archive_bytes": (
                opened_metadata["censorwatch-redis.tar.gz"].st_size
                if censorwatch_mode == "included"
                else 0
            ),
            "redis_members": len(redis_members),
            "redis_uncompressed_bytes": redis_uncompressed_bytes,
        },
        "counts": {
            "artifact_directories": artifact_directories,
            "artifact_files": artifact_files,
            "artifact_members": len(archive_members),
            "checksum_entries": len(actual_digests),
            "snapshot_files": len(snapshot_files),
            "witness_history_records": witness_history_records,
        },
        "digests": dict(sorted(actual_digests.items())),
        "schema": SCHEMA,
        "snapshot": snapshot_id,
        "status": "verified",
        "format_version": 5,
    }


def verify_snapshot(
    snapshot_dir: str | os.PathLike[str],
    *,
    snapshot_id: str,
    scratch_restore: bool = False,
    expected_uid: int = 1001,
    expected_gid: int = 1001,
) -> dict[str, object]:
    """Verify one snapshot without extracting or executing any of its bytes."""

    _validate_snapshot_id(snapshot_id)
    if expected_uid < 0 or expected_gid < 0:
        raise VerificationError("expected uid and gid must be non-negative")
    snapshot_path = Path(snapshot_dir)
    if snapshot_path.name != snapshot_id:
        raise VerificationError("snapshot directory basename does not match its id")

    directory_descriptor, directory_metadata = _open_snapshot_directory(
        snapshot_path,
        scratch_restore=scratch_restore,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    file_descriptors: dict[str, int] = {}
    try:
        metadata_by_name = _inspect_snapshot_files(
            directory_descriptor,
            scratch_restore=scratch_restore,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        file_descriptors, opened_metadata = _open_all_snapshot_files(
            directory_descriptor,
            metadata_by_name,
            scratch_restore=scratch_restore,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        return _verify_open_snapshot(
            directory_descriptor,
            directory_metadata,
            file_descriptors,
            opened_metadata,
            snapshot_id=snapshot_id,
        )
    finally:
        for descriptor in file_descriptors.values():
            os.close(descriptor)
        os.close(directory_descriptor)


class _HashingReader:
    """Hash the exact descriptor bytes handed to TarFile.addfile()."""

    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        chunk = self.source.read(size)
        self.digest.update(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self.digest.hexdigest()


def _write_outer_archive(
    output_descriptor: int,
    *,
    snapshot_id: str,
    directory_descriptor: int,
    directory_metadata: os.stat_result,
    file_descriptors: dict[str, int],
    opened_metadata: dict[str, os.stat_result],
    expected_digests: dict[str, str],
) -> None:
    os.lseek(output_descriptor, 0, os.SEEK_SET)
    destination = os.fdopen(os.dup(output_descriptor), "wb")
    try:
        with tarfile.open(
            fileobj=destination,
            mode="w",
            format=tarfile.USTAR_FORMAT,
            dereference=False,
        ) as archive:
            root = tarfile.TarInfo(snapshot_id)
            root.type = tarfile.DIRTYPE
            root.mode = stat.S_IMODE(directory_metadata.st_mode)
            root.uid = directory_metadata.st_uid
            root.gid = directory_metadata.st_gid
            root.mtime = int(directory_metadata.st_mtime)
            root.size = 0
            archive.addfile(root)

            for name in sorted(file_descriptors):
                metadata = opened_metadata[name]
                member = tarfile.TarInfo(f"{snapshot_id}/{name}")
                member.type = tarfile.REGTYPE
                member.mode = stat.S_IMODE(metadata.st_mode)
                member.uid = metadata.st_uid
                member.gid = metadata.st_gid
                member.mtime = int(metadata.st_mtime)
                member.size = metadata.st_size
                os.lseek(file_descriptors[name], 0, os.SEEK_SET)
                source = os.fdopen(os.dup(file_descriptors[name]), "rb")
                reader = _HashingReader(source)
                try:
                    archive.addfile(member, reader)
                finally:
                    source.close()
                if reader.hexdigest() != expected_digests[name]:
                    raise VerificationError(
                        f"snapshot file changed while it was packed: {name}"
                    )
        destination.flush()
        os.fsync(destination.fileno())
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise VerificationError("outer snapshot archive could not be written") from exc
    finally:
        destination.close()

    snapshot_files = frozenset(file_descriptors)
    for name in sorted(snapshot_files):
        _validate_file_unchanged(
            directory_descriptor,
            name,
            file_descriptors[name],
            opened_metadata[name],
        )
    if _metadata_signature(os.fstat(directory_descriptor)) != _metadata_signature(
        directory_metadata
    ):
        raise VerificationError("snapshot directory changed while it was packed")
    if set(os.listdir(directory_descriptor)) != snapshot_files:
        raise VerificationError("snapshot inventory changed while it was packed")


def _inspect_outer_descriptor(
    descriptor: int,
    opened_metadata: os.stat_result,
    *,
    snapshot_id: str,
) -> tuple[dict[str, str], int]:
    seen: set[str] = set()
    payload_digests: dict[str, str] = {}
    manifest_payload: bytes | None = None
    owner: tuple[int, int] | None = None
    end_offset = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    source = os.fdopen(os.dup(descriptor), "rb")
    try:
        try:
            with tarfile.open(fileobj=source, mode="r:") as archive:
                if archive.pax_headers:
                    raise VerificationError(
                        "outer archive contains global PAX metadata"
                    )
                for member in archive:
                    if len(seen) > len(ALL_SNAPSHOT_FILES):
                        raise VerificationError("outer archive inventory is not exact")
                    safe_child = member.name.removeprefix(f"{snapshot_id}/")
                    if (
                        member.name.startswith("/")
                        or "\\" in member.name
                        or "\x00" in member.name
                        or "//" in member.name
                        or member.name in seen
                        or (
                            member.name != snapshot_id
                            and (
                                not member.name.startswith(f"{snapshot_id}/")
                                or safe_child not in ALL_SNAPSHOT_FILES
                            )
                        )
                        or member.pax_headers
                    ):
                        raise VerificationError(
                            "outer archive contains an unsafe or duplicate member"
                        )
                    seen.add(member.name)
                    if member.uid < 0 or member.gid < 0 or member.uname or member.gname:
                        raise VerificationError(
                            "outer archive ownership metadata is not numeric"
                        )
                    current_owner = (member.uid, member.gid)
                    if owner is None:
                        owner = current_owner
                    elif current_owner != owner:
                        raise VerificationError(
                            "outer archive ownership metadata is inconsistent"
                        )
                    if member.name == snapshot_id:
                        if (
                            not member.isdir()
                            or member.size != 0
                            or stat.S_IMODE(member.mode) != 0o700
                        ):
                            raise VerificationError(
                                "outer archive top directory contract is invalid"
                            )
                        continue
                    if (
                        not member.isreg()
                        or member.issparse()
                        or stat.S_IMODE(member.mode) != 0o600
                    ):
                        raise VerificationError(
                            "outer archive contains a link or special member"
                        )
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise VerificationError("outer archive member cannot be read")
                    digest = hashlib.sha256()
                    bytes_read = 0
                    manifest_bytes = bytearray()
                    while chunk := extracted.read(READ_CHUNK_BYTES):
                        digest.update(chunk)
                        bytes_read += len(chunk)
                        if safe_child == "MANIFEST.txt":
                            manifest_bytes.extend(chunk)
                            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                                raise VerificationError(
                                    "outer archive manifest is oversized"
                                )
                    if bytes_read != member.size:
                        raise VerificationError("outer archive member is truncated")
                    payload_digests[safe_child] = digest.hexdigest()
                    if safe_child == "MANIFEST.txt":
                        manifest_payload = bytes(manifest_bytes)
                end_offset = archive.offset
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise VerificationError("outer archive is corrupt or unreadable") from exc
    finally:
        source.close()

    snapshot_files = frozenset(payload_digests)
    if (
        manifest_payload is None
        or snapshot_files not in SNAPSHOT_FILES_BY_MODE.values()
        or seen != {
            snapshot_id,
            *(f"{snapshot_id}/{name}" for name in snapshot_files),
        }
    ):
        raise VerificationError("outer archive inventory is not exact")
    manifest = _parse_manifest(manifest_payload, snapshot_id=snapshot_id)
    if snapshot_files != SNAPSHOT_FILES_BY_MODE[manifest["censorwatch_mode"]]:
        raise VerificationError(
            "outer archive inventory does not match its CensorWatch mode"
        )
    os.lseek(descriptor, end_offset, os.SEEK_SET)
    trailing_bytes = bytearray()
    while chunk := os.read(descriptor, READ_CHUNK_BYTES):
        trailing_bytes.extend(chunk)
    if (
        len(trailing_bytes) < 1024
        or opened_metadata.st_size % tarfile.BLOCKSIZE != 0
        or any(trailing_bytes)
    ):
        raise VerificationError("outer archive has invalid trailing data")
    if _metadata_signature(os.fstat(descriptor)) != _metadata_signature(
        opened_metadata
    ):
        raise VerificationError("outer archive changed during inspection")
    return dict(sorted(payload_digests.items())), len(seen)


def _open_outer_archive(path: Path) -> tuple[int, os.stat_result]:
    if not hasattr(os, "O_NOFOLLOW"):
        raise VerificationError("host lacks required no-follow file support")
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise VerificationError("outer archive cannot be opened safely") from exc
    descriptor_metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(descriptor_metadata.st_mode)
        or descriptor_metadata.st_nlink != 1
        or not _same_identity(path_metadata, descriptor_metadata)
    ):
        os.close(descriptor)
        raise VerificationError("outer archive is not a single-link regular file")
    return descriptor, descriptor_metadata


def inspect_outer_archive(
    archive_path: str | os.PathLike[str],
    *,
    snapshot_id: str,
) -> dict[str, object]:
    """Validate the non-extracted transport tar around a verified snapshot."""

    _validate_snapshot_id(snapshot_id)
    descriptor, opened_metadata = _open_outer_archive(Path(archive_path))
    try:
        payload_digests, members = _inspect_outer_descriptor(
            descriptor,
            opened_metadata,
            snapshot_id=snapshot_id,
        )
        archive_sha256 = _sha256_open_descriptor(
            descriptor, "outer archive", opened_metadata
        )
    finally:
        os.close(descriptor)
    return {
        "archive_bytes": opened_metadata.st_size,
        "archive_sha256": archive_sha256,
        "counts": {"members": members, "snapshot_files": len(payload_digests)},
        "censorwatch_mode": (
            "included"
            if frozenset(payload_digests) == SNAPSHOT_FILES_BY_MODE["included"]
            else "absent"
        ),
        "digests": payload_digests,
        "schema": OUTER_SCHEMA,
        "snapshot": snapshot_id,
        "status": "verified",
    }


def _open_output_archive(path: Path) -> tuple[int, int, os.stat_result]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise VerificationError("output archive path must be an absolute file path")
    parent = path.parent
    try:
        parent_path_metadata = os.stat(parent, follow_symlinks=False)
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise VerificationError(
            "output archive parent cannot be opened safely"
        ) from exc
    parent_descriptor_metadata = os.fstat(parent_descriptor)
    if not stat.S_ISDIR(parent_descriptor_metadata.st_mode) or not _same_identity(
        parent_path_metadata, parent_descriptor_metadata
    ):
        os.close(parent_descriptor)
        raise VerificationError("output archive parent is not a real directory")
    try:
        descriptor = os.open(
            path.name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, 0o600)
    except OSError as exc:
        os.close(parent_descriptor)
        raise VerificationError(
            "output archive already exists or cannot be created"
        ) from exc
    metadata = os.fstat(descriptor)
    return parent_descriptor, descriptor, metadata


def _remove_created_output(
    parent_descriptor: int,
    descriptor: int,
    name: str,
) -> None:
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _same_identity(descriptor_metadata, path_metadata):
            os.unlink(name, dir_fd=parent_descriptor)
    except OSError:
        pass


def pack_snapshot(
    snapshot_dir: str | os.PathLike[str],
    *,
    snapshot_id: str,
    output: str | os.PathLike[str],
    expected_uid: int = 1001,
    expected_gid: int = 1001,
) -> dict[str, object]:
    """Verify and pack the same already-open snapshot file descriptors."""

    _validate_snapshot_id(snapshot_id)
    if expected_uid < 0 or expected_gid < 0:
        raise VerificationError("expected uid and gid must be non-negative")
    snapshot_path = Path(snapshot_dir)
    if snapshot_path.name != snapshot_id:
        raise VerificationError("snapshot directory basename does not match its id")
    output_path = Path(output)
    if output_path.parent == snapshot_path:
        raise VerificationError("output archive cannot be created inside the snapshot")

    directory_descriptor, directory_metadata = _open_snapshot_directory(
        snapshot_path,
        scratch_restore=False,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    file_descriptors: dict[str, int] = {}
    parent_descriptor: int | None = None
    output_descriptor: int | None = None
    output_created = False
    try:
        metadata_by_name = _inspect_snapshot_files(
            directory_descriptor,
            scratch_restore=False,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        file_descriptors, opened_metadata = _open_all_snapshot_files(
            directory_descriptor,
            metadata_by_name,
            scratch_restore=False,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        verification = _verify_open_snapshot(
            directory_descriptor,
            directory_metadata,
            file_descriptors,
            opened_metadata,
            snapshot_id=snapshot_id,
        )
        expected_digests = dict(verification["digests"])
        expected_digests["SHA256SUMS"] = _sha256_open_descriptor(
            file_descriptors["SHA256SUMS"],
            "SHA256SUMS",
            opened_metadata["SHA256SUMS"],
        )

        parent_descriptor, output_descriptor, _ = _open_output_archive(output_path)
        output_created = True
        _write_outer_archive(
            output_descriptor,
            snapshot_id=snapshot_id,
            directory_descriptor=directory_descriptor,
            directory_metadata=directory_metadata,
            file_descriptors=file_descriptors,
            opened_metadata=opened_metadata,
            expected_digests=expected_digests,
        )
        output_metadata = os.fstat(output_descriptor)
        payload_digests, members = _inspect_outer_descriptor(
            output_descriptor,
            output_metadata,
            snapshot_id=snapshot_id,
        )
        archive_sha256 = _sha256_open_descriptor(
            output_descriptor, "outer archive", output_metadata
        )
        path_metadata = os.stat(
            output_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_identity(output_metadata, path_metadata):
            raise VerificationError("output archive identity changed")
        # _write_outer_archive fsyncs the file. Persist its directory entry too
        # before returning a receipt that callers may treat as durable.
        os.fsync(parent_descriptor)
        return {
            "archive_bytes": output_metadata.st_size,
            "archive_sha256": archive_sha256,
            "counts": {
                "members": members,
                "snapshot_files": len(file_descriptors),
            },
            "censorwatch_mode": verification["censorwatch"]["mode"],
            "digests": payload_digests,
            "schema": PACK_SCHEMA,
            "snapshot": snapshot_id,
            "status": "packed",
        }
    except Exception:
        if (
            output_created
            and parent_descriptor is not None
            and output_descriptor is not None
        ):
            _remove_created_output(
                parent_descriptor,
                output_descriptor,
                output_path.name,
            )
        raise
    finally:
        if output_descriptor is not None:
            os.close(output_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        for descriptor in file_descriptors.values():
            os.close(descriptor)
        os.close(directory_descriptor)


def _nonnegative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a Palimpsest node-backup snapshot without extracting it."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("snapshot_dir")
    verify.add_argument("--snapshot-id", required=True)
    verify.add_argument("--scratch-restore", action="store_true")
    verify.add_argument("--expected-uid", type=_nonnegative_integer, default=1001)
    verify.add_argument("--expected-gid", type=_nonnegative_integer, default=1001)
    pack = subparsers.add_parser("pack")
    pack.add_argument("snapshot_dir")
    pack.add_argument("--snapshot-id", required=True)
    pack.add_argument("--output", required=True)
    pack.add_argument("--expected-uid", type=_nonnegative_integer, default=1001)
    pack.add_argument("--expected-gid", type=_nonnegative_integer, default=1001)
    inspect_outer = subparsers.add_parser("inspect-outer")
    inspect_outer.add_argument("archive_path")
    inspect_outer.add_argument("--snapshot-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_snapshot(
                args.snapshot_dir,
                snapshot_id=args.snapshot_id,
                scratch_restore=args.scratch_restore,
                expected_uid=args.expected_uid,
                expected_gid=args.expected_gid,
            )
        elif args.command == "pack":
            result = pack_snapshot(
                args.snapshot_dir,
                snapshot_id=args.snapshot_id,
                output=args.output,
                expected_uid=args.expected_uid,
                expected_gid=args.expected_gid,
            )
        else:
            result = inspect_outer_archive(
                args.archive_path,
                snapshot_id=args.snapshot_id,
            )
    except VerificationError as exc:
        print(f"node backup verification failed: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - no traceback at this privileged boundary
        print("node backup verification failed unexpectedly", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
