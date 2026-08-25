"""Inside View's command-and-poll capability is narrow and hostile-input safe."""

from __future__ import annotations

import json

import pytest

from collectors import inside_view as iv
from core.safe_fetch import FetchError, SafeFetchResponse


def _response(payload, *, status=200, content_type="application/json"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {} if content_type is None else {"Content-Type": content_type}
    return SafeFetchResponse(status=status, headers=headers, body=body, url=iv.API)


def test_request_uses_exact_bounded_post_contract():
    seen = {}

    def fetch(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _response({"id": "measurement_123"}, status=201)

    body = {
        "type": "dns",
        "target": "torproject.org",
        "limit": iv.CN_PROBES,
        "locations": [{"magic": f"CN+{asn}"} for asn in iv.CLOUD_ASNS],
    }
    assert iv._request(iv.API, body, fetch_response=fetch) == {
        "id": "measurement_123"
    }
    assert seen["url"] == iv.API
    assert seen["method"] == "POST"
    assert seen["max_redirects"] == 0
    assert seen["max_bytes"] == iv.MAX_RESPONSE_BYTES
    assert len(seen["body"]) <= iv.MAX_REQUEST_BYTES
    seen["url_policy"](iv.API)
    with pytest.raises(FetchError):
        seen["url_policy"]("https://127.0.0.1/v1/measurements")


def test_poll_request_is_get_and_only_accepts_bounded_ids():
    seen = {}

    def fetch(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _response({"status": "finished", "results": []})

    assert iv._request(f"{iv.API}/abc_123", fetch_response=fetch)["results"] == []
    assert seen["method"] == "GET"
    assert seen["body"] is None

    for bad in ("../admin", "a/b", "", "x" * 129):
        with pytest.raises(iv.GlobalpingError):
            iv._collect(bad, poll=0, tries=1)


def test_rate_limit_and_non_success_statuses_stay_distinct():
    with pytest.raises(iv.RateLimited):
        iv._request(
            iv.API,
            {},
            fetch_response=lambda *_a, **_k: _response({}, status=429),
        )
    with pytest.raises(iv.GlobalpingHTTPError) as error:
        iv._request(
            iv.API,
            {},
            fetch_response=lambda *_a, **_k: _response({}, status=503),
        )
    assert error.value.status == 503


@pytest.mark.parametrize(
    "payload",
    [
        b'{"id":"first","id":"second"}',
        b'{"value":NaN}',
        b"[]",
        b"\xff",
    ],
)
def test_hostile_response_json_is_rejected(payload):
    with pytest.raises(iv.GlobalpingError):
        iv._request(
            iv.API,
            fetch_response=lambda *_a, **_k: _response(payload),
        )


def test_non_json_media_type_is_rejected():
    with pytest.raises(iv.GlobalpingError, match="not JSON"):
        iv._request(
            iv.API,
            fetch_response=lambda *_a, **_k: _response(
                {"status": "finished"}, content_type="text/html"
            ),
        )


def test_create_accepts_only_reviewed_target_location_and_probe_shapes(monkeypatch):
    asked = []

    def request(url, body):
        asked.append((url, body))
        return {"id": "bounded-id"}

    monkeypatch.setattr(iv, "_request", request)
    controls = [{"country": country} for country in iv.CONTROL_COUNTRIES]
    clouds = [{"magic": f"CN+{asn}"} for asn in iv.CLOUD_ASNS]

    assert iv._create("torproject.org", controls, iv.CONTROL_PROBES) == "bounded-id"
    assert iv._create("torproject.org", clouds, iv.CN_PROBES) == "bounded-id"
    assert len(asked) == 2

    with pytest.raises(ValueError, match="reviewed panel"):
        iv._create("torproject.org", [{"country": "CN"}], 1)
    with pytest.raises(ValueError, match="reviewed panel"):
        iv._create("torproject.org", clouds, iv.CN_PROBES + 1)
    with pytest.raises(ValueError, match="canonical ASCII domain"):
        iv._create("http://127.0.0.1/", controls, iv.CONTROL_PROBES)
    assert len(asked) == 2


def test_results_are_reduced_and_cardinality_capped():
    raw = [{
        "probe": {
            "city": "Beijing",
            "country": "CN",
            "asn": 45090,
            "network": "bounded network",
            "secret_extra": "discarded",
        },
        "result": {
            "answers": [{"type": "A", "value": "1.1.1.1"}],
            "rawOutput": "discarded",
        },
        "extra": "discarded",
    }]
    assert iv._normalize_results(raw) == [{
        "probe": {
            "city": "Beijing",
            "country": "CN",
            "asn": 45090,
            "network": "bounded network",
        },
        "result": {"answers": [{"type": "A", "value": "1.1.1.1"}]},
    }]

    with pytest.raises(iv.GlobalpingError, match="cardinality"):
        iv._normalize_results([{}] * (iv.MAX_PROBE_RESULTS + 1))
    with pytest.raises(iv.GlobalpingError, match="invalid A answer"):
        iv._normalize_results([{
            "probe": {"asn": 45090},
            "result": {"answers": [{"type": "A", "value": "not-an-ip"}]},
        }])


def test_poll_and_panel_budgets_are_bounded(monkeypatch):
    monkeypatch.setattr(
        iv,
        "_request",
        lambda _url: {"status": "finished", "results": []},
    )
    assert iv._collect("measurement-1", poll=0, tries=1) == []
    with pytest.raises(ValueError, match="tries"):
        iv._collect("measurement-1", poll=0, tries=iv.MAX_POLL_TRIES + 1)
    with pytest.raises(ValueError, match="time budget"):
        iv._collect("measurement-1", poll=iv.MAX_POLL_SECONDS, tries=2)
    with pytest.raises(ValueError, match="panel"):
        iv.observe_panel([])
    with pytest.raises(ValueError, match="panel"):
        iv.observe_panel([{}] * (iv.MAX_PANEL_ENTRIES + 1))
