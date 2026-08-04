"""Offline regression tests for per-source detector worklists."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from censorwatch.detector import recheck_source
from censorwatch.interfaces import LivenessState, Observation


class _FakeQuery:
    """Apply the two production predicates to in-memory rows."""

    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, predicate):
        column = getattr(getattr(predicate, "left", None), "name", None)
        if column == "deleted_at":
            self._rows = [row for row in self._rows if row.deleted_at is None]
        elif column == "source":
            source_name = predicate.right.value
            self._rows = [row for row in self._rows if row.source == source_name]
        return self

    def order_by(self, *_args):
        return self

    def limit(self, value):
        self._rows = self._rows[:value]
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _FakeQuery(self._rows)

    def add(self, _row):
        raise AssertionError("LIVE observations must not append deletions")

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _Collector:
    name = "eastmoney_guba"

    def __init__(self, checked_at):
        self.checked_at = checked_at
        self.observed_post_ids = []
        self.closed = False

    def control_posts(self):
        return ["https://example.invalid/control"]

    async def observe(self, post):
        if post.post_id != "__control__":
            self.observed_post_ids.append(post.post_id)
        return Observation(state=LivenessState.LIVE, checked_at=self.checked_at)

    async def close(self):
        self.closed = True


def _row(row_id, source, post_id, posted_at, *, deleted_at=None):
    return SimpleNamespace(
        id=row_id,
        source=source,
        post_id=post_id,
        posted_at=posted_at,
        first_seen_at=posted_at,
        deleted_at=deleted_at,
        url=f"https://example.invalid/{post_id}",
        full_text="offline fixture",
        gone_streak=0,
        check_count=0,
    )


def test_recheck_observes_only_pending_rows_for_requested_source(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        _row(1, "eastmoney_guba", "target-pending", now - timedelta(minutes=5)),
        _row(2, "xueqiu", "other-source", now - timedelta(minutes=5)),
        _row(
            3,
            "eastmoney_guba",
            "target-deleted",
            now - timedelta(minutes=5),
            deleted_at=now - timedelta(minutes=1),
        ),
    ]
    collector = _Collector(now)

    import api.database as database
    import censorwatch.registry as registry

    monkeypatch.setattr(database, "SessionLocal", lambda: _FakeSession(rows))
    monkeypatch.setattr(registry, "get_collector", lambda _name: collector)

    result = asyncio.run(
        recheck_source(
            "eastmoney_guba",
            min_age_hours=0,
            max_age_hours=1,
            batch_limit=10,
        )
    )

    assert result["checked"] == 1
    assert collector.observed_post_ids == ["target-pending"]
    assert collector.closed
