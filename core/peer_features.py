"""Stable ML feature rows for the reading-analysis / peer-context ranker.

This is not an LLM rewrite. Event analysis still writes the canned sentence.
The warehouse emits one closed feature row per peer observation so
``processors/peer_context.py`` (PR 86) can fit unusualness and join rank
against already-held Palimpsest objects.

Every row credits the peer. Silent peers fail closed: no hollow latest file.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from collectors.ooni_peer_join import host_of
from collectors.weiboscope import ATTRIBUTION as WEIBOSCOPE_ATTRIBUTION
from collectors.weiboscope import CITATION as WEIBOSCOPE_CITATION
from collectors.weiboscope import DOI as WEIBOSCOPE_DOI
from collectors.weiboscope import documented_abstention
from core.china_observation import gazetteer_hits, iso_z, public_text
from core.peer_context import (
    CDT_ATTRIBUTION,
    CDT_EXCERPT_LIMIT,
    GREATFIRE_ATTRIBUTION,
    OONI_ATTRIBUTION,
    RELATION,
    bound_cdt_excerpt,
)
from processors.editorial_priority import editorial_priority


FEATURE_SCHEMA = "palimpsest-peer-context-features/v1"
GF_SCHEMA = "palimpsest-greatfire-context/v1"
OONI_SCHEMA = "palimpsest-ooni-peer-context/v1"
CDT_SCHEMA = "palimpsest-cdt-context/v1"
WEIBOSCOPE_SCHEMA = "palimpsest-weiboscope-context/v1"
METHOD_VERSION = 1

FEATURE_FIELDS = frozenset(
    {
        "schema_version",
        "peer",
        "credit",
        "relation",
        "status",
        "host",
        "path",
        "verdict",
        "window_start",
        "window_end",
        "block_share_90d",
        "n_tests",
        "last_tested_at",
        "asn",
        "n_measurements",
        "anomaly_rate",
        "last_measured_at",
        "title",
        "url",
        "published_at",
        "excerpt_len_bounded",
        "extracted_terms",
        "doi",
        "review",
    }
)

_EMPTY = {
    "host": None,
    "path": None,
    "verdict": None,
    "window_start": None,
    "window_end": None,
    "block_share_90d": None,
    "n_tests": None,
    "last_tested_at": None,
    "asn": None,
    "n_measurements": None,
    "anomaly_rate": None,
    "last_measured_at": None,
    "title": None,
    "url": None,
    "published_at": None,
    "excerpt_len_bounded": None,
    "extracted_terms": [],
    "doi": None,
}


def _feature_row(**fields: Any) -> dict[str, Any]:
    row = {
        "schema_version": FEATURE_SCHEMA,
        **_EMPTY,
        **fields,
        "relation": RELATION,
    }
    extra = set(row) - FEATURE_FIELDS
    if extra:
        raise ValueError(f"peer feature row has unknown fields: {sorted(extra)}")
    missing = FEATURE_FIELDS - set(row)
    if missing:
        raise ValueError(f"peer feature row missing fields: {sorted(missing)}")
    if not public_text(row.get("credit"), limit=400):
        raise ValueError("peer feature row must credit the peer")
    return {key: row[key] for key in sorted(FEATURE_FIELDS)}


def _review(*, groups: int = 1, strength: int = 1, live: int = 1) -> dict[str, Any]:
    """Existing editorial-priority ranker. Review order only — not a rewrite."""

    return editorial_priority(
        {
            "archive_targets": 0,
            "archive_anomaly_max": None,
            "archive_anomalies": 0,
            "linked_signals": live,
            "live_linked_signals": live,
            "independent_evidence_groups": groups,
            "evidence_strength_ordinal": strength,
        }
    )


def _share(percent: Any) -> float | None:
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    value = float(percent)
    if value != value or value in {float("inf"), float("-inf")}:
        return None
    if value > 1.0:
        value = value / 100.0
    if value < 0.0:
        return None
    return round(min(value, 1.0), 4)


def _window(
    *,
    as_of: str | None,
    last_tested: str | None,
    window_days: int,
) -> tuple[str | None, str | None]:
    end = iso_z(last_tested) or iso_z(as_of)
    if not end:
        return None, None
    try:
        moment = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None, end
    start = (moment - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


def _terms(*texts: str) -> list[str]:
    hits = gazetteer_hits(*texts, limit=12)
    out: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        token = public_text(hit.get("zh") or hit.get("en"), limit=80)
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _iso_week_start(stamp: str | None) -> str | None:
    parsed = iso_z(stamp)
    if not parsed:
        return None
    try:
        moment = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
    except ValueError:
        return None
    monday = moment.date() - timedelta(days=moment.weekday())
    return f"{monday.isoformat()}T00:00:00Z"


def greatfire_is_live(document: Mapping[str, Any] | None) -> bool:
    if not isinstance(document, Mapping):
        return False
    verdicts = document.get("verdicts") or document.get("hosts") or []
    if not isinstance(verdicts, list):
        return False
    return any(
        isinstance(row, Mapping)
        and (row.get("found") or row.get("verdict") or row.get("block_share_90d") is not None)
        for row in verdicts
    )


def ooni_is_live(document: Mapping[str, Any] | None) -> bool:
    if not isinstance(document, Mapping):
        return False
    if int(document.get("n_hits") or 0) > 0:
        return True
    return any(
        isinstance(row, Mapping) and row.get("status") == "live"
        for row in document.get("hosts") or []
    )


def cdt_is_live(items: Sequence[Mapping[str, Any]] | None) -> bool:
    return bool(items)


def greatfire_hosts(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Ranker-shaped GreatFire host rows. Compact 90-day stats only."""

    raw_rows = document.get("verdicts") or document.get("hosts") or []
    hosts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            continue
        if not raw.get("found") and not raw.get("verdict"):
            continue
        host = host_of(str(raw.get("query_url") or raw.get("path") or raw.get("host") or ""))
        path = public_text(raw.get("path"), limit=1024) or None
        if not host or host in seen:
            continue
        seen.add(host)
        window_days = raw.get("window_days") if type(raw.get("window_days")) is int else 90
        window_start, window_end = _window(
            as_of=raw.get("as_of") or raw.get("peer_date") or raw.get("observed_at"),
            last_tested=raw.get("last_tested") or raw.get("last_tested_at"),
            window_days=window_days,
        )
        share = _share(raw.get("block_share_90d") if raw.get("block_share_90d") is not None else raw.get("blocked_percent") or raw.get("block_share"))
        n_tests = raw.get("n_tests") if type(raw.get("n_tests")) is int else raw.get("conclusions")
        if type(n_tests) is not int:
            n_tests = None
        last_tested = iso_z(raw.get("last_tested") or raw.get("last_tested_at") or raw.get("as_of"))
        hosts.append(
            {
                "host": host,
                "path": path,
                "verdict": public_text(raw.get("verdict"), limit=64) or None,
                "window_days": window_days,
                "window_start": window_start,
                "window_end": window_end,
                "block_share": share,
                "block_share_90d": share,
                "blocked": share is not None and share >= 0.5,
                "tested": n_tests,
                "n_tests": n_tests,
                "peer_date": (last_tested or window_end or "")[:10] or None,
                "observed_at": last_tested,
                "last_tested_at": last_tested,
                "source": "cached-verdicts-only",
                "attribution": GREATFIRE_ATTRIBUTION,
            }
        )
    return hosts


