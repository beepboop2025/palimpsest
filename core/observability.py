"""Local control-plane observations for the always-on measurement node.

This module deliberately keeps three questions separate:

* are PostgreSQL and Redis reachable (readiness)?
* are scheduled collector runs completing (pipeline health)?
* are the committed evidence snapshots current (evidence freshness)?

That separation prevents a recently successful no-op or abstention from being
reported as fresh evidence.  The public API only receives bounded status data;
collector exception text and connection details never cross this boundary.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


UTC = timezone.utc
_TIMESTAMP_FIELDS = (
    "generated_at",
    "observed_at",
    "as_of",
    "asof",
    "timestamp",
    "collected_at",
    "updated_at",
)
_PIPELINE_SUCCESS = {"success", "ok"}
_PIPELINE_DEGRADED = {"abstained", "skipped", "halted", "disabled"}
QUEUE_HEARTBEAT_MAX_AGE_SECONDS = 150
DEFAULT_EXECUTION_QUEUES = ("default", "collectors")


@dataclass(frozen=True)
class CollectorSpec:
    """The monitoring contract exported by the collector registry."""

    source: str
    output_path: str | None
    cadence_seconds: int
    grace_seconds: int
    task_name: str

    @property
    def freshness_budget_seconds(self) -> int:
        return self.cadence_seconds + self.grace_seconds


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: Any) -> datetime | None:
    """Parse a persisted timestamp without accepting booleans or NaN values."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return None
        try:
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _bounded_int(value: Any, *, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _normalise_spec(raw: Mapping[str, Any] | CollectorSpec) -> CollectorSpec:
    if isinstance(raw, CollectorSpec):
        return raw
    source = str(raw.get("source") or "").strip()
    task_name = str(raw.get("task_name") or "").strip()
    if not source or not task_name:
        raise ValueError("collector specs require source and task_name")
    cadence = _bounded_int(raw.get("cadence_seconds"), default=0, minimum=1)
    if cadence <= 0:
        raise ValueError("collector specs require a positive cadence_seconds")
    grace = _bounded_int(raw.get("grace_seconds"), default=max(300, cadence // 2))
    output = raw.get("output_path")
    return CollectorSpec(
        source=source,
        output_path=str(output) if output else None,
        cadence_seconds=cadence,
        grace_seconds=grace,
        task_name=task_name,
    )


def load_collector_specs(
    profile: str | None = None,
    *,
    include_collectors: bool = True,
    include_warehouse: bool | None = None,
) -> tuple[CollectorSpec, ...]:
    """Load enabled acquisition contracts into one observability registry."""

    raw: list[Mapping[str, Any]] = []
    if include_collectors:
        from core.collector_fleet import expected_collector_specs

        raw.extend(expected_collector_specs(profile))
    if include_warehouse is None:
        from core.ooni_warehouse import warehouse_enabled

        include_warehouse = warehouse_enabled()
    if include_warehouse:
        from core.ooni_warehouse import expected_warehouse_specs

        raw.extend(expected_warehouse_specs())
    specs = tuple(_normalise_spec(item) for item in raw)
    sources = [spec.source for spec in specs]
    if len(sources) != len(set(sources)):
        raise ValueError("collector specs contain duplicate sources")
    return specs


def _log_names(spec: CollectorSpec) -> tuple[str, ...]:
    """Return durable-log names for a schedule entry.

    The cheap DDTI feed-head task is scheduled as ``ddti-feed-head`` but its
    BaseCollector audit rows predate the fleet and retain the stable source name
    ``ddti_probe``.  Supporting both avoids rewriting historical audit data.
    """

    if spec.task_name == "core.tasks.collect_ddti_feed_head":
        return (spec.source, "ddti-feed-head", "ddti_probe")
    return (spec.source,)


def read_latest_collection_logs(
    specs: Sequence[CollectorSpec],
    *,
    session_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Read one newest ``CollectionLog`` row per expected collector.

    A grouped max query keeps this bounded as the append-only audit table grows.
    Returned values are ORM rows used only inside the process; error text is
    intentionally never copied into a control-plane response.
    """

    if not specs:
        return {}
    if session_factory is None:
        from api.database import SessionLocal

        session_factory = SessionLocal

    from sqlalchemy import and_, func, select
    from storage.models import CollectionLog

    aliases = {spec.source: _log_names(spec) for spec in specs}
    requested = sorted({name for names in aliases.values() for name in names})
    newest = (
        select(
            CollectionLog.source.label("source"),
            func.max(CollectionLog.run_at).label("run_at"),
        )
        .where(CollectionLog.source.in_(requested))
        .group_by(CollectionLog.source)
        .subquery()
    )
    statement = select(CollectionLog).join(
        newest,
        and_(
            CollectionLog.source == newest.c.source,
            CollectionLog.run_at == newest.c.run_at,
        ),
    )

    db = session_factory()
    try:
        rows = list(db.execute(statement).scalars().all())
    finally:
        db.close()

    newest_by_log_name: dict[str, Any] = {}
    for row in rows:
        current = newest_by_log_name.get(row.source)
        current_id = getattr(current, "id", -1) if current is not None else -1
        if current is None or int(getattr(row, "id", 0) or 0) >= int(current_id or 0):
            newest_by_log_name[row.source] = row

    out: dict[str, Any] = {}
    for spec in specs:
        candidates = [newest_by_log_name[name] for name in aliases[spec.source]
                      if name in newest_by_log_name]
        if candidates:
            out[spec.source] = max(
                candidates,
                key=lambda row: _as_utc(getattr(row, "run_at", None))
                or datetime.min.replace(tzinfo=UTC),
            )
    return out


def _read_evidence_timestamp(path: Path) -> tuple[datetime | None, str]:
    """Return a snapshot timestamp and its provenance, without returning data."""

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        path.stat()
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, ValueError, TypeError):
        return None, "invalid"

    if not isinstance(doc, dict):
        return None, "invalid"
    for field in _TIMESTAMP_FIELDS:
        parsed = _as_utc(doc.get(field))
        if parsed is not None:
            return parsed, field
    # File mtimes are deployment metadata, not evidence time: rsync, image
    # extraction, and git checkout can all make an old snapshot look new.
    return None, "undated"


def _source_pipeline_status(
    spec: CollectorSpec,
    log: Any | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    if log is None:
        return {
            "state": "no-data",
            "last_run_at": None,
            "age_seconds": None,
            "records_collected": None,
            "duration_seconds": None,
        }

    run_at = _as_utc(getattr(log, "run_at", None))
    signed_age = (now - run_at).total_seconds() if run_at else None
    future = signed_age is not None and signed_age < -300
    age = max(0.0, signed_age) if signed_age is not None and not future else None
    raw_status = str(getattr(log, "status", "") or "").strip().lower()
    overdue = age is None or age > spec.freshness_budget_seconds
    if future:
        state = "invalid"
    elif overdue:
        state = "overdue"
    elif raw_status in _PIPELINE_SUCCESS:
        state = "healthy"
    elif raw_status in _PIPELINE_DEGRADED:
        state = raw_status
    else:
        state = "failed"

    records = _bounded_int(getattr(log, "records_collected", None), default=0)
    try:
        duration = max(0.0, float(getattr(log, "duration_seconds", 0) or 0))
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "state": state,
        "last_run_at": _iso(run_at),
        "age_seconds": round(age, 3) if age is not None else None,
        "records_collected": records,
        "duration_seconds": round(duration, 3),
    }


def _source_evidence_status(
    spec: CollectorSpec,
    *,
    root: Path,
    now: datetime,
) -> dict[str, Any]:
    base = {
        "cadence_seconds": spec.cadence_seconds,
        "grace_seconds": spec.grace_seconds,
    }
    if spec.output_path is None:
        return {**base, "state": "not-applicable", "observed_at": None, "age_seconds": None}

    path = Path(spec.output_path)
    if not path.is_absolute():
        path = root / path
    observed_at, provenance = _read_evidence_timestamp(path)
    if observed_at is None:
        return {
            **base,
            "state": provenance,
            "observed_at": None,
            "age_seconds": None,
        }

    signed_age = (now - observed_at).total_seconds()
    if signed_age < -300:
        state = "invalid"
        age: float | None = None
    else:
        age = max(0.0, signed_age)
        state = "fresh" if age <= spec.freshness_budget_seconds else "stale"
    return {
        **base,
        "state": state,
        "observed_at": _iso(observed_at),
        "age_seconds": round(age, 3) if age is not None else None,
        "timestamp_source": provenance,
    }


def _count_states(sources: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in sources.values():
        state = str(item.get("state", "unknown"))
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))


def read_queue_heartbeats(
    *,
    queues: Sequence[str] = DEFAULT_EXECUTION_QUEUES,
    redis_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Read bounded queue heartbeat values from Redis.

    The task payload also contains a worker hostname, but this function returns
    the raw value only to the in-process parser below; the public status shape
    has no field into which that hostname can flow.
    """

    if redis_factory is None:
        import os
        import redis

        def redis_factory():
            return redis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )

    client = redis_factory()
    try:
        return {
            queue: client.get(f"palimpsest:queue-heartbeat:{queue}")
            for queue in queues
        }
    finally:
        client.close()


def _heartbeat_timestamp(raw: Any) -> datetime | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, Mapping):
        return None
    return _as_utc(raw.get("timestamp"))


def build_execution_status(
    heartbeats: Mapping[str, Any],
    *,
    now: datetime | None = None,
    queues: Sequence[str] = DEFAULT_EXECUTION_QUEUES,
    storage_available: bool = True,
    max_age_seconds: int = QUEUE_HEARTBEAT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Summarise Beat/broker/worker heartbeats without exposing worker identity."""

    observed_now = _as_utc(now) or _utc_now()
    queue_status: dict[str, dict[str, Any]] = {}
    for queue in queues:
        raw = heartbeats.get(queue)
        if raw is None:
            queue_status[queue] = {
                "state": "missing",
                "last_heartbeat_at": None,
                "age_seconds": None,
            }
            continue
        heartbeat_at = _heartbeat_timestamp(raw)
        if heartbeat_at is None:
            queue_status[queue] = {
                "state": "stale",
                "last_heartbeat_at": None,
                "age_seconds": None,
            }
            continue
        signed_age = (observed_now - heartbeat_at).total_seconds()
        valid_clock = signed_age >= -300
        age = max(0.0, signed_age) if valid_clock else None
        state = (
            "fresh"
            if age is not None and age <= max(1, int(max_age_seconds))
            else "stale"
        )
        queue_status[queue] = {
            "state": state,
            "last_heartbeat_at": _iso(heartbeat_at),
            "age_seconds": round(age, 3) if age is not None else None,
        }

    counts = _count_states(queue_status)
    if not storage_available:
        status = "unavailable"
    elif queue_status and counts.get("fresh", 0) == len(queue_status):
        status = "healthy"
    else:
        status = "degraded"
    return {
        "status": status,
        "storage_available": bool(storage_available),
        "counts": counts,
        "queues": queue_status,
    }


def build_node_status(
    specs: Sequence[Mapping[str, Any] | CollectorSpec],
    logs: Mapping[str, Any],
    *,
    root: Path | str,
    now: datetime | None = None,
    collectors_enabled: bool = True,
    profile: str = "standard",
    pipeline_storage_available: bool = True,
    queue_heartbeats: Mapping[str, Any] | None = None,
    execution_queues: Sequence[str] = DEFAULT_EXECUTION_QUEUES,
    execution_storage_available: bool = True,
) -> dict[str, Any]:
    """Build the bounded, serialisable node status document.

    This is a pure assembly function apart from reading the declared evidence
    files, which makes status semantics straightforward to test offline.
    """

    observed_now = _as_utc(now) or _utc_now()
    normalised = tuple(_normalise_spec(spec) for spec in specs)
    repo = Path(root)

    pipeline_sources = {
        spec.source: _source_pipeline_status(spec, logs.get(spec.source), now=observed_now)
        for spec in normalised
    }
    evidence_sources = {
        spec.source: _source_evidence_status(spec, root=repo, now=observed_now)
        for spec in normalised
    }
    pipeline_counts = _count_states(pipeline_sources)
    evidence_counts = _count_states(evidence_sources)
    execution = build_execution_status(
        queue_heartbeats or {},
        now=observed_now,
        queues=execution_queues,
        storage_available=execution_storage_available,
    )

    pipeline_states = {
        str(item.get("state", "unknown")) for item in pipeline_sources.values()
    }
    operational_states = {"healthy", "abstained"}
    if not collectors_enabled:
        pipeline_state = "disabled"
    elif not pipeline_storage_available:
        pipeline_state = "unavailable"
    elif not pipeline_sources or pipeline_states == {"no-data"}:
        pipeline_state = "no-data"
    elif pipeline_states <= operational_states:
        pipeline_state = "healthy"
    else:
        pipeline_state = "degraded"

    applicable_evidence = sum(
        count for state, count in evidence_counts.items() if state != "not-applicable"
    )
    fresh = evidence_counts.get("fresh", 0)
    if applicable_evidence == 0:
        evidence_state = "no-data"
    elif fresh == applicable_evidence:
        evidence_state = "fresh"
    elif fresh == 0:
        evidence_state = "stale-or-missing"
    else:
        evidence_state = "degraded"

    if not collectors_enabled:
        node_state = "disabled"
    elif (
        pipeline_state == "healthy"
        and evidence_state == "fresh"
        and execution["status"] == "healthy"
    ):
        node_state = "healthy"
    else:
        node_state = "degraded"

    return {
        "status": node_state,
        "generated_at": _iso(observed_now),
        "collectors_enabled": bool(collectors_enabled),
        "profile": profile,
        "pipeline": {
            "status": pipeline_state,
            "storage_available": bool(pipeline_storage_available),
            "counts": pipeline_counts,
            "sources": pipeline_sources,
        },
        "evidence": {
            "status": evidence_state,
            "counts": evidence_counts,
            "sources": evidence_sources,
        },
        "execution": execution,
    }


def collect_node_status(
    *,
    session_factory: Callable[[], Any] | None = None,
    specs_provider: Callable[[str | None], Iterable[Mapping[str, Any] | CollectorSpec]] | None = None,
    heartbeat_provider: Callable[[], Mapping[str, Any]] | None = None,
    root: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect the production node status, degrading safely if audit DB reads fail."""

    from core.collector_fleet import collection_profile, collectors_enabled
    from core.ooni_warehouse import warehouse_enabled

    passive_enabled = collectors_enabled()
    bulk_enabled = warehouse_enabled()
    enabled = passive_enabled or bulk_enabled
    profile = collection_profile() if passive_enabled or not bulk_enabled else "warehouse"
    raw_specs = (
        tuple(specs_provider(profile))
        if specs_provider is not None
        else load_collector_specs(
            profile if passive_enabled else None,
            # Retain the historical disabled registry when no acquisition leg
            # is on; in warehouse-only mode do not report every passive job as
            # missing merely because its separate profile is disabled.
            include_collectors=passive_enabled or not enabled,
            include_warehouse=bulk_enabled,
        )
    )
    specs = tuple(_normalise_spec(item) for item in raw_specs)
    storage_available = True
    try:
        logs = read_latest_collection_logs(specs, session_factory=session_factory)
    except Exception:
        logs = {}
        storage_available = False

    execution_queues = ["default"]
    if passive_enabled:
        execution_queues.append("collectors")
    if bulk_enabled:
        execution_queues.append("warehouse")
    execution_storage_available = True
    try:
        heartbeats = dict(
            heartbeat_provider()
            if heartbeat_provider is not None
            else read_queue_heartbeats(queues=execution_queues)
        )
    except Exception:
        heartbeats = {}
        execution_storage_available = False

    if root is None:
        root = Path(__file__).resolve().parent.parent
    return build_node_status(
        specs,
        logs,
        root=root,
        now=now,
        collectors_enabled=enabled,
        profile=profile,
        pipeline_storage_available=storage_available,
        queue_heartbeats=heartbeats,
        execution_queues=execution_queues,
        execution_storage_available=execution_storage_available,
    )


def check_readiness(
    *,
    session_factory: Callable[[], Any] | None = None,
    redis_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Probe the two stateful dependencies and return only boolean outcomes."""

    from sqlalchemy import text

    if session_factory is None:
        from api.database import SessionLocal

        session_factory = SessionLocal
    if redis_factory is None:
        import os
        import redis

        def redis_factory():
            return redis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                socket_connect_timeout=2,
                socket_timeout=2,
            )

    dependencies = {"postgres": False, "redis": False}
    db = None
    try:
        db = session_factory()
        db.execute(text("SELECT 1"))
        dependencies["postgres"] = True
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    client = None
    try:
        client = redis_factory()
        dependencies["redis"] = bool(client.ping())
    except Exception:
        pass
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    return {
        "status": "ready" if all(dependencies.values()) else "not-ready",
        "dependencies": dependencies,
    }


def _metric_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_prometheus_metrics(
    status: Mapping[str, Any],
    readiness: Mapping[str, Any],
) -> str:
    """Render a deterministic, low-cardinality Prometheus exposition."""

    dependencies = readiness.get("dependencies", {})
    ready = readiness.get("status") == "ready"
    lines = [
        "# HELP palimpsest_node_ready Whether all required node dependencies are reachable.",
        "# TYPE palimpsest_node_ready gauge",
        f"palimpsest_node_ready {1 if ready else 0}",
        "# HELP palimpsest_dependency_ready Whether a required dependency is reachable.",
        "# TYPE palimpsest_dependency_ready gauge",
    ]
    for name in ("postgres", "redis"):
        lines.append(
            f'palimpsest_dependency_ready{{dependency="{name}"}} '
            f'{1 if dependencies.get(name) is True else 0}'
        )

    lines.extend([
        "# HELP palimpsest_pipeline_sources Number of collector pipelines by current state.",
        "# TYPE palimpsest_pipeline_sources gauge",
    ])
    pipeline = status.get("pipeline", {}) if isinstance(status, Mapping) else {}
    for state, count in sorted((pipeline.get("counts") or {}).items()):
        lines.append(
            f'palimpsest_pipeline_sources{{state="{_metric_label(state)}"}} {int(count)}'
        )

    lines.extend([
        "# HELP palimpsest_evidence_sources Number of evidence snapshots by freshness state.",
        "# TYPE palimpsest_evidence_sources gauge",
    ])
    evidence = status.get("evidence", {}) if isinstance(status, Mapping) else {}
    for state, count in sorted((evidence.get("counts") or {}).items()):
        lines.append(
            f'palimpsest_evidence_sources{{state="{_metric_label(state)}"}} {int(count)}'
        )

    lines.extend([
        "# HELP palimpsest_collector_last_run_age_seconds Age of a collector's newest audit row.",
        "# TYPE palimpsest_collector_last_run_age_seconds gauge",
        "# HELP palimpsest_evidence_age_seconds Age of the latest committed evidence snapshot.",
        "# TYPE palimpsest_evidence_age_seconds gauge",
    ])
    pipeline_sources = pipeline.get("sources") or {}
    evidence_sources = evidence.get("sources") or {}
    for source in sorted(set(pipeline_sources) | set(evidence_sources)):
        label = _metric_label(source)
        run_age = (pipeline_sources.get(source) or {}).get("age_seconds")
        if isinstance(run_age, (int, float)) and not isinstance(run_age, bool):
            lines.append(
                f'palimpsest_collector_last_run_age_seconds{{source="{label}"}} {float(run_age):.3f}'
            )
        evidence_age = (evidence_sources.get(source) or {}).get("age_seconds")
        if isinstance(evidence_age, (int, float)) and not isinstance(evidence_age, bool):
            lines.append(
                f'palimpsest_evidence_age_seconds{{source="{label}"}} {float(evidence_age):.3f}'
            )

    lines.extend([
        "# HELP palimpsest_queue_heartbeat_up Whether a named worker queue heartbeat is fresh.",
        "# TYPE palimpsest_queue_heartbeat_up gauge",
        "# HELP palimpsest_queue_heartbeat_age_seconds Age of a named worker queue heartbeat.",
        "# TYPE palimpsest_queue_heartbeat_age_seconds gauge",
    ])
    execution = status.get("execution", {}) if isinstance(status, Mapping) else {}
    for queue, queue_status in sorted((execution.get("queues") or {}).items()):
        label = _metric_label(queue)
        lines.append(
            f'palimpsest_queue_heartbeat_up{{queue="{label}"}} '
            f'{1 if queue_status.get("state") == "fresh" else 0}'
        )
        heartbeat_age = queue_status.get("age_seconds")
        if isinstance(heartbeat_age, (int, float)) and not isinstance(heartbeat_age, bool):
            lines.append(
                f'palimpsest_queue_heartbeat_age_seconds{{queue="{label}"}} '
                f'{float(heartbeat_age):.3f}'
            )
    return "\n".join(lines) + "\n"
