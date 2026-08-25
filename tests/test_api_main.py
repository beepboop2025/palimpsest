from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.main import create_app


STATUS = {
    "status": "degraded",
    "generated_at": "2026-08-11T12:00:00Z",
    "pipeline": {
        "status": "healthy",
        "counts": {"healthy": 1},
        "sources": {"ooni-gfw": {"age_seconds": 42}},
    },
    "evidence": {
        "status": "stale-or-missing",
        "counts": {"stale": 1},
        "sources": {"ooni-gfw": {"age_seconds": 7200}},
    },
    "execution": {
        "status": "healthy",
        "counts": {"fresh": 2},
        "queues": {
            "default": {"state": "fresh", "age_seconds": 12},
            "collectors": {"state": "fresh", "age_seconds": 19},
        },
    },
}


def _client(*, ready: bool = True, status_provider=lambda: STATUS):
    dependencies = {"postgres": True, "redis": ready}
    app = create_app(
        status_provider=status_provider,
        readiness_provider=lambda: {
            "status": "ready" if ready else "not-ready",
            "dependencies": dependencies,
        },
    )
    return TestClient(app)


def test_liveness_aliases_are_lightweight_and_never_cached():
    client = _client()

    for path in ("/livez", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}
        assert response.headers["cache-control"] == "no-store"


def test_readiness_requires_postgres_and_redis_without_exposing_errors():
    response = _client(ready=False).get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not-ready",
        "dependencies": {"postgres": True, "redis": False},
    }
    assert response.headers["cache-control"] == "no-store"


def test_status_preserves_pipeline_evidence_separation():
    response = _client().get("/api/v1/node/status")

    assert response.status_code == 200
    assert response.json()["pipeline"]["status"] == "healthy"
    assert response.json()["evidence"]["status"] == "stale-or-missing"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_metrics_use_prometheus_text_and_operational_state_only():
    response = _client().get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "palimpsest_node_ready 1" in response.text
    assert 'palimpsest_pipeline_sources{state="healthy"} 1' in response.text
    assert 'palimpsest_queue_heartbeat_up{queue="collectors"} 1' in response.text
    assert "traceback" not in response.text.lower()
    assert response.headers["cache-control"] == "no-store"


def test_provider_failures_are_sanitised():
    def broken_provider():
        raise RuntimeError("redis://user:super-secret@private-host:6379")

    response = _client(status_provider=broken_provider).get("/api/v1/node/status")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert "secret" not in response.text
    assert "private-host" not in response.text


def test_primary_api_never_mounts_or_imports_censorwatch():
    app = create_app(
        status_provider=lambda: STATUS,
        readiness_provider=lambda: {},
    )
    assert TestClient(app).get("/api/v5/censorwatch/").status_code == 404

    source = (Path(__file__).resolve().parents[1] / "api" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "censorwatch" not in source.lower()
