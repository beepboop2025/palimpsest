"""Celery tasks for the DDTI index and the opt-in passive collector fleet."""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from core.scheduler import app

logger = logging.getLogger(__name__)


def _alert_webhook_is_public_https(url: str) -> bool:
    """Reject credentialed, non-TLS, and private alert destinations."""

    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            return False
        addresses = socket.getaddrinfo(parts.hostname, parts.port or 443)
        if not addresses:
            return False
        for _family, _kind, _proto, _canon, sockaddr in addresses:
            address = ipaddress.ip_address(sockaddr[0])
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                return False
        return True
    except (OSError, ValueError):
        return False


def _run_with_lease(
    name: str,
    operation: Callable[[], dict],
    *,
    timeout_s: int,
    collector_name: str | None = None,
) -> dict:
    """Run once across the fleet, or skip when a previous run still owns the lease.

    Beat schedules the intent; the Redis lease enforces non-overlap after slow
    sources, worker restarts, or manual invocations.  Redis is already the task
    broker, so failing to acquire it is a real infrastructure failure and the
    safe response is to collect nothing.
    """

    import redis

    source = collector_name or name

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
                "collector": source,
                "status": "skipped",
                "records_collected": 0,
                "error": "previous run still holds the collector lease",
            }
        return operation()
    except Exception as exc:
        logger.exception("[%s] collector task failed", name)
        return {
            "collector": source,
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
    """Persist one terminal outcome and its optional artifact (best effort)."""

    try:
        from api.database import SessionLocal
        from storage.models import CollectionLog, ObservationArtifact

        db = SessionLocal()
        try:
            db.add(CollectionLog(
                source=str(result.get("collector", "snapshot"))[:64],
                status=str(result.get("status", "failed"))[:16],
                records_collected=int(result.get("records_collected", 0) or 0),
                duration_seconds=float(result.get("duration_seconds", 0) or 0),
                error_message=(str(result.get("error") or "")[:1000] or None),
            ))
            artifact = result.get("artifact")
            if isinstance(artifact, dict) and artifact.get("sha256"):
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                statement = pg_insert(ObservationArtifact.__table__).values(
                    source=str(artifact.get("source") or result.get("collector"))[:64],
                    sha256=str(artifact["sha256"])[:64],
                    archive_path=str(artifact.get("archive_path") or ""),
                    generated_at=(str(artifact.get("generated_at"))[:128]
                                  if artifact.get("generated_at") is not None else None),
                    original_bytes=int(artifact.get("original_bytes", 0) or 0),
                    compressed_bytes=int(artifact.get("compressed_bytes", 0) or 0),
                    record_count=int(result.get("records_collected", 0) or 0),
                ).on_conflict_do_nothing(
                    constraint="uq_artifact_source_sha256"
                )
                db.execute(statement)
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("snapshot collection logging failed: %s", exc)


@app.task(
    bind=True,
    name="core.tasks.generate_ddti_index",
    max_retries=1,
    soft_time_limit=20 * 60,
    time_limit=25 * 60,
)
def generate_ddti_index(self) -> dict:
    """Recompute the Deletion-Differential Threat Index from recent deletions.

    Reads CDT deletion records (Article.category == 'ddti_deletion'), recomputes
    selectivity + novelty, publishes to Redis (ddti:index:latest), and writes one
    DDTIIndexSnapshot time-series row. Returns a structured status dict.
    """
    from processors.ddti_index import DDTIIndexProcessor

    def operation() -> dict:
        started = time.monotonic()
        raw = DDTIIndexProcessor().run()
        status = {
            "error": "failed",
            "abstain": "abstained",
        }.get(str(raw.get("status")), str(raw.get("status", "failed")))
        return {
            "collector": "ddti-index",
            "status": status,
            "records_collected": int(
                raw.get("observations", raw.get("terms", 0)) or 0
            ),
            "duration_seconds": round(time.monotonic() - started, 2),
            "error": str(raw.get("error") or raw.get("reason") or ""),
            "processor": raw,
        }

    result = _run_with_lease(
        "processor:ddti-index",
        operation,
        timeout_s=25 * 60,
        collector_name="ddti-index",
    )
    _log_snapshot_result(result)
    logger.info("[ddti] index run: %s", result)
    if result.get("status") == "failed":
        raise self.retry(
            exc=RuntimeError(result.get("error") or "DDTI index failed"),
            countdown=5 * 60,
        )
    return result


@app.task(name="core.tasks.queue_heartbeat", ignore_result=True)
def queue_heartbeat(queue: str) -> dict:
    """Prove the complete beat -> broker -> named worker queue path is alive."""

    if queue not in {"default", "collectors"}:
        raise ValueError(f"unknown heartbeat queue: {queue!r}")
    import json
    import redis

    now = datetime.now(timezone.utc).isoformat()
    client = redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )
    try:
        payload = {
            "queue": queue,
            "timestamp": now,
            "worker": socket.gethostname(),
        }
        client.set(
            f"palimpsest:queue-heartbeat:{queue}",
            json.dumps(payload, separators=(",", ":")),
            ex=180,
        )
        return payload
    finally:
        client.close()


