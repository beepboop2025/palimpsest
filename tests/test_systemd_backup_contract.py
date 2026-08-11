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
