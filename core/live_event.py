"""Metadata-only live events for the box NDJSON lane.

A line is a receipt, not a page. Bodies, media, cookies, and raw messages are
rejected. Git receives only the sealed live-watch summary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_VERSION = "palimpsest-live-event.v1"
WATCH_SCHEMA_VERSION = "palimpsest-live-watch.v1"
WATCH_RELATION = "context-only-not-corroboration"
RELATIONS = frozenset(
    {
        "rumour-board-context-not-corroboration",
        "official-list-context-not-corroboration",
        "stream-seismograph-context-not-corroboration",
        "demand-rank-context-not-corroboration",
        "physical-pulse-context-not-corroboration",
        "name-lifecycle-context-not-corroboration",
    }
)
RIGHTS_CLASSES = frozenset({"public-metadata", "rumour-board"})
REVIEW_STATUSES = frozenset({"machine-accepted", "not-attempted", "withheld"})
WATCH_STATUSES = frozenset({"warming_up", "live", "halted"})
TAP_STATUSES = frozenset({"success", "not-attempted", "halted"})

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EVENT_ID = re.compile(r"^live-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,47}$")

_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "tap_id",
    "source_id",
    "url",
    "title",
    "content_sha256",
    "observed_at",
    "vantage",
    "relation",
    "gazetteer_hits",
    "rights_class",
    "review_status",
}
_WATCH_FIELDS = {
    "schema_version",
    "generated_at",
    "source",
    "method",
    "scope",
    "status",
    "relation",
    "n_events",
    "n_taps",
    "coverage",
    "taps",
    "publication_policy",
    "limitations",
}
_FORBIDDEN_FIELDS = {
    "body",
    "html",
    "raw_text",
    "message_text",
    "media",
    "image",
    "cookie",
    "cookies",
    "comment",
    "com",
}


class LiveEventError(ValueError):
    """A live event or sealed watch summary violates the metadata-only boundary."""


def canonical_json_bytes(value: Any) -> bytes:
    def reject(node: Any, path: str = "live_event") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise LiveEventError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise LiveEventError(f"{path} contains a non-string key")
                reject(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                reject(child, f"{path}[{index}]")

    reject(value)
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _exact(value: Any, fields: set[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise LiveEventError(f"{path} does not use its exact field set")
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not _TIMESTAMP.fullmatch(value):
        raise LiveEventError(f"{path} must be canonical UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise LiveEventError(f"{path} is not a real UTC timestamp") from exc
    return value


def _text(value: Any, path: str, *, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise LiveEventError(f"{path} must be non-empty bounded text")
    if value != value.strip():
        raise LiveEventError(f"{path} has leading or trailing whitespace")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise LiveEventError(f"{path} contains unsafe Unicode")
    return value


def _identifier(value: Any, path: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise LiveEventError(f"{path} is not a bounded identifier")
    return value


def _count(value: Any, path: str, *, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise LiveEventError(f"{path} must be a bounded non-negative integer")
    return value


def _https_url(value: Any, path: str) -> str:
    text = _text(value, path, maximum=500)
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise LiveEventError(f"{path} must be a keyless https URL")
    return text


def _scan_forbidden(node: Any, path: str) -> None:
    if isinstance(node, Mapping):
        for key, child in node.items():
            if str(key).casefold() in _FORBIDDEN_FIELDS:
                raise LiveEventError(f"{path} contains forbidden field {key!r}")
            _scan_forbidden(child, f"{path}.{key}")
    elif isinstance(node, list):
        for index, child in enumerate(node):
            _scan_forbidden(child, f"{path}[{index}]")


def event_id(tap_id: str, source_id: str, url: str, observed_at: str) -> str:
    digest = hashlib.sha256(
        f"{tap_id}\0{source_id}\0{url}\0{observed_at}".encode("utf-8")
    ).hexdigest()
    return f"live-{digest[:24]}"


def content_digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def validate_live_event(document: Mapping[str, Any]) -> None:
    top = _exact(document, _EVENT_FIELDS, "live_event")
    if top["schema_version"] != SCHEMA_VERSION:
        raise LiveEventError("invalid live-event schema version")
    if type(top["event_id"]) is not str or not _EVENT_ID.fullmatch(top["event_id"]):
        raise LiveEventError("event_id is invalid")
    _identifier(top["tap_id"], "tap_id")
    _identifier(top["source_id"], "source_id")
    _https_url(top["url"], "url")
    _text(top["title"], "title", maximum=180)
    if type(top["content_sha256"]) is not str or not _SHA256.fullmatch(top["content_sha256"]):
        raise LiveEventError("content_sha256 is invalid")
    _timestamp(top["observed_at"], "observed_at")
    _identifier(top["vantage"], "vantage")
    if top["relation"] not in RELATIONS:
        raise LiveEventError("relation is not a locked context relation")
    hits = top["gazetteer_hits"]
    if type(hits) is not list or len(hits) > 16:
        raise LiveEventError("gazetteer_hits must be a bounded list")
    if hits != sorted(set(hits)):
        raise LiveEventError("gazetteer_hits must be unique and sorted")
    for index, hit in enumerate(hits):
        _identifier(hit, f"gazetteer_hits[{index}]")
    if top["rights_class"] not in RIGHTS_CLASSES:
        raise LiveEventError("rights_class is invalid")
    if top["review_status"] not in REVIEW_STATUSES:
        raise LiveEventError("review_status is invalid")
    _scan_forbidden(document, "live_event")
    canonical_json_bytes(document)


def validate_live_watch(document: Mapping[str, Any]) -> None:
    top = _exact(document, _WATCH_FIELDS, "live_watch")
    if top["schema_version"] != WATCH_SCHEMA_VERSION:
        raise LiveEventError("invalid live-watch schema version")
    _timestamp(top["generated_at"], "generated_at")
    _text(top["source"], "source", maximum=500)
    _text(top["method"], "method", maximum=1200)
    _text(top["scope"], "scope", maximum=1200)
    if top["status"] not in WATCH_STATUSES:
        raise LiveEventError("invalid live-watch status")
    if top["relation"] != WATCH_RELATION:
        raise LiveEventError("live-watch relation must remain context-only")
    _count(top["n_events"], "n_events")
    _count(top["n_taps"], "n_taps", maximum=256)
    coverage = _exact(
        top["coverage"],
        {"configured", "attempted", "not_attempted", "accepted", "dropped"},
        "coverage",
    )
    for key, value in coverage.items():
        _count(value, f"coverage.{key}", maximum=1_000_000 if key in {"accepted", "dropped"} else 256)
    if coverage["configured"] != top["n_taps"]:
        raise LiveEventError("coverage.configured must equal n_taps")
    if coverage["attempted"] + coverage["not_attempted"] != coverage["configured"]:
        raise LiveEventError("attempted plus not_attempted must equal configured")
    if coverage["accepted"] != top["n_events"]:
        raise LiveEventError("coverage.accepted must equal n_events")

    taps = top["taps"]
    if type(taps) is not list or len(taps) != top["n_taps"]:
        raise LiveEventError("taps must match n_taps")
    seen: set[str] = set()
    accepted_sum = 0
    dropped_sum = 0
    for index, raw in enumerate(taps):
        path = f"taps[{index}]"
        row = _exact(raw, {"tap_id", "status", "accepted", "dropped", "error_code"}, path)
        tap_id = _identifier(row["tap_id"], f"{path}.tap_id")
        if tap_id in seen:
            raise LiveEventError("duplicate tap_id")
        seen.add(tap_id)
        if row["status"] not in TAP_STATUSES:
            raise LiveEventError(f"{path}.status is invalid")
        accepted_sum += _count(row["accepted"], f"{path}.accepted")
        dropped_sum += _count(row["dropped"], f"{path}.dropped")
        error = row["error_code"]
        if error is not None:
            _identifier(error, f"{path}.error_code")
        if row["status"] == "not-attempted" and row["accepted"] != 0:
            raise LiveEventError(f"{path} accepted events while not attempted")
    if accepted_sum != top["n_events"] or dropped_sum != coverage["dropped"]:
        raise LiveEventError("tap counts do not match the sealed coverage")

    policy = top["publication_policy"]
    if policy != {
        "bodies_included": False,
        "media_included": False,
        "counts_as_corroboration": False,
    }:
        raise LiveEventError("publication policy broadened the metadata-only boundary")
    limitations = top["limitations"]
    if type(limitations) is not list or not 3 <= len(limitations) <= 8:
        raise LiveEventError("limitations must contain 3 to 8 statements")
    for index, value in enumerate(limitations):
        _text(value, f"limitations[{index}]", maximum=500)
    _scan_forbidden(document, "live_watch")
    canonical_json_bytes(document)


def empty_watch_document(
    generated_at: str,
    taps: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Coverage receipt for a configured lane that has not collected yet."""

    receipts = []
    not_attempted = 0
    for raw in taps:
        tap_id = _identifier(raw["tap_id"], "tap.tap_id")
        status = raw.get("status", "not-attempted")
        if status not in TAP_STATUSES:
            raise LiveEventError("empty watch tap status is invalid")
        if status == "not-attempted":
            not_attempted += 1
        receipts.append(
            {
                "tap_id": tap_id,
                "status": status,
                "accepted": 0,
                "dropped": 0,
                "error_code": raw.get("error_code"),
            }
        )
    receipts.sort(key=lambda row: row["tap_id"])
    document = {
        "schema_version": WATCH_SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": (
            "Reviewed public-vantage registry. Live bulk is NDJSON on the node: "
            "source, url, title, hash, time, vantage. Git only gets a summary."
        ),
        "method": (
            "Drink firehoses. Watch a URL list the cheap way. Take other "
            "people's warehouses incrementally. Never keep the HTML. Filter "
            "with the gazetteer and write one line. The public reading is a "
            "sealed coverage summary, not the event stream."
        ),
        "scope": (
            "The way to get more grey data is more public vantages, "
            "continuously, then join them. One headline on Xinhua, gone from "
            "Baidu hot, blocked in OONI, still in Wayback: that tuple is the "
            "product. Official text alone is the censor's story."
        ),
        "status": "warming_up",
        "relation": WATCH_RELATION,
        "n_events": 0,
        "n_taps": len(receipts),
        "coverage": {
            "configured": len(receipts),
            "attempted": len(receipts) - not_attempted,
            "not_attempted": not_attempted,
            "accepted": 0,
            "dropped": 0,
        },
        "taps": receipts,
        "publication_policy": {
            "bodies_included": False,
            "media_included": False,
            "counts_as_corroboration": False,
        },
        "limitations": [
            "A zero event count is a coverage receipt, not evidence of silence.",
            "Rumour, rank flips, wiki edits, CT names, or Telegram previews cannot corroborate a publisher report.",
            "Day files remain on the node. Git only gets a summary. Never keep the HTML.",
        ],
    }
    validate_live_watch(document)
    return document


