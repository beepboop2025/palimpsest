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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from core.newswire import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_PATH,
    NoSuccessfulSources,
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
DEFAULT_LOCK_NAME = "newswire.lock"
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


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
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
        now = _parse_now(args.now)
        previous = _load_previous(args.output)
        last_generated_at, last_sha256 = _last_good_receipt(args.output, previous)
        registry = load_source_registry(args.config)
        prior_ledger = _load_ledger(args.ledger)
        proxy = os.environ.get("PALIMPSEST_PROXY") or None

        def fetch(url: str, **kwargs: Any) -> bytes:
            return safe_fetch_bytes(url, proxy=proxy, **kwargs)

        document = collect_newswire(
            registry,
            fetch,
            now=now,
            previous=previous,
            max_workers=args.workers,
        )
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
