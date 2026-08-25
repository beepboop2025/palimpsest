"""Dedicated SQLAlchemy authorities for the isolated CensorWatch database.

Runtime code gets only ``writer_session`` or ``reader_session``. Schema
ownership is deliberately absent from both and is available solely to the
one-shot provisioning service through ``admin_engine``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import NoReturn

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from censorwatch.runtime_secrets import database_authority


class CensorwatchBase(DeclarativeBase):
    pass


class CensorwatchPersistenceError(RuntimeError):
    """A required CensorWatch database write did not durably complete."""


def fail_persistence(session, *, operation: str, cause: BaseException) -> NoReturn:
    """Rollback best-effort, then raise a bounded error that cannot look successful.

    Hostile source content and database driver details must not be copied into task
    results or health records.  The original exception remains chained for local
    traceback diagnosis, while the public error text contains only our fixed
    operation label.
    """
    try:
        session.rollback()
    except Exception as rollback_error:
        raise CensorwatchPersistenceError(
            f"CensorWatch {operation} failed and rollback did not complete"
        ) from rollback_error
    raise CensorwatchPersistenceError(f"CensorWatch {operation} failed") from cause


def _engine(role: str, *, pool_size: int):
    authority = database_authority(role)
    transaction_mode = "on" if role == "reader" else "off"
    return create_engine(
        authority.url,
        pool_size=pool_size,
        max_overflow=2,
        pool_pre_ping=True,
        connect_args={
            "options": f"-c default_transaction_read_only={transaction_mode}"
        },
    )


@lru_cache(maxsize=1)
def admin_engine():
    return _engine("admin", pool_size=1)


@lru_cache(maxsize=1)
def writer_engine():
    return _engine("writer", pool_size=4)


@lru_cache(maxsize=1)
def reader_engine():
    return _engine("reader", pool_size=2)


@lru_cache(maxsize=1)
def _writer_factory():
    return sessionmaker(bind=writer_engine(), autocommit=False, autoflush=False)


@lru_cache(maxsize=1)
def _reader_factory():
    return sessionmaker(bind=reader_engine(), autocommit=False, autoflush=False)


def writer_session():
    """Return a CensorWatch writer session; never falls back to the main DB."""
    return _writer_factory()()


def reader_session():
    """Return an API read-only session with transaction-level read-only forced."""
    return _reader_factory()()
