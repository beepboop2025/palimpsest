"""Deletion detector — the LIVE / GONE / UNKNOWN / DEGRADED state machine.

Per source, per cycle:
  1. LIVENESS PROBE FIRST. Observe the source's control post(s). If none read as
     LIVE, the cycle is DEGRADED → suppress ALL deletion writes and return. (From a
     blocked egress everything looks "gone", so we must refuse to record deletions.)
  2. Otherwise re-fetch each pending post (deleted_at IS NULL) in the age cohort,
     youngest-first (deletions cluster early), and update its state:
       LIVE    → gone_streak = 0
       UNKNOWN → gone_streak unchanged   (ambiguous; retry next cycle)
       GONE    → gone_streak += 1, then ask the confirmation predicate
  3. When the predicate confirms, write deleted_at + latency and append a
     PostDeletion row. Only confirmed deletions ever reach the signal layer.

The pure decision core (``apply_observation`` + ``is_confirmed_deletion``) has no
DB or clock dependency and is unit-tested; ``recheck_source`` is the DB
orchestration around it.
"""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from censorwatch.config import CensorwatchSettings, get_settings
from censorwatch.interfaces import LivenessState, Observation, Post

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
#  CONFIRMATION PREDICATE  — ★ OWNER-AUTHORED DECISION ★
# ════════════════════════════════════════════════════════════════════
def is_confirmed_deletion(
    gone_streak: int,
    cohort: str,
    settings: CensorwatchSettings,
) -> bool:
    """Decide whether a post is CONFIRMED censored/deleted.

    Called only after a GONE observation, with ``gone_streak`` already incremented
    to include it. Returning True writes ``deleted_at`` and emits a PostDeletion —
    so this is the knob that trades false positives against detection latency.

    This is YOURS to shape — the censorship-research judgment call. The default
    below is the simple, defensible baseline:

        confirm once we've seen `settings.confirmations` consecutive GONEs
        (each from a non-DEGRADED cycle, which the caller guarantees).

    Ideas you might encode instead (replace the body, keep the signature):
      - Require MORE confirmations for the `fresh` cohort, where transient
        unavailability and edit-churn are most common, and fewer for `mature`.
      - Require the streak to span a minimum wall-clock spread (defeat a brief
        outage that returns GONE several times in quick succession) — you'd need
        to thread timing in; ask and I'll widen the signature.
      - Demand an *explicit* censorship marker (法律法规) rather than a bare 404
        before confirming, to bias toward true censorship over self-deletion.

    Return True to confirm, False to keep waiting.
    """
    # --- default baseline (safe to ship; tune freely) ---
    return gone_streak >= settings.confirmations
# ════════════════════════════════════════════════════════════════════


@dataclass
class DeletionDecision:
    """Outcome of applying one observation to a post's running state."""

    gone_streak: int
    last_state: str
    confirmed: bool
    latency_seconds: float | None = None


def apply_observation(
    gone_streak: int,
    posted_at: datetime | None,
    obs: Observation,
    settings: CensorwatchSettings,
    cohort: str,
) -> DeletionDecision:
    """Pure state transition: (current streak, observation) → new state + verdict.

    No DB, no clock beyond the observation's own ``checked_at`` — fully testable.
    """
    if obs.state == LivenessState.LIVE:
        return DeletionDecision(gone_streak=0, last_state="live", confirmed=False)

    if obs.state in (LivenessState.UNKNOWN, LivenessState.DEGRADED):
        # Ambiguous: leave the streak untouched, try again next cycle.
        return DeletionDecision(gone_streak=gone_streak, last_state="unknown",
                                confirmed=False)

    # GONE
    new_streak = gone_streak + 1
    confirmed = is_confirmed_deletion(new_streak, cohort, settings)
    latency = None
    if confirmed and posted_at is not None:
        delta = obs.checked_at - posted_at
        latency = max(0.0, delta.total_seconds())
    return DeletionDecision(gone_streak=new_streak, last_state="gone",
                            confirmed=confirmed, latency_seconds=latency)


# ── DB orchestration ────────────────────────────────────────────────
async def _probe_source(collector) -> bool:
    """Liveness probe: True iff at least one control post reads as LIVE."""
    for url in collector.control_posts():
        try:
            obs = await collector.observe(Post(source=collector.name, post_id="__control__",
                                               url=url, full_text=""))
            if obs.state == LivenessState.LIVE:
                return True
        except Exception as e:
            logger.warning("[detector:%s] control probe error %s: %s",
                           collector.name, url, e)
    return False


