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
    helper = (ROOT / "scripts/palimpsest_backup_archive.py").read_text(
        encoding="utf-8"
    )
    documentation = (ROOT / "ops/backup/README.md").read_text(encoding="utf-8")

    assert "/app/scripts/palimpsest_backup_archive.py" in script
    assert "--entrypoint /usr/local/bin/python3" in script
    assert '"$artifact_image" -I -B' in script
    assert "def _write_archive" in helper
    assert "tarfile.open(" in helper
    assert 'mode="w|gz"' in helper
    assert "format=tarfile.PAX_FORMAT" in helper
    assert "dereference=False" in helper
    assert 'ARCHIVE_ROOTS = ("readings", "data", "analysis", "newswire")' in helper
    assert 'member.uname = ""' in helper
    assert 'member.gname = ""' in helper
    assert "subprocess" not in helper
    assert "--log-driver none" in script
    assert "format_version=3" in script
    assert "artifact_roots=readings,data,newswire,analysis" in script
    assert "dst=/source/analysis,readonly" in script
    assert "dst=/source/newswire,readonly" in script
    assert "PALIMPSEST_BACKUP_COPY_DIR is retired" in script
    assert "PALIMPSEST_BACKUP_HOOK is retired" in script
    assert "PALIMPSEST_BACKUP_OFFSITE_ENCRYPTED is retired" in script
    assert 'LOCK_PATH = "/source/analysis/private/cascade.lock"' in helper
    assert 'NEWSWIRE_LOCK_PATH = "/source/newswire/newswire.lock"' in helper
    assert "os.O_NOFOLLOW" in helper
    assert "dir_fd=parent_descriptor" in helper
    assert "fcntl.LOCK_SH" in helper
    assert "follow_symlinks=False" in helper
    assert "descriptor_metadata.st_ino != path_metadata.st_ino" in helper
    assert 'raise ArchivePreflightError("fixed artifact archive failed")' in helper
    assert 'set(root_entries) != {"runs", "private", "delivery"}' in helper
    assert 'DELIVERY_FILES = ("wire-claim-audits-latest.json",)' in helper
    assert "MAX_DELIVERY_BYTES = 16 * 1024 * 1024" in helper
    assert "MAX_ANALYSIS_ENTRIES = 32768" in helper
    assert "MAX_RUNS = 48" in helper
    assert "--extract --gzip --numeric-owner --same-owner" in documentation
    assert "replacement-bundle" in documentation

    dockerfile = (ROOT / "ops/docker/Dockerfile.app").read_text(encoding="utf-8")
    assert "COPY --chown=palimpsest:palimpsest scripts/" in dockerfile


def test_backup_unit_exposes_analysis_tree_read_only():
    service = (ROOT / "ops/systemd/palimpsest-backup.service").read_text(
        encoding="utf-8"
    )

    assert (
        "ReadOnlyPaths=/var/lib/palimpsest-analysis /var/lib/palimpsest/newswire"
        in service
    )
