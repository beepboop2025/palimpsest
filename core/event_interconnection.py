"""Fail-closed named-key interconnection for one Palimpsest wire event.

The fat object is the product: every peer that belongs on the event is attached
with a named exact key and a recorded miss. Matches are never invented. Live
``*-latest.json`` readings are projected into warehouse slots when a warehouse
file is absent. Missing or unreadable sources stay silent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from core import live_paths


SCHEMA_VERSION = "palimpsest-event-interconnection.v1"
WAREHOUSE_SCHEMA = "palimpsest-peer-warehouse.v1"
QUALITY_BAR = "two-independent-source-groups"
RELATION = "topic-surface-only"
WINDOW_HOURS = 24
EXACT_KEYS = ("host", "url_path", "term", "asn")
SKIP_REASONS = frozenset({"no_key", "silent", "warming_up", "window_missed"})
PEER_STATUSES = frozenset({"joined", "skipped"})

SLOT_IDS = (
    "official-first-seen",
    "greatfire",
    "ooni",
    "bleedthrough",
    "wayback",
    "ddti",
    "weibo-hotsearch",
    "cdt",
    "gazetteer",
    "public-board",
)
SLOT_NAMES = {
    "official-first-seen": "official-first-seen",
    "greatfire": "GreatFire",
    "ooni": "OONI",
    "bleedthrough": "Bleedthrough",
    "wayback": "Wayback",
    "ddti": "DDTI",
    "weibo-hotsearch": "Weibo hot search",
    "cdt": "China Digital Times",
    "gazetteer": "gazetteer",
    "public-board": "public board",
}
WAREHOUSE_FILENAMES = {
    slot: (f"{slot}-warehouse.json", f"{slot}-latest.json")
    for slot in SLOT_IDS
}
WAREHOUSE_FILENAMES["ooni"] = ("ooni-warehouse.json", "ooni-gfw-warehouse.json")

_MULTI_SUFFIXES = (
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "ac.cn",
    "co.uk",
    "org.uk",
    "ac.uk",
    "com.hk",
    "gov.hk",
    "org.hk",
    "com.tw",
)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "relation",
        "quality_bar",
        "independent_source_groups",
        "meets_quality_bar",
        "joined_count",
        "required_exact_keys",
        "window",
        "event_keys",
        "peers",
    }
)
_WINDOW_FIELDS = frozenset({"unit", "radius_hours", "anchor"})
_EVENT_KEY_FIELDS = frozenset({"hosts", "url_paths", "terms", "calendar_day", "asns"})
_PEER_FIELDS = frozenset(
    {
        "peer_id",
        "record_id",
        "peer_name",
        "independence_group",
        "status",
        "join_keys",
        "why_joined",
        "why_skipped",
        "skip_reason",
        "peer_date",
        "observed_at",
        "count",
        "count_label",
        "denominator_label",
        "denominator_value",
        "citation",
        "reading_url",
        "input_sha256",
        "relation",
    }
)
_WAREHOUSE_FIELDS = frozenset(
    {
        "schema_version",
        "warehouse_id",
        "peer_name",
        "independence_group",
        "generated_at",
        "status",
        "reading_url",
        "peers",
    }
)
_RECORD_FIELDS = frozenset(
    {
        "record_id",
        "hosts",
        "url_paths",
        "terms",
        "asns",
        "observed_at",
        "count",
        "count_label",
        "denominator_label",
        "denominator_value",
    }
)


class InterconnectionError(ValueError):
    """The interconnection block violates its closed join contract."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
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
    except (TypeError, ValueError) as exc:
        raise InterconnectionError("interconnection payload is not canonical JSON") from exc


