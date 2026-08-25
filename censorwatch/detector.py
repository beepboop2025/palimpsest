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

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from censorwatch.config import CensorwatchSettings, get_settings
from censorwatch.interfaces import LivenessState, Observation, Post

logger = logging.getLogger(__name__)
_MAX_BATCH_LIMIT = 500
_COMMIT_CHUNK_SIZE = 25
_MIN_GONE_CONFIRMATION_SPAN_SECONDS = 300
_OBSERVATION_RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_COHORT_SLOT_SECONDS = {"fresh": 900, "aging": 7200, "mature": 43200}


def _stable_observation_run_id(
    supplied: object, *, cohort: str, now: datetime
) -> str:
    """Return one bounded identity shared by every retry/redelivery.

    Celery preserves the request ID across ``retry()`` and broker redelivery.
    Direct/operator invocations have no request ID, so they fall back to the
    same cadence slot used by the corresponding Beat entry.
    """
    if type(supplied) is str and _OBSERVATION_RUN_ID.fullmatch(supplied):
        return supplied
    try:
        slot_seconds = _COHORT_SLOT_SECONDS[cohort]
    except KeyError as exc:
        raise ValueError("invalid detector cohort") from exc
    return f"slot:{cohort}:{int(now.timestamp()) // slot_seconds}"


def _parsed_utc_timestamp(value: object) -> datetime | None:
    if type(value) is not str or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


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
        return DeletionDecision(
            gone_streak=gone_streak, last_state="unknown", confirmed=False
        )

    # GONE
    new_streak = gone_streak + 1
    confirmed = is_confirmed_deletion(new_streak, cohort, settings)
    latency = None
    if confirmed and posted_at is not None:
        delta = obs.checked_at - posted_at
        latency = max(0.0, delta.total_seconds())
    return DeletionDecision(
        gone_streak=new_streak,
        last_state="gone",
        confirmed=confirmed,
        latency_seconds=latency,
    )


# ── DB orchestration ────────────────────────────────────────────────
async def _probe_source(collector) -> bool:
    """Liveness probe: True iff at least one control post reads as LIVE."""
    for url in collector.control_posts():
        try:
            obs = await collector.observe(
                Post(
                    source=collector.name, post_id="__control__", url=url, full_text=""
                )
            )
            if obs.state == LivenessState.LIVE:
                return True
        except Exception as exc:
            logger.warning(
                "[detector:%s] control probe failed (%s)",
                collector.name,
                type(exc).__name__,
            )
    return False


