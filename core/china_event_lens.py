"""Declared event lenses over the public Weibo hot-search board.

The board is a record of permitted attention, not a census of public opinion.
This module answers a narrower question: when an already-declared event moves
through revised headlines, does the board show current visibility, state-pinned
framing, or an unresolved withdrawal candidate?

Event definitions are deliberately authored below.  A model does not discover
events or decide what counts as censorship at publication time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "palimpsest.china-event-lenses.v1"
SOURCE_URL = (
    "https://www.palimpsest.info/readings/"
    "weibo-hotsearch-terms-latest.json"
)
FRESH_FOR_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class EventDefinition:
    event_id: str
    title: str
    cross_border_label: str
    china_side_label: str
    cross_border_geography: tuple[str, ...]
    china_side_geography: tuple[str, ...]
    context_terms: tuple[str, ...]


# The event is human-declared and evidence-bounded.  Geography alone is not
# enough: each title must also carry a disaster/rescue context term, preventing
# unrelated Nepal or Tibet headlines from entering the comparison.
DECLARED_EVENTS = (
    EventDefinition(
        event_id="nepal-flood-tibet-jilong-2026-08",
        title="Nepal flood / Tibet–Jilong disaster",
        cross_border_label="Nepal-side attention",
        china_side_label="Tibet–Jilong attention",
        cross_border_geography=("尼泊尔",),
        china_side_geography=("西藏", "吉隆"),
        context_terms=(
            "山洪",
            "洪水",
            "泥石流",
            "冰崩",
            "堰塞湖",
            "灾害",
            "灾区",
            "受灾",
            "遇难",
            "失联",
            "搜救",
            "救援",
            "幸存",
            "冲毁",
            "冲断",
            "泥浆",
            "废墟",
            "默哀",
            "抢险",
        ),
    ),
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


def _matches(title: str, geography: Sequence[str], context: Sequence[str]) -> bool:
    return bool(title) and any(term in title for term in geography) and any(
        term in title for term in context
    )


def _event_matches(title: str, event: EventDefinition) -> bool:
    return _matches(title, event.cross_border_geography, event.context_terms) or _matches(
        title, event.china_side_geography, event.context_terms
    )


def _rank(value: Any) -> int | None:
    return value if type(value) is int and 1 <= value <= 100 else None


def _date(value: Any) -> str:
    text = _bounded_text(value, 10)
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return ""
    return text


def _term_rows(
    document: Mapping[str, Any],
    geography: Sequence[str],
    context: Sequence[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in document.get("terms") or []:
        if not isinstance(raw, Mapping):
            continue
        title = _bounded_text(raw.get("title"), 180)
        if not _matches(title, geography, context):
            continue
        first_seen = _date(raw.get("first_seen"))
        last_seen = _date(raw.get("last_seen"))
        if not first_seen or not last_seen or first_seen > last_seen:
            continue
        appearances = raw.get("appearances")
        rows.append(
            {
                "title": title,
                "best_rank": _rank(raw.get("best_rank")),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "appearances": (
                    appearances if type(appearances) is int and appearances > 0 else 1
                ),
                "pinned": bool(raw.get("pinned")),
            }
        )
    rows.sort(
        key=lambda row: (
            row["last_seen"],
            -(row["best_rank"] or 101),
            row["title"],
        ),
        reverse=True,
    )
    return rows


def _side_summary(rows: list[dict[str, Any]], latest_day: str) -> dict[str, Any]:
    ranks = [row["best_rank"] for row in rows if row["best_rank"] is not None]
    current = [row for row in rows if row["last_seen"] == latest_day]
    current.sort(key=lambda row: (row["best_rank"] is None, row["best_rank"] or 101))
    return {
        "distinct_headlines": len(rows),
        "title_days": sum(row["appearances"] for row in rows),
        "first_seen": min((row["first_seen"] for row in rows), default=None),
        "last_seen": max((row["last_seen"] for row in rows), default=None),
        "visible_on_latest_day": bool(current),
        "best_rank": min(ranks) if ranks else None,
        "top_10_headlines": sum(
            1 for row in rows if row["best_rank"] is not None and row["best_rank"] <= 10
        ),
        "current_headlines": current[:5],
    }


def _pinned_rows(
    document: Mapping[str, Any], event: EventDefinition
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw_day in document.get("pinned_headlines") or []:
        if not isinstance(raw_day, Mapping):
            continue
        day = _date(raw_day.get("date"))
        if not day:
            continue
        for value in raw_day.get("pinned") or []:
            title = _bounded_text(value, 180)
            if _matches(title, event.china_side_geography, event.context_terms):
                rows.append({"date": day, "title": title})
    rows.sort(key=lambda row: (row["date"], row["title"]))
    return rows


def _withdrawal_read(
    document: Mapping[str, Any],
    event: EventDefinition,
    cross_rows: list[dict[str, Any]],
    china_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    watch = document.get("withdrawal_watch")
    watch = watch if isinstance(watch, Mapping) else {}
    items: list[dict[str, Any]] = []

    for raw in watch.get("candidates") or []:
        if not isinstance(raw, Mapping):
            continue
        title = _bounded_text(raw.get("title"), 180)
        day = _date(raw.get("date"))
        if not day or not _event_matches(title, event):
            continue
        relevant_rows = (
            cross_rows
            if _matches(title, event.cross_border_geography, event.context_terms)
            else china_rows
        )
        continued = any(row["last_seen"] > day for row in relevant_rows)
        items.append(
            {
                "title": title,
                "date": day,
                "best_rank": _rank(raw.get("best_rank")),
                "raw_state": "candidate",
                "event_state": (
                    "event_continued_after_headline" if continued else "unconfirmed_exit"
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
        if not day or not _event_matches(title, event):
            continue
        items.append(
            {
                "title": title,
                "date": day,
                "best_rank": _rank(raw.get("best_rank")),
                "raw_state": "sense_filtered",
                "event_state": "ordinary_disaster_sense_rejected",
                "matched_terms": [
                    _bounded_text(item.get("term"), 40)
                    for item in (raw.get("sense_filtered_terms") or [])
                    if isinstance(item, Mapping) and _bounded_text(item.get("term"), 40)
                ],
            }
        )

    return {
        "raw_flags": len(items),
        "resolved_by_later_attention": sum(
            item["event_state"] == "event_continued_after_headline" for item in items
        ),
        "ordinary_sense_rejections": sum(
            item["event_state"] == "ordinary_disaster_sense_rejected" for item in items
        ),
        "unresolved": sum(item["event_state"] == "unconfirmed_exit" for item in items),
        "items": items[:12],
        "baseline_persist_rate": watch.get("baseline_persist_rate"),
    }


def _ddti_overlap(document: Mapping[str, Any], event: EventDefinition) -> int:
    matched = 0
    for raw in document.get("ddti_join") or []:
        if not isinstance(raw, Mapping):
            continue
        pieces = [_bounded_text(raw.get("term"), 100)]
        for sample in raw.get("samples") or []:
            if isinstance(sample, Mapping):
                pieces.append(_bounded_text(sample.get("title"), 180))
        if _event_matches(" ".join(pieces), event):
            matched += 1
    return matched


def _evidence_rows(
    cross_rows: list[dict[str, Any]],
    china_rows: list[dict[str, Any]],
    pins: list[dict[str, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add_board(row: Mapping[str, Any], side: str) -> None:
        candidate = {
            "kind": "board_title",
            "side": side,
            "date": row.get("last_seen"),
            "title": row.get("title"),
            "best_rank": row.get("best_rank"),
            "status": "permitted_attention",
            "source_url": SOURCE_URL,
        }
        if candidate not in out:
            out.append(candidate)

    for rows, side in ((cross_rows, "cross_border"), (china_rows, "china_side")):
        if rows:
            latest = max(rows, key=lambda row: (row["last_seen"], -(row["best_rank"] or 101)))
            best = min(rows, key=lambda row: row["best_rank"] or 101)
            add_board(latest, side)
            add_board(best, side)
    for row in pins[-2:]:
        out.append(
            {
                "kind": "pinned_headline",
                "side": "china_side",
                "date": row["date"],
                "title": row["title"],
                "best_rank": None,
                "status": "state_pinned_attention",
                "source_url": SOURCE_URL,
            }
        )
    return out[:8]


def _unavailable_event(
    event: EventDefinition,
    *,
    generated_at: str | None,
    evaluated_at: str,
    window_start: str | None,
    window_end: str | None,
    freshness: str,
    age_seconds: int | None,
    reason: str,
) -> dict[str, Any]:
    empty = _side_summary([], window_end or "")
    return {
        "event_id": event.event_id,
        "title": event.title,
        "finding_state": "unavailable",
        "assessment": {
            "code": "unavailable",
            "label": "EVENT LENS · UNAVAILABLE",
            "headline": "No current censorship inference",
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
        "attention": {
            "cross_border_label": event.cross_border_label,
            "china_side_label": event.china_side_label,
            "cross_border": empty,
            "china_side": empty,
            "state_pins": {"count": 0, "days": [], "items": []},
        },
        "withdrawal_watch": {
            "raw_flags": 0,
            "resolved_by_later_attention": 0,
            "ordinary_sense_rejections": 0,
            "unresolved": 0,
            "items": [],
            "baseline_persist_rate": None,
        },
        "ddti_corroboration": {
            "matches": 0,
            "state": "unavailable",
            "note": "The event-specific DDTI join was not available.",
        },
        "evidence": [],
        "limitations": _limitations(),
    }


def _limitations() -> list[str]:
    return [
        "The Weibo hot-search board is a curated permitted-attention surface, not neutral public volume.",
        "The archive is a per-day union and does not preserve hourly dwell time or view counts.",
        "A one-day exact-headline exit is not a takedown; event continuity and deletion evidence must be checked.",
        "Casualty figures are retained only as unverified headline text and are not validated by this instrument.",
        "No post body, user graph, private account, or in-China observer is used.",
    ]


def _build_event(
    document: Mapping[str, Any],
    event: EventDefinition,
    *,
    generated_at: str,
    evaluated_at: str,
    window_start: str,
    window_end: str,
    freshness: str,
    age_seconds: int,
) -> dict[str, Any]:
    cross_rows = _term_rows(
        document, event.cross_border_geography, event.context_terms
    )
    china_rows = _term_rows(document, event.china_side_geography, event.context_terms)
    cross = _side_summary(cross_rows, window_end)
    china = _side_summary(china_rows, window_end)
    pins = _pinned_rows(document, event)
    withdrawals = _withdrawal_read(document, event, cross_rows, china_rows)
    ddti_matches = _ddti_overlap(document, event)

    if not cross_rows and not china_rows:
        code = "not_observed"
        label = "NO MATCHING EVENT RECORD"
        headline = "Absence is not a blackout finding"
        reading = (
            "No declared event headline appears in this bounded board window. "
            "That absence cannot distinguish censorship from ordinary lack of attention."
        )
        finding_state = "bounded_absence"
    elif cross["visible_on_latest_day"] and (
        cross["best_rank"] is not None and cross["best_rank"] <= 10
    ):
        code = "visible_managed_attention" if pins else "visible_permitted_attention"
        label = "VISIBLE · MANAGED ATTENTION" if pins else "VISIBLE · PERMITTED ATTENTION"
        headline = "No topic-level blackout detected"
        resolved = withdrawals["resolved_by_later_attention"]
        rejected = withdrawals["ordinary_sense_rejections"]
        if resolved:
            withdrawal_sentence = (
                f" {resolved} exact-headline exit flag was followed by later event "
                "headlines and is treated as revision churn, not a confirmed takedown."
            )
        elif rejected:
            withdrawal_sentence = (
                f" {rejected} raw withdrawal trigger was rejected because the sensitive "
                "term appeared in an ordinary disaster context."
            )
        else:
            withdrawal_sentence = " No unresolved event-level withdrawal is present."
        reading = (
            f"The Nepal-side event remained on the permitted Weibo attention surface "
            f"through {window_end}: {cross['distinct_headlines']} distinct headlines, "
            f"{cross['title_days']} title-days, and a best rank of #{cross['best_rank']}. "
            f"The linked official Tibet–Jilong disaster frame occupied the state-pinned slot on "
            f"{len({row['date'] for row in pins})} day(s)."
            f"{withdrawal_sentence} This supports managed visibility, not a topic blackout; "
            "it does not prove that discussion was uncensored."
        )
        finding_state = "bounded_observation"
    elif withdrawals["unresolved"] and not cross["visible_on_latest_day"]:
        code = "withdrawal_watch_unconfirmed"
        label = "WITHDRAWAL WATCH · UNCONFIRMED"
        headline = "The event left the latest board window"
        reading = (
            "A sensitive exact headline exited after one observed day and no later matching "
            "event headline is present in this window. This is a review trigger, not proof "
            "of a takedown, because no deletion trail or hourly dwell record is available."
        )
        finding_state = "instrument_warning"
    else:
        code = "visible_in_window_current_uncertain"
        label = "VISIBLE IN WINDOW · CURRENT STATUS UNCERTAIN"
        headline = "The event was observed, but not on the latest board day"
        reading = (
            f"The event appeared in {cross['distinct_headlines']} Nepal-side headlines in "
            "the bounded window. Its latest-day visibility is absent or below the declared "
            "high-attention rule, so Palimpsest does not infer either a blackout or normality."
        )
        finding_state = "bounded_observation"

    pin_days = sorted({row["date"] for row in pins})
    return {
        "event_id": event.event_id,
        "title": event.title,
        "finding_state": finding_state,
        "assessment": {
            "code": code,
            "label": label,
            "headline": headline,
            "reading": reading,
        },
        "clocks": {
            "source_generated_at": generated_at,
            "evaluated_at": evaluated_at,
            "window_start": window_start,
            "window_end": window_end,
            "freshness": freshness,
            "age_seconds": age_seconds,
        },
        "attention": {
            "cross_border_label": event.cross_border_label,
            "china_side_label": event.china_side_label,
            "cross_border": cross,
            "china_side": china,
            "state_pins": {"count": len(pins), "days": pin_days, "items": pins[-8:]},
        },
        "withdrawal_watch": withdrawals,
        "ddti_corroboration": {
            "matches": ddti_matches,
            "state": "present" if ddti_matches else "absent",
            "note": (
                "Event-specific DDTI deletion-pressure overlap is present."
                if ddti_matches
                else "No event-specific DDTI deletion corroboration is present in this board record."
            ),
        },
        "evidence": _evidence_rows(cross_rows, china_rows, pins),
        "limitations": _limitations(),
    }


def build_declared_event_lenses(
    document: Mapping[str, Any] | None,
    *,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build every declared lens from one public board document.

    Stale, malformed, or abstaining input produces an unavailable event, never a
    quiet or no-blackout reading.
    """

    now = evaluated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    evaluated = _iso_z(now)
    payload = document if isinstance(document, Mapping) else {}
    generated_text = _bounded_text(payload.get("generated_at"), 64) or None
    generated = _utc(generated_text)
    days = [day for day in (_date(value) for value in (payload.get("window_days") or [])) if day]
    window_start = min(days) if days else None
    window_end = max(days) if days else None
    age_seconds = max(0, int((now - generated).total_seconds())) if generated else None
    freshness = (
        "fresh"
        if age_seconds is not None and age_seconds <= FRESH_FOR_SECONDS
        else "stale" if age_seconds is not None else "unavailable"
    )

    unavailable_reason = ""
    if payload.get("status") != "live":
        unavailable_reason = "The public board source is absent or abstaining."
    elif generated is None or not window_start or not window_end:
        unavailable_reason = "The public board source is missing a valid clock or window."
    elif generated > now.replace(microsecond=0):
        unavailable_reason = "The public board source clock is in the future."
    elif freshness != "fresh":
        unavailable_reason = (
            "The public board source is stale; Palimpsest preserves the last evidence "
            "but withholds a current censorship inference."
        )

    if unavailable_reason:
        events = [
            _unavailable_event(
                event,
                generated_at=generated_text,
                evaluated_at=evaluated,
                window_start=window_start,
                window_end=window_end,
                freshness=freshness,
                age_seconds=age_seconds,
                reason=unavailable_reason,
            )
            for event in DECLARED_EVENTS
        ]
    else:
        events = [
            _build_event(
                payload,
                event,
                generated_at=generated_text or "",
                evaluated_at=evaluated,
                window_start=window_start or "",
                window_end=window_end or "",
                freshness=freshness,
                age_seconds=age_seconds or 0,
            )
            for event in DECLARED_EVENTS
        ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_text,
        "evaluated_at": evaluated,
        "source": {
            "product": "Palimpsest",
            "dataset": "weibo-hotsearch-terms",
            "url": SOURCE_URL,
            "evidence_class": "observed_and_derived",
            "observer_class": "outside-china-public-board-archive",
        },
        "method": (
            "Human-declared geography plus disaster-context matching over public board "
            "titles. Exact-headline exits are checked against later matching event "
            "headlines before any withdrawal warning. State pins and permitted attention "
            "remain separate; neither is treated as uncensored public volume."
        ),
        "events": events,
    }


__all__ = [
    "DECLARED_EVENTS",
    "FRESH_FOR_SECONDS",
    "SCHEMA_VERSION",
    "SOURCE_URL",
    "build_declared_event_lenses",
]
