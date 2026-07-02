"""Cloud consolidation + upload for CensorWatch (S3-compatible backends).

Supports AWS S3 and compatible object stores (Cloudflare R2, Backblaze B2 S3,
MinIO). The sync job exports a bounded lookback snapshot from the three
censorwatch tables to NDJSON.GZ and uploads those artifacts plus a manifest.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from censorwatch.config import CensorwatchSettings, get_settings

logger = logging.getLogger(__name__)


def _iso(v: Any):
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).isoformat()
        return v.isoformat()
    return v


def _row_to_jsonable(row: Any, columns: list[str]) -> dict:
    out = {}
    for c in columns:
        out[c] = _iso(getattr(row, c))
    return out


def _write_ndjson_gz(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, mode="wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def _artifact_keys(prefix: str, stamp: str) -> dict:
    root = f"{prefix}/snapshots/{stamp}"
    return {
        "root": root,
        "posts": f"{root}/censored_posts.ndjson.gz",
        "deletions": f"{root}/post_deletions.ndjson.gz",
        "velocity": f"{root}/deletion_velocity_snapshots.ndjson.gz",
        "manifest": f"{root}/manifest.json",
    }


def _build_s3_client(settings: CensorwatchSettings):
    try:
        import boto3
    except Exception as e:  # pragma: no cover - runtime dependency
        raise RuntimeError(
            "boto3 is required for cloud sync. Install requirements.txt in the runtime image."
        ) from e

    kwargs = {"region_name": settings.cloud_region}
    if settings.cloud_endpoint_url:
        kwargs["endpoint_url"] = settings.cloud_endpoint_url
    return boto3.client("s3", **kwargs)


def run_cloud_sync(settings: CensorwatchSettings | None = None, now: datetime | None = None) -> dict:
    """Export recent CensorWatch state and upload to S3-compatible cloud storage."""
    settings = settings or get_settings()
    if not settings.cloud_sync_enabled:
        return {"status": "disabled", "note": "CENSORWATCH_CLOUD_SYNC_ENABLED not set"}
    if not settings.cloud_bucket:
        return {"status": "error", "error": "CENSORWATCH_CLOUD_BUCKET is required"}

    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=settings.cloud_lookback_hours)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    keys = _artifact_keys(settings.cloud_prefix, stamp)

    from api.database import SessionLocal
    from censorwatch.models import CensoredPost, PostDeletion, DeletionVelocitySnapshot

    columns_posts = [
        "id", "source", "post_id", "author", "posted_at", "full_text", "url", "content_hash",
        "first_seen_at", "last_checked_at", "check_count", "gone_streak", "last_state",
        "deleted_at", "deletion_latency_seconds", "liveness_at_deletion", "archive_path",
    ]
    columns_del = [
        "id", "post_pk", "source", "post_id", "posted_at", "deleted_at", "latency_seconds",
        "keywords", "confirmations", "liveness_state", "created_at",
    ]
    columns_vel = [
        "id", "generated_at", "window", "n_deletions", "n_terms", "top_term",
        "top_velocity", "ranked", "scope",
    ]

    db = SessionLocal()
    tmp_root = Path("./data/censorwatch/exports") / stamp
    try:
        posts = (
            db.query(CensoredPost)
            .filter(CensoredPost.first_seen_at >= start)
            .order_by(CensoredPost.first_seen_at.asc())
            .all()
        )
        deletions = (
            db.query(PostDeletion)
            .filter(PostDeletion.created_at >= start)
            .order_by(PostDeletion.created_at.asc())
            .all()
        )
        velocity = (
            db.query(DeletionVelocitySnapshot)
            .filter(DeletionVelocitySnapshot.generated_at >= start)
            .order_by(DeletionVelocitySnapshot.generated_at.asc())
            .all()
        )

        posts_rows = [_row_to_jsonable(r, columns_posts) for r in posts]
        del_rows = [_row_to_jsonable(r, columns_del) for r in deletions]
        vel_rows = [_row_to_jsonable(r, columns_vel) for r in velocity]

        f_posts = tmp_root / "censored_posts.ndjson.gz"
        f_del = tmp_root / "post_deletions.ndjson.gz"
        f_vel = tmp_root / "deletion_velocity_snapshots.ndjson.gz"
        _write_ndjson_gz(f_posts, posts_rows)
        _write_ndjson_gz(f_del, del_rows)
        _write_ndjson_gz(f_vel, vel_rows)

        manifest = {
            "generated_at": now.isoformat(),
            "window_start": start.isoformat(),
            "window_end": now.isoformat(),
            "counts": {
                "censored_posts": len(posts_rows),
                "post_deletions": len(del_rows),
                "deletion_velocity_snapshots": len(vel_rows),
            },
            "keys": keys,
        }
        f_manifest = tmp_root / "manifest.json"
        f_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        s3 = _build_s3_client(settings)
        s3.upload_file(str(f_posts), settings.cloud_bucket, keys["posts"])
        s3.upload_file(str(f_del), settings.cloud_bucket, keys["deletions"])
        s3.upload_file(str(f_vel), settings.cloud_bucket, keys["velocity"])
        s3.put_object(
            Bucket=settings.cloud_bucket,
            Key=keys["manifest"],
            Body=json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
        )

        if settings.cloud_include_archive:
            arch_root = Path(settings.archive_dir)
            if arch_root.exists():
                for fp in arch_root.rglob("*"):
                    if fp.is_file():
                        rel = fp.relative_to(arch_root).as_posix()
                        key = f"{settings.cloud_prefix}/archive/{rel}"
                        s3.upload_file(str(fp), settings.cloud_bucket, key)

        logger.info(
            "[cloud_sync] uploaded snapshot %s (posts=%d, deletions=%d, velocity=%d)",
            stamp, len(posts_rows), len(del_rows), len(vel_rows),
        )
        return {
            "status": "ok",
            "snapshot": stamp,
            "bucket": settings.cloud_bucket,
            "prefix": settings.cloud_prefix,
            "counts": manifest["counts"],
            "manifest_key": keys["manifest"],
        }
    finally:
        db.close()
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)

