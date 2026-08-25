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
        self.predicates = []
        self.order = ()
        self.limit_value = None

    def filter(self, predicate):
        self.predicates.append(predicate)
        column = getattr(getattr(predicate, "left", None), "name", None)
        if column == "deleted_at":
            self._rows = [row for row in self._rows if row.deleted_at is None]
        elif column == "source":
            source_name = predicate.right.value
            self._rows = [row for row in self._rows if row.source == source_name]
        return self

    def order_by(self, *_args):
        self.order = _args
        return self

    def limit(self, value):
        self.limit_value = value
        self._rows = self._rows[:value]
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None
        self.commits = 0

    def query(self, _model):
        self.last_query = _FakeQuery(self._rows)
        return self.last_query

    def add(self, _row):
        raise AssertionError("LIVE observations must not append deletions")

    def commit(self):
        self.commits += 1

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
        last_state="live",
        last_checked_at=None,
        extra_data={},
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

    import censorwatch.db as database
    import censorwatch.registry as registry

    session = _FakeSession(rows)
    monkeypatch.setattr(database, "writer_session", lambda: session)
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
    assert len(session.last_query.predicates) == 4
    assert "coalesce" in str(session.last_query.predicates[2]).lower()
    assert "coalesce" in str(session.last_query.predicates[3]).lower()
    assert "last_checked_at" in str(session.last_query.order[0])
    assert session.last_query.limit_value == 10
    assert collector.closed


def test_recheck_commits_bounded_chunks_and_advances_fair_worklist(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        _row(
            index,
            "eastmoney_guba",
            f"pending-{index}",
            now - timedelta(hours=2),
        )
        for index in range(1, 53)
    ]
    collector = _Collector(now)
    session = _FakeSession(rows)

    import censorwatch.db as database
    import censorwatch.registry as registry

    monkeypatch.setattr(database, "writer_session", lambda: session)
    monkeypatch.setattr(registry, "get_collector", lambda _name: collector)

    result = asyncio.run(
        recheck_source(
            "eastmoney_guba",
            min_age_hours=1,
            max_age_hours=3,
            batch_limit=10_000,
        )
    )

    assert result["checked"] == 52
    assert session.last_query.limit_value == 500
    assert session.commits == 3  # 25 + 25 + final 2
    assert collector.observed_post_ids == [f"pending-{index}" for index in range(1, 53)]


def test_redelivery_of_same_observation_run_does_not_recheck_or_advance(monkeypatch):
    now = datetime.now(timezone.utc)
    row = _row(1, "eastmoney_guba", "same-run", now - timedelta(hours=1))
    collector = _Collector(now)
    session = _FakeSession([row])

    import censorwatch.db as database
    import censorwatch.registry as registry

    monkeypatch.setattr(database, "writer_session", lambda: session)
    monkeypatch.setattr(registry, "get_collector", lambda _name: collector)

    first = asyncio.run(
        recheck_source(
            "eastmoney_guba",
            observation_run_id="celery-delivery-1",
        )
    )
    second = asyncio.run(
        recheck_source(
            "eastmoney_guba",
            observation_run_id="celery-delivery-1",
        )
    )

    assert first["checked"] == 1
    assert second["checked"] == 0
    assert collector.observed_post_ids == ["same-run"]
    assert row.check_count == 1


def test_gone_confirmations_require_distinct_runs_and_minimum_span(monkeypatch):
    now = datetime.now(timezone.utc)
    row = _row(1, "eastmoney_guba", "bounded-gone", now - timedelta(hours=1))
    row.gone_streak = 1
    row.last_state = "gone"
    row.extra_data = {
        "detector_last_accepted_gone_at": (now - timedelta(seconds=60)).isoformat()
    }

    class GoneCollector(_Collector):
        async def observe(self, post):
            if post.post_id == "__control__":
                return Observation(state=LivenessState.LIVE, checked_at=self.checked_at)
            self.observed_post_ids.append(post.post_id)
            return Observation(state=LivenessState.GONE, checked_at=self.checked_at)

    class DeletionSession(_FakeSession):
        def __init__(self, rows):
            super().__init__(rows)
            self.added = []

        def add(self, row):
            self.added.append(row)

    collector = GoneCollector(now)
    session = DeletionSession([row])

    import censorwatch.db as database
    import censorwatch.registry as registry

    monkeypatch.setattr(database, "writer_session", lambda: session)
    monkeypatch.setattr(registry, "get_collector", lambda _name: collector)

    result = asyncio.run(
        recheck_source(
            "eastmoney_guba",
            settings=SimpleNamespace(confirmations=2, enabled=False),
            observation_run_id="celery-delivery-2",
        )
    )

    assert result["checked"] == 1 and result["confirmed"] == 0
    assert row.gone_streak == 1
    assert session.added == []
