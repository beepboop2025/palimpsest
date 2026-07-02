"""Celery tasks for censorwatch — autodiscovered via ``core.scheduler``.

These are registered whenever the worker imports the ``censorwatch`` package, but
they only DO anything when ``CENSORWATCH_ENABLED`` is set (a manual invocation
with the flag off returns a no-op marker). The beat entries that drive them
(``beat.py``) are themselves only merged into the schedule when the flag is on,
so with the flag unset these tasks are inert in every respect.

Step 0 provides the task wiring + the safety guards; the bodies delegate to
modules built in later steps (collector → detector → signal). Until those land,
each task returns a structured ``"pending"`` result instead of raising, so an
early enable can't crash the beat.
"""

from __future__ import annotations

import logging

from core.scheduler import app
from censorwatch.config import get_settings

logger = logging.getLogger(__name__)


def _disabled_result(task: str) -> dict:
    return {"task": task, "status": "disabled", "note": "CENSORWATCH_ENABLED not set"}


@app.task(bind=True, name="censorwatch.tasks.cw_collect", max_retries=3,
          default_retry_delay=60)
def cw_collect(self, source_name: str):
    """Capture recent posts from one source and upsert them (idempotent).

    Implemented in Step 2 (``collectors/base_post_collector.py`` + per-source).
    """
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_collect")
    try:
        import asyncio
        from censorwatch.db import create_tables
        from censorwatch.registry import get_collector

        create_tables()  # idempotent; ensures isolated tables exist
        collector = get_collector(source_name)
        if collector is None:
            return {"task": "cw_collect", "source": source_name,
                    "status": "skipped", "note": "unknown or disabled source"}
        # BaseCollector.run() drives collect → store_raw → parse → validate →
        # _upsert (our override → censored_posts). Returns its own result dict.
        result = asyncio.run(collector.run())
        logger.info("[censorwatch] cw_collect(%s): %s (%s records)", source_name,
                    result.get("status"), result.get("records_collected", 0))
        return {"task": "cw_collect", "source": source_name, **result}
    except Exception as e:  # never let a censorwatch failure escalate to the beat
        logger.error("[censorwatch] cw_collect(%s) failed: %s", source_name, e)
        return {"task": "cw_collect", "source": source_name, "status": "error",
                "error": str(e)}


@app.task(bind=True, name="censorwatch.tasks.cw_recheck", max_retries=2,
          default_retry_delay=120)
def cw_recheck(self, cohort: str = "fresh", min_age_hours: float = 0,
               max_age_hours: float = 6):
    """Re-check pending posts in an age cohort for deletion.

    Runs the per-source liveness probe first, then the LIVE/GONE/UNKNOWN/DEGRADED
    state machine. Implemented in Step 4 (``detector.py``).
    """
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_recheck")
    try:
        import asyncio
        from censorwatch.db import create_tables
        from censorwatch.registry import enabled_sources
        from censorwatch.detector import recheck_source

        create_tables()

        async def _run():
            out = []
            for src in enabled_sources():
                out.append(await recheck_source(
                    src, cohort=cohort,
                    min_age_hours=min_age_hours, max_age_hours=max_age_hours,
                ))
            return out

        results = asyncio.run(_run())
        confirmed = sum(r.get("confirmed", 0) for r in results)
        logger.info("[censorwatch] cw_recheck(cohort=%s): %d sources, %d confirmed deletions",
                    cohort, len(results), confirmed)
        return {"task": "cw_recheck", "cohort": cohort, "status": "ok",
                "confirmed": confirmed, "sources": results}
    except Exception as e:
        logger.error("[censorwatch] cw_recheck(%s) failed: %s", cohort, e)
        return {"task": "cw_recheck", "cohort": cohort, "status": "error", "error": str(e)}


@app.task(bind=True, name="censorwatch.tasks.cw_signal")
def cw_signal(self):
    """Recompute deletion velocity + spike flags from confirmed deletions.

    Implemented in Step 5 (``signal.py``).
    """
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_signal")
    try:
        from censorwatch.db import create_tables
        from censorwatch.signal import run_signal
        create_tables()
        signal = run_signal()
        return {"task": "cw_signal", "status": "ok",
                "n_deletions": signal["n_deletions"], "n_spikes": signal["n_spikes"],
                "top_term": signal["top_term"]}
    except Exception as e:
        logger.error("[censorwatch] cw_signal failed: %s", e)
        return {"task": "cw_signal", "status": "error", "error": str(e)}


@app.task(bind=True, name="censorwatch.tasks.cw_cloud_sync")
def cw_cloud_sync(self):
    """Export + upload consolidated censorwatch data to cloud object storage."""
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_cloud_sync")
    try:
        from censorwatch.db import create_tables
        from censorwatch.cloud_sync import run_cloud_sync

        create_tables()
        out = run_cloud_sync(settings=settings)
        return {"task": "cw_cloud_sync", **out}
    except Exception as e:
        logger.error("[censorwatch] cw_cloud_sync failed: %s", e)
        return {"task": "cw_cloud_sync", "status": "error", "error": str(e)}


@app.task(bind=True, name="censorwatch.tasks.cw_consolidate")
def cw_consolidate(self):
    """Continuously consolidate collector outputs into one structured dataset."""
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_consolidate")
    try:
        from censorwatch.db import create_tables
        from censorwatch.consolidator import run_consolidation

        create_tables()
        out = run_consolidation(settings=settings)
        return {"task": "cw_consolidate", **out}
    except Exception as e:
        logger.error("[censorwatch] cw_consolidate failed: %s", e)
        return {"task": "cw_consolidate", "status": "error", "error": str(e)}


@app.task(bind=True, name="censorwatch.tasks.cw_emulate")
def cw_emulate(self):
    """Run predeploy censorship emulation and promotion gate checks."""
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_emulate")
    try:
        from censorwatch.emulation import run_emulation

        out = run_emulation(settings=settings)
        return {"task": "cw_emulate", **out}
    except Exception as e:
        logger.error("[censorwatch] cw_emulate failed: %s", e)
        return {"task": "cw_emulate", "status": "error", "error": str(e)}


@app.task(bind=True, name="censorwatch.tasks.cw_fusion")
def cw_fusion(self):
    """Run weighted multi-source fusion timeline generation."""
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_fusion")
    try:
        from censorwatch.fusion import run_fusion

        out = run_fusion(settings=settings)
        return {"task": "cw_fusion", **out}
    except Exception as e:
        logger.error("[censorwatch] cw_fusion failed: %s", e)
        return {"task": "cw_fusion", "status": "error", "error": str(e)}
