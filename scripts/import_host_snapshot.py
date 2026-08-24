"""Import sanitized Hetzner readings into the public Git tree.

The collectors run on the fixed German vantage. GitHub Actions must not invent a
second measurement. This boundary fetches only the exact-path Caddy publications
under api.seiche.info, with redirects disabled, and writes an atomic last-good
replacement. Origins are code constants: changing a URL requires review.

Every successful comparison emits one JSON outcome. A reviewed batch may retain a
newer validated local document when a host lags, but equivocation and invalid evidence
always fail closed.

Usage:  PYTHONPATH=. python -m scripts.import_host_snapshot \
          --allow-empty-bootstrap-404 --keep-last-good-on-stale
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable

from core.safe_fetch import FetchError, safe_fetch_bytes


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
TIMEOUT_SECONDS = 15.0
EARLIEST = datetime(2025, 1, 1, tzinfo=timezone.utc)
MAX_FUTURE_SKEW_SECONDS = 300.0

Fetcher = Callable[..., bytes]


class HostSnapshotImportError(RuntimeError):
    """A Hetzner host snapshot failed closed-schema import."""


@dataclass(frozen=True)
class HostSnapshot:
    """One exact-path publication the public importer may fetch."""

    snapshot_id: str
    url: str
    filename: str
    max_bytes: int
    required_fields: tuple[str, ...]


@dataclass(frozen=True)
class ImportOutcome:
    """Observable result of comparing one host publication with its high-water mark."""

    snapshot_id: str
    status: str
    incoming_generated_at: str | None
    retained_generated_at: str | None
    incoming_sha256: str | None
    retained_sha256: str | None
    wrote: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Deliberately not environment or CLI. Changing the trust origin is a code review.
SNAPSHOTS: tuple[HostSnapshot, ...] = (
    HostSnapshot(
        snapshot_id="baike-public-snapshot",
        url=(
            "https://api.seiche.info/palimpsest/baike-public-snapshot/"
            "baike-public-snapshot-latest.json"
        ),
        filename="baike-public-snapshot-latest.json",
        max_bytes=256 * 1024,
        required_fields=(
            "generated_at",
            "source",
            "method",
            "scope",
            "n_pages",
            "n_ok",
            "n_observations",
        ),
    ),
    HostSnapshot(
        snapshot_id="peer-context",
        url="https://api.seiche.info/palimpsest/peer-context/peer-context-latest.json",
        filename="peer-context-latest.json",
        max_bytes=512 * 1024,
        required_fields=("generated_at", "source", "method", "scope", "n_hosts"),
    ),
    HostSnapshot(
        snapshot_id="greatfire-context",
        url=(
            "https://api.seiche.info/palimpsest/greatfire-context/"
            "greatfire-context-latest.json"
        ),
        filename="greatfire-context-latest.json",
        max_bytes=256 * 1024,
        required_fields=("generated_at", "source", "method", "scope", "n_urls_queried"),
    ),
    HostSnapshot(
        snapshot_id="public-deletion-ledgers",
        url=(
            "https://api.seiche.info/palimpsest/public-deletion-ledgers/"
            "public-deletion-ledgers-latest.json"
        ),
        filename="public-deletion-ledgers-latest.json",
        max_bytes=512 * 1024,
        required_fields=("generated_at", "source", "method", "scope", "n_observations"),
    ),
)


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]], *, snapshot_id: str
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise HostSnapshotImportError(
                f"{snapshot_id} repeats JSON key {key!r}"
            )
        document[key] = value
    return document


def _reject_constant(value: str, *, snapshot_id: str) -> None:
    raise HostSnapshotImportError(
        f"{snapshot_id} contains non-finite JSON number {value}"
    )


def _parse_json(payload: bytes, *, snapshot_id: str) -> dict[str, Any]:
    if not isinstance(payload, bytes):
        raise HostSnapshotImportError(f"{snapshot_id} fetch must return raw bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostSnapshotImportError(f"{snapshot_id} is not strict UTF-8") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=lambda pairs: _reject_duplicate_keys(
                pairs, snapshot_id=snapshot_id
            ),
            parse_constant=lambda value: _reject_constant(
                value, snapshot_id=snapshot_id
            ),
        )
    except HostSnapshotImportError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise HostSnapshotImportError(f"{snapshot_id} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise HostSnapshotImportError(f"{snapshot_id} root must be an object")
    return document


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_generated_at(value: Any, *, snapshot_id: str, now: float) -> datetime:
    if not isinstance(value, str):
        raise HostSnapshotImportError(f"{snapshot_id} generated_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HostSnapshotImportError(f"{snapshot_id} generated_at is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise HostSnapshotImportError(f"{snapshot_id} generated_at must be UTC")
    parsed = parsed.astimezone(timezone.utc)
    epoch = parsed.timestamp()
    if epoch < EARLIEST.timestamp() or epoch > now + MAX_FUTURE_SKEW_SECONDS:
        raise HostSnapshotImportError(f"{snapshot_id} generated_at is outside the accepted clock")
    return parsed


def validate_document(
    document: dict[str, Any],
    spec: HostSnapshot,
    *,
    now: float,
) -> dict[str, Any]:
    missing = [field for field in spec.required_fields if field not in document]
    if missing:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} missing required fields: {', '.join(missing)}"
        )
    _parse_generated_at(document["generated_at"], snapshot_id=spec.snapshot_id, now=now)
    for field in ("source", "method", "scope"):
        value = document.get(field)
        if not isinstance(value, str) or not value.strip():
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} {field} must be a non-empty string"
            )
    for field in spec.required_fields:
        if not field.startswith("n_"):
            continue
        value = document[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} {field} must be a non-negative integer"
            )
    return document


def _validated_payload(
    payload: bytes,
    spec: HostSnapshot,
    *,
    now: float,
    source: str,
) -> tuple[dict[str, Any], bytes, datetime]:
    if len(payload) > spec.max_bytes:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} {source} exceeds {spec.max_bytes} bytes"
        )
    document = validate_document(
        _parse_json(payload, snapshot_id=spec.snapshot_id),
        spec,
        now=now,
    )
    canonical = _canonical(document)
    generated_at = _parse_generated_at(
        document["generated_at"],
        snapshot_id=spec.snapshot_id,
        now=now,
    )
    return document, canonical, generated_at


def _write_atomic(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    directory = target.parent
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=directory)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = ""
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _download(
    spec: HostSnapshot,
    fetcher: Fetcher,
    *,
    allow_not_found: bool,
) -> bytes | None:
    try:
        payload = fetcher(
            spec.url,
            max_bytes=spec.max_bytes,
            timeout=TIMEOUT_SECONDS,
            max_redirects=0,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
    except FetchError as exc:
        if allow_not_found and type(exc) is FetchError and exc.args == ("http status 404",):
            return None
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} download failed ({type(exc).__name__})"
        ) from exc
    except (OSError, TimeoutError) as exc:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} download failed ({type(exc).__name__})"
        ) from exc
    if not isinstance(payload, bytes):
        raise HostSnapshotImportError(f"{spec.snapshot_id} fetch must return raw bytes")
    if len(payload) > spec.max_bytes:
        raise HostSnapshotImportError(f"{spec.snapshot_id} exceeds {spec.max_bytes} bytes")
    return payload


def import_one(
    spec: HostSnapshot,
    *,
    output: Path,
    fetcher: Fetcher = safe_fetch_bytes,
    now: float | None = None,
    allow_empty_bootstrap_404: bool = False,
    keep_last_good_on_stale: bool = False,
) -> ImportOutcome:
    checked_at = time_now(now)
    payload = _download(
        spec,
        fetcher,
        allow_not_found=allow_empty_bootstrap_404,
    )
    if payload is None:
        if output.exists() or output.is_symlink():
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} endpoint returned 404 after local publication began"
            )
        return ImportOutcome(
            snapshot_id=spec.snapshot_id,
            status="bootstrap-pending",
            incoming_generated_at=None,
            retained_generated_at=None,
            incoming_sha256=None,
            retained_sha256=None,
            wrote=False,
        )
    document, canonical, incoming_at = _validated_payload(
        payload,
        spec,
        now=checked_at,
        source="incoming publication",
    )
    incoming_digest = _sha256(canonical)
    if output.exists() or output.is_symlink():
        try:
            previous_payload = output.read_bytes()
        except OSError as exc:
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} existing latest is unreadable"
            ) from exc
        try:
            previous, previous_canonical, previous_at = _validated_payload(
                previous_payload,
                spec,
                now=checked_at,
                source="existing latest",
            )
        except HostSnapshotImportError as exc:
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} existing latest is invalid: {exc}"
            ) from exc
        previous_digest = _sha256(previous_canonical)
        if incoming_at < previous_at:
            if not keep_last_good_on_stale:
                raise HostSnapshotImportError(
                    f"{spec.snapshot_id} generated_at would roll back the last-good "
                    "high-water mark (pass --keep-last-good-on-stale only in the "
                    "reviewed batch publication workflow)"
                )
            return ImportOutcome(
                snapshot_id=spec.snapshot_id,
                status="kept-last-good",
                incoming_generated_at=document["generated_at"],
                retained_generated_at=previous["generated_at"],
                incoming_sha256=incoming_digest,
                retained_sha256=previous_digest,
                wrote=False,
            )
        if incoming_at == previous_at:
            if canonical != previous_canonical:
                raise HostSnapshotImportError(
                    f"{spec.snapshot_id} equivocated at generated_at "
                    f"{document['generated_at']}: equal timestamp has different content"
                )
            return ImportOutcome(
                snapshot_id=spec.snapshot_id,
                status="unchanged",
                incoming_generated_at=document["generated_at"],
                retained_generated_at=previous["generated_at"],
                incoming_sha256=incoming_digest,
                retained_sha256=previous_digest,
                wrote=False,
            )
    _write_atomic(output, canonical + b"\n")
    return ImportOutcome(
        snapshot_id=spec.snapshot_id,
        status="imported",
        incoming_generated_at=document["generated_at"],
        retained_generated_at=document["generated_at"],
        incoming_sha256=incoming_digest,
        retained_sha256=incoming_digest,
        wrote=True,
    )


def time_now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def import_all(
    *,
    readings: Path = READINGS,
    fetcher: Fetcher = safe_fetch_bytes,
    now: float | None = None,
    allow_empty_bootstrap_404: bool = False,
    keep_last_good_on_stale: bool = False,
) -> dict[str, ImportOutcome]:
    results: dict[str, ImportOutcome] = {}
    for spec in SNAPSHOTS:
        outcome = import_one(
            spec,
            output=Path(readings) / spec.filename,
            fetcher=fetcher,
            now=now,
            allow_empty_bootstrap_404=allow_empty_bootstrap_404,
            keep_last_good_on_stale=keep_last_good_on_stale,
        )
        results[spec.snapshot_id] = outcome
        print(json.dumps(outcome.as_dict(), sort_keys=True, separators=(",", ":")))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--readings",
        type=Path,
        default=READINGS,
        help="directory that receives the imported latest files",
    )
    parser.add_argument(
        "--keep-last-good-on-stale",
        action="store_true",
        help=(
            "when a valid host snapshot is older than the validated local high-water "
            "mark, retain the local artifact, emit a structured kept-last-good outcome, "
            "and continue importing the remaining snapshots"
        ),
    )
    parser.add_argument(
        "--allow-empty-bootstrap-404",
        action="store_true",
        help=(
            "succeed without writing a missing snapshot only when that endpoint "
            "returns 404 and no local artifact exists"
        ),
    )
    args = parser.parse_args(argv)
    try:
        import_all(
            readings=args.readings,
            allow_empty_bootstrap_404=args.allow_empty_bootstrap_404,
            keep_last_good_on_stale=args.keep_last_good_on_stale,
        )
    except HostSnapshotImportError as exc:
        print(f"host snapshot import refused: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
