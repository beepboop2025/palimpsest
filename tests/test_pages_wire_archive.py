from __future__ import annotations

import hashlib
import io
import json
import lzma
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


def _rights_status(
    root: Path,
    quarantined_paths: list[str],
    *,
    publication_sha: str = PUBLICATION_SHA,
) -> dict[str, object]:
    policy = {
        "default_decision": "deny",
        "policy_scope": "china_economic_values_and_seiche_export",
        "schema_version": "palimpsest.china-economic-source-policy.v1",
    }
    policy_raw = _json_bytes(policy)
    _write(root / "config/china_econ_source_policy.json", policy_raw)
    paths = sorted(quarantined_paths)
    status: dict[str, object] = {
        "artifact": {
            "media_type": "application/json",
            "path": wire_archive.RIGHTS_STATUS_RELATIVE_PATH.as_posix(),
        },
        "availability": "unavailable",
        "counts": {
            "allowed_records": 0,
            "input_records": 0,
            "published_records": 0,
            "quarantined_artifacts": len(paths),
            "restricted_records": 0,
        },
        "limitations": [
            "No denied source value is published.",
            "Unavailable evidence is not zero.",
            "This status conveys no observation authority.",
        ],
        "policy": {
            "bytes": len(policy_raw),
            "default_decision": "deny",
            "path": "config/china_econ_source_policy.json",
            "policy_scope": "china_economic_values_and_seiche_export",
            "schema_version": "palimpsest.china-economic-source-policy.v1",
            "sha256": hashlib.sha256(policy_raw).hexdigest(),
        },
        "publication_allowed": False,
        "publication_sha": publication_sha,
        "quarantined_paths": paths,
        "reason": "Fixture source policy denies publication.",
        "rights_evaluated_at": "2026-08-26T00:00:00Z",
        "schema_version": wire_archive.RIGHTS_STATUS_SCHEMA,
        "source_decisions": [{"source_id": "fixture_denied"}],
        "status": "restricted",
    }
    _write(root / wire_archive.RIGHTS_STATUS_RELATIVE_PATH, _json_bytes(status))
    return status


def _apply_restricted_stubs(
    root: Path, status: dict[str, object], restricted: list[str]
) -> None:
    status_raw = (root / wire_archive.RIGHTS_STATUS_RELATIVE_PATH).read_bytes()
    counts = status["counts"]
    assert isinstance(counts, dict)
    for relative in restricted:
        _write(
            root / relative,
            _json_bytes(
                {
                    "artifact": {
                        "media_type": "application/json",
                        "path": relative,
                    },
                    "availability": "unavailable",
                    "counts": {
                        "input_records": counts["input_records"],
                        "published_records": 0,
                        "restricted_records": counts["restricted_records"],
                    },
                    "limitations": status["limitations"],
                    "master_status": {
                        "bytes": len(status_raw),
                        "path": "/"
                        + wire_archive.RIGHTS_STATUS_RELATIVE_PATH.as_posix(),
                        "sha256": hashlib.sha256(status_raw).hexdigest(),
                    },
                    "policy": status["policy"],
                    "publication_allowed": False,
                    "publication_sha": status["publication_sha"],
                    "reason": status["reason"],
                    "rights_evaluated_at": status["rights_evaluated_at"],
                    "schema_version": wire_archive.RIGHTS_ENDPOINT_STATUS_SCHEMA,
                    "status": "restricted",
                }
            ),
        )


def _wire_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted((root / "news/wire").rglob("*"))
        if path.is_file()
    }


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


def test_pages_api_archives_when_rights_status_has_no_wire_restriction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "archived"
    revisions = _fixture(root)
    _rights_status(root, ["readings/restricted-economic-endpoint.json"])

    result = wire_archive.build_for_pages(root, PUBLICATION_SHA)

    assert result["mode"] == "archived"
    assert result["publication_sha"] == PUBLICATION_SHA
    assert result["archive"]["entry_count"] == len(revisions)
    assert (root / wire_archive.ARCHIVE_RELATIVE_PATH).is_file()
    assert (root / wire_archive.RECEIPT_RELATIVE_PATH).is_file()
    assert wire_archive.verify_for_pages(root, PUBLICATION_SHA) == result


