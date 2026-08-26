from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import build_pages_wire_archive as wire_archive


PUBLICATION_SHA = "1" * 40
EVENT_A = "event-" + "a" * 24
EVENT_B = "event-" + "b" * 24
ANALYSIS_A = "analysisv-" + "c" * 24
ANALYSIS_OLD = "analysisv-" + "d" * 24
ANALYSIS_B = "analysisv-" + "e" * 24


def _json_bytes(document: object) -> bytes:
    return (
        json.dumps(document, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
        + b"\n"
    )


def _write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _fixture(root: Path) -> dict[str, bytes]:
    head_a = _json_bytes({"analysis_id": ANALYSIS_A, "summary": "current a"})
    old_a = _json_bytes({"analysis_id": ANALYSIS_OLD, "summary": "old a"})
    head_b = _json_bytes({"analysis_id": ANALYSIS_B, "summary": "current b"})
    revisions = {
        f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_A}.json": head_a,
        f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_OLD}.json": old_a,
        f"news/wire/{EVENT_B}/analysis/revisions/{ANALYSIS_B}.json": head_b,
    }
    for relative, raw in revisions.items():
        _write(root / relative, raw)
    _write(root / f"news/wire/{EVENT_A}/analysis.json", head_a)
    _write(root / f"news/wire/{EVENT_B}/analysis.json", head_b)
    _write(
        root / f"news/wire/{EVENT_A}/revisions/eventv-{'f' * 24}.json",
        b'{"event":"a"}\n',
    )
    _write(
        root / f"news/wire/{EVENT_B}/revisions/eventv-{'0' * 24}.json",
        b'{"event":"b"}\n',
    )
    analysis_entries = wire_archive._revision_entries(root)
    event_entries = wire_archive._event_revision_entries(root)
    _write(
        root / "news/wire-history-integrity.json",
        _json_bytes(
            {
                "schema_version": "palimpsest-wire-history-integrity.v1",
                "entry_algorithm": "sha256(canonical-entry-json-lines)/v1",
                "history_tree_sha256": wire_archive._history_tree(
                    analysis_entries, event_entries
                ),
                "n_analysis_revisions": len(analysis_entries),
                "n_event_revisions": len(event_entries),
                "n_revisions": len(analysis_entries) + len(event_entries),
                "total_bytes": sum(entry.size for entry in analysis_entries)
                + sum(entry.size for entry in event_entries),
            }
        ),
    )
    return revisions


def _archive_member_bytes(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:xz") as archive:
        return {
            member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in archive
        }


def test_build_is_deterministic_and_preserves_complete_history(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    revisions = _fixture(first_root)
    _fixture(second_root)

    first = wire_archive.build(first_root, PUBLICATION_SHA)
    wire_archive.build(second_root, PUBLICATION_SHA)

    first_archive = first_root / wire_archive.ARCHIVE_RELATIVE_PATH
    second_archive = second_root / wire_archive.ARCHIVE_RELATIVE_PATH
    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert (
        first_root / wire_archive.RECEIPT_RELATIVE_PATH
    ).read_bytes() == (
        second_root / wire_archive.RECEIPT_RELATIVE_PATH
    ).read_bytes()
    assert _archive_member_bytes(first_archive) == revisions

    assert first["publication_sha"] == PUBLICATION_SHA
    assert first["archive"]["entry_count"] == 3
    assert first["archive"]["expanded_bytes"] == sum(map(len, revisions.values()))
    assert first["archive"]["url"].endswith(
        "?sha256=" + first["archive"]["sha256"]
    )
    integrity = json.loads(
        (first_root / "news/wire-history-integrity.json").read_text(encoding="utf-8")
    )
    assert (
        first["source_integrity"]["history_tree_sha256"]
        == integrity["history_tree_sha256"]
    )
    assert first["direct_access"] == {
        "current_analysis_head_count": 2,
        "current_analysis_path_pattern": "news/wire/event-*/analysis.json",
        "historical_analysis_access": "archive-member-by-exact-path",
        "removed_non_head_revision_count": 1,
        "retained_current_revision_count": 2,
        "retained_current_revision_path_pattern": (
            "news/wire/event-*/analysis/revisions/analysisv-*.json"
        ),
    }

    assert not (
        first_root
        / f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_OLD}.json"
    ).exists()
    for event, analysis_id in ((EVENT_A, ANALYSIS_A), (EVENT_B, ANALYSIS_B)):
        assert (
            first_root / f"news/wire/{event}/analysis/revisions/{analysis_id}.json"
        ).read_bytes() == (first_root / f"news/wire/{event}/analysis.json").read_bytes()
    assert len(list(first_root.glob("news/wire/event-*/revisions/eventv-*.json"))) == 2
    assert wire_archive.verify(first_root, PUBLICATION_SHA) == first


def test_build_refuses_ambiguous_or_missing_current_revision(tmp_path: Path) -> None:
    ambiguous = tmp_path / "ambiguous"
    revisions = _fixture(ambiguous)
    head_raw = revisions[
        f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_A}.json"
    ]
    (ambiguous / f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_OLD}.json").write_bytes(
        head_raw
    )
    with pytest.raises(wire_archive.ArchiveError, match="maps byte-identically to 2"):
        wire_archive.build(ambiguous, PUBLICATION_SHA)

    missing = tmp_path / "missing"
    _fixture(missing)
    (missing / f"news/wire/{EVENT_A}/analysis.json").write_bytes(
        _json_bytes({"analysis_id": "analysisv-" + "8" * 24})
    )
    with pytest.raises(wire_archive.ArchiveError, match="revision is missing"):
        wire_archive.build(missing, PUBLICATION_SHA)


