"""Honest cross-signal joins for China Palimpsest reconstructions.

A journalist-facing record is unique because it *joins* already-public
signals that live in this repository: DDTI/CDT titles, Weibo permitted
attention, GDELT global coverage, OONI GFW index, Bleedthrough injector
telemetry, UNDERTEXT fusion. Absence stays null. Instrument joins are
labeled as instrument-context, never as URL corroboration.

Nothing here fetches the network or invents a related record.
"""

from __future__ import annotations

from typing import Any, Mapping

from core.china_observation import (
    apply_uncertainty,
    iso_z,
    merge_cross_links,
    observation_key,
    public_text,
)


INSTRUMENT_NOTE = "instrument-context-not-url-corroboration"
_LAKE_MATCH_RANK = {"url": 3, "digest": 2, "host": 1}


def _prefer_lake_match(left: Any, right: Any) -> dict[str, Any] | None:
    left_row = left if isinstance(left, Mapping) and left else None
    right_row = right if isinstance(right, Mapping) and right else None
    if right_row and _LAKE_MATCH_RANK.get(str(right_row.get("match_kind") or ""), 0) > _LAKE_MATCH_RANK.get(
        str((left_row or {}).get("match_kind") or ""), 0
    ):
        return dict(right_row)
    if left_row:
        return dict(left_row)
    if right_row:
        return dict(right_row)
    return None
CDT_HOST_MARKERS = ("//chinadigitaltimes.net/", "//www.chinadigitaltimes.net/")
GREATFIRE_HOST_MARKERS = ("//en.greatfire.org/", "//greatfire.org/")


def _https(value: Any) -> str:
    text = public_text(value, limit=2048)
    return text if text.startswith("https://") else ""


def instrument_ooni(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping) or not payload:
        return None
    index = payload.get("gfw_index")
    generated = iso_z(payload.get("generated_at"))
    if index is None and not generated:
        return None
    return {
        "id": "ooni-gfw",
        "url": "https://palimpsest.info/readings/ooni-gfw-latest.json",
        "title": f"OONI GFW index {index}" if index is not None else "OONI GFW index",
        "note": (
            f"{INSTRUMENT_NOTE}; GFW anomaly index {index} from "
            f"{generated or 'unknown clock'}; not a claim about this URL"
        ),
    }


def instrument_bleedthrough(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping) or not payload:
        return None
    generated = iso_z(payload.get("generated_at"))
    if not generated and "events" not in payload and "method" not in payload:
        return None
    pools = payload.get("distinct_pools")
    return {
        "id": "bleedthrough",
        "url": "https://palimpsest.info/readings/bleedthrough-latest.json",
        "title": (
            f"Bleedthrough injector floor, {pools} distinct pools"
            if pools is not None
            else "Bleedthrough injector telemetry"
        ),
        "note": (
            f"{INSTRUMENT_NOTE}; last reading {generated or 'unknown clock'}; "
            "forged-IP pool rotations, not a deletion of this post"
        ),
    }


