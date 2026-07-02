"""Weighted multi-source shutdown/censorship fusion timeline.

Builds one canonical incident timeline by combining source-level deletion events
with per-source reliability weights.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from censorwatch.config import CensorwatchSettings, get_settings

logger = logging.getLogger(__name__)


def _hour_bucket(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def _compute_source_reliability(posts_by_source: dict[str, list]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for src, rows in posts_by_source.items():
        n = max(1, len(rows))
        unknown = sum(1 for r in rows if (r.last_state or "") == "unknown")
        unknown_rate = unknown / n
        # Keep a floor so weak sources still contribute but are down-weighted.
        weights[src] = round(max(0.1, 1.0 - unknown_rate), 4)
    return weights


def _incident_severity(score: float, threshold: float) -> str:
    if score >= threshold * 2:
        return "critical"
    if score >= threshold * 1.4:
        return "high"
    if score >= threshold:
        return "medium"
    return "low"


def run_fusion(settings: CensorwatchSettings | None = None, now: datetime | None = None) -> dict:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    lookback_start = now - timedelta(hours=settings.fusion_lookback_hours)

    from api.database import SessionLocal
    from censorwatch.models import CensoredPost, PostDeletion

    db = SessionLocal()
    try:
        posts = (
            db.query(CensoredPost)
            .filter(CensoredPost.last_checked_at >= lookback_start)
            .all()
        )
        deletions = (
            db.query(PostDeletion)
            .filter(PostDeletion.deleted_at >= lookback_start)
            .all()
        )
    finally:
        db.close()

    posts_by_source: dict[str, list] = defaultdict(list)
    for p in posts:
        posts_by_source[p.source].append(p)

    reliability = _compute_source_reliability(posts_by_source)

    buckets = defaultdict(lambda: defaultdict(int))  # hour -> source -> deletions
    for d in deletions:
        b = _hour_bucket(d.deleted_at)
        buckets[b][d.source] += 1

    timeline = []
    for b in sorted(buckets.keys()):
        per_source = buckets[b]
        weighted_score = 0.0
        details = []
        for src, n in per_source.items():
            w = reliability.get(src, 0.5)
            contrib = w * n
            weighted_score += contrib
            details.append({"source": src, "deletions": n, "weight": w, "weighted": round(contrib, 3)})
        timeline.append(
            {
                "window_start": b.isoformat(),
                "window_end": (b + timedelta(hours=1)).isoformat(),
                "weighted_score": round(weighted_score, 3),
                "sources": sorted(details, key=lambda x: -x["weighted"]),
            }
        )

    scores = [t["weighted_score"] for t in timeline] or [0.0]
    mean = statistics.fmean(scores)
    std = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    threshold = max(1.0, mean + settings.fusion_alert_z * max(std, 1.0))

    incidents = []
    for t in timeline:
        if t["weighted_score"] >= threshold:
            incidents.append(
                {
                    **t,
                    "severity": _incident_severity(t["weighted_score"], threshold),
                    "threshold": round(threshold, 3),
                }
            )

    payload = {
        "generated_at": now.isoformat(),
        "window_hours": settings.fusion_lookback_hours,
        "source_reliability": reliability,
        "threshold": round(threshold, 3),
        "timeline": timeline,
        "incidents": incidents,
    }

    out_dir = Path("./data/censorwatch/fusion")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    try:
        import redis

        r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        r.set("censorwatch:fusion:latest", json.dumps(payload, ensure_ascii=False), ex=3600)
        r.close()
    except Exception:
        pass

    logger.info(
        "[fusion] timeline=%d incidents=%d threshold=%.3f",
        len(timeline),
        len(incidents),
        threshold,
    )
    return {
        "status": "ok",
        "timeline_windows": len(timeline),
        "incidents": len(incidents),
        "threshold": round(threshold, 3),
    }

