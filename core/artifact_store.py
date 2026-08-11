"""Immutable, content-addressed archives for normalized collector observations.

Snapshot runners intentionally keep a small ``*-latest.json`` public surface.
On the always-on node that pointer is not enough: every successful look must be
recoverable for longitudinal analysis.  This module copies the exact normalized
reading into a private, gzip-compressed archive without changing the public
publication boundary.

The archive is opt-in and stores *normalized observations*, not every upstream
HTTP response.  That distinction keeps high-volume APIs bounded while still
preserving what the collector actually asserted at each observation time.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
_SOURCE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


def archive_enabled() -> bool:
    """Whether successful node observations should be retained privately."""

    value = os.getenv("PALIMPSEST_OBSERVATION_ARCHIVE_ENABLED", "")
    return value.strip().lower() in _TRUTHY


def _archive_root(repo_root: Path) -> Path:
    configured = os.getenv("PALIMPSEST_OBSERVATION_DIR", "").strip()
    return Path(configured) if configured else repo_root / "data" / "observations"


def _max_bytes() -> int:
    raw = os.getenv("PALIMPSEST_OBSERVATION_MAX_BYTES", str(DEFAULT_MAX_BYTES))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("PALIMPSEST_OBSERVATION_MAX_BYTES must be an integer") from exc
    if value < 1024:
        raise ValueError("PALIMPSEST_OBSERVATION_MAX_BYTES must be at least 1024")
    return value


def archive_observation(
    source: str,
    reading_path: Path | str,
    *,
    repo_root: Path | str | None = None,
    observed_at: datetime | None = None,
) -> dict:
    """Archive one exact JSON reading and return ledger-ready metadata.

    Files are keyed by their SHA-256 digest, so retrying the same observation is
    idempotent.  A temporary file and ``os.replace`` make the commit atomic; a
    crash can leave at most a disposable ``.partial-*`` file, never a truncated
    artifact at its final path.
    """

    if not _SOURCE.fullmatch(source):
        raise ValueError(f"unsafe observation source: {source!r}")

    root = Path(repo_root) if repo_root is not None else ROOT
    path = Path(reading_path)
    raw = path.read_bytes()
    limit = _max_bytes()
    if len(raw) > limit:
        raise ValueError(
            f"observation {path.name} is {len(raw)} bytes; limit is {limit}"
        )

    # Prove this is JSON before treating it as a normalized observation.  Keep
    # the original bytes (rather than re-serializing) so the digest is an exact
    # commitment to the collector output.
    import json

    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("normalized observation must be a JSON object")

    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    digest = hashlib.sha256(raw).hexdigest()
    archive_root = _archive_root(root)
    directory = archive_root / source / digest[:2]
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{digest}.json.gz"
    destination = directory / filename

    if not destination.exists():
        # gzip.compress(mtime=0) makes identical input deterministic across
        # runs, which in turn makes checksum verification meaningful.
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        fd, temporary = tempfile.mkstemp(
            prefix=".partial-", suffix=".json.gz", dir=directory
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(compressed)
                handle.flush()
                os.fsync(handle.fileno())
                # The bind-mounted archive is backed up by the unprivileged
                # host operator (UID 1001), while this container runs as UID
                # 10001. Normalized public-source observations contain no
                # credentials, so make the final evidence deliberately
                # host-readable without granting write access.
                os.fchmod(handle.fileno(), 0o644)
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    else:
        # Heal artifacts written by an older image that inherited mkstemp's
        # 0600 mode and therefore could not be included in host-side backups.
        os.chmod(destination, 0o644)
    compressed_bytes = destination.stat().st_size

    try:
        archive_path = str(destination.relative_to(root))
    except ValueError:
        archive_path = str(destination)

    generated_at = (
        document.get("generated_at")
        or document.get("as_of")
        or document.get("timestamp")
    )
    return {
        "source": source,
        "sha256": digest,
        "archive_path": archive_path,
        "original_bytes": len(raw),
        "compressed_bytes": compressed_bytes,
        "generated_at": str(generated_at) if generated_at is not None else None,
        "archived_at": now.isoformat(),
    }
