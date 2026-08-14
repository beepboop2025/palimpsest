"""Celery tasks for the DDTI index and the opt-in passive collector fleet."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from core.scheduler import app

logger = logging.getLogger(__name__)

_ALERT_STATE_KEY = "palimpsest:alert-conditions:v1"
_ALERT_STATE_SCHEMA = "palimpsest-alert-conditions.v1"
_ALERT_PAYLOAD_SCHEMA = "palimpsest-node-alert.v2"
_ALERT_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_ALERT_MAX_CONDITIONS = 128
_ALERT_MAX_TRANSITIONS = 64
_ALERT_MAX_PAYLOAD_BYTES = 16 * 1024


def _alert_identifier(value: object, *, fallback: str) -> str:
    """Return a bounded public identifier, never arbitrary status text."""

    candidate = str(value or "").strip().casefold()
    return candidate if _ALERT_IDENTIFIER.fullmatch(candidate) else fallback


def _node_alert_conditions(status: dict) -> dict[str, str]:
    """Flatten unhealthy node details into stable, secret-free condition keys."""

    node_state = _alert_identifier(status.get("status"), fallback="unknown")
    if node_state == "disabled":
        return {}
    conditions: dict[str, str] = {}
    sections = (
        ("pipeline", "sources", frozenset({"healthy", "abstained"})),
        ("evidence", "sources", frozenset({"fresh", "not-applicable"})),
        ("execution", "queues", frozenset({"fresh"})),
    )
    for scope, collection, healthy_states in sections:
        section = status.get(scope)
        entries = section.get(collection) if isinstance(section, dict) else None
        if isinstance(entries, dict):
            ordered_entries = sorted(entries.items(), key=lambda item: str(item[0]))
            for index, (raw_subject, raw_detail) in enumerate(ordered_entries):
                detail = raw_detail if isinstance(raw_detail, dict) else {}
                state = _alert_identifier(detail.get("state"), fallback="unknown")
                if state in healthy_states:
                    continue
                subject = _alert_identifier(raw_subject, fallback=f"invalid-{index + 1}")
                conditions[f"{scope}/{subject}"] = state

        if isinstance(section, dict) and section.get("storage_available") is False:
            conditions[f"{scope}/storage"] = "unavailable"

    # Preserve fail-loud behavior if a future status shape becomes degraded but
    # contains no source-level detail that this version understands.
    if node_state not in {"healthy", "disabled"} and not conditions:
        conditions["node/status"] = node_state

    ordered = dict(sorted(conditions.items()))
    if len(ordered) <= _ALERT_MAX_CONDITIONS:
        return ordered
    kept = dict(list(ordered.items())[: _ALERT_MAX_CONDITIONS - 1])
    kept["node/condition-overflow"] = "present"
    return kept


def _load_alert_conditions(raw: object) -> dict[str, str]:
    """Read the v1 Redis latch; old scalar latches intentionally alert once."""

    try:
        document = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    if not isinstance(document, dict) or document.get("schema_version") != _ALERT_STATE_SCHEMA:
        return {}
    values = document.get("conditions")
    if not isinstance(values, dict) or len(values) > _ALERT_MAX_CONDITIONS:
        return {}
    conditions: dict[str, str] = {}
    for raw_key, raw_state in values.items():
        if not isinstance(raw_key, str) or "/" not in raw_key or len(raw_key) > 140:
            return {}
        scope, subject = raw_key.split("/", 1)
        safe_scope = _alert_identifier(scope, fallback="")
        safe_subject = _alert_identifier(subject, fallback="")
        safe_state = _alert_identifier(raw_state, fallback="")
        if not safe_scope or not safe_subject or not safe_state:
            return {}
        conditions[f"{safe_scope}/{safe_subject}"] = safe_state
    return dict(sorted(conditions.items()))


def _dump_alert_conditions(conditions: dict[str, str]) -> str:
    return json.dumps(
        {"schema_version": _ALERT_STATE_SCHEMA, "conditions": conditions},
        sort_keys=True,
        separators=(",", ":"),
    )


def _alert_transition(
    status: dict, previous: dict[str, str]
) -> tuple[dict[str, str], list[dict[str, str]], list[str]]:
    """Return current conditions plus newly opened/changed and resolved keys."""

    current = _node_alert_conditions(status)
    opened = [
        {"condition": condition, "state": state}
        for condition, state in current.items()
        if previous.get(condition) != state
    ]
    resolved = [condition for condition in previous if condition not in current]
    return current, opened, sorted(resolved)


def _bounded_alert_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for index, (raw_name, raw_count) in enumerate(sorted(value.items())):
        if index == 16:
            break
        name = _alert_identifier(raw_name, fallback=f"unknown-{index + 1}")
        if type(raw_count) is int:
            out[name] = min(max(raw_count, 0), 1_000_000)
    return out


def _node_alert_payload(
    status: dict,
    current: dict[str, str],
    opened: list[dict[str, str]],
    resolved: list[str],
) -> bytes:
    """Build a size-capped summary containing no exception or observation text."""

    generated_at = status.get("generated_at")
    try:
        parsed_generated_at = datetime.fromisoformat(
            generated_at.replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError):
        generated_at = None
    else:
        if (
            parsed_generated_at.tzinfo is None
            or parsed_generated_at.utcoffset() is None
        ):
            generated_at = None
        else:
            generated_at = (
                parsed_generated_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
    payload = {
        "schema_version": _ALERT_PAYLOAD_SCHEMA,
        "service": "palimpsest",
        "status": _alert_identifier(status.get("status"), fallback="unknown"),
        "generated_at": generated_at,
        "active_count": len(current),
        "opened_count": len(opened),
        "resolved_count": len(resolved),
        "opened": opened[:_ALERT_MAX_TRANSITIONS],
        "resolved": resolved[:_ALERT_MAX_TRANSITIONS],
        "counts": {
            scope: _bounded_alert_counts((status.get(scope) or {}).get("counts"))
            for scope in ("pipeline", "evidence", "execution")
            if isinstance(status.get(scope), dict)
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _ALERT_MAX_PAYLOAD_BYTES:
        # Defensive fallback: the fixed identifiers/counts retain the incident
        # signal even if a future schema makes a supposedly bounded field grow.
        payload.pop("counts", None)
        payload["opened"] = payload["opened"][:16]
        payload["resolved"] = payload["resolved"][:16]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _ALERT_MAX_PAYLOAD_BYTES:
        raise ValueError("node alert payload exceeds its fixed byte ceiling")
    return encoded


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

    if queue not in {"default", "collectors", "warehouse"}:
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

    # Redis holds only a compact condition map for transition de-duplication;
    # PostgreSQL and the artifact archive remain the durable evidence stores.
    previous_conditions: dict[str, str] = {}
    alert_state_available = False
    try:
        import redis

        client = redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        try:
            previous_conditions = _load_alert_conditions(client.get(_ALERT_STATE_KEY))
            alert_state_available = True
            client.set("palimpsest:node-status-state", str(status.get("status", "unknown")))
            client.set("palimpsest:node-status", payload.decode(), ex=15 * 60)
        finally:
            client.close()
    except Exception:
        logger.warning("node status could not be cached in Redis")

    state = str(status.get("status", "unknown"))
    current_conditions, opened, resolved = _alert_transition(
        status, previous_conditions
    )
    webhook = os.getenv("PALIMPSEST_ALERT_WEBHOOK_URL", "").strip()
    delivered = False
    if webhook and _alert_webhook_is_public_https(webhook) and opened:
        # Send a deliberately small, secret-free summary.  The detailed source
        # matrix stays on the localhost-only control API.
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None

        request = urllib.request.Request(
            webhook,
            data=_node_alert_payload(status, current_conditions, opened, resolved),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=10) as response:
                response.read(1024)
            delivered = True
        except Exception:
            # Never log the exception: request errors can echo a credentialed
            # webhook URL. Operators still see task failure counters/status.
            logger.warning("node alert webhook delivery failed")

    # Resolutions update silently. New/changed faults are stored only after a
    # successful delivery; a broken webhook or missing Redis therefore repeats
    # an alert instead of suppressing an incident.
    should_store = alert_state_available and (not opened or delivered)
    if should_store and current_conditions != previous_conditions:
        try:
            import redis

            client = redis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
            )
            try:
                client.set(_ALERT_STATE_KEY, _dump_alert_conditions(current_conditions))
            finally:
                client.close()
        except Exception:
            # Losing only the de-duplication latch may repeat an alert, which is
            # safer than suppressing a new or recurring incident.
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


@app.task(
    bind=True,
    name="core.tasks.ingest_ooni_bulk_hour",
    max_retries=1,
    soft_time_limit=50 * 60,
    time_limit=55 * 60,
)
def ingest_ooni_bulk_hour(self, hour: str | None = None) -> dict:
    """Ingest one explicit or configured-lagged OONI S3 archive hour."""

    from collectors.ooni_bulk import (
        format_hour,
        ingest_hour,
        latest_lagged_hour,
        load_config,
        parse_hour,
    )

    # Freeze the default hour before the first attempt.  If Celery retries over
    # an hour boundary it resumes this manifest instead of silently moving on.
    target = parse_hour(hour) if hour is not None else latest_lagged_hour(load_config())
    target_text = format_hour(target)
    result = _run_with_lease(
        "warehouse:ooni-bulk",
        lambda: ingest_hour(hour=target),
        timeout_s=55 * 60,
        collector_name="ooni-bulk",
    )
    _log_snapshot_result(result)
    logger.info("[ooni-bulk] warehouse run: %s", result)
    if result.get("status") == "failed":
        raise self.retry(
            exc=RuntimeError(result.get("error") or "OONI bulk ingest failed"),
            countdown=10 * 60,
            kwargs={"hour": target_text},
        )
    return result
