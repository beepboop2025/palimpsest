"""Production-local FastAPI control plane for the Palimpsest node."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from core.observability import (
    check_readiness,
    collect_node_status,
    render_prometheus_metrics,
)


StatusProvider = Callable[[], Mapping[str, Any]]
ReadinessProvider = Callable[[], Mapping[str, Any]]

_UNAVAILABLE_STATUS = {
    "status": "unavailable",
    "pipeline": {
        "status": "unavailable",
        "storage_available": False,
        "counts": {},
        "sources": {},
    },
    "evidence": {"status": "unavailable", "counts": {}, "sources": {}},
}
_NOT_READY = {
    "status": "not-ready",
    "dependencies": {"postgres": False, "redis": False},
}


def _safe_status(provider: StatusProvider) -> Mapping[str, Any]:
    try:
        value = provider()
    except Exception:
        return _UNAVAILABLE_STATUS
    return value if isinstance(value, Mapping) else _UNAVAILABLE_STATUS


def _safe_readiness(provider: ReadinessProvider) -> Mapping[str, Any]:
    try:
        value = provider()
    except Exception:
        return _NOT_READY
    if not isinstance(value, Mapping):
        return _NOT_READY
    raw_dependencies = value.get("dependencies")
    dependencies = raw_dependencies if isinstance(raw_dependencies, Mapping) else {}
    postgres = dependencies.get("postgres") is True
    redis = dependencies.get("redis") is True
    return {
        "status": "ready" if postgres and redis else "not-ready",
        "dependencies": {"postgres": postgres, "redis": redis},
    }


def create_app(
    *,
    status_provider: StatusProvider | None = None,
    readiness_provider: ReadinessProvider | None = None,
) -> FastAPI:
    """Build the API with replaceable probes for deterministic offline tests."""

    get_status = status_provider if status_provider is not None else collect_node_status
    get_readiness = (
        readiness_provider if readiness_provider is not None else check_readiness
    )

    app = FastAPI(
        title="Palimpsest node control plane",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def control_plane_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def liveness_response() -> dict[str, str]:
        return {"status": "alive"}

    app.add_api_route("/livez", liveness_response, methods=["GET"], tags=["health"])
    app.add_api_route("/healthz", liveness_response, methods=["GET"], tags=["health"])

    @app.get("/readyz", tags=["health"])
    def readiness():
        payload = _safe_readiness(get_readiness)
        status_code = 200 if payload.get("status") == "ready" else 503
        return JSONResponse(dict(payload), status_code=status_code)

    @app.get("/api/v1/node/status", tags=["node"])
    def node_status():
        payload = _safe_status(get_status)
        status_code = 503 if payload.get("status") == "unavailable" else 200
        return JSONResponse(dict(payload), status_code=status_code)

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        status = _safe_status(get_status)
        readiness = _safe_readiness(get_readiness)
        return Response(
            render_prometheus_metrics(status, readiness),
            media_type="text/plain; version=0.0.4",
        )

    return app


app = create_app()
