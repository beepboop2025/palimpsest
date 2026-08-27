#!/usr/bin/env python3
"""Privately acquire or rights-gated replay UCDP 26.1 annual evidence.

Network access is explicit and private-only. Public replay requires all three
exact ZIPs, their canonical receipt sidecars, the captured rights page and its
receipt, an approved repository-reviewed lock, and explicit publication/current
clock checks. Raw acquisition evidence never receives a public default path.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from collectors.ucdp_bulk import (
    DEFAULT_PUBLIC_SCHEMA,
    MAX_RECEIPT_BYTES,
    MAX_RIGHTS_SNAPSHOT_BYTES,
    UCDPAcquisitionReceipt,
    UCDPBulkError,
    UCDPRightsSnapshotReceipt,
    build_bundle,
    fetch_archive,
    fetch_rights_snapshot,
    load_registry,
    load_review_lock,
    verify_acquisition_receipt,
)
from core.governance import RateCeiling
from core.ucdp_aggregate import (
    UCDPAggregateError,
    canonical_json_bytes,
    canonical_public_bytes,
    parse_timestamp,
    sha256_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "ucdp_aggregate.json"
DEFAULT_REVIEW_LOCK = ROOT / "config" / "ucdp_acquisition_lock.json"
MAX_DERIVED_BYTES = 8 * 1024 * 1024
INPUT_ORDER = ("armed_conflict", "actor_registry", "organized_country_year")
RIGHTS_SNAPSHOT_NAME = "rights-page.snapshot.html"
RIGHTS_SNAPSHOT_RECEIPT_NAME = "rights-page.receipt.json"


def _add_registry_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)


def _add_review_arguments(parser: argparse.ArgumentParser) -> None:
    _add_registry_arguments(parser)
    parser.add_argument(
        "--input-dir",
        type=Path,
        help="offline directory containing exact ZIPs and canonical receipts",
    )
    parser.add_argument(
        "--publication-at",
        help="explicit canonical UTC as-of clock required for public evidence replay",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check",
        help="validate adapter state or publication-eligible reviewed evidence",
    )
    _add_review_arguments(check)
    archival = commands.add_parser(
        "archive-check",
        help="validate internally consistent private evidence without publication authority",
    )
    _add_registry_arguments(archival)
    archival.add_argument("--input-dir", type=Path, required=True)
    acquire = commands.add_parser(
        "acquire",
        help="explicitly capture private evidence; never build a public artifact",
    )
    _add_registry_arguments(acquire)
    acquire.add_argument(
        "--fetch",
        action="store_true",
        help="required explicit network opt-in",
    )
    acquire.add_argument("--evidence-output-dir", type=Path, required=True)
    build = commands.add_parser("build", help="build one rights-approved public artifact")
    _add_review_arguments(build)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument(
        "--replace-derived",
        action="store_true",
        help="explicitly replace a differing derived review artifact",
    )
    return parser


def _absolute_without_symlinks(path: Path, *, label: str) -> Path:
    try:
        absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise UCDPBulkError(f"cannot resolve {label}: {exc}") from exc
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise UCDPBulkError(f"cannot inspect {label}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise UCDPBulkError(f"{label} path must not contain symlink components")
    return absolute


def _read_bounded(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    candidate = _absolute_without_symlinks(path, label=label)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise UCDPBulkError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise UCDPBulkError(f"{label} must be a regular non-symlink file")
    if not 0 < metadata.st_size <= maximum_bytes:
        raise UCDPBulkError(f"{label} is empty or exceeds {maximum_bytes} bytes")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise UCDPBulkError(f"cannot open {label}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise UCDPBulkError(f"{label} changed while opening")
            raw = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise UCDPBulkError(f"cannot read {label}: {exc}") from exc
    if not raw or len(raw) > maximum_bytes:
        raise UCDPBulkError(f"{label} is empty or exceeds {maximum_bytes} bytes")
    return raw


def _is_within(path: Path, parent: Path) -> bool:
    candidate = _absolute_without_symlinks(path, label="path containment candidate")
    boundary = _absolute_without_symlinks(parent, label="path containment boundary")
    try:
        candidate.relative_to(boundary)
    except ValueError:
        return False
    return True


def _validate_arguments(args: argparse.Namespace) -> None:
    registry = _absolute_without_symlinks(args.registry, label="registry")
    if args.command == "acquire" and not args.fetch:
        raise UCDPBulkError("acquire requires explicit --fetch")
    if args.command == "build" and args.input_dir is None:
        raise UCDPBulkError("public build requires --input-dir")
    if args.command in {"build", "check"}:
        if (args.input_dir is None) != (args.publication_at is None):
            raise UCDPBulkError(
                "--input-dir and --publication-at are required together"
            )
        _absolute_without_symlinks(DEFAULT_REVIEW_LOCK, label="review lock")
    output = getattr(args, "output", None)
    if output is not None:
        output = _absolute_without_symlinks(output, label="derived output")
        if output == registry:
            raise UCDPBulkError("derived output must not alias the registry")
        for label, directory in (
            ("input directory", getattr(args, "input_dir", None)),
            (
                "evidence output directory",
                getattr(args, "evidence_output_dir", None),
            ),
        ):
            if directory is not None and _is_within(output, directory):
                raise UCDPBulkError(f"derived output must remain outside {label}")
    for label, directory in (
        ("input directory", getattr(args, "input_dir", None)),
        (
            "evidence output directory",
            getattr(args, "evidence_output_dir", None),
        ),
    ):
        if directory is not None:
            candidate = _absolute_without_symlinks(directory, label=label)
            if candidate == registry or candidate == registry.parent:
                raise UCDPBulkError(f"{label} must not alias the registry")


def _evidence_paths(directory: Path, input_id: str) -> tuple[Path, Path]:
    root = _absolute_without_symlinks(directory, label="evidence directory")
    return root / f"{input_id}.zip", root / f"{input_id}.receipt.json"


def _rights_evidence_paths(directory: Path) -> tuple[Path, Path]:
    root = _absolute_without_symlinks(directory, label="evidence directory")
    return root / RIGHTS_SNAPSHOT_NAME, root / RIGHTS_SNAPSHOT_RECEIPT_NAME


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_parent(path: Path, *, private: bool, label: str) -> Path:
    candidate = _absolute_without_symlinks(path, label=label)
    try:
        candidate.parent.mkdir(
            parents=True, exist_ok=True, mode=0o700 if private else 0o755
        )
    except OSError as exc:
        raise UCDPBulkError(f"cannot create {label} parent: {exc}") from exc
    _absolute_without_symlinks(candidate.parent, label=f"{label} parent")
    if private:
        try:
            mode = stat.S_IMODE(candidate.parent.stat().st_mode)
        except OSError as exc:
            raise UCDPBulkError(
                f"cannot inspect private evidence directory: {exc}"
            ) from exc
        if mode & 0o077:
            raise UCDPBulkError(
                "evidence output directory must not grant group or other permissions"
            )
    return candidate


def _preflight_immutable(path: Path, payload: bytes, *, label: str) -> bool:
    candidate = _absolute_without_symlinks(path, label=label)
    if not candidate.exists():
        return False
    existing = _read_bounded(candidate, maximum_bytes=len(payload), label=label)
    if existing != payload:
        raise UCDPBulkError(
            f"{label} already exists with different bytes; evidence is immutable"
        )
    mode = stat.S_IMODE(candidate.stat().st_mode)
    if mode & 0o077:
        raise UCDPBulkError(f"{label} must not grant group or other permissions")
    return True


def _write_immutable(path: Path, payload: bytes, *, label: str) -> str:
    if type(payload) is not bytes or not payload:
        raise UCDPBulkError(f"{label} payload must be non-empty bytes")
    if _preflight_immutable(path, payload, label=label):
        return "identical"
    candidate = _prepare_parent(path, private=True, label=label)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags, 0o600)
    except FileExistsError:
        if _preflight_immutable(candidate, payload, label=label):
            return "identical"
        raise AssertionError("immutable preflight must return or raise")
    except OSError as exc:
        raise UCDPBulkError(f"cannot create {label}: {exc}") from exc
    created = True
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        _fsync_directory(candidate.parent)
        created = False
    finally:
        if created:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
    return "created"


def _write_derived(path: Path, payload: bytes, *, replace_existing: bool) -> str:
    if type(payload) is not bytes or not payload or len(payload) > MAX_DERIVED_BYTES:
        raise UCDPBulkError("derived output is empty or exceeds its byte bound")
    candidate = _absolute_without_symlinks(path, label="derived output")
    existed = candidate.exists()
    if existed:
        existing = _read_bounded(
            candidate,
            maximum_bytes=MAX_DERIVED_BYTES,
            label="derived output",
        )
        if existing == payload:
            return "identical"
        if not replace_existing:
            raise UCDPBulkError(
                "derived output differs; pass --replace-derived to authorize replacement"
            )
    candidate = _prepare_parent(candidate, private=False, label="derived output")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{candidate.name}.", suffix=".tmp", dir=candidate.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        _absolute_without_symlinks(candidate, label="derived output")
        os.replace(temporary, candidate)
        _fsync_directory(candidate.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return "replaced" if existed else "created"


def _offline_evidence(registry, directory: Path):
    archives: dict[str, bytes] = {}
    receipts: dict[str, UCDPAcquisitionReceipt] = {}
    maximum_age = registry.source["maximum_source_age_days"]
    for input_id in INPUT_ORDER:
        spec = registry.inputs[input_id]
        archive_path, receipt_path = _evidence_paths(directory, input_id)
        archive = _read_bounded(
            archive_path,
            maximum_bytes=spec.maximum_archive_bytes,
            label=f"{input_id} ZIP",
        )
        raw_receipt = _read_bounded(
            receipt_path,
            maximum_bytes=MAX_RECEIPT_BYTES,
            label=f"{input_id} receipt",
        )
        receipt = verify_acquisition_receipt(
            raw_receipt,
            archive=archive,
            spec=spec,
            maximum_source_age_days=maximum_age,
        )
        archives[input_id] = archive
        receipts[input_id] = receipt
    snapshot_path, snapshot_receipt_path = _rights_evidence_paths(directory)
    snapshot = _read_bounded(
        snapshot_path,
        maximum_bytes=MAX_RIGHTS_SNAPSHOT_BYTES,
        label="rights-page snapshot",
    )
    raw_snapshot_receipt = _read_bounded(
        snapshot_receipt_path,
        maximum_bytes=MAX_RECEIPT_BYTES,
        label="rights-page snapshot receipt",
    )
    snapshot_receipt = UCDPRightsSnapshotReceipt.from_bytes(raw_snapshot_receipt)
    if (
        snapshot_receipt.snapshot_sha256 != sha256_bytes(snapshot)
        or snapshot_receipt.snapshot_bytes != len(snapshot)
    ):
        raise UCDPBulkError("rights-page snapshot is not bound to its receipt")
    return archives, receipts, snapshot, snapshot_receipt


def _post_response_clock() -> datetime:
    return datetime.now(UTC)


def _live_evidence(registry):
    archives: dict[str, bytes] = {}
    receipts: dict[str, UCDPAcquisitionReceipt] = {}
    maximum_age = registry.source["maximum_source_age_days"]
    ceiling = RateCeiling(rate=0.2, capacity=1.0)
    for input_id in INPUT_ORDER:
        fetched = fetch_archive(
            registry.inputs[input_id],
            maximum_source_age_days=maximum_age,
            clock=_post_response_clock,
            rate_ceiling=ceiling,
        )
        archives[input_id] = fetched.archive
        receipts[input_id] = fetched.receipt
    rights = fetch_rights_snapshot(clock=_post_response_clock)
    return archives, receipts, rights.snapshot, rights.receipt


def _persist_evidence(
    directory: Path,
    archives,
    receipts,
    rights_snapshot: bytes,
    rights_snapshot_receipt: UCDPRightsSnapshotReceipt,
) -> None:
    planned: list[tuple[Path, bytes, str]] = []
    for input_id in INPUT_ORDER:
        archive_path, receipt_path = _evidence_paths(directory, input_id)
        planned.extend(
            [
                (archive_path, archives[input_id], f"{input_id} ZIP"),
                (
                    receipt_path,
                    canonical_json_bytes(receipts[input_id].to_dict()),
                    f"{input_id} receipt",
                ),
            ]
        )
    snapshot_path, snapshot_receipt_path = _rights_evidence_paths(directory)
    planned.extend(
        [
            (snapshot_path, rights_snapshot, "rights-page snapshot"),
            (
                snapshot_receipt_path,
                canonical_json_bytes(rights_snapshot_receipt.to_dict()),
                "rights-page snapshot receipt",
            ),
        ]
    )
    for path, payload, label in planned:
        _prepare_parent(path, private=True, label=label)
        _preflight_immutable(path, payload, label=label)
    for path, payload, label in planned:
        _write_immutable(path, payload, label=label)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _validate_arguments(args)
        registry = load_registry(args.registry)
        if args.command == "acquire":
            archives, receipts, rights_snapshot, rights_receipt = _live_evidence(
                registry
            )
            _persist_evidence(
                args.evidence_output_dir,
                archives,
                receipts,
                rights_snapshot,
                rights_receipt,
            )
            print(
                "ucdp-bulk: private acquisition captured; no public artifact built "
                f"inputs={len(archives)} rights_snapshot={rights_receipt.snapshot_sha256}"
            )
            return 0
        if args.command == "archive-check":
            archives, receipts, rights_snapshot, rights_receipt = _offline_evidence(
                registry,
                args.input_dir,
            )
            print(
                "ucdp-bulk: private archival evidence internally consistent; "
                "not publication-authorized "
                f"inputs={len(archives)} rights_snapshot={rights_receipt.snapshot_sha256}"
            )
            return 0

        # Publication authority is the repository-controlled lock only.  A
        # caller-supplied path would turn a self-issued file into approval.
        review_lock = load_review_lock(DEFAULT_REVIEW_LOCK)
        if args.input_dir is None:
            print(
                "ucdp-bulk: adapter configuration valid "
                f"inputs={len(registry.inputs)} version={registry.source['dataset_version']} "
                f"review_lock={review_lock.status}"
            )
            return 0
        archives, receipts, rights_snapshot, rights_receipt = _offline_evidence(
            registry,
            args.input_dir,
        )
        publication_at = parse_timestamp(
            args.publication_at,
            label="publication_at",
        )
        bundle = build_bundle(
            registry,
            archives=archives,
            receipts=receipts,
            review_lock=review_lock,
            rights_snapshot=rights_snapshot,
            rights_snapshot_receipt=rights_receipt,
            publication_at=publication_at,
            current_at=_post_response_clock(),
        )
        payload = canonical_public_bytes(
            bundle,
            schema_path=DEFAULT_PUBLIC_SCHEMA,
            forbidden_values=(),
        )
        if args.command == "build":
            disposition = _write_derived(
                args.output,
                payload,
                replace_existing=args.replace_derived,
            )
            print(
                "ucdp-bulk: built "
                f"bundle={bundle.bundle_id} conflicts={len(bundle.conflict_years)} "
                f"country_years={len(bundle.country_years)} "
                f"output={args.output} disposition={disposition}"
            )
        else:
            print(
                "ucdp-bulk: reviewed evidence publication-eligible "
                f"bundle={bundle.bundle_id} conflicts={len(bundle.conflict_years)} "
                f"country_years={len(bundle.country_years)}"
            )
        return 0
    except (OSError, TypeError, UCDPAggregateError, UCDPBulkError) as exc:
        print(f"ucdp-bulk: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
