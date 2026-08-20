"""Peer-context review ranker for GreatFire, OONI, and CDT.

Reuses the in-tree unusualness and editorial_priority trainers. It orders and
flags peer rows; it does not write motive, replace event_analysis sentences, or
train on Weiboscope's 2012 dump. Features come from cached peer-warehouse
files when present. Missing warehouse files fail closed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from collectors.common_crawl_lake import _canonical_json
from processors.editorial_priority import editorial_priority
from processors.reading_analysis import (
    FORBIDDEN_COPY,
    MAD_MIN_HISTORY,
    UNUSUAL_THRESHOLD,
    review_rank_meaning,
    robust_unusualness,
)


UTC = timezone.utc
SCHEMA = "palimpsest-peer-context/v1"
JOB = "peer-context"
FEATURE_SCHEMA = "palimpsest-peer-context-features/v1"
EXCERPT_CHARS = 280
WEIBOSCOPE_CITATION = (
    "Weiboscope is cited as a literature pointer only; the 2012 dump is not "
    "loaded or trained on"
)
METHOD = (
    "Per-peer robust MAD (prequential-robust-mad/v1) against that peer series' "
    "own history, then a fail-closed join onto a Palimpsest object by "
    "term/host/ASN/day. Review rank only. No generative brief. " + WEIBOSCOPE_CITATION
)

PEER_FILES = {
    "greatfire": "greatfire-context-latest.json",
    "greatfire_history": "greatfire-context-history.jsonl",
    "ooni": "ooni-peer-context-latest.json",
    "ooni_history": "ooni-peer-context-history.jsonl",
    "cdt": "cdt-context-latest.json",
    "cdt_history": "cdt-context-history.jsonl",
}


def _optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _iso_now(now: datetime | None) -> str:
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    return clock.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _host(value: object) -> str | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if "://" in text:
        text = (urlsplit(text).hostname or "").casefold()
    text = text.removeprefix("www.")
    return text or None


def _asn(value: object) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if "/AS" in text:
        text = "AS" + text.rsplit("/AS", 1)[1]
    if text.startswith("AS") and text[2:].isdigit():
        return text
    if text.isdigit():
        return f"AS{text}"
    return None


def _day(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    return text[:10] if len(text) >= 10 else None


def _term(value: object) -> str | None:
    text = " ".join(str(value or "").casefold().split())
    return text or None


def bound_excerpt(value: object, *, limit: int = EXCERPT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def peer_copy(row: Mapping[str, Any]) -> str:
    """Context-only flag. Cites the peer name and their date. No motive."""

    peer = str(row.get("peer") or "peer")
    date = str(row.get("peer_date") or "undated")
    n_history = int(row.get("n_history") or 0)
    state = row.get("state")
    if state == "warming_up":
        copy = (
            f"{peer} {date}: this series is warming up vs its own {n_history} "
            "prior points"
        )
    elif row.get("unusual") is True:
        copy = f"{peer} {date}: this series is unusual vs its own {n_history} prior points"
    else:
        copy = f"{peer} {date}: this series is within its own {n_history} prior points"
    if any(token in copy.casefold() for token in FORBIDDEN_COPY):
        raise ValueError("peer-context copy is not context-only")
    return copy


def _score_series(
    values: list[float],
    *,
    peer: str,
    series_id: str,
    field: str,
    peer_date: str | None,
    minimum: int = MAD_MIN_HISTORY,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = values[-1] if values else None
    prior = values[:-1] if values else []
    unusualness = robust_unusualness(current, prior, side="high", minimum=minimum)
    if unusualness is None:
        state = "warming_up"
        unusual = None
    else:
        state = "scored"
        unusual = unusualness >= UNUSUAL_THRESHOLD
    row: dict[str, Any] = {
        "schema_version": FEATURE_SCHEMA,
        "peer": peer,
        "series_id": series_id,
        "field": field,
        "peer_date": peer_date,
        "current_value": current,
        "n_history": len(prior),
        "minimum_prior": minimum,
        "state": state,
        "unusualness": unusualness,
        "unusual": unusual,
        "label": None,
        "label_source": "human-editorial-review-required",
        "rights": {"training_use": "derived_only"},
        "citation": WEIBOSCOPE_CITATION if peer == "weiboscope" else None,
    }
    if extra:
        row.update(extra)
    row["public_copy"] = peer_copy(row)
    return row


def _history_values(rows: list[dict[str, Any]], key: str, field: str) -> list[float]:
    values = []
    for row in rows:
        if str(row.get("series_id") or row.get("host") or row.get("key") or "") != key:
            continue
        raw = row.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        values.append(float(raw))
    return values


def fit_greatfire(document: Mapping[str, Any] | None, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """90-day block-share vs that host's own GreatFire history. Cached verdicts only."""

    if not isinstance(document, Mapping) or document.get("schema_version") != "palimpsest-greatfire-context/v1":
        return []
    fitted = []
    for host_row in document.get("hosts") or []:
        if not isinstance(host_row, dict):
            continue
        host = _host(host_row.get("host"))
        share = host_row.get("block_share")
        if host is None or not isinstance(share, (int, float)) or isinstance(share, bool):
            continue
        prior = _history_values(history, host, "block_share")
        values = prior + [float(share)]
        fitted.append(
            _score_series(
                values,
                peer="GreatFire",
                series_id=host,
                field="block_share_90d",
                peer_date=_day(host_row.get("peer_date") or host_row.get("observed_at")),
                extra={
                    "host": host,
                    "window_days": 90,
                    "blocked": host_row.get("blocked"),
                    "tested": host_row.get("tested"),
                    "source": "cached-verdicts-only",
                },
            )
        )
    return fitted


