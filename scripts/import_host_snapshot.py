"""Import sanitized Hetzner readings into the public Git tree.

The collectors run on the fixed German vantage. GitHub Actions must not invent a
second measurement. This boundary fetches only the exact-path Caddy publications
under api.seiche.info, with redirects disabled, and writes an atomic last-good
replacement. Origins are code constants: changing a URL requires review.

Usage:  PYTHONPATH=. python -m scripts.import_host_snapshot --allow-empty-bootstrap-404
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _parse_json(payload: bytes, *, snapshot_id: str) -> dict[str, Any]:
    if not isinstance(payload, bytes):
        raise HostSnapshotImportError(f"{snapshot_id} fetch must return raw bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostSnapshotImportError(f"{snapshot_id} is not strict UTF-8") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HostSnapshotImportError(f"{snapshot_id} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise HostSnapshotImportError(f"{snapshot_id} root must be an object")
    return document


def _parse_generated_at(value: Any, *, snapshot_id: str, now: float) -> datetime:
    if not isinstance(value, str):
        raise HostSnapshotImportError(f"{snapshot_id} generated_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HostSnapshotImportError(f"{snapshot_id} generated_at is not ISO-8601") from exc
    if parsed.tzinfo is None:
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
    return document


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
) -> dict[str, Any] | None:
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
        return None
    document = validate_document(_parse_json(payload, snapshot_id=spec.snapshot_id), spec, now=checked_at)
    if output.exists() or output.is_symlink():
        try:
            previous = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} existing latest is unreadable"
            ) from exc
        if isinstance(previous, dict) and "generated_at" in previous:
            incoming = _parse_generated_at(
                document["generated_at"], snapshot_id=spec.snapshot_id, now=checked_at
            )
            previous_at = _parse_generated_at(
                previous["generated_at"], snapshot_id=spec.snapshot_id, now=checked_at
            )
            if incoming < previous_at:
                raise HostSnapshotImportError(
                    f"{spec.snapshot_id} generated_at would roll back the last-good high-water mark"
                )
    canonical = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _write_atomic(output, canonical + b"\n")
    return document


def time_now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def import_all(
    *,
    readings: Path = READINGS,
    fetcher: Fetcher = safe_fetch_bytes,
    now: float | None = None,
    allow_empty_bootstrap_404: bool = False,
) -> dict[str, str]:
    results: dict[str, str] = {}
    for spec in SNAPSHOTS:
        document = import_one(
            spec,
            output=Path(readings) / spec.filename,
            fetcher=fetcher,
            now=now,
            allow_empty_bootstrap_404=allow_empty_bootstrap_404,
        )
        if document is None:
            results[spec.snapshot_id] = "bootstrap-pending"
            print(f"{spec.snapshot_id}: bootstrap pending (endpoint 404, no local artifact)")
            continue
        digest = hashlib.sha256(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        results[spec.snapshot_id] = document["generated_at"]
        print(
            f"imported host snapshot {spec.snapshot_id} "
            f"({document['generated_at']}, sha256:{digest})"
        )
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
        )
    except HostSnapshotImportError as exc:
        print(f"host snapshot import refused: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
