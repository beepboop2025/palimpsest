#!/usr/bin/env python3
"""Pull the closed RSS/Atom registry and atomically publish the evidence newswire.

This command has the network binding; ``core.newswire`` itself receives an injected
byte fetcher and is fully testable offline.  A run with zero structurally successful
sources exits non-zero without touching the prior latest document or version ledger.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from core.newswire import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_PATH,
    MAX_FEED_BYTES,
    NoSuccessfulSources,
    SourceRegistry,
    canonical_json_bytes,
    collect_newswire,
    load_source_registry,
    strict_json_loads,
    validate_prior_newswire_document,
)
from core.safe_fetch import safe_fetch_bytes

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER_PATH = ROOT / "readings" / "newswire-versions.jsonl"
STATUS_SCHEMA = "palimpsest-evidence-wire-attempt.v1"
MAX_LEDGER_RECORDS = 16384
MAX_LEDGER_BYTES = 32 * 1024 * 1024
MAX_PREVIOUS_BYTES = 64 * 1024 * 1024
SNAPSHOT_SCHEMA = "palimpsest-newswire-acquisition.v1"
MAX_SNAPSHOT_MANIFEST_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
_TEMP_SUFFIX_RE = re.compile(r"^[a-z0-9_]{8}$")
_EVENT_ID_RE = re.compile(r"^event-[0-9a-f]{24}$")
_EVENT_VERSION_ID_RE = re.compile(r"^eventv-[0-9a-f]{24}$")
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EVIDENCE_STRENGTHS = {
    "measurement-corroborated",
    "primary-corroborated",
    "multi-source",
    "single-measurement-source",
    "single-primary-source",
    "single-source",
}
_LEDGER_FIELDS = frozenset(
    {
        "event_id",
        "version_id",
        "recorded_at",
        "published_at",
        "headline",
        "evidence_strength",
        "source_ids",
        "previous_version_id",
    }
)


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--now must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--now must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _load_previous(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.stat().st_size > MAX_PREVIOUS_BYTES:
        raise ValueError(f"{path} exceeds the previous-document byte cap")
    value = strict_json_loads(path.read_bytes(), label=str(path))
    # The prior edition is continuity input, not a publication candidate.  Its
    # derived lead/order state may predate the current editorial rule; all source,
    # evidence, version, coverage, and safety fields are still validated strictly.
    validate_prior_newswire_document(value)
    return value


def _load_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.stat().st_size > MAX_LEDGER_BYTES:
        raise ValueError(f"{path} exceeds the bounded-ledger byte cap")
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for line_number, raw in enumerate(path.read_bytes().splitlines(), 1):
        if not raw.strip():
            continue
        if len(raw) > 4096:
            raise ValueError(f"{path}:{line_number} exceeds the ledger-line cap")
        value = strict_json_loads(raw, label=f"{path}:{line_number}")
        _validate_ledger_record(value, f"{path}:{line_number}")
        key = (value["event_id"], value["version_id"])
        if key in seen:
            raise ValueError(f"{path}:{line_number} duplicates an event version")
        seen.add(key)
        records.append(value)
        if len(records) > MAX_LEDGER_RECORDS:
            raise ValueError(f"{path} contains more than the bounded ledger record cap")
    return records[-MAX_LEDGER_RECORDS:]


def _validate_ledger_record(value: Any, path: str) -> None:
    if type(value) is not dict or set(value) != _LEDGER_FIELDS:
        raise ValueError(f"{path} does not match the ledger contract")
    if type(value["event_id"]) is not str or not _EVENT_ID_RE.fullmatch(
        value["event_id"]
    ):
        raise ValueError(f"{path} has an invalid event_id")
    if type(value["version_id"]) is not str or not _EVENT_VERSION_ID_RE.fullmatch(
        value["version_id"]
    ):
        raise ValueError(f"{path} has an invalid version_id")
    previous = value["previous_version_id"]
    if previous is not None and (
        type(previous) is not str or not _EVENT_VERSION_ID_RE.fullmatch(previous)
    ):
        raise ValueError(f"{path} has an invalid previous_version_id")
    for field in ("recorded_at", "published_at"):
        timestamp = value[field]
        if type(timestamp) is not str or not _TIMESTAMP_RE.fullmatch(timestamp):
            raise ValueError(f"{path} has an invalid {field}")
        try:
            datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ValueError(f"{path} has an invalid {field}") from exc
    headline = value["headline"]
    if (
        type(headline) is not str
        or not headline
        or len(headline) > 240
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in headline)
    ):
        raise ValueError(f"{path} has an invalid headline")
    if value["evidence_strength"] not in _EVIDENCE_STRENGTHS:
        raise ValueError(f"{path} has an invalid evidence_strength")
    source_ids = value["source_ids"]
    if (
        type(source_ids) is not list
        or not source_ids
        or len(source_ids) > 64
        or source_ids != sorted(set(source_ids))
        or any(
            type(source_id) is not str or not _SOURCE_ID_RE.fullmatch(source_id)
            for source_id in source_ids
        )
    ):
        raise ValueError(f"{path} has invalid source_ids")


def _next_ledger(
    document: dict[str, Any], prior: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    seen = {(row["event_id"], row["version_id"]) for row in prior}
    appended: list[dict[str, Any]] = []
    for event in sorted(document["events"], key=lambda value: value["event_id"]):
        key = (event["event_id"], event["version_id"])
        if key in seen:
            continue
        appended.append(
            {
                "event_id": event["event_id"],
                "version_id": event["version_id"],
                "recorded_at": document["generated_at"],
                "published_at": event["published_at"],
                "headline": event["headline"],
                "evidence_strength": event["evidence_strength"],
                "source_ids": sorted(
                    {ref["source_id"] for ref in event["evidence_refs"]}
                ),
                "previous_version_id": event["mutation"]["previous_version_id"],
            }
        )
    return (prior + appended)[-MAX_LEDGER_RECORDS:]


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


class AcquisitionFetchError(RuntimeError):
    """Normalized transport failure retained in a replayable acquisition."""


_SNAPSHOT_FIELDS = frozenset(
    {"schema_version", "registry_sha256", "observed_at", "total_bytes", "sources"}
)
_SNAPSHOT_SOURCE_FIELDS = frozenset(
    {"source_id", "feed_url", "status", "bytes", "sha256"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _snapshot_timestamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot_blob_path(root: Path, source_id: str) -> Path:
    if not _SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError("acquisition snapshot source id is unsafe")
    return root / "blobs" / f"{source_id}.feed"


def _read_snapshot_file(path: Path, maximum: int) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise ValueError("acquisition snapshot file cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > maximum
        ):
            raise ValueError("acquisition snapshot file contract is invalid")
        chunks: list[bytes] = []
        received = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > maximum:
                raise ValueError("acquisition snapshot file exceeds its byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


class AcquisitionSnapshotWriter:
    """Capture the first and only network acquisition for deterministic replay."""

    def __init__(
        self,
        root: Path,
        registry: SourceRegistry,
        observed_at: datetime,
        network_fetch,
    ) -> None:
        self.root = root
        self.registry = registry
        self.observed_at = observed_at
        self._network_fetch = network_fetch
        self._sources_by_url = {source.feed_url: source for source in registry.sources}
        if len(self._sources_by_url) != len(registry.sources):
            raise ValueError("acquisition snapshot requires unique source URLs")
        self._records: dict[str, dict[str, Any]] = {}
        self._seen: set[str] = set()
        self._total_bytes = 0
        self._fatal: BaseException | None = None
        self._lock = threading.Lock()
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        os.chmod(root, 0o700)
        (root / "blobs").mkdir(mode=0o700)

    def __call__(self, url: str, **kwargs: Any) -> bytes:
        source = self._sources_by_url.get(url)
        if source is None:
            self._fatal = ValueError("collector requested a URL outside the snapshot registry")
            raise AcquisitionFetchError("acquisition snapshot rejected an unknown URL")
        with self._lock:
            if source.id in self._seen:
                self._fatal = ValueError("collector requested one snapshot source more than once")
                raise AcquisitionFetchError("acquisition snapshot rejected a duplicate fetch")
            self._seen.add(source.id)
        try:
            raw = self._network_fetch(url, **kwargs)
        except Exception as exc:
            with self._lock:
                self._records[source.id] = {
                    "source_id": source.id,
                    "feed_url": source.feed_url,
                    "status": "fetch_error",
                    "bytes": 0,
                    "sha256": None,
                }
            raise AcquisitionFetchError("source acquisition failed") from exc
        if type(raw) is not bytes or not raw or len(raw) > MAX_FEED_BYTES:
            with self._lock:
                self._records[source.id] = {
                    "source_id": source.id,
                    "feed_url": source.feed_url,
                    "status": "fetch_error",
                    "bytes": 0,
                    "sha256": None,
                }
            raise AcquisitionFetchError("source acquisition returned invalid bytes")
        try:
            with self._lock:
                if self._total_bytes + len(raw) > MAX_SNAPSHOT_BYTES:
                    raise ValueError("acquisition snapshot exceeds its total byte limit")
                self._total_bytes += len(raw)
            _atomic_write(
                _snapshot_blob_path(self.root, source.id),
                raw,
                mode=0o600,
            )
            record = {
                "source_id": source.id,
                "feed_url": source.feed_url,
                "status": "success",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            with self._lock:
                self._records[source.id] = record
            return raw
        except Exception as exc:
            self._fatal = exc
            raise AcquisitionFetchError("acquisition snapshot could not retain bytes") from exc

    def finalize(self) -> None:
        if self._fatal is not None:
            raise ValueError("acquisition snapshot capture failed") from self._fatal
        expected = {source.id for source in self.registry.sources}
        if self._seen != expected or set(self._records) != expected:
            raise ValueError("acquisition snapshot did not observe every registry source")
        document = {
            "schema_version": SNAPSHOT_SCHEMA,
            "registry_sha256": self.registry.sha256,
            "observed_at": _snapshot_timestamp(self.observed_at),
            "total_bytes": self._total_bytes,
            "sources": [self._records[source_id] for source_id in sorted(self._records)],
        }
        rendered = canonical_json_bytes(document) + b"\n"
        if len(rendered) > MAX_SNAPSHOT_MANIFEST_BYTES:
            raise ValueError("acquisition snapshot manifest exceeds its byte limit")
        _atomic_write(self.root / "manifest.json", rendered, mode=0o600)


class AcquisitionSnapshotReader:
    """Replay exact retained source outcomes; this class has no network path."""

    def __init__(self, root: Path, registry: SourceRegistry) -> None:
        self.root = root
        self.registry = registry
        self._fatal: BaseException | None = None
        self._seen: set[str] = set()
        try:
            root_metadata = root.lstat()
        except OSError as exc:
            raise ValueError("acquisition snapshot directory is unavailable") from exc
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
            or root_metadata.st_gid != os.getegid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise ValueError("acquisition snapshot directory contract is invalid")
        raw_manifest = _read_snapshot_file(
            root / "manifest.json", MAX_SNAPSHOT_MANIFEST_BYTES
        )
        manifest = strict_json_loads(raw_manifest, label="acquisition snapshot manifest")
        if type(manifest) is not dict or set(manifest) != _SNAPSHOT_FIELDS:
            raise ValueError("acquisition snapshot manifest fields do not match")
        if (
            manifest["schema_version"] != SNAPSHOT_SCHEMA
            or manifest["registry_sha256"] != registry.sha256
        ):
            raise ValueError("acquisition snapshot does not match the source registry")
        observed_at = _parse_now(manifest["observed_at"])
        if _snapshot_timestamp(observed_at) != manifest["observed_at"]:
            raise ValueError("acquisition snapshot clock is not normalized")
        self.observed_at = observed_at
        rows = manifest["sources"]
        if type(rows) is not list or len(rows) != len(registry.sources):
            raise ValueError("acquisition snapshot source count does not match")
        self._records: dict[str, dict[str, Any]] = {}
        expected_by_id = {source.id: source for source in registry.sources}
        total_bytes = 0
        expected_blobs: set[str] = set()
        for row in rows:
            if type(row) is not dict or set(row) != _SNAPSHOT_SOURCE_FIELDS:
                raise ValueError("acquisition snapshot source fields do not match")
            source = expected_by_id.get(row["source_id"])
            if (
                source is None
                or row["source_id"] in self._records
                or row["feed_url"] != source.feed_url
                or row["status"] not in {"success", "fetch_error"}
            ):
                raise ValueError("acquisition snapshot source identity is invalid")
            if row["status"] == "success":
                if (
                    type(row["bytes"]) is not int
                    or row["bytes"] < 1
                    or row["bytes"] > MAX_FEED_BYTES
                    or type(row["sha256"]) is not str
                    or not _SHA256_RE.fullmatch(row["sha256"])
                ):
                    raise ValueError("acquisition snapshot success receipt is invalid")
                total_bytes += row["bytes"]
                expected_blobs.add(f"{source.id}.feed")
            elif row["bytes"] != 0 or row["sha256"] is not None:
                raise ValueError("acquisition snapshot failure receipt is invalid")
            self._records[source.id] = row
        if (
            set(self._records) != set(expected_by_id)
            or type(manifest["total_bytes"]) is not int
            or manifest["total_bytes"] != total_bytes
            or total_bytes > MAX_SNAPSHOT_BYTES
        ):
            raise ValueError("acquisition snapshot total does not match its receipts")
        try:
            blobs_metadata = (root / "blobs").lstat()
            if (
                not stat.S_ISDIR(blobs_metadata.st_mode)
                or blobs_metadata.st_uid != os.geteuid()
                or blobs_metadata.st_gid != os.getegid()
                or stat.S_IMODE(blobs_metadata.st_mode) != 0o700
            ):
                raise ValueError("acquisition snapshot blob directory is unsafe")
            actual_root = {entry.name for entry in root.iterdir()}
            actual_blobs = {entry.name for entry in (root / "blobs").iterdir()}
        except OSError as exc:
            raise ValueError("acquisition snapshot inventory cannot be read") from exc
        if actual_root != {"manifest.json", "blobs"} or actual_blobs != expected_blobs:
            raise ValueError("acquisition snapshot inventory contains unexpected paths")
        self._sources_by_url = {source.feed_url: source for source in registry.sources}

    def __call__(self, url: str, **_kwargs: Any) -> bytes:
        source = self._sources_by_url.get(url)
        if source is None or source.id in self._seen:
            self._fatal = ValueError("snapshot replay received an invalid source request")
            raise AcquisitionFetchError("snapshot replay rejected a source request")
        self._seen.add(source.id)
        record = self._records[source.id]
        if record["status"] == "fetch_error":
            raise AcquisitionFetchError("source acquisition failed")
        try:
            raw = _read_snapshot_file(
                _snapshot_blob_path(self.root, source.id), MAX_FEED_BYTES
            )
            if (
                len(raw) != record["bytes"]
                or hashlib.sha256(raw).hexdigest() != record["sha256"]
            ):
                raise ValueError("snapshot replay bytes do not match their receipt")
            return raw
        except Exception as exc:
            self._fatal = exc
            raise AcquisitionFetchError("snapshot replay validation failed") from exc

    def finalize(self) -> None:
        if self._fatal is not None:
            raise ValueError("acquisition snapshot replay failed") from self._fatal
        if self._seen != set(self._records):
            raise ValueError("acquisition snapshot replay did not consume every source")


def _reconcile_atomic_temporaries(paths: Sequence[Path | None]) -> None:
    """Remove only abandoned files created by this producer's atomic writes."""

    seen: set[tuple[Path, str]] = set()
    for path in paths:
        if path is None:
            continue
        parent = path.parent
        if not parent.exists():
            continue
        prefix = f".{path.name}."
        try:
            parent_descriptor = os.open(
                parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except OSError as exc:
            raise ValueError("newswire temporary directory is unsafe") from exc
        try:
            for name in os.listdir(parent_descriptor):
                key = (parent, name)
                suffix = name.removeprefix(prefix)
                if (
                    key in seen
                    or not name.startswith(prefix)
                    or not _TEMP_SUFFIX_RE.fullmatch(suffix)
                ):
                    continue
                seen.add(key)
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise ValueError(
                        "newswire temporary artifact cannot be inspected"
                    ) from exc
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_gid != os.getegid()
                    or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o644}
                ):
                    raise ValueError("newswire temporary artifact is unsafe")
                try:
                    os.unlink(name, dir_fd=parent_descriptor)
                except OSError as exc:
                    raise ValueError(
                        "newswire temporary artifact cannot be reconciled"
                    ) from exc
        finally:
            os.close(parent_descriptor)


