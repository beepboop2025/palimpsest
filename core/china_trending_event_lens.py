"""Deterministic event-cluster readings for current Weibo hot-search trends.

The public hot-search board is a record of *permitted attention*.  This module
clusters numerical and editorial revisions of the current headlines, then reads
each cluster against three bounded instruments already present in the board
artifact: the state-pinned slot, exact-title withdrawal watch, and DDTI overlap.

The implementation is deliberately lexical and conservative.  It does not ask a
model to infer that two topics are the same event, translate English wire copy,
or turn missing evidence into a censorship finding.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "palimpsest.china-trending-event-lenses.v1"
BOARD_SCHEMA_VERSION = "palimpsest-weibo-hotsearch-terms.v1"
NEWSWIRE_SCHEMA_VERSION = "palimpsest-newswire.v1"
BOARD_SOURCE_URL = (
    "https://www.palimpsest.info/readings/"
    "weibo-hotsearch-terms-latest.json"
)
NEWSWIRE_SOURCE_URL = "https://www.palimpsest.info/readings/newswire-latest.json"
BOARD_FRESH_FOR_SECONDS = 6 * 60 * 60
DDTI_FRESH_FOR_SECONDS = 6 * 60 * 60
NEWSWIRE_FRESH_FOR_SECONDS = 12 * 60 * 60
DEFAULT_MAX_EVENTS = 24
MAX_EVENTS = 40
MATCH_THRESHOLD = 0.58

_ARABIC_NUMBER = re.compile(r"[0-9０-９]+(?:[.,，．][0-9０-９]+)*%?")
_CJK_COUNT = re.compile(
    r"[零〇一二两三四五六七八九十百千万亿]+"
    r"(?=(?:人|名|位|例|岁|年|月|日|时|分|秒|个|家|所|次|起|架|艘|辆|米|公里|元))"
)
_HEADLINE_CHAR = re.compile(r"[^a-z0-9#\u3400-\u9fff]+")
_HAN = re.compile(r"[\u3400-\u9fff]")
_UPDATE_MARKERS = (
    "已致",
    "导致",
    "造成",
    "遇难",
    "失联",
    "受伤",
    "获救",
    "确认",
    "新增",
    "升至",
    "超过",
    "最新",
    "回应",
    "通报",
    "公布",
    "发现",
    "救援",
    "搜救",
    "进展",
    "现场",
    "持续",
    "仍有",
    "人数",
)


def _bounded_text(value: Any, limit: int = 240) -> str:
    if type(value) is not str:
        return ""
    return " ".join(value.split())[:limit]


def _utc(value: Any) -> datetime | None:
    text = _bounded_text(value, 64)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date(value: Any) -> str:
    text = _bounded_text(value, 10)
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return ""
    return text


def _rank(value: Any) -> int | None:
    return value if type(value) is int and 1 <= value <= 100 else None


def headline_key(value: Any) -> str:
    """Return a punctuation-free key with volatile counts collapsed.

    Chinese numerals are collapsed only when they behave as counts (for
    example, before 人 or 例).  That keeps named dates such as 五四 from being
    silently equated with unrelated numbered events.
    """

    text = unicodedata.normalize("NFKC", _bounded_text(value, 180)).casefold()
    text = _ARABIC_NUMBER.sub("#", text)
    text = _CJK_COUNT.sub("#", text)
    text = _HEADLINE_CHAR.sub("", text)
    return re.sub(r"#+", "#", text)[:180]


def _trigrams(value: str) -> set[str]:
    if len(value) < 3:
        return {value} if value else set()
    return {value[index : index + 3] for index in range(len(value) - 2)}


def headline_similarity(left: Any, right: Any) -> float:
    """Conservative, deterministic similarity for headline revisions."""

    a = headline_key(left)
    b = headline_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    shorter, longer = sorted((a, b), key=lambda value: (len(value), value))
    if shorter in longer and len(shorter) >= 5:
        ratio = len(shorter) / len(longer)
        addition = longer.replace(shorter, "", 1)
        if ratio >= 0.72 or any(marker in addition for marker in _UPDATE_MARKERS):
            return 0.9

    left_grams = _trigrams(a)
    right_grams = _trigrams(b)
    shared = left_grams & right_grams
    if len(shared) < 2:
        return 0.0
    return (2.0 * len(shared)) / (len(left_grams) + len(right_grams))


def _same_event(left: Any, right: Any) -> bool:
    return headline_similarity(left, right) >= MATCH_THRESHOLD


def _term_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in document.get("terms") or []:
        if not isinstance(raw, Mapping):
            continue
        title = _bounded_text(raw.get("title"), 180)
        first_seen = _date(raw.get("first_seen"))
        last_seen = _date(raw.get("last_seen"))
        if not title or not first_seen or not last_seen or first_seen > last_seen:
            continue
        days_present = raw.get("days_present")
        appearances = raw.get("appearances")
        rows.append(
            {
                "title": title,
                "key": headline_key(title),
                "best_rank": _rank(raw.get("best_rank")),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "days_present": (
                    days_present
                    if type(days_present) is int and days_present > 0
                    else appearances
                    if type(appearances) is int and appearances > 0
                    else 1
                ),
                "pinned": bool(raw.get("pinned")),
            }
        )
    rows.sort(
        key=lambda row: (
            row["best_rank"] is None,
            row["best_rank"] or 101,
            row["title"],
        )
    )
    return rows


def _cluster_rows(
    rows: Sequence[dict[str, Any]], latest_day: str
) -> list[dict[str, Any]]:
    """Cluster around current seeds without transitive historical bridges."""

    current = [row for row in rows if row["last_seen"] == latest_day]
    historical = [row for row in rows if row["last_seen"] != latest_day]
    clusters: list[dict[str, Any]] = []

    for row in current:
        scored = [
            (
                max(
                    headline_similarity(row["title"], item["title"])
                    for item in cluster["current"]
                ),
                index,
            )
            for index, cluster in enumerate(clusters)
        ]
        score, index = max(scored, default=(0.0, -1))
        if score >= MATCH_THRESHOLD:
            clusters[index]["current"].append(row)
            clusters[index]["rows"].append(row)
        else:
            clusters.append({"current": [row], "rows": [row]})

    for row in historical:
        scored = [
            (
                max(
                    headline_similarity(row["title"], item["title"])
                    for item in cluster["current"]
                ),
                -index,
                index,
            )
            for index, cluster in enumerate(clusters)
        ]
        score, _negative_index, index = max(scored, default=(0.0, 0, -1))
        if score >= MATCH_THRESHOLD:
            clusters[index]["rows"].append(row)

    for cluster in clusters:
        cluster["current"].sort(
            key=lambda row: (
                row["best_rank"] is None,
                row["best_rank"] or 101,
                row["title"],
            )
        )
        cluster["rows"].sort(
            key=lambda row: (
                row["last_seen"] != latest_day,
                row["best_rank"] is None,
                row["best_rank"] or 101,
                row["title"],
            )
        )
        keys = sorted({row["key"] for row in cluster["rows"] if row["key"]})
        anchor = min(keys, key=lambda key: (len(key), key))
        cluster["anchor"] = anchor
        cluster["trend_id"] = "trend-" + hashlib.sha256(
            anchor.encode("utf-8")
        ).hexdigest()[:20]

    clusters.sort(
        key=lambda cluster: (
            not any(row["pinned"] for row in cluster["current"]),
            min(
                (
                    row["best_rank"]
                    for row in cluster["current"]
                    if row["best_rank"] is not None
                ),
                default=101,
            ),
            -len(cluster["rows"]),
            cluster["current"][0]["title"],
        )
    )
    return clusters


def _matches_cluster(title: Any, cluster: Mapping[str, Any]) -> bool:
    text = _bounded_text(title, 180)
    return bool(text) and any(
        _same_event(text, row["title"]) for row in cluster.get("rows") or []
    )


def _pinned_read(
    document: Mapping[str, Any], cluster: Mapping[str, Any]
) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    for raw_day in document.get("pinned_headlines") or []:
        if not isinstance(raw_day, Mapping):
            continue
        day = _date(raw_day.get("date"))
        if not day:
            continue
        for raw_title in raw_day.get("pinned") or []:
            title = _bounded_text(raw_title, 180)
            if _matches_cluster(title, cluster):
                items.append({"date": day, "title": title})
    items.sort(key=lambda row: (row["date"], row["title"]))
    days = sorted({row["date"] for row in items})
    return {
        "matches": len(items),
        "days": days,
        "items": items[-8:],
        "match_method": "deterministic_headline_similarity",
    }


def _withdrawal_read(
    document: Mapping[str, Any], cluster: Mapping[str, Any]
) -> dict[str, Any]:
    watch = document.get("withdrawal_watch")
    watch = watch if isinstance(watch, Mapping) else {}
    items: list[dict[str, Any]] = []

    for raw in watch.get("candidates") or []:
        if not isinstance(raw, Mapping):
            continue
        title = _bounded_text(raw.get("title"), 180)
        day = _date(raw.get("date"))
        if not day or not _matches_cluster(title, cluster):
            continue
        continued = any(row["last_seen"] > day for row in cluster.get("rows") or [])
        items.append(
            {
                "title": title,
                "date": day,
                "best_rank": _rank(raw.get("best_rank")),
                "raw_state": "candidate",
                "event_state": (
                    "event_continued_after_headline"
                    if continued
                    else "unconfirmed_exit"
                ),
                "matched_terms": [
                    _bounded_text(term, 40)
                    for term in (raw.get("matched_terms") or [])
                    if _bounded_text(term, 40)
                ],
            }
        )

    for raw in watch.get("sense_filtered") or []:
        if not isinstance(raw, Mapping):
            continue
        title = _bounded_text(raw.get("title"), 180)
        day = _date(raw.get("date"))
        if not day or not _matches_cluster(title, cluster):
            continue
        items.append(
            {
                "title": title,
                "date": day,
                "best_rank": _rank(raw.get("best_rank")),
                "raw_state": "sense_filtered",
                "event_state": "ordinary_sense_rejected",
                "matched_terms": [
                    _bounded_text(item.get("term"), 40)
                    for item in (raw.get("sense_filtered_terms") or [])
                    if isinstance(item, Mapping) and _bounded_text(item.get("term"), 40)
                ],
            }
        )

    items.sort(key=lambda row: (row["date"], row["title"]))
    return {
        "raw_flags": len(items),
        "resolved_by_later_attention": sum(
            row["event_state"] == "event_continued_after_headline" for row in items
        ),
        "ordinary_sense_rejections": sum(
            row["event_state"] == "ordinary_sense_rejected" for row in items
        ),
        "unresolved": sum(
            row["event_state"] == "unconfirmed_exit" for row in items
        ),
        "items": items[:12],
        "baseline_persist_rate": watch.get("baseline_persist_rate"),
    }


def _ddti_read(
    document: Mapping[str, Any],
    cluster: Mapping[str, Any],
    source_state: Mapping[str, Any],
) -> dict[str, Any]:
    matches: list[dict[str, str]] = []
    cluster_titles = [row["title"] for row in cluster.get("rows") or []]
    for raw in document.get("ddti_join") or []:
        if not isinstance(raw, Mapping):
            continue
        term = _bounded_text(raw.get("term"), 100)
        regime = _bounded_text(raw.get("regime"), 40)
        samples = [
            _bounded_text(sample.get("title"), 180)
            for sample in (raw.get("samples") or [])
            if isinstance(sample, Mapping)
            and _bounded_text(sample.get("title"), 180)
        ]
        direct = len(term) >= 2 and any(term in title for title in cluster_titles)
        sample_match = any(
            _matches_cluster(sample, cluster) for sample in samples
        )
        if (direct or sample_match) and term:
            matches.append({"term": term, "regime": regime or "unknown"})
    matches.sort(key=lambda row: (row["term"], row["regime"]))
    current_matches = len(matches) if source_state.get("state") == "fresh" else 0
    if matches and current_matches:
        state = "present"
        note = "A fresh DDTI deletion-pressure row overlaps this permitted-attention cluster."
    elif matches:
        state = f"present_{source_state.get('state') or 'unavailable'}"
        note = (
            "A DDTI row overlaps this cluster, but its source clock is not current; "
            "the overlap is retained as evidence and cannot set the current label."
        )
    else:
        state = source_state.get("state") or "unavailable"
        note = "No DDTI overlap is present in this board record."
    return {
        "matches": len(matches),
        "current_matches": current_matches,
        "state": state,
        "source_state": source_state.get("state"),
        "source_generated_at": source_state.get("source_generated_at"),
        "terms": matches[:12],
        "note": note,
    }


def _ddti_state(value: Any, now: datetime) -> dict[str, Any]:
    generated_text = _bounded_text(value, 64) or None
    generated = _utc(generated_text)
    age_seconds = int((now - generated).total_seconds()) if generated else None
    if generated is None:
        state = "unclocked"
        reason = (
            "The board carries DDTI join rows without a bound DDTI source clock; "
            "they cannot set a current assessment."
        )
    elif age_seconds is None or age_seconds < -300:
        state = "unavailable"
        reason = "The DDTI source clock is invalid."
    elif age_seconds > DDTI_FRESH_FOR_SECONDS:
        state = "stale"
        reason = "The DDTI source is stale and cannot set a current assessment."
    else:
        state = "fresh"
        reason = "The DDTI source clock is current."
    return {
        "state": state,
        "source_generated_at": generated_text,
        "age_seconds": max(0, age_seconds) if age_seconds is not None else None,
        "reason": reason,
    }


def _newswire_state(
    document: Mapping[str, Any] | None, now: datetime
) -> dict[str, Any]:
    payload = document if isinstance(document, Mapping) else {}
    generated_text = _bounded_text(payload.get("generated_at"), 64) or None
    generated = _utc(generated_text)
    age_seconds = int((now - generated).total_seconds()) if generated else None
    if payload.get("schema_version") != NEWSWIRE_SCHEMA_VERSION:
        state = "unavailable"
        reason = "No compatible newswire artifact was supplied."
    elif generated is None or age_seconds is None or age_seconds < -300:
        state = "unavailable"
        reason = "The newswire source clock is invalid."
    elif age_seconds > NEWSWIRE_FRESH_FOR_SECONDS:
        state = "stale"
        reason = "The newswire artifact is stale; it is not used as current context."
    elif not isinstance(payload.get("events"), list):
        state = "unavailable"
        reason = "The newswire event array is unavailable."
    else:
        state = "fresh"
        reason = (
            "Fresh newswire context is matched only when Chinese headline text "
            "clears the same deterministic similarity rule."
        )
    return {
        "state": state,
        "source_generated_at": generated_text,
        "age_seconds": max(0, age_seconds) if age_seconds is not None else None,
        "reason": reason,
    }


def _newswire_read(
    document: Mapping[str, Any] | None,
    cluster: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    if state.get("state") != "fresh" or not isinstance(document, Mapping):
        return {
            "state": state.get("state") or "unavailable",
            "matches": 0,
            "independent_publisher_groups": 0,
            "events": [],
            "note": state.get("reason"),
            "match_method": "chinese_headline_only",
        }

    events: list[dict[str, Any]] = []
    group_ids: set[str] = set()
    for raw in document.get("events") or []:
        if not isinstance(raw, Mapping):
            continue
        texts = [_bounded_text(raw.get("headline"), 240)]
        refs = [
            ref
            for ref in (raw.get("evidence_refs") or [])
            if isinstance(ref, Mapping)
        ]
        texts.extend(_bounded_text(ref.get("title"), 240) for ref in refs)
        chinese_texts = [text for text in texts if text and _HAN.search(text)]
        if not any(_matches_cluster(text, cluster) for text in chinese_texts):
            continue

        event_groups = {
            _bounded_text(group.get("group_id"), 100)
            for group in (raw.get("evidence_groups") or [])
            if isinstance(group, Mapping) and _bounded_text(group.get("group_id"), 100)
        }
        if not event_groups:
            event_groups = {
                _bounded_text(ref.get("independence_group"), 100)
                for ref in refs
                if _bounded_text(ref.get("independence_group"), 100)
            }
        group_ids.update(event_groups)
        events.append(
            {
                "event_id": _bounded_text(raw.get("event_id"), 100),
                "headline": _bounded_text(raw.get("headline"), 240),
                "url": _bounded_text(raw.get("url"), 500),
                "evidence_strength": _bounded_text(
                    raw.get("evidence_strength"), 40
                ),
                "independent_publisher_groups": len(event_groups),
            }
        )

    events.sort(key=lambda row: (row["headline"], row["event_id"]))
    return {
        "state": "present" if events else "none",
        "matches": len(events),
        "independent_publisher_groups": len(group_ids),
        "events": events[:4],
        "note": (
            "Matched independent-publisher context; it does not determine the censorship reading."
            if events
            else "No Chinese-headline newswire match cleared the deterministic threshold."
        ),
        "match_method": "chinese_headline_only",
    }


def _evidence_rows(
    cluster: Mapping[str, Any],
    pins: Mapping[str, Any],
    ddti: Mapping[str, Any],
    wire: Mapping[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in (cluster.get("rows") or [])[:5]:
        out.append(
            {
                "kind": "board_title",
                "date": row["last_seen"],
                "title": row["title"],
                "best_rank": row["best_rank"],
                "status": "permitted_attention",
                "source_url": BOARD_SOURCE_URL,
            }
        )
    for row in (pins.get("items") or [])[-2:]:
        out.append(
            {
                "kind": "pinned_headline",
                "date": row["date"],
                "title": row["title"],
                "best_rank": None,
                "status": "state_pinned_attention",
                "source_url": BOARD_SOURCE_URL,
            }
        )
    for row in (ddti.get("terms") or [])[:2]:
        out.append(
            {
                "kind": "ddti_overlap",
                "date": None,
                "title": row["term"],
                "best_rank": None,
                "status": f"{row['regime']}; source_{ddti.get('source_state')}",
                "source_url": BOARD_SOURCE_URL,
            }
        )
    for row in (wire.get("events") or [])[:2]:
        out.append(
            {
                "kind": "newswire_context",
                "date": None,
                "title": row["headline"],
                "best_rank": None,
                "status": row["evidence_strength"] or "context",
                "source_url": row["url"] or NEWSWIRE_SOURCE_URL,
            }
        )
    return out[:10]


def _assessment(
    cluster: Mapping[str, Any],
    latest_day: str,
    pins: Mapping[str, Any],
    withdrawal: Mapping[str, Any],
    ddti: Mapping[str, Any],
) -> dict[str, str]:
    rows = cluster.get("rows") or []
    ranks = [row["best_rank"] for row in rows if row["best_rank"] is not None]
    best_rank = min(ranks) if ranks else None
    variants = len(rows)
    title_days = sum(row["days_present"] for row in rows)

    if pins.get("matches"):
        code = "visible_state_pinned_framing"
        label = "VISIBLE · STATE-PINNED FRAMING"
        headline = "Visible attention with a linked pinned frame"
    elif ddti.get("current_matches"):
        code = "visible_ddti_overlap"
        label = "VISIBLE · DDTI OVERLAP"
        headline = "Permitted attention overlaps deletion pressure"
    elif withdrawal.get("unresolved"):
        code = "visible_withdrawal_watch_unconfirmed"
        label = "VISIBLE · WITHDRAWAL WATCH OPEN"
        headline = "Visible now; an exact-title exit still needs review"
    else:
        code = "visible_permitted_attention"
        label = "VISIBLE · PERMITTED ATTENTION"
        headline = "Visible on the curated attention surface"

    rank_sentence = (
        f" and a best observed rank of #{best_rank}" if best_rank is not None else ""
    )
    reading = (
        f"This cluster is present on the latest board day ({latest_day}) across "
        f"{variants} headline variant(s) and {title_days} title-day(s)"
        f"{rank_sentence}. "
    )
    if pins.get("matches"):
        reading += (
            f"A deterministically linked headline occupied the state-pinned slot on "
            f"{len(pins.get('days') or [])} day(s). "
        )
    if ddti.get("current_matches"):
        reading += (
            f"The cluster overlaps {ddti['current_matches']} fresh DDTI "
            "deletion-pressure row(s), "
            "which supports a containment review but does not establish cause or scale. "
        )
    if withdrawal.get("resolved_by_later_attention"):
        reading += (
            f"{withdrawal['resolved_by_later_attention']} exact-title exit flag(s) "
            "were followed by later cluster attention and are treated as revision churn. "
        )
    if withdrawal.get("ordinary_sense_rejections"):
        reading += (
            f"{withdrawal['ordinary_sense_rejections']} raw withdrawal trigger(s) "
            "were rejected by the source sense gate. "
        )
    if withdrawal.get("unresolved"):
        reading += (
            f"{withdrawal['unresolved']} exact-title exit remains unconfirmed; without "
            "a deletion trail or hourly dwell record it is not a takedown finding. "
        )
    reading += (
        "This proves only visibility on a curated permitted-attention surface; it does "
        "not prove uncensored discussion, neutral popularity, or the absence of broader censorship."
    )
    return {"code": code, "label": label, "headline": headline, "reading": reading}


def _limitations() -> list[str]:
    return [
        "The Weibo hot-search board is a curated permitted-attention surface, not neutral public volume.",
        "Automatic clusters use only bounded headline similarity; semantically related but lexically different headlines may remain separate.",
        "A one-day exact-title exit is not a takedown; event continuity and independent deletion evidence must be checked.",
        "Newswire matching is Chinese-headline-only and optional; an unmatched event is not evidence that independent reporting is absent.",
        "Casualty figures are retained only as unverified headline text and are not validated by this instrument.",
        "No post body, user graph, private account, or in-China observer is used.",
    ]


def _unavailable(
    *,
    evaluated_at: str,
    generated_at: str | None,
    window_start: str | None,
    window_end: str | None,
    freshness: str,
    age_seconds: int | None,
    reason: str,
    ddti: Mapping[str, Any],
    newswire: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "unavailable",
        "assessment": {
            "code": "unavailable",
            "label": "TREND LENSES · UNAVAILABLE",
            "headline": "No current trend-level censorship inference",
            "reading": reason,
        },
        "clocks": {
            "source_generated_at": generated_at,
            "evaluated_at": evaluated_at,
            "window_start": window_start,
            "window_end": window_end,
            "freshness": freshness,
            "age_seconds": age_seconds,
        },
        "selection": {
            "latest_day": window_end,
            "current_titles": None,
            "current_clusters": None,
            "published_clusters": 0,
            "max_events": DEFAULT_MAX_EVENTS,
            "truncated": False,
        },
        "ddti_context": dict(ddti),
        "newswire_context": dict(newswire),
        "events": [],
        "limitations": _limitations(),
    }


def build_trending_event_lenses(
    board_document: Mapping[str, Any] | None,
    newswire_document: Mapping[str, Any] | None = None,
    *,
    ddti_source_generated_at: str | None = None,
    evaluated_at: datetime | None = None,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> dict[str, Any]:
    """Build bounded analyses for current permitted-board headline clusters."""

    now = evaluated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    evaluated = _iso_z(now)
    payload = board_document if isinstance(board_document, Mapping) else {}
    generated_text = _bounded_text(payload.get("generated_at"), 64) or None
    generated = _utc(generated_text)
    raw_age = int((now - generated).total_seconds()) if generated else None
    age_seconds = max(0, raw_age) if raw_age is not None else None
    days = [
        day
        for day in (
            _date(value) for value in (payload.get("window_days") or [])
        )
        if day
    ]
    window_start = min(days) if days else None
    window_end = max(days) if days else None
    freshness = (
        "fresh"
        if raw_age is not None and -300 <= raw_age <= BOARD_FRESH_FOR_SECONDS
        else "stale"
        if raw_age is not None and raw_age > BOARD_FRESH_FOR_SECONDS
        else "unavailable"
    )
    ddti_state = _ddti_state(ddti_source_generated_at, now)
    wire_state = _newswire_state(newswire_document, now)

    reason = ""
    if payload.get("schema_version") != BOARD_SCHEMA_VERSION:
        reason = "The permitted-board artifact is missing or uses an unsupported schema."
    elif payload.get("status") != "live":
        reason = "The permitted-board collector abstained; no quiet-state reading is substituted."
    elif generated is None or raw_age is None or raw_age < -300:
        reason = "The permitted-board source clock is invalid."
    elif raw_age > BOARD_FRESH_FOR_SECONDS:
        reason = "The permitted-board artifact is stale, so current trend readings are withheld."
    elif not window_end:
        reason = "The permitted-board window is unavailable."

    rows = _term_rows(payload) if not reason else []
    if not reason and not rows:
        reason = "The permitted-board artifact contains no valid title rows."
    if not reason and not any(row["last_seen"] == window_end for row in rows):
        reason = "No title row reaches the latest board day; the source is internally incomplete."
    if reason:
        return _unavailable(
            evaluated_at=evaluated,
            generated_at=generated_text,
            window_start=window_start,
            window_end=window_end,
            freshness=freshness,
            age_seconds=age_seconds,
            reason=reason,
            ddti=ddti_state,
            newswire=wire_state,
        )

    limit = (
        max_events
        if type(max_events) is int and 1 <= max_events <= MAX_EVENTS
        else DEFAULT_MAX_EVENTS
    )
    clusters = _cluster_rows(rows, window_end)
    candidates: list[dict[str, Any]] = []
    for cluster in clusters:
        current = cluster["current"]
        all_rows = cluster["rows"]
        canonical = current[0]
        pins = _pinned_read(payload, cluster)
        withdrawal = _withdrawal_read(payload, cluster)
        ddti = _ddti_read(payload, cluster, ddti_state)
        wire = _newswire_read(newswire_document, cluster, wire_state)
        ranks = [row["best_rank"] for row in all_rows if row["best_rank"] is not None]
        candidates.append(
            {
                "trend_id": cluster["trend_id"],
                "canonical_headline": canonical["title"],
                "finding_state": "bounded_observation",
                "assessment": _assessment(
                    cluster, window_end, pins, withdrawal, ddti
                ),
                "clocks": {
                    "source_generated_at": generated_text,
                    "evaluated_at": evaluated,
                    "window_start": window_start,
                    "window_end": window_end,
                    "freshness": freshness,
                    "age_seconds": age_seconds,
                },
                "attention": {
                    "visible_on_latest_day": True,
                    "latest_day_headlines": len(current),
                    "distinct_headlines": len(all_rows),
                    "title_days": sum(row["days_present"] for row in all_rows),
                    "first_seen": min(row["first_seen"] for row in all_rows),
                    "last_seen": max(row["last_seen"] for row in all_rows),
                    "best_rank": min(ranks) if ranks else None,
                    "current_headlines": [
                        {
                            "title": row["title"],
                            "best_rank": row["best_rank"],
                            "last_seen": row["last_seen"],
                        }
                        for row in current[:5]
                    ],
                    "state_pins": pins,
                },
                "withdrawal_watch": withdrawal,
                "ddti_corroboration": ddti,
                "newswire_context": wire,
                "evidence": _evidence_rows(cluster, pins, ddti, wire),
                "limitations": _limitations(),
            }
        )

    def event_order(event: Mapping[str, Any]) -> tuple[Any, ...]:
        attention = event["attention"]
        pins = attention["state_pins"]
        ddti = event["ddti_corroboration"]
        withdrawal = event["withdrawal_watch"]
        if pins["matches"]:
            signal_tier = 0
        elif ddti["current_matches"]:
            signal_tier = 1
        elif withdrawal["unresolved"]:
            signal_tier = 2
        else:
            signal_tier = 3
        best_rank = attention["best_rank"]
        return (
            signal_tier,
            best_rank is None,
            best_rank or 101,
            -attention["distinct_headlines"],
            event["canonical_headline"],
        )

    candidates.sort(key=event_order)
    events = candidates[:limit]

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "live",
        "assessment": {
            "code": "current_clusters_available",
            "label": "TREND LENSES · CURRENT",
            "headline": "Current permitted-attention clusters are available",
            "reading": (
                "Each card is an independent bounded reading of one current headline "
                "cluster. Presence, pinning, withdrawal watch, and DDTI overlap remain "
                "separate measurements."
            ),
        },
        "clocks": {
            "source_generated_at": generated_text,
            "evaluated_at": evaluated,
            "window_start": window_start,
            "window_end": window_end,
            "freshness": freshness,
            "age_seconds": age_seconds,
        },
        "selection": {
            "latest_day": window_end,
            "current_titles": sum(row["last_seen"] == window_end for row in rows),
            "current_clusters": len(clusters),
            "published_clusters": len(events),
            "max_events": limit,
            "truncated": len(clusters) > limit,
            "ordering": (
                "state_pin_then_ddti_then_unconfirmed_withdrawal_then_"
                "best_rank_then_variant_count_then_headline"
            ),
        },
        "ddti_context": ddti_state,
        "newswire_context": wire_state,
        "events": events,
        "method": {
            "discovery": "all valid titles whose last_seen equals the latest board day",
            "clustering": (
                "NFKC headline normalization, contextual number folding, conservative "
                "containment, and character-trigram similarity"
            ),
            "match_threshold": MATCH_THRESHOLD,
            "trend_id": "sha256 prefix of the shortest normalized headline anchor in the bounded window",
            "publication_boundary": (
                "automatic event cards may describe permitted attention and instrument "
                "overlap; they may not assert a takedown, blackout, or uncensored discussion"
            ),
        },
        "limitations": _limitations(),
    }


__all__ = [
    "BOARD_SOURCE_URL",
    "DEFAULT_MAX_EVENTS",
    "SCHEMA_VERSION",
    "build_trending_event_lenses",
    "headline_key",
    "headline_similarity",
]
