"""Offline response-boundary tests for the internal render gateway client."""

from __future__ import annotations

import asyncio
import json

import httpx

from censorwatch.render_client import render_via_gateway


def _run(handler, *, gateway="http://censorwatch-render-gateway:8080", max_bytes=1024):
    return asyncio.run(
        render_via_gateway(
            gateway,
            source="weibo_search",
            url="https://s.weibo.com/weibo?q=test",
            referer="https://s.weibo.com/",
            timeout=5,
            max_bytes=max_bytes,
            transport=httpx.MockTransport(handler),
        )
    )


def test_valid_bounded_gateway_response_maps_to_fetch_result():
    payload = {
        "status": 200,
        "html": "<html>正文</html>",
        "final_url": "https://s.weibo.com/weibo?q=test",
    }
    result = _run(lambda _request: httpx.Response(200, json=payload))
    assert result.status == 200 and result.text == payload["html"] and result.transport_ok


def test_gateway_redirect_and_host_expansion_are_refused():
    bad_final = {
        "status": 200,
        "html": "ok",
        "final_url": "http://127.0.0.1/admin",
    }
    result = _run(lambda _request: httpx.Response(200, json=bad_final))
    assert result.status is None and result.error == "render_gateway_malformed"
    invalid_gateway = _run(
        lambda _request: (_ for _ in ()).throw(AssertionError("must not connect")),
        gateway="http://127.0.0.1:8080",
    )
    assert invalid_gateway.error == "render_gateway_invalid"


def test_gateway_body_is_streamed_through_a_hard_cap():
    body = json.dumps(
        {
            "status": 200,
            "html": "x" * 70_000,
            "final_url": "https://s.weibo.com/",
        }
    ).encode()
    result = _run(
        lambda _request: httpx.Response(
            200, content=body, headers={"Content-Type": "application/json"}
        ),
        max_bytes=128,
    )
    assert result.status is None and result.error == "render_gateway_oversized"


def test_gateway_errors_are_sanitized():
    result = _run(lambda _request: httpx.Response(502, text="browser secret /tmp/path"))
    assert result.error == "render_gateway_http_502"
    assert "secret" not in result.error
