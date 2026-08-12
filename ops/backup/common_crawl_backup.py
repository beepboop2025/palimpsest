#!/usr/bin/env python3
"""Create and validate a bounded private Common Crawl evidence snapshot.

The snapshot deliberately excludes the public Parquet mirror.  It contains the
consistent SQLite database, explicitly selected content-addressed WARC records,
and the small review/context/import state needed to reconstruct editorial work.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

UTC = timezone.utc
SCHEMA = "palimpsest-common-crawl-backup/v1"
DATABASE_NAME = "common-crawl.sqlite3"
WAREHOUSE_LOCK = ".common-crawl.lock"
MANIFEST_NAME = "MANIFEST.json"
CHECKSUM_NAME = "SHA256SUMS"
SNAPSHOT_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Durable state is opt-in.  A new top-level path fails the backup until its
# retention semantics are reviewed; that prevents future labels from silently
# falling outside this protection boundary.
INCLUDED_DIRECTORIES = frozenset(
    {
        "decisions",
        "derived",
        "inbox",
        "labels",
        "manifests",
        "receipts",
        "records",
        "reviews",
    }
)
RECONSTRUCTIBLE_DIRECTORIES = frozenset(
    {
        "bulk",
        "duckdb-spill",
        "mirror",
        "parquet",
        "staging",
        "tmp",
        "url-index",
        "url-index-mirror",
    }
)
IGNORED_FILES = frozenset(
    {WAREHOUSE_LOCK, f"{DATABASE_NAME}-shm", f"{DATABASE_NAME}-wal"}
)


class BackupError(RuntimeError):
    """A snapshot or verification invariant failed."""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_directory(path: Path, label: str, *, create: bool = False) -> Path:
    if not path.is_absolute() or path == Path(path.anchor):
        raise BackupError(f"{label} must be an absolute non-root path")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.is_symlink():
        raise BackupError(f"{label} must be a real directory: {path}")
    return path.resolve(strict=True)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


@contextmanager
def _exclusive_lock(
    path: Path, timeout_seconds: float, *, create: bool = False
) -> Iterator[None]:
    label = "snapshot coordination lock" if create else "shared warehouse lock"
    flags = (
        (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
    ) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError as exc:
        raise BackupError(f"{label} is missing: {path}") from exc
    except OSError as exc:
        raise BackupError(f"cannot open {label}: {path}: {exc}") from exc

    deadline = time.monotonic() + timeout_seconds
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError(f"{label} is unsafe: {path}")
        while True:
            try:
                # Linux permits an exclusive advisory flock on a read-only file
                # descriptor.  The backup can therefore coordinate with writers
                # without needing any write access to the evidence warehouse.
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BackupError(f"timed out waiting for lock: {path}") from None
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _safe_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\n" in value
        or "\r" in value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise BackupError(f"unsafe relative path in snapshot: {value!r}")
    return Path(*pure.parts)


def _validate_source_layout(warehouse: Path) -> list[str]:
    included: list[str] = []
    for child in sorted(warehouse.iterdir(), key=lambda item: item.name):
        name = child.name
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BackupError(f"warehouse top-level path is a symlink: {name}")
        if name == DATABASE_NAME:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise BackupError("Common Crawl database is not a regular file")
            continue
        if name in IGNORED_FILES:
            if not stat.S_ISREG(info.st_mode):
                raise BackupError(f"expected regular runtime file: {name}")
            continue
        if name in INCLUDED_DIRECTORIES:
            if not stat.S_ISDIR(info.st_mode):
                raise BackupError(f"expected durable directory: {name}")
            included.append(name)
            continue
        if name in RECONSTRUCTIBLE_DIRECTORIES:
            if not stat.S_ISDIR(info.st_mode):
                raise BackupError(f"expected reconstructible directory: {name}")
            continue
        raise BackupError(
            f"unreviewed warehouse top-level path would be omitted: {name}"
        )
    if not (warehouse / DATABASE_NAME).is_file():
        raise BackupError(f"database is missing: {warehouse / DATABASE_NAME}")
    return included


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    limits: dict[str, int],
) -> None:
    destination.mkdir(mode=0o700)
    for current, directory_names, file_names in os.walk(source, followlinks=False):
        current_path = Path(current)
        relative_root = current_path.relative_to(source)
        target_root = destination / relative_root
        target_root.chmod(0o700)
        directory_names.sort()
        file_names.sort()
        for directory_name in directory_names:
            relative = relative_root / directory_name
            child = source / relative
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise BackupError(f"durable tree contains unsafe directory: {child}")
            (destination / relative).mkdir(mode=0o700)
        for file_name in file_names:
            relative = relative_root / file_name
            child = source / relative
            info = child.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1
            ):
                raise BackupError(f"durable tree contains non-regular file: {child}")
            if "\n" in file_name or "\r" in file_name or "\\" in file_name:
                raise BackupError(f"durable tree contains unsafe file name: {child}")
            limits["files"] += 1
            limits["bytes"] += info.st_size
            if limits["files"] > limits["maximum_files"]:
                raise BackupError("snapshot exceeds the reviewed file-count limit")
            if limits["bytes"] > limits["maximum_bytes"]:
                raise BackupError("snapshot exceeds the reviewed byte limit")
            target = destination / relative
            shutil.copyfile(child, target, follow_symlinks=False)
            target.chmod(0o600)
            copied = target.stat()
            after = child.stat(follow_symlinks=False)
            source_identity = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if copied.st_size != info.st_size or source_identity != after_identity:
                raise BackupError(f"file changed while being copied: {child}")


def _database_backup(source_path: Path, destination_path: Path) -> dict[str, int]:
    source_uri = source_path.resolve(strict=True).as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    destination_path.chmod(0o600)
    connection = sqlite3.connect(
        destination_path.resolve(strict=True).as_uri() + "?mode=ro", uri=True
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise BackupError(f"SQLite integrity check failed: {integrity[:3]}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise BackupError("SQLite foreign-key check failed")
        schema = connection.execute(
            "SELECT value FROM lake_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if schema != ("1",):
            raise BackupError(f"unsupported Common Crawl schema: {schema!r}")
        return {
            "observations": int(
                connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            ),
            "distinct_urls": int(
                connection.execute(
                    "SELECT COUNT(DISTINCT canonical_url) FROM observations"
                ).fetchone()[0]
            ),
            "record_objects": int(
                connection.execute("SELECT COUNT(*) FROM record_objects").fetchone()[0]
            ),
            "record_bytes": int(
                connection.execute(
                    "SELECT COALESCE(SUM(object_bytes), 0) FROM record_objects"
                ).fetchone()[0]
            ),
        }
    finally:
        connection.close()


def _payload_entries(snapshot: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(snapshot.rglob("*")):
        relative = path.relative_to(snapshot).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BackupError(f"snapshot contains a symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise BackupError(f"snapshot contains a non-regular file: {relative}")
        if relative in {MANIFEST_NAME, CHECKSUM_NAME}:
            continue
        digest, size = _sha256(path)
        entries.append({"path": relative, "bytes": size, "sha256": digest})
    return entries


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_checksums(snapshot: Path, entries: list[dict[str, Any]]) -> None:
    lines = [f"{entry['sha256']}  {entry['path']}" for entry in entries]
    manifest_hash, _ = _sha256(snapshot / MANIFEST_NAME)
    lines.append(f"{manifest_hash}  {MANIFEST_NAME}")
    (snapshot / CHECKSUM_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (snapshot / CHECKSUM_NAME).chmod(0o600)


def create_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    warehouse = _canonical_directory(args.warehouse, "warehouse")
    output_root = _canonical_directory(args.output_root, "output root", create=True)
    if _is_within(output_root, warehouse) or _is_within(warehouse, output_root):
        raise BackupError("warehouse and output root must not contain one another")
    if not SNAPSHOT_RE.fullmatch(args.snapshot_id):
        raise BackupError("snapshot id must be an exact UTC YYYYMMDDTHHMMSSZ value")
    final = output_root / args.snapshot_id
    incomplete = output_root / f".{args.snapshot_id}.incomplete.{os.getpid()}"
    if final.exists() or incomplete.exists():
        raise BackupError(f"snapshot destination already exists: {args.snapshot_id}")
    revision = "unknown"
    if args.revision_file:
        revision_file = args.revision_file
        if (
            not revision_file.is_absolute()
            or not revision_file.is_file()
            or revision_file.is_symlink()
        ):
            raise BackupError("revision file must be an absolute regular file")
        revision = revision_file.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise BackupError("revision receipt is malformed")

    if args.maximum_files < 1 or args.maximum_bytes < 1 or args.lock_timeout < 0:
        raise BackupError("snapshot limits must be positive")
    incomplete.mkdir(mode=0o700)
    try:
        with _exclusive_lock(
            output_root / ".snapshot.lock", args.lock_timeout, create=True
        ):
            with _exclusive_lock(warehouse / WAREHOUSE_LOCK, args.lock_timeout):
                directories = _validate_source_layout(warehouse)
                counts = _database_backup(
                    warehouse / DATABASE_NAME, incomplete / DATABASE_NAME
                )
                limits = {
                    "files": 1,
                    "bytes": (incomplete / DATABASE_NAME).stat().st_size,
                    "maximum_files": args.maximum_files,
                    "maximum_bytes": args.maximum_bytes,
                }
                for name in directories:
                    _copy_tree(
                        warehouse / name,
                        incomplete / name,
                        limits=limits,
                    )
                entries = _payload_entries(incomplete)
                manifest = {
                    "schema_version": SCHEMA,
                    "snapshot_id": args.snapshot_id,
                    "created_at_utc": _utc_now(),
                    "source_revision": revision,
                    "selection_policy": {
                        "included_directories": sorted(INCLUDED_DIRECTORIES),
                        "excluded_reconstructible_directories": sorted(
                            RECONSTRUCTIBLE_DIRECTORIES
                        ),
                        "public_parquet_mirror_included": False,
                    },
                    "database": counts,
                    "payload_files": entries,
                    "payload_bytes": sum(entry["bytes"] for entry in entries),
                }
                _write_json(incomplete / MANIFEST_NAME, manifest)
                _write_checksums(incomplete, entries)
                verify_snapshot(incomplete, expected_snapshot_id=args.snapshot_id)
                incomplete.rename(final)
        return verify_snapshot(final, expected_snapshot_id=args.snapshot_id)
    except Exception:
        if incomplete.exists():
            shutil.rmtree(incomplete)
        raise


def _load_manifest(snapshot: Path) -> dict[str, Any]:
    try:
        raw = (snapshot / MANIFEST_NAME).read_bytes()
    except OSError as exc:
        raise BackupError(f"cannot read snapshot manifest: {exc}") from exc
    if len(raw) > 4 * 1024 * 1024:
        raise BackupError("snapshot manifest exceeds the safety limit")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise BackupError("snapshot manifest is not valid JSON") from exc
    if type(value) is not dict or value.get("schema_version") != SCHEMA:
        raise BackupError("snapshot manifest schema is unsupported")
    return value


def _verify_record_objects(snapshot: Path, database: Path) -> dict[str, int]:
    connection = sqlite3.connect(
        database.resolve(strict=True).as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT object_sha256, object_bytes, relative_path FROM record_objects"
        ).fetchall()
    finally:
        connection.close()
    expected_paths: set[str] = set()
    expected_objects: dict[str, tuple[str, int]] = {}
    for row in rows:
        relative = _safe_relative(str(row["relative_path"]))
        relative_text = relative.as_posix()
        digest = str(row["object_sha256"])
        size = int(row["object_bytes"])
        if (
            not SHA256_RE.fullmatch(digest)
            or relative.parts[:2] != ("records", "sha256")
            or relative.name != f"{digest}.warc.gz"
        ):
            raise BackupError("database contains an unsafe WARC object mapping")
        prior = expected_objects.get(relative_text)
        if prior is not None and prior != (digest, size):
            raise BackupError("database maps one WARC path to conflicting identities")
        expected_objects[relative_text] = (digest, size)
        expected_paths.add(relative_text)
    for relative_text, (expected_hash, expected_size) in expected_objects.items():
        path = snapshot / _safe_relative(relative_text)
        if not path.is_file() or path.is_symlink():
            raise BackupError(f"mapped WARC object is missing: {relative_text}")
        actual_hash, actual_size = _sha256(path)
        if actual_hash != expected_hash or actual_size != expected_size:
            raise BackupError(f"mapped WARC object fails identity: {relative_text}")
    actual_paths: set[str] = set()
    records = snapshot / "records"
    if records.exists():
        for path in records.rglob("*"):
            if path.is_file():
                actual_paths.add(path.relative_to(snapshot).as_posix())
            elif path.is_symlink() or not path.is_dir():
                raise BackupError("records tree contains an unsafe entry")
    if actual_paths != expected_paths:
        raise BackupError("snapshot WARC files do not exactly match database mappings")
    return {
        "record_rows": len(rows),
        "record_objects": len(expected_objects),
        "record_bytes": sum(value[1] for value in expected_objects.values()),
    }


def verify_snapshot(
    snapshot_value: Path | str, *, expected_snapshot_id: str | None = None
) -> dict[str, Any]:
    snapshot = _canonical_directory(Path(snapshot_value), "snapshot")
    manifest = _load_manifest(snapshot)
    snapshot_id = manifest.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_RE.fullmatch(snapshot_id):
        raise BackupError("manifest snapshot id is malformed")
    if expected_snapshot_id is not None and snapshot_id != expected_snapshot_id:
        raise BackupError("manifest snapshot id does not match the requested snapshot")
    raw_entries = manifest.get("payload_files")
    if type(raw_entries) is not list:
        raise BackupError("manifest payload list is malformed")
    expected: dict[str, tuple[str, int]] = {}
    for entry in raw_entries:
        if type(entry) is not dict:
            raise BackupError("manifest payload entry is malformed")
        relative = _safe_relative(str(entry.get("path", ""))).as_posix()
        digest = entry.get("sha256")
        size = entry.get("bytes")
        if (
            relative in {MANIFEST_NAME, CHECKSUM_NAME}
            or relative in expected
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or type(size) is not int
            or size < 0
        ):
            raise BackupError("manifest payload identity is malformed or duplicated")
        expected[relative] = (digest, size)
    if manifest.get("payload_bytes") != sum(value[1] for value in expected.values()):
        raise BackupError("manifest payload byte count is inconsistent")
    policy = manifest.get("selection_policy")
    if type(policy) is not dict or policy != {
        "included_directories": sorted(INCLUDED_DIRECTORIES),
        "excluded_reconstructible_directories": sorted(RECONSTRUCTIBLE_DIRECTORIES),
        "public_parquet_mirror_included": False,
    }:
        raise BackupError("snapshot selection policy is malformed")
    actual_files: set[str] = set()
    for path in snapshot.rglob("*"):
        relative = path.relative_to(snapshot).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BackupError(f"snapshot contains a symlink: {relative}")
        if stat.S_ISREG(info.st_mode):
            actual_files.add(relative)
        elif not stat.S_ISDIR(info.st_mode):
            raise BackupError(f"snapshot contains a non-regular file: {relative}")
    expected_files = set(expected) | {MANIFEST_NAME, CHECKSUM_NAME}
    if actual_files != expected_files:
        raise BackupError("snapshot has missing or unmanifested files")
    for relative, (expected_hash, expected_size) in expected.items():
        actual_hash, actual_size = _sha256(snapshot / _safe_relative(relative))
        if actual_hash != expected_hash or actual_size != expected_size:
            raise BackupError(f"snapshot payload fails identity: {relative}")
    checksum_lines = (snapshot / CHECKSUM_NAME).read_text(encoding="utf-8").splitlines()
    checksum_map: dict[str, str] = {}
    for line in checksum_lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or not SHA256_RE.fullmatch(parts[0]):
            raise BackupError("SHA256SUMS contains a malformed row")
        relative = _safe_relative(parts[1]).as_posix()
        if relative in checksum_map:
            raise BackupError("SHA256SUMS contains a duplicate path")
        checksum_map[relative] = parts[0]
    manifest_hash, _ = _sha256(snapshot / MANIFEST_NAME)
    expected_checksums = {key: value[0] for key, value in expected.items()}
    expected_checksums[MANIFEST_NAME] = manifest_hash
    if checksum_map != expected_checksums:
        raise BackupError("SHA256SUMS does not exactly match the manifest")
    database = snapshot / DATABASE_NAME
    # Verification never writes beside evidence.  That keeps the verifier usable
    # against a read-only restore mount and avoids contaminating the manifest set.
    with tempfile.TemporaryDirectory(prefix="palimpsest-common-crawl-verify-") as temp:
        counts = _database_backup(database, Path(temp) / "verified.sqlite3")
    if manifest.get("database") != counts:
        raise BackupError("database counts differ from the snapshot manifest")
    records = _verify_record_objects(snapshot, database)
    return {
        "status": "verified",
        "schema_version": SCHEMA,
        "snapshot_id": snapshot_id,
        "source_revision": manifest.get("source_revision"),
        "payload_files": len(expected),
        "payload_bytes": sum(value[1] for value in expected.values()),
        "observations": counts["observations"],
        "distinct_urls": counts["distinct_urls"],
        **records,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create and validate one snapshot")
    create.add_argument("--warehouse", type=Path, required=True)
    create.add_argument("--output-root", type=Path, required=True)
    create.add_argument("--snapshot-id", required=True)
    create.add_argument("--revision-file", type=Path)
    create.add_argument("--lock-timeout", type=float, default=7200)
    create.add_argument("--maximum-files", type=int, default=1_000_000)
    create.add_argument("--maximum-bytes", type=int, default=1024**4)
    verify = subparsers.add_parser("verify", help="fully verify one extracted snapshot")
    verify.add_argument("snapshot", type=Path)
    verify.add_argument("--snapshot-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            result = create_snapshot(args)
        else:
            result = verify_snapshot(
                args.snapshot, expected_snapshot_id=args.snapshot_id
            )
    except (BackupError, OSError, sqlite3.Error, ValueError) as exc:
        print(
            json.dumps(
                {"status": "failed", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
