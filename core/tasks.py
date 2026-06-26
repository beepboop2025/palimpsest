"""Celery tasks for the DDTI selectivity/novelty index (always-on leg)."""

from __future__ import annotations

import logging

from core.scheduler import app

logger = logging.getLogger(__name__)


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