def _ledger_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(record) + b"\n" for record in records)


def _utc_stamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _failure_class(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) else "Exception"


def _last_good_receipt(
    output: Path, previous: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    if previous is None or not output.is_file():
        return None, None
    generated_at = previous.get("generated_at")
    return (
        generated_at if isinstance(generated_at, str) else None,
        hashlib.sha256(output.read_bytes()).hexdigest(),
    )


def _write_status(
    path: Path | None,
    *,
    attempted_at: str,
    status: str,
    fresh_sources: int | None,
    output_generated_at: str | None,
    output_sha256: str | None,
    failure_class: str | None,
) -> None:
    if path is None:
        return
    receipt = {
        "schema_version": STATUS_SCHEMA,
        "attempted_at": attempted_at,
        "completed_at": None if status == "running" else _utc_stamp(),
        "status": status,
        "fresh_sources": fresh_sources,
        "output_generated_at": output_generated_at,
        "output_sha256": output_sha256,
        "failure_class": failure_class,
    }
    _atomic_write(
        path,
        json.dumps(
            receipt,
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("ascii")
        + b"\n",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument(
        "--status",
        type=Path,
        help="atomic per-attempt status receipt (required by the scheduled service)",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        help="persistent exclusive attempt lock (required by the scheduled service)",
    )
    parser.add_argument("--workers", type=int, default=6)
    snapshot = parser.add_mutually_exclusive_group()
    snapshot.add_argument(
        "--snapshot-out",
        type=Path,
        help="new private directory for exact acquisition bytes used by race replays",
    )
    snapshot.add_argument(
        "--snapshot-in",
        type=Path,
        help="validated private acquisition directory to replay without network access",
    )
    parser.add_argument(
        "--now", help="fixed ISO-8601 clock for reproducible/offline runs"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock_descriptor: int | None = None
    if args.lock is not None:
        args.lock.parent.mkdir(parents=True, exist_ok=True)
        lock_descriptor = os.open(
            args.lock,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        metadata = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(lock_descriptor)
            raise ValueError("newswire attempt lock contract is invalid")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)

    try:
        if lock_descriptor is not None:
            _reconcile_atomic_temporaries((args.output, args.ledger, args.status))
        return _main_locked(args)
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)


def _main_locked(args: argparse.Namespace) -> int:
    """Run one complete attempt while the caller retains the exclusive lease."""

    attempted_at = _utc_stamp()
    previous: dict[str, Any] | None = None
    last_generated_at: str | None = None
    last_sha256: str | None = None

    # Publish the in-flight state before reading or fetching any inputs.  The
    # analysis timer must never mistake the prior successful receipt for the
    # outcome of an attempt that is still able to replace the wire underneath
    # its snapshot.
    _write_status(
        args.status,
        attempted_at=attempted_at,
        status="running",
        fresh_sources=None,
        output_generated_at=None,
        output_sha256=None,
        failure_class=None,
    )

    try:
        previous = _load_previous(args.output)
        last_generated_at, last_sha256 = _last_good_receipt(args.output, previous)
        registry = load_source_registry(args.config)
        prior_ledger = _load_ledger(args.ledger)
        snapshot_transport: AcquisitionSnapshotWriter | AcquisitionSnapshotReader | None
        if args.snapshot_in is not None:
            snapshot_transport = AcquisitionSnapshotReader(args.snapshot_in, registry)
            now = snapshot_transport.observed_at
            if args.now is not None and _parse_now(args.now) != now:
                raise ValueError("--now does not match the acquisition snapshot clock")
            fetch = snapshot_transport
        else:
            now = _parse_now(args.now)
            proxy = os.environ.get("PALIMPSEST_PROXY") or None

            def network_fetch(url: str, **kwargs: Any) -> bytes:
                return safe_fetch_bytes(url, proxy=proxy, **kwargs)

            if args.snapshot_out is not None:
                snapshot_transport = AcquisitionSnapshotWriter(
                    args.snapshot_out,
                    registry,
                    now,
                    network_fetch,
                )
                fetch = snapshot_transport
            else:
                snapshot_transport = None
                fetch = network_fetch

        try:
            document = collect_newswire(
                registry,
                fetch,
                now=now,
                previous=previous,
                max_workers=args.workers,
            )
        finally:
            if snapshot_transport is not None:
                snapshot_transport.finalize()
    except NoSuccessfulSources as exc:
        _write_status(
            args.status,
            attempted_at=attempted_at,
            status="no-fresh-sources",
            fresh_sources=0,
            output_generated_at=last_generated_at,
            output_sha256=last_sha256,
            failure_class=_failure_class(exc),
        )
        print(f"newswire: {exc}; prior latest and ledger preserved")
        return 2
    except Exception as exc:
        _write_status(
            args.status,
            attempted_at=attempted_at,
            status="failed",
            fresh_sources=None,
            output_generated_at=last_generated_at,
            output_sha256=last_sha256,
            failure_class=_failure_class(exc),
        )
        raise

    coverage = document.get("coverage")
    counts = coverage.get("counts") if isinstance(coverage, dict) else None
    fresh_sources = counts.get("success") if isinstance(counts, dict) else None
    if type(fresh_sources) is not int or fresh_sources < 0:
        exc = ValueError("newswire coverage is missing a valid fresh-source count")
        _write_status(
            args.status,
            attempted_at=attempted_at,
            status="failed",
            fresh_sources=None,
            output_generated_at=last_generated_at,
            output_sha256=last_sha256,
            failure_class=_failure_class(exc),
        )
        raise exc
    if fresh_sources == 0:
        _write_status(
            args.status,
            attempted_at=attempted_at,
            status="no-fresh-sources",
            fresh_sources=0,
            output_generated_at=last_generated_at,
            output_sha256=last_sha256,
            failure_class="NoFreshSources",
        )
        print("newswire: zero fresh sources; prior latest and ledger preserved")
        return 2

    try:
        ledger = _next_ledger(document, prior_ledger)
        # The bounded ledger lands before latest.  A crash can therefore leave an
        # unused version receipt, but never a public latest version with no
        # corresponding receipt.
        _atomic_write(args.ledger, _ledger_bytes(ledger))
        rendered = (
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        _atomic_write(args.output, rendered)
        _write_status(
            args.status,
            attempted_at=attempted_at,
            status="success",
            fresh_sources=fresh_sources,
            output_generated_at=document["generated_at"],
            output_sha256=hashlib.sha256(rendered).hexdigest(),
            failure_class=None,
        )
    except Exception as exc:
        _write_status(
            args.status,
            attempted_at=attempted_at,
            status="failed",
            fresh_sources=fresh_sources,
            output_generated_at=last_generated_at,
            output_sha256=last_sha256,
            failure_class=_failure_class(exc),
        )
        raise
    print(
        "newswire: "
        f"{document['n_items']} items -> {document['n_events']} events; "
        f"{coverage['successful_sources']}/{coverage['registry_sources']} sources; "
        f"coverage={coverage['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
