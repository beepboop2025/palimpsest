"""Celery tasks for the DDTI index and the opt-in passive collector fleet."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from core.scheduler import app

logger = logging.getLogger(__name__)


def _run_with_lease(name: str, operation: Callable[[], dict], *, timeout_s: int) -> dict:
    """Run once across the fleet, or skip when a previous run still owns the lease.

    Beat schedules the intent; the Redis lease enforces non-overlap after slow
    sources, worker restarts, or manual invocations.  Redis is already the task
    broker, so failing to acquire it is a real infrastructure failure and the
    safe response is to collect nothing.
    """

    import redis

    client = redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )
    lock = client.lock(
        f"palimpsest:collector-lock:{name}",
        timeout=timeout_s,
        blocking_timeout=0,
    )
    acquired = False
    try:
        acquired = lock.acquire(blocking=False)
        if not acquired:
            return {
                "collector": name,
                "status": "skipped",
                "records_collected": 0,
                "error": "previous run still holds the collector lease",
            }
        return operation()
    except Exception as exc:
        logger.exception("[%s] collector task failed", name)
        return {
            "collector": name,
            "status": "failed",
            "records_collected": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if acquired:
            try:
                lock.release()
            except Exception:
                logger.warning("[%s] collector lease expired before release", name)
        client.close()


def _log_snapshot_result(result: dict) -> None:
    """Persist one snapshot-run outcome in PostgreSQL (best effort)."""

    try:
        from api.database import SessionLocal
        from storage.models import CollectionLog

        db = SessionLocal()
        try:
            db.add(CollectionLog(
                source=str(result.get("collector", "snapshot"))[:64],
                status=str(result.get("status", "failed"))[:16],
                records_collected=int(result.get("records_collected", 0) or 0),
                duration_seconds=float(result.get("duration_seconds", 0) or 0),
                error_message=(str(result.get("error") or "")[:1000] or None),
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("snapshot collection logging failed: %s", exc)


@app.task(name="core.tasks.generate_ddti_index")
def generate_ddti_index() -> dict:
    """Recompute the Deletion-Differential Threat Index from recent deletions.

    Reads CDT deletion records (Article.category == 'ddti_deletion'), recomputes
    selectivity + novelty, publishes to Redis (ddti:index:latest), and writes one
    DDTIIndexSnapshot time-series row. Returns a structured status dict.
    """
    from processors.ddti_index import DDTIIndexProcessor

    result = DDTIIndexProcessor().run()
    logger.info("[ddti] index run: %s", result)
    return result


@app.task(
    bind=True,
    name="core.tasks.collect_ddti_feed_head",
    max_retries=2,
    soft_time_limit=8 * 60,
    time_limit=10 * 60,
)
def collect_ddti_feed_head(self) -> dict:
    """Capture the newest CDT feed page into immutable raw storage + Postgres."""

    from core.collector_fleet import run_ddti_head

    result = _run_with_lease("ddti-feed-head", run_ddti_head, timeout_s=10 * 60)
    logger.info("[ddti-feed-head] run: %s", result)
    if result.get("status") == "failed":
        raise self.retry(
            exc=RuntimeError(result.get("error") or "DDTI feed-head collection failed"),
            countdown=5 * 60,
        )
    return result


@app.task(
    bind=True,
    name="core.tasks.refresh_public_snapshot",
    max_retries=2,
    soft_time_limit=25 * 60,
    time_limit=30 * 60,
)
def refresh_public_snapshot(self, name: str) -> dict:
    """Refresh one allowlisted keyless snapshot on the measurement node."""

    from core.collector_fleet import run_snapshot_job

    result = _run_with_lease(
        f"snapshot:{name}",
        lambda: run_snapshot_job(name),
        timeout_s=30 * 60,
    )
    _log_snapshot_result(result)
    logger.info("[%s] snapshot run: %s", name, result)
    if result.get("status") == "failed":
        raise self.retry(
            exc=RuntimeError(result.get("error") or f"{name} snapshot failed"),
            countdown=10 * 60,
        )
    return result
