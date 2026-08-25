"""Credential-free Playwright gateway for reviewed hostile public surfaces.

Run this module only in its disposable Compose service.  It receives one reviewed source
and URL, pins every permitted hostname to a public address before Chromium starts, blocks
unreviewed requests at the browser-context router, and returns only bounded inert HTML.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from censorwatch.source_policy import (
    enforce_source_url,
    source_network_policy,
    source_url_is_allowed,
)
from core.safe_fetch import FetchError, _validate_public

_BROWSER_SOURCES = frozenset({"weibo_search", "xueqiu"})
_DEFAULT_MAX_HTML_BYTES = 8 * 1024 * 1024
_MAX_HTML_BYTES = 16 * 1024 * 1024
_DEFAULT_TIMEOUT_S = 40
_MAX_TIMEOUT_S = 60
_DEFAULT_SETTLE_MS = 1500
_MAX_SETTLE_MS = 5000


def _bounded_env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


class RenderRequest(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=16 * 1024)
    referer: str | None = Field(default=None, max_length=16 * 1024)


class RenderResponse(BaseModel):
    status: int | None
    html: str
    final_url: str


def _pins_for_source(source: str) -> dict[str, str]:
    """Resolve reviewed hosts once; omitted hosts are unavailable, never ambient."""
    policy = source_network_policy(source)
    pins: dict[str, str] = {}
    for host in sorted(policy.hosts_for("render")):
        try:
            answers = _validate_public(host)
        except FetchError:
            continue
        # Prefer IPv4 for Chromium's resolver-rule syntax, then use the first
        # globally routable answer.  Every answer was already required public.
        ordered = sorted(answers, key=lambda answer: answer[0] != socket.AF_INET)
        pins[host] = ordered[0][1]
    return pins


def _resolver_rules(pins: dict[str, str]) -> str:
    rules = []
    for host, raw_ip in sorted(pins.items()):
        ip = ipaddress.ip_address(raw_ip)
        rendered = f"[{ip}]" if ip.version == 6 else str(ip)
        rules.append(f"MAP {host} {rendered}")
    rules.append("MAP * ~NOTFOUND")
    return ", ".join(rules)


def _request_is_allowed(
    source: str,
    url: str,
    *,
    resource_type: str,
    pins: dict[str, str],
) -> bool:
    """Browser document requests use page policy; subresources use render policy."""
    purpose = "page" if resource_type == "document" else "render"
    if not source_url_is_allowed(source, url, purpose=purpose):
        return False
    try:
        host = urlsplit(url).hostname
    except (TypeError, ValueError, UnicodeError):
        return False
    return bool(host and host.lower() in pins)


def _proxy_launch_config() -> tuple[dict[str, str] | None, dict[str, str]]:
    """Return only the operator-supplied browser proxy; never inherit ambient env."""
    value = (os.getenv("CENSORWATCH_GATEWAY_PROXY_URL") or "").strip()
    if not value:
        return None, {}
    try:
        parts = urlsplit(value)
        port = parts.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FetchError("render proxy URL is invalid") from exc
    if (
        parts.scheme not in {"http", "https", "socks5"}
        or not parts.hostname
        or port is None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise FetchError("render proxy URL is not canonical")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    config = {"server": f"{parts.scheme}://{host}:{port}"}
    if parts.username is not None:
        config["username"] = parts.username
    if parts.password is not None:
        config["password"] = parts.password
    answers = _validate_public(parts.hostname)
    ordered = sorted(answers, key=lambda answer: answer[0] != socket.AF_INET)
    return config, {parts.hostname.lower(): ordered[0][1]}


async def _render_page(request: RenderRequest) -> RenderResponse:
    from playwright.async_api import async_playwright

    enforce_source_url(request.source, request.url, purpose="page")
    if request.source not in _BROWSER_SOURCES:
        raise FetchError("source does not require browser execution")
    if request.referer:
        enforce_source_url(request.source, request.referer, purpose="page")
    pins = _pins_for_source(request.source)
    initial_host = urlsplit(request.url).hostname
    if not initial_host or initial_host.lower() not in pins:
        raise FetchError("initial render host has no validated public pin")

    max_html_bytes = _bounded_env_int(
        "CENSORWATCH_GATEWAY_MAX_HTML_BYTES",
        _DEFAULT_MAX_HTML_BYTES,
        low=1024,
        high=_MAX_HTML_BYTES,
    )
    timeout_s = _bounded_env_int(
        "CENSORWATCH_GATEWAY_TIMEOUT_S",
        _DEFAULT_TIMEOUT_S,
        low=5,
        high=_MAX_TIMEOUT_S,
    )
    settle_ms = _bounded_env_int(
        "CENSORWATCH_GATEWAY_SETTLE_MS",
        _DEFAULT_SETTLE_MS,
        low=0,
        high=_MAX_SETTLE_MS,
    )
    proxy, proxy_pins = _proxy_launch_config()
    async with async_playwright() as playwright:
        launch = {
            "headless": True,
            "args": [
                f"--host-resolver-rules={_resolver_rules(pins | proxy_pins)}",
                "--disable-background-networking",
                "--disable-breakpad",
                "--disable-component-update",
                "--disable-sync",
                "--no-first-run",
            ],
        }
        if proxy:
            launch["proxy"] = proxy
        browser = await playwright.chromium.launch(**launch)
        try:
            context = await browser.new_context(
                accept_downloads=False,
                service_workers="block",
                user_agent="Mozilla/5.0 (Palimpsest hostile-source quarantine renderer)",
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"},
            )

            async def route_request(route, browser_request):
                if _request_is_allowed(
                    request.source,
                    browser_request.url,
                    resource_type=browser_request.resource_type,
                    pins=pins,
                ):
                    await route.continue_()
                else:
                    await route.abort("blockedbyclient")

            await context.route("**/*", route_request)
            page = await context.new_page()
            response = await page.goto(
                request.url,
                referer=request.referer,
                wait_until="domcontentloaded",
                timeout=timeout_s * 1000,
            )
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            final_url = page.url
            enforce_source_url(request.source, final_url, purpose="page")
            html = await page.content()
            if len(html.encode("utf-8")) > max_html_bytes:
                raise FetchError("rendered HTML exceeds byte budget")
            return RenderResponse(
                status=response.status if response else None,
                html=html,
                final_url=final_url,
            )
        finally:
            await browser.close()


app = FastAPI(
    title="Palimpsest CensorWatch render gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
_RENDER_SLOT = asyncio.Semaphore(1)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/render", response_model=RenderResponse)
async def render(request: RenderRequest) -> RenderResponse:
    timeout_s = _bounded_env_int(
        "CENSORWATCH_GATEWAY_TIMEOUT_S",
        _DEFAULT_TIMEOUT_S,
        low=5,
        high=_MAX_TIMEOUT_S,
    )
    try:
        async with _RENDER_SLOT:
            return await asyncio.wait_for(_render_page(request), timeout=timeout_s + 5)
    except Exception:  # noqa: BLE001 - sanitize the whole browser/process boundary
        # The privileged worker needs an abstention code, never browser internals,
        # local paths, proxy credentials, or attacker-controlled URL text.
        raise HTTPException(status_code=502, detail="render_refused") from None
