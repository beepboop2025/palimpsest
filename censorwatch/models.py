"""Isolated SQLAlchemy models for censorwatch.

Three tables, all on the shared ``api.database.Base`` so they live in the same
Postgres database as the rest of the platform — but they are **never** written to
by production code paths, and ``db.create_censorwatch_tables()`` creates only
these three (via ``create_all(tables=[...])``), so initializing them cannot
touch or migrate the production schema.

  1. censored_posts             — every captured post + its deletion lifecycle
  2. post_deletions             — append-only event log of *confirmed* deletions
  3. deletion_velocity_snapshots — time-series of the velocity/spike signal

Conventions mirror ``storage/models.py`` (Integer PK, JSONB ``metadata`` column,
indices in ``__table_args__``, timezone-aware datetimes).
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from api.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CensoredPost(Base):
    """A captured public post and the running state of its deletion lifecycle.

    Idempotency key is ``(source, post_id)`` — re-capturing the same post is a
    no-op upsert, which is what makes the collector safe to restart.
    """

    __tablename__ = "censored_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Identity + content (the user's required per-post fields) ──
    source = Column(String(64), nullable=False)
    post_id = Column(String(128), nullable=False)
    author = Column(String(256), nullable=True)
    posted_at = Column(DateTime(timezone=True), nullable=True)   # original "timestamp"
    full_text = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    content_hash = Column(String(64), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # ── Detection lifecycle state (mutated by the detector) ──────
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    check_count = Column(Integer, default=0, nullable=False)
    gone_streak = Column(Integer, default=0, nullable=False)      # consecutive GONE
    last_state = Column(String(16), default="live", nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)   # set only when confirmed
    deletion_latency_seconds = Column(Float, nullable=True)       # deleted_at − posted_at
    liveness_at_deletion = Column(String(16), nullable=True)      # tag for audit

    # ── Archive + misc ───────────────────────────────────────────
    archive_path = Column(Text, nullable=True)
    extra_data = Column("metadata", JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("source", "post_id", name="uq_censored_source_postid"),
        Index("idx_censored_deleted_at", "deleted_at"),
        Index("idx_censored_posted_at", "posted_at"),
        Index("idx_censored_source", "source"),
        Index("idx_censored_last_checked", "last_checked_at"),
        Index("idx_censored_first_seen", "first_seen_at"),
        # Composite that the detector's worklist query rides:
        # "pending posts (deleted_at IS NULL) ordered by staleness".
        Index("idx_censored_pending", "deleted_at", "last_checked_at"),
    )


class PostDeletion(Base):
    """Append-only log of *confirmed* deletions — one row per scrubbed post.

    The signal layer reads from here exclusively, so by construction it only ever
    sees deletions that survived the N-confirmation + non-DEGRADED gate. UNKNOWN
    observations never produce a row.
    """

    __tablename__ = "post_deletions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_pk = Column(Integer, ForeignKey("censored_posts.id", ondelete="CASCADE"),
                     nullable=False)

    # Denormalized for fast signal queries without a join.
    source = Column(String(64), nullable=False)
    post_id = Column(String(128), nullable=False)

    posted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=False)
    latency_seconds = Column(Float, nullable=True)

    keywords = Column(JSONB, default=list)          # terms extracted at deletion time
    confirmations = Column(Integer, default=0, nullable=False)
    liveness_state = Column(String(16), default="live", nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("idx_deletion_deleted_at", "deleted_at"),
        Index("idx_deletion_source", "source"),
        UniqueConstraint("post_pk", name="uq_deletion_post_pk"),
    )


class DeletionVelocitySnapshot(Base):
    """Time-series of the deletion-velocity / scrub-cluster signal.

    Mirrors ``storage/models.DDTIIndexSnapshot``: scalar columns are denormalized
    for fast charting; the full ranked "being scrubbed now" list lives in
    ``ranked`` (JSONB).
    """

    __tablename__ = "deletion_velocity_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    generated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    window = Column(JSONB, default=dict)        # window minutes, baseline config
    n_deletions = Column(Integer, default=0)    # deletions in the active window
    n_terms = Column(Integer, default=0)
    top_term = Column(Text, nullable=True)
    top_velocity = Column(Float, default=0.0)
    ranked = Column(JSONB, default=list)        # full ranked term list w/ z-scores
    scope = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_velocity_generated_at", "generated_at"),
        Index("idx_velocity_top_term", "top_term"),
    )
