"""API hardening checks for censorwatch routes."""

from pathlib import Path
from datetime import datetime, timedelta, timezone
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import censorwatch.routes as routes


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v5")
    return TestClient(app)


def test_velocity_includes_security_headers():
    client = _client()
    r = client.get("/api/v5/censorwatch/velocity")
    assert r.status_code == 503
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in r.headers["Content-Security-Policy"]


def test_dashboard_fallback_does_not_leak_exception(monkeypatch):
    client = _client()
    monkeypatch.setattr(routes, "_DASHBOARD", Path("/tmp/missing-dashboard.html"))
    r = client.get("/api/v5/censorwatch/")
    assert r.status_code == 503
    assert "dashboard unavailable" in r.text
    assert "No such file or directory" not in r.text


def test_deletions_rejects_out_of_range_limit():
    client = _client()
    r = client.get("/api/v5/censorwatch/deletions?limit=101")
    assert r.status_code == 422


def test_dashboard_assets_are_external_and_csp_compatible():
    client = _client()
    dashboard = client.get("/api/v5/censorwatch/")
    css = client.get("/api/v5/censorwatch/dashboard.css")
    javascript = client.get("/api/v5/censorwatch/dashboard.js")

    assert dashboard.status_code == css.status_code == javascript.status_code == 200
    assert '<script src="dashboard.js" defer></script>' in dashboard.text
    assert '<link rel="stylesheet" href="dashboard.css">' in dashboard.text
    assert "<script>" not in dashboard.text and "<style>" not in dashboard.text
    assert css.headers["content-type"].startswith("text/css")
    assert javascript.headers["content-type"].startswith("application/javascript")


class _ReaderSession:
    def execute(self, _statement, _parameters=None):
        return object()

    def close(self):
        pass


class _ReaderCache:
    def __init__(self, values):
        self.values = values

    def ping(self):
        return True

    def get(self, key):
        return self.values.get(key)

    def close(self):
        pass


def test_health_requires_database_cache_beat_fresh_capture_and_detector(monkeypatch):
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    values = {
        "censorwatch:beat:heartbeat": json.dumps({"timestamp": now.isoformat()}),
        "health:eastmoney_guba": json.dumps(
            {"status": "success", "timestamp": now.isoformat()}
        ),
        "health:detector:eastmoney_guba": json.dumps(
            {"source": "eastmoney_guba", "status": "success", "timestamp": now.isoformat()}
        ),
    }
    monkeypatch.setattr(routes, "_open_control_redis", lambda: _ReaderCache(values))
    monkeypatch.setattr(routes, "_open_data_redis", lambda: _ReaderCache(values))
    monkeypatch.setattr("censorwatch.db.reader_session", lambda: _ReaderSession())
    monkeypatch.setattr(
        "censorwatch.registry.enabled_sources", lambda: ["eastmoney_guba"]
    )

    payload = routes._censorwatch_readiness_payload(now=now)
    assert payload["status"] == "ready"
    assert payload["dependencies"] == {
        "database": True,
        "data_cache": True,
        "control_cache": True,
    }
    assert payload["sources"]["eastmoney_guba"] == {
        "status": "success",
        "fresh": True,
        "capture": {"status": "success", "fresh": True},
        "detector": {"status": "success", "fresh": True},
    }

    values["health:eastmoney_guba"] = json.dumps(
        {
            "status": "success",
            "timestamp": (now - timedelta(hours=2)).isoformat(),
        }
    )
    payload = routes._censorwatch_readiness_payload(now=now)
    assert payload["status"] == "not-ready"
    assert payload["sources"]["eastmoney_guba"]["fresh"] is False
    assert payload["sources"]["eastmoney_guba"]["status"] == "stale"

    values["health:eastmoney_guba"] = json.dumps(
        {"status": "success", "timestamp": now.isoformat()}
    )
    values["health:detector:eastmoney_guba"] = json.dumps(
        {"source": "eastmoney_guba", "status": "degraded", "timestamp": now.isoformat()}
    )
    payload = routes._censorwatch_readiness_payload(now=now)
    assert payload["status"] == "not-ready"
    assert payload["sources"]["eastmoney_guba"]["status"] == "degraded"
    assert payload["sources"]["eastmoney_guba"]["detector"] == {
        "status": "degraded",
        "fresh": False,
    }


