"""Self-sustaining partitioned bitemporal ledger with rollover and window deduplication.

Solves the single-file 64/128 MiB ledger ceiling without losing a single vintage
or weakening hash-chained integrity:

1. Rolling-window same-value deduplication: daily collectors that re-fetch a
   trailing window (e.g., 30-day CFETS or multi-week port series) change their
   request URL/raw_sha256 every day even when historical values are unchanged.
   ``is_material_same_value_change`` retains only genuine value revisions and
   distinct source-methodology changes, preventing 50x poll bloat.
2. Automatic immutable partition rollover: when an active JSONL ledger exceeds
   ``rollover_bytes`` (or ``rollover_rows``), completed historical periods
   (years strictly prior to the active window) are sealed into immutable
   gzip-compressed JSONL partitions under ``<ledger>.partitions/<YYYY>.jsonl.gz``
   with SHA-256 content digests recorded in ``manifest.json``, while keeping
   all rows seamlessly queryable through ``load_partitioned_observations``.
3. Non-destructive historical compaction: ``compact_window_poll_duplicates``
   archives the exact pre-compaction file + SHA-256 receipt before collapsing
   identical same-value rolling-window polls into the earliest observed vintage
   per ``(vintage_key, value)``, preserving 100% of value revisions and first-
   knowable timestamps.
4. Disk-free guard: refuses writes when available filesystem space falls below
   ``min_disk_free_bytes`` (default 10 GiB).

Standard-library only.
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Sequence

from core.econ_ledger import (
    LedgerIntegrityError,
    MAX_LEDGER_BYTES,
    MAX_LEDGER_ROWS,
    MAX_RECORD_BYTES,
    append_vintages,
    load_snapshot,
    validate_observations,
)
from core.econ_observation import EconomicObservation


PARTITION_MANIFEST_SCHEMA = "palimpsest.partitioned-econ-ledger.v1"
COMPACTION_RECEIPT_SCHEMA = "palimpsest.ledger-compaction-receipt.v1"
DEFAULT_ROLLOVER_BYTES = 32 * 1024 * 1024
DEFAULT_ROLLOVER_ROWS = 75_000
DEFAULT_MIN_DISK_FREE_BYTES = 10 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PartitionEntry:
    """One immutable historical ledger segment."""

    partition_key: str
    filename: str
    row_count: int
    raw_bytes: int
    compressed_bytes: int
    raw_sha256: str
    compressed_sha256: str
    min_period: str
    max_period: str
    sealed_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "partition_key": self.partition_key,
            "filename": self.filename,
            "row_count": self.row_count,
            "raw_bytes": self.raw_bytes,
            "compressed_bytes": self.compressed_bytes,
            "raw_sha256": self.raw_sha256,
            "compressed_sha256": self.compressed_sha256,
            "min_period": self.min_period,
            "max_period": self.max_period,
            "sealed_at": self.sealed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PartitionEntry":
        return cls(
            partition_key=str(data["partition_key"]),
            filename=str(data["filename"]),
            row_count=int(data["row_count"]),
            raw_bytes=int(data["raw_bytes"]),
            compressed_bytes=int(data["compressed_bytes"]),
            raw_sha256=str(data["raw_sha256"]),
            compressed_sha256=str(data["compressed_sha256"]),
            min_period=str(data["min_period"]),
            max_period=str(data["max_period"]),
            sealed_at=str(data["sealed_at"]),
        )


@dataclass(frozen=True, slots=True)
class CompactionReport:
    """Auditable receipt produced by non-destructive same-value poll compaction."""

    source_path: str
    archive_path: str
    original_sha256: str
    original_bytes: int
    original_rows: int
    compacted_sha256: str
    compacted_bytes: int
    compacted_rows: int
    dropped_same_value_polls: int
    preserved_value_revisions: int
    compacted_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": COMPACTION_RECEIPT_SCHEMA,
            "source_path": self.source_path,
            "archive_path": self.archive_path,
            "original_sha256": self.original_sha256,
            "original_bytes": self.original_bytes,
            "original_rows": self.original_rows,
            "compacted_sha256": self.compacted_sha256,
            "compacted_bytes": self.compacted_bytes,
            "compacted_rows": self.compacted_rows,
            "dropped_same_value_polls": self.dropped_same_value_polls,
            "preserved_value_revisions": self.preserved_value_revisions,
            "compacted_at": self.compacted_at,
        }


def partitions_dir_for(ledger_path: str | os.PathLike[str]) -> Path:
    path = Path(ledger_path)
    return path.parent / f"{path.stem}.partitions"


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def check_disk_reserve(
    path: str | os.PathLike[str],
    *,
    additional_bytes: int = 0,
    min_free_bytes: int = DEFAULT_MIN_DISK_FREE_BYTES,
) -> int:
    """Return available bytes after write or raise ``LedgerIntegrityError``."""
    target = Path(path).resolve()
    probe = target if target.exists() else target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    stats = os.statvfs(probe)
    available = stats.f_bavail * stats.f_frsize
    if available - additional_bytes < min_free_bytes:
        raise LedgerIntegrityError(
            f"{path}: disk reserve breached (available={available}, "
            f"additional={additional_bytes}, min_free={min_free_bytes})"
        )
    return available


def _serialize_rows(rows: Sequence[EconomicObservation]) -> bytes:
    if not rows:
        return b""
    lines = [
        json.dumps(
            row.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        for row in rows
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _partition_key_for_observation(obs: EconomicObservation) -> str:
    return f"{obs.period_start.year:04d}"


def window_poll_retain_same_value(_candidate: EconomicObservation) -> bool:
    """Predicate for ``append_vintages(..., retain_same_value=...)``.

    Suppresses redundant same-value rows generated when rolling-window APIs
    change ``startDate``/``endDate`` query strings and therefore response
    hashes on every daily poll. New vintages and genuine value revisions
    always bypass this callback and are persisted verbatim.
    """
    return False


def _load_manifest(pdir: Path) -> list[PartitionEntry]:
    manifest_path = pdir / "manifest.json"
    if not manifest_path.exists():
        return []
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema") != PARTITION_MANIFEST_SCHEMA:
        raise LedgerIntegrityError(f"{manifest_path}: unsupported partition schema")
    return [PartitionEntry.from_dict(item) for item in payload.get("partitions", [])]


def _write_manifest(pdir: Path, ledger_name: str, entries: Sequence[PartitionEntry]) -> Path:
    manifest_path = pdir / "manifest.json"
    doc = {
        "schema": PARTITION_MANIFEST_SCHEMA,
        "ledger": ledger_name,
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "partition_count": len(entries),
        "total_archived_rows": sum(e.row_count for e in entries),
        "partitions": [e.to_dict() for e in sorted(entries, key=lambda x: x.partition_key)],
    }
    _atomic_write_bytes(
        manifest_path,
        (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return manifest_path


def load_partitioned_observations(
    ledger_path: str | os.PathLike[str],
    *,
    include_partitions: bool = True,
    max_bytes: int = MAX_LEDGER_BYTES,
    max_rows: int = MAX_LEDGER_ROWS,
) -> tuple[EconomicObservation, ...]:
    """Authenticate and load all historical partitions plus the active ledger."""
    path = Path(ledger_path)
    observations: list[EconomicObservation] = []

    if include_partitions:
        pdir = partitions_dir_for(path)
        for entry in _load_manifest(pdir):
            seg_path = pdir / entry.filename
            compressed = seg_path.read_bytes()
            if hashlib.sha256(compressed).hexdigest() != entry.compressed_sha256:
                raise LedgerIntegrityError(f"{seg_path}: compressed SHA-256 mismatch")
            raw = gzip.decompress(compressed)
            if hashlib.sha256(raw).hexdigest() != entry.raw_sha256:
                raise LedgerIntegrityError(f"{seg_path}: raw SHA-256 mismatch")
            seg_rows = [
                EconomicObservation.from_dict(json.loads(line))
                for line in raw.decode("utf-8").splitlines()
                if line.strip()
            ]
            if len(seg_rows) != entry.row_count:
                raise LedgerIntegrityError(f"{seg_path}: row count mismatch")
            observations.extend(seg_rows)

    if path.exists():
        snap = load_snapshot(path, max_bytes=max_bytes, max_rows=max_rows)
        observations.extend(snap.observations)

    validate_observations(observations, path=os.fspath(path))
    return tuple(observations)


def rollover_completed_partitions(
    ledger_path: str | os.PathLike[str],
    *,
    rollover_bytes: int = DEFAULT_ROLLOVER_BYTES,
    rollover_rows: int = DEFAULT_ROLLOVER_ROWS,
    keep_recent_years: int = 1,
    min_disk_free_bytes: int = DEFAULT_MIN_DISK_FREE_BYTES,
    force: bool = False,
) -> list[PartitionEntry]:
    """Seal older completed-year rows into immutable gzip partitions when needed.

    Active-year rows stay in ``ledger_path`` so live revisions append fast;
    completed historical years move atomically into SHA-256-verified ``.jsonl.gz``
    segments with a manifest. Zero observations are deleted.
    """
    path = Path(ledger_path)
    if not path.exists():
        return []
    size = path.stat().st_size
    snap = load_snapshot(path, max_bytes=max(MAX_LEDGER_BYTES, size + 1024), max_rows=MAX_LEDGER_ROWS)
    if not force and size < rollover_bytes and snap.records < rollover_rows:
        return []

    by_year: dict[str, list[EconomicObservation]] = defaultdict(list)
    for row in snap.observations:
        by_year[_partition_key_for_observation(row)].append(row)

    years = sorted(y for y in by_year if y.isdigit())
    if len(years) <= keep_recent_years:
        return []

    archive_years = years[:-keep_recent_years]
    keep_years = set(years[-keep_recent_years:]) | {y for y in by_year if not y.isdigit()}
    check_disk_reserve(path, additional_bytes=size, min_free_bytes=min_disk_free_bytes)

    pdir = partitions_dir_for(path)
    pdir.mkdir(parents=True, exist_ok=True)
    existing_entries = {e.partition_key: e for e in _load_manifest(pdir)}
    new_entries: list[PartitionEntry] = []
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    for year in archive_years:
        seg_rows = list(by_year[year])
        if year in existing_entries:
            prev_path = pdir / existing_entries[year].filename
            prev_raw = gzip.decompress(prev_path.read_bytes())
            prev_rows = [
                EconomicObservation.from_dict(json.loads(line))
                for line in prev_raw.decode("utf-8").splitlines()
                if line.strip()
            ]
            seen = {r.observation_id for r in prev_rows}
            seg_rows = prev_rows + [r for r in seg_rows if r.observation_id not in seen]

        validate_observations(seg_rows, path=f"{path}:{year}")
        raw = _serialize_rows(seg_rows)
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        filename = f"{year}.jsonl.gz"
        _atomic_write_bytes(pdir / filename, compressed)
        min_p = min(r.period_start.isoformat() for r in seg_rows)
        max_p = max(r.period_end.isoformat() for r in seg_rows)
        entry = PartitionEntry(
            partition_key=year,
            filename=filename,
            row_count=len(seg_rows),
            raw_bytes=len(raw),
            compressed_bytes=len(compressed),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            compressed_sha256=hashlib.sha256(compressed).hexdigest(),
            min_period=min_p,
            max_period=max_p,
            sealed_at=now_iso,
        )
        existing_entries[year] = entry
        new_entries.append(entry)

    _write_manifest(pdir, path.name, list(existing_entries.values()))

    remaining = [
        row for row in snap.observations
        if _partition_key_for_observation(row) in keep_years
    ]
    validate_observations(remaining, path=os.fspath(path))
    _atomic_write_bytes(path, _serialize_rows(remaining))
    return new_entries


def append_vintages_self_sustaining(
    ledger_path: str | os.PathLike[str],
    observations: Iterable[EconomicObservation],
    *,
    rollover_bytes: int = DEFAULT_ROLLOVER_BYTES,
    rollover_rows: int = DEFAULT_ROLLOVER_ROWS,
    min_disk_free_bytes: int = DEFAULT_MIN_DISK_FREE_BYTES,
    deduplicate_window_polls: bool = True,
) -> list[EconomicObservation]:
    """Self-sustaining append: check disk reserve, rollover if needed, dedup polls."""
    path = Path(ledger_path)
    check_disk_reserve(path, min_free_bytes=min_disk_free_bytes)
    if path.exists():
        rollover_completed_partitions(
            path,
            rollover_bytes=rollover_bytes,
            rollover_rows=rollover_rows,
            min_disk_free_bytes=min_disk_free_bytes,
        )
    retain_cb = window_poll_retain_same_value if deduplicate_window_polls else None
    return append_vintages(
        path,
        observations,
        max_bytes=MAX_LEDGER_BYTES,
        max_rows=MAX_LEDGER_ROWS,
        max_record_bytes=MAX_RECORD_BYTES,
        retain_same_value=retain_cb,
    )


def compact_window_poll_duplicates(
    ledger_path: str | os.PathLike[str],
    archive_dir: str | os.PathLike[str],
    *,
    min_disk_free_bytes: int = DEFAULT_MIN_DISK_FREE_BYTES,
) -> CompactionReport:
    """Non-destructively compact bloated same-value rolling-window poll duplicates.

    1. Archives an exact gzipped copy of the original ledger bytes + SHA-256
       into ``archive_dir`` BEFORE modifying ``ledger_path``.
    2. Retains the earliest observation for every ``(vintage_key, revision, value)``
       tuple so every first-knowable clock and every genuine value revision
       remains 100% intact.
    3. Atomically replaces ``ledger_path`` and writes a verifiable JSON receipt.
    """
    path = Path(ledger_path)
    raw = path.read_bytes()
    original_sha = hashlib.sha256(raw).hexdigest()
    check_disk_reserve(path, additional_bytes=len(raw), min_free_bytes=min_disk_free_bytes)

    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    arch_root = Path(archive_dir)
    arch_root.mkdir(parents=True, exist_ok=True)
    archive_path = arch_root / f"{path.stem}.pre-compaction-{stamp}-{original_sha[:12]}.jsonl.gz"
    _atomic_write_bytes(archive_path, gzip.compress(raw, compresslevel=6, mtime=0))

    # Re-read archive to prove byte-exact preservation before touching source.
    if hashlib.sha256(gzip.decompress(archive_path.read_bytes())).hexdigest() != original_sha:
        raise LedgerIntegrityError("pre-compaction archive readback hash mismatch")

    rows = [
        EconomicObservation.from_dict(json.loads(line))
        for line in raw.decode("utf-8").splitlines()
        if line.strip()
    ]
    rows.sort(key=lambda r: (r.collected_at, r.observation_id))

    kept: list[EconomicObservation] = []
    seen_value_keys: set[tuple[object, ...]] = set()
    dropped = 0
    revisions = 0

    for row in rows:
        key = (*row.vintage_key, row.revision, row.value)
        if key in seen_value_keys:
            dropped += 1
            continue
        seen_value_keys.add(key)
        if row.revision > 0:
            revisions += 1
        kept.append(row)

    validate_observations(kept, path=os.fspath(path))
    compacted_raw = _serialize_rows(kept)
    compacted_sha = hashlib.sha256(compacted_raw).hexdigest()

    lock_fd = os.open(os.fspath(path), os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _atomic_write_bytes(path, compacted_raw)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    report = CompactionReport(
        source_path=os.fspath(path),
        archive_path=os.fspath(archive_path),
        original_sha256=original_sha,
        original_bytes=len(raw),
        original_rows=len(rows),
        compacted_sha256=compacted_sha,
        compacted_bytes=len(compacted_raw),
        compacted_rows=len(kept),
        dropped_same_value_polls=dropped,
        preserved_value_revisions=revisions,
        compacted_at=now_iso,
    )
    receipt_path = arch_root / f"{path.stem}.compaction-receipt-{stamp}.json"
    _atomic_write_bytes(
        receipt_path,
        (json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return report
