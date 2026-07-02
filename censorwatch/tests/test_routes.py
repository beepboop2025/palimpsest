"""API hardening checks for censorwatch routes."""

from pathlib import Path

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
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_dashboard_fallback_does_not_leak_exception(monkeypatch):
    client = _client()
    monkeypatch.setattr(routes, "_DASHBOARD", Path("/tmp/missing-dashboard.html"))
    r = client.get("/api/v5/censorwatch/")
    assert r.status_code == 200
    assert "dashboard unavailable" in r.text
    assert "No such file or directory" not in r.text


def test_deletions_rejects_out_of_range_limit():
    client = _client()
    r = client.get("/api/v5/censorwatch/deletions?limit=501")
    assert r.status_code == 422