@app.task(name="core.tasks.refresh_node_status")
def refresh_node_status() -> dict:
    """Materialize bounded node health and optionally alert on bad transitions."""

    import json
    import tempfile

    from core.observability import collect_node_status

    status = collect_node_status()
    root = Path(__file__).resolve().parent.parent
    configured = os.getenv("PALIMPSEST_STATUS_PATH", "").strip()
    destination = Path(configured) if configured else root / "data" / "node-status.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".node-status-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            # This document is explicitly secret-free and must be readable by
            # the host-side, unprivileged backup service across the bind-mount
            # UID boundary (container 10001 vs host operator 1001).
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

    # Redis holds only the last compact state for transition de-duplication;
    # PostgreSQL and the artifact archive remain the durable evidence stores.
    previous_alert = None
    try:
        import redis

        client = redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        try:
            previous_alert = client.get("palimpsest:last-alert-state")
            client.set("palimpsest:node-status-state", str(status.get("status", "unknown")))
            client.set("palimpsest:node-status", payload.decode(), ex=15 * 60)
            if str(status.get("status")) in {"healthy", "disabled"}:
                # Reset the transition latch so a later regression alerts even
                # if it returns to the same degraded state as an older incident.
                client.set("palimpsest:last-alert-state", str(status.get("status")))
        finally:
            client.close()
    except Exception:
        logger.warning("node status could not be cached in Redis")

    state = str(status.get("status", "unknown"))
    webhook = os.getenv("PALIMPSEST_ALERT_WEBHOOK_URL", "").strip()
    if (
        webhook
        and _alert_webhook_is_public_https(webhook)
        and state not in {"healthy", "disabled"}
        and previous_alert != state
    ):
        # Send a deliberately small, secret-free summary.  The detailed source
        # matrix stays on the localhost-only control API.
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None

        alert = {
            "service": "palimpsest",
            "status": state,
            "generated_at": status.get("generated_at"),
            "pipeline": (status.get("pipeline") or {}).get("counts", {}),
            "evidence": (status.get("evidence") or {}).get("counts", {}),
        }
        request = urllib.request.Request(
            webhook,
            data=json.dumps(alert, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        delivered = False
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=10) as response:
                response.read(1024)
            delivered = True
        except Exception:
            # Never log the exception: request errors can echo a credentialed
            # webhook URL. Operators still see task failure counters/status.
            logger.warning("node alert webhook delivery failed")
        if delivered:
            try:
                import redis

                client = redis.from_url(
                    os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                    decode_responses=True,
                )
                try:
                    client.set("palimpsest:last-alert-state", state)
                finally:
                    client.close()
            except Exception:
                # Delivery succeeded. Losing only the de-duplication latch may
                # repeat an alert, which is safer than suppressing an incident.
                logger.warning("node alert de-duplication state could not be stored")

    return {
        "status": state,
        "generated_at": status.get("generated_at"),
        "status_path": str(destination),
    }


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
    _log_snapshot_result(result)
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
        collector_name=name,
    )
    _log_snapshot_result(result)
    logger.info("[%s] snapshot run: %s", name, result)
    if result.get("status") == "failed":
        raise self.retry(
            exc=RuntimeError(result.get("error") or f"{name} snapshot failed"),
            countdown=10 * 60,
        )
    return result
