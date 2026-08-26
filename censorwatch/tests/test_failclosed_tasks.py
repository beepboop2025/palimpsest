"""Focused regressions for CensorWatch persistence and task fail-closed paths."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from censorwatch.collectors.base_post_collector import BasePostCollector
from censorwatch.db import CensorwatchPersistenceError
from censorwatch.interfaces import LivenessState, Observation


class _Source(BasePostCollector):
    name = "test_source"
    network_policy_name = "eastmoney_guba"

    async def collect(self):
        return [{"post_id": "p1"}]

    async def parse(self, _raw):
        return pd.DataFrame(
            [
                {
                    "post_id": "p1",
                    "full_text": "bounded",
                    "url": "https://guba.eastmoney.com/p1",
                }
            ]
        )

    def validate(self, _df):
        return True

    def control_posts(self):
        return ["https://guba.eastmoney.com/control"]


class _InsertStatement:
    def values(self, _rows):
        return self

    def on_conflict_do_nothing(self, **_kwargs):
        return self

    def returning(self, _column):
        return self


def test_upsert_database_error_marks_base_lifecycle_failed(monkeypatch):
    class FailingSession:
        rolled_back = False
        closed = False

        def execute(self, _statement):
            raise RuntimeError("hostile-row secret=must-not-escape")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    session = FailingSession()
    monkeypatch.setattr("censorwatch.db.writer_session", lambda: session)
    monkeypatch.setattr(
        "sqlalchemy.dialects.postgresql.insert", lambda _model: _InsertStatement()
    )

    source = _Source({"retry_count": 1, "log_collection": False})
    source._store_raw = lambda _raw: "/bounded/raw.json"
    health = []
    source._report_health = lambda status, message="": health.append((status, message))
    source._maybe_alert = lambda: None
    source._circuit_breaker = SimpleNamespace(
        can_execute=lambda: True,
        record_success=lambda: None,
        record_failure=lambda: None,
        failure_count=1,
    )

    result = asyncio.run(source.run())

    assert result["status"] == "failed"
    assert result["error"] == "CensorwatchPersistenceError"
    assert health[-1] == ("failed", "CensorwatchPersistenceError")
    assert session.rolled_back and session.closed
    assert "hostile-row" not in repr((result, health))


def test_archive_retry_claim_database_error_propagates(monkeypatch):
    class FailingQuery:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, _limit):
            return self

        def all(self):
            raise RuntimeError("database-row-content")

    class FailingSession:
        rolled_back = False
        closed = False

        def query(self, _model):
            return FailingQuery()

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    session = FailingSession()
    monkeypatch.setattr("censorwatch.db.writer_session", lambda: session)

    with pytest.raises(CensorwatchPersistenceError) as failure:
        _Source({"archive_retry_batch": 2})._claim_archive_retry_rows(exclude_ids=set())

    assert "database-row-content" not in str(failure.value)
    assert session.rolled_back and session.closed


def test_archive_path_database_error_is_not_swallowed(monkeypatch):
    import censorwatch.archiver as archiver

    source = _Source({})
    monkeypatch.setattr(source, "_get_fetcher", lambda: object())

    async def archived(*_args, **_kwargs):
        return "/private/archive/path"

    monkeypatch.setattr(archiver, "archive_post", archived)

    def persistence_failure(*_args, **_kwargs):
        raise CensorwatchPersistenceError("CensorWatch archive path update failed")

    monkeypatch.setattr(source, "_set_archive_path", persistence_failure)
    with pytest.raises(CensorwatchPersistenceError):
        asyncio.run(
            source._archive_new(
                {
                    "post_id": "p1",
                    "url": "https://guba.eastmoney.com/p1",
                }
            )
        )


def test_archive_path_update_rolls_back_and_raises(monkeypatch):
    class FailingUpdate:
        def filter_by(self, **_kwargs):
            return self

        def update(self, _values):
            raise RuntimeError("driver detail")

    class FailingSession:
        rolled_back = False
        closed = False

        def query(self, _model):
            return FailingUpdate()

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    session = FailingSession()
    monkeypatch.setattr("censorwatch.db.writer_session", lambda: session)

    with pytest.raises(CensorwatchPersistenceError):
        _Source({})._set_archive_path("p1", "/private/archive/path")

    assert session.rolled_back and session.closed


class _DetectorQuery:
    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, _limit):
        return self

    def all(self):
        return []


class _FailingDetectorSession:
    rolled_back = False
    closed = False

    def query(self, _model):
        return _DetectorQuery()

    def commit(self):
        raise RuntimeError("database-detail-not-for-result")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _DetectorCollector:
    name = "eastmoney_guba"

    def __init__(self):
        self.closed = False

    def control_posts(self):
        return ["https://guba.eastmoney.com/control"]

    async def observe(self, _post):
        return Observation(
            state=LivenessState.LIVE,
            checked_at=datetime.now(timezone.utc),
        )

    async def close(self):
        self.closed = True


def test_detector_commit_error_raises_and_closes(monkeypatch):
    import censorwatch.detector as detector

    session = _FailingDetectorSession()
    collector = _DetectorCollector()
    monkeypatch.setattr("censorwatch.db.writer_session", lambda: session)
    monkeypatch.setattr("censorwatch.registry.get_collector", lambda _source: collector)
    health = []
    monkeypatch.setattr(
        detector,
        "_report_detector_health",
        lambda source, status: health.append((source, status)),
    )

    with pytest.raises(CensorwatchPersistenceError) as failure:
        asyncio.run(
            detector.recheck_source(
                "eastmoney_guba",
                settings=SimpleNamespace(confirmations=2, enabled=True),
            )
        )

    assert "database-detail" not in str(failure.value)
    assert session.rolled_back and session.closed and collector.closed
    assert health == [("eastmoney_guba", "failed")]


class _Transaction:
    def __init__(self, cache):
        self.cache = cache
        self.pending_delete = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def watch(self, key):
        self.cache.commands.append(("watch", key))

    def get(self, key):
        self.cache.commands.append(("get", key))
        return self.cache.values.get(key)

    def unwatch(self):
        self.cache.commands.append(("unwatch",))

    def multi(self):
        self.cache.commands.append(("multi",))

    def delete(self, key):
        self.cache.commands.append(("delete", key))
        self.pending_delete = key

    def execute(self):
        self.cache.commands.append(("exec",))
        if self.cache.watch_failures:
            from redis.exceptions import WatchError

            self.cache.watch_failures -= 1
            raise WatchError("simulated concurrent replacement")
        existed = self.pending_delete in self.cache.values
        self.cache.values.pop(self.pending_delete, None)
        return [1 if existed else 0]


class _Cache:
    def __init__(self, *, acquire=True, set_error=None, watch_failures=0):
        self.acquire = acquire
        self.set_error = set_error
        self.watch_failures = watch_failures
        self.values = {}
        self.sets = []
        self.commands = []
        self.closed = False

    def set(self, key, value, **kwargs):
        if self.set_error:
            raise self.set_error
        self.sets.append((key, value, kwargs))
        if kwargs.get("nx") and not self.acquire:
            return False
        self.values[key] = value
        return True

    def pipeline(self):
        return _Transaction(self)

    def close(self):
        self.closed = True


def test_detector_health_is_bounded_and_scoped(monkeypatch):
    import censorwatch.cache as cache_module
    import censorwatch.detector as detector

    cache = _Cache()
    monkeypatch.setattr(cache_module, "open_writer_cache", lambda: cache)
    detector._report_detector_health("eastmoney_guba", "degraded")

    key, payload, options = cache.sets[0]
    assert key == "health:detector:eastmoney_guba"
    assert options == {"ex": 7200}
    parsed = json.loads(payload)
    assert parsed["source"] == "eastmoney_guba"
    assert parsed["status"] == "degraded"
    assert len(payload) < 160
    assert cache.closed


def test_task_lease_ttl_exceeds_limit_and_release_is_token_safe(monkeypatch):
    import censorwatch.cache as cache_module
    import censorwatch.tasks as tasks

    cache = _Cache()
    monkeypatch.setattr(cache_module, "open_writer_cache", lambda: cache)
    with tasks._task_lease("cw_collect", identity="eastmoney_guba") as acquired:
        assert acquired
        key, token, options = cache.sets[0]
        assert key == "censorwatch:task-lease:cw_collect:eastmoney_guba"
        assert options == {"nx": True, "ex": 1620}
        assert cache.values[key] == token

    assert key not in cache.values
    assert [command[0] for command in cache.commands] == [
        "watch",
        "get",
        "multi",
        "delete",
        "exec",
    ]
    assert cache.closed


def test_task_lease_never_deletes_successor_token(monkeypatch):
    import censorwatch.cache as cache_module
    import censorwatch.tasks as tasks

    cache = _Cache()
    monkeypatch.setattr(cache_module, "open_writer_cache", lambda: cache)
    key = "censorwatch:task-lease:cw_signal"
    with pytest.raises(tasks.CensorwatchTaskError, match="release failed"):
        with tasks._task_lease("cw_signal") as acquired:
            assert acquired
            cache.values[key] = "successor-token"

    assert cache.values[key] == "successor-token"
    assert ("unwatch",) in cache.commands


def test_task_lease_release_retries_watch_conflict_boundedly():
    import censorwatch.tasks as tasks

    cache = _Cache(watch_failures=2)
    key = "censorwatch:task-lease:cw_signal"
    cache.values[key] = "owned-token"
    assert tasks._release_task_lease(cache, key=key, token="owned-token") is True
    assert [command[0] for command in cache.commands].count("watch") == 3
    assert key not in cache.values

    cache = _Cache(watch_failures=3)
    cache.values[key] = "owned-token"
    assert tasks._release_task_lease(cache, key=key, token="owned-token") is False
    assert [command[0] for command in cache.commands].count("watch") == 3
    assert cache.values[key] == "owned-token"


@contextmanager
def _acquired_lease(*_args, **_kwargs):
    yield True


@contextmanager
def _busy_lease(*_args, **_kwargs):
    yield False


def test_redelivery_behind_orphan_lease_never_acks_success(monkeypatch):
    import censorwatch.signal as signal_module
    import censorwatch.tasks as tasks

    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(enabled=True))
    monkeypatch.setattr(tasks, "_task_lease", _busy_lease)
    monkeypatch.setattr(
        signal_module,
        "run_signal",
        lambda: pytest.fail("busy task must not run the signal body"),
    )

    with pytest.raises(tasks.CensorwatchTaskError) as failure:
        tasks.cw_signal.run()
    assert "already held" in str(failure.value.__cause__)


def test_failed_collector_result_raises_instead_of_false_success(monkeypatch):
    import censorwatch.registry as registry
    import censorwatch.tasks as tasks

    class FailedCollector:
        async def run(self):
            return {"status": "failed", "error": "CensorwatchPersistenceError"}

    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(enabled=True))
    monkeypatch.setattr(tasks, "_task_lease", _acquired_lease)
    monkeypatch.setattr(registry, "get_collector", lambda _source: FailedCollector())

    with pytest.raises(tasks.CensorwatchTaskError):
        tasks.cw_collect.run("eastmoney_guba")


def test_task_exception_is_raised_with_bounded_text(monkeypatch):
    import censorwatch.signal as signal_module
    import censorwatch.tasks as tasks

    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(enabled=True))
    monkeypatch.setattr(tasks, "_task_lease", _acquired_lease)

    def explode():
        raise RuntimeError("hostile secret=do-not-serialize")

    monkeypatch.setattr(signal_module, "run_signal", explode)
    with pytest.raises(tasks.CensorwatchTaskError) as failure:
        tasks.cw_signal.run()

    assert "hostile" not in str(failure.value)
    assert "secret" not in str(failure.value)


def test_heartbeat_persists_bounded_timestamp_with_ttl(monkeypatch):
    import censorwatch.cache as cache_module
    import censorwatch.tasks as tasks

    heartbeat_cache = _Cache()
    monkeypatch.setattr(
        cache_module, "open_control_cache", lambda: heartbeat_cache
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(enabled=True))

    result = tasks.cw_heartbeat.run()

    assert result["status"] == "ok"
    key, payload, options = heartbeat_cache.sets[0]
    assert key == "censorwatch:beat:heartbeat"
    assert payload == json.dumps(
        {"timestamp": result["timestamp"]}, separators=(",", ":"), sort_keys=True
    )
    assert len(payload) <= 48
    assert options == {"ex": 300}
    assert heartbeat_cache.closed


def test_heartbeat_persistence_error_fails_task(monkeypatch):
    import censorwatch.cache as cache_module
    import censorwatch.tasks as tasks

    heartbeat_cache = _Cache(set_error=RuntimeError("redis detail"))
    monkeypatch.setattr(
        cache_module, "open_control_cache", lambda: heartbeat_cache
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(enabled=True))

    with pytest.raises(tasks.CensorwatchTaskError):
        tasks.cw_heartbeat.run()
