"""Shared richness for China OSINT observations.

Collectors historically emitted a thin DDTI row (terms, detected_at, title, url,
source). This module adds the fields the observatory can honestly carry from
*public* inputs already in hand: archived text, bilingual spans, sighting
brackets, deletion-confirmation trails, archive lookup/snapshot URLs, gazetteer
hits, source/mirror URLs, content hashes, related-signal cross-links, and
capture provenance.

Nothing here invents a live reading. Archive lookup URLs are constructed from a
public URL; they are *addresses to try*, not claimed captures, unless a caller
supplies a witnessed snapshot. Cross-links are attached only when the caller
passes a real related record. Gazetteer hits are lexical over the
human-authored lexicon.

The extra keys are additive. Existing scorers that read only terms / detected_at
/ title / url keep working.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent.parent
GAZETTEER_PATH = ROOT / "config" / "zh_censorship_gazetteer.json"

SCHEMA_VERSION = "palimpsest-china-observation.v1"
METHOD_VERSION = 1
MAX_PUBLIC_TEXT = 8_000
MAX_CONFIRMATIONS = 12
MAX_MIRRORS = 24
MAX_HITS = 24
MAX_UNCERTAINTY = 12

WAYBACK_LOOKUP = "https://web.archive.org/web/*/{url}"
WAYBACK_SNAPSHOT = "https://web.archive.org/web/{ts}/{url}"
WAYBACK_RAW = "https://web.archive.org/web/{ts}id_/{url}"
ARCHIVE_TODAY_LOOKUP = "https://archive.today/{url}"
GHOSTARCHIVE_LOOKUP = "https://ghostarchive.org/search?term={url}"
LANGUAGE_TAGS = ("zh", "en", "mixed", "unknown")

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]{2,}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime | str | None) -> str | None:
    """Normalise a timestamp to ``YYYY-MM-DDTHH:MM:SSZ`` or return None."""

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("+00:00"):
            text = text[:-6] + "Z"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
            return text
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        value = parsed.astimezone(timezone.utc)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_sha256(*parts: str) -> str:
    payload = "\x1f".join(p or "" for p in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def observation_key(observation: Mapping[str, Any]) -> str:
    """Stable public identity used by Situation and the Common Crawl lake join."""

    url = public_text(observation.get("url") or observation.get("source_url"), limit=2048)
    return content_sha256(
        public_text(observation.get("source"), limit=80),
        url,
        public_text(observation.get("title"), limit=240),
    )[:32]


def public_text(value: Any, *, limit: int = MAX_PUBLIC_TEXT) -> str:
    """Bound public text. Strip angle brackets so feed HTML cannot become markup."""

    if not isinstance(value, str):
        return ""
    cleaned = value.replace("<", "").replace(">", "").strip()
    if len(cleaned) > limit:
        return cleaned[:limit]
    return cleaned


def bilingual_fields(title: str = "", text: str = "") -> dict[str, str]:
    """Split already-present zh/en spans. Does not machine-translate."""

    blob = f"{title or ''}\n{text or ''}".strip()
    zh_parts: list[str] = []
    en_parts: list[str] = []
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        if _CJK_RE.search(line):
            zh_parts.append(line)
        elif _LATIN_WORD_RE.search(line):
            en_parts.append(line)
    return {
        "text_zh": public_text("\n".join(zh_parts)),
        "text_en": public_text("\n".join(en_parts)),
    }


def language_tag(*, text_zh: str = "", text_en: str = "") -> str:
    """Lexical language tag from already-split spans. Not a translator."""

    has_zh = bool((text_zh or "").strip())
    has_en = bool((text_en or "").strip())
    if has_zh and has_en:
        return "mixed"
    if has_zh:
        return "zh"
    if has_en:
        return "en"
    return "unknown"


@lru_cache(maxsize=1)
def load_gazetteer_index() -> tuple[dict[str, dict[str, str]], tuple[str, ...]]:
    """Return (zh -> {zh, en, category, first_seen}, terms longest-first)."""

    try:
        doc = json.loads(GAZETTEER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, ()
    index: dict[str, dict[str, str]] = {}
    for category, entries in (doc.get("categories") or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            zh = str(entry.get("zh") or "").strip()
            if len(zh) < 2:
                continue
            index[zh] = {
                "zh": zh,
                "en": str(entry.get("en") or "").strip(),
                "category": str(category),
                "first_seen": str(entry.get("first_seen") or ""),
            }
    terms = tuple(sorted(index, key=len, reverse=True))
    return index, terms


def gazetteer_hits(*texts: str, limit: int = MAX_HITS) -> list[dict[str, str]]:
    """Lexical gazetteer hits over public text. Watch the vocabulary, not a person."""

    blob = " ".join(t for t in texts if t)
    if not blob:
        return []
    index, terms = load_gazetteer_index()
    hits: list[dict[str, str]] = []
    seen: set[str] = set()
    for term in terms:
        if term in seen or term not in blob:
            continue
        seen.add(term)
        hits.append(dict(index[term]))
        if len(hits) >= limit:
            break
    return hits


def archive_lookup(url: str) -> dict[str, str | None]:
    """Construct public archive *lookup* addresses. Not a claim that a capture exists."""

    url = (url or "").strip()
    if not url.startswith("https://") and not url.startswith("http://"):
        return {
            "wayback_lookup": None,
            "wayback_snapshot": None,
            "wayback_raw": None,
            "archive_today_lookup": None,
            "ghostarchive_lookup": None,
        }
    encoded = quote(url, safe=":/")
    return {
        "wayback_lookup": WAYBACK_LOOKUP.format(url=encoded),
        "wayback_snapshot": None,
        "wayback_raw": None,
        "archive_today_lookup": ARCHIVE_TODAY_LOOKUP.format(url=encoded),
        "ghostarchive_lookup": GHOSTARCHIVE_LOOKUP.format(url=encoded),
    }


def archive_witness(
    url: str,
    *,
    last_live_ts: str | None = None,
    post_event_ts: str | None = None,
    last_live_snapshot: str | None = None,
    post_event_snapshot: str | None = None,
) -> dict[str, Any]:
    """Archive addresses plus optional witnessed snapshot URLs and timestamp brackets."""

    row = archive_lookup(url)
    encoded = quote((url or "").strip(), safe=":/") if url else ""
    if last_live_snapshot:
        row["wayback_snapshot"] = last_live_snapshot
    elif last_live_ts and encoded:
        row["wayback_snapshot"] = WAYBACK_SNAPSHOT.format(ts=last_live_ts, url=encoded)
        row["wayback_raw"] = WAYBACK_RAW.format(ts=last_live_ts, url=encoded)
    if post_event_snapshot:
        row["post_event_snapshot"] = post_event_snapshot
    elif post_event_ts and encoded:
        row["post_event_snapshot"] = WAYBACK_SNAPSHOT.format(ts=post_event_ts, url=encoded)
    else:
        row["post_event_snapshot"] = None
    row["timestamp_bracket"] = {
        "last_live": last_live_ts or None,
        "post_event": post_event_ts or None,
    }
    return row


def deletion_confirmation(
    *,
    status: str,
    observed_at: datetime | str | None,
    source: str,
    note: str = "",
) -> dict[str, str | None]:
    return {
        "status": public_text(status, limit=80),
        "observed_at": iso_z(observed_at),
        "source": public_text(source, limit=120),
        "note": public_text(note, limit=400),
    }


def sighting_fields(
    *,
    first_seen: datetime | str | None = None,
    last_seen: datetime | str | None = None,
    last_confirmed_alive: datetime | str | None = None,
    confirmations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    trail = []
    for item in list(confirmations or [])[:MAX_CONFIRMATIONS]:
        if not isinstance(item, Mapping):
            continue
        trail.append({
            "status": public_text(item.get("status"), limit=80),
            "observed_at": iso_z(item.get("observed_at")),
            "source": public_text(item.get("source"), limit=120),
            "note": public_text(item.get("note"), limit=400),
        })
    return {
        "first_seen": iso_z(first_seen),
        "last_seen": iso_z(last_seen),
        "last_confirmed_alive": iso_z(last_confirmed_alive),
        "deletion_confirmation": trail,
    }


def _related_link(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, Mapping) or not record:
        return None
    return {
        "id": public_text(record.get("id") or record.get("signal_id"), limit=80) or None,
        "url": public_text(record.get("url"), limit=2048) or None,
        "title": public_text(record.get("title"), limit=240) or None,
        "note": public_text(record.get("note"), limit=400) or None,
    }


def cross_links(
    *,
    cdt: Mapping[str, Any] | None = None,
    gdelt: Mapping[str, Any] | None = None,
    ooni: Mapping[str, Any] | None = None,
    greatfire: Mapping[str, Any] | None = None,
    weibo: Mapping[str, Any] | None = None,
    undertext: Mapping[str, Any] | None = None,
    bleedthrough: Mapping[str, Any] | None = None,
    common_crawl: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach only caller-supplied related records. Absence stays null, never a zero."""

    return {
        "cdt": _related_link(cdt),
        "gdelt": _related_link(gdelt),
        "ooni": _related_link(ooni),
        "greatfire": _related_link(greatfire),
        "weibo": _related_link(weibo),
        "undertext": _related_link(undertext),
        "bleedthrough": _related_link(bleedthrough),
        "common_crawl": _related_link(common_crawl),
    }