def test_build_refuses_symlinks_and_xz_failure(tmp_path: Path) -> None:
    symlink_root = tmp_path / "symlink"
    _fixture(symlink_root)
    source = symlink_root / f"news/wire/{EVENT_A}/analysis.json"
    link = symlink_root / f"news/wire/{EVENT_A}/untrusted-link"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(wire_archive.ArchiveError, match="refuses symlink"):
        wire_archive.build(symlink_root, PUBLICATION_SHA)

    failed_xz_root = tmp_path / "failed-xz"
    _fixture(failed_xz_root)
    false_binary = shutil.which("false")
    assert false_binary is not None
    with pytest.raises(wire_archive.ArchiveError, match="xz failed"):
        wire_archive.build(failed_xz_root, PUBLICATION_SHA, xz_binary=false_binary)


def test_build_recomputes_the_authoritative_history_tree(tmp_path: Path) -> None:
    byte_tamper_root = tmp_path / "byte-tamper"
    _fixture(byte_tamper_root)
    old_path = (
        byte_tamper_root
        / f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_OLD}.json"
    )
    old_path.write_bytes(old_path.read_bytes().replace(b"old a", b"old z"))
    with pytest.raises(wire_archive.ArchiveError, match="tree does not match"):
        wire_archive.build(byte_tamper_root, PUBLICATION_SHA)

    count_tamper_root = tmp_path / "count-tamper"
    _fixture(count_tamper_root)
    integrity_path = count_tamper_root / "news/wire-history-integrity.json"
    integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    integrity["n_event_revisions"] += 1
    integrity_path.write_bytes(_json_bytes(integrity))
    with pytest.raises(
        wire_archive.ArchiveError,
        match="n_event_revisions does not match staging",
    ):
        wire_archive.build(count_tamper_root, PUBLICATION_SHA)


@pytest.mark.parametrize("tamper_target", ["archive", "receipt"])
def test_verify_refuses_archive_or_receipt_tamper(
    tmp_path: Path, tamper_target: str
) -> None:
    root = tmp_path / tamper_target
    _fixture(root)
    wire_archive.build(root, PUBLICATION_SHA)
    if tamper_target == "archive":
        archive_path = root / wire_archive.ARCHIVE_RELATIVE_PATH
        raw = bytearray(archive_path.read_bytes())
        raw[len(raw) // 2] ^= 1
        archive_path.write_bytes(raw)
        expected = "xz failed|cannot inspect"
    else:
        receipt_path = root / wire_archive.RECEIPT_RELATIVE_PATH
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["archive"]["bytes"] += 1
        receipt_path.write_bytes(_json_bytes(receipt))
        expected = "receipt does not match"
    with pytest.raises(wire_archive.ArchiveError, match=expected):
        wire_archive.verify(root, PUBLICATION_SHA)


def test_inspection_refuses_duplicate_archive_members(tmp_path: Path) -> None:
    member_name = (
        f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_A}.json"
    )
    tar_path = tmp_path / "duplicate.tar"
    with tarfile.open(tar_path, "w", format=tarfile.USTAR_FORMAT) as archive:
        for _ in range(2):
            info = tarfile.TarInfo(member_name)
            info.size = 2
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(b"{}"))
    archive_path = tmp_path / "duplicate.tar.xz"
    with archive_path.open("wb") as output:
        completed = subprocess.run(
            [
                "xz",
                "-9",
                "--threads=1",
                "--check=sha256",
                "--stdout",
                str(tar_path),
            ],
            check=False,
            stdout=output,
            stderr=subprocess.PIPE,
        )
    assert completed.returncode == 0, completed.stderr
    with pytest.raises(wire_archive.ArchiveError, match="duplicate member"):
        wire_archive._inspect_archive(archive_path, xz_binary="xz")
