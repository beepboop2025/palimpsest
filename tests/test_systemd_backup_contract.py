"""The backup unit uses systemd's real executable-file condition directive."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backup_units_use_valid_executable_condition():
    for relative in (
        "ops/systemd/palimpsest-backup.service",
        "ops/systemd/palimpsest-backup.override.example.conf",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "ConditionFileIsExecutable=" in text
        assert "ConditionPathIsExecutable=" not in text


def test_backup_archive_and_restore_preserve_numeric_producer_ownership():
    script = (ROOT / "ops/backup/palimpsest-backup.sh").read_text(encoding="utf-8")
    documentation = (ROOT / "ops/backup/README.md").read_text(encoding="utf-8")

    assert "--create --gzip --numeric-owner --file -" in script
    assert "--log-driver none" in script
    assert "format_version=2" in script
    assert "--extract --gzip --numeric-owner --same-owner" in documentation
    assert "replacement-state-root" in documentation
