"""Censored Planet transport retries remain bounded and fail soft."""

from __future__ import annotations

import io
import json
import urllib.error

from collectors import censored_planet as cp


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


def test_transient_failure_is_retried_then_returns_data(monkeypatch):
    attempts = []
    sleeps = []

    def open_once(_request, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise urllib.error.URLError("temporary")
        return _Response(json.dumps({"data": {"ok": True}}).encode())

    monkeypatch.setattr(cp.urllib.request, "urlopen", open_once)
    assert cp._gql("query { ok }", retries=1, sleeper=sleeps.append) == {"ok": True}
    assert len(attempts) == 2
    assert sleeps == [1]


def test_retry_budget_is_bounded(monkeypatch):
    attempts = []

    def fail(_request, timeout):
        attempts.append(timeout)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(cp.urllib.request, "urlopen", fail)
    assert cp._gql("query { ok }", retries=2, sleeper=lambda _seconds: None) is None
    assert len(attempts) == 3


def test_oversized_response_is_rejected_without_retry(monkeypatch):
    calls = []

    def oversized(_request, timeout):
        calls.append(timeout)
        return _Response(b"x" * (cp.MAX_RESPONSE_BYTES + 1))

    monkeypatch.setattr(cp.urllib.request, "urlopen", oversized)
    assert cp._gql("query { ok }", retries=2, sleeper=lambda _seconds: None) is None
    assert len(calls) == 1
