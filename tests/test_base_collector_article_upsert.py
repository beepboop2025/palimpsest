"""The recurring article ingest is idempotent and preserves DDTI evidence."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy.dialects import postgresql

import api.database
from collectors.ddti_probe import DDTIProbeCollector
from core.exceptions import StorageError


class _Session:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.statements = []
        self.commits = 0
        self.closed = 0

    def execute(self, statement):
        if self.fail:
            raise RuntimeError("database unavailable")
        self.statements.append(statement)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed += 1


def _frame():
    published = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
    row = {
        "title": "A censorship-vault item",
        "full_text": "body",
        "url": "https://example.test/items/42",
        "author": "cdt_root_head",
        "published_at": published,
        "category": "ddti_deletion",
        "metadata": {"tags": ["Censorship Vault", "Economy"]},
    }
    # Feed overlap can put the same URL in one batch. It must become one upsert,
    # not two conflicting INSERTs that roll back the whole transaction.
    return pd.DataFrame([row, dict(row)])


def test_article_path_uses_url_hash_conflict_update_and_keeps_metadata(monkeypatch):
    session = _Session()
    monkeypatch.setattr(api.database, "SessionLocal", lambda: session)
    collector = DDTIProbeCollector({"deletion_feeds": []})

    count = asyncio.run(collector._upsert(_frame(), "/data/raw/ddti/round.json"))
    asyncio.run(collector.close())

    assert count == 1
    assert session.commits == 1
    assert len(session.statements) == 1
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "ON CONFLICT (url_hash) DO UPDATE" in sql
    assert "extra_data = excluded.extra_data" in sql
    assert {"tags": ["Censorship Vault", "Economy"]} in compiled.params.values()


def test_database_failure_cannot_be_reported_as_a_successful_zero(monkeypatch):
    session = _Session(fail=True)
    monkeypatch.setattr(api.database, "SessionLocal", lambda: session)
    collector = DDTIProbeCollector({"deletion_feeds": []})

    with pytest.raises(StorageError, match="database unavailable"):
        asyncio.run(collector._upsert(_frame(), "/data/raw/ddti/round.json"))
    asyncio.run(collector.close())

    assert session.commits == 0
    assert session.closed == 1
