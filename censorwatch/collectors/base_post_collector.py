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
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from core.base_collector import BaseCollector
from censorwatch.interfaces import (
    LivenessState,
    Observation,
    Post,
    PostSource,
    content_hash,
)
from censorwatch.source_policy import source_url_is_allowed

logger = logging.getLogger(__name__)

# Columns the upsert understands; everything else on the row is ignored.
_POST_COLUMNS = (
    "source",
    "post_id",
    "author",
    "posted_at",
    "full_text",
    "url",
    "content_hash",
)
_POST_ID_MAX_CHARS = 128
_POST_ID_MAX_BYTES = 512
_AUTHOR_MAX_CHARS = 256
_AUTHOR_MAX_BYTES = 1024
_FULL_TEXT_MAX_CHARS = 16_384
_FULL_TEXT_MAX_BYTES = 65_536
_URL_MAX_CHARS = 2_048
_URL_MAX_BYTES = 8_192


def _bounded_hostile_text(
    value: object,
    *,
    max_chars: int,
    max_bytes: int,
    allow_empty: bool = False,
) -> str | None:
    """Normalize one HTML-derived string without truncating evidence.

    Truncating identity or content would make the stored hash describe bytes we
    did not actually observe. Oversized/non-string/NUL-bearing fields are
    therefore rejected as rows before SQLAlchemy or PostgreSQL sees them.
    """
    if type(value) is not str:
        return None
    normalized = value.strip()
    if (not normalized and not allow_empty) or "\x00" in normalized:
        return None
    if len(normalized) > max_chars:
        return None
    try:
        encoded_size = len(normalized.encode("utf-8", errors="strict"))
    except UnicodeError:
        return None
    if encoded_size > max_bytes:
        return None
    return normalized


