"""Journalist erasure trail — flatten public observations into citeable rows.

Offline fusion of committed readings. The clock is the newest input
``generated_at``, never wall-clock. Public deletion ledgers are included only
when a committed file exists; the builder does not invent a live ledger round.

A journalist who is not the operator should be able to find a public deletion
or reconstruction, see first-seen / last-seen / snapshots / hashes / source
URLs, export the row, and cite it. The desk states what is captured (public
posts, deletions, archives, GFW injector telemetry from separate instruments)
and what is not (private WeChat, classified systems, in-country accounts).

Usage:
    PYTHONPATH=. python3 -m scripts.build_erasure_trail
    PYTHONPATH=. python3 -m scripts.build_erasure_trail --check
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from core import censorship_practice_dossiers as practice_dossiers
from core.china_observation import content_sha256, iso_z, public_text
from scripts import site_nav


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
PAGE_DIR = ROOT / "news" / "china" / "erasure"
JSON_OUT = READINGS / "erasure-trail-latest.json"
DOSSIER_JSON_OUT = READINGS / "censorship-practice-dossiers-latest.json"
CSV_OUT = READINGS / "erasure-trail.csv"
HIST = READINGS / "erasure-trail-history.jsonl"
HTML_OUT = PAGE_DIR / "index.html"
SITE = "https://palimpsest.info"
METHOD_VERSION = 1
SCHEMA_VERSION = "palimpsest-erasure-trail.v1"

INPUT_FILES = (
    "undertext-latest.json",
    "wayback-latest.json",
    "weibo-hotsearch-latest.json",
    "ddti-latest.json",
    "gdelt-latest.json",
    "ooni-gfw-latest.json",
    "bleedthrough-latest.json",
    "public-deletion-ledgers-latest.json",
)
DOSSIER_INPUT_FILES = tuple(
    name for name in practice_dossiers.INPUT_FILES if name.endswith(".json")
)
SOCIAL_VERSIONS_FILE = "social-observations-versions.jsonl"

ROW_FIELDS = (
    "key",
    "title",
    "excerpt",
    "text",
    "text_zh",
    "text_en",
    "language",
    "uncertainty",
    "terms",
    "source",
    "source_url",
    "mirror_urls",
    "first_seen",
    "last_seen",
    "last_confirmed_alive",
    "deletion_signal",
    "deletion_confirmations",
    "content_sha256",
    "wayback_lookup",
    "wayback_snapshot",
    "wayback_raw",
    "archive_today_lookup",
    "ghostarchive_lookup",
    "post_event_snapshot",
    "bracket_before",
    "bracket_after",
    "cross_links_cdt",
    "cross_links_gdelt",
    "cross_links_ooni",
    "cross_links_greatfire",
    "cross_links_weibo",
    "cross_links_undertext",
    "cross_links_bleedthrough",
    "cross_links_common_crawl",
    "common_crawl_match_kind",
    "common_crawl_host",
    "common_crawl_capture_at",
    "common_crawl_mime_type",
    "common_crawl_languages",
    "common_crawl_content_digest",
    "common_crawl_locator_sha256",
    "gazetteer",
    "collector",
    "cite",
)

HONESTY_CAPTURES = (
    "Public posts that were already published, public deletion and blocking "
    "ledgers, Wayback reconstructions of those public URLs, GFW injector "
    "telemetry from separate instruments (OONI, Censored Planet, Inside View, "
    "Bleedthrough), and sanitized Common Crawl lake joins (capture time, MIME, "
    "language, digest) when a matching URL, host, or digest already exists on "
    "the node. The lake is not a live scrape and is not copied here."
)
HONESTY_DOES_NOT = (
    "Private WeChat, classified systems, in-country accounts, follower graphs, "
    "comments, locations, media binaries, or consumer profiles."
)
HONESTY_LIVE = (
    "This desk never fabricates a live reading. A missing ledger or silent "
    "collector is a coverage gap, not proof of calm and not a zero."
)

_TOP_FIELDS = frozenset({
    "schema_version",
    "generated_at",
    "source",
    "method",
    "scope",
    "safety",
    "honesty",
    "n_rows",
    "inputs",
    "rows",
})
_ROW_FIELDS = frozenset(ROW_FIELDS)


def _load_json(name: str, readings_dir: Path) -> dict[str, Any] | None:
    path = readings_dir / name
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_jsonl(name: str, readings_dir: Path) -> tuple[dict[str, Any], ...]:
    """Load a complete retained JSONL input or abstain from that input."""

    path = readings_dir / name
    if not path.is_file():
        return ()
    rows: list[dict[str, Any]] = []
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            row = json.loads(raw_line)
            if not isinstance(row, dict):
                return ()
            rows.append(row)
    except (OSError, UnicodeError, ValueError):
        return ()
    return tuple(rows)


def _wayback_timestamp(value: Any) -> str | None:
    """Accept a CDX 14-digit stamp or an ISO timestamp. Never invent one."""

    if isinstance(value, str) and len(value) == 14 and value.isdigit():
        return (
            f"{value[0:4]}-{value[4:6]}-{value[6:8]}T"
            f"{value[8:10]}:{value[10:12]}:{value[12:14]}Z"
        )
    return iso_z(value)


def _parse_generated_at(value: Any) -> datetime | None:
    stamp = iso_z(value)
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def fusion_clock(payloads: Mapping[str, Mapping[str, Any] | None]) -> datetime | None:
    """Newest committed input timestamp. Never invents a later clock."""

    newest: datetime | None = None
    for payload in payloads.values():
        if not payload:
            continue
        parsed = _parse_generated_at(payload.get("generated_at"))
        if parsed is None:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def _https(url: Any) -> str:
    text = public_text(url, limit=2048)
    return text if text.startswith("https://") else ""


def _sha(value: Any) -> str:
    if isinstance(value, str) and len(value) == 64 and all(
        ch in "0123456789abcdef" for ch in value
    ):
        return value
    return ""


def _archive_url(archive: Any, key: str) -> str:
    if not isinstance(archive, dict):
        return ""
    return _https(archive.get(key))


def _gazetteer_label(hits: Any) -> str:
    labels: list[str] = []
    if not isinstance(hits, list):
        return ""
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        zh = public_text(hit.get("zh"), limit=80)
        en = public_text(hit.get("en"), limit=160)
        label = " — ".join(part for part in (zh, en) if part)
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= 6:
            break
    return "; ".join(labels)


def _cite(row: Mapping[str, str]) -> str:
    url = row["source_url"] or f"{SITE}/news/china/erasure/#{row['key']}"
    first = row["first_seen"] or "unknown"
    last = row["last_seen"] or "unknown"
    digest = row["content_sha256"] or "none"
    return (
        f"Palimpsest erasure trail ({row['key']}), “{row['title']}”, "
        f"first seen {first}, last seen {last}, SHA-256 {digest}. "
        f"Public source: {url}. "
        f"Desk: {SITE}/news/china/erasure/#{row['key']}"
    )


def _flatten_link(record: Any) -> str:
    if not isinstance(record, dict) or not record:
        return ""
    parts = [
        public_text(record.get("id"), limit=80),
        public_text(record.get("url"), limit=2048),
        public_text(record.get("note"), limit=240),
    ]
    return " | ".join(part for part in parts if part)


def _flatten_confirmations(trail: Any) -> str:
    parts: list[str] = []
    if not isinstance(trail, list):
        return ""
    for item in trail[:12]:
        if not isinstance(item, dict):
            continue
        status = public_text(item.get("status"), limit=80)
        when = iso_z(item.get("observed_at")) or ""
        source = public_text(item.get("source"), limit=80)
        note = public_text(item.get("note"), limit=160)
        chunk = " ".join(part for part in (status, when, source) if part)
        if note:
            chunk = f"{chunk} — {note}" if chunk else note
        if chunk and chunk not in parts:
            parts.append(chunk)
    return "; ".join(parts)


def _row(
    *,
    title: str,
    excerpt: str,
    text: str = "",
    text_zh: str = "",
    text_en: str = "",
    language: str = "",
    uncertainty: str = "",
    terms: str = "",
    source: str,
    source_url: str,
    mirror_urls: str = "",
    first_seen: str | None,
    last_seen: str | None,
    last_confirmed_alive: str | None,
    deletion_signal: str,
    deletion_confirmations: str = "",
    content_sha256_value: str,
    wayback_lookup: str,
    wayback_snapshot: str,
    wayback_raw: str = "",
    archive_today_lookup: str,
    ghostarchive_lookup: str = "",
    post_event_snapshot: str = "",
    bracket_before: str = "",
    bracket_after: str = "",
    cross_links_cdt: str = "",
    cross_links_gdelt: str = "",
    cross_links_ooni: str = "",
    cross_links_greatfire: str = "",
    cross_links_weibo: str = "",
    cross_links_undertext: str = "",
    cross_links_bleedthrough: str = "",
    cross_links_common_crawl: str = "",
    common_crawl_match_kind: str = "",
    common_crawl_host: str = "",
    common_crawl_capture_at: str = "",
    common_crawl_mime_type: str = "",
    common_crawl_languages: str = "",
    common_crawl_content_digest: str = "",
    common_crawl_locator_sha256: str = "",
    gazetteer: str,
    collector: str,
) -> dict[str, str]:
    key = content_sha256(collector, source, source_url, title)[:32]
    body = public_text(text or excerpt, limit=8000)
    row = {
        "key": key,
        "title": public_text(title, limit=240) or "(untitled public record)",
        "excerpt": public_text(excerpt or body, limit=400),
        "text": body,
        "text_zh": public_text(text_zh, limit=8000),
        "text_en": public_text(text_en, limit=8000),
        "language": public_text(language, limit=16),
        "uncertainty": public_text(uncertainty, limit=2000),
        "terms": public_text(terms, limit=800),
        "source": public_text(source, limit=80) or "unknown",
        "source_url": source_url,
        "mirror_urls": public_text(mirror_urls, limit=4000),
        "first_seen": iso_z(first_seen) or "",
        "last_seen": iso_z(last_seen) or "",
        "last_confirmed_alive": iso_z(last_confirmed_alive) or "",
        "deletion_signal": public_text(deletion_signal, limit=80),
        "deletion_confirmations": public_text(deletion_confirmations, limit=2000),
        "content_sha256": content_sha256_value,
        "wayback_lookup": wayback_lookup,
        "wayback_snapshot": wayback_snapshot,
        "wayback_raw": wayback_raw,
        "archive_today_lookup": archive_today_lookup,
        "ghostarchive_lookup": ghostarchive_lookup,
        "post_event_snapshot": post_event_snapshot,
        "bracket_before": public_text(bracket_before, limit=80),
        "bracket_after": public_text(bracket_after, limit=80),
        "cross_links_cdt": public_text(cross_links_cdt, limit=800),
        "cross_links_gdelt": public_text(cross_links_gdelt, limit=800),
        "cross_links_ooni": public_text(cross_links_ooni, limit=800),
        "cross_links_greatfire": public_text(cross_links_greatfire, limit=800),
        "cross_links_weibo": public_text(cross_links_weibo, limit=800),
        "cross_links_undertext": public_text(cross_links_undertext, limit=800),
        "cross_links_bleedthrough": public_text(cross_links_bleedthrough, limit=800),
        "cross_links_common_crawl": public_text(cross_links_common_crawl, limit=800),
        "common_crawl_match_kind": public_text(common_crawl_match_kind, limit=16),
        "common_crawl_host": public_text(common_crawl_host, limit=253),
        "common_crawl_capture_at": public_text(common_crawl_capture_at, limit=32),
        "common_crawl_mime_type": public_text(common_crawl_mime_type, limit=64),
        "common_crawl_languages": public_text(common_crawl_languages, limit=64),
        "common_crawl_content_digest": public_text(common_crawl_content_digest, limit=40),
        "common_crawl_locator_sha256": public_text(common_crawl_locator_sha256, limit=64),
        "gazetteer": gazetteer,
        "collector": public_text(collector, limit=40) or "unknown",
        "cite": "",
    }
    row["cite"] = _cite(row)
    return row


def _from_observation(obs: Mapping[str, Any], *, collector: str) -> dict[str, str] | None:
    title = public_text(obs.get("title"), limit=240)
    url = _https(obs.get("url") or obs.get("source_url"))
    if not title and not url:
        return None
    archive = obs.get("archive") if isinstance(obs.get("archive"), dict) else {}
    bracket = archive.get("timestamp_bracket") if isinstance(archive.get("timestamp_bracket"), dict) else {}
    links = obs.get("cross_links") if isinstance(obs.get("cross_links"), dict) else {}
    lake = obs.get("common_crawl") if isinstance(obs.get("common_crawl"), dict) else {}
    digest = _sha(obs.get("content_sha256"))
    body = public_text(obs.get("text") or obs.get("detail") or obs.get("note"), limit=8000)
    if not digest:
        digest = content_sha256(title, body, url)
    mirrors = []
    for candidate in obs.get("mirror_urls") or []:
        item = _https(candidate)
        if item and item not in mirrors:
            mirrors.append(item)
    terms = []
    for term in obs.get("terms") or []:
        item = public_text(term, limit=80)
        if item and item not in terms:
            terms.append(item)
    notes = []
    for note in obs.get("uncertainty") or []:
        item = public_text(note, limit=240)
        if item and item not in notes:
            notes.append(item)
    return _row(
        title=title,
        excerpt=public_text(body, limit=400),
        text=body,
        text_zh=public_text(obs.get("text_zh"), limit=8000),
        text_en=public_text(obs.get("text_en"), limit=8000),
        language=public_text(obs.get("language"), limit=16),
        uncertainty=" | ".join(notes),
        terms="; ".join(terms),
        source=public_text(obs.get("source"), limit=80) or collector,
        source_url=url,
        mirror_urls=" ".join(mirrors),
        first_seen=iso_z(obs.get("first_seen") or obs.get("detected_at")),
        last_seen=iso_z(obs.get("last_seen") or obs.get("detected_at")),
        last_confirmed_alive=iso_z(obs.get("last_confirmed_alive")),
        deletion_signal=public_text(
            obs.get("deletion_signal") or obs.get("event"), limit=80
        ),
        deletion_confirmations=_flatten_confirmations(obs.get("deletion_confirmation")),
        content_sha256_value=digest,
        wayback_lookup=_archive_url(archive, "wayback_lookup"),
        wayback_snapshot=_archive_url(archive, "wayback_snapshot"),
        wayback_raw=_archive_url(archive, "wayback_raw"),
        archive_today_lookup=_archive_url(archive, "archive_today_lookup"),
        ghostarchive_lookup=_archive_url(archive, "ghostarchive_lookup"),
        post_event_snapshot=_archive_url(archive, "post_event_snapshot"),
        bracket_before=public_text(bracket.get("last_live"), limit=80),
        bracket_after=public_text(bracket.get("post_event"), limit=80),
        cross_links_cdt=_flatten_link(links.get("cdt")),
        cross_links_gdelt=_flatten_link(links.get("gdelt")),
        cross_links_ooni=_flatten_link(links.get("ooni")),
        cross_links_greatfire=_flatten_link(links.get("greatfire")),
        cross_links_weibo=_flatten_link(links.get("weibo")),
        cross_links_undertext=_flatten_link(links.get("undertext")),
        cross_links_bleedthrough=_flatten_link(links.get("bleedthrough")),
        cross_links_common_crawl=_flatten_link(links.get("common_crawl")),
        common_crawl_match_kind=public_text(lake.get("match_kind"), limit=16),
        common_crawl_host=public_text(lake.get("host"), limit=253),
        common_crawl_capture_at=iso_z(lake.get("capture_at")) or "",
        common_crawl_mime_type=public_text(lake.get("mime_type"), limit=64),
        common_crawl_languages=public_text(lake.get("languages"), limit=64),
        common_crawl_content_digest=public_text(lake.get("content_digest"), limit=40),
        common_crawl_locator_sha256=public_text(lake.get("locator_sha256"), limit=64),
        gazetteer=_gazetteer_label(obs.get("gazetteer_hits")),
        collector=collector,
    )


def _from_wayback(rec: Mapping[str, Any]) -> dict[str, str] | None:
    title = public_text(rec.get("term") or rec.get("url"), limit=240)
    url = _https(rec.get("url"))
    if not title and not url:
        return None
    event = public_text(rec.get("event"), limit=80) or "unknown"
    first = _wayback_timestamp(rec.get("first_capture"))
    last = _wayback_timestamp(rec.get("last_capture"))
    snapshot = _https(rec.get("last_live_snapshot") or rec.get("wayback_snapshot"))
    lookup = f"https://web.archive.org/web/*/{url}" if url else ""
    return _row(
        title=f"[wayback:{event}] {title}" if title else f"[wayback:{event}]",
        excerpt=public_text(rec.get("detail") or rec.get("note") or event, limit=400),
        source="wayback:reconstruction",
        source_url=url,
        first_seen=first or last,
        last_seen=last or first,
        last_confirmed_alive=first,
        deletion_signal=event,
        content_sha256_value=content_sha256("wayback", event, url, title),
        wayback_lookup=lookup,
        wayback_snapshot=snapshot,
        wayback_raw="",
        archive_today_lookup=f"https://archive.today/{url}" if url else "",
        ghostarchive_lookup=f"https://ghostarchive.org/search?term={url}" if url else "",
        gazetteer=title,
        collector="wayback",
    )


def _richer(existing: Mapping[str, str], candidate: Mapping[str, str]) -> bool:
    def score(row: Mapping[str, str]) -> tuple[int, ...]:
        return (
            1 if row.get("wayback_snapshot") else 0,
            1 if row.get("source_url") else 0,
            1 if row.get("content_sha256") else 0,
            1 if row.get("last_confirmed_alive") else 0,
            1 if row.get("cross_links_gdelt") else 0,
            1 if row.get("cross_links_ooni") else 0,
            len(row.get("text") or row.get("excerpt") or ""),
            len(row.get("uncertainty") or ""),
            len(row.get("deletion_confirmations") or ""),
        )

    return score(candidate) > score(existing)


def _merge_gazetteer(*labels: str) -> str:
    seen: list[str] = []
    for label in labels:
        for part in (label or "").split("; "):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
            if len(seen) >= 8:
                return "; ".join(seen)
    return "; ".join(seen)


def _merge_row(existing: Mapping[str, str], candidate: Mapping[str, str]) -> dict[str, str]:
    keep = dict(candidate if _richer(existing, candidate) else existing)
    other = candidate if keep["key"] == existing.get("key") else existing
    keep["gazetteer"] = _merge_gazetteer(existing.get("gazetteer", ""), candidate.get("gazetteer", ""))
    if len(candidate.get("text") or "") > len(keep.get("text") or ""):
        keep["text"] = candidate["text"]
        keep["excerpt"] = candidate.get("excerpt") or keep.get("excerpt") or ""
    for field in (
        "text_zh", "text_en", "language", "uncertainty", "terms", "mirror_urls",
        "deletion_confirmations", "wayback_raw", "ghostarchive_lookup",
        "post_event_snapshot", "bracket_before", "bracket_after",
        "cross_links_cdt", "cross_links_gdelt", "cross_links_ooni",
        "cross_links_greatfire", "cross_links_weibo", "cross_links_undertext",
        "cross_links_bleedthrough", "cross_links_common_crawl",
        "common_crawl_match_kind", "common_crawl_host", "common_crawl_capture_at",
        "common_crawl_mime_type", "common_crawl_languages",
        "common_crawl_content_digest", "common_crawl_locator_sha256",
    ):
        if not keep.get(field) and other.get(field):
            keep[field] = other[field]
        elif field in {"terms", "mirror_urls", "uncertainty"} and other.get(field):
            parts = []
            sep = " | " if field == "uncertainty" else ("; " if field == "terms" else " ")
            for chunk in (keep.get(field) or "", other.get(field) or ""):
                for part in chunk.split(sep):
                    part = part.strip()
                    if part and part not in parts:
                        parts.append(part)
            keep[field] = sep.join(parts)
    keep["cite"] = _cite(keep)
    return keep


def _collect_rows(payloads: Mapping[str, Mapping[str, Any] | None]) -> list[dict[str, str]]:
    collected: dict[str, dict[str, str]] = {}
    by_url: dict[str, str] = {}

    def add(row: dict[str, str] | None) -> None:
        if not row:
            return
        url = row.get("source_url") or ""
        key = by_url.get(url) if url else None
        if key and key in collected:
            collected[key] = _merge_row(collected[key], row)
            return
        key = row["key"]
        if key in collected:
            collected[key] = _merge_row(collected[key], row)
            return
        collected[key] = row
        if url:
            by_url[url] = key

    undertext = payloads.get("undertext-latest.json") or {}
    for obs in undertext.get("observations") or []:
        if isinstance(obs, dict):
            add(_from_observation(obs, collector="undertext"))

    wayback = payloads.get("wayback-latest.json") or {}
    for rec in wayback.get("reconstructions") or []:
        if isinstance(rec, dict):
            add(_from_wayback(rec))
    for obs in wayback.get("observation_records") or []:
        if isinstance(obs, dict):
            add(_from_observation(obs, collector="wayback"))

    weibo = payloads.get("weibo-hotsearch-latest.json") or {}
    for obs in weibo.get("observation_records") or []:
        if isinstance(obs, dict):
            add(_from_observation(obs, collector="weibo-hotsearch"))

    ddti = payloads.get("ddti-latest.json") or {}
    for obs in ddti.get("observation_records") or []:
        if isinstance(obs, dict):
            add(_from_observation(obs, collector="ddti"))

    ledgers = payloads.get("public-deletion-ledgers-latest.json")
    if ledgers:
        for obs in ledgers.get("observations") or ledgers.get("observation_records") or []:
            if isinstance(obs, dict):
                add(_from_observation(obs, collector="public-deletion-ledgers"))

    rows = list(collected.values())
    rows.sort(key=lambda row: (
        row.get("last_seen") or "",
        row.get("first_seen") or "",
        row.get("key") or "",
    ), reverse=True)
    return rows


def _input_receipts(payloads: Mapping[str, Mapping[str, Any] | None]) -> list[dict[str, Any]]:
    receipts = []
    for name in INPUT_FILES:
        payload = payloads.get(name)
        receipts.append({
            "filename": name,
            "available": payload is not None,
            "generated_at": iso_z(payload.get("generated_at")) if payload else None,
        })
    return receipts


def build_document(
    *,
    readings_dir: Path | None = None,
) -> dict[str, Any] | None:
    readings_dir = readings_dir or READINGS
    payloads = {name: _load_json(name, readings_dir) for name in INPUT_FILES}
    clock = fusion_clock(payloads)
    if clock is None:
        return None
    rows = _collect_rows(payloads)
    if not rows:
        return None
    generated = iso_z(clock)
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "source": (
            "Offline journalist projection of Palimpsest reconstructions: "
            "UNDERTEXT, Wayback, Weibo-board, DDTI, GDELT/OONI/Bleedthrough "
            "joins, and optional public-deletion-ledger readings"
        ),
        "method": (
            "Deterministic flatten of already-published fat observation records "
            "and Wayback reconstructions, clustered by public URL. Clock is the "
            "newest input generated_at. No network fetch. No invented live ledger."
        ),
        "scope": (
            "Already-public China posts, deletions, and archive reconstructions "
            "that Palimpsest has already published. Not a census of all censorship."
        ),
        "safety": (
            "Watch the censor, never the censored. Public data only. Nobody "
            "inside China is asked to act."
        ),
        "honesty": {
            "captures": HONESTY_CAPTURES,
            "does_not_capture": HONESTY_DOES_NOT,
            "live_claim": HONESTY_LIVE,
        },
        "n_rows": len(rows),
        "inputs": _input_receipts(payloads),
        "rows": rows,
    }
    extra = set(document) - _TOP_FIELDS
    if extra:
        raise ValueError(f"erasure-trail document has unexpected keys: {sorted(extra)}")
    for row in rows:
        extra_row = set(row) - _ROW_FIELDS
        if extra_row:
            raise ValueError(f"erasure-trail row has unexpected keys: {sorted(extra_row)}")
    return document


def build_dossier_document(
    *,
    readings_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Build the separate piece-level dossier artifact from retained inputs."""

    readings_dir = readings_dir or READINGS
    payloads = {
        name: _load_json(name, readings_dir) for name in DOSSIER_INPUT_FILES
    }
    clock = fusion_clock(payloads)
    if clock is None:
        return None
    versions = _load_jsonl(SOCIAL_VERSIONS_FILE, readings_dir)
    return practice_dossiers.build_document(
        payloads,
        generated_at=iso_z(clock) or "",
        social_versions=versions,
    )


