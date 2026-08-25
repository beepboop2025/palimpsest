"""Censored Planet's fixed POST boundary stays bounded and fail soft."""

from __future__ import annotations

import json

import pytest

from collectors import censored_planet as cp
from core.safe_fetch import FetchError, ResponseTooLarge


def _encoded(data):
    return json.dumps({"data": data}, separators=(",", ":")).encode()


def test_transient_failure_is_retried_then_returns_data():
    attempts = []
    sleeps = []

    def fetch(_url, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise FetchError("temporary and potentially URL-bearing")
        return _encoded({"ok": True})

    assert cp._gql(
        cp.INTERFERENCE_QUERY,
        retries=1,
        sleeper=sleeps.append,
        fetch_bytes=fetch,
    ) == {"ok": True}
    assert len(attempts) == 2
    assert sleeps == [1]


def test_retry_budget_is_bounded():
    attempts = []

    def fail(_url, **_kwargs):
        attempts.append(True)
        raise FetchError("down")

    assert cp._gql(
        cp.INTERFERENCE_QUERY,
        retries=2,
        sleeper=lambda _seconds: None,
        fetch_bytes=fail,
    ) is None
    assert len(attempts) == 3


def test_oversized_response_is_rejected_without_retry():
    calls = []

    def oversized(_url, **_kwargs):
        calls.append(True)
        raise ResponseTooLarge("hostile expansion")

    assert cp._gql(
        cp.INTERFERENCE_QUERY,
        retries=2,
        sleeper=lambda _seconds: None,
        fetch_bytes=oversized,
    ) is None
    assert len(calls) == 1


def test_graphql_uses_exact_bounded_post_contract():
    seen = {}

    def fetch(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _encoded({"ok": True})

    assert cp._gql(
        cp.INTERFERENCE_QUERY,
        {"range": {"startDate": "2026-01-01", "endDate": "2026-01-02"}},
        fetch_bytes=fetch,
    ) == {"ok": True}

    assert seen["url"] == cp.ENDPOINT
    assert seen["method"] == "POST"
    assert seen["max_redirects"] == 0
    assert seen["max_bytes"] == cp.MAX_RESPONSE_BYTES
    assert len(seen["body"]) <= cp.MAX_REQUEST_BYTES
    assert json.loads(seen["body"])["query"] == cp.INTERFERENCE_QUERY
    seen["url_policy"](cp.ENDPOINT)
    with pytest.raises(FetchError):
        seen["url_policy"]("https://127.0.0.1/query")


def test_unreviewed_query_and_unbounded_request_controls_are_refused():
    with pytest.raises(ValueError, match="reviewed set"):
        cp._gql("query { arbitraryExpansion }", fetch_bytes=lambda *_a, **_k: b"{}")
    with pytest.raises(ValueError, match="retries"):
        cp._gql(cp.INTERFERENCE_QUERY, retries=99)
    with pytest.raises(ValueError, match="timeout"):
        cp._gql(cp.INTERFERENCE_QUERY, timeout=0)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"data":{"ok":1},"data":{"ok":2}}',
        b'{"data":{"value":NaN}}',
        b"\xff",
    ],
)
def test_hostile_json_is_rejected_without_retry(payload):
    calls = []

    def fetch(_url, **_kwargs):
        calls.append(True)
        return payload

    assert cp._gql(
        cp.INTERFERENCE_QUERY,
        retries=2,
        sleeper=lambda _seconds: None,
        fetch_bytes=fetch,
    ) is None
    assert calls == [True]


@pytest.mark.parametrize(
    ("since", "until"),
    [
        ("2026-1-01", "2026-01-02"),
        ("2026-01-03", "2026-01-02"),
        ("2025-01-01", "2026-12-31"),
    ],
)
def test_date_window_is_canonical_ordered_and_bounded(since, until):
    with pytest.raises(ValueError):
        cp.cn_interference_rate(since, until)


def test_rate_must_be_finite_and_within_percent_range(monkeypatch):
    monkeypatch.setattr(
        cp,
        "_gql",
        lambda *_a, **_k: {
            "interferenceRateByCountry": [
                {"country": "CN", "unexpectedRate": 4.271}
            ]
        },
    )
    assert cp.cn_interference_rate("2026-01-01", "2026-01-02") == 4.27

    monkeypatch.setattr(
        cp,
        "_gql",
        lambda *_a, **_k: {
            "interferenceRateByCountry": [
                {"country": "CN", "unexpectedRate": float("inf")}
            ]
        },
    )
    assert cp.cn_interference_rate("2026-01-01", "2026-01-02") is None


def test_timeseries_is_bounded_normalized_and_within_requested_window(monkeypatch):
    monkeypatch.setattr(
        cp,
        "_gql",
        lambda *_a, **_k: {
            "cenalertTimeseries": [{"date": "2026-01-02", "value": 2}]
        },
    )
    assert cp.cn_timeseries("2026-01-01", "2026-01-03") == [
        {"date": "2026-01-02", "value": 2.0}
    ]

    monkeypatch.setattr(
        cp,
        "_gql",
        lambda *_a, **_k: {
            "cenalertTimeseries": [{"date": "2030-01-01", "value": 2}]
        },
    )
    assert cp.cn_timeseries("2026-01-01", "2026-01-03") == []


def test_result_cardinality_caps_fail_closed(monkeypatch):
    monkeypatch.setattr(
        cp,
        "_gql",
        lambda *_a, **_k: {
            "cenalertEvents": [{}] * (cp.MAX_EVENT_ROWS + 1),
            "cenalertTimeseries": [{}] * (cp.MAX_TIMESERIES_ROWS + 1),
        },
    )
    assert cp.cn_events("2026-01-01", "2026-01-02") == []
    assert cp.cn_timeseries("2026-01-01", "2026-01-02") == []