def load_tap_registry(path: str | Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if type(raw) is not dict or raw.get("schema_version") != "palimpsest-live-taps.v1":
        raise LiveEventError("live tap registry schema is invalid")
    taps = raw.get("taps")
    if type(taps) is not list:
        raise LiveEventError("live tap registry must contain a taps list")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, tap in enumerate(taps):
        if type(tap) is not dict:
            raise LiveEventError(f"taps[{index}] must be an object")
        tap_id = _identifier(tap.get("tap_id"), f"taps[{index}].tap_id")
        if tap_id in seen:
            raise LiveEventError(f"duplicate tap_id {tap_id}")
        seen.add(tap_id)
        relation = tap.get("relation")
        if relation not in RELATIONS:
            raise LiveEventError(f"taps[{index}].relation is not locked")
        out.append(tap)
    return out


def append_events(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    """Append validated events to a day file. The path must stay off git."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for event in events:
        validate_live_event(event)
        lines.append(canonical_json_bytes(event))
    with path.open("ab") as handle:
        handle.writelines(lines)


__all__ = [
    "LiveEventError",
    "RELATIONS",
    "SCHEMA_VERSION",
    "WATCH_RELATION",
    "WATCH_SCHEMA_VERSION",
    "append_events",
    "canonical_json_bytes",
    "content_digest",
    "empty_watch_document",
    "event_id",
    "load_tap_registry",
    "validate_live_event",
    "validate_live_watch",
]
