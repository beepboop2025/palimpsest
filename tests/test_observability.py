from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import core.observability as observability

from core.observability import (
    CollectorSpec,
    build_execution_status,
    build_node_status,
    check_readiness,
    render_prometheus_metrics,
)


UTC = timezone.utc


def _spec(source: str, output: str | None) -> CollectorSpec:
    return CollectorSpec(
        source=source,
        output_path=output,
        cadence_seconds=3600,
        grace_seconds=900,
        task_name="core.tasks.refresh_public_snapshot",
    )


def _log(*, when: datetime, status: str = "success", error: str | None = None):
    return SimpleNamespace(
        id=1,
        status=status,
        run_at=when,
        records_collected=17,
        duration_seconds=2.25,
        error_message=error,
    )


def test_status_separates_pipeline_health_from_evidence_freshness(tmp_path):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    fresh = tmp_path / "fresh.json"
    stale = tmp_path / "stale.json"
    fresh.write_text(json.dumps({"generated_at": (now - timedelta(minutes=5)).isoformat()}))
    stale.write_text(json.dumps({"generated_at": (now - timedelta(hours=4)).isoformat()}))
    specs = (_spec("fresh-source", fresh.name), _spec("stale-source", stale.name))
    logs = {
        spec.source: _log(
            when=now - timedelta(minutes=10),
            error="password=super-secret host=private-db",
        )
        for spec in specs
    }

    status = build_node_status(
        specs,
        logs,
        root=tmp_path,
        now=now,
        collectors_enabled=True,
        profile="vigorous",
    )

    assert status["pipeline"]["status"] == "healthy"
    assert status["pipeline"]["counts"] == {"healthy": 2}
    assert status["evidence"]["status"] == "degraded"
    assert status["evidence"]["sources"]["fresh-source"]["state"] == "fresh"
    assert status["evidence"]["sources"]["stale-source"]["state"] == "stale"
    assert "secret" not in json.dumps(status)


def test_status_distinguishes_abstention_missing_and_not_applicable(tmp_path):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    specs = (
        _spec("abstaining", "missing.json"),
        _spec("feed-head", None),
    )
    status = build_node_status(
        specs,
        {"abstaining": _log(when=now, status="abstained")},
        root=tmp_path,
        now=now,
    )

    assert status["pipeline"]["sources"]["abstaining"]["state"] == "abstained"
    assert status["pipeline"]["sources"]["feed-head"]["state"] == "no-data"
    assert status["evidence"]["sources"]["abstaining"]["state"] == "missing"
    assert status["evidence"]["sources"]["feed-head"]["state"] == "not-applicable"


def test_recent_abstention_is_operationally_healthy_but_remains_visible(tmp_path):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    evidence = tmp_path / "current.json"
    evidence.write_text(json.dumps({"generated_at": now.isoformat()}))

    status = build_node_status(
        (_spec("monthly-source", evidence.name),),
        {"monthly-source": _log(when=now, status="abstained")},
        root=tmp_path,
        now=now,
        queue_heartbeats={
            queue: {"timestamp": now.isoformat()}
            for queue in ("default", "collectors")
        },
    )

    assert status["pipeline"]["status"] == "healthy"
    assert status["pipeline"]["sources"]["monthly-source"]["state"] == "abstained"
    assert status["evidence"]["status"] == "fresh"
    assert status["status"] == "healthy"


def test_failed_only_pipeline_is_degraded_not_no_data(tmp_path):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)

    status = build_node_status(
        (_spec("failed-source", None),),
        {"failed-source": _log(when=now, status="failed")},
        root=tmp_path,
        now=now,
    )

    assert status["pipeline"]["status"] == "degraded"
    assert status["pipeline"]["sources"]["failed-source"]["state"] == "failed"


def test_future_or_invalid_evidence_is_not_reported_fresh(tmp_path):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    future = tmp_path / "future.json"
    broken = tmp_path / "broken.json"
    future.write_text(json.dumps({"generated_at": (now + timedelta(hours=2)).isoformat()}))
    broken.write_text("not-json")

    status = build_node_status(
        (_spec("clock-skew", future.name), _spec("broken", broken.name)),
        {},
        root=tmp_path,
        now=now,
    )

    assert status["evidence"]["sources"]["clock-skew"]["state"] == "invalid"
    assert status["evidence"]["sources"]["broken"]["state"] == "invalid"