def greatfire_document(source: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    if not greatfire_is_live(source):
        return None
    hosts = greatfire_hosts(source)
    if not hosts:
        return None
    stamp = iso_z(source.get("generated_at") or now or datetime.now(timezone.utc))
    return {
        "schema_version": GF_SCHEMA,
        "generated_at": stamp,
        "method_version": METHOD_VERSION,
        "source": source.get("source") or "GreatFire Analyzer open API, CC BY 4.0",
        "method": (
            "Keyless /api/url/ and /api/verdict lookups for already-held Palimpsest "
            "URLs. Per-test history discarded. Not a 700k catalog crawl."
        ),
        "scope": "90-day host verdicts Palimpsest already holds.",
        "attribution": GREATFIRE_ATTRIBUTION,
        "license": "CC BY 4.0",
        "n_urls_queried": source.get("n_urls_queried"),
        "n_verdicts": len(hosts),
        "window_days": 90,
        "hosts": hosts,
        "verdicts": source.get("verdicts") or [],
        "ledgers": source.get("ledgers") or [],
    }


def greatfire_feature_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for host in greatfire_hosts(document):
        rows.append(
            _feature_row(
                peer="greatfire",
                credit=GREATFIRE_ATTRIBUTION,
                status="live",
                host=host["host"],
                path=host["path"],
                verdict=host["verdict"],
                window_start=host["window_start"],
                window_end=host["window_end"],
                block_share_90d=host["block_share_90d"],
                n_tests=host["n_tests"],
                last_tested_at=host["last_tested_at"],
                url=f"https://en.greatfire.org/{host['path']}" if host.get("path") else None,
                review=_review(strength=2 if host.get("blocked") else 1),
            )
        )
    return rows


def normalize_asn(value: Any) -> str | None:
    if type(value) is int and 0 < value <= 4_294_967_295:
        return f"AS{value}"
    text = public_text(value, limit=32).upper()
    if not text:
        return None
    if text.startswith("AS") and text[2:].isdigit():
        return f"AS{int(text[2:])}"
    if text.isdigit():
        return f"AS{int(text)}"
    return None


def ooni_series(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in document.get("hosts") or document.get("series") or []:
        if not isinstance(raw, Mapping) or raw.get("status") == "miss":
            continue
        host = host_of(str(raw.get("host") or raw.get("key") or ""))
        if raw.get("kind") == "asn":
            host = None
        asn = normalize_asn(raw.get("asn") or (raw.get("key") if raw.get("kind") == "asn" else None))
        rate = raw.get("anomaly_rate")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            continue
        count = raw.get("n_measurements") or raw.get("measurement_count")
        if type(count) is not int:
            count = None
        measured = iso_z(raw.get("last_measured_at") or raw.get("last_measurement") or raw.get("peer_date"))
        if host and ("host", host) not in seen:
            seen.add(("host", host))
            series.append(
                {
                    "kind": "host",
                    "key": host,
                    "host": host,
                    "asn": asn,
                    "n_measurements": count,
                    "anomaly_rate": float(rate),
                    "last_measured_at": measured,
                    "peer_date": (measured or "")[:10] or None,
                    "observed_at": measured,
                    "attribution": OONI_ATTRIBUTION,
                }
            )
        if asn and ("asn", asn) not in seen:
            seen.add(("asn", asn))
            series.append(
                {
                    "kind": "asn",
                    "key": asn,
                    "host": host,
                    "asn": asn,
                    "n_measurements": count,
                    "anomaly_rate": float(rate),
                    "last_measured_at": measured,
                    "peer_date": (measured or "")[:10] or None,
                    "observed_at": measured,
                    "attribution": OONI_ATTRIBUTION,
                }
            )
    return series


def ooni_document(source: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    if not ooni_is_live(source):
        return None
    series = ooni_series(source)
    if not series:
        return None
    stamp = iso_z(source.get("generated_at") or now or datetime.now(timezone.utc))
    return {
        "schema_version": OONI_SCHEMA,
        "generated_at": stamp,
        "method_version": METHOD_VERSION,
        "source": source.get("source") or "OONI bulk warehouse and/or ooni-gfw-latest",
        "method": (
            "Exact host join against already-held ooni-gfw-latest / ooni-bulk. "
            "Does not re-download the 33G archive."
        ),
        "scope": "Host/ASN counts for URLs Palimpsest already holds.",
        "attribution": OONI_ATTRIBUTION,
        "n_hosts": source.get("n_hosts"),
        "n_hits": len([row for row in series if row["kind"] == "host"]),
        "series": series,
        "hosts": [row for row in (source.get("hosts") or []) if isinstance(row, Mapping)],
    }


def ooni_feature_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for series in ooni_series(document):
        if series["kind"] != "host":
            continue
        rows.append(
            _feature_row(
                peer="ooni",
                credit=OONI_ATTRIBUTION,
                status="live",
                host=series["host"],
                asn=series["asn"],
                n_measurements=series["n_measurements"],
                anomaly_rate=series["anomaly_rate"],
                last_measured_at=series["last_measured_at"],
                review=_review(strength=2 if (series["anomaly_rate"] or 0) >= 0.5 else 1),
            )
        )
    return rows


def cdt_items(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        url = public_text(raw.get("url") or raw.get("item_id"), limit=2048)
        title = public_text(raw.get("title"), limit=240)
        if not url.startswith("https://") or not title or url in seen:
            continue
        seen.add(url)
        excerpt = bound_cdt_excerpt(raw.get("excerpt") or raw.get("text") or title)
        terms = list(raw.get("extracted_terms") or raw.get("terms") or [])
        if not terms:
            terms = _terms(title, excerpt)
        published = iso_z(raw.get("published_at") or raw.get("peer_date"))
        out.append(
            {
                "item_id": url,
                "title": title,
                "url": url,
                "excerpt": excerpt,
                "excerpt_len_bounded": min(len(excerpt), CDT_EXCERPT_LIMIT),
                "extracted_terms": terms,
                "terms": terms,
                "host": host_of(url),
                "published_at": published,
                "peer_date": (published or "")[:10] or None,
                "day": (published or "")[:10] or None,
                "peer": "CDT",
                "attribution": CDT_ATTRIBUTION,
            }
        )
    return out


def cdt_weeks(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        week = _iso_week_start(item.get("published_at") or item.get("peer_date"))
        if not week:
            continue
        counts[week] = counts.get(week, 0) + 1
    return [
        {"week_start": week, "n_titles": counts[week]}
        for week in sorted(counts)
    ]


def cdt_document(
    items: Sequence[Mapping[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    compact = cdt_items(items or [])
    if not compact:
        return None
    weeks = cdt_weeks(compact)
    stamp = iso_z(now or datetime.now(timezone.utc))
    return {
        "schema_version": CDT_SCHEMA,
        "generated_at": stamp,
        "method_version": METHOD_VERSION,
        "source": "China Digital Times public RSS (titles, links, bounded excerpts)",
        "method": (
            f"Titles, links, and excerpts bounded at {CDT_EXCERPT_LIMIT} characters. "
            "Full CDT articles are not republished. Terms are gazetteer hits."
        ),
        "scope": "Peer editorial context. Palimpsest did not write these pieces.",
        "attribution": CDT_ATTRIBUTION,
        "n_items": len(compact),
        "current_week_titles": weeks[-1]["n_titles"] if weeks else len(compact),
        "weeks": weeks,
        "items": compact,
    }


def cdt_feature_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in cdt_items(document.get("items") or []):
        rows.append(
            _feature_row(
                peer="cdt",
                credit=CDT_ATTRIBUTION,
                status="live",
                host=item["host"],
                title=item["title"],
                url=item["url"],
                published_at=item["published_at"],
                excerpt_len_bounded=item["excerpt_len_bounded"],
                extracted_terms=item["extracted_terms"],
                review=_review(strength=1, groups=1),
            )
        )
    return rows


def weiboscope_document(*, now: datetime | None = None) -> dict[str, Any]:
    abstention = documented_abstention(now=now)
    stamp = iso_z(now or datetime.now(timezone.utc))
    return {
        "schema_version": WEIBOSCOPE_SCHEMA,
        "generated_at": stamp,
        "method_version": METHOD_VERSION,
        "source": "Weiboscope / HKU JMSC citation only",
        "method": "Do not download doi:10.25442/hku.16674565. Record the abstention.",
        "scope": "Historical 2012 volume is not on this node.",
        "attribution": WEIBOSCOPE_ATTRIBUTION,
        "status": "abstain",
        "dump_on_node": False,
        "doi": WEIBOSCOPE_DOI,
        "citation": WEIBOSCOPE_CITATION,
        "n_messages_upstream": 226_841_122,
        "abstention": abstention,
    }


def weiboscope_feature_rows(document: Mapping[str, Any] | None = None, *, now: datetime | None = None) -> list[dict[str, Any]]:
    payload = document if isinstance(document, Mapping) else weiboscope_document(now=now)
    return [
        _feature_row(
            peer="weiboscope",
            credit=WEIBOSCOPE_ATTRIBUTION,
            status="abstain",
            doi=payload.get("doi") or WEIBOSCOPE_DOI,
            url="https://doi.org/10.25442/hku.16674565",
            title="Weiboscope Open Data (2012)",
            review=_review(live=0, groups=0, strength=0),
        )
    ]


def build_feature_table(
    *,
    greatfire: Mapping[str, Any] | None = None,
    ooni: Mapping[str, Any] | None = None,
    cdt_items_or_doc: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Closed feature table the ranker can append. Silent peers contribute nothing."""

    rows: list[dict[str, Any]] = []
    gf_doc = greatfire_document(greatfire, now=now) if greatfire is not None else None
    ooni_doc = ooni_document(ooni, now=now) if ooni is not None else None
    if isinstance(cdt_items_or_doc, Mapping) and cdt_items_or_doc.get("items") is not None:
        cdt_doc = cdt_document(cdt_items_or_doc.get("items"), now=now)
    else:
        cdt_doc = cdt_document(cdt_items_or_doc, now=now)  # type: ignore[arg-type]
    weibo_doc = weiboscope_document(now=now)
    if gf_doc:
        rows.extend(greatfire_feature_rows(gf_doc))
    if ooni_doc:
        rows.extend(ooni_feature_rows(ooni_doc))
    if cdt_doc:
        rows.extend(cdt_feature_rows(cdt_doc))
    rows.extend(weiboscope_feature_rows(weibo_doc, now=now))
    stamp = iso_z(now or datetime.now(timezone.utc))
    return {
        "schema_version": FEATURE_SCHEMA,
        "generated_at": stamp,
        "method_version": METHOD_VERSION,
        "source": "Attributed peer-context feature table for the review ranker",
        "method": (
            "One closed row per GreatFire / OONI / CDT / Weiboscope observation. "
            "Review score is processors.editorial_priority. No LLM rewrite. "
            "Unusualness fit belongs to the reading-analysis ranker."
        ),
        "scope": "Features only. Palimpsest is not the origin of peer measurements.",
        "n_rows": len(rows),
        "n_greatfire": sum(1 for row in rows if row["peer"] == "greatfire"),
        "n_ooni": sum(1 for row in rows if row["peer"] == "ooni"),
        "n_cdt": sum(1 for row in rows if row["peer"] == "cdt"),
        "n_weiboscope": sum(1 for row in rows if row["peer"] == "weiboscope"),
        "rows": rows,
        "documents": {
            "greatfire": gf_doc,
            "ooni": ooni_doc,
            "cdt": cdt_doc,
            "weiboscope": weibo_doc,
        },
    }


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_feature_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_history(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


__all__ = [
    "CDT_SCHEMA",
    "FEATURE_FIELDS",
    "FEATURE_SCHEMA",
    "GF_SCHEMA",
    "OONI_SCHEMA",
    "WEIBOSCOPE_SCHEMA",
    "build_feature_table",
    "cdt_document",
    "cdt_is_live",
    "greatfire_document",
    "greatfire_is_live",
    "normalize_asn",
    "ooni_document",
    "ooni_is_live",
    "weiboscope_document",
    "write_feature_jsonl",
    "write_json",
    "append_history",
]