def fit_ooni(
    document: Mapping[str, Any] | None,
    history: list[dict[str, Any]],
    *,
    gfw_history: list[float] | None = None,
    gfw_date: str | None = None,
) -> list[dict[str, Any]]:
    """Anomaly rate vs that host/ASN's own OONI series. No invented mutation scores."""

    fitted = []
    if isinstance(document, Mapping) and document.get("schema_version") == "palimpsest-ooni-peer-context/v1":
        for series in document.get("series") or []:
            if not isinstance(series, dict):
                continue
            kind = series.get("kind")
            key = _host(series.get("key")) if kind == "host" else _asn(series.get("key"))
            rate = series.get("anomaly_rate")
            if key is None or not isinstance(rate, (int, float)) or isinstance(rate, bool):
                continue
            prior = _history_values(history, key, "anomaly_rate")
            fitted.append(
                _score_series(
                    prior + [float(rate)],
                    peer="OONI",
                    series_id=key,
                    field="anomaly_rate",
                    peer_date=_day(series.get("peer_date") or series.get("observed_at")),
                    extra={
                        "kind": kind,
                        "host": key if kind == "host" else None,
                        "asn": key if kind == "asn" else None,
                        "n_measurements": series.get("n_measurements"),
                    },
                )
            )
    if gfw_history:
        fitted.append(
            _score_series(
                gfw_history,
                peer="OONI",
                series_id="cn-aggregate",
                field="gfw_index",
                peer_date=gfw_date,
                extra={"kind": "country", "probe_cc": "CN"},
            )
        )
    return fitted


