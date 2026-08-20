"""Resolve the live 30-minute wire and readings without inventing latest files.

The evidence-wire timer writes ``/var/lib/palimpsest/newswire/newswire-latest.json``.
GitHub Pages and CI keep using the repo ``readings/`` copy. Readers must prefer
the live file when it exists so per-event analysis is not frozen at publish time.
"""

from __future__ import annotations

import json
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


def resolve_newswire_path(*, preferred: Path | str | None = None) -> Path:
    """Return the live timer file when present, else the repo or preferred path."""

    if LIVE_NEWSWIRE_PATH.is_file():
        return LIVE_NEWSWIRE_PATH
    if preferred is not None:
        return Path(preferred)
    return REPO_NEWSWIRE_PATH


def resolve_readings_dir(*, preferred: Path | str | None = None) -> Path:
    """Return the node readings directory when it exists, else repo/preferred."""

    if LIVE_READINGS_DIR.is_dir():
        return LIVE_READINGS_DIR
    if preferred is not None:
        return Path(preferred)
    return REPO_READINGS_DIR


def readings_search_dirs(*, preferred: Path | str | None = None) -> list[Path]:
    """Search live readings first so a missing PR82 file abstains on the node."""

    dirs: list[Path] = []
    if LIVE_READINGS_DIR.is_dir():
        dirs.append(LIVE_READINGS_DIR)
    if preferred is not None:
        path = Path(preferred)
        if path not in dirs:
            dirs.append(path)
    return dirs


def resolve_live_analysis_path(*, preferred: Path | str | None = None) -> Path:
    if LIVE_NEWSWIRE_DIR.is_dir():
        return LIVE_ANALYSIS_PATH
    if preferred is not None:
        return Path(preferred)
    return ROOT / "readings" / "event-analysis-latest.json"


def load_json_if_present(path: Path | str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if type(value) is dict else None


def load_archive_refresh_status(path: Path | str | None = None) -> str:
    """Report the ExecStartPre revision-pin outcome without inventing a refresh."""

    document = load_json_if_present(path or LIVE_ARCHIVE_ATTEMPT_PATH)
    if document is None:
        return "unknown"
    pin = document.get("revision_pin")
    if pin == "mismatch":
        return "revision_pin_mismatch"
    if pin == "match":
        return "ok"
    return "unknown"
