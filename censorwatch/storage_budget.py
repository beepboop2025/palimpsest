"""Bounded, symlink-averse storage helpers for hostile-source evidence."""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.exceptions import StorageError

_MAX_TREE_ENTRIES = 100_000


def tree_usage_bytes(root: Path, *, max_entries: int = _MAX_TREE_ENTRIES) -> int:
    """Return regular-file bytes below ``root`` or fail closed on unsafe shape."""
    if root.is_symlink():
        raise StorageError(str(root), "storage root is a symlink")
    if not root.exists():
        return 0
    if not root.is_dir():
        raise StorageError(str(root), "storage root is not a directory")

    total = 0
    seen = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise StorageError(str(directory), type(exc).__name__) from exc
        for entry in entries:
            seen += 1
            if seen > max_entries:
                raise StorageError(str(root), "storage tree entry budget exceeded")
            try:
                if entry.is_symlink():
                    raise StorageError(entry.path, "symlink inside storage tree")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    if stat.st_nlink != 1:
                        raise StorageError(entry.path, "hard-linked evidence file")
                    total += stat.st_size
                else:
                    raise StorageError(entry.path, "special file inside storage tree")
            except OSError as exc:
                raise StorageError(entry.path, type(exc).__name__) from exc
    return total


def prune_expired_raw(
    root: Path, *, retention_days: int, now: float | None = None
) -> int:
    """Delete only expired regular raw files; unsafe tree entries abort pruning."""
    if not root.exists():
        return 0
    # Validate the complete tree before deleting anything. This avoids a partial
    # prune followed by discovering an operator mistake or a hostile symlink.
    tree_usage_bytes(root)
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    removed = 0
    directories: list[Path] = []
    for directory, child_dirs, filenames in os.walk(
        root, topdown=True, followlinks=False
    ):
        path = Path(directory)
        directories.extend(path / name for name in child_dirs)
        for name in filenames:
            candidate = path / name
            stat = candidate.stat(follow_symlinks=False)
            if stat.st_mtime < cutoff:
                candidate.unlink()
                removed += 1
    for directory in sorted(
        directories, key=lambda item: len(item.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def store_raw_snapshot(
    data: list[dict[str, Any]],
    *,
    source: str,
    root: Path,
    max_snapshot_bytes: int,
    max_total_bytes: int,
    min_free_bytes: int,
    retention_days: int,
    now: datetime | None = None,
) -> str:
    """Serialize and atomically create one immutable, quota-checked raw capture."""
    timestamp = now or datetime.now(timezone.utc)
    payload = (
        json.dumps(data, default=str, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(payload) > max_snapshot_bytes:
        raise StorageError(str(root), "raw snapshot byte budget exceeded")

    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    prune_expired_raw(
        root, retention_days=retention_days, now=timestamp.timestamp()
    )
    used = tree_usage_bytes(root)
    if used + len(payload) > max_total_bytes:
        raise StorageError(str(root), "raw storage quota reached")
    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        raise StorageError(str(root), type(exc).__name__) from exc
    if free < min_free_bytes + len(payload):
        raise StorageError(str(root), "raw storage free-space reserve reached")

    safe_source = "".join(
        character
        for character in source
        if character.isalnum() or character in "-_"
    )
    if not safe_source or safe_source != source:
        raise StorageError(str(root), "unsafe source storage component")
    directory = root / safe_source / timestamp.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    filename = (
        f"{safe_source}_{timestamp.strftime('%H%M%S')}_"
        f"{int(timestamp.timestamp() * 1_000_000_000)}.json"
    )
    path = directory / filename
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StorageError(str(path), type(exc).__name__) from exc
    return str(path)