def _report_detector_health(source_name: str, status: str) -> None:
    """Persist a bounded per-source detector state for readiness."""
    from censorwatch.cache import open_writer_cache
    from censorwatch.db import CensorwatchPersistenceError

    cache = None
    try:
        cache = open_writer_cache()
        payload = json.dumps(
            {
                "source": source_name,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        if not cache.set(f"health:detector:{source_name}", payload, ex=7200):
            raise CensorwatchPersistenceError(
                "CensorWatch detector health persistence failed"
            )
    except CensorwatchPersistenceError:
        raise
    except Exception as exc:
        raise CensorwatchPersistenceError(
            "CensorWatch detector health persistence failed"
        ) from exc
    finally:
        if cache is not None:
            try:
                cache.close()
            except Exception as exc:
                logger.warning(
                    "[detector:%s] health cache close failed (%s)",
                    source_name,
                    type(exc).__name__,
                )


async def recheck_source(
    source_name: str,
    *,
    cohort: str = "fresh",
    min_age_hours: float = 0.0,
    max_age_hours: float = 6.0,
    settings: CensorwatchSettings | None = None,
    batch_limit: int = 500,
    observation_run_id: str | None = None,
) -> dict:
    """Run one re-check cycle for a source. Returns a summary dict."""
    settings = settings or get_settings()
    from censorwatch.registry import get_collector

    collector = get_collector(source_name)
    if collector is None:
        return {"source": source_name, "cohort": cohort, "status": "skipped"}

    now = datetime.now(timezone.utc)
    stable_run_id = _stable_observation_run_id(
        observation_run_id, cohort=cohort, now=now
    )
    health_enabled = bool(getattr(settings, "enabled", False))
    try:
        # 1) Liveness probe FIRST — gate the whole cycle.
        if not await _probe_source(collector):
            logger.warning(
                "[detector:%s] DEGRADED — control posts not LIVE; "
                "suppressing deletions this cycle",
                source_name,
            )
            if health_enabled:
                _report_detector_health(source_name, "degraded")
            return {
                "source": source_name,
                "cohort": cohort,
                "liveness": "degraded",
                "checked": 0,
                "confirmed": 0,
            }

        from censorwatch.db import fail_persistence, writer_session
        from censorwatch.models import CensoredPost, PostDeletion
        from sqlalchemy import func

        db = writer_session()
        checked = confirmed = 0
        try:
            try:
                bounded_limit = min(_MAX_BATCH_LIMIT, max(1, int(batch_limit)))
                minimum_age = float(min_age_hours)
                maximum_age = float(max_age_hours)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid detector cohort bounds") from exc
            if (
                not math.isfinite(minimum_age)
                or not math.isfinite(maximum_age)
                or minimum_age < 0
                or maximum_age < minimum_age
            ):
                raise ValueError("invalid detector cohort bounds")
            reference_time = func.coalesce(
                CensoredPost.posted_at, CensoredPost.first_seen_at
            )
            oldest_allowed = now - timedelta(hours=maximum_age)
            newest_allowed = now - timedelta(hours=minimum_age)
            pending = (
                db.query(CensoredPost)
                .filter(CensoredPost.deleted_at.is_(None))
                .filter(CensoredPost.source == source_name)
                .filter(reference_time >= oldest_allowed)
                .filter(reference_time <= newest_allowed)
                .order_by(
                    CensoredPost.last_checked_at.asc().nullsfirst(),
                    reference_time.asc(),
                    CensoredPost.id.asc(),
                )
                .limit(bounded_limit)
                .all()
            )
            pending_in_transaction = 0
            for row in pending:
                metadata = dict(getattr(row, "extra_data", None) or {})
                raw_runs = metadata.get("detector_observation_runs")
                prior_runs = dict(raw_runs) if isinstance(raw_runs, dict) else {}
                if prior_runs.get(cohort) == stable_run_id:
                    # A committed chunk from this same Celery delivery was
                    # redelivered. Do not re-fetch or advance its streak again.
                    continue
                post = Post(
                    source=row.source,
                    post_id=row.post_id,
                    url=row.url or "",
                    full_text=row.full_text or "",
                    posted_at=row.posted_at,
                    first_seen_at=row.first_seen_at,
                )
                obs = await collector.observe(post)
                last_gone_at = _parsed_utc_timestamp(
                    metadata.get("detector_last_accepted_gone_at")
                )
                gone_too_soon = bool(
                    obs.state == LivenessState.GONE
                    and last_gone_at is not None
                    and (
                        obs.checked_at.astimezone(timezone.utc) - last_gone_at
                    ).total_seconds()
                    < _MIN_GONE_CONFIRMATION_SPAN_SECONDS
                )
                if gone_too_soon:
                    decision = DeletionDecision(
                        gone_streak=row.gone_streak,
                        last_state="gone",
                        confirmed=False,
                    )
                else:
                    decision = apply_observation(
                        row.gone_streak, row.posted_at, obs, settings, cohort
                    )
                checked += 1

                prior_runs[cohort] = stable_run_id
                metadata["detector_observation_runs"] = prior_runs
                if obs.state == LivenessState.GONE and not gone_too_soon:
                    metadata["detector_last_accepted_gone_at"] = (
                        obs.checked_at.astimezone(timezone.utc).isoformat()
                    )
                elif obs.state == LivenessState.LIVE:
                    metadata.pop("detector_last_accepted_gone_at", None)
                row.extra_data = metadata
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
                    db.add(
                        PostDeletion(
                            post_pk=row.id,
                            source=row.source,
                            post_id=row.post_id,
                            posted_at=row.posted_at,
                            deleted_at=obs.checked_at,
                            latency_seconds=decision.latency_seconds,
                            keywords=terms,
                            confirmations=decision.gone_streak,
                            liveness_state="live",
                        )
                    )
                    confirmed += 1
                pending_in_transaction += 1
                if pending_in_transaction >= _COMMIT_CHUNK_SIZE:
                    db.commit()
                    pending_in_transaction = 0
            if pending_in_transaction or not pending:
                db.commit()
        except Exception as exc:
            error_code = type(exc).__name__
            logger.error("[detector:%s] cycle failed (%s)", source_name, error_code)
            if health_enabled:
                try:
                    _report_detector_health(source_name, "failed")
                except Exception as health_error:
                    logger.error(
                        "[detector:%s] failed health write (%s)",
                        source_name,
                        type(health_error).__name__,
                    )
            fail_persistence(db, operation="detector cycle", cause=exc)
        finally:
            db.close()

        logger.info(
            "[detector:%s] cohort=%s checked=%d confirmed=%d",
            source_name,
            cohort,
            checked,
            confirmed,
        )
        if health_enabled:
            _report_detector_health(source_name, "success")
        return {
            "source": source_name,
            "cohort": cohort,
            "liveness": "healthy",
            "checked": checked,
            "confirmed": confirmed,
        }
    finally:
        await collector.close()
