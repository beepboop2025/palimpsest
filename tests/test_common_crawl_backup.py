import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from collectors import common_crawl_lake as lake

ROOT = Path(__file__).resolve().parents[1]
BACKUP_PATH = ROOT / "ops" / "backup" / "common_crawl_backup.py"


def _load_backup_module():
    spec = importlib.util.spec_from_file_location(
        "palimpsest_common_crawl_backup", BACKUP_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backup = _load_backup_module()


def _warehouse(root: Path, *, with_record: bool = True) -> Path:
    warehouse = root / "warehouse"
    warehouse.mkdir()
    (warehouse / "derived").mkdir()
    (warehouse / "derived" / "story-ranking-features.jsonl").write_text(
        '{"training_label":"unreviewed"}\n', encoding="utf-8"
    )
    (warehouse / "inbox").mkdir()
    (warehouse / "inbox" / "CC-MAIN-2026-30.jsonl.gz").write_bytes(b"public-export")
    # The full public mirror is reconstructible and intentionally excluded.
    (warehouse / "parquet").mkdir()
    (warehouse / "parquet" / "part-00000.parquet").write_bytes(b"public-mirror")

    database = warehouse / backup.DATABASE_NAME
    connection = lake._connect(database)
    try:
        lake.initialize_database(connection)
        connection.execute(
            """
            INSERT INTO ingest_runs (
                input_sha256, input_name, input_bytes, input_format, crawl_hint,
                scope_sha256, ingested_at, rows_seen, rows_accepted,
                rows_out_of_scope, rows_duplicate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a" * 64,
                "fixture.jsonl",
                100,
                "jsonl",
                "CC-MAIN-2026-30",
                "b" * 64,
                "2026-08-12T00:00:00Z",
                1,
                1,
                0,
                0,
            ),
        )
        locator = "c" * 64
        connection.execute(
            """
            INSERT INTO observations (
                target_id, crawl, canonical_url, url_sha256, capture_at,
                fetch_status, content_digest, mime_type, languages,
                warc_filename, warc_record_offset, warc_record_length,
                locator_sha256, input_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pbc",
                "CC-MAIN-2026-30",
                "https://www.pbc.gov.cn/example",
                "d" * 64,
                "2026-07-01T00:00:00Z",
                200,
                "A" * 32,
                "text/html",
                "zho",
                "crawl-data/CC-MAIN-2026-30/segments/fixture/warc/fixture.warc.gz",
                10,
                12,
                locator,
                "a" * 64,
            ),
        )
        if with_record:
            raw = b"\x1f\x8bselected-private-warc"
            digest = hashlib.sha256(raw).hexdigest()
            relative = Path("records") / "sha256" / digest[:2] / f"{digest}.warc.gz"
            path = warehouse / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            connection.execute(
                """
                INSERT INTO record_objects (
                    locator_sha256, object_sha256, object_bytes,
                    relative_path, retrieved_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    locator,
                    digest,
                    len(raw),
                    relative.as_posix(),
                    "2026-08-12T00:10:00Z",
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return warehouse


def _create(tmp_path: Path, warehouse: Path | None = None):
    source = warehouse or _warehouse(tmp_path)
    output = tmp_path / "snapshots"
    revision = tmp_path / "deployed-commit"
    revision.write_text("e" * 40 + "\n", encoding="utf-8")
    args = backup.build_parser().parse_args(
        [
            "create",
            "--warehouse",
            str(source),
            "--output-root",
            str(output),
            "--snapshot-id",
            "20260812T010203Z",
            "--revision-file",
            str(revision),
        ]
    )
    result = backup.create_snapshot(args)
    return output / "20260812T010203Z", result


def test_snapshot_is_consistent_bounded_and_fully_verifiable(tmp_path):
    snapshot, result = _create(tmp_path)

    assert result["status"] == "verified"
    assert result["observations"] == 1
    assert result["distinct_urls"] == 1
    assert result["record_objects"] == 1
    assert (snapshot / backup.DATABASE_NAME).is_file()
    assert (snapshot / "derived" / "story-ranking-features.jsonl").is_file()
    assert (snapshot / "inbox" / "CC-MAIN-2026-30.jsonl.gz").is_file()
    assert not (snapshot / "parquet").exists()
    assert backup.verify_snapshot(snapshot) == result


def test_verifier_detects_payload_tampering(tmp_path):
    snapshot, _ = _create(tmp_path)
    target = snapshot / "derived" / "story-ranking-features.jsonl"
    target.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(backup.BackupError, match="fails identity"):
        backup.verify_snapshot(snapshot)


def test_verifier_rejects_an_unmanifested_symlink(tmp_path):
    snapshot, _ = _create(tmp_path)
    (snapshot / "derived" / "unmanifested").symlink_to(tmp_path / "outside")

    with pytest.raises(backup.BackupError, match="snapshot contains a symlink"):
        backup.verify_snapshot(snapshot)


def test_missing_selected_warc_can_never_publish(tmp_path):
    warehouse = _warehouse(tmp_path)
    next((warehouse / "records").rglob("*.warc.gz")).unlink()

    with pytest.raises(backup.BackupError, match="mapped WARC object is missing"):
        _create(tmp_path, warehouse)
    assert not (tmp_path / "snapshots" / "20260812T010203Z").exists()


def test_unreviewed_top_level_state_fails_closed(tmp_path):
    warehouse = _warehouse(tmp_path)
    (warehouse / "future-editor-state").mkdir()

    with pytest.raises(backup.BackupError, match="unreviewed warehouse top-level"):
        _create(tmp_path, warehouse)


def test_included_tree_rejects_symlinks(tmp_path):
    warehouse = _warehouse(tmp_path)
    (warehouse / "labels").mkdir()
    (warehouse / "labels" / "outside").symlink_to(tmp_path / "outside")

    with pytest.raises(backup.BackupError, match="non-regular file"):
        _create(tmp_path, warehouse)


def test_verifier_does_not_need_to_write_into_snapshot(tmp_path):
    snapshot, expected = _create(tmp_path)
    for path in sorted(snapshot.rglob("*"), reverse=True):
        path.chmod(0o500 if path.is_dir() else 0o400)
    snapshot.chmod(0o500)
    try:
        assert backup.verify_snapshot(snapshot) == expected
    finally:
        snapshot.chmod(0o700)
        for path in snapshot.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)


def test_copy_is_not_confused_by_a_source_mutation(tmp_path, monkeypatch):
    warehouse = _warehouse(tmp_path)
    original = shutil.copyfile

    def mutating_copy(source, destination, *, follow_symlinks=True):
        result = original(source, destination, follow_symlinks=follow_symlinks)
        if Path(source).name == "story-ranking-features.jsonl":
            Path(source).write_text("changed-size-after-copy\n", encoding="utf-8")
        return result

    monkeypatch.setattr(backup.shutil, "copyfile", mutating_copy)
    with pytest.raises(backup.BackupError, match="changed while being copied"):
        _create(tmp_path, warehouse)


def test_systemd_job_is_sandboxed_and_uses_restore_verified_script():
    unit = (ROOT / "ops/systemd/palimpsest-common-crawl-backup.service").read_text(
        encoding="utf-8"
    )
    timer = (ROOT / "ops/systemd/palimpsest-common-crawl-backup.timer").read_text(
        encoding="utf-8"
    )

    assert "ConditionFileIsExecutable=" in unit
    assert "EnvironmentFile=/root/.config/anchor/object-storage.env" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadOnlyPaths=" in unit and "/var/lib/palimpsest/common-crawl" in unit
    assert "ReadWritePaths=" in unit and ".common-crawl.lock" in unit
    assert "StateDirectory=palimpsest-common-crawl-backup" in unit
    assert "OnCalendar=Sun" in timer
    assert "Persistent=true" in timer


def test_uploader_is_encrypted_copy_only_and_receipt_last():
    script = (ROOT / "ops/backup/palimpsest-common-crawl-offsite-backup.sh").read_text(
        encoding="utf-8"
    )

    assert "--symmetric --cipher-algo AES256" in script
    assert 'export GNUPGHOME="$run_root/gnupg"' in script
    assert "--immutable" in script
    assert "?object-lock" in script
    assert "must remain enabled" in script
    assert 'common_crawl_backup.py" verify' not in script  # path is held in a variable
    assert '"$snapshot_tool" verify' in script
    assert "rclone sync" not in script
    assert "rclone delete" not in script
    assert "rclone purge" not in script
    restore_position = script.index('"$snapshot_tool" verify')
    receipt_position = script.index(
        'rclone copyto "$receipt" "$remote_base/RECEIPT.json"'
    )
    assert restore_position < receipt_position
