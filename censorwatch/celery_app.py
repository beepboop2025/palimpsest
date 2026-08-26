"""Dedicated Celery application for the isolated CensorWatch data plane."""

from __future__ import annotations

import os

from celery import Celery

from censorwatch.beat import build_censorwatch_schedule
from censorwatch.config import is_enabled
from censorwatch.runtime_secrets import redis_url


def _broker_url() -> str:
    # Imports and offline tests remain inert while the feature is off. An
    # enabled worker/beat has no fallback: missing or mis-scoped secret files
    # abort application construction before it can consume any task.
    if not is_enabled():
        return "memory://"
    role = os.getenv("CENSORWATCH_CELERY_ROLE", "").strip()
    purposes = {
        "producer-data": "broker-data-producer",
        "data": "broker-data",
        "producer-control": "broker-control-producer",
        "control": "broker-control",
    }
    try:
        purpose = purposes[role]
    except KeyError as exc:
        raise RuntimeError(
            "CENSORWATCH_CELERY_ROLE must be producer-data, data, "
            "producer-control, or control"
        ) from exc
    return redis_url(purpose)


BROKER_URL = _broker_url()
CELERY_ROLE = os.getenv("CENSORWATCH_CELERY_ROLE", "").strip()


def _broker_transport_options() -> dict[str, object]:
    options: dict[str, object] = {
        "visibility_timeout": 2100,
        "global_keyprefix": "censorwatch:broker:",
        # No CensorWatch task uses priority. A single step keeps each queue on
        # one exact Redis key instead of Kombu's four priority-suffixed keys.
        "priority_steps": [0],
    }
    # Kombu otherwise shares its visibility-timeout ledger between every
    # consumer attached to this Redis database. Give each consumer lane its
    # own exact keys so the hostile data worker cannot acknowledge, restore,
    # or delete messages owned by the control worker.
    if CELERY_ROLE in {"data", "control"}:
        options.update(
            {
                "unacked_key": f"{CELERY_ROLE}:unacked",
                "unacked_index_key": f"{CELERY_ROLE}:unacked_index",
                "unacked_mutex_key": f"{CELERY_ROLE}:unacked_mutex",
            }
        )
    return options


# Results are intentionally ignored for this workload. A process-local backend
# avoids granting any worker an unnecessary second Redis write authority.
app = Celery("censorwatch", broker=BROKER_URL, backend="cache+memory://")
CONTROL_ROLE = CELERY_ROLE in {"producer-control", "control"}
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_acks_on_failure_or_timeout=True,
    task_reject_on_worker_lost=True,
    task_track_started=False,
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    task_send_sent_event=False,
    worker_send_task_events=False,
    worker_enable_remote_control=False,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,
    broker_transport_options=_broker_transport_options(),
    task_default_queue=("censorwatch-control" if CONTROL_ROLE else "censorwatch"),
    # Long collection and recheck jobs intentionally run at concurrency one.
    # Route the execution heartbeat onto a separate worker so readiness proves
    # that the control lane is live even while hostile-source work is busy.
    task_routes={
        "censorwatch.tasks.cw_heartbeat": {"queue": "censorwatch-control"},
        "censorwatch.tasks.*": {"queue": "censorwatch"},
    },
)
app.autodiscover_tasks(["censorwatch"])
if is_enabled() and CELERY_ROLE in {"producer-data", "producer-control"}:
    expected_plane = CELERY_ROLE.removeprefix("producer-")
    configured_plane = os.getenv("CENSORWATCH_BEAT_PLANE", "").strip()
    if configured_plane != expected_plane:
        raise RuntimeError("CENSORWATCH_BEAT_PLANE does not match the producer role")
    app.conf.beat_schedule = build_censorwatch_schedule(plane=expected_plane)
else:
    app.conf.beat_schedule = {}
