#!/usr/bin/env python3
"""Stream the fixed node artifact archive under the analysis cascade lock."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import sys
import tarfile
from typing import BinaryIO, Callable


LOCK_PATH = "/source/analysis/private/cascade.lock"
NEWSWIRE_LOCK_PATH = "/source/newswire/newswire.lock"
ANALYSIS_ROOT = "/source/analysis"
RUNS_ROOT = f"{ANALYSIS_ROOT}/runs"
PRIVATE_ROOT = f"{ANALYSIS_ROOT}/private"
ROOT_UID = 0
ROOT_GID = 0
RUNTIME_UID = 10001
RUNTIME_GID = 10001
NEWSWIRE_UID = 1001
NEWSWIRE_GID = 1001
MAX_RUNS = 48
MAX_ANALYSIS_ENTRIES = 32768
MAX_ANALYSIS_DEPTH = 8
MAX_ARTIFACT_ENTRIES = 1_000_000
MAX_ARTIFACT_DEPTH = 64
_RUN_NAME = re.compile(r"run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
_ANALYSIS_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
# Live readings/data contain dot-prefixed coordination files and ISO timestamps
# with a plus sign. They are safe tar path components; traversal separators and
# the special dot components remain forbidden separately.
_ARTIFACT_SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]{1,255}")
SOURCE_ROOT = "/source"
ARCHIVE_ROOTS = ("readings", "data", "analysis", "newswire")
ARCHIVE_WRITE_ORDER = ("analysis", "readings", "data", "newswire")
NEWSWIRE_FILES = (
    "newswire-latest.json",
    "newswire-status.json",
    "newswire-versions.jsonl",
    "newswire.lock",
)
NEWSWIRE_STATUS_MAX_BYTES = 16 * 1024
NEWSWIRE_STATUS_SCHEMA = "palimpsest-evidence-wire-attempt.v1"

_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)
_REGULAR_OPEN_FLAGS = (
    os.O_RDONLY
    | os.O_NOFOLLOW
    | os.O_NONBLOCK
    | getattr(os, "O_CLOEXEC", 0)
)
_LOCK_OPEN_FLAGS = _REGULAR_OPEN_FLAGS


class ArchivePreflightError(RuntimeError):
    """The fixed private-analysis archive boundary is not trustworthy."""


class _DescriptorArchiveReader:
    """Hash, and optionally capture, the exact descriptor bytes tar consumes."""

    def __init__(self, source: BinaryIO, *, capture_limit: int | None = None) -> None:
        self._source = source
        self._capture_limit = capture_limit
        self._captured = bytearray()
        self._digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(size)
        self._digest.update(chunk)
        if self._capture_limit is not None:
            if len(self._captured) + len(chunk) > self._capture_limit:
                raise ArchivePreflightError("newswire status exceeds its size bound")
            self._captured.extend(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._digest.hexdigest()

    def captured(self) -> bytes:
        return bytes(self._captured)


def _validate_lock(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != RUNTIME_UID
        or metadata.st_gid != RUNTIME_GID
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ArchivePreflightError("analysis cascade lock contract is invalid")


def _lstat_lock_path() -> os.stat_result:
    return os.stat(LOCK_PATH, follow_symlinks=False)


def _validate_lock_path(descriptor: int) -> None:
    descriptor_metadata = os.fstat(descriptor)
    _validate_lock(descriptor_metadata)
    path_metadata = _lstat_lock_path()
    _validate_lock(path_metadata)
    if (
        descriptor_metadata.st_dev != path_metadata.st_dev
        or descriptor_metadata.st_ino != path_metadata.st_ino
    ):
        raise ArchivePreflightError("analysis cascade lock identity changed")


def _validate_newswire_lock(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != NEWSWIRE_UID
        or metadata.st_gid != NEWSWIRE_GID
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ArchivePreflightError("newswire generation lock contract is invalid")


def _lstat_newswire_lock_path() -> os.stat_result:
    return os.stat(NEWSWIRE_LOCK_PATH, follow_symlinks=False)


def _validate_newswire_lock_path(descriptor: int) -> None:
    descriptor_metadata = os.fstat(descriptor)
    _validate_newswire_lock(descriptor_metadata)
    path_metadata = _lstat_newswire_lock_path()
    _validate_newswire_lock(path_metadata)
    if (
        descriptor_metadata.st_dev != path_metadata.st_dev
        or descriptor_metadata.st_ino != path_metadata.st_ino
    ):
        raise ArchivePreflightError("newswire generation lock identity changed")


def _validate_directory(
    metadata: os.stat_result, *, uid: int, gid: int, mode: int
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise ArchivePreflightError("analysis directory contract is invalid")


def _entry_signature(relative_path: str, metadata: os.stat_result) -> tuple[object, ...]:
    return (
        relative_path,
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


def _scan_branch(
    *,
    branch_root: str,
    directory_uid: int,
    directory_gid: int,
    directory_mode: int,
    file_uid: int,
    file_gid: int,
    file_mode: int,
) -> list[tuple[object, ...]]:
    signatures: list[tuple[object, ...]] = []
    pending = [(branch_root, 1)]
    while pending:
        directory, depth = pending.pop()
        if depth > MAX_ANALYSIS_DEPTH:
            raise ArchivePreflightError("analysis tree exceeds its depth bound")
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise ArchivePreflightError("analysis tree cannot be inspected") from exc
        with entries:
            for entry in entries:
                if not _ANALYSIS_SAFE_NAME.fullmatch(entry.name):
                    raise ArchivePreflightError(
                        "analysis tree entry name is unsafe"
                    )
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ArchivePreflightError(
                        "analysis tree entry cannot be inspected"
                    ) from exc
                relative = os.path.relpath(entry.path, ANALYSIS_ROOT)
                signatures.append(_entry_signature(relative, metadata))
                if len(signatures) > MAX_ANALYSIS_ENTRIES:
                    raise ArchivePreflightError(
                        "analysis tree exceeds its entry bound"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    _validate_directory(
                        metadata,
                        uid=directory_uid,
                        gid=directory_gid,
                        mode=directory_mode,
                    )
                    pending.append((entry.path, depth + 1))
                elif stat.S_ISREG(metadata.st_mode):
                    if (
                        metadata.st_nlink != 1
                        or metadata.st_uid != file_uid
                        or metadata.st_gid != file_gid
                        or stat.S_IMODE(metadata.st_mode) != file_mode
                    ):
                        raise ArchivePreflightError(
                            "analysis file ownership or mode is invalid"
                        )
                else:
                    raise ArchivePreflightError(
                        "analysis tree contains a link or special file"
                    )
    return signatures


def _validate_analysis_tree() -> tuple[tuple[object, ...], ...]:
    try:
        root_metadata = os.stat(ANALYSIS_ROOT, follow_symlinks=False)
        root_entries = {entry.name: entry for entry in os.scandir(ANALYSIS_ROOT)}
    except OSError as exc:
        raise ArchivePreflightError("analysis root cannot be inspected") from exc
    _validate_directory(
        root_metadata, uid=ROOT_UID, gid=ROOT_GID, mode=0o711
    )
    if set(root_entries) != {"runs", "private"}:
        raise ArchivePreflightError("analysis root inventory is not exact")

    runs_metadata = root_entries["runs"].stat(follow_symlinks=False)
    private_metadata = root_entries["private"].stat(follow_symlinks=False)
    _validate_directory(
        runs_metadata, uid=ROOT_UID, gid=RUNTIME_GID, mode=0o710
    )
    _validate_directory(
        private_metadata, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o700
    )
    signatures = [
        _entry_signature(".", root_metadata),
        _entry_signature("runs", runs_metadata),
        _entry_signature("private", private_metadata),
    ]

    try:
        run_entries = list(os.scandir(RUNS_ROOT))
    except OSError as exc:
        raise ArchivePreflightError("analysis runs cannot be inspected") from exc
    if len(run_entries) > MAX_RUNS:
        raise ArchivePreflightError("analysis run retention bound is exceeded")
    for run_entry in run_entries:
        if not _RUN_NAME.fullmatch(run_entry.name):
            raise ArchivePreflightError(
                "analysis runs contain a staging or malformed directory"
            )
        run_metadata = run_entry.stat(follow_symlinks=False)
        _validate_directory(
            run_metadata, uid=ROOT_UID, gid=RUNTIME_GID, mode=0o750
        )
        relative = f"runs/{run_entry.name}"
        signatures.append(_entry_signature(relative, run_metadata))
        signatures.extend(
            _scan_branch(
                branch_root=run_entry.path,
                directory_uid=ROOT_UID,
                directory_gid=RUNTIME_GID,
                directory_mode=0o750,
                file_uid=ROOT_UID,
                file_gid=RUNTIME_GID,
                file_mode=0o640,
            )
        )

    signatures.extend(
        _scan_branch(
            branch_root=PRIVATE_ROOT,
            directory_uid=RUNTIME_UID,
            directory_gid=RUNTIME_GID,
            directory_mode=0o700,
            file_uid=RUNTIME_UID,
            file_gid=RUNTIME_GID,
            file_mode=0o600,
        )
    )
    if len(signatures) > MAX_ANALYSIS_ENTRIES:
        raise ArchivePreflightError("analysis tree exceeds its entry bound")
    return tuple(sorted(signatures))


def _archive_filter(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """Reject restore-hostile member names and normalize numeric ownership."""

    path = PurePosixPath(member.name)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] not in ARCHIVE_ROOTS
        or any(part in {"", ".", ".."} for part in path.parts)
        or member.issym()
        or member.islnk()
        or member.isdev()
        or member.isfifo()
        or not (member.isdir() or member.isreg())
    ):
        raise ArchivePreflightError("artifact archive member is unsafe")
    # Numeric ownership is the recovery contract. Empty names prevent a
    # restoring tar implementation from resolving container-local accounts.
    member.uname = ""
    member.gname = ""
    return member


def _metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
    """Return the mutation-sensitive fields used for descriptor identity."""

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


def _validate_open_identity(
    *,
    parent_descriptor: int,
    name: str,
    descriptor: int,
    expected: os.stat_result,
) -> os.stat_result:
    """Prove an open descriptor still names the entry inspected before open."""

    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ArchivePreflightError("artifact entry identity cannot be verified") from exc
    expected_signature = _metadata_signature(expected)
    if (
        _metadata_signature(descriptor_metadata) != expected_signature
        or _metadata_signature(path_metadata) != expected_signature
    ):
        raise ArchivePreflightError("artifact entry identity changed")
    return descriptor_metadata


def _open_child_descriptor(
    parent_descriptor: int, name: str
) -> tuple[int, os.stat_result]:
    """Open one child relative to its parent without ever following a link."""

    try:
        expected = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISDIR(expected.st_mode):
            flags = _DIRECTORY_OPEN_FLAGS
        elif stat.S_ISREG(expected.st_mode):
            flags = _REGULAR_OPEN_FLAGS
        else:
            raise ArchivePreflightError(
                "artifact tree contains a link or special file"
            )
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except ArchivePreflightError:
        raise
    except OSError as exc:
        raise ArchivePreflightError("artifact entry cannot be opened safely") from exc
    try:
        metadata = _validate_open_identity(
            parent_descriptor=parent_descriptor,
            name=name,
            descriptor=descriptor,
            expected=expected,
        )
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise ArchivePreflightError("artifact tree contains a hard-linked file")
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _safe_directory_names(
    descriptor: int, *, root_name: str
) -> tuple[str, ...]:
    try:
        names = tuple(sorted(os.listdir(descriptor)))
    except OSError as exc:
        raise ArchivePreflightError("artifact directory cannot be enumerated") from exc
    pattern = _ANALYSIS_SAFE_NAME if root_name == "analysis" else _ARTIFACT_SAFE_NAME
    if any(
        name in {".", ".."} or not pattern.fullmatch(name)
        for name in names
    ):
        raise ArchivePreflightError("artifact tree entry name is unsafe")
    return names


def _validate_analysis_archive_entry(
    relative_parts: tuple[str, ...], metadata: os.stat_result
) -> None:
    """Reapply the private analysis ownership contract to opened objects."""

    if not relative_parts:
        _validate_directory(metadata, uid=ROOT_UID, gid=ROOT_GID, mode=0o711)
        return
    if relative_parts == ("runs",):
        _validate_directory(metadata, uid=ROOT_UID, gid=RUNTIME_GID, mode=0o710)
        return
    if relative_parts == ("private",):
        _validate_directory(
            metadata,
            uid=RUNTIME_UID,
            gid=RUNTIME_GID,
            mode=0o700,
        )
        return

    branch = relative_parts[0]
    if branch == "runs" and len(relative_parts) >= 2:
        if not _RUN_NAME.fullmatch(relative_parts[1]):
            raise ArchivePreflightError("analysis run name is invalid")
        if len(relative_parts) == 2:
            _validate_directory(
                metadata,
                uid=ROOT_UID,
                gid=RUNTIME_GID,
                mode=0o750,
            )
            return
        expected = (
            ROOT_UID,
            RUNTIME_GID,
            0o750 if stat.S_ISDIR(metadata.st_mode) else 0o640,
        )
    elif branch == "private":
        expected = (
            RUNTIME_UID,
            RUNTIME_GID,
            0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600,
        )
    else:
        raise ArchivePreflightError("analysis root inventory is not exact")

    uid, gid, mode = expected
    if (
        metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise ArchivePreflightError("analysis entry ownership or mode is invalid")


def _tar_info(
    member_name: str, metadata: os.stat_result
) -> tarfile.TarInfo:
    member = tarfile.TarInfo(member_name)
    member.mode = stat.S_IMODE(metadata.st_mode)
    member.uid = metadata.st_uid
    member.gid = metadata.st_gid
    member.mtime = metadata.st_mtime
    if stat.S_ISDIR(metadata.st_mode):
        member.type = tarfile.DIRTYPE
        member.size = 0
    elif stat.S_ISREG(metadata.st_mode):
        member.type = tarfile.REGTYPE
        member.size = metadata.st_size
    else:
        raise ArchivePreflightError("artifact tree contains a link or special file")
    return _archive_filter(member)


def _archive_open_tree(
    *,
    archive: tarfile.TarFile,
    root_name: str,
) -> None:
    """Walk one already-open root and add only descriptor-backed members."""

    entry_limit = (
        MAX_ANALYSIS_ENTRIES if root_name == "analysis" else MAX_ARTIFACT_ENTRIES
    )
    depth_limit = (
        MAX_ANALYSIS_DEPTH if root_name == "analysis" else MAX_ARTIFACT_DEPTH
    )
    entries_seen = 0
    newswire_latest_sha256: str | None = None
    newswire_status: dict[str, object] | None = None

    def visit(
        *,
        descriptor: int,
        metadata: os.stat_result,
        relative_parts: tuple[str, ...],
        parent_descriptor: int,
        entry_name: str,
        depth: int,
    ) -> None:
        nonlocal entries_seen, newswire_latest_sha256, newswire_status
        entries_seen += 1
        if entries_seen > entry_limit or depth > depth_limit:
            raise ArchivePreflightError("artifact tree exceeds its traversal bound")
        if root_name == "analysis":
            _validate_analysis_archive_entry(relative_parts, metadata)
        if root_name == "newswire" and relative_parts:
            if len(relative_parts) != 1 or relative_parts[0] not in NEWSWIRE_FILES:
                raise ArchivePreflightError("newswire root inventory is not exact")
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ArchivePreflightError("newswire recovery artifact is unsafe")
            if relative_parts == ("newswire.lock",):
                _validate_newswire_lock(metadata)

        member_name = "/".join((root_name, *relative_parts))
        member = _tar_info(member_name, metadata)
        if stat.S_ISREG(metadata.st_mode):
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                with os.fdopen(os.dup(descriptor), "rb", closefd=True) as source:
                    capture_limit = (
                        NEWSWIRE_STATUS_MAX_BYTES
                        if root_name == "newswire"
                        and relative_parts == ("newswire-status.json",)
                        else None
                    )
                    reader = _DescriptorArchiveReader(
                        source,
                        capture_limit=capture_limit,
                    )
                    archive.addfile(member, reader)
                    if root_name == "newswire":
                        if relative_parts == ("newswire-latest.json",):
                            newswire_latest_sha256 = reader.hexdigest()
                        elif relative_parts == ("newswire-status.json",):
                            try:
                                decoded = json.loads(reader.captured())
                            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                                raise ArchivePreflightError(
                                    "newswire status is invalid"
                                ) from exc
                            if not isinstance(decoded, dict):
                                raise ArchivePreflightError(
                                    "newswire status is invalid"
                                )
                            newswire_status = decoded
            except (OSError, tarfile.TarError) as exc:
                raise ArchivePreflightError("artifact file cannot be streamed") from exc
        else:
            archive.addfile(member)
            names_before = _safe_directory_names(descriptor, root_name=root_name)
            if root_name == "analysis" and not relative_parts:
                if set(names_before) != {"runs", "private"}:
                    raise ArchivePreflightError("analysis root inventory is not exact")
            if root_name == "analysis" and relative_parts == ("runs",):
                if len(names_before) > MAX_RUNS or any(
                    not _RUN_NAME.fullmatch(name) for name in names_before
                ):
                    raise ArchivePreflightError("analysis run inventory is invalid")
            if root_name == "newswire" and not relative_parts:
                if tuple(names_before) != NEWSWIRE_FILES:
                    raise ArchivePreflightError(
                        "newswire root inventory is not exact"
                    )
            for name in names_before:
                child_descriptor, child_metadata = _open_child_descriptor(
                    descriptor, name
                )
                try:
                    visit(
                        descriptor=child_descriptor,
                        metadata=child_metadata,
                        relative_parts=(*relative_parts, name),
                        parent_descriptor=descriptor,
                        entry_name=name,
                        depth=depth + 1,
                    )
                finally:
                    os.close(child_descriptor)
            if (
                _safe_directory_names(descriptor, root_name=root_name)
                != names_before
            ):
                raise ArchivePreflightError("artifact directory inventory changed")

        _validate_open_identity(
            parent_descriptor=parent_descriptor,
            name=entry_name,
            descriptor=descriptor,
            expected=metadata,
        )

    source_descriptor = os.open(SOURCE_ROOT, _DIRECTORY_OPEN_FLAGS)
    root_descriptor_for_close: int | None = None
    try:
        root_descriptor_for_close, opened_metadata = _open_child_descriptor(
            source_descriptor, root_name
        )
        if not stat.S_ISDIR(opened_metadata.st_mode):
            raise ArchivePreflightError("artifact root is not a directory")
        visit(
            descriptor=root_descriptor_for_close,
            metadata=opened_metadata,
            relative_parts=(),
            parent_descriptor=source_descriptor,
            entry_name=root_name,
            depth=0,
        )
        if root_name == "newswire":
            expected_status_fields = {
                "schema_version",
                "attempted_at",
                "completed_at",
                "status",
                "fresh_sources",
                "output_generated_at",
                "output_sha256",
                "failure_class",
            }
            if (
                newswire_latest_sha256 is None
                or newswire_status is None
                or set(newswire_status) != expected_status_fields
                or newswire_status.get("schema_version") != NEWSWIRE_STATUS_SCHEMA
                or newswire_status.get("status") == "running"
                or not isinstance(newswire_status.get("output_sha256"), str)
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(newswire_status["output_sha256"])
                )
                or newswire_status["output_sha256"] != newswire_latest_sha256
            ):
                raise ArchivePreflightError(
                    "newswire status does not bind the archived latest output"
                )
    except OSError as exc:
        raise ArchivePreflightError("artifact root cannot be opened safely") from exc
    finally:
        if root_descriptor_for_close is not None:
            os.close(root_descriptor_for_close)
        os.close(source_descriptor)


def _write_archive(
    fileobj: BinaryIO | None = None,
    *,
    analysis_complete: Callable[[], None] | None = None,
    newswire_begin: Callable[[], None] | None = None,
    newswire_complete: Callable[[], None] | None = None,
) -> None:
    """Stream fixed roots, allowing the analysis lease to end between roots."""

    destination = fileobj if fileobj is not None else sys.stdout.buffer
    try:
        with tarfile.open(
            fileobj=destination,
            mode="w|gz",
            format=tarfile.PAX_FORMAT,
            dereference=False,
        ) as archive:
            for root in ARCHIVE_WRITE_ORDER:
                if root == "newswire" and newswire_begin is not None:
                    newswire_begin()
                _archive_open_tree(
                    archive=archive,
                    root_name=root,
                )
                if root == "analysis" and analysis_complete is not None:
                    analysis_complete()
                if root == "newswire" and newswire_complete is not None:
                    newswire_complete()
    except (OSError, ValueError, tarfile.TarError, ArchivePreflightError) as exc:
        raise ArchivePreflightError("fixed artifact archive failed") from exc


def archive() -> None:
    """Hold the runner's shared lease only while streaming analysis."""

    descriptor: int | None = os.open(LOCK_PATH, _LOCK_OPEN_FLAGS)
    newswire_descriptor: int | None = None
    try:
        _validate_lock(os.fstat(descriptor))
        # Blocking is deliberate: an in-flight analytical promotion owns
        # LOCK_EX, so the backup waits for one coherent completed state.
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        _validate_lock_path(descriptor)
        tree_before = _validate_analysis_tree()

        def finish_analysis() -> None:
            nonlocal descriptor
            assert descriptor is not None
            _validate_lock_path(descriptor)
            if _validate_analysis_tree() != tree_before:
                raise ArchivePreflightError("analysis tree changed during archive")
            # Closing the descriptor releases LOCK_SH before the immutable and
            # independently produced readings/data roots are streamed.
            os.close(descriptor)
            descriptor = None

        def begin_newswire() -> None:
            nonlocal newswire_descriptor
            newswire_descriptor = os.open(NEWSWIRE_LOCK_PATH, _LOCK_OPEN_FLAGS)
            _validate_newswire_lock(os.fstat(newswire_descriptor))
            fcntl.flock(newswire_descriptor, fcntl.LOCK_SH)
            _validate_newswire_lock_path(newswire_descriptor)

        def finish_newswire() -> None:
            nonlocal newswire_descriptor
            assert newswire_descriptor is not None
            _validate_newswire_lock_path(newswire_descriptor)
            os.close(newswire_descriptor)
            newswire_descriptor = None

        _write_archive(
            analysis_complete=finish_analysis,
            newswire_begin=begin_newswire,
            newswire_complete=finish_newswire,
        )
    finally:
        if newswire_descriptor is not None:
            os.close(newswire_descriptor)
        if descriptor is not None:
            os.close(descriptor)


def main() -> int:
    try:
        archive()
    except Exception:  # noqa: BLE001 - this is the final privacy boundary
        # Never print exception detail: archive source paths and payload data are
        # private. Even an unexpected stdlib failure must not emit a traceback;
        # Docker logging is disabled as an additional safeguard.
        print("palimpsest artifact archive preflight failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
