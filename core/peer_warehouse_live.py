"""Project already-published readings into interconnection warehouse slots.

This is not a collector. It reads files Palimpsest already sealed and emits
the closed ``palimpsest-peer-warehouse.v1`` shape the fat-object join expects.
Missing files, unreadable clocks, or rows with no extractable host / path /
term / ASN stay silent. Fuzzy tokens are never promoted to join keys.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from core.china_observation import iso_z, public_text
from core.event_interconnection import (
    SLOT_IDS,
    SLOT_NAMES,
    WAREHOUSE_SCHEMA,
    peer_record,
    registrable_domain,
    warehouse_fixture,
)
from core.live_paths import load_json_if_present


MAX_RECORDS = 48
_ASN = re.compile(r"AS(\d+)", re.IGNORECASE)
_SLUG = re.compile(r"[^a-z0-9]+")

LIVE_SOURCES: dict[str, tuple[str, ...]] = {
    "official-first-seen": ("official-first-seen-latest.json",),
    "greatfire": ("greatfire-context-latest.json",),
    "ooni": ("ooni-peer-context-latest.json", "ooni-gfw-latest.json"),
    "bleedthrough": ("bleedthrough-latest.json",),
    "wayback": ("wayback-latest.json",),
    "ddti": ("ddti-latest.json",),
    "weibo-hotsearch": ("weibo-hotsearch-latest.json",),
    "cdt": ("cdt-context-latest.json",),
    "gazetteer": ("weibo-hotsearch-latest.json",),
    "public-board": ("public-board-terms-latest.json",),
}


def _record_id(prefix: str, *parts: str) -> str:
    slug = _SLUG.sub("-", "-".join(part for part in parts if part).casefold()).strip("-")
    text = f"{prefix}-{slug}" if slug and not slug.startswith(prefix) else (slug or prefix)
    if not text or not text[0].isalpha():
        text = f"{prefix}-{text}".strip("-")
    return text[:80]


def _clock(*values: Any) -> str | None:
    for value in values:
        stamp = iso_z(value)
        if stamp:
            return stamp
    return None


def _hosts_from_url(value: Any) -> list[str]:
    if type(value) is not str or not value.strip():
        return []
    host = (urlsplit(value.strip()).hostname or "").casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return []
    keys = [host]
    registered = registrable_domain(host)
    if registered and registered not in keys:
        keys.append(registered)
    return keys


def _path_from_url(value: Any) -> list[str]:
    if type(value) is not str or "://" not in value:
        return []
    path = urlsplit(value).path.rstrip("/")
    return [path] if len(path) > 1 else []


def _term(value: Any) -> str:
    text = " ".join(public_text(value, limit=240).strip().casefold().split())
    return text if len(text) >= 2 else ""


def _asns(*values: Any) -> list[int]:
    found: list[int] = []
    seen: set[int] = set()
    for raw in values:
        if type(raw) is int and not isinstance(raw, bool) and raw > 0 and raw not in seen:
            seen.add(raw)
            found.append(raw)
            continue
        text = public_text(raw, limit=64)
        match = _ASN.search(text)
        if match:
            number = int(match.group(1))
            if number > 0 and number not in seen:
                seen.add(number)
                found.append(number)
    return found


def _append(
    records: list[dict[str, Any]],
    seen: set[str],
    prefix: str,
    *identity: str,
    hosts: list[str] | tuple[str, ...] = (),
    url_paths: list[str] | tuple[str, ...] = (),
    terms: list[str] | tuple[str, ...] = (),
    asns: list[int] | tuple[int, ...] = (),
    observed_at: str | None,
    count: float | int | None = None,
    count_label: str = "count",
    denominator_label: str = "declared denominator",
    denominator_value: float | int | None = None,
) -> None:
    if observed_at is None:
        return
    clean_hosts = [host for host in hosts if host]
    clean_paths = [path for path in url_paths if path]
    clean_terms = [term for term in terms if term]
    clean_asns = [asn for asn in asns if type(asn) is int and asn > 0]
    if not clean_hosts and not clean_paths and not clean_terms and not clean_asns:
        return
    record_id = _record_id(prefix, *identity)
    if record_id in seen:
        return
    seen.add(record_id)
    records.append(
        peer_record(
            record_id,
            hosts=clean_hosts,
            url_paths=clean_paths,
            terms=clean_terms,
            asns=clean_asns,
            observed_at=observed_at,
            count=count,
            count_label=count_label,
            denominator_label=denominator_label,
            denominator_value=denominator_value,
        )
    )


def _from_official(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("generated_at"))
    rows = document.get("pages") or document.get("observations") or document.get("records") or []
    if type(rows) is not list:
        return []
    for row in rows:
        if type(row) is not dict:
            continue
        url = row.get("url") or row.get("source_url") or row.get("page_url")
        _append(
            records,
            seen,
            "official",
            public_text(url, limit=200) or public_text(row.get("title"), limit=80),
            hosts=_hosts_from_url(url),
            url_paths=_path_from_url(url),
            terms=[_term(row.get("title") or row.get("term"))],
            observed_at=_clock(row.get("first_seen"), row.get("observed_at"), row.get("last_seen"), fallback),
            count=1,
            count_label="official pages first seen",
            denominator_label="official-first-seen watchlist",
            denominator_value=1,
        )
        if len(records) >= MAX_RECORDS:
            break
    return records


def _from_greatfire(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("generated_at"))
    rows = document.get("hosts") or document.get("verdicts") or []
    if type(rows) is not list:
        return []
    for row in rows:
        if type(row) is not dict:
            continue
        host = public_text(row.get("host") or row.get("query_url") or row.get("path"), limit=253)
        hosts = _hosts_from_url(host) if "://" in host else ([host.casefold()] if host else [])
        path = public_text(row.get("path"), limit=1024)
        _append(
            records,
            seen,
            "greatfire",
            host or path,
            hosts=hosts,
            url_paths=[path] if path.startswith("/") and len(path) > 1 else [],
            observed_at=_clock(row.get("last_tested_at"), row.get("last_tested"), row.get("as_of"), fallback),
            count=row.get("n_tests") if type(row.get("n_tests")) is int else row.get("blocked_count"),
            count_label="GreatFire blocked samples",
            denominator_label="GreatFire probe set",
            denominator_value=row.get("n_tests") if type(row.get("n_tests")) is int else None,
        )
        if len(records) >= MAX_RECORDS:
            break
    return records


def _from_ooni(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("until"), document.get("generated_at"))
    series = document.get("hosts") or document.get("series") or []
    if type(series) is list:
        for row in series:
            if type(row) is not dict or row.get("status") == "miss":
                continue
            host = public_text(row.get("host") or row.get("key"), limit=253)
            _append(
                records,
                seen,
                "ooni",
                host or public_text(row.get("asn"), limit=16),
                hosts=[host.casefold()] if host and "." in host else [],
                asns=_asns(row.get("asn"), row.get("key") if row.get("kind") == "asn" else None),
                observed_at=_clock(row.get("last_measured_at"), row.get("last_measurement"), fallback),
                count=row.get("n_measurements") if type(row.get("n_measurements")) is int else row.get("measurement_count"),
                count_label="OONI China measurements",
                denominator_label="OONI measurement set",
                denominator_value=row.get("n_measurements") if type(row.get("n_measurements")) is int else row.get("measurement_count"),
            )
            if len(records) >= MAX_RECORDS:
                return records
    blocked = document.get("top_blocked") or []
    if type(blocked) is list:
        for row in blocked:
            if type(row) is not dict:
                continue
            host = public_text(row.get("domain"), limit=253)
            if host.startswith("www."):
                host = host[4:]
            _append(
                records,
                seen,
                "ooni",
                host,
                hosts=[host] if host else [],
                observed_at=fallback,
                count=row.get("anomaly_count") if type(row.get("anomaly_count")) is int else None,
                count_label="OONI anomaly count",
                denominator_label="OONI completed measurements",
                denominator_value=row.get("completed_measurement_count")
                if type(row.get("completed_measurement_count")) is int
                else row.get("measurement_count"),
            )
            if len(records) >= MAX_RECORDS:
                break
    return records


def _from_bleedthrough(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("last_changed_at"), document.get("generated_at"))
    probe = public_text(document.get("probe_domain"), limit=253)
    events = document.get("events") or []
    if type(events) is not list:
        events = []
    if probe and fallback:
        asns = []
        for row in events:
            if type(row) is dict:
                asns.extend(_asns(row.get("vantage")))
        _append(
            records,
            seen,
            "bleedthrough",
            probe,
            hosts=[probe.casefold()],
            asns=asns,
            observed_at=fallback,
            count=len(events) or None,
            count_label="injector vantage rows",
            denominator_label="Bleedthrough vantage set",
            denominator_value=document.get("vantages_probed")
            if type(document.get("vantages_probed")) is int
            else None,
        )
    return records


def _from_wayback(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("generated_at"))
    rows = document.get("reconstructions") or []
    if type(rows) is not list:
        return []
    for row in rows:
        if type(row) is not dict:
            continue
        url = row.get("url")
        _append(
            records,
            seen,
            "wayback",
            public_text(url, limit=200) or _term(row.get("term")),
            hosts=_hosts_from_url(url),
            url_paths=_path_from_url(url),
            terms=[_term(row.get("term"))],
            observed_at=_clock(row.get("last_capture"), row.get("first_capture"), fallback),
            count=row.get("n_captures") if type(row.get("n_captures")) is int else None,
            count_label="Wayback captures",
            denominator_label="watched reconstructions",
            denominator_value=document.get("n_watched") if type(document.get("n_watched")) is int else None,
        )
        if len(records) >= MAX_RECORDS:
            break
    return records


def _from_ddti(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("generated_at"))
    rows = document.get("ranked") or []
    if type(rows) is not list:
        return []
    for row in rows:
        if type(row) is not dict:
            continue
        term = _term(row.get("term"))
        _append(
            records,
            seen,
            "ddti",
            term,
            terms=[term],
            observed_at=_clock(row.get("last_seen"), row.get("first_seen"), fallback),
            count=row.get("recent_count") if type(row.get("recent_count")) is int else None,
            count_label="DDTI recent mentions",
            denominator_label="DDTI ranked terms",
            denominator_value=document.get("n_terms") if type(document.get("n_terms")) is int else None,
        )
        if len(records) >= MAX_RECORDS:
            break
    return records


def _from_weibo(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("generated_at"))
    rows = document.get("observation_records") or []
    if type(rows) is not list:
        return []
    for row in rows:
        if type(row) is not dict:
            continue
        url = row.get("url") or row.get("source_url")
        terms = [_term(row.get("title"))]
        listed = row.get("terms") if type(row.get("terms")) is list else []
        terms.extend(_term(item) for item in listed)
        _append(
            records,
            seen,
            "weibo",
            _term(row.get("title")) or public_text(url, limit=80),
            hosts=_hosts_from_url(url) or ["s.weibo.com"],
            url_paths=_path_from_url(url),
            terms=terms,
            observed_at=_clock(
                row.get("last_seen"),
                row.get("first_seen"),
                row.get("last_confirmed_alive"),
                fallback,
            ),
            count=1,
            count_label="Weibo hot-search observations",
            denominator_label="Weibo observation records",
            denominator_value=len(rows),
        )
        if len(records) >= MAX_RECORDS:
            break
    return records


def _from_cdt(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("generated_at"))
    rows = document.get("items") or document.get("cdt_items") or []
    if type(rows) is not list:
        return []
    for row in rows:
        if type(row) is not dict:
            continue
        url = row.get("url")
        title = _term(row.get("title"))
        _append(
            records,
            seen,
            "cdt",
            title or public_text(url, limit=80),
            hosts=_hosts_from_url(url),
            url_paths=_path_from_url(url),
            terms=[title],
            observed_at=_clock(row.get("published_at"), row.get("observed_at"), fallback),
            count=1,
            count_label="CDT items",
            denominator_label="CDT item set",
            denominator_value=len(rows),
        )
        if len(records) >= MAX_RECORDS:
            break
    return records


def _from_gazetteer(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("generated_at"))
    rows = document.get("gazetteer_breakthroughs") or []
    if type(rows) is not list:
        return []
    for row in rows:
        if type(row) is not dict:
            continue
        term = _term(row.get("term"))
        days = row.get("days_present") if type(row.get("days_present")) is list else []
        day = days[-1] if days and type(days[-1]) is str else None
        observed = _clock(f"{day}T00:00:00Z" if day else None, fallback)
        _append(
            records,
            seen,
            "gazetteer",
            term,
            terms=[term],
            observed_at=observed,
            count=row.get("appearances") if type(row.get("appearances")) is int else None,
            count_label="gazetteer appearances",
            denominator_label="gazetteer breakthroughs",
            denominator_value=len(rows),
        )
        if len(records) >= MAX_RECORDS:
            break
    return records


def _from_public_board(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    fallback = _clock(document.get("generated_at"))
    rows = document.get("terms") or document.get("rows") or document.get("board_terms") or []
    if type(rows) is not list:
        return []
    for row in rows:
        if type(row) is not dict:
            continue
        term = _term(row.get("term") or row.get("title"))
        host = public_text(row.get("host"), limit=253)
        _append(
            records,
            seen,
            "board",
            term or host,
            hosts=[host.casefold()] if host else [],
            terms=[term],
            observed_at=_clock(row.get("last_seen"), row.get("first_seen"), row.get("observed_at"), fallback),
            count=row.get("rank") if type(row.get("rank")) is int else 1,
            count_label="public-board rank",
            denominator_label="public-board terms",
            denominator_value=len(rows),
        )
        if len(records) >= MAX_RECORDS:
            break
    return records


_PROJECTORS = {
    "official-first-seen": _from_official,
    "greatfire": _from_greatfire,
    "ooni": _from_ooni,
    "bleedthrough": _from_bleedthrough,
    "wayback": _from_wayback,
    "ddti": _from_ddti,
    "weibo-hotsearch": _from_weibo,
    "cdt": _from_cdt,
    "gazetteer": _from_gazetteer,
    "public-board": _from_public_board,
}


def project_live_warehouse(slot: str, document: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return one warehouse document or None when the slot cannot be projected."""

    if slot not in SLOT_IDS or type(document) is not dict:
        return None
    projector = _PROJECTORS[slot]
    generated_at = _clock(document.get("generated_at"), document.get("until"), document.get("last_changed_at"))
    if generated_at is None:
        return None
    state = public_text(document.get("anomaly_state") or document.get("status"), limit=32)
    if state == "warming_up":
        status = "warming_up"
        records: list[dict[str, Any]] = []
    else:
        records = projector(document)
        status = "live" if records else "silent"
    reading_name = LIVE_SOURCES[slot][0]
    warehouse = warehouse_fixture(
        slot,
        status=status,
        records=records,
        generated_at=generated_at,
        peer_name=SLOT_NAMES[slot],
    )
    warehouse["reading_url"] = f"https://palimpsest.info/readings/{reading_name}"
    warehouse["schema_version"] = WAREHOUSE_SCHEMA
    return warehouse


def project_live_warehouses(readings_dir: Path | str) -> dict[str, dict[str, Any] | None]:
    """Project every declared slot from one readings directory."""

    root = Path(readings_dir)
    loaded: dict[str, dict[str, Any] | None] = {slot: None for slot in SLOT_IDS}
    for slot, names in LIVE_SOURCES.items():
        for name in names:
            document = load_json_if_present(root / name)
            projected = project_live_warehouse(slot, document)
            if projected is None:
                continue
            loaded[slot] = projected
            break
    return loaded


__all__ = [
    "LIVE_SOURCES",
    "MAX_RECORDS",
    "project_live_warehouse",
    "project_live_warehouses",
]
