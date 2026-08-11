"""Opt-in Celery schedule and monitoring contract for OONI bulk storage."""

from __future__ import annotations

from collectors.ooni_bulk import warehouse_enabled


WAREHOUSE_QUEUE = "warehouse"
WAREHOUSE_CADENCE_SECONDS = 60 * 60
WAREHOUSE_GRACE_SECONDS = 75 * 60


def build_warehouse_schedule(*, schedule_factory=None) -> dict:
    """One lagged hour per tick; task expiry prevents outage backfill."""

    if schedule_factory is None:
        from celery.schedules import crontab as schedule_factory

    return {
        "heartbeat-warehouse": {
            "task": "core.tasks.queue_heartbeat",
            "schedule": schedule_factory(minute="*"),
            "args": [WAREHOUSE_QUEUE],
            "options": {"queue": WAREHOUSE_QUEUE, "expires": 50},
        },
        "ingest-ooni-bulk-hour": {
            "task": "core.tasks.ingest_ooni_bulk_hour",
            "schedule": schedule_factory(minute=35),
            # No hour argument is intentional: the task resolves one configured
            # lagged hour when it starts.  Beat cannot enqueue a date range.
            "options": {"queue": WAREHOUSE_QUEUE, "expires": 50 * 60},
        },
    }


def expected_warehouse_specs() -> list[dict]:
    return [{
        "source": "ooni-bulk",
        "output_path": "readings/ooni-bulk-latest.json",
        "cadence_seconds": WAREHOUSE_CADENCE_SECONDS,
        "grace_seconds": WAREHOUSE_GRACE_SECONDS,
        "task_name": "core.tasks.ingest_ooni_bulk_hour",
    }]


__all__ = [
    "WAREHOUSE_QUEUE",
    "build_warehouse_schedule",
    "expected_warehouse_specs",
    "warehouse_enabled",
]
