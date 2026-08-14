"""Fail-closed storage helpers for aggregate economic observations.

The public economic-observation artifact is JSONL because it is append-only and
can grow independently of the compact pulse.  This module is the single trust
boundary for that file: readers get bounded parsing plus cross-row invariant
checks, while writers take an advisory file lock around the complete
read/decide/append transaction.

Only :class:`core.econ_observation.EconomicObservation` records are accepted.
That contract is deliberately aggregate-only and rejects respondent, person,
device, and company identifiers in metadata.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, Sequence

from core.econ_observation import EconomicObservation


MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_ROWS = 1_000_000
MAX_RECORD_BYTES = 1024 * 1024


class LedgerIntegrityError(RuntimeError):
    """The ledger cannot be trusted as a complete observation history."""


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """One validated view of the exact bytes stored in a JSONL ledger."""

    observations: tuple[EconomicObservation, ...]
    byte_size: int
    byte_sha256: str

    @property
    def records(self) -> int:
        return len(self.observations)

    @property
    def as_of(self) -> datetime | None:
        """Newest collection clock in this snapshot, or ``None`` if empty."""

        return snapshot_as_of(self.observations)


def _positive_limit(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _read_bounded(descriptor: int, path: str, max_bytes: int) -> bytes:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise LedgerIntegrityError(f"{path}: ledger must be a regular file")
    if info.st_size > max_bytes:
        raise LedgerIntegrityError(
            f"{path}: ledger is {info.st_size} bytes; limit is {max_bytes}"
        )
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > max_bytes:
        raise LedgerIntegrityError(f"{path}: ledger exceeds {max_bytes} bytes")
    return raw


def _decode_rows(
    raw: bytes,
    path: str,
    *,
    max_rows: int,
    max_record_bytes: int,
) -> list[EconomicObservation]:
    if raw and not raw.endswith(b"\n"):
        raise LedgerIntegrityError(
            f"{path}: append-only ledger does not end at a record boundary"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerIntegrityError(f"{path}: ledger is not valid UTF-8") from exc

    lines = text.splitlines()
    if len(lines) > max_rows:
        raise LedgerIntegrityError(
            f"{path}: ledger has more than the {max_rows} permitted records"
        )

    rows: list[EconomicObservation] = []
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            raise LedgerIntegrityError(f"{path}:{lineno}: blank JSONL record")
        if len(line.encode("utf-8")) > max_record_bytes:
            raise LedgerIntegrityError(
                f"{path}:{lineno}: record exceeds {max_record_bytes} bytes"
            )
        try:
            encoded = json.loads(
                line,
                object_pairs_hook=_no_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number {value}")
                ),
            )
            if not isinstance(encoded, dict):
                raise TypeError("record must be an object")
            if "observation_id" not in encoded:
                raise ValueError("observation_id is required")
            supplied_id = encoded["observation_id"]
            row = EconomicObservation.from_dict(encoded)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, RecursionError) as exc:
            raise LedgerIntegrityError(
                f"{path}:{lineno}: invalid economic observation: {exc}"
            ) from exc
        if supplied_id != row.observation_id:
            raise LedgerIntegrityError(
                f"{path}:{lineno}: observation_id does not match record contents"
            )
        rows.append(row)

    validate_observations(rows, path=path)
    return rows


def validate_observations(
    observations: Sequence[EconomicObservation], *, path: str = "<memory>"
) -> None:
    """Enforce invariants which cannot be expressed by a row-level schema.

    A source's revision counter advances exactly once for a value change.  A
    changed transport hash, evidence URL, quality annotation, or method may be
    retained as another provenance vintage of the same value and revision.
    """

    seen_ids: set[str] = set()
    latest: dict[tuple[object, ...], EconomicObservation] = {}
    series_contracts: dict[tuple[str, ...], tuple[str, str]] = {}
    status_rank = {"forecast": 0, "estimate": 1, "observed": 2}
    previous_collection: datetime | None = None
    for position, candidate in enumerate(observations, 1):
        if not isinstance(candidate, EconomicObservation):
            raise LedgerIntegrityError(
                f"{path}:{position}: row is not an EconomicObservation"
            )
        # Re-serialize to close the mutable-metadata escape hatch before using
        # the identity or accepting the row into a trusted snapshot.
        try:
            row = EconomicObservation.from_dict(candidate.to_dict())
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise LedgerIntegrityError(f"{path}:{position}: invalid row: {exc}") from exc
        if row.observation_id in seen_ids:
            raise LedgerIntegrityError(
                f"{path}:{position}: duplicate observation_id {row.observation_id}"
            )
        seen_ids.add(row.observation_id)

        if previous_collection is not None and row.collected_at < previous_collection:
            raise LedgerIntegrityError(
                f"{path}:{position}: collected_at moves backwards in append order"
            )
        previous_collection = row.collected_at

        # Multiple independent sources may report the same canonical series,
        # so source_id belongs in the contract key.  Within one source's series
        # slice, however, a new unit or frequency requires a new series_id; a
        # silent change would make comparisons and revisions dimensionally
        # invalid.
        contract_key = (*row.slice_key, row.source_id)
        contract = (row.unit, row.frequency)
        established = series_contracts.get(contract_key)
        if established is not None and contract != established:
            raise LedgerIntegrityError(
                f"{path}:{position}: unit/frequency drift for source series slice; "
                f"expected {established!r}, got {contract!r}"
            )
        series_contracts[contract_key] = contract

        prior = latest.get(row.vintage_key)
        if prior is None and row.revision != 0:
            raise LedgerIntegrityError(
                f"{path}:{position}: first source vintage must use revision 0, "
                f"got {row.revision}"
            )
        if prior is not None:
            if row.released_at < prior.released_at:
                raise LedgerIntegrityError(
                    f"{path}:{position}: released_at moves backwards within "
                    "a source vintage"
                )
            if status_rank[row.status] < status_rank[prior.status]:
                raise LedgerIntegrityError(
                    f"{path}:{position}: status moves backwards from "
                    f"{prior.status} to {row.status}"
                )
            if row.revision < prior.revision:
                raise LedgerIntegrityError(
                    f"{path}:{position}: source revision moves backwards"
                )
            value_changed = row.value != prior.value
            expected = prior.revision + (1 if value_changed else 0)
            if row.revision != expected:
                change = "value-changing" if value_changed else "same-value provenance"
                raise LedgerIntegrityError(
                    f"{path}:{position}: {change} vintage must use revision "
                    f"{expected}, got {row.revision}"
                )
        latest[row.vintage_key] = row


def _snapshot_from_raw(
    raw: bytes,
    path: str,
    *,
    max_rows: int,
    max_record_bytes: int,
) -> LedgerSnapshot:
    rows = _decode_rows(
        raw, path, max_rows=max_rows, max_record_bytes=max_record_bytes
    )
    return LedgerSnapshot(
        observations=tuple(rows),
        byte_size=len(raw),
        byte_sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_snapshot(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = MAX_LEDGER_BYTES,
    max_rows: int = MAX_LEDGER_ROWS,
    max_record_bytes: int = MAX_RECORD_BYTES,
) -> LedgerSnapshot:
    """Load a bounded, locked snapshot and authenticate every row and revision."""

    max_bytes = _positive_limit(max_bytes, "max_bytes")
    max_rows = _positive_limit(max_rows, "max_rows")
    max_record_bytes = _positive_limit(max_record_bytes, "max_record_bytes")
    location = os.fspath(path)
    try:
        descriptor = os.open(location, os.O_RDONLY)
    except FileNotFoundError:
        return LedgerSnapshot((), 0, hashlib.sha256(b"").hexdigest())
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        raw = _read_bounded(descriptor, location, max_bytes)
        return _snapshot_from_raw(
            raw,
            location,
            max_rows=max_rows,
            max_record_bytes=max_record_bytes,
        )
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def load_observations(
    path: str | os.PathLike[str],
    **limits: int,
) -> list[EconomicObservation]:
    """Compatibility convenience returning the validated rows as a list."""

    return list(load_snapshot(path, **limits).observations)


def snapshot_as_of(observations: Iterable[EconomicObservation]) -> datetime | None:
    """Return the newest collection clock without depending on input order."""

    clocks = [row.collected_at for row in observations]
    return max(clocks) if clocks else None


def snapshot_digest(observations: Iterable[EconomicObservation]) -> str:
    """Digest a logical observation set in deterministic identity order.

    This differs intentionally from :attr:`LedgerSnapshot.byte_sha256`: the
    former authenticates a logical result set (useful for an as-of query), while
    the latter authenticates the exact published JSONL bytes.
    """

    records = sorted(
        (row.to_dict() for row in observations), key=lambda row: row["observation_id"]
    )
    payload = json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def observations_as_of(
    observations: Iterable[EconomicObservation], decision_time: datetime
) -> list[EconomicObservation]:
    """Return the latest knowable vintage for every source/series/period."""

    if not isinstance(decision_time, datetime):
        raise TypeError("decision_time must be a datetime")
    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise ValueError("decision_time must be timezone-aware")
    latest: dict[tuple[object, ...], EconomicObservation] = {}
    for row in observations:
        if row.released_at > decision_time or row.collected_at > decision_time:
            continue
        prior = latest.get(row.vintage_key)
        if prior is None or (
            row.released_at,
            row.collected_at,
            row.revision,
            row.observation_id,
        ) > (
            prior.released_at,
            prior.collected_at,
            prior.revision,
            prior.observation_id,
        ):
            latest[row.vintage_key] = row
    return sorted(
        latest.values(),
        key=lambda row: (
            row.period_end,
            row.series_id,
            row.geography,
            row.sector,
            row.firm_size,
            row.ownership,
            row.source_id,
        ),
    )


def _provenance_signature(row: EconomicObservation) -> str:
    body = row.to_dict()
    for key in ("observation_id", "value", "revision", "released_at", "collected_at"):
        body.pop(key)
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _normalized_rows(observations: Iterable[EconomicObservation]) -> list[EconomicObservation]:
    rows: list[EconomicObservation] = []
    for position, row in enumerate(observations, 1):
        if not isinstance(row, EconomicObservation):
            raise TypeError(f"observation {position} must be an EconomicObservation")
        rows.append(EconomicObservation.from_dict(row.to_dict()))
    return sorted(
        rows,
        key=lambda row: (
            row.collected_at,
            row.released_at,
            row.period_start,
            row.period_end,
            row.series_id,
            row.geography,
            row.sector,
            row.firm_size,
            row.ownership,
            row.source_id,
            row.observation_id,
        ),
    )


def _serialize(rows: Iterable[EconomicObservation]) -> bytes:
    return "".join(
        json.dumps(row.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    ).encode("utf-8")


def _append_transaction(
    path: str | os.PathLike[str],
    observations: Iterable[EconomicObservation],
    *,
    assign_revisions: bool,
    max_bytes: int,
    max_rows: int,
    max_record_bytes: int,
) -> list[EconomicObservation]:
    max_bytes = _positive_limit(max_bytes, "max_bytes")
    max_rows = _positive_limit(max_rows, "max_rows")
    max_record_bytes = _positive_limit(max_record_bytes, "max_record_bytes")
    candidates = _normalized_rows(observations)
    if not candidates:
        return []

    location = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(location))
    os.makedirs(parent, exist_ok=True)
    descriptor = os.open(location, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        raw = _read_bounded(descriptor, location, max_bytes)
        snapshot = _snapshot_from_raw(
            raw,
            location,
            max_rows=max_rows,
            max_record_bytes=max_record_bytes,
        )
        combined = list(snapshot.observations)
        seen_ids = {row.observation_id for row in combined}
        latest: dict[tuple[object, ...], EconomicObservation] = {}
        for row in combined:
            latest[row.vintage_key] = row

        pending: list[EconomicObservation] = []
        for candidate in candidates:
            if candidate.observation_id in seen_ids:
                continue
            prior = latest.get(candidate.vintage_key)
            row = candidate
            if assign_revisions:
                if prior is None:
                    row = replace(candidate, revision=0)
                elif candidate.value != prior.value:
                    row = replace(candidate, revision=prior.revision + 1)
                elif _provenance_signature(candidate) != _provenance_signature(prior):
                    row = replace(candidate, revision=prior.revision)
                else:
                    # A later poll clock alone is not a new economic vintage.
                    continue
            if row.observation_id in seen_ids:
                continue
            pending.append(row)
            combined.append(row)
            seen_ids.add(row.observation_id)
            latest[row.vintage_key] = row

        validate_observations(combined, path=location)
        if len(combined) > max_rows:
            raise LedgerIntegrityError(
                f"{location}: append would exceed the {max_rows} record limit"
            )
        payload = _serialize(pending)
        if any(len(line) > max_record_bytes + 1 for line in payload.splitlines(True)):
            raise LedgerIntegrityError(
                f"{location}: append contains a record over {max_record_bytes} bytes"
            )
        if len(raw) + len(payload) > max_bytes:
            raise LedgerIntegrityError(
                f"{location}: append would exceed the {max_bytes} byte limit"
            )

        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise LedgerIntegrityError(f"{location}: append made no progress")
            offset += written
        if payload:
            os.fsync(descriptor)
            if not raw:
                directory_descriptor = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        return pending
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def append_observations(
    path: str | os.PathLike[str],
    observations: Iterable[EconomicObservation],
    *,
    max_bytes: int = MAX_LEDGER_BYTES,
    max_rows: int = MAX_LEDGER_ROWS,
    max_record_bytes: int = MAX_RECORD_BYTES,
) -> list[EconomicObservation]:
    """Append already revisioned rows after a locked full-ledger validation."""

    return _append_transaction(
        path,
        observations,
        assign_revisions=False,
        max_bytes=max_bytes,
        max_rows=max_rows,
        max_record_bytes=max_record_bytes,
    )


def append_vintages(
    path: str | os.PathLike[str],
    observations: Iterable[EconomicObservation],
    *,
    max_bytes: int = MAX_LEDGER_BYTES,
    max_rows: int = MAX_LEDGER_ROWS,
    max_record_bytes: int = MAX_RECORD_BYTES,
) -> list[EconomicObservation]:
    """Append candidates while assigning value revisions inside the file lock.

    Same-value candidates are retained only when material provenance changes;
    those rows keep the current revision.  A changed value advances the
    source/vintage revision exactly once.
    """

    return _append_transaction(
        path,
        observations,
        assign_revisions=True,
        max_bytes=max_bytes,
        max_rows=max_rows,
        max_record_bytes=max_record_bytes,
    )


__all__ = [
    "MAX_LEDGER_BYTES",
    "MAX_LEDGER_ROWS",
    "MAX_RECORD_BYTES",
    "LedgerIntegrityError",
    "LedgerSnapshot",
    "append_observations",
    "append_vintages",
    "load_observations",
    "load_snapshot",
    "observations_as_of",
    "snapshot_as_of",
    "snapshot_digest",
    "validate_observations",
]
