"""Offline tests for BasePostCollector — the BaseCollector→censorwatch bridge.

Covers the two parts that don't need Postgres:
  - _rows_from_df: pure row-building (idempotency key, hash + first_seen fill,
    drops rows with no stable post_id).
  - observe(): re-fetch → classify, via an injected fake fetcher (verifies the
    detector path maps a deleted body to GONE and a throttle to UNKNOWN).

The DB write in _upsert (pg_insert ON CONFLICT) requires a live Postgres and is
exercised in the docker-compose integration run, not here.

    python3 -m pytest censorwatch/tests/test_base_post_collector.py
    python3 censorwatch/tests/test_base_post_collector.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from censorwatch.collectors.base_post_collector import BasePostCollector
from censorwatch.interfaces import FetchResult, LivenessState, Post


class _FakeFetcher:
    """Returns a queued FetchResult regardless of URL."""
    def __init__(self, result: FetchResult):
        self._result = result
        self.closed = False

    async def fetch(self, url, **kw):
        return self._result

    async def aclose(self):
        self.closed = True


class _Source(BasePostCollector):
    """Minimal concrete source for testing the base class."""
    name = "test_source"
    network_policy_name = "eastmoney_guba"
    deletion_markers = ("该帖子可能已被删除",)

    async def collect(self): return []
    async def parse(self, raw): return pd.DataFrame()
    def validate(self, df): return True
    def control_posts(self): return ["https://guba.eastmoney.com/control"]


def _make() -> _Source:
    return _Source({"schedule": "*/10 * * * *"})


def test_rows_from_df_fills_and_keys():
    s = _make()
    df = pd.DataFrame([
        {"post_id": "p1", "author": "老张", "full_text": "  茅台  走势 ",
         "url": "https://guba.eastmoney.com/p1"},
        {"post_id": "p2", "author": None, "full_text": "second",
         "url": "https://guba.eastmoney.com/p2",
         "content_hash": "precomputed"},
        {"post_id": "", "full_text": "no id — must be dropped",
         "url": "https://guba.eastmoney.com/p3"},
    ])
    rows = s._rows_from_df(df, raw_path=None)
    assert len(rows) == 2, "row with empty post_id must be dropped"
    r1, r2 = rows
    assert r1["source"] == "test_source" and r1["post_id"] == "p1"
    assert r1["content_hash"] and r1["content_hash"] != "precomputed"  # computed
    assert r2["content_hash"] == "precomputed"  # parser-supplied hash respected
    assert all(r["first_seen_at"] is not None and r["last_state"] == "live"
               for r in rows)


def _observe(result: FetchResult) -> LivenessState:
    s = _make()
    s._fetcher = _FakeFetcher(result)
    post = Post(source="test_source", post_id="p1",
                url="https://guba.eastmoney.com/p1", full_text="x")
    obs = asyncio.run(s.observe(post))
    return obs.state


def test_observe_maps_states():
    # A source-specific deletion notice → GONE.
    gone = _observe(FetchResult(url="u1", status=200,
                    text="<div>该帖子可能已被删除</div>" + "页面框架填充内容" * 20))
    assert gone == LivenessState.GONE
    # A throttle (429) → UNKNOWN, never deleted.
    unknown = _observe(FetchResult(url="u1", status=429, text="too many requests"))
    assert unknown == LivenessState.UNKNOWN
    # A live page → LIVE.
    live = _observe(FetchResult(url="u1", status=200,
                    text="茅台基本面没变,长期看好,仅供参考不构成建议。" * 3))
    assert live == LivenessState.LIVE


def test_poisoned_urls_are_never_stored_archived_or_fetched(monkeypatch):
    source = _make()
    rows = source._rows_from_df(
        pd.DataFrame(
            [
                {"post_id": "good", "full_text": "ok",
                 "url": "https://guba.eastmoney.com/good"},
                {"post_id": "metadata", "full_text": "bad",
                 "url": "http://169.254.169.254/latest/meta-data/"},
                {"post_id": "userinfo", "full_text": "bad",
                 "url": "https://guba.eastmoney.com@127.0.0.1/"},
            ]
        ),
        raw_path=None,
    )
    assert [row["post_id"] for row in rows] == ["good"]

    called = []
    source._fetcher = _FakeFetcher(
        FetchResult(url="unused", status=200, text="should not run")
    )
    source._fetcher.fetch = lambda *args, **kwargs: called.append(args)  # type: ignore[method-assign]
    post = Post(
        source="test_source",
        post_id="old-poisoned-row",
        url="http://127.0.0.1/admin",
        full_text="legacy",
    )
    obs = asyncio.run(source.observe(post))
    assert obs.state == LivenessState.UNKNOWN
    assert obs.reason == "url_policy_rejected"
    assert called == []


def test_archive_retry_claim_is_bounded_and_rotates_failures(monkeypatch):
    now = datetime.now(timezone.utc)
    candidates = [
        SimpleNamespace(
            id=i,
            post_id=f"p{i}",
            url=f"https://example.com/{i}",
            first_seen_at=now - timedelta(minutes=i),
            extra_data=({} if i < 3 else {"archive_retry_at": "2026-08-10T00:00:00+00:00"}),
        )
        for i in range(1, 7)
    ]

    class FakeQuery:
        def __init__(self):
            self.limit_value = None
        def filter(self, *args):
            return self
        def order_by(self, *args):
            self.order = args
            return self
        def limit(self, value):
            self.limit_value = value
            return self
        def all(self):
            # The real database supplies oldest-attempt-first ordering; emulate
            # the first fair page and verify the production hard limit.
            return candidates[:self.limit_value]

    query = FakeQuery()

    class FakeDB:
        committed = False
        rolled_back = False
        closed = False
        def query(self, model): return query
        def commit(self): self.committed = True
        def rollback(self): self.rolled_back = True
        def close(self): self.closed = True

    db = FakeDB()
    monkeypatch.setattr("api.database.SessionLocal", lambda: db)
    source = _Source({"archive_retry_batch": 2})
    claimed = source._claim_archive_retry_rows(exclude_ids={"new-post"})

    assert [row["post_id"] for row in claimed] == ["p1", "p2"]
    assert query.limit_value == 2
    assert db.committed and db.closed and not db.rolled_back
    for candidate in candidates[:2]:
        assert candidate.extra_data["archive_retry_count"] == 1
        assert candidate.extra_data["archive_retry_at"]


def test_archive_retry_batch_has_hard_bounds():
    assert _Source({"archive_retry_batch": 0}).archive_retry_batch == 1
    assert _Source({"archive_retry_batch": 10_000}).archive_retry_batch == 100
    assert _Source({"archive_retry_batch": "bad"}).archive_retry_batch == 20


def test_hostile_rows_are_bounded_before_database_work():
    source = _Source({"max_records_per_cycle": 2})
    rows = source._rows_from_df(
        pd.DataFrame(
            [
                {
                    "post_id": str(index),
                    "full_text": "bounded",
                    "url": f"https://guba.eastmoney.com/{index}",
                }
                for index in range(10)
            ]
        ),
        raw_path=None,
    )

    assert [row["post_id"] for row in rows] == ["0", "1"]
    assert _Source({"max_records_per_cycle": 10_000}).max_records_per_cycle == 1000
    assert _Source({"max_records_per_cycle": 0}).max_records_per_cycle == 1


def test_hostile_exception_text_never_reaches_results_health_or_logs(caplog):
    source = _Source({"retry_count": 1})
    reports = []
    durable_logs = []

    async def explode():
        raise ValueError("hostile-body secret=proxy-password")

    source.collect = explode
    source._report_health = lambda status, message="": reports.append((status, message))
    source._log_collection = lambda *args: durable_logs.append(args)

    with caplog.at_level("ERROR"):
        result = asyncio.run(source.run())

    combined = repr((result, reports, durable_logs, caplog.text))
    assert result["error"] == "ValueError"
    assert "hostile-body" not in combined
    assert "proxy-password" not in combined


@pytest.mark.parametrize("value", (None, "", "bad\nsource", "a" * 65))
def test_task_identifier_shape_is_bounded(value):
    from censorwatch.tasks import _identifier

    assert _identifier(value) is None


def _run_all():
    test_rows_from_df_fills_and_keys()
    print("  PASS rows_from_df")
    test_observe_maps_states()
    print("  PASS observe_maps_states")
    print("\n2/2 base_post_collector checks passed")


if __name__ == "__main__":
    _run_all()