def test_pages_api_suppresses_archive_and_preserves_direct_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "suppressed"
    _fixture(root)
    restricted = [
        f"news/wire/{EVENT_A}/analysis.json",
        f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_A}.json",
        f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_OLD}.json",
    ]
    status = _rights_status(root, restricted)
    _apply_restricted_stubs(root, status, restricted)
    before = _wire_file_bytes(root)

    result = wire_archive.build_for_pages(root, PUBLICATION_SHA)

    assert result == {
        "mode": "rights-suppressed",
        "publication_sha": PUBLICATION_SHA,
        "reason": "canonical-rights-status-quarantines-wire-analysis",
        "restricted_wire_path_count": 3,
        "rights_status": {
            "bytes": (
                root / wire_archive.RIGHTS_STATUS_RELATIVE_PATH
            ).stat().st_size,
            "path": wire_archive.RIGHTS_STATUS_RELATIVE_PATH.as_posix(),
            "sha256": hashlib.sha256(
                (root / wire_archive.RIGHTS_STATUS_RELATIVE_PATH).read_bytes()
            ).hexdigest(),
        },
        "suppressed_outputs": {
            "archive": wire_archive.ARCHIVE_RELATIVE_PATH.as_posix(),
            "receipt": wire_archive.RECEIPT_RELATIVE_PATH.as_posix(),
        },
    }
    assert not (root / wire_archive.ARCHIVE_RELATIVE_PATH).exists()
    assert not (root / wire_archive.RECEIPT_RELATIVE_PATH).exists()
    assert _wire_file_bytes(root) == before
    assert wire_archive.verify_for_pages(root, PUBLICATION_SHA) == result
    assert wire_archive.build_for_pages(root, PUBLICATION_SHA) == result


@pytest.mark.parametrize("output", ["archive", "receipt"])
def test_pages_suppression_refuses_preexisting_outputs(
    tmp_path: Path, output: str
) -> None:
    root = tmp_path / output
    _fixture(root)
    restricted = [
        f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_OLD}.json"
    ]
    status = _rights_status(root, restricted)
    _apply_restricted_stubs(root, status, restricted)
    relative = (
        wire_archive.ARCHIVE_RELATIVE_PATH
        if output == "archive"
        else wire_archive.RECEIPT_RELATIVE_PATH
    )
    _write(root / relative, b"preexisting\n")

    with pytest.raises(wire_archive.ArchiveError, match="refuses preexisting"):
        wire_archive.build_for_pages(root, PUBLICATION_SHA)
    with pytest.raises(wire_archive.ArchiveError, match="refuses preexisting"):
        wire_archive.verify_for_pages(root, PUBLICATION_SHA)


