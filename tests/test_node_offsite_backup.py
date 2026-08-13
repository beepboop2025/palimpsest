from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops/backup/palimpsest-node-offsite-backup.sh"


def test_offsite_lane_encrypts_before_any_remote_transfer() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    pack = source.index('"$snapshot_tool" pack')
    encrypt = source.index("--symmetric --cipher-algo AES256")
    upload = source.index('rclone copyto "$archive"')
    assert pack < encrypt < upload
    assert "palimpsest-node-backup.tar.gpg" in source
    assert 'artifacts.tar.gz" "$remote_base' not in source


def test_offsite_lane_is_receipt_last_and_restore_verified() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    archive_upload = source.index('rclone copyto "$archive"')
    download = source.index(
        'rclone copyto "$remote_base/palimpsest-node-backup.tar.gpg"'
    )
    decrypt = source.index("--decrypt --output")
    safe_inspection = source.index('"$snapshot_tool" inspect-outer')
    restored_validation = source.index(
        '"$snapshot_tool" verify "$restore_root/$snapshot_id"'
    )
    postgres_validation = source.index(
        'pg_restore --list "$restore_root/$snapshot_id/postgres.dump"'
    )
    isolated_restore = source.index("docker run --detach --pull never --network none")
    core_relations = source.index(
        "articles collection_logs observation_artifacts ddti_index_snapshots"
    )
    receipt_upload = source.index('rclone copyto "$receipt"')
    status_success = source.index("write_status success")
    assert (
        archive_upload
        < download
        < decrypt
        < safe_inspection
        < restored_validation
        < postgres_validation
        < isolated_restore
        < core_relations
        < receipt_upload
        < status_success
    )
    assert "Receipt-last publication is the remote commit marker" in source


def test_offsite_lane_fails_closed_on_storage_and_source_boundaries() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'retention_mode" == COMPLIANCE' in source
    assert "<ObjectLockEnabled>Enabled</ObjectLockEnabled>" in source
    assert "x-amz-object-lock-mode: COMPLIANCE" in source
    assert "retention deadline is shorter than policy" in source
    assert "--immutable" in source
    assert "--log-driver none" in source
    assert "--read-only" in source
    assert "--exit-on-error" in source
    assert "--tmpfs /var/run/postgresql:rw,noexec,nosuid,size=16m" in source
    for capability in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"):
        assert f"--cap-add {capability}" in source
    assert "flock -s 9" in source
    assert "stat -c '%u:%g:%a:%h'" in source
    assert "nodevault" in source
    assert '"$CREDENTIALS_DIRECTORY"' not in source
    assert 'credentials_directory="${CREDENTIALS_DIRECTORY:-}"' in source
    assert 'rclone_config="${credentials_directory}/node-offsite-rclone.conf"' in source
    assert "parser.sections() != [remote]" in source
    assert "RCLONE_CONFIG_ANCHOR" not in source
    assert "common-crawl-backup.passphrase" not in source
    assert 'b"\\n" in payload' in source
    assert "not 32 <= len(payload) <= 4096" in source
    assert "one canonical 32-4096 byte line" in source


def test_status_is_atomic_and_contains_no_exception_text() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'mktemp "${status_path}.tmp.XXXXXX"' in source
    assert 'mv -f -- "$temporary" "$status_path"' in source
    assert '"failure_class": failure_class or None' in source
    assert '"pending": {"attempt_id": attempt' in source
    assert '"last_success": json.loads(previous_success)' in source
    assert "write_status failed operational_failure" in source
    assert "trap 'cleanup \"$?\"' EXIT" in source
    assert "trap 'exit 130' INT" in source
    assert "trap 'exit 143' TERM" in source
    assert "set +e" in source
    assert 'docker rm -f "$restore_container"' in source
    assert '"error"' not in source
    assert '"message"' not in source


def test_restore_archive_is_inspected_before_extraction() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert source.index('"$snapshot_tool" inspect-outer') < source.index(
        "tar --extract --no-same-owner"
    )
    assert "--no-same-permissions" in source
    assert "--scratch-restore" in source
