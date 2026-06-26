"""Cross-sensor deletion deduplication — one scrubbed post, merged provenance.

The censorwatch capture sensors (Xueqiu / Weibo / Guba) and the ingested
third-party feeds (FreeWeibo / GreatFire) are *independent sensors looking at the
same censorship*. When two sensors report the deletion of the **same underlying
post**, the velocity signal must count it **once** — otherwise a topic that both
we and FreeWeibo catch looks twice as "scrubbed" as one only one of us caught,
biasing ``signal.compute_velocity_signal`` toward well-covered posts.

Identity across sensors can't use ``post_id`` (each platform/source has its own id
namespace), so we key on the **content fingerprint** (``interfaces.content_hash``,
whitespace-normalized SHA-256). Same text seen twice ⇒ same event.

This module is split so the merge logic is unit-testable without a database:
  * pure core   — ``merge_source_list`` (provenance set algebra)
  * DB boundary — ``find_duplicate_post`` / ``record_corroboration``

Reused by any deletion-emitting collector; never inline this in a collector.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# How far back a prior detection still counts as "the same event". Two sensors
# rarely confirm the same scrub more than a few days apart; beyond that a repost
# of identical text is plausibly a *new* censorship event worth its own count.
DEFAULT_DEDUP_WINDOW_HOURS = 72

# Where corroborating-sensor provenance is recorded on the surviving CensoredPost.
PROVENANCE_KEY = "corroborating_sources"


# ── pure core (no DB / no clock) ─────────────────────────────────────
def merge_source_list(existing: list[str] | None, new_source: str) -> list[str]:
    """Add ``new_source`` to a provenance list, deduped and order-stable.

    Returns a NEW sorted list (callers persist it); never mutates the input.
    A blank ``new_source`` is ignored so we never record an empty provenance tag.
    """
    sources = {s for s in (existing or []) if s}
    if new_source:
        sources.add(new_source)
    return sorted(sources)


def is_within_window(prior: datetime, now: datetime, window_hours: int) -> bool:
    """True if ``prior`` falls inside ``[now - window, now]`` (tz-safe).

    Naive datetimes are treated as UTC so fixtures and DB rows compare cleanly.
    A ``prior`` in the future (clock skew) is out of window, not "0 ago".
    """
    if prior.tzinfo is None:
        prior = prior.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = (now - prior).total_seconds()
    return 0 <= age <= window_hours * 3600


# ── DB boundary ──────────────────────────────────────────────────────
def find_duplicate_post(db, *, content_hash: str, exclude_source: str,
                        now: datetime | None = None,
                        window_hours: int = DEFAULT_DEDUP_WINDOW_HOURS):
    """Return an already-*confirmed-deleted* CensoredPost matching this content
    from a DIFFERENT source within the dedup window, or None.

    "Confirmed-deleted" = ``deleted_at IS NOT NULL`` (it already produced a
    PostDeletion). Matching one means our own detector — or another ingested feed
    — has already counted this scrub, so the caller should merge provenance
    instead of writing a second deletion. An empty/missing hash never matches
    (we won't collapse two untexted posts into one).
    """
    if not content_hash:
        return None
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)

    from censorwatch.models import CensoredPost
    candidates = (
        db.query(CensoredPost)
        .filter(CensoredPost.content_hash == content_hash)
        .filter(CensoredPost.source != exclude_source)
        .filter(CensoredPost.deleted_at.isnot(None))
        .filter(CensoredPost.deleted_at >= cutoff)
        .order_by(CensoredPost.deleted_at.desc())
        .all()
    )
    for row in candidates:
        if is_within_window(row.deleted_at, now, window_hours):
            return row
    return None


def record_corroboration(db, post, new_source: str) -> None:
    """Record that ``new_source`` independently confirmed an existing deletion.

    Appends to ``CensoredPost.extra_data[PROVENANCE_KEY]`` (the existing JSONB
    "metadata" column — no schema change). Best-effort: a provenance write must
    never fail the collection run, so callers may wrap this, but we also reassign
    the dict so SQLAlchemy reliably flags the JSONB column dirty.
    """
    data = dict(post.extra_data or {})
    data[PROVENANCE_KEY] = merge_source_list(data.get(PROVENANCE_KEY), new_source)
    post.extra_data = data
    logger.info("[dedup] %s corroborated existing deletion %s/%s (provenance=%s)",
                new_source, post.source, post.post_id, data[PROVENANCE_KEY])