def _age_hours(post, now: datetime) -> float:
    ref = post.posted_at or post.first_seen_at
    if ref is None:
        return 0.0
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (now - ref).total_seconds() / 3600.0


async def recheck_source(
    source_name: str,
    *,
    cohort: str = "fresh",
    min_age_hours: float = 0.0,
    max_age_hours: float = 6.0,
    settings: CensorwatchSettings | None = None,
    batch_limit: int = 500,
) -> dict:
    """Run one re-check cycle for a source. Returns a summary dict."""
    settings = settings or get_settings()
    from censorwatch.registry import get_collector
    collector = get_collector(source_name)
    if collector is None:
        return {"source": source_name, "cohort": cohort, "status": "skipped"}

    now = datetime.now(timezone.utc)
    try:
        # 1) Liveness probe FIRST — gate the whole cycle.
        if not await _probe_source(collector):
            logger.warning("[detector:%s] DEGRADED — control posts not LIVE; "
                           "suppressing deletions this cycle", source_name)
            return {"source": source_name, "cohort": cohort, "liveness": "degraded",
                    "checked": 0, "confirmed": 0}

        from api.database import SessionLocal
        from censorwatch.models import CensoredPost, PostDeletion

        db = SessionLocal()
        checked = confirmed = 0
        try:
            pending = (
                db.query(CensoredPost)
                .filter(CensoredPost.source == source_name)
                .filter(CensoredPost.deleted_at.is_(None))
                .order_by(CensoredPost.posted_at.desc().nullslast())
                .limit(batch_limit)
                .all()
            )
            candidates = []
            for row in pending:
                age = _age_hours(row, now)
                if min_age_hours <= age <= max_age_hours:
                    candidates.append(row)

            sem = asyncio.Semaphore(max(1, settings.recheck_concurrency))

            async def _observe_row(row):
                async with sem:
                    post = Post(
                        source=row.source,
                        post_id=row.post_id,
                        url=row.url or "",
                        full_text=row.full_text or "",
                        posted_at=row.posted_at,
                        first_seen_at=row.first_seen_at,
                    )
                    try:
                        obs = await collector.observe(post)
                    except Exception as e:
                        obs = Observation(
                            state=LivenessState.UNKNOWN,
                            checked_at=datetime.now(timezone.utc),
                            reason=f"observe_error:{type(e).__name__}",
                        )
                    return row.id, obs

            observed_pairs = await asyncio.gather(*[_observe_row(r) for r in candidates])
            observed = {row_id: obs for row_id, obs in observed_pairs}

            for row in candidates:
                obs = observed.get(row.id)
                if obs is None:
                    continue
                decision = apply_observation(row.gone_streak, row.posted_at, obs,
                                             settings, cohort)
                checked += 1

                row.gone_streak = decision.gone_streak
                row.last_state = decision.last_state
                row.last_checked_at = obs.checked_at
                row.check_count = (row.check_count or 0) + 1

                if decision.confirmed:
                    row.deleted_at = obs.checked_at
                    row.deletion_latency_seconds = decision.latency_seconds
                    row.liveness_at_deletion = "live"
                    # Tag the deletion with its censored terms now, so the signal
                    # layer can rank without re-reading the (possibly large) post.
                    try:
                        from censorwatch.signal import extract_terms_for
                        terms = extract_terms_for(row.full_text or "")
                    except Exception:
                        terms = []
                    db.add(PostDeletion(
                        post_pk=row.id, source=row.source, post_id=row.post_id,
                        posted_at=row.posted_at, deleted_at=obs.checked_at,
                        latency_seconds=decision.latency_seconds, keywords=terms,
                        confirmations=decision.gone_streak, liveness_state="live",
                    ))
                    confirmed += 1
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error("[detector:%s] cycle failed: %s", source_name, e)
            return {"source": source_name, "cohort": cohort, "status": "error",
                    "error": str(e)}
        finally:
            db.close()

        logger.info("[detector:%s] cohort=%s checked=%d confirmed=%d",
                    source_name, cohort, checked, confirmed)
        return {"source": source_name, "cohort": cohort, "liveness": "healthy",
                "checked": checked, "confirmed": confirmed}
    finally:
        await collector.close()
