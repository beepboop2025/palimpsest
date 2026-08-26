"""Whole-tree storage limits for hostile CensorWatch captures."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.exceptions import StorageError
from censorwatch.storage_budget import (
    prune_expired_raw,
    store_raw_snapshot,
    tree_usage_bytes,
)


NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


def _store(root: Path, data: list[dict], **overrides) -> Path:
    values = {
        "source": "eastmoney_guba",
        "root": root,
        "max_snapshot_bytes": 1024,
        "max_total_bytes": 4096,
        "min_free_bytes": 0,
        "retention_days": 30,
        "now": NOW,
    }
    values.update(overrides)
    return Path(store_raw_snapshot(data, **values))


def test_raw_snapshot_is_create_only_bounded_and_private(tmp_path):
    path = _store(tmp_path / "raw", [{"post_id": "1", "text": "bounded"}])

    assert path.read_text(encoding="utf-8").endswith("\n")
    assert path.stat().st_mode & 0o777 == 0o600
    assert tree_usage_bytes(tmp_path / "raw") == path.stat().st_size

    with pytest.raises(StorageError, match="snapshot byte budget"):
        _store(tmp_path / "other", [{"text": "x" * 2048}])
    assert not (tmp_path / "other").exists()


def test_expired_raw_is_pruned_before_total_quota_is_applied(tmp_path):
    root = tmp_path / "raw"
    old = root / "eastmoney_guba" / "2025-01-01" / "old.json"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x" * 300)
    os.utime(old, (0, 0))

    path = _store(root, [{"post_id": "new"}], max_total_bytes=128)

    assert path.exists()
    assert not old.exists()


def test_total_quota_and_unsafe_tree_fail_closed(tmp_path):
    root = tmp_path / "raw"
    retained = root / "eastmoney_guba" / "2026-08-25" / "kept.json"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"x" * 100)

    with pytest.raises(StorageError, match="storage quota"):
        _store(root, [{"post_id": "2"}], max_total_bytes=110)
    assert retained.exists()

    target = tmp_path / "target"
    target.write_text("outside", encoding="utf-8")
    (root / "unsafe-link").symlink_to(target)
    with pytest.raises(StorageError, match="symlink"):
        prune_expired_raw(root, retention_days=1, now=NOW.timestamp())
    assert target.read_text(encoding="utf-8") == "outside"
