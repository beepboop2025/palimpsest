"""Request archive snapshots for newly first-seen public URLs.

Lookup addresses are always safe to publish: they are places to *try*.
A snapshot URL is attached only when an archive API response contains one.
This module never claims a capture that the API did not confirm.

Internet Archive Save Page Now is keyless. archive.today often requires a
browser/captcha, so this collector only publishes its lookup address unless a
caller supplies a witnessed snapshot URL.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from core.china_observation import public_text


WAYBACK_SAVE = "https://web.archive.org/save/{url}"
WAYBACK_SNAPSHOT_RE = re.compile(
    r"https?://web\.archive\.org/web/(\d{14})/(https?://[^\s\"'<>]+)",
    re.IGNORECASE,
)
ARCHIVE_TODAY_LOOKUP = "https://archive.today/{url}"

FetchText = Callable[[str], str]


def previous_urls_from_reading(path: Path | str | None) -> set[str]:
    """URLs already recorded on a prior reading. Missing/invalid files are empty."""

    if path is None:
        return set()
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(doc, dict):
        return set()
    urls: set[str] = set()
    for obs in doc.get("observations") or []:
        if not isinstance(obs, dict):
            continue
        url = public_text(obs.get("url") or obs.get("source_url"), limit=2048)
        if url.startswith("https://"):
            urls.add(url)
    return urls


def archive_today_lookup(url: str) -> str | None:
    text = public_text(url, limit=2048)
    if not text.startswith("https://"):
        return None
    return ARCHIVE_TODAY_LOOKUP.format(url=text)


def parse_wayback_snapshot(payload: str) -> str | None:
    """Return a witnessed IA snapshot URL, or None if the API did not name one."""

    if not isinstance(payload, str) or not payload:
        return None
    match = WAYBACK_SNAPSHOT_RE.search(payload)
    if match is None:
        return None
    stamp, target = match.group(1), match.group(2).rstrip(").,;\"'")
    if not target.startswith("http"):
        return None
    return f"https://web.archive.org/web/{stamp}/{target}"


def request_wayback_save(url: str, fetch: FetchText) -> dict[str, Any]:
    """Ask IA to save ``url``. Snapshot is null unless the response contains one."""

    target = public_text(url, limit=2048)
    if not target.startswith("https://"):
        return {
            "url": target or None,
            "save_requested": False,
            "wayback_snapshot": None,
            "archive_today_lookup": None,
            "note": "not an https public URL; no save requested",
        }
    save_url = WAYBACK_SAVE.format(url=quote(target, safe=":/?#[]@!$&'()*+,;="))
    snapshot = None
    note = "Save Page Now requested; no witnessed snapshot in the response"
    try:
        body = fetch(save_url)
    except OSError as exc:
        note = f"Save Page Now transport failed ({type(exc).__name__}); lookup only"
        body = ""
    if body:
        snapshot = parse_wayback_snapshot(body)
        if snapshot:
            note = "Internet Archive confirmed a snapshot URL"
    return {
        "url": target,
        "save_requested": True,
        "wayback_snapshot": snapshot,
        "archive_today_lookup": archive_today_lookup(target),
        "note": note,
    }


def attach_new_url_captures(
    observations: list[Mapping[str, Any]],
    *,
    previous_urls: set[str],
    fetch: FetchText | None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Save newly first-seen observation URLs. Missing fetch leaves lookups only."""

    attached = 0
    out: list[dict[str, Any]] = []
    for raw in observations:
        row = dict(raw)
        url = public_text(row.get("url") or row.get("source_url"), limit=2048)
        archive = dict(row.get("archive") or {})
        if (
            fetch is not None
            and url.startswith("https://")
            and url not in previous_urls
            and attached < limit
        ):
            capture = request_wayback_save(url, fetch)
            attached += 1
            if capture.get("wayback_snapshot") and not archive.get("wayback_snapshot"):
                archive["wayback_snapshot"] = capture["wayback_snapshot"]
            archive.setdefault("archive_today_lookup", capture.get("archive_today_lookup"))
            archive["save_note"] = capture.get("note")
        row["archive"] = archive
        out.append(row)
    return out