def fit_cdt(document: Mapping[str, Any] | None, history: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Weekly title volume vs CDT's own recent weeks. Titles/excerpts only."""

    items: list[dict[str, Any]] = []
    if isinstance(document, Mapping) and document.get("schema_version") == "palimpsest-cdt-context/v1":
        for raw in document.get("items") or []:
            if not isinstance(raw, dict):
                continue
            title = bound_excerpt(raw.get("title"))
            excerpt = bound_excerpt(raw.get("excerpt") or raw.get("title"))
            if not title:
                continue
            items.append(
                {
                    "item_id": raw.get("item_id") or raw.get("url"),
                    "title": title,
                    "excerpt": excerpt,
                    "url": raw.get("url"),
                    "host": _host(raw.get("host") or raw.get("url")),
                    "terms": [
                        term
                        for term in (_term(item) for item in (raw.get("terms") or []))
                        if term
                    ],
                    "day": _day(raw.get("published_at") or raw.get("peer_date")),
                    "peer_date": _day(raw.get("peer_date") or raw.get("published_at")),
                    "peer": "CDT",
                }
            )
        weeks = []
        for row in document.get("weeks") or []:
            if isinstance(row, dict) and isinstance(row.get("n_titles"), (int, float)):
                weeks.append(float(row["n_titles"]))
        if not weeks:
            weeks = _history_values(history, "cdt-weekly-titles", "n_titles")
            current = document.get("current_week_titles")
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                weeks = weeks + [float(current)]
        date = None
        if document.get("weeks") and isinstance(document["weeks"][-1], dict):
            date = _day(document["weeks"][-1].get("week_start"))
        series = []
        if weeks:
            series.append(
                _score_series(
                    weeks,
                    peer="CDT",
                    series_id="cdt-weekly-titles",
                    field="n_titles",
                    peer_date=date or (items[-1]["peer_date"] if items else None),
                    extra={"kind": "weekly-title-volume"},
                )
            )
        return series, items
    return [], []


def palimpsest_object_keys(obj: Mapping[str, Any]) -> dict[str, set[str]]:
    """Extract join keys. Empty keys mean the object cannot take a peer score."""

    hosts: set[str] = set()
    terms: set[str] = set()
    asns: set[str] = set()
    days: set[str] = set()
    signals: set[str] = set()

    kind = str(obj.get("kind") or "")
    if kind == "official-first-seen":
        for page in obj.get("pages") or []:
            if isinstance(page, dict):
                host = _host(page.get("url") or page.get("host"))
                if host:
                    hosts.add(host)
        day = _day(obj.get("generated_at") or obj.get("day"))
        if day:
            days.add(day)
    elif kind == "board-term":
        term = _term(obj.get("term"))
        if term:
            terms.add(term)
        for field in ("first_seen", "last_seen", "day"):
            day = _day(obj.get(field))
            if day:
                days.add(day)
    elif kind == "bleedthrough-host":
        host = _host(obj.get("host") or obj.get("probe_domain"))
        if host:
            hosts.add(host)
        for event in obj.get("events") or []:
            if isinstance(event, dict):
                asn = _asn(event.get("vantage") or event.get("asn"))
                if asn:
                    asns.add(asn)
        day = _day(obj.get("generated_at") or obj.get("day"))
        if day:
            days.add(day)
    elif kind == "wire-event":
        host = _host(obj.get("url") or obj.get("host"))
        if host:
            hosts.add(host)
        for topic in obj.get("topics") or obj.get("terms") or []:
            term = _term(topic)
            if term:
                terms.add(term)
        day = _day(obj.get("published_at") or obj.get("day"))
        if day:
            days.add(day)
        declared = obj.get("declared_links") if isinstance(obj.get("declared_links"), dict) else {}
        for field in ("scan_signal_ids", "economic_signal_ids"):
            for item in declared.get(field) or []:
                if isinstance(item, str) and item:
                    signals.add(item)
    return {"hosts": hosts, "terms": terms, "asns": asns, "days": days, "signals": signals}


def _peer_keys(row: Mapping[str, Any], items: list[dict[str, Any]] | None = None) -> dict[str, set[str]]:
    hosts, terms, asns, days, signals = set(), set(), set(), set(), set()
    host = _host(row.get("host") or (row.get("series_id") if row.get("kind") == "host" else None))
    if host:
        hosts.add(host)
    asn = _asn(row.get("asn") or (row.get("series_id") if row.get("kind") == "asn" else None))
    if asn:
        asns.add(asn)
    day = _day(row.get("peer_date"))
    if day:
        days.add(day)
    if row.get("series_id") == "cn-aggregate":
        signals.update({"ooni-gfw", "ooni-bulk"})
    if row.get("series_id") == "cdt-weekly-titles" and items:
        for item in items:
            if item.get("host"):
                hosts.add(item["host"])
            if item.get("day"):
                days.add(item["day"])
            terms.update(item.get("terms") or [])
    return {"hosts": hosts, "terms": terms, "asns": asns, "days": days, "signals": signals}


_SUBSTANTIVE_JOIN = frozenset({"hosts", "asns", "terms", "signals"})


def _overlap(object_keys: Mapping[str, set[str]], peer_keys: Mapping[str, set[str]]) -> list[str]:
    """Day overlap can strengthen a join; it cannot create one by itself."""

    matched = []
    substantive = False
    for name in ("hosts", "asns", "terms", "days", "signals"):
        if object_keys.get(name) and object_keys[name] & peer_keys.get(name, set()):
            matched.append(name[:-1] if name.endswith("s") else name)
            if name in _SUBSTANTIVE_JOIN:
                substantive = True
    return matched if substantive else []


def rank_joins(
    obj: Mapping[str, Any],
    peer_rows: list[dict[str, Any]],
    *,
    cdt_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fail closed: no overlapping peer row, no score."""

    object_keys = palimpsest_object_keys(obj)
    if not any(object_keys.values()):
        return []
    ranked = []
    for row in peer_rows:
        matched = _overlap(object_keys, _peer_keys(row, cdt_items))
        if not matched:
            continue
        groups = len(set(matched))
        strength = 2 if {"host", "asn"} & set(matched) else 1
        features = {
            "archive_targets": 0,
            "archive_anomaly_max": None,
            "archive_anomalies": 0,
            "linked_signals": 1,
            "live_linked_signals": 1,
            "independent_evidence_groups": groups,
            "evidence_strength_ordinal": strength,
        }
        priority = editorial_priority(features)
        unusual_points = 0.0
        if row.get("unusual") is True and row.get("unusualness") is not None:
            unusual_points = min(50.0, float(row["unusualness"]) / UNUSUAL_THRESHOLD * 25.0)
        join_score = round(min(100.0, float(priority["score"]) * 0.7 + unusual_points), 1)
        join = {
            "object_id": obj.get("object_id") or obj.get("id") or obj.get("event_id"),
            "object_kind": obj.get("kind"),
            "peer": row.get("peer"),
            "series_id": row.get("series_id"),
            "peer_date": row.get("peer_date"),
            "match": matched,
            "state": row.get("state"),
            "unusual": row.get("unusual"),
            "unusualness": row.get("unusualness"),
            "n_history": row.get("n_history"),
            "public_copy": row.get("public_copy"),
            "editorial_priority": priority,
            "join_score": join_score,
            "join_meaning": review_rank_meaning(),
            "label": None,
            "rights": {"training_use": "derived_only"},
            "relation": "peer-context-not-causation",
        }
        ranked.append(join)
    ranked.sort(key=lambda item: (-float(item["join_score"]), str(item.get("peer_date") or "")))
    return ranked


def attach_peer_context(
    obj: Mapping[str, Any],
    document: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Hook for event_analysis / news pages. Empty list if no peer row matches."""

    if not isinstance(document, Mapping):
        return []
    return rank_joins(
        obj,
        [row for row in (document.get("peer_series") or []) if isinstance(row, dict)],
        cdt_items=[row for row in (document.get("cdt_items") or []) if isinstance(row, dict)],
    )


def collect_palimpsest_objects(readings: Path) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    official = _optional_json(readings / "official-first-seen-latest.json")
    if official:
        objects.append({
            "kind": "official-first-seen",
            "object_id": "official-first-seen",
            "generated_at": official.get("generated_at"),
            "pages": official.get("pages") or [],
        })
    ddti = _optional_json(readings / "ddti-latest.json")
    if ddti:
        for term in ddti.get("ranked") or []:
            if isinstance(term, dict) and term.get("term"):
                objects.append({
                    "kind": "board-term",
                    "object_id": f"board-term:{term['term']}",
                    "term": term["term"],
                    "first_seen": term.get("first_seen"),
                    "last_seen": term.get("last_seen"),
                })
    bleed = _optional_json(readings / "bleedthrough-latest.json")
    if bleed:
        objects.append({
            "kind": "bleedthrough-host",
            "object_id": "bleedthrough",
            "probe_domain": bleed.get("probe_domain"),
            "generated_at": bleed.get("generated_at"),
            "events": bleed.get("events") or [],
        })
    wire = _optional_json(readings / "newswire-latest.json")
    if wire and wire.get("schema_version") == "palimpsest-newswire.v1":
        for event in wire.get("events") or []:
            if isinstance(event, dict) and event.get("event_id"):
                objects.append({
                    "kind": "wire-event",
                    "object_id": event["event_id"],
                    "event_id": event["event_id"],
                    "url": event.get("url"),
                    "topics": event.get("topics") or [],
                    "published_at": event.get("published_at"),
                    "declared_links": event.get("declared_links") or {},
                })
    return objects


def cdt_items_from_ddti(readings: Path) -> dict[str, Any] | None:
    """Bound CDT titles already stored on DDTI samples. No article bodies."""

    ddti = _optional_json(readings / "ddti-latest.json")
    if not ddti:
        return None
    items = []
    seen = set()
    for term in ddti.get("ranked") or []:
        if not isinstance(term, dict):
            continue
        for sample in term.get("samples") or []:
            if not isinstance(sample, dict):
                continue
            url = sample.get("url") or ""
            if "chinadigitaltimes.net" not in url or url in seen:
                continue
            seen.add(url)
            items.append({
                "item_id": url,
                "title": sample.get("title"),
                "excerpt": sample.get("title"),
                "url": url,
                "host": "chinadigitaltimes.net",
                "terms": [term.get("term")] if term.get("term") else [],
                "published_at": term.get("last_seen") or ddti.get("generated_at"),
                "peer_date": term.get("last_seen") or ddti.get("generated_at"),
            })
    if not items:
        return None
    return {
        "schema_version": "palimpsest-cdt-context/v1",
        "source": "DDTI public CDT RSS titles already stored",
        "items": items,
        "current_week_titles": len(items),
    }


def gfw_series(readings: Path) -> tuple[list[float], str | None]:
    path = readings / "ooni-gfw-history.jsonl"
    if not path.is_file():
        return [], None
    values = []
    last_date = None
    for row in _jsonl(path):
        raw = row.get("gfw_index")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            values.append(float(raw))
            last_date = _day(row.get("generated_at") or row.get("window_end"))
    return values, last_date


def build_peer_context(
    readings_dir: Path | str,
    *,
    now: datetime | None = None,
    objects: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(readings_dir)
    greatfire = fit_greatfire(
        _optional_json(root / PEER_FILES["greatfire"]),
        _jsonl(root / PEER_FILES["greatfire_history"]),
    )
    gfw_values, gfw_date = gfw_series(root)
    ooni = fit_ooni(
        _optional_json(root / PEER_FILES["ooni"]),
        _jsonl(root / PEER_FILES["ooni_history"]),
        gfw_history=gfw_values,
        gfw_date=gfw_date,
    )
    cdt_doc = _optional_json(root / PEER_FILES["cdt"]) or cdt_items_from_ddti(root)
    cdt_series, cdt_items = fit_cdt(cdt_doc, _jsonl(root / PEER_FILES["cdt_history"]))
    peer_series = greatfire + ooni + cdt_series
    live_objects = objects if objects is not None else collect_palimpsest_objects(root)
    joins = []
    for obj in live_objects:
        joins.extend(rank_joins(obj, peer_series, cdt_items=cdt_items))

    document = {
        "schema_version": SCHEMA,
        "job": JOB,
        "generated_at": _iso_now(now),
        "source": (
            "Cached GreatFire verdicts, OONI host/ASN or on-disk ooni-gfw history, "
            "and bounded CDT titles. Warehouse files are optional."
        ),
        "method": METHOD,
        "scope": (
            "Per-peer unusualness vs that peer series' own history, plus a "
            "fail-closed join onto Palimpsest objects. No causal attribution. "
            "No Weiboscope dump. No GreatFire catalog crawl."
        ),
        "rights": {"training_use": "derived_only"},
        "n_peer_series": len(peer_series),
        "n_peer_series_scored": sum(1 for row in peer_series if row["state"] == "scored"),
        "n_peer_series_warming_up": sum(1 for row in peer_series if row["state"] == "warming_up"),
        "n_cdt_items": len(cdt_items),
        "n_joins": len(joins),
        "n_objects_considered": len(live_objects),
        "peer_series": peer_series,
        "cdt_items": cdt_items,
        "joins": joins,
        "publication_policy": {
            "automatic_publication": "prohibited",
            "human_review_required": True,
            "causal_language": "prohibited",
            "generative_model": "prohibited",
            "event_analysis_prose": "unchanged",
        },
    }
    copy_blob = " ".join(
        [METHOD, document["scope"], *(str(row.get("public_copy") or "") for row in peer_series)]
    )
    if any(token in copy_blob.casefold() for token in FORBIDDEN_COPY):
        raise ValueError("peer-context document is not context-only")
    document["analysis_sha256"] = hashlib.sha256(_canonical_json(document)).hexdigest()
    return document
