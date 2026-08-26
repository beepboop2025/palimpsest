"""Celery beat entries owned only by ``censorwatch.celery_app``.

Cadence design:
- **Capture** runs per source on a fixed interval.
- **Re-check is tiered by post age** because deletions cluster in the first hours
  after posting (Zhu et al. 2013): fresh posts are re-checked aggressively, aging
  ones less often, mature ones rarely before retirement. Each ``cw_recheck`` call
  runs its own liveness probe first (see ``detector.py``).
- **Signal** recomputes velocity/spikes on a steady beat.

Data entries route to a dedicated ``censorwatch`` queue and Redis process. The
execution heartbeat has its own ``censorwatch-control`` queue, Redis process,
and Beat. A hostile data-lane capacity failure therefore cannot starve the
control heartbeat. Run separate workers and Beat processes:
    celery -A censorwatch.celery_app worker -Q censorwatch -c 1
    celery -A censorwatch.celery_app worker -Q censorwatch-control -c 1
"""

from __future__ import annotations

from celery.schedules import crontab


def _options(*, expires: int, queue: str = "censorwatch") -> dict:
    return {"queue": queue, "expires": expires}


def build_censorwatch_schedule(*, plane: str = "all") -> dict:
    """Return an exhaustive, disjoint data/control Beat schedule."""
    if plane not in {"all", "data", "control"}:
        raise ValueError("CensorWatch Beat plane must be all, data, or control")

    schedule: dict = {}
    if plane in {"all", "data"}:
        from censorwatch.registry import load_sources

        sources = load_sources()
        schedule.update({
            f"cw-collect-{name}": {
                "task": "censorwatch.tasks.cw_collect",
                "schedule": crontab(minute=f"*/{config['capture_interval_min']}"),
                "args": [name],
                "options": _options(
                    expires=max(60, config["capture_interval_min"] * 60 - 60)
                ),
            }
            for name, config in sorted(sources.items())
            if config["enabled"]
        })
        schedule.update({
            # ── RE-CHECK (tiered by post age) ────────────────────
            "cw-recheck-fresh": {
                "task": "censorwatch.tasks.cw_recheck",
                "schedule": crontab(minute="*/15"),
                "kwargs": {"cohort": "fresh", "max_age_hours": 6},
                "options": _options(expires=14 * 60),
            },
            "cw-recheck-aging": {
                "task": "censorwatch.tasks.cw_recheck",
                "schedule": crontab(minute=5, hour="*/2"),
                "kwargs": {
                    "cohort": "aging",
                    "min_age_hours": 6,
                    "max_age_hours": 72,
                },
                "options": _options(expires=60 * 60),
            },
            "cw-recheck-mature": {
                "task": "censorwatch.tasks.cw_recheck",
                "schedule": crontab(minute=20, hour="*/12"),
                "kwargs": {
                    "cohort": "mature",
                    "min_age_hours": 72,
                    "max_age_hours": 336,
                },
                "options": _options(expires=6 * 60 * 60),
            },
            "cw-signal": {
                "task": "censorwatch.tasks.cw_signal",
                "schedule": crontab(minute="*/20"),
                "options": _options(expires=19 * 60),
            },
        })
    if plane in {"all", "control"}:
        schedule["cw-heartbeat"] = {
            "task": "censorwatch.tasks.cw_heartbeat",
            "schedule": crontab(minute="*"),
            "options": _options(expires=45, queue="censorwatch-control"),
        }
    return schedule
