"""Proxy-aware, polite HTTP fetcher — shared by collectors and the detector.

Responsibilities:
- Honor ``settings.proxy_url`` (we run from outside China; datacenter exits get
  403'd by Weibo, so the proxy is load-bearing for the velocity signal).
- Apply a randomized inter-request delay in ``[min_delay_s, max_delay_s]`` and
  rotate User-Agents (politeness / ban-avoidance).
- Optionally render JS-heavy pages via Playwright (Weibo search) behind the same
  ``fetch()`` signature, so callers don't care which engine ran.

Always returns an ``interfaces.FetchResult`` — transport failures become
``status=None`` + ``error`` rather than exceptions, so the classifier can map them
to ``UNKNOWN`` uniformly (a thrown exception mid-cycle would otherwise look like a
deletion to careless callers).
"""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

from censorwatch.config import CensorwatchSettings, get_settings
from censorwatch.interfaces import FetchResult

logger = logging.getLogger(__name__)


class Fetcher:
    """Async fetcher with proxy, jitter, and UA rotation.

    A ``transport`` may be injected (e.g. ``httpx.MockTransport``) for tests so the
    full path is exercised without a network.
    """

    def __init__(
        self,
        settings: CensorwatchSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        rng: random.Random | None = None,
    ):
        self.s = settings or get_settings()
        self._rng = rng or random.Random()
        # httpx reads no_proxy/env separately; we pass proxy explicitly so the
        # censorwatch proxy is independent of any ambient HTTP_PROXY for other code.
        client_kwargs = dict(
            timeout=self.s.request_timeout_s,
            follow_redirects=True,
        )
        if self.s.proxy_url:
            client_kwargs["proxy"] = self.s.proxy_url
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    # ── politeness ───────────────────────────────────────────────
    async def _jitter(self):
        """Sleep a uniform-random interval to look human and avoid bans."""
        lo, hi = self.s.min_delay_s, self.s.max_delay_s
        delay = self._rng.uniform(lo, hi) if hi > lo else lo
        if delay > 0:
            await asyncio.sleep(delay)

    def _headers(self, referer: str | None = None) -> dict:
        h = {
            "User-Agent": self._rng.choice(self.s.user_agents),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if referer:
            h["Referer"] = referer
        return h

    # ── fetch ────────────────────────────────────────────────────
    async def fetch(
        self,
        url: str,
        *,
        referer: str | None = None,
        render: bool = False,
        polite: bool = True,
    ) -> FetchResult:
        """GET ``url`` and return a FetchResult. Never raises on transport errors.

        ``render=True`` routes through Playwright for JS-heavy pages.
        ``polite=False`` skips the jitter (used by the liveness probe, which we
        want fast and which is a single known-stable URL).
        """
        if polite:
            await self._jitter()
        if render:
            return await self._render(url, referer=referer)
        try:
            resp = await self._client.get(url, headers=self._headers(referer))
            return FetchResult(
                url=url,
                status=resp.status_code,
                text=resp.text,
                final_url=str(resp.url),
            )
        except httpx.TimeoutException as e:
            return FetchResult(url=url, status=None, text=None, error=f"timeout:{e}")
        except httpx.HTTPError as e:
            return FetchResult(url=url, status=None, text=None, error=f"http_error:{e}")

    async def fetch_bytes(
        self, url: str, *, referer: str | None = None, polite: bool = False
    ) -> tuple[int | None, bytes | None, str | None]:
        """GET raw bytes (for archiving images). Returns (status, content, error).

        Never raises — a failed image download must not abort an archive run.
        Defaults to ``polite=False`` since a post may reference many images and
        per-image jitter would make archiving impractically slow.
        """
        if polite:
            await self._jitter()
        try:
            resp = await self._client.get(url, headers=self._headers(referer))
            return resp.status_code, resp.content, None
        except httpx.HTTPError as e:
            return None, None, f"http_error:{e}"

    async def _render(self, url: str, referer: str | None = None) -> FetchResult:
        """Render a JS-heavy page with Playwright (lazy import).

        If Playwright isn't installed/usable, return a transport error → UNKNOWN,
        never a false deletion.
        """
        try:
            from playwright.async_api import async_playwright
        except Exception as e:  # pragma: no cover - optional dependency
            return FetchResult(url=url, status=None, text=None,
                               error=f"playwright_unavailable:{e}")
        try:
            async with async_playwright() as p:
                launch_kwargs = {}
                if self.s.proxy_url:
                    launch_kwargs["proxy"] = {"server": self.s.proxy_url}
                browser = await p.chromium.launch(headless=True, **launch_kwargs)
                try:
                    page = await browser.new_page(
                        user_agent=self._rng.choice(self.s.user_agents),
                        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
                    )
                    resp = await page.goto(url, wait_until="networkidle",
                                           timeout=self.s.request_timeout_s * 1000)
                    text = await page.content()
                    return FetchResult(
                        url=url,
                        status=resp.status if resp else None,
                        text=text,
                        final_url=page.url,
                    )
                finally:
                    await browser.close()
        except Exception as e:  # pragma: no cover - runtime/network dependent
            return FetchResult(url=url, status=None, text=None, error=f"render_error:{e}")
