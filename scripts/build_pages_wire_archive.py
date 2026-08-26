#!/usr/bin/env python3
"""Build and verify the deterministic Pages archive for wire analysis history.

The Git tree remains the authoritative, fully expanded evidence record.  This
release bridge runs only against the exact Pages staging tree: it archives every
analysis revision, retains each current revision beside its ``analysis.json``
alias, and removes only non-current analysis revision copies from staging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import re
import sys
import tarfile
import tempfile
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


ARCHIVE_RELATIVE_PATH = Path("news/wire/analysis-revisions.tar.xz")
RECEIPT_RELATIVE_PATH = Path("news/wire/analysis-revisions-archive.json")
INTEGRITY_RELATIVE_PATH = Path("news/wire-history-integrity.json")
CANONICAL_ARCHIVE_URL = (
    "https://palimpsest.info/news/wire/analysis-revisions.tar.xz"
)
SCHEMA_VERSION = "palimpsest-pages-wire-analysis-archive.v1"
PUBLICATION_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
EVENT_ID_RE = re.compile(r"event-[0-9a-f]{24}\Z")
ANALYSIS_ID_RE = re.compile(r"analysisv-[0-9a-f]{24}\Z")
REVISION_PATH_RE = re.compile(
    r"news/wire/event-[0-9a-f]{24}/analysis/revisions/"
    r"analysisv-[0-9a-f]{24}\.json\Z"
)
EVENT_REVISION_PATH_RE = re.compile(
    r"news/wire/event-[0-9a-f]{24}/revisions/eventv-[0-9a-f]{24}\.json\Z"
)
XZ_CHECK = "SHA-256"
XZ_MAGIC = b"\xfd7zXZ\x00"
XZ_SHA256_FLAGS = b"\x00\x0a"


class ArchiveError(RuntimeError):
    """Raised when staging cannot preserve the wire-history contract."""


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ArchiveInspection:
    entries: tuple[Entry, ...]
    expanded_bytes: int
    entry_tree_sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _pretty_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _strict_json(path: Path) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in pairs:
            if key in document:
                raise ArchiveError(f"duplicate JSON key {key!r} in {path}")
            document[key] = value
        return document

    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ArchiveError(f"non-finite JSON value {value!r} in {path}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"cannot read strict JSON from {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ArchiveError(f"expected a JSON object in {path}")
    return document


def _entry_tree(entries: Iterable[Entry]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda candidate: candidate.path):
        digest.update(
            _canonical_json(
                {
                    "path": entry.path,
                    "sha256": entry.sha256,
                    "size": entry.size,
                }
            )
        )
    return digest.hexdigest()


def _history_tree(
    analysis_entries: tuple[Entry, ...], event_entries: tuple[Entry, ...]
) -> str:
    """Recompute the canonical v1 wire-history tree from exact staged bytes."""

    rows: list[dict[str, object]] = []
    for kind, entries in (
        ("event-analysis", analysis_entries),
        ("event-dossier", event_entries),
    ):
        for entry in entries:
            parts = PurePosixPath(entry.path).parts
            rows.append(
                {
                    "path": entry.path,
                    "kind": kind,
                    "event_id": parts[2],
                    "version_id": PurePosixPath(entry.path).stem,
                    "size": entry.size,
                    "sha256": entry.sha256,
                }
            )
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda candidate: str(candidate["path"])):
        digest.update(_canonical_json(row))
    return digest.hexdigest()


def _regular_file_entry(root: Path, path: Path, *, pattern: re.Pattern[str]) -> Entry:
    if path.is_symlink() or not path.is_file():
        raise ArchiveError(f"archive input is not a regular file: {path}")
    relative = path.relative_to(root).as_posix()
    if not pattern.fullmatch(relative):
        raise ArchiveError(f"archive input has a non-canonical path: {relative}")
    return Entry(relative, path.stat().st_size, _sha256_file(path))


def _reject_wire_symlinks(wire_root: Path) -> None:
    if wire_root.is_symlink() or not wire_root.is_dir():
        raise ArchiveError(f"wire root is not a regular directory: {wire_root}")
    for parent, directory_names, file_names in os.walk(wire_root, followlinks=False):
        parent_path = Path(parent)
        for name in (*directory_names, *file_names):
            candidate = parent_path / name
            if candidate.is_symlink():
                raise ArchiveError(f"Pages wire staging refuses symlink: {candidate}")


def _revision_entries(root: Path) -> tuple[Entry, ...]:
    candidates = sorted(
        root.glob("news/wire/event-*/analysis/revisions/*.json"),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not candidates:
        raise ArchiveError("wire analysis revision set is empty")
    entries = tuple(
        _regular_file_entry(root, path, pattern=REVISION_PATH_RE)
        for path in candidates
    )
    paths = [entry.path for entry in entries]
    if len(paths) != len(set(paths)):
        raise ArchiveError("duplicate normalized analysis revision archive path")
    return entries


def _event_revision_entries(root: Path) -> tuple[Entry, ...]:
    candidates = sorted(
        root.glob("news/wire/event-*/revisions/eventv-*.json"),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    return tuple(
        _regular_file_entry(root, path, pattern=EVENT_REVISION_PATH_RE)
        for path in candidates
    )


def _head_revision_paths(
    root: Path, entries: tuple[Entry, ...]
) -> tuple[set[str], int]:
    by_event_and_digest: Counter[tuple[str, str]] = Counter()
    available_paths = {entry.path: entry for entry in entries}
    for entry in entries:
        event_name = PurePosixPath(entry.path).parts[2]
        by_event_and_digest[(event_name, entry.sha256)] += 1

    retained: set[str] = set()
    heads = sorted(
        root.glob("news/wire/event-*/analysis.json"),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not heads:
        raise ArchiveError("wire current analysis head set is empty")
    for head_path in heads:
        if head_path.is_symlink() or not head_path.is_file():
            raise ArchiveError(f"current analysis head is not a regular file: {head_path}")
        event_name = head_path.parent.name
        if not EVENT_ID_RE.fullmatch(event_name):
            raise ArchiveError(f"current analysis has invalid event path: {head_path}")
        document = _strict_json(head_path)
        analysis_id = document.get("analysis_id")
        if not isinstance(analysis_id, str) or not ANALYSIS_ID_RE.fullmatch(analysis_id):
            raise ArchiveError(f"current analysis has invalid analysis_id: {head_path}")
        expected = (
            Path("news/wire")
            / event_name
            / "analysis"
            / "revisions"
            / f"{analysis_id}.json"
        ).as_posix()
        revision = available_paths.get(expected)
        if revision is None:
            raise ArchiveError(f"current analysis revision is missing: {expected}")
        head_size = head_path.stat().st_size
        head_digest = _sha256_file(head_path)
        if revision.size != head_size or revision.sha256 != head_digest:
            raise ArchiveError(
                f"current analysis is not byte-identical to its revision: {head_path}"
            )
        matches = by_event_and_digest[(event_name, head_digest)]
        if matches != 1:
            raise ArchiveError(
                f"current analysis maps byte-identically to {matches} revisions: "
                f"{head_path}"
            )
        retained.add(expected)
    if len(retained) != len(heads):
        raise ArchiveError("multiple current analyses resolve to one revision path")
    return retained, len(heads)


def _integrity_binding(
    root: Path,
    *,
    analysis_entries: tuple[Entry, ...],
    event_entries: tuple[Entry, ...],
) -> dict[str, object]:
    path = root / INTEGRITY_RELATIVE_PATH
    if path.is_symlink() or not path.is_file():
        raise ArchiveError(f"wire history integrity receipt is missing: {path}")
    document = _strict_json(path)
    history_tree = document.get("history_tree_sha256")
    if not isinstance(history_tree, str) or not re.fullmatch(r"[0-9a-f]{64}", history_tree):
        raise ArchiveError("wire history integrity receipt has an invalid history tree")
    expected_counts = {
        "n_analysis_revisions": len(analysis_entries),
        "n_event_revisions": len(event_entries),
        "n_revisions": len(analysis_entries) + len(event_entries),
        "total_bytes": sum(entry.size for entry in analysis_entries)
        + sum(entry.size for entry in event_entries),
    }
    for field, expected in expected_counts.items():
        value = document.get(field)
        if type(value) is not int or value != expected:
            raise ArchiveError(
                f"wire history integrity receipt {field} does not match staging "
                f"({value!r} != {expected})"
            )
    if document.get("entry_algorithm") != "sha256(canonical-entry-json-lines)/v1":
        raise ArchiveError("wire history integrity receipt has an unknown tree algorithm")
    recomputed_tree = _history_tree(analysis_entries, event_entries)
    if recomputed_tree != history_tree:
        raise ArchiveError(
            "wire history integrity tree does not match the exact staged revision bytes"
        )
    return {
        "history_tree_sha256": history_tree,
        **expected_counts,
        "path": INTEGRITY_RELATIVE_PATH.as_posix(),
        "sha256": _sha256_file(path),
    }


def _tar_info(entry: Entry) -> tarfile.TarInfo:
    info = tarfile.TarInfo(entry.path)
    info.size = entry.size
    info.mtime = 0
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def _build_tar(root: Path, entries: tuple[Entry, ...], destination: Path) -> None:
    try:
        with tarfile.open(destination, "w", format=tarfile.USTAR_FORMAT) as archive:
            for entry in entries:
                source = root / PurePosixPath(entry.path)
                with source.open("rb") as handle:
                    archive.addfile(_tar_info(entry), handle)
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise ArchiveError(f"cannot build deterministic wire tar: {exc}") from exc


def _compress_xz(source: Path, destination: Path) -> None:
    """Write one deterministic in-process XZ stream with a SHA-256 check."""

    if not lzma.is_check_supported(lzma.CHECK_SHA256):
        raise ArchiveError("Python liblzma does not support the XZ SHA-256 check")
    try:
        compressor = lzma.LZMACompressor(
            format=lzma.FORMAT_XZ,
            check=lzma.CHECK_SHA256,
            preset=9,
        )
        with source.open("rb") as input_handle, destination.open("wb") as output:
            for block in iter(lambda: input_handle.read(1024 * 1024), b""):
                output.write(compressor.compress(block))
            output.write(compressor.flush())
    except (OSError, lzma.LZMAError) as exc:
        raise ArchiveError(f"cannot build deterministic XZ stream: {exc}") from exc


def _verify_xz(path: Path) -> None:
    """Verify the SHA-256 stream header and reject truncation or extra streams."""

    if not lzma.is_check_supported(lzma.CHECK_SHA256):
        raise ArchiveError("Python liblzma does not support the XZ SHA-256 check")
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
            if len(header) != 12 or header[:6] != XZ_MAGIC:
                raise ArchiveError("wire archive has an invalid XZ stream header")
            flags = header[6:8]
            expected_crc = zlib.crc32(flags).to_bytes(4, "little")
            if flags != XZ_SHA256_FLAGS or header[8:12] != expected_crc:
                raise ArchiveError("wire archive does not declare an XZ SHA-256 check")
            handle.seek(0)
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
            while not decoder.eof:
                block = handle.read(1024 * 1024) if decoder.needs_input else b""
                if not block and decoder.needs_input:
                    raise ArchiveError("wire archive contains a truncated XZ stream")
                decoder.decompress(block, max_length=1024 * 1024)
            if decoder.unused_data or handle.read(1):
                raise ArchiveError("wire archive contains trailing or multiple XZ streams")
    except ArchiveError:
        raise
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise ArchiveError(f"cannot verify wire analysis XZ stream: {exc}") from exc


def _inspect_archive(path: Path) -> ArchiveInspection:
    if path.is_symlink() or not path.is_file():
        raise ArchiveError(f"wire analysis archive is not a regular file: {path}")
    _verify_xz(path)
    entries: list[Entry] = []
    seen: set[str] = set()
    try:
        with tarfile.open(path, "r:xz") as archive:
            for member in archive:
                member_path = PurePosixPath(member.name)
                if (
                    member.name.startswith("/")
                    or ".." in member_path.parts
                    or member_path.as_posix() != member.name
                    or not REVISION_PATH_RE.fullmatch(member.name)
                ):
                    raise ArchiveError(
                        f"wire archive contains a non-canonical member: {member.name}"
                    )
                if member.name in seen:
                    raise ArchiveError(
                        f"wire archive contains duplicate member: {member.name}"
                    )
                if not member.isfile() or member.issym() or member.islnk():
                    raise ArchiveError(
                        f"wire archive member is not a regular file: {member.name}"
                    )
                if (
                    member.mtime != 0
                    or member.mode != 0o644
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname
                    or member.gname
                ):
                    raise ArchiveError(
                        f"wire archive member has non-deterministic metadata: {member.name}"
                    )
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ArchiveError(f"cannot read wire archive member: {member.name}")
                entries.append(Entry(member.name, member.size, _sha256_stream(extracted)))
                seen.add(member.name)
    except (OSError, tarfile.TarError, EOFError, lzma.LZMAError) as exc:
        raise ArchiveError(f"cannot inspect wire analysis archive: {exc}") from exc
    ordered = tuple(entries)
    if [entry.path for entry in ordered] != sorted(entry.path for entry in ordered):
        raise ArchiveError("wire archive members are not sorted deterministically")
    if not ordered:
        raise ArchiveError("wire analysis archive is empty")
    return ArchiveInspection(
        entries=ordered,
        expanded_bytes=sum(entry.size for entry in ordered),
        entry_tree_sha256=_entry_tree(ordered),
    )


def _build_receipt(
    *,
    publication_sha: str,
    archive_path: Path,
    inspection: ArchiveInspection,
    current_heads: int,
    event_entries: tuple[Entry, ...],
    integrity: dict[str, object],
) -> dict[str, object]:
    archive_sha256 = _sha256_file(archive_path)
    archive_bytes = archive_path.stat().st_size
    return {
        "schema_version": SCHEMA_VERSION,
        "publication_sha": publication_sha,
        "archive": {
            "bytes": archive_bytes,
            "compression": "xz-9-single-thread-sha256",
            "entry_count": len(inspection.entries),
            "entry_tree_sha256": inspection.entry_tree_sha256,
            "expanded_bytes": inspection.expanded_bytes,
            "media_type": "application/x-xz",
            "path": ARCHIVE_RELATIVE_PATH.as_posix(),
            "sha256": archive_sha256,
            "tar_format": "ustar-fixed-metadata-v1",
            "url": f"{CANONICAL_ARCHIVE_URL}?sha256={archive_sha256}",
        },
        "direct_access": {
            "current_analysis_head_count": current_heads,
            "current_analysis_path_pattern": "news/wire/event-*/analysis.json",
            "historical_analysis_access": "archive-member-by-exact-path",
            "removed_non_head_revision_count": len(inspection.entries) - current_heads,
            "retained_current_revision_count": current_heads,
            "retained_current_revision_path_pattern": (
                "news/wire/event-*/analysis/revisions/analysisv-*.json"
            ),
        },
        "event_revisions": {
            "entry_count": len(event_entries),
            "entry_tree_sha256": _entry_tree(event_entries),
            "expanded_bytes": sum(entry.size for entry in event_entries),
            "path_pattern": "news/wire/event-*/revisions/eventv-*.json",
            "staging_representation": "direct-files-unchanged",
        },
        "source_integrity": integrity,
        "scope": {
            "archive_member_path_pattern": (
                "news/wire/event-*/analysis/revisions/analysisv-*.json"
            ),
            "git_representation": "all-analysis-revisions-direct",
            "pages_representation": "all-in-archive-current-revisions-also-direct",
        },
        "verification": {
            "archive_member_bytes": "byte-identical-to-exact-git-staging-input",
            "current_head_closure": "exactly-one-byte-identical-revision-per-head",
            "xz_integrity_check": XZ_CHECK,
        },
    }


def _validate_publication_sha(publication_sha: str) -> None:
    if not PUBLICATION_SHA_RE.fullmatch(publication_sha):
        raise ArchiveError("publication SHA must be exactly 40 lowercase hex characters")


def build(root: Path, publication_sha: str) -> dict[str, object]:
    root = root.resolve()
    _validate_publication_sha(publication_sha)
    _reject_wire_symlinks(root / "news/wire")
    archive_path = root / ARCHIVE_RELATIVE_PATH
    receipt_path = root / RECEIPT_RELATIVE_PATH
    if archive_path.exists() or archive_path.is_symlink():
        raise ArchiveError(f"refusing to overwrite wire archive: {archive_path}")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise ArchiveError(f"refusing to overwrite wire archive receipt: {receipt_path}")

    analysis_entries = _revision_entries(root)
    event_entries_before = _event_revision_entries(root)
    retained_paths, current_heads = _head_revision_paths(root, analysis_entries)
    integrity = _integrity_binding(
        root,
        analysis_entries=analysis_entries,
        event_entries=event_entries_before,
    )

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".palimpsest-wire-archive-", dir=archive_path.parent
    ) as temp_text:
        temp_root = Path(temp_text)
        tar_path = temp_root / "analysis-revisions.tar"
        compressed_path = temp_root / "analysis-revisions.tar.xz"
        _build_tar(root, analysis_entries, tar_path)
        _compress_xz(tar_path, compressed_path)
        inspection = _inspect_archive(compressed_path)
        if inspection.entries != analysis_entries:
            raise ArchiveError("wire archive members are not byte-identical to staging")
        os.replace(compressed_path, archive_path)

    for entry in analysis_entries:
        if entry.path not in retained_paths:
            (root / PurePosixPath(entry.path)).unlink()

    retained_entries = _revision_entries(root)
    if {entry.path for entry in retained_entries} != retained_paths:
        raise ArchiveError("Pages staging retained an incorrect analysis revision set")
    if _event_revision_entries(root) != event_entries_before:
        raise ArchiveError("Pages staging changed an event revision")

    receipt = _build_receipt(
        publication_sha=publication_sha,
        archive_path=archive_path,
        inspection=inspection,
        current_heads=current_heads,
        event_entries=event_entries_before,
        integrity=integrity,
    )
    receipt_path.write_bytes(_pretty_json(receipt))
    verify(root, publication_sha)
    return receipt


def verify(root: Path, publication_sha: str) -> dict[str, object]:
    root = root.resolve()
    _validate_publication_sha(publication_sha)
    _reject_wire_symlinks(root / "news/wire")
    archive_path = root / ARCHIVE_RELATIVE_PATH
    receipt_path = root / RECEIPT_RELATIVE_PATH
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ArchiveError(f"wire archive receipt is missing: {receipt_path}")
    receipt = _strict_json(receipt_path)
    inspection = _inspect_archive(archive_path)
    retained_entries = _revision_entries(root)
    retained_paths, current_heads = _head_revision_paths(root, inspection.entries)
    if {entry.path for entry in retained_entries} != retained_paths:
        raise ArchiveError("direct analysis revisions are not exactly the current heads")
    archive_by_path = {entry.path: entry for entry in inspection.entries}
    if any(archive_by_path.get(entry.path) != entry for entry in retained_entries):
        raise ArchiveError("retained current revision differs from its archive member")
    event_entries = _event_revision_entries(root)
    integrity = _integrity_binding(
        root,
        analysis_entries=inspection.entries,
        event_entries=event_entries,
    )
    expected = _build_receipt(
        publication_sha=publication_sha,
        archive_path=archive_path,
        inspection=inspection,
        current_heads=current_heads,
        event_entries=event_entries,
        integrity=integrity,
    )
    if receipt != expected:
        raise ArchiveError("wire archive receipt does not match the staged archive")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="exact materialized Pages staging root",
    )
    parser.add_argument("--publication-sha", required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify an existing archive and receipt without modifying staging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.check:
            receipt = verify(arguments.root, arguments.publication_sha)
        else:
            receipt = build(arguments.root, arguments.publication_sha)
    except ArchiveError as exc:
        print(f"wire analysis archive refused: {exc}", file=sys.stderr)
        return 1
    archive = receipt["archive"]
    print(
        "wire-analysis-archive "
        f"entries={archive['entry_count']} expanded_bytes={archive['expanded_bytes']} "
        f"archive_bytes={archive['bytes']} sha256={archive['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