def render_csv(document: Mapping[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(ROW_FIELDS),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in document["rows"]:
        writer.writerow({field: row.get(field, "") for field in ROW_FIELDS})
    return buffer.getvalue()


def _h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _link(url: str, label: str | None = None) -> str:
    if not url.startswith("https://"):
        return _h(label or "—")
    return f'<a href="{_h(url)}">{_h(label or url)}</a>'


def _card(row: Mapping[str, str]) -> str:
    def pair(label: str, value: str, *, link: bool = False) -> str:
        if not value:
            return ""
        shown = _link(value) if link and value.startswith("https://") else _h(value)
        return f"<div><dt>{_h(label)}</dt><dd>{shown}</dd></div>"

    text = row.get("text") or row.get("excerpt") or ""
    return (
        f"<details class=\"et-card\" id=\"{_h(row['key'])}\">"
        f"<summary><strong>{_h(row['title'])}</strong>"
        f"<span>{_h(row.get('deletion_signal') or row.get('language') or 'public record')}</span></summary>"
        f"<p class=\"et-card-text\">{_h(text or '—')}</p>"
        "<dl class=\"et-card-meta\">"
        + pair("Language", row.get("language") or "")
        + pair("Terms", row.get("terms") or "")
        + pair("First seen", row.get("first_seen") or "")
        + pair("Last seen", row.get("last_seen") or "")
        + pair("Last confirmed alive", row.get("last_confirmed_alive") or "")
        + pair("SHA-256", row.get("content_sha256") or "")
        + pair("Source URL", row.get("source_url") or "", link=True)
        + pair("Mirrors", row.get("mirror_urls") or "", link=False)
        + pair("Wayback snapshot", row.get("wayback_snapshot") or "", link=True)
        + pair("Wayback lookup", row.get("wayback_lookup") or "", link=True)
        + pair("Wayback raw", row.get("wayback_raw") or "", link=True)
        + pair("archive.today", row.get("archive_today_lookup") or "", link=True)
        + pair("Ghostarchive", row.get("ghostarchive_lookup") or "", link=True)
        + pair("Post-event snapshot", row.get("post_event_snapshot") or "", link=True)
        + pair("Bracket before", row.get("bracket_before") or "")
        + pair("Bracket after", row.get("bracket_after") or "")
        + pair("Confirmations", row.get("deletion_confirmations") or "")
        + pair("Gazetteer", row.get("gazetteer") or "")
        + pair("Uncertainty", row.get("uncertainty") or "")
        + pair("CDT", row.get("cross_links_cdt") or "")
        + pair("GDELT", row.get("cross_links_gdelt") or "")
        + pair("OONI", row.get("cross_links_ooni") or "")
        + pair("GreatFire", row.get("cross_links_greatfire") or "")
        + pair("Weibo", row.get("cross_links_weibo") or "")
        + pair("UNDERTEXT", row.get("cross_links_undertext") or "")
        + pair("Bleedthrough", row.get("cross_links_bleedthrough") or "")
        + pair("Common Crawl lake", row.get("cross_links_common_crawl") or "")
        + pair("CC match", row.get("common_crawl_match_kind") or "")
        + pair("CC host", row.get("common_crawl_host") or "")
        + pair("CC capture", row.get("common_crawl_capture_at") or "")
        + pair("CC MIME", row.get("common_crawl_mime_type") or "")
        + pair("CC language", row.get("common_crawl_languages") or "")
        + pair("CC digest", row.get("common_crawl_content_digest") or "")
        + pair("CC locator", row.get("common_crawl_locator_sha256") or "")
        + f"<div><dt>Cite</dt><dd>{_h(row.get('cite') or '')}</dd></div>"
        + "</dl></details>"
    )


def _dossier_card(dossier: Mapping[str, Any]) -> str:
    qualification = dossier["qualification"]
    subject = dossier["subject"]
    practice = dossier["practice"]
    actor = practice["actor"]
    criticality = "".join(
        f"<li>{_h(item)}</li>" for item in qualification["criticality_basis"]
    ) or "<li>No separate criticality label was retained.</li>"
    timeline = "".join(
        "<li>"
        f"<time>{_h(row['at'])}</time> · {_h(row['event'])} "
        f"<span>({_h(row['source'])}; {_h(row['precision'])})</span>"
        "</li>"
        for row in dossier["timeline"]
    ) or "<li>Exact event time unavailable in retained metadata.</li>"
    measurements = "".join(
        "<article class=\"et-measure\">"
        f"<p class=\"et-measure-id\">{_h(row['reading_id'])} · "
        f"{_h(row['status'])} · {_h(row['match_kind'])}</p>"
        f"<h4>{_h(row['metric'])}</h4>"
        f"<p>{_h(row['value'] or '—')}</p>"
        "<dl>"
        f"<div><dt>Source clock</dt><dd>{_h(row['source_timestamp'] or 'unknown')}</dd></div>"
        f"<div><dt>Reading</dt><dd>{_link(row['reading_url'], row['reading_id'])}</dd></div>"
        f"<div><dt>Input SHA-256</dt><dd><code>{_h(row['input_sha256'] or 'unavailable')}</code></dd></div>"
        f"<div><dt>Limit</dt><dd>{_h(row['interpretation_limit'])}</dd></div>"
        "</dl></article>"
        for row in dossier["measurements"]
    )
    evidence = "".join(
        "<li>"
        f"<strong>{_h(row['relation'])}</strong> · {_h(row['claim'])} "
        f"{_link(row['source_url'], row['source_name']) if row['source_url'] else _h(row['source_name'])} "
        f"<span>({_h(row['observed_at'] or 'clock unavailable')}; "
        f"SHA-256 {_h(row['input_sha256'])})</span>"
        "</li>"
        for row in dossier["evidence"]
    )
    counter = "".join(
        f"<li>{_h(item)}</li>" for item in dossier["counter_readings"]
    )
    unknowns = "".join(f"<li>{_h(item)}</li>" for item in dossier["unknowns"])
    mechanisms = " · ".join(practice["mechanisms"])
    actor_name = actor["name"] or "Actor not established"
    excerpt = subject["excerpt"] or "No public excerpt retained."
    return (
        f"<details class=\"et-dossier\" id=\"{_h(dossier['dossier_id'])}\" open>"
        "<summary>"
        "<span class=\"et-dossier-heading\">"
        f"<span class=\"et-state et-state--{_h(qualification['state'])}\">"
        f"{_h(qualification['state'].replace('_', ' '))}</span>"
        f"<strong>{_h(subject['title'])}</strong>"
        "</span>"
        f"<span class=\"et-mechanism\">{_h(mechanisms)}</span>"
        "</summary>"
        "<div class=\"et-dossier-body\">"
        f"<p class=\"et-finding\">{_h(practice['finding'])}</p>"
        f"<blockquote>{_h(excerpt)}</blockquote>"
        "<div class=\"et-dossier-grid\">"
        "<section><h3>What qualifies this piece</h3>"
        f"<p>{_h(qualification['basis'])}</p><ul>{criticality}</ul></section>"
        "<section><h3>Actor attribution</h3>"
        f"<p><strong>{_h(actor_name)}</strong> · {_h(actor['role'])} · "
        f"{_h(actor['attribution'])}</p>"
        f"<p>{_h(actor['basis'])}</p></section>"
        "<section><h3>Timeline</h3>"
        f"<ol class=\"et-timeline\">{timeline}</ol></section>"
        "<section><h3>Piece identity</h3><dl class=\"et-card-meta\">"
        f"<div><dt>Kind</dt><dd>{_h(subject['kind'])}</dd></div>"
        f"<div><dt>Platform</dt><dd>{_h(subject['platform'] or 'unavailable')}</dd></div>"
        f"<div><dt>Source</dt><dd>{_h(subject['source'] or 'unavailable')}</dd></div>"
        f"<div><dt>URL</dt><dd>{_link(subject['url']) if subject['url'] else 'unavailable'}</dd></div>"
        f"<div><dt>Content SHA-256</dt><dd><code>{_h(subject['content_sha256'])}</code></dd></div>"
        "</dl></section></div>"
        "<section><h3>What Palimpsest measured</h3>"
        f"<div class=\"et-measures\">{measurements}</div></section>"
        "<div class=\"et-dossier-grid\">"
        f"<section><h3>Evidence rows</h3><ul>{evidence}</ul></section>"
        f"<section><h3>Counter-readings</h3><ul>{counter}</ul></section>"
        f"<section><h3>Unknowns</h3><ul>{unknowns}</ul></section>"
        "<section><h3>Claim ceiling</h3>"
        f"<p>{_h(practice['interpretation_limit'])}</p></section>"
        "</div>"
        f"<p class=\"et-citation\">{_h(dossier['cite'])}</p>"
        f"<button type=\"button\" class=\"et-cite\" data-cite=\"{_h(dossier['cite'])}\">Copy cite</button>"
        "</div></details>"
    )


def render_html(
    document: Mapping[str, Any],
    dossier_document: Mapping[str, Any] | None = None,
) -> str:
    rows_html = []
    cards_html = []
    for row in document["rows"]:
        cards_html.append(_card(row))
        rows_html.append(
            "<tr>"
            f"<td><strong>{_h(row['title'])}</strong>"
            f"<div class=\"et-excerpt\">{_h(row['excerpt'] or '—')}</div></td>"
            f"<td>{_h(row['deletion_signal'] or '—')}</td>"
            f"<td>{_link(row['source_url'], row['source'])}</td>"
            f"<td><time>{_h(row['first_seen'] or 'unknown')}</time></td>"
            f"<td><time>{_h(row['last_seen'] or 'unknown')}</time></td>"
            f"<td>{_link(row['wayback_snapshot'], 'snapshot') if row['wayback_snapshot'] else _link(row['wayback_lookup'], 'lookup')}</td>"
            f"<td><code>{_h(row['content_sha256'][:16] + '…' if row['content_sha256'] else '—')}</code></td>"
            f"<td><button type=\"button\" class=\"et-cite\" data-cite=\"{_h(row['cite'])}\">Copy cite</button></td>"
            "</tr>"
        )
    table = "\n".join(rows_html) if rows_html else (
        "<tr><td colspan=\"8\">No public rows in the committed inputs. "
        "Absence is a coverage gap, not a live zero.</td></tr>"
    )
    cards = "\n".join(cards_html) if cards_html else (
        "<p>No public reconstructions in the committed inputs. "
        "Absence is a coverage gap, not a live zero.</p>"
    )
    nav = site_nav.render("/news/china/erasure/")
    generated = _h(document["generated_at"])
    dossier_document = dossier_document or {
        "generated_at": document["generated_at"],
        "status": "coverage_gap",
        "counts": {
            "dossiers": 0,
            "observed_disappearances": 0,
            "peer_reported": 0,
            "pattern_signals": 0,
            "review_required": 0,
            "captured_items_reviewed": 0,
            "excluded_items": 0,
        },
        "coverage": {"collector_receipts": [], "exclusions": []},
        "dossiers": [],
    }
    dossier_cards = "\n".join(
        _dossier_card(dossier) for dossier in dossier_document["dossiers"]
    ) or (
        "<p class=\"et-empty\">No qualifying piece-level dossier in the retained "
        "inputs. This is a coverage state, not a zero-censorship finding.</p>"
    )
    dossier_counts = dossier_document["counts"]
    collector_gaps = sum(
        1
        for row in dossier_document["coverage"]["collector_receipts"]
        if row["status"] not in {"ok", "success"}
    )
    exclusion_lines = "".join(
        f"<li><strong>{_h(row['count'])}</strong> · "
        f"{_h(row['reason'].replace('_', ' '))}</li>"
        for row in dossier_document["coverage"]["exclusions"]
    ) or "<li>No excluded candidates in the retained inputs.</li>"
    dossier_generated = _h(dossier_document["generated_at"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>How China censorship is practiced · piece-level evidence · Palimpsest</title>
<meta name="description" content="Piece-level China censorship-practice dossiers: exact post or article, observed or reported mechanism, actor attribution, timeline, Palimpsest measurements, evidence hashes, counter-readings and unknowns.">
<link rel="canonical" href="{SITE}/news/china/erasure/">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Palimpsest">
<meta property="og:title" content="How censorship was practiced — piece by piece">
<meta property="og:description" content="Exact evidence state, mechanism, actor attribution, timeline, measurement receipts and claim limits for every qualifying item Palimpsest captured.">
<meta property="og:url" content="{SITE}/news/china/erasure/">
<meta property="og:image" content="{SITE}/brand/og-site.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="stylesheet" href="/dashboards/assets/tikto.css">
<link rel="stylesheet" href="/assets/shell.css">
<style>
.et-hero{{padding:clamp(36px,7vw,72px) 0 24px}}
.et-title{{font-size:clamp(40px,7vw,84px);font-weight:800;line-height:.9;letter-spacing:-.04em;margin:0;max-width:900px}}
.et-deck{{font-size:clamp(16px,2vw,21px);line-height:1.5;color:var(--tk-text-2);max-width:760px;margin:18px 0 0}}
.et-actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}}
.et-counts{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:0 0 24px}}
.et-count{{padding:14px 16px;border:1px solid var(--tk-line-1);background:var(--tk-bg-1)}}
.et-count strong{{display:block;font:800 26px/1 var(--tk-font-mono)}}
.et-count span{{display:block;margin-top:7px;color:var(--tk-text-3);font:600 10px/1.3 var(--tk-font-mono);letter-spacing:.08em;text-transform:uppercase}}
.et-honesty{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:0 0 28px}}
.et-honesty article{{padding:16px 18px}}
.et-honesty h2{{margin:0 0 8px;font-size:13px;letter-spacing:.08em;text-transform:uppercase}}
.et-honesty p{{margin:0;color:var(--tk-text-2);font-size:14px;line-height:1.55}}
.et-meta{{font:600 11px/1.4 var(--tk-font-mono);color:var(--tk-text-3);margin:0 0 14px}}
.et-table{{width:100%;border-collapse:collapse;font-size:13px}}
.et-table th{{text-align:left;font:600 10px/1.3 var(--tk-font-mono);letter-spacing:.1em;text-transform:uppercase;color:var(--tk-text-4);padding:10px 8px;border-bottom:1px solid var(--tk-line-2)}}
.et-table td{{padding:12px 8px;border-bottom:1px solid var(--tk-line-1);vertical-align:top}}
.et-excerpt{{color:var(--tk-text-3);font-size:12px;margin-top:6px;max-width:36ch}}
.et-table code{{font-size:11px}}
.et-cite{{min-height:36px;padding:6px 10px;border:1px solid var(--tk-line-2);background:transparent;color:var(--tk-text-2);font:600 10px/1 var(--tk-font-mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer}}
.et-cite:hover{{color:var(--tk-text-0);border-color:var(--tk-line-3)}}
.et-section-head{{margin:34px 0 8px;max-width:900px}}
.et-section-deck{{max-width:820px;color:var(--tk-text-2);line-height:1.6;margin:0 0 18px}}
.et-dossiers{{display:grid;gap:16px;margin:20px 0 34px}}
.et-dossier{{border:1px solid var(--tk-line-2);background:var(--tk-bg-1)}}
.et-dossier>summary{{cursor:pointer;display:flex;justify-content:space-between;gap:24px;padding:18px;align-items:flex-start}}
.et-dossier-heading{{display:grid;gap:9px}}
.et-dossier-heading strong{{font-size:clamp(17px,2vw,23px);line-height:1.25}}
.et-state{{width:max-content;padding:5px 7px;border:1px solid var(--tk-line-2);font:700 9px/1 var(--tk-font-mono);letter-spacing:.09em;text-transform:uppercase}}
.et-state--observed_disappearance{{border-color:#d66;color:#f99}}
.et-state--peer_reported{{border-color:#d89b45;color:#efbd73}}
.et-state--pattern_signal{{border-color:#7799d8;color:#a9c1f2}}
.et-state--review_required{{border-color:#888;color:var(--tk-text-2)}}
.et-mechanism{{max-width:34ch;color:var(--tk-text-3);font:600 10px/1.4 var(--tk-font-mono);letter-spacing:.07em;text-align:right;text-transform:uppercase}}
.et-dossier-body{{padding:0 18px 20px;border-top:1px solid var(--tk-line-1)}}
.et-finding{{font-size:clamp(16px,2vw,20px);line-height:1.55;max-width:920px}}
.et-dossier blockquote{{margin:16px 0;padding:12px 16px;border-left:3px solid var(--tk-line-3);color:var(--tk-text-2);white-space:pre-wrap}}
.et-dossier-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:16px 0}}
.et-dossier-grid section{{padding:14px 16px;border:1px solid var(--tk-line-1)}}
.et-dossier h3{{margin:0 0 8px;font:700 11px/1.3 var(--tk-font-mono);letter-spacing:.08em;text-transform:uppercase}}
.et-dossier h4{{margin:6px 0 8px;font-size:15px}}
.et-dossier ul,.et-dossier ol{{margin:8px 0 0;padding-left:1.2em;color:var(--tk-text-2);line-height:1.55}}
.et-dossier li+li{{margin-top:6px}}
.et-dossier li span{{color:var(--tk-text-3)}}
.et-measures{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
.et-measure{{padding:14px;border:1px solid var(--tk-line-1);background:var(--tk-bg-0)}}
.et-measure-id{{margin:0;color:var(--tk-text-3);font:600 9px/1.4 var(--tk-font-mono);letter-spacing:.07em;text-transform:uppercase}}
.et-measure p{{color:var(--tk-text-2);line-height:1.5}}
.et-measure dl{{margin:0;display:grid;gap:8px}}
.et-measure dt{{font:600 9px/1.3 var(--tk-font-mono);letter-spacing:.07em;text-transform:uppercase;color:var(--tk-text-4)}}
.et-measure dd{{margin:3px 0 0;overflow-wrap:anywhere;color:var(--tk-text-2)}}
.et-citation{{overflow-wrap:anywhere;color:var(--tk-text-3);font:500 11px/1.5 var(--tk-font-mono)}}
.et-exclusions{{padding:16px 18px;margin:0 0 28px;border:1px solid var(--tk-line-1)}}
.et-exclusions ul{{columns:2;gap:28px;color:var(--tk-text-2);line-height:1.55}}
.et-empty{{padding:18px;border:1px solid var(--tk-line-1);color:var(--tk-text-2)}}
.et-records{{display:grid;gap:12px;margin:28px 0}}
.et-card{{padding:16px 18px;border:1px solid var(--tk-line-1);background:var(--tk-bg-1)}}
.et-card summary{{cursor:pointer;display:flex;justify-content:space-between;gap:12px;align-items:baseline}}
.et-card summary span{{font:600 11px/1.3 var(--tk-font-mono);color:var(--tk-text-3);letter-spacing:.08em;text-transform:uppercase}}
.et-card-text{{white-space:pre-wrap;color:var(--tk-text-1);line-height:1.55;margin:14px 0}}
.et-card-meta{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 18px;margin:0}}
.et-card-meta dt{{font:600 10px/1.3 var(--tk-font-mono);letter-spacing:.08em;text-transform:uppercase;color:var(--tk-text-4)}}
.et-card-meta dd{{margin:4px 0 0;color:var(--tk-text-2);overflow-wrap:anywhere}}
.et-how{{margin:28px 0;max-width:760px}}
.et-how ol{{margin:8px 0 0;padding-left:1.2em;color:var(--tk-text-2);line-height:1.6}}
@media(max-width:900px){{.et-counts{{grid-template-columns:repeat(2,minmax(0,1fr))}}.et-honesty,.et-dossier-grid,.et-measures{{grid-template-columns:1fr}}.et-dossier>summary{{display:grid}}.et-mechanism{{text-align:left}}.et-exclusions ul{{columns:1}}.et-wrap{{overflow-x:auto}}}}
</style>
</head>
<body class="ps">
{nav}
<main id="main" class="ps-wrap ps-wrap--wide">
  <header class="et-hero">
    <p class="ps-kicker">Censorship-practice dossiers · public evidence only</p>
    <h1 class="et-title">How censorship was practiced.<br>Piece by piece.</h1>
    <p class="et-deck">Every qualifying post, article, or topic Palimpsest captured gets its own dossier: what the item said, what was observed versus reported, the information-control mechanism, who the evidence actually names, the timeline, every matching Palimpsest reading and input hash, counter-readings, and what remains unknown. Reporting critical of PRC authorities is not called censored merely because it is critical. “Every” means every qualifying item in the disclosed collector inputs—not every post on the internet.</p>
    <div class="et-actions">
      <a class="ps-btn" href="/readings/censorship-practice-dossiers-latest.json">Download dossier JSON</a>
      <a class="ps-btn ps-btn--ghost" href="/readings/erasure-trail.csv">Download CSV (raw)</a>
      <a class="ps-btn ps-btn--ghost" href="/readings/erasure-trail-latest.json">Download JSON (raw)</a>
      <a class="ps-btn ps-btn--ghost" href="/docs/FOR-JOURNALISTS.md">How to cite</a>
      <a class="ps-btn ps-btn--ghost" href="/osint-china.html">Signal board</a>
    </div>
  </header>

  <section class="et-honesty" aria-label="What this desk captures">
    <article class="ps-p2">
      <h2>Captured</h2>
      <p>{_h(document["honesty"]["captures"])}</p>
    </article>
    <article class="ps-p2">
      <h2>Not captured</h2>
      <p>{_h(document["honesty"]["does_not_capture"])}</p>
    </article>
    <article class="ps-p2">
      <h2>Live readings</h2>
      <p>{_h(document["honesty"]["live_claim"])}</p>
    </article>
  </section>

  <section class="et-counts" aria-label="Dossier evidence-state counts">
    <div class="et-count"><strong>{_h(dossier_counts["dossiers"])}</strong><span>Qualifying dossiers</span></div>
    <div class="et-count"><strong>{_h(dossier_counts["observed_disappearances"])}</strong><span>Observed disappearances</span></div>
    <div class="et-count"><strong>{_h(dossier_counts["peer_reported"])}</strong><span>Peer reported</span></div>
    <div class="et-count"><strong>{_h(dossier_counts["pattern_signals"] + dossier_counts["review_required"])}</strong><span>Patterns / review</span></div>
    <div class="et-count"><strong>{_h(collector_gaps)}</strong><span>Collector gaps</span></div>
  </section>

  <p class="et-meta">{_h(dossier_counts["captured_items_reviewed"])} captured input records reviewed · {_h(dossier_counts["dossiers"])} qualifying dossiers · dossier clock {dossier_generated} · method v{practice_dossiers.METHOD_VERSION}</p>
  <h2 class="ps-section-head et-section-head">Every qualifying captured piece</h2>
  <p class="et-section-deck">Open a dossier to see the exact claim ceiling. <b>Observed disappearance</b> establishes a state transition, not its cause. <b>Peer reported</b> preserves an external report without pretending Palimpsest verified it. <b>Pattern signal</b> is topic-level. <b>Review required</b> is deliberately not a censorship finding. CCP, PRC authority, platform, or local-authority responsibility appears only when retained evidence explicitly names that actor.</p>
  <section class="et-dossiers" aria-label="Piece-level censorship-practice dossiers, readable without JavaScript">
    {dossier_cards}
  </section>

  <aside class="et-exclusions">
    <p class="ps-kicker">Qualification audit</p>
    <h2>What the classifier reviewed but refused to call censorship</h2>
    <p>Excluded items remain counted so silence in this dossier list cannot hide a failed or noisy input.</p>
    <ul>{exclusion_lines}</ul>
  </aside>

  <h2 class="ps-section-head et-section-head">Raw reconstruction archive</h2>
  <p class="et-section-deck">The archive below preserves every retained reconstruction—including archive transitions and lexical board rows that do not clear the dossier qualification gate. It is evidence for inspection, not a list of confirmed censorship acts.</p>
  <p class="et-meta">{_h(document["n_rows"])} public reconstructions · archive clock {generated} from committed inputs · method v{METHOD_VERSION}</p>
  <section class="et-records" aria-label="Fat Palimpsest reconstructions, readable without JavaScript">
    {cards}
  </section>
  <div class="et-wrap ps-p1" tabindex="0" role="region" aria-label="Public deletion and reconstruction trail, scrolls horizontally">
    <table class="et-table">
      <thead>
        <tr>
          <th>Record</th>
          <th>Signal</th>
          <th>Source URL</th>
          <th>First seen</th>
          <th>Last seen</th>
          <th>Snapshot</th>
          <th>SHA-256</th>
          <th>Cite</th>
        </tr>
      </thead>
      <tbody>
        {table}
      </tbody>
    </table>
  </div>

  <section class="et-how" id="cite">
    <p class="ps-kicker">How to cite</p>
    <h2 class="ps-section-head">Export the row. Quote the trail. Do not upgrade the claim.</h2>
    <ol>
      <li>Find the row by title, gazetteer term, or source URL.</li>
      <li>Copy first-seen, last-seen, the snapshot or lookup URL, and the SHA-256.</li>
      <li>Download the <a href="/readings/censorship-practice-dossiers-latest.json">dossier JSON</a> for item-level claims, or the <a href="/readings/erasure-trail.csv">raw CSV</a> / <a href="/readings/erasure-trail-latest.json">raw JSON</a> for the complete reconstruction record.</li>
      <li>Preserve the dossier evidence state: cite an observed disappearance as a state transition, a peer report as attributed reporting, and a board pattern as context. Do not upgrade any of them into motive or actor attribution.</li>
    </ol>
    <p>Method: <a href="/docs/CHINA-CAPTURE.md">China capture</a> · <a href="/docs/FOR-JOURNALISTS.md">Journalist guide</a> · <a href="/protocol/china-observation-v1.schema.json">Observation schema</a>.</p>
  </section>
</main>
{site_nav.FOOT}
<script>
document.querySelectorAll(".et-cite").forEach(function (button) {{
  button.addEventListener("click", function () {{
    var text = button.getAttribute("data-cite") || "";
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(text).then(function () {{
        button.textContent = "Copied";
      }}).catch(function () {{
        button.textContent = "Copy failed";
      }});
    }}
  }});
}});
</script>
</body>
</html>
"""


def _canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _history_payload(document: Mapping[str, Any], *, path: Path | None = None) -> str:
    """Return the information-preserving, idempotent history payload."""
    history_path = path or HIST
    rows: list[Any] = []
    seen: set[str] = set()
    if history_path.is_file():
        for line_number, raw_line in enumerate(
            history_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON in {history_path} at line {line_number}: {exc.msg}"
                ) from exc
            identity = json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if identity not in seen:
                rows.append(row)
                seen.add(identity)

    current = {
        "generated_at": document["generated_at"],
        "n_rows": document["n_rows"],
    }
    identity = json.dumps(
        current, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if identity not in seen:
        rows.append(current)

    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )


def write_outputs(
    document: Mapping[str, Any],
    dossier_document: Mapping[str, Any] | None = None,
    *,
    history: bool = True,
) -> None:
    READINGS.mkdir(parents=True, exist_ok=True)
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(_canonical_json(document), encoding="utf-8")
    if dossier_document is not None:
        DOSSIER_JSON_OUT.write_text(
            _canonical_json(dossier_document), encoding="utf-8"
        )
    CSV_OUT.write_text(render_csv(document), encoding="utf-8")
    HTML_OUT.write_text(
        render_html(document, dossier_document=dossier_document), encoding="utf-8"
    )
    if history:
        HIST.write_text(_history_payload(document), encoding="utf-8")


def check_outputs(
    document: Mapping[str, Any],
    dossier_document: Mapping[str, Any] | None = None,
) -> list[str]:
    problems: list[str] = []
    expected = {
        JSON_OUT: _canonical_json(document),
        CSV_OUT: render_csv(document),
        HTML_OUT: render_html(document, dossier_document=dossier_document),
        HIST: _history_payload(document),
    }
    if dossier_document is not None:
        expected[DOSSIER_JSON_OUT] = _canonical_json(dossier_document)
    for path, payload in expected.items():
        if not path.is_file():
            problems.append(f"missing {path.relative_to(ROOT)}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != payload:
            problems.append(f"stale {path.relative_to(ROOT)}")
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate committed outputs")
    parser.add_argument("--readings-dir", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    readings_dir = args.readings_dir or READINGS
    document = build_document(readings_dir=readings_dir)
    if document is None:
        print("erasure-trail: no dated public inputs — abstaining")
        return 2
    dossier_document = build_dossier_document(readings_dir=readings_dir)
    if dossier_document is None:
        print("erasure-trail: no dated dossier inputs — abstaining")
        return 2
    if args.check:
        problems = check_outputs(document, dossier_document=dossier_document)
        if problems:
            print("erasure-trail --check failed:\n  " + "\n  ".join(problems))
            return 1
        print(
            "erasure-trail: current · "
            f"{document['n_rows']} rows · "
            f"{dossier_document['counts']['dossiers']} dossiers · "
            f"{dossier_document['generated_at']}"
        )
        return 0
    write_outputs(document, dossier_document=dossier_document)
    print(
        f"erasure-trail: {document['n_rows']} row(s) · "
        f"{dossier_document['counts']['dossiers']} dossier(s) · "
        f"{dossier_document['generated_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
