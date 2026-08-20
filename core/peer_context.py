"""Attributed peer-context warehouse for GreatFire, OONI, CDT, and Weiboscope.

Palimpsest does not become the origin of these measurements. Every public
sentence names the peer and the date of their verdict. Collector joins stay
topic- or host-level context; they never collapse a peer denominator into ours.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from collectors.ooni_peer_join import host_of, join_hosts
from collectors.weiboscope import ATTRIBUTION as WEIBOSCOPE_ATTRIBUTION
from collectors.weiboscope import SENTENCE as WEIBOSCOPE_SENTENCE
from collectors.weiboscope import documented_abstention
from core.china_observation import iso_z, public_text


SCHEMA_VERSION = "palimpsest-peer-context.v1"
METHOD_VERSION = 1
RELATION = "peer-context-not-palimpsest-capture"
CDT_EXCERPT_LIMIT = 280
MAX_CDT_PER_EVENT = 3
MAX_PEERS_PER_EVENT = 8
BLEEDTHROUGH_PROBE_DOMAIN = "torproject.org"

PEERS = frozenset({"greatfire", "ooni", "cdt", "weiboscope"})
STATUSES = frozenset({"live", "miss", "silent", "abstain"})
PEER_FIELDS = frozenset(
    {
        "peer",
        "status",
        "sentence",
        "as_of",
        "peer_url",
        "title",
        "excerpt",
        "host",
        "measurement_count",
        "anomaly_rate",
        "verdict",
        "window_days",
        "attribution",
        "relation",
    }
)

GREATFIRE_ATTRIBUTION = (
    "GreatFire Analyzer, CC BY 4.0. Palimpsest is not the origin of these "
    "measurements."
)
OONI_ATTRIBUTION = (
    "OONI Probe / OONI data. Palimpsest is not the origin of these measurements."
)
CDT_ATTRIBUTION = (
    "China Digital Times. Palimpsest did not write that piece."
)

READING_URL_FILES = (
    "official-first-seen-latest.json",
    "public-deletion-ledgers-latest.json",
    "newswire-latest.json",
    "news-wire-live-latest.json",
    "wayback-latest.json",
    "bleedthrough-latest.json",
)
CONFIG_URL_FILES = (
    "config/official_first_seen.json",
    "config/wayback_watchlist.json",
)


def _date_only(stamp: str | None) -> str:
    if not stamp:
        return "an unknown date"
    return stamp[:10]


def greatfire_sentence(verdict: str | None, as_of: str | None, *, status: str) -> str:
    if status == "silent":
        return (
            "GreatFire's open API did not answer; Palimpsest abstains rather "
            "than invent a verdict."
        )
    if status != "live" or not verdict:
        return (
            f"GreatFire has no cached 90-day verdict for this host as of "
            f"{_date_only(as_of)}."
        )
    return (
        f"GreatFire's 90-day verdict for this host is {verdict} as of "
        f"{_date_only(as_of)}."
    )


def ooni_sentence(
    count: int | None,
    *,
    anomaly_rate: float | None = None,
    as_of: str | None = None,
    status: str,
) -> str:
    if status != "live" or not count:
        return (
            "OONI has no China measurements on this host in the local warehouse "
            "or ooni-gfw-latest reading."
        )
    rate_bit = ""
    if isinstance(anomaly_rate, (int, float)):
        rate_bit = f" (anomaly rate {round(float(anomaly_rate) * 100, 1)}%)"
    as_of_bit = f" as of {_date_only(as_of)}" if as_of else ""
    return f"OONI has {count} China measurements on this host{rate_bit}{as_of_bit}."


def cdt_sentence(title: str, url: str) -> str:
    return (
        f'CDT published a related title "{title}" ({url}). '
        "Palimpsest did not write that piece."
    )


def peer_row(
    *,
    peer: str,
    status: str,
    sentence: str,
    as_of: str | None = None,
    peer_url: str | None = None,
    title: str | None = None,
    excerpt: str | None = None,
    host: str | None = None,
    measurement_count: int | None = None,
    anomaly_rate: float | None = None,
    verdict: str | None = None,
    window_days: int | None = None,
    attribution: str,
) -> dict[str, Any]:
    if peer not in PEERS:
        raise ValueError(f"unknown peer: {peer}")
    if status not in STATUSES:
        raise ValueError(f"unknown peer status: {status}")
    excerpt_text = public_text(excerpt, limit=CDT_EXCERPT_LIMIT) or None
    return {
        "peer": peer,
        "status": status,
        "sentence": public_text(sentence, limit=600),
        "as_of": iso_z(as_of) if as_of else None,
        "peer_url": public_text(peer_url, limit=2048) or None,
        "title": public_text(title, limit=240) or None,
        "excerpt": excerpt_text,
        "host": public_text(host, limit=253) or None,
        "measurement_count": measurement_count if type(measurement_count) is int else None,
        "anomaly_rate": anomaly_rate if isinstance(anomaly_rate, (int, float)) else None,
        "verdict": public_text(verdict, limit=64) or None,
        "window_days": window_days if type(window_days) is int else None,
        "attribution": public_text(attribution, limit=400),
        "relation": RELATION,
    }


def weiboscope_row(*, now: datetime | None = None) -> dict[str, Any]:
    raw = documented_abstention(now=now)
    return peer_row(
        peer="weiboscope",
        status="abstain",
        sentence=WEIBOSCOPE_SENTENCE,
        as_of=raw["as_of"],
        peer_url=raw["peer_url"],
        title=raw["title"],
        attribution=WEIBOSCOPE_ATTRIBUTION,
    )


def _walk_urls(node: Any, into: list[str]) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in {
                "url",
                "source_url",
                "canonical_url",
                "article_url",
                "probe_domain",
            } and isinstance(value, str):
                if key == "probe_domain":
                    into.append(f"https://{value}/")
                else:
                    into.append(value)
            else:
                _walk_urls(value, into)
    elif isinstance(node, list):
        for item in node:
            _walk_urls(item, into)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def collect_palimpsest_urls(
    readings: Path | str,
    *,
    root: Path | str | None = None,
) -> list[str]:
    """URLs Palimpsest already has. Never invent a crawl list."""

    readings_dir = Path(readings)
    repo = Path(root) if root is not None else readings_dir.parent
    found: list[str] = []
    for name in READING_URL_FILES:
        payload = _load_json(readings_dir / name)
        if payload is not None:
            _walk_urls(payload, found)
    for rel in CONFIG_URL_FILES:
        payload = _load_json(repo / rel)
        if payload is not None:
            _walk_urls(payload, found)
    found.append(f"https://{BLEEDTHROUGH_PROBE_DOMAIN}/")
    unique: list[str] = []
    seen: set[str] = set()
    for url in found:
        text = public_text(url, limit=2048)
        if not text.startswith("http"):
            continue
        parsed = urlsplit(text)
        if parsed.username or parsed.password:
            continue
        if text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _greatfire_by_host(reading: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(reading, Mapping):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for row in reading.get("verdicts") or []:
        if not isinstance(row, Mapping):
            continue
        host = host_of(str(row.get("query_url") or row.get("path") or ""))
        if not host:
            continue
        index[host] = dict(row)
    return index


def _ooni_by_host(reading: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(reading, Mapping):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for row in reading.get("hosts") or []:
        if not isinstance(row, Mapping):
            continue
        host = host_of(str(row.get("host") or ""))
        if host:
            index[host] = dict(row)
    return index


def bound_cdt_excerpt(text: str | None) -> str:
    return public_text(text, limit=CDT_EXCERPT_LIMIT)


def cdt_items_from_readings(
    readings: Path | str | None = None,
    *,
    ledgers: Mapping[str, Any] | None = None,
    wire: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Titles, links, and bounded excerpts. Never a full CDT article."""

    items: list[dict[str, Any]] = []
    documents: list[Mapping[str, Any]] = []
    if isinstance(ledgers, Mapping):
        documents.append(ledgers)
    if readings is not None:
        payload = _load_json(Path(readings) / "public-deletion-ledgers-latest.json")
        if isinstance(payload, dict):
            documents.append(payload)
    for document in documents:
        for obs in document.get("observations") or []:
            if not isinstance(obs, Mapping):
                continue
            source = str(obs.get("source") or obs.get("ledger_kind") or "")
            url = public_text(obs.get("url") or obs.get("source_url"), limit=2048)
            if "cdt" not in source.casefold() and "chinadigitaltimes.net" not in url:
                continue
            title = public_text(obs.get("title"), limit=240)
            if not title or not url.startswith("https://"):
                continue
            items.append({
                "title": title,
                "url": url,
                "excerpt": bound_cdt_excerpt(obs.get("text") or obs.get("excerpt")),
                "published_at": iso_z(obs.get("first_seen") or obs.get("detected_at")),
                "source": "cdt",
            })
    if wire is None and readings is not None:
        loaded = _load_json(Path(readings) / "newswire-latest.json")
        wire = loaded if isinstance(loaded, dict) else None
    if isinstance(wire, Mapping):
        for item in wire.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            if item.get("source_id") != "china-digital-times":
                continue
            url = public_text(item.get("canonical_url") or item.get("url"), limit=2048)
            title = public_text(item.get("title"), limit=240)
            if not title or not url.startswith("https://"):
                continue
            items.append({
                "title": title,
                "url": url,
                "excerpt": bound_cdt_excerpt(item.get("excerpt")),
                "published_at": iso_z(item.get("published_at")),
                "source": "cdt",
            })
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    return unique


