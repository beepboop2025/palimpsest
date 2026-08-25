"""Bounded client for the one fixed internal CensorWatch render gateway."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx

from censorwatch.interfaces import FetchResult
from censorwatch.source_policy import source_url_is_allowed

_GATEWAY_HOST = "censorwatch-render-gateway"
_GATEWAY_PORT = 8080


def _render_endpoint(gateway_url: str) -> str:
    try:
        parts = urlsplit(gateway_url)
        port = parts.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("invalid render gateway URL") from exc
    if (
        parts.scheme != "http"
        or parts.hostname != _GATEWAY_HOST
        or port != _GATEWAY_PORT
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("render gateway must be the fixed internal service")
    return f"http://{_GATEWAY_HOST}:{_GATEWAY_PORT}/render"


async def render_via_gateway(
    gateway_url: str,
    *,
    source: str,
    url: str,
    referer: str | None,
    timeout: float,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchResult:
    """Render one page and validate the gateway response before returning it."""
    try:
        endpoint = _render_endpoint(gateway_url)
    except ValueError:
        return FetchResult(url=url, status=None, text=None, error="render_gateway_invalid")
    if type(max_bytes) is not int or max_bytes <= 0:
        return FetchResult(url=url, status=None, text=None, error="render_gateway_invalid")
    response_cap = min(33 * 1024 * 1024, max_bytes * 2 + 64 * 1024)
    client_args = {
        "timeout": timeout + 5,
        "follow_redirects": False,
        "trust_env": False,
    }
    if transport is not None:
        client_args["transport"] = transport
    try:
        async with httpx.AsyncClient(**client_args) as client, client.stream(
            "POST",
            endpoint,
            json={"source": source, "url": url, "referer": referer},
            headers={"Accept": "application/json"},
        ) as response:
            if response.status_code != 200:
                return FetchResult(
                    url=url,
                    status=None,
                    text=None,
                    error=f"render_gateway_http_{response.status_code}",
                )
            if "application/json" not in response.headers.get(
                "Content-Type", ""
            ).lower():
                return FetchResult(
                    url=url, status=None, text=None, error="render_gateway_malformed"
                )
            declared = response.headers.get("Content-Length")
            if declared and (not declared.isdecimal() or int(declared) > response_cap):
                return FetchResult(
                    url=url, status=None, text=None, error="render_gateway_oversized"
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > response_cap:
                    return FetchResult(
                        url=url,
                        status=None,
                        text=None,
                        error="render_gateway_oversized",
                    )
    except (httpx.HTTPError, OSError, TimeoutError):
        return FetchResult(url=url, status=None, text=None, error="render_gateway_error")

    try:
        payload = json.loads(bytes(body))
        status = payload.get("status")
        html = payload["html"]
        final_url = payload["final_url"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return FetchResult(url=url, status=None, text=None, error="render_gateway_malformed")
    if (
        (status is not None and (type(status) is not int or not 100 <= status <= 599))
        or type(html) is not str
        or len(html.encode("utf-8")) > max_bytes
        or type(final_url) is not str
        or not source_url_is_allowed(source, final_url, purpose="page")
    ):
        return FetchResult(url=url, status=None, text=None, error="render_gateway_malformed")
    return FetchResult(url=url, status=status, text=html, final_url=final_url)
