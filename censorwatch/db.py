"""Table creation + session helpers for censorwatch.

``create_tables()`` creates ONLY the three censorwatch tables — it passes an
explicit ``tables=`` list to ``create_all`` so it physically cannot create,
alter, or drop any production table. This is the censorwatch analogue of
``api.database.init_db()`` (which the DDTI table-bootstrap used the same way).
"""

from __future__ import annotations

import logging

from api.database import Base, SessionLocal, engine
from censorwatch import models

logger = logging.getLogger(__name__)

# The exhaustive set of tables censorwatch owns. Anything not in this list is
# off-limits — create_all(tables=...) is scoped strictly to these.
_OWNED_TABLES = (
    models.CensoredPost.__table__,
    models.PostDeletion.__table__,
    models.DeletionVelocitySnapshot.__table__,
)


def create_tables() -> list[str]:
    """Create the censorwatch tables if missing. Returns the table names touched.

    Safe and idempotent: ``create_all`` only creates tables that don't already
    exist, and the ``tables=`` scope guarantees no production table is involved.
    """
    Base.metadata.create_all(bind=engine, tables=list(_OWNED_TABLES))
    names = [t.name for t in _OWNED_TABLES]
    logger.info("[censorwatch] ensured tables: %s", ", ".join(names))
    return names


def get_session():
    """Return a new SQLAlchemy session (caller owns close())."""
    return SessionLocal()