def merge_cross_links(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Prefer a non-null incoming link; keep an existing one when the incoming is empty."""

    base = cross_links()
    for source in (existing, incoming):
        if not isinstance(source, Mapping):
            continue
        for key in base:
            linked = _related_link(source.get(key) if isinstance(source.get(key), Mapping) else None)
            if linked:
                base[key] = linked
    return base


def uncertainty_notes(observation: Mapping[str, Any]) -> list[str]:
    """Honest gaps. Absence is named; it is never filled with a synthetic fact."""

    notes: list[str] = []
    url = public_text(observation.get("url") or observation.get("source_url"), limit=2048)
    text = public_text(observation.get("text"), limit=MAX_PUBLIC_TEXT)
    title = public_text(observation.get("title"), limit=1000)
    archive = observation.get("archive") if isinstance(observation.get("archive"), dict) else {}
    links = observation.get("cross_links") if isinstance(observation.get("cross_links"), dict) else {}
    confirmations = observation.get("deletion_confirmation")
    if not url.startswith("https://"):
        notes.append("no public source URL on this row")
    if not archive.get("wayback_snapshot"):
        notes.append("no witnessed Wayback snapshot; lookup is an address to try")
    if not text or text == title or len(text) < 80:
        notes.append("public text is title/term only; article body was not captured")
    if not isinstance(confirmations, list) or not confirmations:
        notes.append("no deletion-confirmation trail on this row")
    if not observation.get("last_confirmed_alive"):
        notes.append("last-confirmed-alive is unknown")
    if not links.get("cdt"):
        notes.append("no China Digital Times ledger join")
    if links.get("ooni") or links.get("bleedthrough"):
        notes.append("OONI/Bleedthrough join is instrument-level, not URL corroboration")
    lake = observation.get("common_crawl") if isinstance(observation.get("common_crawl"), dict) else {}
    if links.get("common_crawl") or lake:
        if (lake.get("match_kind") or "") == "host":
            notes.append(
                "Common Crawl join is host-level archive context, not URL corroboration"
            )
        else:
            notes.append("Common Crawl join is archive coverage, not a deletion claim")
    if not archive.get("ghostarchive_lookup") and url.startswith("https://"):
        notes.append("Ghostarchive lookup was not attached")
    return notes[:MAX_UNCERTAINTY]


def apply_uncertainty(observation: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(observation)
    row["uncertainty"] = uncertainty_notes(row)
    return row


def capture_provenance(
    *,
    collector: str,
    method: str,
    vantage: str = "outside-china-public-source",
    fetched_at: datetime | str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "collector": public_text(collector, limit=80),
        "method": public_text(method, limit=240),
        "vantage": public_text(vantage, limit=80),
        "fetched_at": iso_z(fetched_at) or iso_z(utc_now()),
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
    }
    if extra:
        for key, value in extra.items():
            if key in row:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                row[key] = value
    return row


def enrich_observation(
    observation: Mapping[str, Any],
    *,
    text: str | None = None,
    source_url: str | None = None,
    mirror_urls: Iterable[str] | None = None,
    first_seen: datetime | str | None = None,
    last_seen: datetime | str | None = None,
    last_confirmed_alive: datetime | str | None = None,
    confirmations: Sequence[Mapping[str, Any]] | None = None,
    last_live_ts: str | None = None,
    post_event_ts: str | None = None,
    last_live_snapshot: str | None = None,
    post_event_snapshot: str | None = None,
    cdt: Mapping[str, Any] | None = None,
    gdelt: Mapping[str, Any] | None = None,
    ooni: Mapping[str, Any] | None = None,
    greatfire: Mapping[str, Any] | None = None,
    weibo: Mapping[str, Any] | None = None,
    undertext: Mapping[str, Any] | None = None,
    bleedthrough: Mapping[str, Any] | None = None,
    common_crawl: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of ``observation`` with additive richness. Never drops existing keys."""

    row = dict(observation)
    title = public_text(row.get("title"), limit=1000)
    body = public_text(text if text is not None else row.get("text"), limit=MAX_PUBLIC_TEXT)
    url = public_text(row.get("url") or source_url, limit=2048)
    if title:
        row["title"] = title
    row["text"] = body
    bilingual = bilingual_fields(title, body)
    row["text_zh"] = bilingual["text_zh"]
    row["text_en"] = bilingual["text_en"]
    row["language"] = language_tag(text_zh=row["text_zh"], text_en=row["text_en"])
    if url:
        row["url"] = url
    row["source_url"] = public_text(source_url or url, limit=2048)
    mirrors: list[str] = []
    seen: set[str] = set()
    for candidate in list(mirror_urls or []) + list(row.get("mirror_urls") or []):
        item = public_text(candidate, limit=2048)
        if not item.startswith("https://") or item in seen:
            continue
        seen.add(item)
        mirrors.append(item)
        if len(mirrors) >= MAX_MIRRORS:
            break
    row["mirror_urls"] = mirrors
    row["content_sha256"] = content_sha256(title, body, url)
    row["gazetteer_hits"] = gazetteer_hits(title, body, " ".join(row.get("terms") or []))
    detected = iso_z(row.get("detected_at") or row.get("published_at"))
    row.update(sighting_fields(
        first_seen=first_seen or row.get("first_seen") or detected,
        last_seen=last_seen or row.get("last_seen") or detected,
        last_confirmed_alive=last_confirmed_alive or row.get("last_confirmed_alive"),
        confirmations=confirmations if confirmations is not None else row.get("deletion_confirmation"),
    ))
    row["archive"] = archive_witness(
        url,
        last_live_ts=last_live_ts,
        post_event_ts=post_event_ts,
        last_live_snapshot=last_live_snapshot or row.get("last_live_snapshot"),
        post_event_snapshot=post_event_snapshot or row.get("post_event_snapshot"),
    )
    row["cross_links"] = merge_cross_links(
        row.get("cross_links") if isinstance(row.get("cross_links"), dict) else None,
        cross_links(
            cdt=cdt,
            gdelt=gdelt,
            ooni=ooni,
            greatfire=greatfire,
            weibo=weibo,
            undertext=undertext,
            bleedthrough=bleedthrough,
            common_crawl=common_crawl,
        ),
    )
    if provenance:
        row["provenance"] = dict(provenance)
    elif not isinstance(row.get("provenance"), dict):
        row["provenance"] = capture_provenance(
            collector=str(row.get("source") or "china-observation"),
            method="public-source observation enrichment",
            fetched_at=detected,
        )
    return apply_uncertainty(row)


def serialize_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """JSON-safe copy: datetimes become Z timestamps."""

    out: dict[str, Any] = {}
    for key, value in observation.items():
        if isinstance(value, datetime):
            out[key] = iso_z(value)
        else:
            out[key] = value
    if isinstance(out.get("detected_at"), datetime) or hasattr(observation.get("detected_at"), "isoformat"):
        out["detected_at"] = iso_z(observation.get("detected_at"))
    return out


def situation_osint_row(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Closed, bounded projection for the China Situation desk. Not corroboration."""

    archive = observation.get("archive") if isinstance(observation.get("archive"), dict) else {}
    hits = []
    for hit in observation.get("gazetteer_hits") or []:
        if not isinstance(hit, dict):
            continue
        zh = public_text(hit.get("zh"), limit=80)
        if not zh:
            continue
        hits.append({"zh": zh, "en": public_text(hit.get("en"), limit=160)})
        if len(hits) >= 8:
            break
    url = public_text(observation.get("url") or observation.get("source_url"), limit=2048)
    if url.startswith("https://"):
        url = url.split("#", 1)[0]
    links = observation.get("cross_links") if isinstance(observation.get("cross_links"), dict) else {}
    compact: dict[str, Any] = {}
    for key in (
        "cdt",
        "gdelt",
        "ooni",
        "greatfire",
        "weibo",
        "undertext",
        "bleedthrough",
        "common_crawl",
    ):
        item = links.get(key)
        if not isinstance(item, dict) or not item:
            compact[key] = None
            continue
        compact[key] = {
            "id": public_text(item.get("id"), limit=80) or None,
            "url": public_text(item.get("url"), limit=2048) or None,
            "note": public_text(item.get("note"), limit=240) or None,
        }
    if compact.get("common_crawl"):
        compact["common_crawl"]["url"] = None
    lake = observation.get("common_crawl") if isinstance(observation.get("common_crawl"), dict) else {}
    match_kind = public_text(lake.get("match_kind"), limit=16)
    if match_kind not in {"url", "host", "digest"}:
        match_kind = ""
    uncertainty = []
    for note in observation.get("uncertainty") or []:
        item = public_text(note, limit=240)
        if item and item not in uncertainty:
            uncertainty.append(item)
        if len(uncertainty) >= 8:
            break
    confirmations = observation.get("deletion_confirmation")
    confirmation_count = (
        len(confirmations) if isinstance(confirmations, list) else 0
    )
    language = public_text(observation.get("language"), limit=16)
    if language not in LANGUAGE_TAGS:
        language = language_tag(
            text_zh=public_text(observation.get("text_zh"), limit=800),
            text_en=public_text(observation.get("text_en"), limit=800),
        )
    return {
        "observation_key": observation_key(observation),
        "source": public_text(observation.get("source"), limit=80) or "unknown",
        "title": public_text(observation.get("title"), limit=240) or "(untitled public record)",
        "url": url if url.startswith("https://") else "",
        "text": public_text(observation.get("text"), limit=2000).replace("\n", " ").replace("\r", " "),
        "language": language,
        "uncertainty": uncertainty,
        "deletion_signal": public_text(observation.get("deletion_signal"), limit=80),
        "confirmation_count": confirmation_count,
        "first_seen": iso_z(observation.get("first_seen")),
        "last_seen": iso_z(observation.get("last_seen")),
        "last_confirmed_alive": iso_z(observation.get("last_confirmed_alive")),
        "content_sha256": (
            observation.get("content_sha256")
            if isinstance(observation.get("content_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", observation.get("content_sha256") or "")
            else None
        ),
        "gazetteer_hits": hits,
        "archive": {
            "wayback_lookup": archive.get("wayback_lookup"),
            "wayback_snapshot": archive.get("wayback_snapshot"),
            "archive_today_lookup": archive.get("archive_today_lookup"),
            "ghostarchive_lookup": archive.get("ghostarchive_lookup"),
            "post_event_snapshot": archive.get("post_event_snapshot"),
            "bracket_before": (archive.get("timestamp_bracket") or {}).get("last_live"),
            "bracket_after": (archive.get("timestamp_bracket") or {}).get("post_event"),
        },
        "cross_links": compact,
        "common_crawl_match_kind": match_kind or None,
        "common_crawl_host": public_text(lake.get("host"), limit=253) or None,
        "common_crawl_capture_at": iso_z(lake.get("capture_at")),
        "relation": "topic-or-url-context-not-corroboration",
    }
