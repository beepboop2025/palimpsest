"""Private normalized-observation retention is immutable and bounded."""

from __future__ import annotations

import gzip
import json
import stat
from datetime import datetime, timezone

import pytest

from core.artifact_store import archive_observation


NOW = datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc)


def test_archives_exact_bytes_as_content_addressed_gzip(tmp_path, monkeypatch):
    monkeypatch.delenv("PALIMPSEST_OBSERVATION_DIR", raising=False)
    reading = tmp_path / "readings" / "signal-latest.json"
    reading.parent.mkdir()
    reading.write_text(
        '{"generated_at":"2026-08-11T09:29:00Z","n_events":7}\n',
        encoding="utf-8",
    )

    artifact = archive_observation(
        "signal", reading, repo_root=tmp_path, observed_at=NOW
    )
    stored = tmp_path / artifact["archive_path"]

    assert stored.is_file()
    assert gzip.decompress(stored.read_bytes()) == reading.read_bytes()
    assert artifact["sha256"] in stored.name
    assert artifact["generated_at"] == "2026-08-11T09:29:00Z"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o644


def test_same_observation_and_time_is_idempotent(tmp_path):
    reading = tmp_path / "reading.json"
    reading.write_text(json.dumps({"generated_at": "t", "value": 1}))

    first = archive_observation("signal", reading, repo_root=tmp_path, observed_at=NOW)
    second = archive_observation("signal", reading, repo_root=tmp_path, observed_at=NOW)

    assert first == second
    assert len(list((tmp_path / "data" / "observations").rglob("*.json.gz"))) == 1


def test_deduplication_heals_old_host_unreadable_mode(tmp_path):
    reading = tmp_path / "reading.json"
    reading.write_text(json.dumps({"generated_at": "t", "value": 1}))
    artifact = archive_observation("signal", reading, repo_root=tmp_path)
    stored = tmp_path / artifact["archive_path"]
    stored.chmod(0o600)

    archive_observation("signal", reading, repo_root=tmp_path)

    assert stat.S_IMODE(stored.stat().st_mode) == 0o644


def test_rejects_path_shaped_source_names_before_writing(tmp_path):
    reading = tmp_path / "reading.json"
    reading.write_text("{}")

    with pytest.raises(ValueError, match="unsafe observation source"):
        archive_observation("../escape", reading, repo_root=tmp_path, observed_at=NOW)
    assert not (tmp_path / "data").exists()


def test_size_budget_fails_without_a_partial_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_OBSERVATION_MAX_BYTES", "1024")
    reading = tmp_path / "reading.json"
    reading.write_text(json.dumps({"payload": "x" * 2048}))

    with pytest.raises(ValueError, match="limit is 1024"):
        archive_observation("signal", reading, repo_root=tmp_path, observed_at=NOW)
    assert not list(tmp_path.rglob("*.json.gz"))


def test_rejects_non_object_json(tmp_path):
    reading = tmp_path / "reading.json"
    reading.write_text("[]")

    with pytest.raises(ValueError, match="JSON object"):
        archive_observation("signal", reading, repo_root=tmp_path, observed_at=NOW)
