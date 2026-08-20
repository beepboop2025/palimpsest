"""Fail-closed 4chan catalog tap. Rumour context only.

This collector does not open a network socket. A caller may inject already
fetched catalogs. Live HTTP remains unwired until the operator flag and a
reviewed transport exist.

Boards are a compile-time allow-list. /pol and /b cannot be added by config.
Media is dropped before any title is considered. If the minor-safety filter is
unsure, the thread is dropped and not stored.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from core import live_event as live_event_model
from core.place_gazetteer import match_lemmas


TAP_ID = "fourchan-catalog"
ALLOWED_BOARDS = frozenset({"news", "int"})
BLOCKED_BOARDS = frozenset({"pol", "b"})
FLAG = "PALIMPSEST_FOURCHAN_ENABLED"
RELATION = "rumour-board-context-not-corroboration"
_MAX_TITLE = 180

# Fail closed. These tokens are enough to drop a thread. The list is not a
# search index and is not expanded from post bodies.
_MINOR_DENY = (
    "loli",
    "shota",
    "child",
    "children",
    "kid",
    "kids",
    "teen",
    "underage",
    "under-age",
    "minor",
    "schoolgirl",
    "schoolboy",
    "preteen",
    "jailbait",
)


class FourchanCatalogError(ValueError):
    """The catalog tap was asked to widen its board or retention boundary."""


def fourchan_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(FLAG, "")).strip() == "1"


def classify_minor_safety(title: str) -> str:
    """Return pass or drop. Unsure becomes drop.

    TODO: tighten this after reviewing the first fixture pack. Keep fail-closed.
    """

    if type(title) is not str:
        return "drop"
    stripped = title.strip()
    if not stripped:
        return "drop"
    folded = stripped.casefold()
    if any(token in folded for token in _MINOR_DENY):
        return "drop"
    return "pass"


def _utc_from_unix(value: Any) -> str | None:
    if type(value) is not int or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _bounded_title(value: Any) -> str | None:
    if type(value) is not str:
        return None
    title = " ".join(value.split())
    if not title:
        return None
    return title[:_MAX_TITLE]


def _thread_url(board: str, thread_no: int) -> str:
    return f"https://boards.4chan.org/{quote(board, safe='')}/thread/{thread_no}"


def collect_catalog(
    catalogs: Mapping[str, Any],
    lemmas: Sequence[Mapping[str, Any]],
    *,
    observed_at: str,
    vantage: str = "test-vantage",
) -> tuple[list[dict[str, Any]], int]:
    """Parse injected catalogs. Return (accepted events, dropped count)."""

    events: list[dict[str, Any]] = []
    dropped = 0
    for board, payload in catalogs.items():
        if board in BLOCKED_BOARDS or board not in ALLOWED_BOARDS:
            raise FourchanCatalogError(f"board {board!r} is outside the allow-list")
        pages = payload if isinstance(payload, list) else []
        for page in pages:
            threads = page.get("threads") if isinstance(page, Mapping) else None
            if type(threads) is not list:
                continue
            for thread in threads:
                if type(thread) is not dict:
                    dropped += 1
                    continue
                if thread.get("ext") or thread.get("tim") or thread.get("filename"):
                    # Attachment present: keep the title path only after dropping
                    # every media field. We never copy those keys forward.
                    pass
                title = _bounded_title(thread.get("sub") or thread.get("semantic_url"))
                if title is None:
                    dropped += 1
                    continue
                if classify_minor_safety(title) != "pass":
                    dropped += 1
                    continue
                hits = match_lemmas(title, lemmas)
                if not hits:
                    dropped += 1
                    continue
                thread_no = thread.get("no")
                if type(thread_no) is not int or thread_no <= 0:
                    dropped += 1
                    continue
                observed = _utc_from_unix(thread.get("time")) or observed_at
                url = _thread_url(board, thread_no)
                event = {
                    "schema_version": live_event_model.SCHEMA_VERSION,
                    "event_id": live_event_model.event_id(
                        TAP_ID, f"fourchan-{board}", url, observed
                    ),
                    "tap_id": TAP_ID,
                    "source_id": f"fourchan-{board}",
                    "url": url,
                    "title": title,
                    "content_sha256": live_event_model.content_digest(
                        board, str(thread_no), title, observed
                    ),
                    "observed_at": observed,
                    "vantage": vantage,
                    "relation": RELATION,
                    "gazetteer_hits": hits,
                    "rights_class": "rumour-board",
                    "review_status": "machine-accepted",
                }
                live_event_model.validate_live_event(event)
                events.append(event)
    events.sort(key=lambda row: (row["observed_at"], row["event_id"]), reverse=True)
    return events, dropped


def collect(
    *,
    catalogs: Mapping[str, Any] | None = None,
    lemmas: Sequence[Mapping[str, Any]] | None = None,
    observed_at: str,
    vantage: str = "box-local",
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a tap receipt. Network fetch is not implemented in this slice."""

    if catalogs is None:
        return {
            "tap_id": TAP_ID,
            "status": "not-attempted",
            "accepted": 0,
            "dropped": 0,
            "error_code": "flag-off" if not fourchan_enabled(environ) else "transport-unwired",
            "events": [],
        }
    events, dropped = collect_catalog(
        catalogs, lemmas or [], observed_at=observed_at, vantage=vantage
    )
    return {
        "tap_id": TAP_ID,
        "status": "success",
        "accepted": len(events),
        "dropped": dropped,
        "error_code": None,
        "events": events,
    }


__all__ = [
    "ALLOWED_BOARDS",
    "BLOCKED_BOARDS",
    "FLAG",
    "FourchanCatalogError",
    "TAP_ID",
    "classify_minor_safety",
    "collect",
    "collect_catalog",
    "fourchan_enabled",
]
