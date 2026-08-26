"""SQLAlchemy models for the Palimpsest censorship observatory.

This is the censorship-only schema. Core tables:
  1. articles               — raw collected items (CDT deletion records land here
                              with category="ddti_deletion")
  2. ddti_index_snapshots   — time-series of DDTI selectivity/novelty runs
  3. collection_logs        — terminal outcome of every scheduled acquisition
  4. observation_artifacts  — immutable normalized observations retained by the node

The CensorWatch velocity leg defines its own metadata and tables in a dedicated
database (see ``censorwatch/models.py``); none are registered on this ``Base``.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from api.database import Base


class Article(Base):
    """Unstructured collected items. CDT deletion records use category='ddti_deletion'."""
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False)
    source_type = Column(String(32), nullable=False)
    url = Column(Text, nullable=True)
    url_hash = Column(String(64), unique=True, nullable=True)
    title = Column(Text, nullable=True)
    author = Column(String(256), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    collected_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    full_text = Column(Text, nullable=True)
    raw_path = Column(Text, nullable=True)
    category = Column(String(64), nullable=True)
    extra_data = Column(JSONB, default=dict)   # CDT tags etc. (read by the DDTI index)
    is_processed = Column(Boolean, default=False)

    __table_args__ = (
        Index("idx_article_url_hash", "url_hash"),
        Index("idx_article_published", "published_at"),
        Index("idx_article_category", "category"),
    )


class CollectionLog(Base):
    """Durable audit row for every collector or snapshot run."""

    __tablename__ = "collection_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False)
    records_collected = Column(Integer, default=0)
    duration_seconds = Column(Float, default=0)
    error_message = Column(Text, nullable=True)
    run_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_log_source", "source"),
        Index("idx_log_status", "status"),
        Index("idx_log_run_at", "run_at"),
    )


class ObservationArtifact(Base):
    """Private immutable copy of one successful normalized observation.

    The public ``*-latest.json`` files remain publication pointers.  This table
    is the node's inventory of content-addressed gzip artifacts under ``data/``;
    it lets operators prove what was retained without walking the filesystem.
    """

    __tablename__ = "observation_artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False)
    sha256 = Column(String(64), nullable=False)
    archive_path = Column(Text, nullable=False)
    generated_at = Column(Text, nullable=True)
    original_bytes = Column(BigInteger, nullable=False)
    compressed_bytes = Column(BigInteger, nullable=False)
    record_count = Column(Integer, default=0)
    archived_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source", "sha256", name="uq_artifact_source_sha256"),
        Index("idx_artifact_source_archived", "source", "archived_at"),
    )


class DDTIIndexSnapshot(Base):
    """Time-series of DDTI selectivity/novelty index computations.

    One row per index run, so threat scores can be charted over time (the Redis
    ``ddti:index:latest`` key is only the live cache). The full ranked list is
    kept in ``ranked`` (JSONB); scalar columns are denormalized for fast querying.
    """
    __tablename__ = "ddti_index_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    generated_at = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))
    n_observations = Column(Integer, default=0)
    n_terms = Column(Integer, default=0)
    n_new = Column(Integer, default=0)          # newly-sensitive terms this window
    top_term = Column(Text, nullable=True)
    top_threat = Column(Float, default=0.0)
    window = Column(JSONB, default=dict)        # current/history days, weights
    ranked = Column(JSONB, default=list)        # full ranked term list
    scope = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_ddti_generated_at", "generated_at"),
        Index("idx_ddti_top_term", "top_term"),
    )
