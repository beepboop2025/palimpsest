"""FastAPI router for the censorwatch dashboard + JSON API.

Mounted in ``api/main.py`` ONLY when CENSORWATCH_ENABLED is set. Endpoints under
``/api/v5/censorwatch``:
  GET /            — the minimal HTML dashboard
  GET /velocity    — latest velocity signal (Redis cache → DB snapshot → live)
  GET /deletions   — recent confirmed deletions (paged)
  GET /scrubbed    — ranked "being scrubbed now" terms (from latest signal)
  GET /health      — per-source liveness + last-cycle status

All endpoints degrade gracefully: if Postgres/Redis aren't up yet, they return
empty payloads (not 500s) so the dashboard renders "no data yet" rather than
breaking. Feed-derived strings are escaped in the HTML template (untrusted data).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/censorwatch", tags=["censorwatch"])

_DASHBOARD = Path(__file__).parent / "dashboard.html"
_REDIS_TIMEOUT_DEFAULT_S = 2.0
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
    ),
}


def _with_security_headers(resp: Response) -> Response:
    for key, value in _SECURITY_HEADERS.items():
        resp.headers.setdefault(key, value)
    return resp


def _json(payload, *, status_code: int = 200) -> JSONResponse:
    return _with_security_headers(JSONResponse(payload, status_code=status_code))


def _html(content: str, *, status_code: int = 200) -> HTMLResponse:
    return _with_security_headers(HTMLResponse(content, status_code=status_code))


def _redis_timeout_seconds() -> float:
    raw = os.getenv("CENSORWATCH_REDIS_TIMEOUT_S", str(_REDIS_TIMEOUT_DEFAULT_S))
    try:
        timeout = float(raw)
    except ValueError:
        logger.warning(
            "[censorwatch] invalid CENSORWATCH_REDIS_TIMEOUT_S=%r; using %.1fs",
            raw,
            _REDIS_TIMEOUT_DEFAULT_S,
        )
        return _REDIS_TIMEOUT_DEFAULT_S
    return max(0.1, timeout)


def _open_redis():
    import redis

    timeout = _redis_timeout_seconds()
    return redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        health_check_interval=30,
    )


def _velocity_payload() -> dict:
    out = {
        "generated_at": None,
        "n_deletions": 0,
        "n_terms": 0,
        "top_term": None,
        "ranked": [],
    }
    # 1) Redis live cache
    try:
        r = _open_redis()
        try:
            cached = r.get("censorwatch:velocity:latest")
            if cached:
                data = json.loads(cached)
                if isinstance(data, dict):
                    return data
                logger.warning("[censorwatch] cached velocity payload is not a dict")
        finally:
            r.close()
    except Exception as e:
        logger.debug("[censorwatch] velocity redis miss: %s", e)
    # 2) Newest persisted snapshot
    try:
        from api.database import SessionLocal
        from censorwatch.models import DeletionVelocitySnapshot

        db = SessionLocal()
        try:
            snap = (
                db.query(DeletionVelocitySnapshot)
                .order_by(DeletionVelocitySnapshot.generated_at.desc())
                .first()
            )
            if snap:
                return {
                    "generated_at": snap.generated_at.isoformat() if snap.generated_at else None,
                    "window": snap.window,
                    "n_deletions": snap.n_deletions,
                    "n_terms": snap.n_terms,
                    "top_term": snap.top_term,
                    "top_velocity": snap.top_velocity,
                    "ranked": snap.ranked or [],
                }
        finally:
            db.close()
    except Exception as e:
        logger.debug("[censorwatch] velocity db miss: %s", e)
    return out


@router.get("/", response_class=HTMLResponse)
def dashboard():
    try:
        return _html(_DASHBOARD.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("[censorwatch] dashboard template load failed")
        return _html("<h1>censorwatch</h1><p>dashboard unavailable</p>", status_code=200)


@router.get("/velocity")
def velocity():
    """Latest velocity signal: Redis cache → newest DB snapshot → empty."""
    return _json(_velocity_payload())


@router.get("/scrubbed")
def scrubbed():
    """Just the ranked term list from the latest signal."""
    data = _velocity_payload()
    ranked = data.get("ranked", []) if isinstance(data, dict) else []
    if not isinstance(ranked, list):
        logger.warning("[censorwatch] ranked payload is not a list")
        ranked = []
    return _json(ranked)


@router.get("/deletions")
def deletions(limit: int = Query(default=50, ge=1, le=500)):
    """Recent confirmed deletions, newest first."""
    try:
        from api.database import SessionLocal
        from censorwatch.models import PostDeletion
        db = SessionLocal()
        try:
            rows = (db.query(PostDeletion)
                      .order_by(PostDeletion.deleted_at.desc())
                      .limit(limit).all())
            return _json([{
                "source": r.source, "post_id": r.post_id,
                "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
                "latency_seconds": r.latency_seconds,
                "keywords": r.keywords or [], "confirmations": r.confirmations,
            } for r in rows])
        finally:
            db.close()
    except Exception as e:
        logger.debug("[censorwatch] deletions db miss: %s", e)
        return _json([])


@router.get("/health")
def health():
    """Per-source liveness summary from Redis health keys (best-effort)."""
    out = {"sources": {}}
    try:
        r = _open_redis()
        try:
            from censorwatch.registry import enabled_sources
            for name in enabled_sources():
                raw = r.get(f"health:{name}")
                out["sources"][name] = json.loads(raw) if raw else {"status": "unknown"}
        finally:
            r.close()
    except Exception as e:
        logger.debug("[censorwatch] health redis miss: %s", e)
    return _json(out)
