"""Celery application and beat schedule for the Palimpsest censorship observatory.

Slim, censorship-only scheduler. It defines one Celery ``app``, autodiscovers the
DDTI index task and the CensorWatch velocity tasks, and assembles the beat
schedule. The CensorWatch velocity leg is merged in ONLY when
``CENSORWATCH_ENABLED`` is set, so the deletion-detection machinery is inert by
default (matching its isolated, feature-flagged design).

Run the API/index worker:
    celery -A core.scheduler worker -c 2
Run the isolated CensorWatch worker (when enabled):
    celery -A core.scheduler worker -Q censorwatch -c 2
Run beat:
    celery -A core.scheduler beat
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

BROKER_URL = os.getenv("CELERY_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0"))
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", BROKER_URL)

app = Celery("palimpsest", broker=BROKER_URL, backend=RESULT_BACKEND)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_max_tasks_per_child=200,
)

# Register task modules. CensorWatch tasks are inert unless CENSORWATCH_ENABLED.
app.autodiscover_tasks(["core", "censorwatch"])


def _base_schedule() -> dict:
    """Selectivity/novelty index — always on. Pulls CDT, recomputes the index."""
    return {
        "ddti-generate-index": {
            "task": "core.tasks.generate_ddti_index",
            "schedule": crontab(minute="*/30"),
        },
    }


def build_beat_schedule() -> dict:
    schedule = _base_schedule()
    if os.getenv("CENSORWATCH_ENABLED"):
        try:
            from censorwatch.beat import build_censorwatch_schedule
            schedule.update(build_censorwatch_schedule())
        except Exception:  # pragma: no cover - velocity leg optional
            pass
    return schedule


app.conf.beat_schedule = build_beat_schedule()
