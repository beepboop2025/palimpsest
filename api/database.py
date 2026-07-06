"""Database configuration and session management."""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/social_scraper")

engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """Dependency that provides a DB session and ensures cleanup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables (dev only — use Alembic in production)."""
    # Import model modules so their tables register on Base.metadata before
    # create_all; without this, create_all sees an empty registry and silently
    # creates nothing (the "articles"/DDTI tables would be missing).
    import storage.models  # noqa: F401
    import censorwatch.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
