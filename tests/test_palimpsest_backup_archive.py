"""Contract tests for the capability-bounded node archive helper."""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tarfile
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts/palimpsest_backup_archive.py"
SPEC = importlib.util.spec_from_file_location("palimpsest_backup_archive", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
archive_helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive_helper)


def _valid_lock(**changes: int) -> SimpleNamespace:
    fields = {
        "st_mode": stat.S_IFREG | 0o600,
        "st_nlink": 1,
        "st_uid": 10001,
        "st_gid": 10001,
        "st_dev": 2049,
        "st_ino": 981723,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def _valid_newswire_lock(**changes: int) -> SimpleNamespace:
    fields = {
        "st_mode": stat.S_IFREG | 0o600,
        "st_nlink": 1,
        "st_uid": 1001,
        "st_gid": 1001,
        "st_dev": 2049,
        "st_ino": 981799,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def _analysis_tree(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "analysis"
    runs = root / "runs"
    private = root / "private"
    run = runs / "run-20260813T010203Z-0123456789ab"
    (run / "inputs").mkdir(parents=True)
    (run / "readings").mkdir()
    (run / "private").mkdir()
    (private / "ledger").mkdir(parents=True)
    (run / "readings" / "analysis-run-manifest.json").write_text("{}\n")
    (run / "private" / "analytical-packets-latest.json").write_text("{}\n")
    (private / "cascade.lock").write_text("\n")
    (private / "state.json").write_text("{}\n")
    (private / "ledger" / "candidate-versions.jsonl").write_text("{}\n")

    root.chmod(0o711)
    runs.chmod(0o710)
    private.chmod(0o700)
    for directory in (run, run / "inputs", run / "readings", run / "private"):
        directory.chmod(0o750)
    (private / "ledger").chmod(0o700)
    for path in run.rglob("*"):
        if path.is_file():
            path.chmod(0o640)
    for path in private.rglob("*"):
        if path.is_file():
            path.chmod(0o600)

    monkeypatch.setattr(archive_helper, "ANALYSIS_ROOT", str(root))
    monkeypatch.setattr(archive_helper, "RUNS_ROOT", str(runs))
    monkeypatch.setattr(archive_helper, "PRIVATE_ROOT", str(private))
    monkeypatch.setattr(archive_helper, "ROOT_UID", os.getuid())
    monkeypatch.setattr(archive_helper, "ROOT_GID", os.getgid())
    monkeypatch.setattr(archive_helper, "RUNTIME_UID", os.getuid())
    monkeypatch.setattr(archive_helper, "RUNTIME_GID", os.getgid())
    return root


def _artifact_tree(tmp_path: Path, monkeypatch) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _analysis_tree(source, monkeypatch)
    readings = source / "readings"
    data = source / "data"
    readings.mkdir()
    data.mkdir()
    newswire = source / "newswire"
    newswire.mkdir()
    (readings / "latest.json").write_text('{"status":"ok"}\n')
    (data / "observations").mkdir()
    (data / "observations" / "sample.json").write_text('{"value":1}\n')
    (data / ".recovery.lock").write_text("")
    (data / "observations" / "index_20260813T010203+0000.json").write_text(
        '{"value":2}\n'
    )
    latest = b'{"generated_at":"2026-08-13T01:02:03Z","wire":"ok"}\n'
    (newswire / "newswire-latest.json").write_bytes(latest)
    (newswire / "newswire-versions.jsonl").write_text('{"event_id":"one"}\n')
    (newswire / "newswire-status.json").write_text(
        json.dumps(
            {
                "schema_version": "palimpsest-evidence-wire-attempt.v1",
                "attempted_at": "2026-08-13T01:02:00Z",
                "completed_at": "2026-08-13T01:02:04Z",
                "status": "success",
                "fresh_sources": 1,
                "output_generated_at": "2026-08-13T01:02:03Z",
                "output_sha256": hashlib.sha256(latest).hexdigest(),
                "failure_class": None,
            },
            sort_keys=True,
        )
        + "\n"
    )
    (newswire / "newswire.lock").write_text("")
    (newswire / "newswire.lock").chmod(0o600)
    monkeypatch.setattr(archive_helper, "NEWSWIRE_UID", os.getuid())
    monkeypatch.setattr(archive_helper, "NEWSWIRE_GID", os.getgid())
    monkeypatch.setattr(archive_helper, "SOURCE_ROOT", str(source))
    return source


def test_analysis_tree_preflight_accepts_exact_immutable_shape(tmp_path, monkeypatch):
    _analysis_tree(tmp_path, monkeypatch)

    signatures = archive_helper._validate_analysis_tree()

    assert signatures
    assert any(row[0] == "private/state.json" for row in signatures)
    assert any(
        row[0]
        == "runs/run-20260813T010203Z-0123456789ab/private/analytical-packets-latest.json"
        for row in signatures
    )


@pytest.mark.parametrize("unsafe_kind", ["symlink", "special", "staging"])
def test_analysis_tree_preflight_rejects_unsafe_entries(
    tmp_path, monkeypatch, unsafe_kind
):
    root = _analysis_tree(tmp_path, monkeypatch)
    if unsafe_kind == "symlink":
        (root / "private" / "escape").symlink_to(root / "private" / "state.json")
    elif unsafe_kind == "special":
        os.mkfifo(root / "private" / "unexpected.pipe", 0o600)
    else:
        staging = root / "runs" / ".staging-0123456789abcdef"
        staging.mkdir(mode=0o750)

    with pytest.raises(archive_helper.ArchivePreflightError):
        archive_helper._validate_analysis_tree()


def test_analysis_tree_preflight_is_bounded(tmp_path, monkeypatch):
    _analysis_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(archive_helper, "MAX_ANALYSIS_ENTRIES", 3)

    with pytest.raises(archive_helper.ArchivePreflightError):
        archive_helper._validate_analysis_tree()


def test_write_archive_streams_only_fixed_roots_with_numeric_ownership(
    tmp_path, monkeypatch
):
    source = _artifact_tree(tmp_path, monkeypatch)
    stream = io.BytesIO()

    archive_helper._write_archive(stream)

    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:gz") as archive:
        members = archive.getmembers()

    names = {member.name for member in members}
    assert {"readings", "data", "analysis", "newswire"}.issubset(names)
    assert "readings/latest.json" in names
    assert "data/observations/sample.json" in names
    assert "data/.recovery.lock" in names
    assert "data/observations/index_20260813T010203+0000.json" in names
    assert "analysis/private/state.json" in names
    assert "newswire/newswire-latest.json" in names
    assert all(
        member.name.split("/", 1)[0] in archive_helper.ARCHIVE_ROOTS
        for member in members
    )
    for member in members:
        source_metadata = (source / member.name).lstat()
        assert member.uid == source_metadata.st_uid
        assert member.gid == source_metadata.st_gid
        assert member.uname == ""
        assert member.gname == ""

    root_order = [member.name for member in members if "/" not in member.name]
    assert root_order == ["analysis", "readings", "data", "newswire"]


def test_path_to_symlink_swap_cannot_archive_external_bytes(tmp_path, monkeypatch):
    source = _artifact_tree(tmp_path, monkeypatch)
    external = tmp_path / "outside-secret.txt"
    external_marker = b"EXTERNAL-BYTES-MUST-NEVER-BE-ARCHIVED"
    external.write_bytes(external_marker)
    target = source / "readings" / "latest.json"
    displaced = source / "readings" / "original.json"
    real_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "latest.json" and dir_fd is not None and not swapped:
            # This happens after the helper's no-follow stat but before its
            # descriptor-relative open: the exact race pathname tar APIs lose.
            target.rename(displaced)
            target.symlink_to(external)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(archive_helper.os, "open", racing_open)
    stream = io.BytesIO()

    with pytest.raises(
        archive_helper.ArchivePreflightError,
        match="fixed artifact archive failed",
    ):
        archive_helper._write_archive(stream)

    assert swapped
    # The gzip writer closes on the fail-closed path, so even its partial tar
    # can be inspected to prove the external payload was never read.
    assert external_marker not in gzip.decompress(stream.getvalue())


def test_newswire_rejects_extra_atomic_temporary_file(tmp_path, monkeypatch):
    source = _artifact_tree(tmp_path, monkeypatch)
    (source / "newswire" / ".newswire-status.json.temporary").write_text("{}\n")

    with pytest.raises(
        archive_helper.ArchivePreflightError,
        match="fixed artifact archive failed",
    ):
        archive_helper._write_archive(io.BytesIO())


def test_newswire_status_must_bind_exact_archived_latest_bytes(
    tmp_path, monkeypatch
):
    source = _artifact_tree(tmp_path, monkeypatch)
    (source / "newswire" / "newswire-latest.json").write_text(
        '{"generated_at":"2026-08-13T01:02:03Z","wire":"changed"}\n'
    )

    with pytest.raises(
        archive_helper.ArchivePreflightError,
        match="fixed artifact archive failed",
    ):
        archive_helper._write_archive(io.BytesIO())


@pytest.mark.parametrize(
    "change",
    [
        {"mode": 0o640},
        {"hardlink": True},
    ],
)
def test_newswire_archive_rejects_unsafe_generation_lock(
    tmp_path, monkeypatch, change
):
    source = _artifact_tree(tmp_path, monkeypatch)
    lock = source / "newswire" / "newswire.lock"
    if "mode" in change:
        lock.chmod(change["mode"])
    else:
        os.link(lock, source / "newswire-lock-alias")

    with pytest.raises(
        archive_helper.ArchivePreflightError,
        match="fixed artifact archive failed",
    ):
        archive_helper._write_archive(io.BytesIO())


@pytest.mark.parametrize("unsafe_kind", ["symlink", "fifo", "hardlink"])
def test_write_archive_rejects_unsafe_filesystem_members(
    tmp_path, monkeypatch, unsafe_kind
):
    source = _artifact_tree(tmp_path, monkeypatch)
    unsafe = source / "readings" / "unsafe"
    if unsafe_kind == "symlink":
        unsafe.symlink_to(source / "readings" / "latest.json")
    elif unsafe_kind == "fifo":
        os.mkfifo(unsafe, 0o600)
    else:
        os.link(source / "readings" / "latest.json", unsafe)

    with pytest.raises(
        archive_helper.ArchivePreflightError,
        match="fixed artifact archive failed",
    ):
        archive_helper._write_archive(io.BytesIO())


@pytest.mark.parametrize(
    "member",
    [
        tarfile.TarInfo("/absolute"),
        tarfile.TarInfo("../escape"),
        tarfile.TarInfo("readings/../escape"),
        tarfile.TarInfo("unapproved/file"),
    ],
)
def test_archive_filter_rejects_members_outside_fixed_roots(member):
    with pytest.raises(archive_helper.ArchivePreflightError):
        archive_helper._archive_filter(member)


def test_write_archive_wraps_writer_failure_without_private_detail(monkeypatch):
    def fail_open(**kwargs):
        raise tarfile.TarError("secret/private/path")

    monkeypatch.setattr(archive_helper.tarfile, "open", fail_open)

    with pytest.raises(
        archive_helper.ArchivePreflightError,
        match="^fixed artifact archive failed$",
    ) as failure:
        archive_helper._write_archive(io.BytesIO())

    assert "secret/private/path" not in str(failure.value)


def test_archive_releases_shared_lock_after_analysis_before_other_roots(monkeypatch):
    events: list[object] = []

    def fake_open(path: str, flags: int) -> int:
        events.append(("open", path, flags))
        return 79 if path == archive_helper.NEWSWIRE_LOCK_PATH else 73

    def fake_flock(descriptor: int, operation: int) -> None:
        events.append(("flock", descriptor, operation))

    def fake_write_archive(
        fileobj=None,
        *,
        analysis_complete,
        newswire_begin,
        newswire_complete,
    ) -> None:
        assert events[-1] == ("flock", 73, fcntl.LOCK_SH)
        events.append(("analysis_write", fileobj))
        analysis_complete()
        assert events[-1] == ("close", 73)
        events.append("readings_data_write")
        newswire_begin()
        assert events[-1] == ("flock", 79, fcntl.LOCK_SH)
        events.append("newswire_write")
        newswire_complete()

    monkeypatch.setattr(archive_helper.os, "open", fake_open)
    monkeypatch.setattr(
        archive_helper.os,
        "fstat",
        lambda descriptor: (
            _valid_newswire_lock() if descriptor == 79 else _valid_lock()
        ),
    )
    monkeypatch.setattr(archive_helper, "_lstat_lock_path", _valid_lock)
    monkeypatch.setattr(
        archive_helper,
        "_lstat_newswire_lock_path",
        _valid_newswire_lock,
    )
    monkeypatch.setattr(archive_helper, "_validate_analysis_tree", lambda: (("a",),))
    monkeypatch.setattr(archive_helper.fcntl, "flock", fake_flock)
    monkeypatch.setattr(archive_helper, "_write_archive", fake_write_archive)
    monkeypatch.setattr(
        archive_helper.os, "close", lambda descriptor: events.append(("close", descriptor))
    )

    archive_helper.archive()

    assert events == [
        (
            "open",
            "/source/analysis/private/cascade.lock",
            archive_helper._LOCK_OPEN_FLAGS,
        ),
        ("flock", 73, fcntl.LOCK_SH),
        ("analysis_write", None),
        ("close", 73),
        "readings_data_write",
        (
            "open",
            "/source/newswire/newswire.lock",
            archive_helper._LOCK_OPEN_FLAGS,
        ),
        ("flock", 79, fcntl.LOCK_SH),
        "newswire_write",
        ("close", 79),
    ]


@pytest.mark.parametrize(
    "metadata",
    [
        _valid_lock(st_mode=stat.S_IFDIR | 0o600),
        _valid_lock(st_mode=stat.S_IFREG | 0o640),
        _valid_lock(st_nlink=2),
        _valid_lock(st_uid=1000),
        _valid_lock(st_gid=1000),
    ],
)
def test_archive_rejects_unsafe_lock_before_flock_or_write(monkeypatch, metadata):
    events: list[object] = []
    monkeypatch.setattr(archive_helper.os, "open", lambda path, flags: 74)
    monkeypatch.setattr(archive_helper.os, "fstat", lambda descriptor: metadata)
    monkeypatch.setattr(
        archive_helper.fcntl,
        "flock",
        lambda descriptor, operation: events.append("flock"),
    )
    monkeypatch.setattr(
        archive_helper, "_write_archive", lambda: events.append("write")
    )
    monkeypatch.setattr(
        archive_helper.os, "close", lambda descriptor: events.append("close")
    )

    with pytest.raises(archive_helper.ArchivePreflightError):
        archive_helper.archive()

    assert events == ["close"]


def test_archive_propagates_generic_archive_writer_failure(monkeypatch):
    monkeypatch.setattr(archive_helper.os, "open", lambda path, flags: 75)
    monkeypatch.setattr(archive_helper.os, "fstat", lambda descriptor: _valid_lock())
    monkeypatch.setattr(archive_helper, "_lstat_lock_path", _valid_lock)
    monkeypatch.setattr(archive_helper, "_validate_analysis_tree", lambda: (("a",),))
    monkeypatch.setattr(archive_helper.fcntl, "flock", lambda descriptor, mode: None)
    monkeypatch.setattr(
        archive_helper,
        "_write_archive",
        lambda **kwargs: (_ for _ in ()).throw(
            archive_helper.ArchivePreflightError("fixed artifact archive failed")
        ),
    )
    monkeypatch.setattr(archive_helper.os, "close", lambda descriptor: None)

    with pytest.raises(archive_helper.ArchivePreflightError):
        archive_helper.archive()


def test_archive_rejects_replaced_lock_path_before_write(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(archive_helper.os, "open", lambda path, flags: 76)
    monkeypatch.setattr(archive_helper.os, "fstat", lambda descriptor: _valid_lock())
    monkeypatch.setattr(
        archive_helper, "_lstat_lock_path", lambda: _valid_lock(st_ino=981724)
    )
    monkeypatch.setattr(archive_helper, "_validate_analysis_tree", lambda: (("a",),))
    monkeypatch.setattr(archive_helper.fcntl, "flock", lambda descriptor, mode: None)
    monkeypatch.setattr(
        archive_helper, "_write_archive", lambda: events.append("write")
    )
    monkeypatch.setattr(archive_helper.os, "close", lambda descriptor: None)

    with pytest.raises(archive_helper.ArchivePreflightError):
        archive_helper.archive()

    assert events == []


def test_archive_rechecks_lock_path_after_write(monkeypatch):
    path_reads = iter((_valid_lock(), _valid_lock(st_ino=981724)))
    monkeypatch.setattr(archive_helper.os, "open", lambda path, flags: 77)
    monkeypatch.setattr(archive_helper.os, "fstat", lambda descriptor: _valid_lock())
    monkeypatch.setattr(archive_helper, "_lstat_lock_path", lambda: next(path_reads))
    monkeypatch.setattr(archive_helper, "_validate_analysis_tree", lambda: (("a",),))
    monkeypatch.setattr(archive_helper.fcntl, "flock", lambda descriptor, mode: None)
    monkeypatch.setattr(
        archive_helper,
        "_write_archive",
        lambda **callbacks: callbacks["analysis_complete"](),
    )
    monkeypatch.setattr(archive_helper.os, "close", lambda descriptor: None)

    with pytest.raises(archive_helper.ArchivePreflightError):
        archive_helper.archive()


def test_archive_rejects_tree_fingerprint_change_after_write(monkeypatch):
    tree_reads = iter((("before",), ("after",)))
    monkeypatch.setattr(archive_helper.os, "open", lambda path, flags: 78)
    monkeypatch.setattr(archive_helper.os, "fstat", lambda descriptor: _valid_lock())
    monkeypatch.setattr(archive_helper, "_lstat_lock_path", _valid_lock)
    monkeypatch.setattr(
        archive_helper, "_validate_analysis_tree", lambda: next(tree_reads)
    )
    monkeypatch.setattr(archive_helper.fcntl, "flock", lambda descriptor, mode: None)
    monkeypatch.setattr(
        archive_helper,
        "_write_archive",
        lambda **callbacks: callbacks["analysis_complete"](),
    )
    monkeypatch.setattr(archive_helper.os, "close", lambda descriptor: None)

    with pytest.raises(archive_helper.ArchivePreflightError):
        archive_helper.archive()


@pytest.mark.parametrize(
    "failure",
    [OSError("secret-source-name"), RecursionError("secret/deep/path")],
)
def test_main_uses_generic_error_without_private_exception_detail(
    monkeypatch, capsys, failure
):
    monkeypatch.setattr(
        archive_helper,
        "archive",
        lambda: (_ for _ in ()).throw(failure),
    )

    assert archive_helper.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "palimpsest artifact archive preflight failed\n"
    assert str(failure) not in captured.err
