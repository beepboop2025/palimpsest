#!/usr/bin/env python3
"""Pull the closed RSS/Atom registry and atomically publish the evidence newswire.

This command has the network binding; ``core.newswire`` itself receives an injected
byte fetcher and is fully testable offline.  A run with zero structurally successful
sources exits non-zero without touching the prior latest document or version ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
MAX_LEDGER_RECORDS = 16384
MAX_LEDGER_BYTES = 32 * 1024 * 1024
MAX_PREVIOUS_BYTES = 64 * 1024 * 1024
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
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
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
    if type(value["event_id"]) is not str or not _EVENT_ID_RE.fullmatch(value["event_id"]):
        raise ValueError(f"{path} has an invalid event_id")
    if type(value["version_id"]) is not str or not _EVENT_VERSION_ID_RE.fullmatch(value["version_id"]):
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
    if type(headline) is not str or not headline or len(headline) > 240 or any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in headline
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
        or any(type(source_id) is not str or not _SOURCE_ID_RE.fullmatch(source_id) for source_id in source_ids)
    ):
        raise ValueError(f"{path} has invalid source_ids")


def _next_ledger(document: dict[str, Any], prior: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                "source_ids": sorted({ref["source_id"] for ref in event["evidence_refs"]}),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--now", help="fixed ISO-8601 clock for reproducible/offline runs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = _parse_now(args.now)
    registry = load_source_registry(args.config)
    previous = _load_previous(args.output)
    prior_ledger = _load_ledger(args.ledger)
    proxy = os.environ.get("PALIMPSEST_PROXY") or None

    def fetch(url: str, **kwargs: Any) -> bytes:
        return safe_fetch_bytes(url, proxy=proxy, **kwargs)

    try:
        document = collect_newswire(
            registry,
            fetch,
            now=now,
            previous=previous,
            max_workers=args.workers,
        )
    except NoSuccessfulSources as exc:
        print(f"newswire: {exc}; prior latest and ledger preserved")
        return 2

    ledger = _next_ledger(document, prior_ledger)
    # The bounded ledger lands before latest.  A crash can therefore leave an unused
    # version receipt, but never a public latest version with no corresponding receipt.
    _atomic_write(args.ledger, _ledger_bytes(ledger))
    rendered = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    _atomic_write(args.output, rendered)
    coverage = document["coverage"]
    print(
        "newswire: "
        f"{document['n_items']} items -> {document['n_events']} events; "
        f"{coverage['successful_sources']}/{coverage['registry_sources']} sources; "
        f"coverage={coverage['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
