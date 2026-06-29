"""BasePostCollector — bridges the platform's BaseCollector to censorwatch.

Subclasses ``core.base_collector.BaseCollector`` to inherit retry/backoff,
immutable raw storage, the circuit breaker, Redis health, and CollectionLog —
then overrides exactly ONE hook, ``_upsert()``, to route rows to the isolated
``censored_posts`` table (idempotent on ``(source, post_id)``) and archive each
post on first capture, instead of the production ``articles`` table.

The same class also implements ``interfaces.PostSource`` (``observe`` +
``control_posts``) so one per-source class serves both lifecycles: CAPTURE (via
BaseCollector.run → collect/parse/validate/_upsert) and RE-CHECK (the detector
calls observe()).

A concrete source provides:
    name, source_type="censorwatch"
    deletion_markers: tuple[str, ...]   # per-source notice strings (maintainer-authored)
    async def collect(self) -> list[dict]
    async def parse(self, raw) -> pd.DataFrame   # columns ⊇ Post fields
    def validate(self, df) -> bool
    def control_posts(self) -> list[str]
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from core.base_collector import BaseCollector
from censorwatch.interfaces import Observation, Post, PostSource, content_hash

logger = logging.getLogger(__name__)

# Columns the upsert understands; everything else on the row is ignored.
_POST_COLUMNS = ("source", "post_id", "author", "posted_at", "full_text", "url",
                 "content_hash")


class BasePostCollector(BaseCollector, PostSource):
    """BaseCollector specialized for post capture + re-check."""

    source_type = "censorwatch"        # marker; _upsert is overridden anyway
    deletion_markers: tuple[str, ...] = ()

    def __init__(self, config: dict):
        super().__init__(config)
        self._fetcher = None  # lazy: only built when we actually fetch

    # ── fetcher lifecycle ────────────────────────────────────────
    def _get_fetcher(self):
        from censorwatch.fetcher import Fetcher
        if self._fetcher is None:
            self._fetcher = Fetcher()
        return self._fetcher

    async def close(self):
        if self._fetcher is not None:
            await self._fetcher.aclose()
            self._fetcher = None
        await super().close()

    # ── CAPTURE: the one overridden hook ─────────────────────────
    def _rows_from_df(self, df: pd.DataFrame, raw_path: str | None) -> list[dict]:
        """Pure transform: parsed DataFrame → list of insertable row dicts.

        Fills content_hash (if the parser didn't) and first_seen_at. Kept pure
        and side-effect-free so it can be unit-tested without a database.
        """
        now = datetime.now(timezone.utc)
        rows = []
        for _, r in df.iterrows():
            post_id = str(r.get("post_id") or "").strip()
            if not post_id:
                continue  # a row with no stable id can't be tracked; skip
            full_text = r.get("full_text") or ""
            rows.append({
                "source": self.name,
                "post_id": post_id,
                "author": (r.get("author") or None),
                "posted_at": r.get("posted_at") or None,
                "full_text": full_text,
                "url": r.get("url") or None,
                "content_hash": r.get("content_hash") or content_hash(full_text),
                "first_seen_at": now,
                "last_state": "live",
            })
        return rows

    async def _upsert(self, df: pd.DataFrame, raw_path: str) -> int:
        """Insert captured posts idempotently; archive newly-seen ones.

        Uses ``INSERT ... ON CONFLICT (source, post_id) DO NOTHING RETURNING`` so
        re-capturing a known post is a no-op (resumable/restart-safe) and we learn
        exactly which posts are NEW — those get archived before they can vanish.
        """
        if df is None or df.empty:
            return 0
        rows = self._rows_from_df(df, raw_path)
        if not rows:
            return 0

        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from api.database import SessionLocal
        from censorwatch.models import CensoredPost

        db = SessionLocal()
        try:
            stmt = (
                pg_insert(CensoredPost)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["source", "post_id"])
                .returning(CensoredPost.post_id)
            )
            new_ids = {row[0] for row in db.execute(stmt)}
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error("[censorwatch:%s] upsert failed: %s", self.name, e)
            return 0
        finally:
            db.close()

        # Archive only the posts we just saw for the first time (Step 3 wires the
        # actual snapshot; until then this is a logged no-op so the path is live).
        new_rows = [r for r in rows if r["post_id"] in new_ids]
        for r in new_rows:
            await self._archive_new(r)
        logger.info("[censorwatch:%s] upsert: %d rows, %d new (archived)",
                    self.name, len(rows), len(new_rows))
        return len(rows)

    async def _archive_new(self, row: dict) -> None:
        """Archive a first-seen post (snapshot its page + images before it vanishes)
        and record the archive path back onto the row. Best-effort: an archive
        failure must not fail the capture run."""
        if not row.get("url"):
            return
        try:
            from censorwatch.archiver import archive_post
            path = await archive_post(
                row["url"], self.name, row["post_id"], fetcher=self._get_fetcher()
            )
            if path:
                self._set_archive_path(row["post_id"], path)
        except Exception as e:
            logger.warning("[censorwatch:%s] archive failed for %s: %s",
                           self.name, row.get("post_id"), e)

    def _set_archive_path(self, post_id: str, path: str) -> None:
        """Persist archive_path on the just-inserted CensoredPost row."""
        from api.database import SessionLocal
        from censorwatch.models import CensoredPost
        db = SessionLocal()
        try:
            db.query(CensoredPost).filter_by(source=self.name, post_id=post_id) \
                .update({"archive_path": path})
            db.commit()
        finally:
            db.close()

    # ── RE-CHECK: PostSource contract ────────────────────────────
    async def observe(self, post: Post) -> Observation:
        """Re-fetch one known post and classify its liveness (defensive)."""
        from censorwatch.classifier import classify
        result = await self._get_fetcher().fetch(post.url, polite=True)
        return classify(result, extra_markers=self.deletion_markers)

    def control_posts(self) -> list[str]:  # pragma: no cover - abstract-ish
        raise NotImplementedError("each source must supply known-stable control posts")
