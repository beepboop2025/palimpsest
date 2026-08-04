"""Revision-safe selection and release-news attribution for economic data."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

from core.econ_observation import EconomicObservation


def latest_as_of(
    observations: Iterable[EconomicObservation], decision_time: datetime
) -> list[EconomicObservation]:
    """Latest knowable revision of every source/series/period at ``decision_time``.

    Both clocks are enforced.  A row released before the decision but collected
    after it is unavailable; so is a backfilled old period whose revised value
    was first published later.  This is the minimum contract for honest replay.
    """
    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise ValueError("decision_time must be timezone-aware")
    grouped: dict[tuple[object, ...], list[EconomicObservation]] = defaultdict(list)
    for row in observations:
        if row.released_at <= decision_time and row.collected_at <= decision_time:
            grouped[row.vintage_key].append(row)
    selected = [
        max(rows, key=lambda r: (r.released_at, r.collected_at, r.revision))
        for rows in grouped.values()
    ]
    return sorted(
        selected,
        key=lambda r: (r.period_end, r.series_id, r.geography, r.sector, r.source_id),
    )


def latest_slice_as_of(
    observations: Iterable[EconomicObservation], decision_time: datetime
) -> list[EconomicObservation]:
    """Newest economic period per series slice and source, revision-safe."""
    rows = latest_as_of(observations, decision_time)
    grouped: dict[tuple[object, ...], list[EconomicObservation]] = defaultdict(list)
    for row in rows:
        grouped[(*row.slice_key, row.source_id)].append(row)
    return sorted(
        (max(v, key=lambda r: (r.period_end, r.period_start)) for v in grouped.values()),
        key=lambda r: (r.series_id, r.geography, r.sector, r.source_id),
    )


def revision_ledger(observations: Iterable[EconomicObservation]) -> list[dict]:
    """Describe value-changing revisions without rewriting their first vintage."""
    grouped: dict[tuple[object, ...], list[EconomicObservation]] = defaultdict(list)
    for row in observations:
        grouped[row.vintage_key].append(row)
    events = []
    for key, rows in grouped.items():
        ordered = sorted(rows, key=lambda r: (r.released_at, r.collected_at, r.revision))
        for previous, current in zip(ordered, ordered[1:]):
            if current.value == previous.value:
                continue
            events.append({
                "series_id": current.series_id,
                "source_id": current.source_id,
                "period_start": current.period_start.isoformat(),
                "period_end": current.period_end.isoformat(),
                "previous_revision": previous.revision,
                "revision": current.revision,
                "previous_value": previous.value,
                "value": current.value,
                "delta": current.value - previous.value,
                "released_at": current.released_at.isoformat(),
                "vintage_key": [str(v) for v in key],
            })
    return sorted(events, key=lambda r: (r["released_at"], r["series_id"]))