def _exact(value: Any, fields: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        missing = sorted(fields - set(value)) if isinstance(value, Mapping) else sorted(fields)
        extra = sorted(set(value) - fields) if isinstance(value, Mapping) else []
        raise InterconnectionError(f"{path} fields differ (missing={missing}, extra={extra})")
    return value


def _parse_time(value: Any) -> datetime | None:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _calendar_day(value: Any) -> str | None:
    clock = _parse_time(value)
    return clock.date().isoformat() if clock is not None else None


def _normalize_host(value: Any) -> str:
    if type(value) is not str:
        return ""
    text = value.strip().casefold().rstrip(".")
    if "://" in text or text.startswith("/"):
        text = (urlsplit(text).hostname or "").casefold().rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def registrable_domain(host: str) -> str:
    """Return a conservative registrable domain. Never invent a match."""

    labels = [part for part in host.split(".") if part]
    if len(labels) < 2:
        return host
    joined = ".".join(labels)
    for suffix in _MULTI_SUFFIXES:
        if joined == suffix or joined.endswith("." + suffix):
            needed = suffix.count(".") + 2
            return ".".join(labels[-needed:]) if len(labels) >= needed else joined
    return ".".join(labels[-2:])


def _host_keys(values: Sequence[Any]) -> set[str]:
    keys: set[str] = set()
    for raw in values:
        host = _normalize_host(raw)
        if not host:
            continue
        keys.add(host)
        keys.add(registrable_domain(host))
    return keys


def _path_key(value: Any) -> str:
    if type(value) is not str:
        return ""
    text = value.strip()
    if "://" in text:
        text = urlsplit(text).path
    path = text.rstrip("/")
    return path if len(path) > 1 else ""


def _term_key(value: Any) -> str:
    if type(value) is not str:
        return ""
    text = " ".join(value.strip().casefold().split())
    return text if len(text) >= 2 else ""


def _asn_keys(values: Sequence[Any]) -> set[int]:
    asns: set[int] = set()
    for raw in values:
        if type(raw) is int and not isinstance(raw, bool) and raw > 0:
            asns.add(raw)
    return asns


def event_join_keys(event: Mapping[str, Any]) -> dict[str, Any]:
    """Extract named keys from retained event metadata only."""

    hosts: set[str] = set()
    paths: set[str] = set()
    terms: set[str] = set()
    asns: set[int] = set()
    for ref in event.get("evidence_refs") or []:
        if type(ref) is not dict:
            continue
        url = ref.get("url")
        if type(url) is str and url:
            hosts |= _host_keys([url])
            path = _path_key(url)
            if path:
                paths.add(path)
        term = _term_key(ref.get("title"))
        if term:
            terms.add(term)
    for field in ("headline", "dek"):
        term = _term_key(event.get(field))
        if term:
            terms.add(term)
    listed = event.get("terms")
    if isinstance(listed, list):
        for item in listed:
            term = _term_key(item)
            if term:
                terms.add(term)
    asns |= _asn_keys(event.get("asns") or [])
    return {
        "hosts": sorted(hosts),
        "url_paths": sorted(paths),
        "terms": sorted(terms),
        "calendar_day": _calendar_day(event.get("published_at")),
        "asns": sorted(asns),
    }


def _record_keys(record: Mapping[str, Any]) -> dict[str, Any]:
    hosts = _host_keys(record.get("hosts") or [])
    paths = {_path_key(item) for item in (record.get("url_paths") or [])}
    paths.discard("")
    terms = {_term_key(item) for item in (record.get("terms") or [])}
    terms.discard("")
    return {
        "hosts": hosts,
        "url_paths": paths,
        "terms": terms,
        "asns": _asn_keys(record.get("asns") or []),
    }


def _shared_exact_keys(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> list[str]:
    keys: list[str] = []
    if set(left["hosts"]) & set(right["hosts"]):
        keys.append("host")
    if set(left["url_paths"]) & set(right["url_paths"]):
        keys.append("url_path")
    if set(left["terms"]) & set(right["terms"]):
        keys.append("term")
    if set(left["asns"]) & set(right["asns"]):
        keys.append("asn")
    return keys


def _why_joined(join_keys: Sequence[str], left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in join_keys:
        if key == "host":
            shared = sorted(set(left["hosts"]) & set(right["hosts"]))
            parts.append("host " + ", ".join(shared[:3]))
        elif key == "url_path":
            shared = sorted(set(left["url_paths"]) & set(right["url_paths"]))
            parts.append("url_path " + ", ".join(shared[:3]))
        elif key == "term":
            shared = sorted(set(left["terms"]) & set(right["terms"]))
            parts.append("term " + ", ".join(shared[:2]))
        elif key == "asn":
            shared = sorted(set(left["asns"]) & set(right["asns"]))
            parts.append("asn " + ", ".join(f"AS{item}" for item in shared[:3]))
    return "exact " + "; ".join(parts)


def _within_window(event_clock: datetime | None, peer_clock: datetime | None) -> bool:
    if event_clock is None or peer_clock is None:
        return False
    return abs(event_clock - peer_clock) <= timedelta(hours=WINDOW_HOURS)


def _finite_or_none(value: Any) -> float | int | None:
    if value is None:
        return None
    if type(value) is int and not isinstance(value, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise InterconnectionError("peer count or denominator is not finite")


def _empty_slot(
    slot: str,
    *,
    skip_reason: str,
    why_skipped: str,
    reading_url: str | None = None,
    input_sha256: str | None = None,
    independence_group: str | None = None,
    peer_name: str | None = None,
) -> dict[str, Any]:
    return {
        "peer_id": slot,
        "record_id": None,
        "peer_name": peer_name or SLOT_NAMES[slot],
        "independence_group": independence_group or slot,
        "status": "skipped",
        "join_keys": [],
        "why_joined": None,
        "why_skipped": why_skipped,
        "skip_reason": skip_reason,
        "peer_date": None,
        "observed_at": None,
        "count": None,
        "count_label": None,
        "denominator_label": None,
        "denominator_value": None,
        "citation": None,
        "reading_url": reading_url,
        "input_sha256": input_sha256,
        "relation": RELATION,
    }


def _joined_row(
    warehouse: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    join_keys: Sequence[str],
    why_joined: str,
) -> dict[str, Any]:
    observed = record["observed_at"]
    day = _calendar_day(observed)
    name = str(warehouse["peer_name"])
    return {
        "peer_id": warehouse["warehouse_id"],
        "record_id": record["record_id"],
        "peer_name": name,
        "independence_group": warehouse["independence_group"],
        "status": "joined",
        "join_keys": list(join_keys),
        "why_joined": why_joined,
        "why_skipped": None,
        "skip_reason": None,
        "peer_date": day,
        "observed_at": observed,
        "count": _finite_or_none(record.get("count")),
        "count_label": record.get("count_label") or "count",
        "denominator_label": record.get("denominator_label") or "declared denominator",
        "denominator_value": _finite_or_none(record.get("denominator_value")),
        "citation": f"{name}, {day}" if day else name,
        "reading_url": warehouse.get("reading_url"),
        "input_sha256": warehouse.get("input_sha256"),
        "relation": RELATION,
    }


def validate_peer_warehouse(document: Any) -> Mapping[str, Any]:
    allowed = _WAREHOUSE_FIELDS | ({"input_sha256"} & set(document or {}))
    _exact(document, allowed, "warehouse")
    if document.get("schema_version") != WAREHOUSE_SCHEMA:
        raise InterconnectionError("warehouse.schema_version is unsupported")
    if document["warehouse_id"] not in SLOT_IDS:
        raise InterconnectionError("warehouse.warehouse_id is not a declared slot")
    if document["status"] not in {"live", "silent", "warming_up"}:
        raise InterconnectionError("warehouse.status is invalid")
    if type(document["peer_name"]) is not str or not document["peer_name"].strip():
        raise InterconnectionError("warehouse.peer_name is invalid")
    if type(document["independence_group"]) is not str or _IDENTIFIER.fullmatch(document["independence_group"]) is None:
        raise InterconnectionError("warehouse.independence_group is invalid")
    if type(document["generated_at"]) is not str or _TIMESTAMP.fullmatch(document["generated_at"]) is None:
        raise InterconnectionError("warehouse.generated_at is invalid")
    peers = document["peers"]
    if type(peers) is not list or len(peers) > 4096:
        raise InterconnectionError("warehouse.peers is invalid")
    seen: set[str] = set()
    for index, row in enumerate(peers):
        item = _exact(row, _RECORD_FIELDS, f"warehouse.peers[{index}]")
        if type(item["record_id"]) is not str or _IDENTIFIER.fullmatch(item["record_id"]) is None:
            raise InterconnectionError(f"warehouse.peers[{index}].record_id is invalid")
        if item["record_id"] in seen:
            raise InterconnectionError("warehouse.peers record_id is not unique")
        seen.add(item["record_id"])
        if type(item["observed_at"]) is not str or _TIMESTAMP.fullmatch(item["observed_at"]) is None:
            raise InterconnectionError(f"warehouse.peers[{index}].observed_at is invalid")
        _finite_or_none(item["count"])
        _finite_or_none(item["denominator_value"])
        for field in ("hosts", "url_paths", "terms"):
            values = item[field]
            if type(values) is not list or any(type(value) is not str or not value for value in values):
                raise InterconnectionError(f"warehouse.peers[{index}].{field} is invalid")
        asns = item["asns"]
        if type(asns) is not list or any(type(value) is not int or isinstance(value, bool) or value < 1 for value in asns):
            raise InterconnectionError(f"warehouse.peers[{index}].asns is invalid")
    digest = document.get("input_sha256")
    if digest is not None and (type(digest) is not str or _SHA256.fullmatch(digest) is None):
        raise InterconnectionError("warehouse.input_sha256 is invalid")
    return document


def _normalize_warehouse(slot: str, raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    if type(raw) is not dict:
        raise InterconnectionError(f"{slot} warehouse is not an object")
    if raw.get("schema_version") != WAREHOUSE_SCHEMA:
        return None
    document = validate_peer_warehouse(raw)
    if document["warehouse_id"] != slot:
        raise InterconnectionError(f"{slot} warehouse_id drifted")
    payload = dict(document)
    if payload.get("input_sha256") is None:
        payload["input_sha256"] = hashlib.sha256(canonical_json_bytes(raw)).hexdigest()
    return payload


def load_optional_peer_warehouses(
    readings_dir: Path | str | None = None,
    *,
    project_live: bool = True,
) -> dict[str, dict[str, Any] | None]:
    """Load warehouse-shaped files when present. Missing slots may project live readings."""

    loaded: dict[str, dict[str, Any] | None] = {slot: None for slot in SLOT_IDS}
    search_dirs = live_paths.readings_search_dirs(preferred=readings_dir)
    for root in search_dirs:
        for slot, names in WAREHOUSE_FILENAMES.items():
            if loaded[slot] is not None:
                continue
            for name in names:
                path = root / name
                value = live_paths.load_json_if_present(path)
                if value is None:
                    continue
                if value.get("schema_version") != WAREHOUSE_SCHEMA:
                    continue
                loaded[slot] = _normalize_warehouse(slot, value)
    if project_live:
        from core import peer_warehouse_live

        for root in search_dirs:
            projected = peer_warehouse_live.project_live_warehouses(root)
            for slot, warehouse in projected.items():
                if loaded[slot] is None and warehouse is not None:
                    loaded[slot] = _normalize_warehouse(slot, warehouse)
    return loaded


def _merge_keys(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "hosts": set(base["hosts"]) | set(extra["hosts"]),
        "url_paths": set(base["url_paths"]) | set(extra["url_paths"]),
        "terms": set(base["terms"]) | set(extra["terms"]),
        "asns": set(base["asns"]) | set(extra["asns"]),
    }


def build_interconnection(
    event: Mapping[str, Any],
    warehouses: Mapping[str, Mapping[str, Any] | None] | None = None,
    *,
    scope_status: str = "in-scope",
) -> dict[str, Any]:
    """Attach every peer that shares an exact named key inside the ±24h window."""

    event_keys = event_join_keys(event)
    event_clock = _parse_time(event.get("published_at"))
    working_keys = {
        "hosts": set(event_keys["hosts"]),
        "url_paths": set(event_keys["url_paths"]),
        "terms": set(event_keys["terms"]),
        "asns": set(event_keys["asns"]),
    }
    supplied = warehouses or {}
    joined: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    in_scope = scope_status == "in-scope"

    for slot in SLOT_IDS:
        raw = supplied.get(slot)
        warehouse = _normalize_warehouse(slot, raw) if raw is not None else None
        if not in_scope:
            skipped.append(
                _empty_slot(
                    slot,
                    skip_reason="no_key",
                    why_skipped="event is outside the China remit; no interconnection peer is attached",
                    reading_url=(warehouse or {}).get("reading_url") if warehouse else None,
                    input_sha256=(warehouse or {}).get("input_sha256") if warehouse else None,
                    independence_group=(warehouse or {}).get("independence_group") if warehouse else slot,
                    peer_name=(warehouse or {}).get("peer_name") if warehouse else None,
                )
            )
            continue
        if warehouse is None:
            skipped.append(
                _empty_slot(
                    slot,
                    skip_reason="silent",
                    why_skipped="peer warehouse is missing; slot left empty for a later fill",
                )
            )
            continue
        if warehouse["status"] == "silent":
            skipped.append(
                _empty_slot(
                    slot,
                    skip_reason="silent",
                    why_skipped="peer warehouse is present but silent; no extractable join keys",
                    reading_url=warehouse.get("reading_url"),
                    input_sha256=warehouse.get("input_sha256"),
                    independence_group=warehouse["independence_group"],
                    peer_name=warehouse["peer_name"],
                )
            )
            continue
        if warehouse["status"] == "warming_up":
            skipped.append(
                _empty_slot(
                    slot,
                    skip_reason="warming_up",
                    why_skipped="peer warehouse anomaly_state is warming_up; no peer is published as a finding",
                    reading_url=warehouse.get("reading_url"),
                    input_sha256=warehouse.get("input_sha256"),
                    independence_group=warehouse["independence_group"],
                    peer_name=warehouse["peer_name"],
                )
            )
            continue
        records = warehouse.get("peers") or []
        slot_joined = 0
        window_misses = 0
        for record in records:
            if type(record) is not dict:
                continue
            peer_keys = _record_keys(record)
            join_keys = _shared_exact_keys(working_keys, peer_keys)
            peer_clock = _parse_time(record.get("observed_at"))
            if not join_keys:
                continue
            if not _within_window(event_clock, peer_clock):
                window_misses += 1
                continue
            row = _joined_row(
                warehouse,
                record,
                join_keys=join_keys,
                why_joined=_why_joined(join_keys, working_keys, peer_keys),
            )
            joined.append(row)
            slot_joined += 1
            working_keys = _merge_keys(working_keys, peer_keys)
        if slot_joined == 0:
            if window_misses:
                skipped.append(
                    _empty_slot(
                        slot,
                        skip_reason="window_missed",
                        why_skipped=(
                            "exact key overlapped but the UTC ±24h window from "
                            "event.published_at missed; no cross-day story is invented"
                        ),
                        reading_url=warehouse.get("reading_url"),
                        input_sha256=warehouse.get("input_sha256"),
                        independence_group=warehouse["independence_group"],
                        peer_name=warehouse["peer_name"],
                    )
                )
            else:
                skipped.append(
                    _empty_slot(
                        slot,
                        skip_reason="no_key",
                        why_skipped="no exact host, url_path, term, or ASN is shared with this event",
                        reading_url=warehouse.get("reading_url"),
                        input_sha256=warehouse.get("input_sha256"),
                        independence_group=warehouse["independence_group"],
                        peer_name=warehouse["peer_name"],
                    )
                )

    groups = {
        group.get("group_id")
        for group in (event.get("evidence_groups") or [])
        if type(group) is dict and type(group.get("group_id")) is str and group["group_id"]
    }
    group_count = len(groups)
    peers = [*joined, *skipped]
    block = {
        "schema_version": SCHEMA_VERSION,
        "relation": RELATION,
        "quality_bar": QUALITY_BAR,
        "independent_source_groups": group_count,
        "meets_quality_bar": group_count >= 2,
        "joined_count": len(joined),
        "required_exact_keys": list(EXACT_KEYS),
        "window": {
            "unit": "hours",
            "radius_hours": WINDOW_HOURS,
            "anchor": "event.published_at",
        },
        "event_keys": event_keys,
        "peers": peers,
    }
    validate_interconnection(block, event=event)
    return block


def interconnection_position_clause(block: Mapping[str, Any]) -> str:
    joined = [row for row in block.get("peers") or [] if row.get("status") == "joined"]
    if not joined:
        return "No interconnection peer met an exact join key."
    cites = ", ".join(
        row["citation"] for row in joined if type(row.get("citation")) is str and row["citation"]
    )
    bar = (
        "Quality bar two-independent-source-groups is met."
        if block.get("meets_quality_bar")
        else "Quality bar two-independent-source-groups is not met."
    )
    return f"Joined peers: {cites}. {bar}"


def peer_brief_sentence(row: Mapping[str, Any]) -> str:
    """Cite peer name + date and keep that peer's own count and denominator."""

    citation = row.get("citation") or row.get("peer_name") or row.get("peer_id")
    count = row.get("count")
    label = row.get("count_label") or "count"
    denom_label = row.get("denominator_label") or "declared denominator"
    denom_value = row.get("denominator_value")
    if count is None:
        return f"{citation}: no count is published; this peer is attached by join key only."
    if type(count) is float and not count.is_integer():
        number = f"{count:.4g}"
    else:
        number = f"{int(count):,}"
    if denom_value is None:
        denom = f"{denom_label} not reported"
    elif type(denom_value) is float and not denom_value.is_integer():
        denom = f"{denom_value:.4g} {denom_label}"
    else:
        denom = f"{int(denom_value):,} {denom_label}"
    return f"{citation}: {number} {label} over {denom}."


def validate_interconnection(
    block: Any, *, event: Mapping[str, Any] | None = None
) -> None:
    document = _exact(block, _ROOT_FIELDS, "interconnection")
    if document["schema_version"] != SCHEMA_VERSION:
        raise InterconnectionError("interconnection.schema_version is unsupported")
    if document["relation"] != RELATION:
        raise InterconnectionError("interconnection relation may not imply verification")
    if document["quality_bar"] != QUALITY_BAR:
        raise InterconnectionError("interconnection quality bar drifted")
    if type(document["independent_source_groups"]) is not int or document["independent_source_groups"] < 0:
        raise InterconnectionError("interconnection.independent_source_groups is invalid")
    if type(document["meets_quality_bar"]) is not bool:
        raise InterconnectionError("interconnection.meets_quality_bar is invalid")
    if document["meets_quality_bar"] is not (document["independent_source_groups"] >= 2):
        raise InterconnectionError("interconnection quality-bar flag does not match group count")
    if document["required_exact_keys"] != list(EXACT_KEYS):
        raise InterconnectionError("interconnection.required_exact_keys drifted")
    window = _exact(document["window"], _WINDOW_FIELDS, "interconnection.window")
    if window != {"unit": "hours", "radius_hours": WINDOW_HOURS, "anchor": "event.published_at"}:
        raise InterconnectionError("interconnection.window drifted")
    keys = _exact(document["event_keys"], _EVENT_KEY_FIELDS, "interconnection.event_keys")
    if keys["calendar_day"] is not None and (
        type(keys["calendar_day"]) is not str or _DAY.fullmatch(keys["calendar_day"]) is None
    ):
        raise InterconnectionError("interconnection.event_keys.calendar_day is invalid")
    if event is not None:
        expected = event_join_keys(event)
        if keys != expected:
            raise InterconnectionError("interconnection.event_keys do not match the event")
    peers = document["peers"]
    if type(peers) is not list or len(peers) > 64:
        raise InterconnectionError("interconnection.peers is invalid")
    joined = 0
    seen_slots: set[str] = set()
    for index, row in enumerate(peers):
        item = _exact(row, _PEER_FIELDS, f"interconnection.peers[{index}]")
        if item["peer_id"] not in SLOT_IDS:
            raise InterconnectionError(f"interconnection.peers[{index}].peer_id is undeclared")
        if item["status"] not in PEER_STATUSES:
            raise InterconnectionError(f"interconnection.peers[{index}].status is invalid")
        if item["relation"] != RELATION:
            raise InterconnectionError("interconnection peer relation may not imply verification")
        if any(key not in EXACT_KEYS for key in item["join_keys"]):
            raise InterconnectionError(f"interconnection.peers[{index}].join_keys is invalid")
        if item["status"] == "joined":
            joined += 1
            if not item["join_keys"] or item["why_joined"] is None or item["skip_reason"] is not None:
                raise InterconnectionError("joined peer is missing an exact key receipt")
            if type(item["citation"]) is not str or not item["citation"]:
                raise InterconnectionError("joined peer is missing a name-and-date citation")
            if item["peer_date"] is None or _DAY.fullmatch(str(item["peer_date"])) is None:
                raise InterconnectionError("joined peer is missing its own date")
        else:
            if item["skip_reason"] not in SKIP_REASONS or item["why_skipped"] is None:
                raise InterconnectionError("skipped peer is missing a fail-closed reason")
            if item["why_joined"] is not None:
                raise InterconnectionError("skipped peer claimed a join")
        if item["record_id"] is None:
            seen_slots.add(item["peer_id"])
        if item["input_sha256"] is not None and (
            type(item["input_sha256"]) is not str or _SHA256.fullmatch(item["input_sha256"]) is None
        ):
            raise InterconnectionError("interconnection peer hash is invalid")
    if document["joined_count"] != joined:
        raise InterconnectionError("interconnection.joined_count does not match joined peers")
    if set(SLOT_IDS) - seen_slots - {row["peer_id"] for row in peers if row["status"] == "joined"}:
        # Every slot must appear as a joined peer or a slot-level / record-level skip.
        covered = {row["peer_id"] for row in peers}
        if covered != set(SLOT_IDS):
            raise InterconnectionError("interconnection does not account for every declared slot")


def warehouse_fixture(
    slot: str,
    *,
    status: str = "live",
    records: Sequence[Mapping[str, Any]] = (),
    generated_at: str = "2026-08-20T06:00:00Z",
    independence_group: str | None = None,
    peer_name: str | None = None,
) -> dict[str, Any]:
    """Build one validated warehouse document for tests and later collector fills."""

    document = {
        "schema_version": WAREHOUSE_SCHEMA,
        "warehouse_id": slot,
        "peer_name": peer_name or SLOT_NAMES[slot],
        "independence_group": independence_group or slot,
        "generated_at": generated_at,
        "status": status,
        "reading_url": f"https://palimpsest.info/readings/{slot}-warehouse.json",
        "peers": [dict(record) for record in records],
    }
    validate_peer_warehouse(document)
    return document


def peer_record(
    record_id: str,
    *,
    hosts: Sequence[str] = (),
    url_paths: Sequence[str] = (),
    terms: Sequence[str] = (),
    asns: Sequence[int] = (),
    observed_at: str,
    count: float | int | None = None,
    count_label: str = "count",
    denominator_label: str = "declared denominator",
    denominator_value: float | int | None = None,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "hosts": list(hosts),
        "url_paths": list(url_paths),
        "terms": list(terms),
        "asns": list(asns),
        "observed_at": observed_at,
        "count": count,
        "count_label": count_label,
        "denominator_label": denominator_label,
        "denominator_value": denominator_value,
    }


__all__ = [
    "EXACT_KEYS",
    "QUALITY_BAR",
    "SCHEMA_VERSION",
    "SLOT_IDS",
    "WAREHOUSE_SCHEMA",
    "WINDOW_HOURS",
    "InterconnectionError",
    "build_interconnection",
    "event_join_keys",
    "interconnection_position_clause",
    "load_optional_peer_warehouses",
    "peer_brief_sentence",
    "peer_record",
    "registrable_domain",
    "validate_interconnection",
    "validate_peer_warehouse",
    "warehouse_fixture",
]
