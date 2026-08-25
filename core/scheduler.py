"""Celery application and beat schedule for the Palimpsest censorship observatory.

It defines the primary Celery ``app`` and assembles two deliberately separate
legs: the DDTI processor and the opt-in passive public-source fleet.
The passive fleet is merged only when ``PALIMPSEST_COLLECTORS_ENABLED`` is set;
The hostile-content CensorWatch data plane has its own application and broker in
``censorwatch.celery_app`` and is intentionally absent here.

Run the API/index worker:
    celery -A core.scheduler worker -c 2
Run the isolated passive collector worker (when enabled):
    celery -A core.scheduler worker -Q collectors -c 2
Run the isolated OONI bulk warehouse worker (when enabled):
    celery -A core.scheduler worker -Q warehouse -c 2
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
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_send_task_events=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=200,
    result_expires=24 * 3600,
    broker_transport_options={"visibility_timeout": 3600},
    task_routes={
        # Keep manual/retry invocation on the same isolated high-volume lane;
        # beat entries also name the queue explicitly for auditability.
        "core.tasks.ingest_ooni_bulk_hour": {"queue": "warehouse"},
    },
)

# Register only primary task modules. CensorWatch must never share this broker.
app.autodiscover_tasks(["core"])


def _base_schedule() -> dict:
    """Selectivity/novelty processor — always on; acquisition is a separate leg."""
    return {
        "ddti-generate-index": {
            "task": "core.tasks.generate_ddti_index",
            "schedule": crontab(minute="*/30"),
            "options": {"queue": "celery", "expires": 25 * 60},
        },
        "heartbeat-default": {
            "task": "core.tasks.queue_heartbeat",
            "schedule": crontab(minute="*"),
            "args": ["default"],
            "options": {"queue": "celery", "expires": 50},
        },
        "refresh-node-status": {
            "task": "core.tasks.refresh_node_status",
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "celery", "expires": 4 * 60},
        },
    }


def build_beat_schedule() -> dict:
    schedule = _base_schedule()
    from core.collector_fleet import collectors_enabled
    if collectors_enabled():
        from core.collector_fleet import build_collector_schedule
        schedule.update(build_collector_schedule())
    from core.ooni_warehouse import warehouse_enabled
    if warehouse_enabled():
        from core.ooni_warehouse import build_warehouse_schedule
        schedule.update(build_warehouse_schedule())
    return schedule


app.conf.beat_schedule = build_beat_schedule()