def _event_hosts(event: Mapping[str, Any]) -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()
    for ref in event.get("evidence_refs") or []:
        if not isinstance(ref, Mapping):
            continue
        host = host_of(str(ref.get("url") or ""))
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def _event_haystack(event: Mapping[str, Any], items: Mapping[str, Mapping[str, Any]]) -> str:
    parts = [str(event.get("headline") or ""), str(event.get("dek") or "")]
    parts.extend(str(topic) for topic in event.get("topics") or [])
    for ref in event.get("evidence_refs") or []:
        if not isinstance(ref, Mapping):
            continue
        parts.append(str(ref.get("title") or ""))
        item = items.get(str(ref.get("item_id") or ""))
        if item is not None:
            parts.append(str(item.get("excerpt") or ""))
    return " ".join(parts).casefold()


def match_cdt_items(
    event: Mapping[str, Any],
    cdt_items: Sequence[Mapping[str, Any]],
    *,
    wire_items: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    urls = {
        public_text(ref.get("url"), limit=2048)
        for ref in event.get("evidence_refs") or []
        if isinstance(ref, Mapping)
    }
    hosts = set(_event_hosts(event))
    haystack = _event_haystack(event, wire_items or {})
    matched: list[dict[str, Any]] = []
    for item in cdt_items:
        url = str(item.get("url") or "")
        host = host_of(url)
        title = str(item.get("title") or "")
        url_hit = url in urls
        host_hit = bool(host and host in hosts)
        title_hit = any(
            len(token) >= 4 and token in haystack
            for token in title.casefold().replace(":", " ").split()
        )
        if not url_hit and not host_hit and not title_hit:
            continue
        matched.append(dict(item))
        if len(matched) >= MAX_CDT_PER_EVENT:
            break
    return matched


def rows_for_hosts(
    hosts: Sequence[str],
    *,
    greatfire: Mapping[str, Any] | None = None,
    ooni: Mapping[str, Any] | None = None,
    cdt_items: Sequence[Mapping[str, Any]] | None = None,
    include_weiboscope: bool = True,
    now: datetime | None = None,
    event: Mapping[str, Any] | None = None,
    wire_items: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build closed peer_context rows for one event or one official URL."""

    now = now or datetime.now(timezone.utc)
    stamp = iso_z(now)
    gf_index = _greatfire_by_host(greatfire)
    ooni_index = _ooni_by_host(ooni)
    rows: list[dict[str, Any]] = []
    primary = hosts[0] if hosts else None

    if greatfire is None:
        rows.append(peer_row(
            peer="greatfire",
            status="silent",
            sentence=greatfire_sentence(None, stamp, status="silent"),
            as_of=stamp,
            host=primary,
            attribution=GREATFIRE_ATTRIBUTION,
            window_days=90,
        ))
    elif primary and primary in gf_index and gf_index[primary].get("verdict"):
        hit = gf_index[primary]
        rows.append(peer_row(
            peer="greatfire",
            status="live",
            sentence=greatfire_sentence(hit.get("verdict"), hit.get("as_of") or hit.get("last_tested"), status="live"),
            as_of=hit.get("as_of") or hit.get("last_tested"),
            peer_url=hit.get("source_url"),
            title=hit.get("path"),
            host=primary,
            verdict=hit.get("verdict"),
            window_days=hit.get("window_days") or 90,
            attribution=GREATFIRE_ATTRIBUTION,
        ))
    else:
        rows.append(peer_row(
            peer="greatfire",
            status="miss",
            sentence=greatfire_sentence(None, stamp, status="miss"),
            as_of=stamp,
            host=primary,
            window_days=90,
            attribution=GREATFIRE_ATTRIBUTION,
        ))

    if ooni is None:
        rows.append(peer_row(
            peer="ooni",
            status="miss",
            sentence=ooni_sentence(None, status="miss"),
            as_of=stamp,
            host=primary,
            attribution=OONI_ATTRIBUTION,
        ))
    elif primary and primary in ooni_index and ooni_index[primary].get("status") == "live":
        hit = ooni_index[primary]
        rows.append(peer_row(
            peer="ooni",
            status="live",
            sentence=ooni_sentence(
                hit.get("measurement_count") or hit.get("completed_measurement_count"),
                anomaly_rate=hit.get("anomaly_rate"),
                as_of=hit.get("last_measurement"),
                status="live",
            ),
            as_of=hit.get("last_measurement"),
            host=primary,
            measurement_count=hit.get("measurement_count") or hit.get("completed_measurement_count"),
            anomaly_rate=hit.get("anomaly_rate"),
            attribution=OONI_ATTRIBUTION,
        ))
    else:
        rows.append(peer_row(
            peer="ooni",
            status="miss",
            sentence=ooni_sentence(None, status="miss"),
            as_of=stamp,
            host=primary,
            attribution=OONI_ATTRIBUTION,
        ))

    if event is not None and cdt_items:
        for item in match_cdt_items(event, cdt_items, wire_items=wire_items):
            rows.append(peer_row(
                peer="cdt",
                status="live",
                sentence=cdt_sentence(item["title"], item["url"]),
                as_of=item.get("published_at") or stamp,
                peer_url=item["url"],
                title=item["title"],
                excerpt=item.get("excerpt"),
                host=host_of(item["url"]),
                attribution=CDT_ATTRIBUTION,
            ))

    if include_weiboscope:
        rows.append(weiboscope_row(now=now))
    return rows[:MAX_PEERS_PER_EVENT]


def peer_context_for_event(
    event: Mapping[str, Any],
    peer: Mapping[str, Any] | None,
    *,
    wire: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Project one event's attributed peer sentences. Empty when no peer document."""

    if peer is None:
        return []
    items = {
        item["item_id"]: item
        for item in (wire or {}).get("items", [])
        if isinstance(item, Mapping) and item.get("item_id")
    }
    return rows_for_hosts(
        _event_hosts(event),
        greatfire=peer.get("greatfire") if isinstance(peer.get("greatfire"), Mapping) else None,
        ooni=peer.get("ooni") if isinstance(peer.get("ooni"), Mapping) else None,
        cdt_items=peer.get("cdt_items") if isinstance(peer.get("cdt_items"), list) else [],
        include_weiboscope=True,
        now=datetime.now(timezone.utc),
        event=event,
        wire_items=items,
    )


def build_peer_document(
    *,
    urls: Iterable[str],
    greatfire: Mapping[str, Any] | None,
    ooni_hosts: Iterable[str] | None = None,
    cdt_items: Sequence[Mapping[str, Any]] | None = None,
    weiboscope: Mapping[str, Any] | None = None,
    gfw_path: Path | str | None = None,
    warehouse: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Offline warehouse reading. Live GreatFire lookups happen in the GF pull."""

    now = now or datetime.now(timezone.utc)
    hosts = []
    seen: set[str] = set()
    for url in urls:
        host = host_of(url)
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    if ooni_hosts:
        for raw in ooni_hosts:
            host = host_of(raw)
            if host and host not in seen:
                seen.add(host)
                hosts.append(host)
    ooni = join_hosts(hosts, gfw_path=gfw_path, warehouse=warehouse, now=now)
    weibo = weiboscope if isinstance(weiboscope, Mapping) else {
        "abstention": documented_abstention(now=now),
        "dump_on_node": False,
        "doi": documented_abstention()["doi"],
    }
    cdt = list(cdt_items or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_z(now),
        "method_version": METHOD_VERSION,
        "source": (
            "Attributed context warehouse: GreatFire Analyzer (CC BY 4.0), "
            "OONI, China Digital Times RSS, Weiboscope citation"
        ),
        "scope": (
            "Peer verdicts for URLs Palimpsest already holds. Palimpsest is not "
            "the origin of these measurements. Weiboscope's 2012 dump is not "
            "on this node."
        ),
        "method": (
            "GreatFire lookups are cached by the greatfire-context job. OONI is "
            "an exact host join against already-held ooni-gfw-latest / ooni-bulk "
            "warehouse objects. CDT keeps titles, links, and excerpts bounded at "
            f"{CDT_EXCERPT_LIMIT} characters. Weiboscope is a documented abstention."
        ),
        "n_hosts": len(hosts),
        "n_greatfire": (
            int(greatfire.get("n_verdicts") or 0) if isinstance(greatfire, Mapping) else 0
        ),
        "n_ooni": ooni["n_hits"],
        "n_cdt": len(cdt),
        "greatfire": greatfire,
        "ooni": ooni,
        "cdt_items": cdt,
        "weiboscope": weibo,
        "disk_estimate": disk_estimate(greatfire=greatfire, ooni=ooni, cdt_items=cdt),
    }


def disk_estimate(
    *,
    greatfire: Mapping[str, Any] | None,
    ooni: Mapping[str, Any] | None,
    cdt_items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Honest bytes: peer cache is small; the 33G OONI warehouse is already on box."""

    gf_rows = len((greatfire or {}).get("verdicts") or []) if isinstance(greatfire, Mapping) else 0
    ooni_rows = len((ooni or {}).get("hosts") or []) if isinstance(ooni, Mapping) else 0
    return {
        "greatfire_context_json": "~32-256 KiB cached verdicts (not the 700k catalog)",
        "ooni_peer_index": f"~{max(ooni_rows, 1)} host rows; warehouse remains ~33G already on node",
        "cdt_excerpts": f"~{len(cdt_items)} titles/links/{CDT_EXCERPT_LIMIT}-char excerpts",
        "weiboscope": "0 bytes of the 2012 dump; citation pointer only",
        "n_greatfire_rows": gf_rows,
        "n_ooni_rows": ooni_rows,
        "n_cdt_rows": len(cdt_items),
    }


def load_peer_document(path: Path | str) -> dict[str, Any] | None:
    payload = _load_json(Path(path))
    return payload if isinstance(payload, dict) else None


__all__ = [
    "CDT_EXCERPT_LIMIT",
    "METHOD_VERSION",
    "PEER_FIELDS",
    "RELATION",
    "SCHEMA_VERSION",
    "bound_cdt_excerpt",
    "build_peer_document",
    "cdt_items_from_readings",
    "cdt_sentence",
    "collect_palimpsest_urls",
    "disk_estimate",
    "greatfire_sentence",
    "load_peer_document",
    "ooni_sentence",
    "peer_context_for_event",
    "peer_row",
    "rows_for_hosts",
    "weiboscope_row",
]