def test_pages_api_refuses_missing_malformed_or_mismatched_rights_status(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    _fixture(missing)
    with pytest.raises(wire_archive.ArchiveError, match="not a regular file"):
        wire_archive.build_for_pages(missing, PUBLICATION_SHA)

    malformed = tmp_path / "malformed"
    _fixture(malformed)
    _write(malformed / wire_archive.RIGHTS_STATUS_RELATIVE_PATH, b"{\n")
    with pytest.raises(wire_archive.ArchiveError, match="strict JSON"):
        wire_archive.build_for_pages(malformed, PUBLICATION_SHA)

    noncanonical = tmp_path / "noncanonical"
    _fixture(noncanonical)
    status = _rights_status(noncanonical, [])
    (noncanonical / wire_archive.RIGHTS_STATUS_RELATIVE_PATH).write_text(
        json.dumps(status, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(wire_archive.ArchiveError, match="not canonical JSON"):
        wire_archive.build_for_pages(noncanonical, PUBLICATION_SHA)

    mismatch = tmp_path / "mismatch"
    _fixture(mismatch)
    _rights_status(mismatch, [], publication_sha="2" * 40)
    with pytest.raises(wire_archive.ArchiveError, match="different publication SHA"):
        wire_archive.build_for_pages(mismatch, PUBLICATION_SHA)


def test_pages_api_refuses_incoherent_wire_quarantine_closure(
    tmp_path: Path,
) -> None:
    head_only = tmp_path / "head-only"
    _fixture(head_only)
    restricted_head = [f"news/wire/{EVENT_A}/analysis.json"]
    status = _rights_status(head_only, restricted_head)
    _apply_restricted_stubs(head_only, status, restricted_head)
    with pytest.raises(wire_archive.ArchiveError, match="lacks a restricted revision"):
        wire_archive.build_for_pages(head_only, PUBLICATION_SHA)

    current_revision_only = tmp_path / "current-revision-only"
    _fixture(current_revision_only)
    restricted_revision = [
        f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_A}.json"
    ]
    status = _rights_status(current_revision_only, restricted_revision)
    _apply_restricted_stubs(current_revision_only, status, restricted_revision)
    with pytest.raises(
        wire_archive.ArchiveError,
        match="unrestricted current analysis points to a restricted revision",
    ):
        wire_archive.build_for_pages(current_revision_only, PUBLICATION_SHA)

    noncanonical = tmp_path / "noncanonical-wire"
    _fixture(noncanonical)
    invalid_path = "news/wire/not-an-event/analysis.json"
    _rights_status(noncanonical, [invalid_path])
    with pytest.raises(wire_archive.ArchiveError, match="non-canonical wire head"):
        wire_archive.build_for_pages(noncanonical, PUBLICATION_SHA)


def test_pages_api_refuses_invalid_quarantine_list_and_cli_reports_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "invalid-list"
    _fixture(invalid)
    status = _rights_status(
        invalid,
        [
            f"news/wire/{EVENT_A}/analysis.json",
            f"news/wire/{EVENT_B}/analysis.json",
        ],
    )
    status["quarantined_paths"] = list(reversed(status["quarantined_paths"]))
    (invalid / wire_archive.RIGHTS_STATUS_RELATIVE_PATH).write_bytes(
        _json_bytes(status)
    )
    with pytest.raises(wire_archive.ArchiveError, match="not sorted and unique"):
        wire_archive.verify_for_pages(invalid, PUBLICATION_SHA)

    suppressed = tmp_path / "cli"
    _fixture(suppressed)
    restricted = [
        f"news/wire/{EVENT_A}/analysis.json",
        f"news/wire/{EVENT_A}/analysis/revisions/{ANALYSIS_A}.json",
    ]
    status = _rights_status(suppressed, restricted)
    _apply_restricted_stubs(suppressed, status, restricted)
    assert wire_archive.main(
        ["--root", str(suppressed), "--publication-sha", PUBLICATION_SHA]
    ) == 0
    assert capsys.readouterr().out == (
        "wire-analysis-archive mode=rights-suppressed "
        f"publication_sha={PUBLICATION_SHA} restricted_wire_paths=2\n"
    )
    assert wire_archive.main(
        [
            "--root",
            str(suppressed),
            "--publication-sha",
            PUBLICATION_SHA,
            "--check",
        ]
    ) == 0


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


def test_build_refuses_symlinks_and_xz_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    def fail_compressor(**_kwargs: object) -> None:
        raise lzma.LZMAError("synthetic compressor failure")

    monkeypatch.setattr(wire_archive.lzma, "LZMACompressor", fail_compressor)
    with pytest.raises(wire_archive.ArchiveError, match="cannot build deterministic"):
        wire_archive.build(failed_xz_root, PUBLICATION_SHA)


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
        expected = "cannot verify|cannot inspect"
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
    archive_path.write_bytes(
        lzma.compress(
            tar_path.read_bytes(),
            format=lzma.FORMAT_XZ,
            check=lzma.CHECK_SHA256,
            preset=9,
        )
    )
    with pytest.raises(wire_archive.ArchiveError, match="duplicate member"):
        wire_archive._inspect_archive(archive_path)