def test_file_mtime_cannot_make_undated_evidence_look_fresh(tmp_path):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    undated = tmp_path / "undated.json"
    undated.write_text(json.dumps({"measurements": [1, 2, 3]}))

    status = build_node_status(
        (_spec("undated", undated.name),),
        {},
        root=tmp_path,
        now=now,
    )

    assert status["evidence"]["sources"]["undated"]["state"] == "undated"


def test_overdue_and_future_collection_logs_are_not_healthy(tmp_path):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    specs = (_spec("overdue", None), _spec("clock-skew", None))

    status = build_node_status(
        specs,
        {
            "overdue": _log(when=now - timedelta(hours=4)),
            "clock-skew": _log(when=now + timedelta(hours=2)),
        },
        root=tmp_path,
        now=now,
    )

    assert status["pipeline"]["sources"]["overdue"]["state"] == "overdue"
    assert status["pipeline"]["sources"]["clock-skew"]["state"] == "invalid"


def test_readiness_returns_booleans_and_closes_both_clients():
    closed = []

    class Session:
        def execute(self, statement):
            return 1

        def close(self):
            closed.append("postgres")

    class Redis:
        def ping(self):
            return True

        def close(self):
            closed.append("redis")

    result = check_readiness(session_factory=Session, redis_factory=Redis)

    assert result == {
        "status": "ready",
        "dependencies": {"postgres": True, "redis": True},
    }
    assert closed == ["postgres", "redis"]


def test_execution_heartbeats_prove_each_named_worker_queue_without_hostname():
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    heartbeats = {
        "default": json.dumps({
            "queue": "default",
            "timestamp": (now - timedelta(seconds=20)).isoformat(),
            "worker": "private-worker-hostname",
        }),
        "collectors": {
            "queue": "collectors",
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
            "worker": "another-private-hostname",
        },
    }

    execution = build_execution_status(heartbeats, now=now)

    assert execution["status"] == "degraded"
    assert execution["queues"]["default"]["state"] == "fresh"
    assert execution["queues"]["collectors"]["state"] == "stale"
    assert "worker" not in json.dumps(execution)
    assert "hostname" not in json.dumps(execution)


def test_collect_node_status_accepts_offline_heartbeat_provider(monkeypatch, tmp_path):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    monkeypatch.setenv("PALIMPSEST_COLLECTORS_ENABLED", "1")
    monkeypatch.setenv("PALIMPSEST_COLLECTION_PROFILE", "vigorous")
    monkeypatch.setattr(observability, "read_latest_collection_logs", lambda *a, **k: {})

    status = observability.collect_node_status(
        specs_provider=lambda profile: [{
            "source": "feed-head",
            "output_path": None,
            "cadence_seconds": 1800,
            "grace_seconds": 900,
            "task_name": "core.tasks.collect_ddti_feed_head",
        }],
        heartbeat_provider=lambda: {
            queue: {"timestamp": (now - timedelta(seconds=10)).isoformat()}
            for queue in ("default", "collectors")
        },
        root=tmp_path,
        now=now,
    )

    assert status["execution"]["status"] == "healthy"
    assert status["execution"]["counts"] == {"fresh": 2}
    assert set(status["execution"]["queues"]) == {"default", "collectors"}


def test_metrics_are_bounded_and_escape_source_labels():
    status = {
        "pipeline": {
            "counts": {"healthy": 1},
            "sources": {'odd"source\\name': {"age_seconds": 12.5}},
        },
        "evidence": {
            "counts": {"fresh": 1},
            "sources": {'odd"source\\name': {"age_seconds": 30}},
        },
        "execution": {
            "queues": {
                "default": {"state": "fresh", "age_seconds": 25},
                "collectors": {"state": "missing", "age_seconds": None},
            },
        },
    }
    readiness = {
        "status": "ready",
        "dependencies": {"postgres": True, "redis": True},
    }

    metrics = render_prometheus_metrics(status, readiness)

    assert "palimpsest_node_ready 1" in metrics
    assert 'state="healthy"} 1' in metrics
    assert 'source="odd\\"source\\\\name"' in metrics
    assert 'palimpsest_queue_heartbeat_up{queue="default"} 1' in metrics
    assert 'palimpsest_queue_heartbeat_up{queue="collectors"} 0' in metrics
    assert 'palimpsest_queue_heartbeat_age_seconds{queue="default"} 25.000' in metrics
    assert "password" not in metrics.lower()
    assert metrics.endswith("\n")
