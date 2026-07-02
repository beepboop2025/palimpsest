"""Agent-like consolidation layer for continuous CensorWatch operations.

This module rolls raw collector state into a single structured payload for
downstream consumers (dashboard/API/export/cloud).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from censorwatch.config import CensorwatchSettings, get_settings
from censorwatch.probe_planner import ProbeSignal, build_probe_priority

logger = logging.getLogger(__name__)


def _to_iso(v):
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).isoformat()
        return v.isoformat()
    return v


def _record(row) -> dict:
    return {
        "source": row.source,
        "post_id": row.post_id,
        "author": row.author,
        "posted_at": _to_iso(row.posted_at),
        "first_seen_at": _to_iso(row.first_seen_at),
        "last_checked_at": _to_iso(row.last_checked_at),
        "url": row.url,
        "last_state": row.last_state,
        "gone_streak": row.gone_streak,
        "deleted_at": _to_iso(row.deleted_at),
        "deletion_latency_seconds": row.deletion_latency_seconds,
        "archive_path": row.archive_path,
    }


def run_consolidation(settings: CensorwatchSettings | None = None, now: datetime | None = None) -> dict:
    """Build and publish the latest structured dataset for all enabled sources."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    lookback_start = now - timedelta(hours=settings.consolidate_lookback_hours)

    from api.database import SessionLocal
    from censorwatch.models import CensoredPost, PostDeletion, DeletionVelocitySnapshot

    db = SessionLocal()
    try:
        posts = (
            db.query(CensoredPost)
            .filter(CensoredPost.first_seen_at >= lookback_start)
            .order_by(CensoredPost.first_seen_at.desc())
            .limit(settings.consolidate_max_rows)
            .all()
        )
        deletions_recent = (
            db.query(PostDeletion)
            .filter(PostDeletion.created_at >= lookback_start)
            .all()
        )
        latest_signal = (
            db.query(DeletionVelocitySnapshot)
            .order_by(DeletionVelocitySnapshot.generated_at.desc())
            .limit(1)
            .one_or_none()
        )

        by_source = {}
        for p in posts:
            s = by_source.setdefault(
                p.source,
                {"source": p.source, "total_posts": 0, "deleted_posts": 0, "pending_posts": 0},
            )
            s["total_posts"] += 1
            if p.deleted_at is None:
                s["pending_posts"] += 1
            else:
                s["deleted_posts"] += 1

        payload = {
            "generated_at": now.isoformat(),
            "window_start": lookback_start.isoformat(),
            "window_hours": settings.consolidate_lookback_hours,
            "totals": {
                "posts": len(posts),
                "deletions": len(deletions_recent),
                "sources": len(by_source),
            },
            "sources": sorted(by_source.values(), key=lambda x: (-x["total_posts"], x["source"])),
            "latest_signal": None
            if latest_signal is None
            else {
                "generated_at": _to_iso(latest_signal.generated_at),
                "n_deletions": latest_signal.n_deletions,
                "n_terms": latest_signal.n_terms,
                "top_term": latest_signal.top_term,
                "top_velocity": latest_signal.top_velocity,
                "scope": latest_signal.scope,
            },
            "records": [_record(p) for p in posts],
        }
    finally:
        db.close()

    # Pull latest fusion timeline (if available) and attach probe-priority plan.
    fusion_latest = None
    fusion_path = Path("./data/censorwatch/fusion/latest.json")
    if fusion_path.exists():
        try:
            fusion_latest = json.loads(fusion_path.read_text(encoding="utf-8"))
        except Exception:
            fusion_latest = None
    if fusion_latest is not None:
        payload["fusion"] = {
            "generated_at": fusion_latest.get("generated_at"),
            "threshold": fusion_latest.get("threshold"),
            "incident_count": len(fusion_latest.get("incidents", [])),
        }

    unknown_states = sum(1 for p in posts if (p.last_state or "") == "unknown")
    unknown_rate = (unknown_states / max(1, len(posts)))
    incident_count = len((fusion_latest or {}).get("incidents", []))
    plan = build_probe_priority(
        ProbeSignal(
            unknown_rate=unknown_rate,
            incident_count=incident_count,
            source_count=len(by_source),
        )
    )
    payload["probe_priority_plan"] = plan

    out_dir = Path("./data/censorwatch/consolidated")
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    try:
        import redis

        r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        r.set("censorwatch:consolidated:latest", json.dumps(payload, ensure_ascii=False), ex=3600)
        r.close()
    except Exception as e:
        logger.debug("[consolidator] redis publish skipped: %s", e)

    logger.info(
        "[consolidator] records=%d sources=%d deletions=%d",
        payload["totals"]["posts"],
        payload["totals"]["sources"],
        payload["totals"]["deletions"],
    )
    return {
        "status": "ok",
        "records": payload["totals"]["posts"],
        "sources": payload["totals"]["sources"],
        "deletions": payload["totals"]["deletions"],
        "output": str(latest),
    }
