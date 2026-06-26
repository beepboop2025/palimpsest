"""SQLAlchemy models for the Palimpsest censorship observatory.

This is the censorship-only schema. Two tables:
  1. articles               — raw collected items (CDT deletion records land here
                              with category="ddti_deletion")
  2. ddti_index_snapshots   — time-series of DDTI selectivity/novelty runs

The CensorWatch velocity leg defines its own isolated tables on the same
``Base`` (see ``censorwatch/models.py``): censored_posts, post_deletions,
deletion_velocity_snapshots.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Index, Integer, String, Text,
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
