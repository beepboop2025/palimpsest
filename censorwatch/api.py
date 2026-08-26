"""Dedicated ASGI boundary for the read-only CensorWatch presentation plane."""

from __future__ import annotations

from fastapi import FastAPI

from censorwatch.routes import router


def create_app() -> FastAPI:
    """Create an app containing CensorWatch routes and no primary app imports."""
    application = FastAPI(
        title="CensorWatch read-only presentation plane",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def boundary_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @application.get("/healthz", include_in_schema=False)
    def liveness() -> dict[str, str]:
        return {"status": "alive", "service": "censorwatch-api"}

    application.include_router(router, prefix="/api/v5")
    return application


app = create_app()