def gdelt_index(payload: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Map lowercased GDELT term -> related-link. Only committed ranked rows."""

    out: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, Mapping):
        return out
    generated = iso_z(payload.get("generated_at"))
    for row in payload.get("ranked") or []:
        if not isinstance(row, dict):
            continue
        term = public_text(row.get("term"), limit=80)
        if len(term) < 2:
            continue
        label = public_text(row.get("label"), limit=40) or "unknown"
        note = (
            f"GDELT DOC 2.0 overlap on committed term {term!r}; "
            f"label={label}; global_norm={row.get('global_norm')}; "
            f"clock {generated or 'unknown'}. Not a deletion claim."
        )
        out[term.casefold()] = {
            "id": f"gdelt:{term}",
            "url": "https://palimpsest.info/readings/gdelt-latest.json",
            "title": term,
            "note": note,
        }
    return out


def weibo_index(payload: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Map Weibo board term -> related-link from join / breakthrough / withdrawal."""

    out: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, Mapping):
        return out
    generated = iso_z(payload.get("generated_at"))

    def add(term: str, title: str, note: str) -> None:
        key = term.casefold()
        if key in out or len(term) < 2:
            return
        out[key] = {
            "id": f"weibo:{term}",
            "url": "https://palimpsest.info/readings/weibo-hotsearch-latest.json",
            "title": title or term,
            "note": note,
        }

    for row in payload.get("join") or []:
        if not isinstance(row, dict):
            continue
        term = public_text(row.get("term"), limit=80)
        regime = public_text(row.get("regime"), limit=40)
        add(
            term,
            term,
            f"Weibo hot-search join regime={regime or 'unclassified'}; "
            f"clock {generated or 'unknown'}. Permitted-attention board, not a private graph.",
        )
    for row in payload.get("gazetteer_breakthroughs") or []:
        if not isinstance(row, dict):
            continue
        term = public_text(row.get("term"), limit=80)
        sample = ((row.get("samples") or [{}])[0] or {})
        title = public_text(sample.get("title"), limit=240) or term
        add(
            term,
            title,
            f"Weibo gazetteer breakthrough on permitted board; "
            f"clock {generated or 'unknown'}. Not a deletion claim.",
        )
    watch = payload.get("withdrawal_watch") if isinstance(payload.get("withdrawal_watch"), dict) else {}
    for row in watch.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        title = public_text(row.get("title"), limit=240)
        for term in row.get("matched_terms") or []:
            add(
                public_text(term, limit=80),
                title,
                f"Weibo withdrawal-watch candidate; clock {generated or 'unknown'}. "
                "Pooled persist-rate baseline; not proof of a takedown.",
            )
    return out


def _cdt_link(url: str, title: str) -> dict[str, Any] | None:
    if not any(marker in url for marker in CDT_HOST_MARKERS):
        return None
    return {
        "id": "cdt-public-article",
        "url": url,
        "title": title,
        "note": "China Digital Times public article URL already on this record; not a private feed",
    }


def _greatfire_link(url: str, title: str) -> dict[str, Any] | None:
    if not any(marker in url for marker in GREATFIRE_HOST_MARKERS):
        return None
    return {
        "id": "greatfire-public",
        "url": url,
        "title": title,
        "note": "GreatFire public URL already on this record",
    }