class BasePostCollector(BaseCollector, PostSource):
    """BaseCollector specialized for post capture + re-check."""

    source_type = "censorwatch"  # marker; _upsert is overridden anyway
    hostile_input_boundary = True
    deletion_markers: tuple[str, ...] = ()
    network_policy_name: str | None = None

    @property
    def _network_source(self) -> str:
        return self.network_policy_name or self.name

    def _url_is_allowed(self, url: str | None) -> bool:
        return isinstance(url, str) and source_url_is_allowed(
            self._network_source, url, purpose="page"
        )

    def __init__(self, config: dict):
        super().__init__(config)
        self._fetcher = None  # lazy: only built when we actually fetch
        try:
            configured_record_cap = int(config.get("max_records_per_cycle", 500))
        except (TypeError, ValueError):
            configured_record_cap = 500
        self.max_records_per_cycle = min(1000, max(1, configured_record_cap))
        try:
            configured_retry_batch = int(config.get("archive_retry_batch", 20))
        except (TypeError, ValueError):
            configured_retry_batch = 20
        # Hard cap protects the collector cycle even if configuration is wrong.
        self.archive_retry_batch = min(100, max(1, configured_retry_batch))

        # BaseCollector's circuit breaker defaults to the shared REDIS_URL.  An
        # enabled hostile-data worker must instead bind it to the dedicated,
        # key-scoped writer authority.  Missing authority is a startup error;
        # there is deliberately no shared-state fallback.
        from censorwatch.config import is_enabled

        if is_enabled():
            from censorwatch.runtime_secrets import redis_url

            self._circuit_breaker.redis_url = redis_url("writer-cache")
            self._circuit_breaker.REDIS_KEY_PREFIX = "censorwatch:circuit_breaker:"

    # ── isolated observability ───────────────────────────────────
    def _report_health(self, status: str, message: str = "") -> None:
        """Publish health only through CensorWatch's scoped Redis authority."""
        cache = None
        try:
            from censorwatch.cache import open_writer_cache

            cache = open_writer_cache()
            cache.set(
                f"health:{self.name}",
                json.dumps(
                    {
                        "source": self.name,
                        "status": status,
                        "message": message,
                        "consecutive_failures": self._consecutive_failures,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                ),
                ex=7200,
            )
        except Exception as exc:
            logger.warning(
                "[censorwatch:%s] health report failed (%s)",
                self.name,
                type(exc).__name__,
            )
        finally:
            if cache is not None:
                cache.close()

    def _log_collection(
        self,
        status: str,
        records: int,
        duration: float,
        error: str = "",
    ) -> None:
        """Emit bounded diagnostics without importing the primary DB models."""
        if not self.log_collection:
            return
        logger.info(
            "[censorwatch:%s] cycle status=%s records=%d duration=%.2fs error_type=%s",
            self.name,
            status,
            records,
            duration,
            error or "none",
        )

    def _maybe_alert(self) -> None:
        """Persist a bounded alert with SET, which the scoped ACL permits."""
        if self._consecutive_failures < 3:
            return
        logger.critical(
            "[censorwatch:%s] ALERT: %d consecutive failures",
            self.name,
            self._consecutive_failures,
        )
        cache = None
        try:
            from censorwatch.cache import open_writer_cache

            cache = open_writer_cache()
            cache.set(
                f"censorwatch:alert:{self.name}",
                json.dumps(
                    {
                        "source": self.name,
                        "failures": self._consecutive_failures,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                ),
                ex=7200,
            )
        except Exception as exc:
            logger.warning(
                "[censorwatch:%s] alert persistence failed (%s)",
                self.name,
                type(exc).__name__,
            )
        finally:
            if cache is not None:
                cache.close()

    # ── fetcher lifecycle ────────────────────────────────────────
    def _get_fetcher(self):
        from censorwatch.fetcher import Fetcher

        if self._fetcher is None:
            self._fetcher = Fetcher(source=self._network_source)
        return self._fetcher

    async def close(self):
        if self._fetcher is not None:
            await self._fetcher.aclose()
            self._fetcher = None
        await super().close()

    # ── CAPTURE: the one overridden hook ─────────────────────────
    def _store_raw(self, data: list[dict]) -> str:
        """Persist hostile raw input under CensorWatch's bounded private root."""
        from censorwatch.config import get_settings
        from censorwatch.storage_budget import store_raw_snapshot

        settings = get_settings()
        return store_raw_snapshot(
            data,
            source=self.name,
            root=Path(settings.raw_dir),
            max_snapshot_bytes=settings.max_raw_snapshot_bytes,
            max_total_bytes=settings.max_raw_total_bytes,
            min_free_bytes=settings.min_archive_free_bytes,
            retention_days=settings.raw_retention_days,
        )

    def _rows_from_df(self, df: pd.DataFrame, raw_path: str | None) -> list[dict]:
        """Pure transform: parsed DataFrame → list of insertable row dicts.

        Fills content_hash (if the parser didn't) and first_seen_at. Kept pure
        and side-effect-free so it can be unit-tested without a database.
        """
        now = datetime.now(timezone.utc)
        rows = []
        rejected = 0
        for _, r in df.iterrows():
            if len(rows) >= self.max_records_per_cycle:
                logger.warning(
                    "[censorwatch:%s] record quota reached; remaining rows dropped",
                    self.name,
                )
                break
            post_id = _bounded_hostile_text(
                r.get("post_id"),
                max_chars=_POST_ID_MAX_CHARS,
                max_bytes=_POST_ID_MAX_BYTES,
            )
            author_value = r.get("author")
            author = None
            if author_value is not None and not pd.isna(author_value):
                author = _bounded_hostile_text(
                    author_value,
                    max_chars=_AUTHOR_MAX_CHARS,
                    max_bytes=_AUTHOR_MAX_BYTES,
                    allow_empty=True,
                )
                if author is None:
                    rejected += 1
                    continue
                author = author or None
            full_text = _bounded_hostile_text(
                r.get("full_text") or "",
                max_chars=_FULL_TEXT_MAX_CHARS,
                max_bytes=_FULL_TEXT_MAX_BYTES,
                allow_empty=True,
            )
            url = _bounded_hostile_text(
                r.get("url"),
                max_chars=_URL_MAX_CHARS,
                max_bytes=_URL_MAX_BYTES,
            )
            if post_id is None or full_text is None or url is None:
                rejected += 1
                continue
            if not self._url_is_allowed(url):
                logger.warning(
                    "[censorwatch:%s] dropped a row with an unreviewed URL",
                    self.name,
                )
                rejected += 1
                continue
            rows.append(
                {
                    "source": self.name,
                    "post_id": post_id,
                    "author": author,
                    "posted_at": r.get("posted_at") or None,
                    "full_text": full_text,
                    "url": url,
                    # Parser-supplied hashes cross the same hostile boundary as
                    # text. Recompute over the exact accepted representation.
                    "content_hash": content_hash(full_text),
                    "first_seen_at": now,
                    "last_state": "live",
                }
            )
        if rejected:
            logger.warning(
                "[censorwatch:%s] rejected %d structurally unsafe row(s)",
                self.name,
                rejected,
            )
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
        from censorwatch.db import fail_persistence, writer_session
        from censorwatch.models import CensoredPost

        db = writer_session()
        try:
            stmt = (
                pg_insert(CensoredPost)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["source", "post_id"])
                .returning(CensoredPost.post_id)
            )
            new_ids = {row[0] for row in db.execute(stmt)}
            db.commit()
        except Exception as exc:
            logger.error(
                "[censorwatch:%s] upsert failed (%s)",
                self.name,
                type(exc).__name__,
            )
            fail_persistence(db, operation="post upsert", cause=exc)
        finally:
            db.close()

        # Archive only the posts we just saw for the first time (Step 3 wires the
        # actual snapshot; until then this is a logged no-op so the path is live).
        new_rows = [r for r in rows if r["post_id"] in new_ids]
        for r in new_rows:
            await self._archive_new(r)

        # A transient first-capture failure used to strand a row forever:
        # ON CONFLICT made it non-new on every later run, so `_archive_new` was
        # never called again.  Claim a bounded, persistent fair batch of older
        # NULL-archive rows after handling all genuinely new rows.
        retry_rows = self._claim_archive_retry_rows(exclude_ids=new_ids)
        for retry_row in retry_rows:
            await self._archive_new(retry_row)

        logger.info(
            "[censorwatch:%s] upsert: %d rows, %d new, %d archive retries",
            self.name,
            len(rows),
            len(new_rows),
            len(retry_rows),
        )
        return len(rows)

    def _claim_archive_retry_rows(self, *, exclude_ids: set[str]) -> list[dict]:
        """Claim a bounded, oldest-attempt-first batch of retryable archives.

        Claim time is persisted in the row's JSON metadata *before* network I/O.
        Failed rows therefore move behind never-attempted/older rows instead of
        monopolising the first N slots forever.  ``id`` is the deterministic
        tie-breaker, and the SQL LIMIT prevents an unbounded table scan result.
        """
        from censorwatch.db import fail_persistence, writer_session
        from censorwatch.models import CensoredPost

        db = writer_session()
        try:
            retry_at = CensoredPost.extra_data["archive_retry_at"].astext
            query = (
                db.query(CensoredPost)
                .filter(CensoredPost.source == self.name)
                .filter(CensoredPost.archive_path.is_(None))
                .filter(CensoredPost.url.isnot(None))
                .filter(CensoredPost.url != "")
            )
            if exclude_ids:
                query = query.filter(CensoredPost.post_id.notin_(sorted(exclude_ids)))
            candidates = (
                query.order_by(
                    retry_at.asc().nullsfirst(),
                    CensoredPost.first_seen_at.asc(),
                    CensoredPost.id.asc(),
                )
                .limit(self.archive_retry_batch)
                .all()
            )
            claimed_at = datetime.now(timezone.utc).isoformat()
            rows: list[dict] = []
            for candidate in candidates:
                metadata = dict(candidate.extra_data or {})
                metadata["archive_retry_at"] = claimed_at
                metadata["archive_retry_count"] = (
                    int(metadata.get("archive_retry_count", 0) or 0) + 1
                )
                candidate.extra_data = metadata
                rows.append(
                    {
                        "post_id": str(candidate.post_id),
                        "url": candidate.url,
                    }
                )
            db.commit()
            return rows
        except Exception as exc:
            logger.warning(
                "[censorwatch:%s] archive retry claim failed (%s)",
                self.name,
                type(exc).__name__,
            )
            fail_persistence(db, operation="archive retry claim", cause=exc)
        finally:
            db.close()

    async def _archive_new(self, row: dict) -> str | None:
        """Archive a first-seen post (snapshot its page + images before it vanishes)
        and record the archive path back onto the row. Acquisition is best effort;
        a failed database transaction remains fatal to the capture run."""
        if not row.get("url"):
            return None
        if not self._url_is_allowed(row["url"]):
            logger.warning(
                "[censorwatch:%s] archive refused unreviewed URL for %s",
                self.name,
                row.get("post_id"),
            )
            return None
        from censorwatch.db import CensorwatchPersistenceError

        try:
            from censorwatch.archiver import archive_post

            path = await archive_post(
                row["url"],
                self.name,
                row["post_id"],
                fetcher=self._get_fetcher(),
                deletion_markers=self.deletion_markers,
            )
            if path:
                self._set_archive_path(row["post_id"], path)
            return path
        except CensorwatchPersistenceError:
            # The archive fetch itself is best effort, but a failed database
            # transaction must fail the collector lifecycle and its health.
            raise
        except Exception as exc:
            logger.warning(
                "[censorwatch:%s] archive failed for %s (%s)",
                self.name,
                row.get("post_id"),
                type(exc).__name__,
            )
            return None

    def _set_archive_path(self, post_id: str, path: str) -> None:
        """Persist archive_path on the just-inserted CensoredPost row."""
        from censorwatch.db import fail_persistence, writer_session
        from censorwatch.models import CensoredPost

        db = writer_session()
        try:
            db.query(CensoredPost).filter_by(source=self.name, post_id=post_id).update(
                {"archive_path": path}
            )
            db.commit()
        except Exception as exc:
            fail_persistence(db, operation="archive path update", cause=exc)
        finally:
            db.close()

    # ── RE-CHECK: PostSource contract ────────────────────────────
    async def observe(self, post: Post) -> Observation:
        """Re-fetch one known post and classify its liveness (defensive)."""
        if not self._url_is_allowed(post.url):
            return Observation(
                state=LivenessState.UNKNOWN,
                checked_at=datetime.now(timezone.utc),
                reason="url_policy_rejected",
            )
        from censorwatch.classifier import classify

        result = await self._get_fetcher().fetch(post.url, polite=True)
        return classify(result, extra_markers=self.deletion_markers)

    def control_posts(self) -> list[str]:  # pragma: no cover - abstract-ish
        raise NotImplementedError("each source must supply known-stable control posts")
