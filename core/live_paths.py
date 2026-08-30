"""Resolve the live 30-minute wire and readings without inventing latest files.

The evidence-wire timer writes ``/var/lib/palimpsest/newswire/newswire-latest.json``.
GitHub Pages and CI keep using the repo ``readings/`` copy. Readers must prefer
the live file when it exists so per-event analysis is not frozen at publish time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent

LIVE_NEWSWIRE_DIR = Path("/var/lib/palimpsest/newswire")
LIVE_NEWSWIRE_PATH = LIVE_NEWSWIRE_DIR / "newswire-latest.json"
LIVE_ANALYSIS_PATH = LIVE_NEWSWIRE_DIR / "event-analysis-latest.json"
LIVE_READINGS_DIR = Path("/var/lib/palimpsest/readings")
LIVE_ARCHIVE_CONTEXT_PATHS = (
    LIVE_READINGS_DIR / "archive-news-context-latest.json",
    Path("/var/lib/palimpsest/common-crawl/derived/archive-news-context.json"),
)
LIVE_ARCHIVE_ATTEMPT_PATH = Path(
    "/var/lib/palimpsest/common-crawl/derived/archive-news-context.last-attempt.json"
)
REPO_NEWSWIRE_PATH = ROOT / "readings" / "newswire-latest.json"
REPO_READINGS_DIR = ROOT / "readings"
PUBLICATION_SNAPSHOT_ROOT_ENV = "PALIMPSEST_PUBLICATION_SNAPSHOT_ROOT"


class LivePathError(ValueError):
    """A configured publication snapshot cannot be used safely."""


def _publication_snapshot_readings_dir() -> Path | None:
    raw_root = os.getenv(PUBLICATION_SNAPSHOT_ROOT_ENV)
    if raw_root is None or raw_root == "":
        return None
    if raw_root != raw_root.strip():
        raise LivePathError("publication snapshot root has surrounding whitespace")
    root = Path(raw_root)
    if not root.is_absolute():
        raise LivePathError("publication snapshot root must be absolute")
    try:
        resolved_root = root.resolve(strict=True)
        readings = (resolved_root / "readings").resolve(strict=True)
    except OSError as exc:
        raise LivePathError("publication snapshot root is unavailable") from exc
    if (
        not resolved_root.is_dir()
        or not readings.is_dir()
        or readings.parent != resolved_root
    ):
        raise LivePathError("publication snapshot readings directory is unsafe")
    return readings


def _required_snapshot_file(readings: Path, filename: str) -> Path:
    try:
        candidate = (readings / filename).resolve(strict=True)
    except OSError as exc:
        raise LivePathError(f"publication snapshot lacks {filename}") from exc
    if not candidate.is_file() or candidate.parent != readings:
        raise LivePathError(f"publication snapshot {filename} is unsafe")
    return candidate


def resolve_newswire_path(*, preferred: Path | str | None = None) -> Path:
    """Return the live timer file when present, else the repo or preferred path."""

    snapshot = _publication_snapshot_readings_dir()
    if snapshot is not None:
        return _required_snapshot_file(snapshot, "newswire-latest.json")
    if LIVE_NEWSWIRE_PATH.is_file():
        return LIVE_NEWSWIRE_PATH
    if preferred is not None:
        return Path(preferred)
    return REPO_NEWSWIRE_PATH


def resolve_readings_dir(*, preferred: Path | str | None = None) -> Path:
    """Return the node readings directory when it exists, else repo/preferred."""

    snapshot = _publication_snapshot_readings_dir()
    if snapshot is not None:
        return snapshot
    if LIVE_READINGS_DIR.is_dir():
        return LIVE_READINGS_DIR
    if preferred is not None:
        return Path(preferred)
    return REPO_READINGS_DIR


def readings_search_dirs(*, preferred: Path | str | None = None) -> list[Path]:
    """Search live readings first so a missing PR82 file abstains on the node."""

    snapshot = _publication_snapshot_readings_dir()
    if snapshot is not None:
        return [snapshot]
    dirs: list[Path] = []
    if LIVE_READINGS_DIR.is_dir():
        dirs.append(LIVE_READINGS_DIR)
    if preferred is not None:
        path = Path(preferred)
        if path not in dirs:
            dirs.append(path)
    return dirs


def resolve_live_analysis_path(*, preferred: Path | str | None = None) -> Path:
    snapshot = _publication_snapshot_readings_dir()
    if snapshot is not None:
        return snapshot / "event-analysis-latest.json"
    if LIVE_NEWSWIRE_DIR.is_dir():
        return LIVE_ANALYSIS_PATH
    if preferred is not None:
        return Path(preferred)
    return ROOT / "readings" / "event-analysis-latest.json"


def load_json_if_present(path: Path | str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        if not candidate.is_file():
            return None
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if type(value) is dict else None


def archive_context_paths() -> tuple[Path, ...]:
    """Return archive context paths without escaping an active snapshot."""

    snapshot = _publication_snapshot_readings_dir()
    if snapshot is not None:
        return (snapshot / "archive-news-context-latest.json",)
    return LIVE_ARCHIVE_CONTEXT_PATHS


def load_archive_refresh_status(path: Path | str | None = None) -> str:
    """Report the ExecStartPre revision-pin outcome without inventing a refresh."""

    if path is None:
        snapshot = _publication_snapshot_readings_dir()
        path = (
            snapshot / "archive-news-context.last-attempt.json"
            if snapshot is not None
            else LIVE_ARCHIVE_ATTEMPT_PATH
        )
    document = load_json_if_present(path)
    if document is None:
        return "unknown"
    pin = document.get("revision_pin")
    if pin == "mismatch":
        return "revision_pin_mismatch"
    if pin == "match":
        return "ok"
    return "unknown"
