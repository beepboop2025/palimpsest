"""Public rumour-board desk. Context only. Never an independent source group."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from core import live_event as live_event_model


SCHEMA_VERSION = "palimpsest-rumour-board.v1"
RELATION = "rumour-board-context-not-corroboration"
STATUSES = frozenset({"COVERAGE_ONLY", "RUMOUR_CONTEXT"})
_ENTRY_ID = re.compile(r"^rumour-[0-9a-f]{24}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,47}$")

_TOP_FIELDS = {
    "schema_version",
    "generated_at",
    "source",
    "method",
    "scope",
    "status",
    "relation",
    "n_entries",
    "entries",
    "publication_policy",
    "limitations",
}
_ENTRY_FIELDS = {
    "entry_id",
    "observed_at",
    "tap_id",
    "surface",
    "title",
    "relation",
    "gazetteer_hits",
}
_POLICY = {
    "counts_as_corroboration": False,
    "media_included": False,
    "raw_messages_included": False,
    "increments_independent_groups": False,
}


class RumourBoardError(ValueError):
    """A rumour-board publication crossed into corroboration or stored a body."""


def canonical_json_bytes(value: Any) -> bytes:
    def reject(node: Any, path: str = "rumour_board") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise RumourBoardError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise RumourBoardError(f"{path} contains a non-string key")
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
        raise RumourBoardError(f"{path} does not use its exact field set")
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not re.fullmatch(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", value
    ):
        raise RumourBoardError(f"{path} must be canonical UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise RumourBoardError(f"{path} is not a real UTC timestamp") from exc
    return value


def _text(value: Any, path: str, *, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise RumourBoardError(f"{path} must be non-empty bounded text")
    if value != value.strip():
        raise RumourBoardError(f"{path} has leading or trailing whitespace")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise RumourBoardError(f"{path} contains unsafe Unicode")
    return value


def validate_rumour_board(document: Mapping[str, Any]) -> None:
    top = _exact(document, _TOP_FIELDS, "rumour_board")
    if top["schema_version"] != SCHEMA_VERSION:
        raise RumourBoardError("invalid rumour-board schema version")
    _timestamp(top["generated_at"], "generated_at")
    _text(top["source"], "source", maximum=500)
    _text(top["method"], "method", maximum=1200)
    _text(top["scope"], "scope", maximum=1200)
    if top["status"] not in STATUSES:
        raise RumourBoardError("invalid rumour-board status")
    if top["relation"] != RELATION:
        raise RumourBoardError("rumour-board relation must remain context-only")
    entries = top["entries"]
    if type(entries) is not list or len(entries) > 10_000:
        raise RumourBoardError("entries must be a bounded list")
    if type(top["n_entries"]) is not int or top["n_entries"] != len(entries):
        raise RumourBoardError("n_entries does not match entries")
    if (top["status"] == "RUMOUR_CONTEXT") != bool(entries):
        raise RumourBoardError("status does not match entry availability")
    if top["publication_policy"] != _POLICY:
        raise RumourBoardError("publication policy broadened the rumour boundary")

    seen: set[str] = set()
    for index, raw in enumerate(entries):
        path = f"entries[{index}]"
        row = _exact(raw, _ENTRY_FIELDS, path)
        if type(row["entry_id"]) is not str or not _ENTRY_ID.fullmatch(row["entry_id"]):
            raise RumourBoardError(f"{path}.entry_id is invalid")
        if row["entry_id"] in seen:
            raise RumourBoardError("duplicate rumour entry_id")
        seen.add(row["entry_id"])
        _timestamp(row["observed_at"], f"{path}.observed_at")
        if type(row["tap_id"]) is not str or not _IDENTIFIER.fullmatch(row["tap_id"]):
            raise RumourBoardError(f"{path}.tap_id is invalid")
        _text(row["surface"], f"{path}.surface", maximum=64)
        _text(row["title"], f"{path}.title", maximum=180)
        if row["relation"] != RELATION:
            raise RumourBoardError(f"{path}.relation is invalid")
        hits = row["gazetteer_hits"]
        if type(hits) is not list or hits != sorted(set(hits)):
            raise RumourBoardError(f"{path}.gazetteer_hits must be unique and sorted")
        for hit_index, hit in enumerate(hits):
            if type(hit) is not str or not _IDENTIFIER.fullmatch(hit):
                raise RumourBoardError(f"{path}.gazetteer_hits[{hit_index}] is invalid")
    limitations = top["limitations"]
    if type(limitations) is not list or not 3 <= len(limitations) <= 8:
        raise RumourBoardError("limitations must contain 3 to 8 statements")
    for index, value in enumerate(limitations):
        _text(value, f"limitations[{index}]", maximum=500)
    canonical_json_bytes(document)


def empty_document(generated_at: str) -> dict[str, Any]:
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": (
            "Reviewed rumour-board taps. Telegram volume still enters through "
            "ScamShield pins and social_sources.json."
        ),
        "method": (
            "Project metadata-only live events whose relation is rumour-board "
            "context. Drop media, bodies, and any post the safety filter cannot clear."
        ),
        "scope": (
            "Taken from rumour boards. This desk is circulation context. It does "
            "not add an independent source group and does not corroborate a wire event."
        ),
        "status": "COVERAGE_ONLY",
        "relation": RELATION,
        "n_entries": 0,
        "entries": [],
        "publication_policy": dict(_POLICY),
        "limitations": [
            "An empty ledger is a coverage gap, not evidence that rumour stopped.",
            "Rumour-board rows cannot increment independent publisher groups.",
            "t.me/s/ preview scraping remains unauthorized by the social-observation contract.",
        ],
    }
    validate_rumour_board(document)
    return document


def entry_id(event_id: str) -> str:
    digest = hashlib.sha256(event_id.encode("ascii")).hexdigest()
    return f"rumour-{digest[:24]}"


def project_events(
    events: Sequence[Mapping[str, Any]],
    *,
    generated_at: str,
) -> dict[str, Any]:
    """Keep rumour-relation live events. Other relations stay off this desk."""

    rows: list[dict[str, Any]] = []
    for event in events:
        live_event_model.validate_live_event(event)
        if event["relation"] != RELATION:
            continue
        rows.append(
            {
                "entry_id": entry_id(event["event_id"]),
                "observed_at": event["observed_at"],
                "tap_id": event["tap_id"],
                "surface": event["source_id"],
                "title": event["title"],
                "relation": RELATION,
                "gazetteer_hits": list(event["gazetteer_hits"]),
            }
        )
    rows.sort(key=lambda row: (row["observed_at"], row["entry_id"]), reverse=True)
    if not rows:
        return empty_document(generated_at)
    document = empty_document(generated_at)
    document["status"] = "RUMOUR_CONTEXT"
    document["n_entries"] = len(rows)
    document["entries"] = rows
    validate_rumour_board(document)
    return document


__all__ = [
    "RELATION",
    "RumourBoardError",
    "SCHEMA_VERSION",
    "canonical_json_bytes",
    "empty_document",
    "entry_id",
    "project_events",
    "validate_rumour_board",
]
