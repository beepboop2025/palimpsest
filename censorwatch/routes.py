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

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/censorwatch", tags=["censorwatch"])

_DASHBOARD = Path(__file__).parent / "dashboard.html"


@router.get("/", response_class=HTMLResponse)
def dashboard():
    try:
        return HTMLResponse(_DASHBOARD.read_text(encoding="utf-8"))
    except Exception as e:
        return HTMLResponse(f"<h1>censorwatch</h1><p>dashboard unavailable: {e}</p>",
                            status_code=200)


@router.get("/velocity")
def velocity():
    """Latest velocity signal: Redis cache → newest DB snapshot → empty."""
    # 1) Redis live cache
    try:
        import redis
        r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                           decode_responses=True)
        cached = r.get("censorwatch:velocity:latest")
        r.close()
        if cached:
            return JSONResponse(json.loads(cached))
    except Exception as e:
        logger.debug("[censorwatch] velocity redis miss: %s", e)
    # 2) Newest persisted snapshot
    try:
        from api.database import SessionLocal
        from censorwatch.models import DeletionVelocitySnapshot
        db = SessionLocal()
        try:
            snap = (db.query(DeletionVelocitySnapshot)
                      .order_by(DeletionVelocitySnapshot.generated_at.desc())
                      .first())
            if snap:
                return JSONResponse({
                    "generated_at": snap.generated_at.isoformat() if snap.generated_at else None,
                    "window": snap.window, "n_deletions": snap.n_deletions,
                    "n_terms": snap.n_terms, "top_term": snap.top_term,
                    "top_velocity": snap.top_velocity, "ranked": snap.ranked or [],
                })
        finally:
            db.close()
    except Exception as e:
        logger.debug("[censorwatch] velocity db miss: %s", e)
    return JSONResponse({"generated_at": None, "n_deletions": 0, "n_terms": 0,
                         "top_term": None, "ranked": []})


@router.get("/scrubbed")
def scrubbed():
    """Just the ranked term list from the latest signal."""
    sig = velocity().body
    data = json.loads(sig)
    return JSONResponse(data.get("ranked", []))


@router.get("/deletions")
def deletions(limit: int = 50):
    """Recent confirmed deletions, newest first."""
    limit = max(1, min(limit, 500))
    try:
        from api.database import SessionLocal
        from censorwatch.models import PostDeletion
        db = SessionLocal()
        try:
            rows = (db.query(PostDeletion)
                      .order_by(PostDeletion.deleted_at.desc())
                      .limit(limit).all())
            return JSONResponse([{
                "source": r.source, "post_id": r.post_id,
                "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
                "latency_seconds": r.latency_seconds,
                "keywords": r.keywords or [], "confirmations": r.confirmations,
            } for r in rows])
        finally:
            db.close()
    except Exception as e:
        logger.debug("[censorwatch] deletions db miss: %s", e)
        return JSONResponse([])


@router.get("/health")
def health():
    """Per-source liveness summary from Redis health keys (best-effort)."""
    out = {"sources": {}}
    try:
        import redis
        r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                           decode_responses=True)
        try:
            from censorwatch.registry import enabled_sources
            for name in enabled_sources():
                raw = r.get(f"health:{name}")
                out["sources"][name] = json.loads(raw) if raw else {"status": "unknown"}
        finally:
            r.close()
    except Exception as e:
        logger.debug("[censorwatch] health redis miss: %s", e)
    return JSONResponse(out)
