"""Fail-closed Celery tasks for the dedicated CensorWatch application.

The feature remains inert while ``CENSORWATCH_ENABLED`` is false. When enabled,
every task takes a key-scoped Redis singleton lease before doing work. A task
body or required persistence failure raises/retries; it is never serialized as
a successful Celery result carrying ``status=error``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone

from censorwatch.celery_app import app
from censorwatch.config import get_settings

logger = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")

_TASK_LIMITS = {
    "cw_collect": {"soft": 1440, "hard": 1500},
    "cw_recheck": {"soft": 1440, "hard": 1500},
    "cw_signal": {"soft": 270, "hard": 300},
    "cw_heartbeat": {"soft": 45, "hard": 60},
}
_LEASE_GRACE_SECONDS = 120
_HEARTBEAT_KEY = "censorwatch:beat:heartbeat"
_HEARTBEAT_TTL_SECONDS = 300
_LEASE_RELEASE_ATTEMPTS = 3


class CensorwatchTaskError(RuntimeError):
    """A CensorWatch task could not produce a trustworthy result."""


def _identifier(value: object) -> str | None:
    return value if type(value) is str and _IDENTIFIER.fullmatch(value) else None


def _disabled_result(task: str) -> dict:
    return {"task": task, "status": "disabled", "note": "CENSORWATCH_ENABLED not set"}


def _lease_key(task_name: str, identity: str | None = None) -> str:
    suffix = f":{identity}" if identity else ""
    return f"censorwatch:task-lease:{task_name}{suffix}"


def _close_cache(cache) -> None:
    try:
        cache.close()
    except Exception as exc:
        logger.warning("[censorwatch] cache close failed (%s)", type(exc).__name__)


def _release_task_lease(cache, *, key: str, token: str) -> bool:
    """Atomically delete ``key`` only while it still contains our token."""
    from redis.exceptions import WatchError

    for _attempt in range(_LEASE_RELEASE_ATTEMPTS):
        with cache.pipeline() as transaction:
            try:
                transaction.watch(key)
                if transaction.get(key) != token:
                    transaction.unwatch()
                    return False
                transaction.multi()
                transaction.delete(key)
                result = transaction.execute()
                return result == [1]
            except WatchError:
                # A concurrent expiry/re-acquisition changed the key between GET
                # and EXEC. Never delete the successor's token; retry boundedly.
                continue
    return False


@contextmanager
def _task_lease(task_name: str, *, identity: str | None = None):
    """Yield whether this invocation owns the task's expiring singleton lease.

    Release uses Redis WATCH/GET/MULTI/DEL/EXEC, so a delayed process can never
    delete a successor's lease after its own token expired. The dedicated cache
    writer needs those transaction commands (plus UNWATCH), while its key ACL
    remains constrained to ``censorwatch:*``.
    """
    try:
        hard_limit = _TASK_LIMITS[task_name]["hard"]
    except KeyError as exc:  # pragma: no cover - internal programming error
        raise ValueError("unknown CensorWatch task lease") from exc

    key = _lease_key(task_name, identity)
    token = secrets.token_urlsafe(32)
    ttl = hard_limit + _LEASE_GRACE_SECONDS
    cache = None
    try:
        from censorwatch.cache import open_writer_cache

        cache = open_writer_cache()
        acquired = bool(cache.set(key, token, nx=True, ex=ttl))
    except Exception as exc:
        if cache is not None:
            _close_cache(cache)
        raise CensorwatchTaskError(
            f"CensorWatch {task_name} lease acquisition failed"
        ) from exc

    if not acquired:
        _close_cache(cache)
        yield False
        return

    body_failed = False
    try:
        yield True
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            if not _release_task_lease(cache, key=key, token=token):
                raise CensorwatchTaskError(
                    f"CensorWatch {task_name} lease ownership was lost"
                )
        except Exception as exc:
            logger.error(
                "[censorwatch] %s lease release failed (%s)",
                task_name,
                type(exc).__name__,
            )
            if not body_failed:
                raise CensorwatchTaskError(
                    f"CensorWatch {task_name} lease release failed"
                ) from exc
        finally:
            _close_cache(cache)


def _retry_task(task, *, task_name: str, error: BaseException):
    """Retry with bounded public text; exhaustion becomes a Celery failure."""
    error_code = type(error).__name__
    logger.error("[censorwatch] %s failed (%s)", task_name, error_code)
    bounded_error = CensorwatchTaskError(
        f"CensorWatch {task_name} failed ({error_code})"
    )
    raise task.retry(exc=bounded_error) from error


@app.task(
    bind=True,
    name="censorwatch.tasks.cw_collect",
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=_TASK_LIMITS["cw_collect"]["soft"],
    time_limit=_TASK_LIMITS["cw_collect"]["hard"],
)
def cw_collect(self, source_name: str):
    """Capture recent posts from one source and durably upsert them."""
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_collect")
    source_name = _identifier(source_name)
    if source_name is None:
        return {"task": "cw_collect", "status": "skipped", "note": "invalid source"}

    try:
        from censorwatch.registry import get_collector

        collector = get_collector(source_name)
        if collector is None:
            return {
                "task": "cw_collect",
                "source": source_name,
                "status": "skipped",
                "note": "unknown or disabled source",
            }
        with _task_lease("cw_collect", identity=source_name) as acquired:
            if not acquired:
                raise CensorwatchTaskError(
                    "CensorWatch cw_collect lease is already held"
                )
            result = asyncio.run(collector.run())
            if not isinstance(result, dict) or result.get("status") != "success":
                raise CensorwatchTaskError(
                    "CensorWatch collector did not complete successfully"
                )
            logger.info(
                "[censorwatch] cw_collect(%s): success (%s records)",
                source_name,
                result.get("records_collected", 0),
            )
            return {"task": "cw_collect", "source": source_name, **result}
    except Exception as exc:
        _retry_task(self, task_name="cw_collect", error=exc)


@app.task(
    bind=True,
    name="censorwatch.tasks.cw_recheck",
    max_retries=2,
    default_retry_delay=120,
    soft_time_limit=_TASK_LIMITS["cw_recheck"]["soft"],
    time_limit=_TASK_LIMITS["cw_recheck"]["hard"],
)
def cw_recheck(
    self,
    cohort: str = "fresh",
    min_age_hours: float = 0,
    max_age_hours: float = 6,
):
    """Re-check pending posts after a source liveness gate."""
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_recheck")
    cohort = _identifier(cohort)
    if cohort not in {"fresh", "aging", "mature"}:
        return {"task": "cw_recheck", "status": "skipped", "note": "invalid cohort"}

    try:
        from censorwatch.detector import recheck_source
        from censorwatch.registry import enabled_sources

        async def _run():
            out = []
            for source_name in enabled_sources():
                out.append(
                    await recheck_source(
                        source_name,
                        cohort=cohort,
                        min_age_hours=min_age_hours,
                        max_age_hours=max_age_hours,
                        observation_run_id=getattr(self.request, "id", None),
                    )
                )
            return out

        with _task_lease("cw_recheck", identity=cohort) as acquired:
            if not acquired:
                raise CensorwatchTaskError(
                    "CensorWatch cw_recheck lease is already held"
                )
            results = asyncio.run(_run())
            if not results:
                return {
                    "task": "cw_recheck",
                    "cohort": cohort,
                    "status": "abstain",
                    "reason": "no_enabled_sources",
                    "confirmed": 0,
                    "sources": [],
                }
            if any(
                not isinstance(result, dict)
                or result.get("status") in {"error", "failed"}
                for result in results
            ):
                raise CensorwatchTaskError(
                    "CensorWatch detector did not complete successfully"
                )
            confirmed = sum(result.get("confirmed", 0) for result in results)
            degraded = any(result.get("liveness") != "healthy" for result in results)
            logger.info(
                "[censorwatch] cw_recheck(cohort=%s): %d sources, %d confirmed",
                cohort,
                len(results),
                confirmed,
            )
            return {
                "task": "cw_recheck",
                "cohort": cohort,
                "status": "degraded" if degraded else "ok",
                "confirmed": confirmed,
                "sources": results,
            }
    except Exception as exc:
        _retry_task(self, task_name="cw_recheck", error=exc)


@app.task(
    bind=True,
    name="censorwatch.tasks.cw_signal",
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=_TASK_LIMITS["cw_signal"]["soft"],
    time_limit=_TASK_LIMITS["cw_signal"]["hard"],
)
def cw_signal(self):
    """Recompute and persist deletion velocity plus spike flags."""
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_signal")
    try:
        from censorwatch.signal import run_signal

        with _task_lease("cw_signal") as acquired:
            if not acquired:
                raise CensorwatchTaskError(
                    "CensorWatch cw_signal lease is already held"
                )
            signal = run_signal()
            if not isinstance(signal, dict) or signal.get("status") not in {
                "ok",
                "abstain",
            }:
                raise CensorwatchTaskError(
                    "CensorWatch signal did not complete successfully"
                )
            return {
                "task": "cw_signal",
                "status": signal["status"],
                "reason": signal.get("reason"),
                "observed_posts": signal.get("observed_posts"),
                "n_deletions": signal["n_deletions"],
                "n_spikes": signal["n_spikes"],
                "top_term": signal["top_term"],
            }
    except Exception as exc:
        _retry_task(self, task_name="cw_signal", error=exc)


@app.task(
    bind=True,
    name="censorwatch.tasks.cw_heartbeat",
    max_retries=2,
    default_retry_delay=20,
    soft_time_limit=_TASK_LIMITS["cw_heartbeat"]["soft"],
    time_limit=_TASK_LIMITS["cw_heartbeat"]["hard"],
)
def cw_heartbeat(self):
    """Write beat freshness through the control-only exact-key authority.

    This task has a dedicated queue and a concurrency-one worker. It is
    intentionally not wrapped in the data-plane lease helper: doing so would
    give the control worker the hostile lane's general cache-writer secret.
    Repeated heartbeat writes are idempotent and carry their own short TTL.
    """
    settings = get_settings()
    if not settings.enabled:
        return _disabled_result("cw_heartbeat")
    try:
        from censorwatch.cache import open_control_cache

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = json.dumps(
            {"timestamp": timestamp}, separators=(",", ":"), sort_keys=True
        )
        cache = open_control_cache()
        try:
            if not cache.set(
                _HEARTBEAT_KEY,
                payload,
                ex=_HEARTBEAT_TTL_SECONDS,
            ):
                raise CensorwatchTaskError(
                    "CensorWatch heartbeat persistence failed"
                )
        finally:
            _close_cache(cache)
        return {
            "task": "cw_heartbeat",
            "status": "ok",
            "timestamp": timestamp,
        }
    except Exception as exc:
        _retry_task(self, task_name="cw_heartbeat", error=exc)
