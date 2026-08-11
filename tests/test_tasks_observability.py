"""Task wrappers keep scheduler identities stable on every terminal path."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from core.tasks import _run_with_lease


class _Lock:
    def __init__(self, acquired):
        self.acquired = acquired

    def acquire(self, **_kwargs):
        return self.acquired

    def release(self):
        pass


class _Redis:
    def __init__(self, acquired):
        self.acquired = acquired

    def lock(self, *_args, **_kwargs):
        return _Lock(self.acquired)

    def close(self):
        pass


def _install_redis(monkeypatch, *, acquired):
    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(
            from_url=lambda *_args, **_kwargs: _Redis(acquired)
        ),
    )


def test_lease_skip_uses_monitoring_source_not_internal_lock_name(monkeypatch):
    _install_redis(monkeypatch, acquired=False)

    result = _run_with_lease(
        "snapshot:ooni-gfw",
        lambda: {"status": "success"},
        timeout_s=60,
        collector_name="ooni-gfw",
    )

    assert result["status"] == "skipped"
    assert result["collector"] == "ooni-gfw"


def test_operation_exception_uses_same_stable_monitoring_source(monkeypatch):
    _install_redis(monkeypatch, acquired=True)

    def fail():
        raise OSError("upstream failed")

    result = _run_with_lease(
        "processor:ddti-index",
        fail,
        timeout_s=60,
        collector_name="ddti-index",
    )

    assert result["status"] == "failed"
    assert result["collector"] == "ddti-index"
