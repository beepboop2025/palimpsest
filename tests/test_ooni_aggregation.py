"""Hostile-response and retry contracts for the shared OONI transport."""

from __future__ import annotations

import logging

import pytest

from collectors import in_path_interference, ooni_gfw
from collectors.ooni_aggregation import OONI_AGG, fetch_aggregation_json
from core.safe_fetch import FetchError, ResponseTooLarge, SafeFetchResponse


PARAMS = {
    "probe_cc": "CN",
    "test_name": "web_connectivity",
    "since": "2026-08-01",
    "until": "2026-08-08",
}
LOGGER = logging.getLogger("test-ooni-aggregation")


def _response(body=b'{"result":{}}', *, status=200, url=None, headers=None):
    return SafeFetchResponse(
        status=status,
        headers=headers or {"Content-Type": "application/json"},
        body=body,
        url=url or f"{OONI_AGG}?probe_cc=CN&test_name=web_connectivity&since=2026-08-01&until=2026-08-08",
    )


def _call(fetcher, **changes):
    kwargs = {
        "user_agent": "Palimpsest test",
        "timeout": 3,
        "retries": 0,
        "max_bytes": 4096,
        "retry_delay": lambda attempt: 1,
        "logger": LOGGER,
        "fetcher": fetcher,
        "sleep": lambda delay: None,
    }
    kwargs.update(changes)
    return fetch_aggregation_json(PARAMS, **kwargs)


def test_ooni_transport_pins_exact_query_and_hard_limits():
    calls = []

    def fetcher(url, **kwargs):
        calls.append((url, kwargs))
        kwargs["url_policy"](url)
        return _response(url=url)

    assert _call(fetcher) == {"result": {}}
    [(url, kwargs)] = calls
    assert url.startswith(OONI_AGG + "?")
    assert kwargs["timeout"] == 3
    assert kwargs["max_bytes"] == 4096
    assert kwargs["max_redirects"] == 0
    assert kwargs["headers"]["Accept"] == "application/json"
    with pytest.raises(FetchError, match="authority or query changed"):
        kwargs["url_policy"]("http://127.0.0.1/private")


def test_ooni_transport_preserves_each_collectors_rate_limit_backoff():
    def responses():
        values = [_response(status=429), _response()]
        return lambda url, **kwargs: values.pop(0)

    gfw_delays = []
    assert ooni_gfw._get(
        PARAMS,
        retries=1,
        fetcher=responses(),
        sleep=gfw_delays.append,
    ) == {"result": {}}
    assert gfw_delays == [5]

    in_path_delays = []
    assert in_path_interference._get(
        PARAMS,
        retries=1,
        fetcher=responses(),
        sleep=in_path_delays.append,
    ) == {"result": {}}
    assert in_path_delays == [1]


@pytest.mark.parametrize(
    "body",
    (
        b"[]",
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b"\xff",
    ),
)
def test_ooni_transport_rejects_ambiguous_json(body):
    assert _call(lambda url, **kwargs: _response(body, url=url)) is None


def test_ooni_transport_rejects_wrong_media_final_url_and_oversize():
    assert _call(
        lambda url, **kwargs: _response(
            url=url, headers={"Content-Type": "text/html"}
        )
    ) is None
    assert _call(
        lambda url, **kwargs: _response(url="https://attacker.example/result")
    ) is None

    def oversized(url, **kwargs):
        raise ResponseTooLarge("hostile body")

    assert _call(oversized) is None


def test_ooni_transport_rejects_unreviewed_queries_before_egress():
    called = False

    def fetcher(url, **kwargs):
        nonlocal called
        called = True
        return _response(url=url)

    invalid = dict(PARAMS, probe_cc="US", arbitrary="value")
    assert fetch_aggregation_json(
        invalid,
        user_agent="Palimpsest test",
        timeout=3,
        retries=0,
        max_bytes=4096,
        retry_delay=lambda attempt: 1,
        logger=LOGGER,
        fetcher=fetcher,
    ) is None
    assert called is False
