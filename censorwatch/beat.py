"""Celery beat entries for censorwatch — merged into the platform schedule ONLY
when ``CENSORWATCH_ENABLED`` is set (see ``core/scheduler.py``).

Cadence design:
- **Capture** runs per source on a fixed interval.
- **Re-check is tiered by post age** because deletions cluster in the first hours
  after posting (Zhu et al. 2013): fresh posts are re-checked aggressively, aging
  ones less often, mature ones rarely before retirement. Each ``cw_recheck`` call
  runs its own liveness probe first (see ``detector.py``).
- **Signal** recomputes velocity/spikes on a steady beat.

All entries route to a dedicated ``censorwatch`` queue so this never competes for
worker slots with production collectors. Run a separate worker:
    celery -A core.scheduler worker -Q censorwatch -c 2
"""

from __future__ import annotations

from celery.schedules import crontab

_Q = {"queue": "censorwatch"}


def build_censorwatch_schedule() -> dict:
    """Return the censorwatch beat_schedule fragment."""
    return {
        # ── CAPTURE ──────────────────────────────────────────────
        # Eastmoney guba is proven first; xueqiu/weibo are added as they come
        # online (their sources.yaml entries stay enabled:false until then).
        "cw-collect-eastmoney_guba": {
            "task": "censorwatch.tasks.cw_collect",
            "schedule": crontab(minute="*/10"),
            "args": ["eastmoney_guba"],
            "options": _Q,
        },

        # ── RE-CHECK (tiered by post age) ────────────────────────
        "cw-recheck-fresh": {                       # posts < 6h old
            "task": "censorwatch.tasks.cw_recheck",
            "schedule": crontab(minute="*/15"),
            "kwargs": {"cohort": "fresh", "max_age_hours": 6},
            "options": _Q,
        },
        "cw-recheck-aging": {                       # 6h–72h old
            "task": "censorwatch.tasks.cw_recheck",
            "schedule": crontab(minute=5, hour="*/2"),
            "kwargs": {"cohort": "aging", "min_age_hours": 6, "max_age_hours": 72},
            "options": _Q,
        },
        "cw-recheck-mature": {                      # 3d–14d old, then retire
            "task": "censorwatch.tasks.cw_recheck",
            "schedule": crontab(minute=20, hour="*/12"),
            "kwargs": {"cohort": "mature", "min_age_hours": 72, "max_age_hours": 336},
            "options": _Q,
        },

        # ── SIGNAL ───────────────────────────────────────────────
        "cw-signal": {
            "task": "censorwatch.tasks.cw_signal",
            "schedule": crontab(minute="*/20"),
            "options": _Q,
        },
    }