def test_health_endpoint_returns_503_for_unready_plane(monkeypatch):
    monkeypatch.setattr(
        routes,
        "_censorwatch_readiness_payload",
        lambda: {
            "status": "not-ready",
            "dependencies": {"database": False, "cache": False},
            "beat": {"status": "unavailable", "fresh": False},
            "sources": {},
        },
    )
    response = _client().get("/api/v5/censorwatch/health")
    assert response.status_code == 503
    assert response.json()["status"] == "not-ready"


def _signal(now: datetime, **overrides):
    value = {
        "generated_at": now.isoformat(),
        "status": "ok",
        "window": {
            "window_min": 60,
            "baseline_windows": 24,
            "z_threshold": 3.0,
        },
        "n_deletions": 1,
        "n_terms": 1,
        "top_term": "term",
        "top_velocity": 1.0,
        "ranked": [
            {
                "term": "term",
                "count": 1,
                "velocity_per_hour": 1.0,
                "z": 1.0,
                "spike": False,
            }
        ],
    }
    value.update(overrides)
    return value


def test_velocity_cache_is_fresh_schema_bounded_and_utf8_bounded(monkeypatch):
    now = datetime.now(timezone.utc)
    ranked = [
        {
            "term": "界" * 100,
            "domain": "domain" * 20,
            "count": index,
            "velocity_per_hour": float(index),
            "z": 1.0,
            "spike": False,
        }
        for index in range(75)
    ]
    values = {
        "censorwatch:velocity:latest": json.dumps(
            _signal(now, ranked=ranked), ensure_ascii=False
        )
    }
    monkeypatch.setattr(routes, "_open_data_redis", lambda: _ReaderCache(values))

    payload, available = routes._velocity_payload()

    assert available is True
    assert payload["status"] == "ok"
    assert len(payload["ranked"]) == routes._MAX_RANKED_ROWS
    assert payload["n_terms"] == routes._MAX_RANKED_ROWS
    assert all(
        len(row["term"].encode("utf-8")) <= 128 for row in payload["ranked"]
    )
    assert all(
        len(row.get("domain", "").encode("utf-8")) <= 64
        for row in payload["ranked"]
    )


def test_stale_cache_and_unavailable_database_never_render_as_zero(monkeypatch):
    now = datetime.now(timezone.utc) - timedelta(hours=2)
    values = {
        "censorwatch:velocity:latest": json.dumps(_signal(now, n_deletions=0))
    }
    monkeypatch.setattr(routes, "_open_data_redis", lambda: _ReaderCache(values))

    def unavailable_database():
        raise RuntimeError("reader unavailable")

    monkeypatch.setattr("censorwatch.db.reader_session", unavailable_database)

    payload, available = routes._velocity_payload()

    assert available is False
    assert payload["status"] == "stale"
    assert payload["n_deletions"] is None
    assert payload["n_terms"] is None
    assert payload["ranked"] == []


def test_oversized_cache_is_rejected_before_json_decode(monkeypatch):
    monkeypatch.setenv("CENSORWATCH_API_MAX_CACHE_BYTES", "4096")
    cache = _ReaderCache(
        {"censorwatch:velocity:latest": b"{" + (b"x" * 8192) + b"}"}
    )
    monkeypatch.setattr(routes, "_open_data_redis", lambda: cache)

    def unavailable_database():
        raise RuntimeError("reader unavailable")

    monkeypatch.setattr("censorwatch.db.reader_session", unavailable_database)
    payload, available = routes._velocity_payload()

    assert available is False
    assert payload["status"] == "unavailable"
    assert payload["n_deletions"] is None


def test_deletion_projection_and_response_are_strictly_bounded():
    deleted_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
    value = {
        "source": "s" * 1000,
        "post_id": "p" * 1000,
        "deleted_at": deleted_at,
        "latency_seconds": float("inf"),
        "keywords": ["界" * 100 for _ in range(100)],
        "confirmations": 3,
    }

    payload = routes._deletion_row(value)

    assert payload is not None
    assert len(payload["source"].encode("utf-8")) <= 64
    assert len(payload["post_id"].encode("utf-8")) <= 128
    assert len(payload["keywords"]) == routes._MAX_KEYWORDS
    assert all(len(item.encode("utf-8")) <= 128 for item in payload["keywords"])
    assert payload["latency_seconds"] is None


def test_dashboard_copy_distinguishes_missing_from_measured_zero():
    javascript = (Path(__file__).parent.parent / "dashboard.js").read_text(
        encoding="utf-8"
    )
    assert "Measurement unavailable" in javascript
    assert "Measured: no deletions" in javascript
    assert "measured ? num(velocity.n_deletions) : '–'" in javascript