def attach_joins(
    observation: Mapping[str, Any],
    *,
    gdelt: Mapping[str, dict[str, Any]] | None = None,
    weibo: Mapping[str, dict[str, Any]] | None = None,
    ooni: Mapping[str, Any] | None = None,
    bleedthrough: Mapping[str, Any] | None = None,
    undertext: Mapping[str, Any] | None = None,
    common_crawl: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach real related records. Recomputes uncertainty after the join."""

    row = dict(observation)
    url = _https(row.get("url") or row.get("source_url"))
    title = public_text(row.get("title"), limit=240)
    terms = [public_text(term, limit=80) for term in (row.get("terms") or []) if term]
    gdelt_hit = None
    for term in terms:
        gdelt_hit = (gdelt or {}).get(term.casefold())
        if gdelt_hit:
            break
    weibo_hit = None
    for term in terms:
        weibo_hit = (weibo or {}).get(term.casefold())
        if weibo_hit:
            break
    incoming = {
        "cdt": _cdt_link(url, title),
        "greatfire": _greatfire_link(url, title),
        "gdelt": gdelt_hit,
        "weibo": weibo_hit,
        "ooni": ooni,
        "bleedthrough": bleedthrough,
        "undertext": undertext,
        "common_crawl": common_crawl,
    }
    row["cross_links"] = merge_cross_links(row.get("cross_links"), incoming)
    return apply_uncertainty(row)


def earlier_stamp(*values: Any) -> str | None:
    stamps = [iso_z(value) for value in values]
    present = [stamp for stamp in stamps if stamp]
    return min(present) if present else None


def later_stamp(*values: Any) -> str | None:
    stamps = [iso_z(value) for value in values]
    present = [stamp for stamp in stamps if stamp]
    return max(present) if present else None


def merge_observations(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Collapse two observations that already share a public URL into one fat record."""

    from core.china_observation import (
        bilingual_fields,
        content_sha256,
        enrich_observation,
        gazetteer_hits,
        language_tag,
    )

    keep = dict(left)
    other = dict(right)
    title_keep = public_text(keep.get("title"), limit=1000)
    title_other = public_text(other.get("title"), limit=1000)
    if title_other.startswith("[") and not title_keep.startswith("["):
        title = title_keep
    elif title_keep.startswith("[") and not title_other.startswith("["):
        title = title_other
    else:
        title = title_keep if len(title_keep) >= len(title_other) else title_other
    texts: list[str] = []
    for blob in (
        keep.get("text"),
        other.get("text"),
        title_keep,
        title_other,
    ):
        line = public_text(blob, limit=4000)
        if line and line not in texts:
            texts.append(line)
    body = public_text("\n".join(texts), limit=8000)
    terms: list[str] = []
    for source in (keep.get("terms") or []), (other.get("terms") or []):
        for term in source:
            item = public_text(term, limit=80)
            if item and item not in terms:
                terms.append(item)
    mirrors: list[str] = []
    for candidate in list(keep.get("mirror_urls") or []) + list(other.get("mirror_urls") or []):
        item = _https(candidate)
        if item and item not in mirrors:
            mirrors.append(item)
    confirmations = []
    seen_conf: set[tuple[str, str, str]] = set()
    for item in list(keep.get("deletion_confirmation") or []) + list(
        other.get("deletion_confirmation") or []
    ):
        if not isinstance(item, dict):
            continue
        key = (
            public_text(item.get("status"), limit=80),
            iso_z(item.get("observed_at")) or "",
            public_text(item.get("source"), limit=120),
        )
        if key in seen_conf:
            continue
        seen_conf.add(key)
        confirmations.append(item)
    signal_keep = public_text(keep.get("deletion_signal"), limit=80)
    signal_other = public_text(other.get("deletion_signal"), limit=80)
    priority = {
        "deletion": 5,
        "mutation": 4,
        "suppressed_invisible": 3,
        "unreachable": 2,
        "no_baseline": 1,
    }
    if priority.get(signal_other, 0) > priority.get(signal_keep, 0):
        deletion_signal = signal_other
    else:
        deletion_signal = signal_keep or signal_other
    url = _https(keep.get("url") or other.get("url") or keep.get("source_url") or other.get("source_url"))
    bilingual = bilingual_fields(title, body)
    merged = {
        **keep,
        "title": title or title_keep or title_other,
        "text": body,
        "text_zh": bilingual["text_zh"],
        "text_en": bilingual["text_en"],
        "language": language_tag(text_zh=bilingual["text_zh"], text_en=bilingual["text_en"]),
        "terms": terms,
        "url": url,
        "source_url": url,
        "mirror_urls": mirrors,
        "deletion_signal": deletion_signal,
        "first_seen": earlier_stamp(keep.get("first_seen"), other.get("first_seen")),
        "last_seen": later_stamp(keep.get("last_seen"), other.get("last_seen")),
        "last_confirmed_alive": later_stamp(
            keep.get("last_confirmed_alive"), other.get("last_confirmed_alive")
        ),
        "deletion_confirmation": confirmations[:12],
        "content_sha256": content_sha256(title, body, url),
        "gazetteer_hits": gazetteer_hits(title, body, " ".join(terms)),
        "cross_links": merge_cross_links(keep.get("cross_links"), other.get("cross_links")),
        "source": public_text(keep.get("source"), limit=120) or public_text(other.get("source"), limit=120),
        "common_crawl": _prefer_lake_match(keep.get("common_crawl"), other.get("common_crawl")),
    }
    archive_keep = keep.get("archive") if isinstance(keep.get("archive"), dict) else {}
    archive_other = other.get("archive") if isinstance(other.get("archive"), dict) else {}
    if archive_other.get("wayback_snapshot") and not archive_keep.get("wayback_snapshot"):
        merged["archive"] = dict(archive_keep)
        merged["archive"].update({
            key: archive_other[key]
            for key in archive_other
            if archive_other.get(key) and not archive_keep.get(key)
        })
    return apply_uncertainty(enrich_observation(
        merged,
        text=merged["text"],
        source_url=url,
        mirror_urls=mirrors,
        first_seen=merged["first_seen"],
        last_seen=merged["last_seen"],
        last_confirmed_alive=merged["last_confirmed_alive"],
        confirmations=confirmations,
        last_live_snapshot=(merged.get("archive") or {}).get("wayback_snapshot"),
        post_event_snapshot=(merged.get("archive") or {}).get("post_event_snapshot"),
        provenance=keep.get("provenance") if isinstance(keep.get("provenance"), dict) else other.get("provenance"),
    ))


def cluster_by_url(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One Palimpsest reconstruction per public HTTPS URL; term-only rows stay separate."""

    clustered: dict[str, dict[str, Any]] = {}
    leftovers: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        url = _https(row.get("url") or row.get("source_url"))
        if not url:
            leftovers.append(row)
            continue
        if url in clustered:
            clustered[url] = merge_observations(clustered[url], row)
        else:
            clustered[url] = row
    return leftovers + list(clustered.values())


def common_crawl_related_link(match: Mapping[str, Any]) -> dict[str, Any]:
    """Compact related-link. Never carries a lake URL or WARC fetch coordinate."""

    ident = (
        public_text(match.get("locator_sha256"), limit=80)
        or (
            f"host:{public_text(match.get('host'), limit=64)}"
            if match.get("host")
            else public_text(match.get("target_id"), limit=64)
        )
        or "common-crawl-lake"
    )
    parts = [
        public_text(match.get("relation"), limit=80) or "archive-coverage-not-deletion",
        f"match_kind={public_text(match.get('match_kind'), limit=16)}",
    ]
    if match.get("host"):
        parts.append(f"host={public_text(match.get('host'), limit=253)}")
    if match.get("crawl"):
        parts.append(f"crawl={public_text(match.get('crawl'), limit=32)}")
    if match.get("capture_at"):
        parts.append(f"capture_at={iso_z(match.get('capture_at')) or public_text(match.get('capture_at'), limit=32)}")
    if match.get("mime_type"):
        parts.append(f"mime={public_text(match.get('mime_type'), limit=64)}")
    if match.get("languages"):
        parts.append(f"languages={public_text(match.get('languages'), limit=64)}")
    if match.get("content_digest"):
        parts.append(f"digest={public_text(match.get('content_digest'), limit=40)}")
    return {
        "id": ident[:80],
        "url": None,
        "title": f"Common Crawl {public_text(match.get('match_kind'), limit=16) or 'lake'} match",
        "note": "; ".join(part for part in parts if part),
    }


def _match_from_receipt(
    observation: Mapping[str, Any], receipt: Mapping[str, Any]
) -> dict[str, Any] | None:
    from collectors.common_crawl_lake import public_match_fields, public_url_identity

    key = observation_key(observation)
    identity = public_url_identity(observation.get("url") or observation.get("source_url"))
    url_sha = identity[0] if identity else None
    for item in receipt.get("matches") or []:
        if not isinstance(item, Mapping):
            continue
        if key and item.get("observation_key") == key:
            return public_match_fields(item)
        if url_sha and item.get("url_sha256") == url_sha:
            return public_match_fields(item)
    return None


def attach_common_crawl_join(
    observation: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any] | None = None,
    connection=None,
    config=None,
) -> dict[str, Any]:
    """Attach a sanitized lake match. Missing/empty lake leaves the join null."""

    from collectors.common_crawl_lake import SANITIZED_MATCH_KEYS, match_observation

    match = None
    if isinstance(receipt, Mapping) and receipt.get("status") == "ok":
        match = _match_from_receipt(observation, receipt)
    elif connection is not None:
        match = match_observation(connection, observation, config)
    row = dict(observation)
    if not match:
        row["common_crawl"] = None
        row["cross_links"] = merge_cross_links(row.get("cross_links"), {"common_crawl": None})
        return apply_uncertainty(row)
    row["common_crawl"] = {key: match.get(key) for key in SANITIZED_MATCH_KEYS}
    row["cross_links"] = merge_cross_links(
        row.get("cross_links"),
        {"common_crawl": common_crawl_related_link(match)},
    )
    return apply_uncertainty(row)
