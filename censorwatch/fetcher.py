"""Proxy-aware, polite HTTP fetcher — shared by collectors and the detector.

Responsibilities:
- Honor ``settings.proxy_url`` (we run from outside China; datacenter exits get
  403'd by Weibo, so the proxy is load-bearing for the velocity signal).
- Apply a randomized inter-request delay in ``[min_delay_s, max_delay_s]`` and
  rotate User-Agents (politeness / ban-avoidance).
- Enforce a per-host minimum interval (``settings.host_min_interval_s``) on top of
  the jitter: the jitter spaces requests globally, this bounds pressure per origin,
  so one collector fanning out over many posts on one platform can never hammer it.
  Off by default (0), preserving prior behavior.
- Revalidate with conditional GETs: when a prior response carried an ``ETag`` /
  ``Last-Modified`` validator, the next fetch of the same URL sends
  ``If-None-Match`` / ``If-Modified-Since`` and maps a 304 back to the cached body
  (``FetchResult.not_modified=True``). Politeness (the origin skips re-sending an
  unchanged body — most re-check fetches) and signal at once: a 304 is the origin
  itself asserting the post is unchanged, a strong LIVE tell for the detector.
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
import time
from urllib.parse import urlparse

import httpx

from censorwatch.config import CensorwatchSettings, get_settings
from censorwatch.interfaces import FetchResult

logger = logging.getLogger(__name__)

# Conditional-GET cache bound. Per-Fetcher (a Fetcher lives for one collector run), so
# this only needs to cover one cycle's URLs; oldest-inserted entries are evicted first.
_CACHE_MAX_ENTRIES = 512


class Fetcher:
    """Async fetcher with proxy, jitter, UA rotation, per-host pacing, and
    conditional-GET revalidation.

    A ``transport`` may be injected (e.g. ``httpx.MockTransport``) for tests so the
    full path is exercised without a network. ``clock`` (monotonic seconds) is
    injectable so the per-host pacer is deterministic under test.
    """

    def __init__(
        self,
        settings: CensorwatchSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        rng: random.Random | None = None,
        clock=time.monotonic,
    ):
        self.s = settings or get_settings()
        self._rng = rng or random.Random()
        self._clock = clock
        self._host_last: dict[str, float] = {}   # host → last-request time (pacer)
        self._cache: dict[str, dict] = {}        # url → {etag, last_modified, status, text, final_url}
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

    async def _host_pace(self, url: str):
        """Enforce the per-host minimum interval (if configured) before a request.

        Applies to page fetches only — fetch_bytes (image archiving) deliberately
        keeps its own polite=False fast path, since a post may reference dozens of
        images and per-image pacing would make first-capture archiving too slow to
        beat the censor."""
        interval = self.s.host_min_interval_s
        if interval <= 0:
            return
        host = urlparse(url).netloc
        if not host:
            return
        last = self._host_last.get(host)
        now = self._clock()
        if last is not None:
            wait = interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._host_last[host] = self._clock()

    # ── conditional-GET revalidation ─────────────────────────────
    def _conditional_headers(self, url: str) -> dict:
        """If-None-Match / If-Modified-Since from the cached validators, if any."""
        c = self._cache.get(url)
        if not c:
            return {}
        h = {}
        if c.get("etag"):
            h["If-None-Match"] = c["etag"]
        if c.get("last_modified"):
            h["If-Modified-Since"] = c["last_modified"]
        return h

    def _remember_validators(self, url: str, resp) -> None:
        """Cache the body of a 200 that carries a validator, for the next revalidation."""
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        if not (etag or last_modified):
            return
        if url not in self._cache and len(self._cache) >= _CACHE_MAX_ENTRIES:
            self._cache.pop(next(iter(self._cache)))   # evict oldest-inserted
        self._cache[url] = {
            "etag": etag, "last_modified": last_modified,
            "status": resp.status_code, "text": resp.text, "final_url": str(resp.url),
        }

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
        await self._host_pace(url)
        if polite:
            await self._jitter()
        if render:
            return await self._render(url, referer=referer)
        headers = self._headers(referer)
        headers.update(self._conditional_headers(url))
        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.TimeoutException as e:
            return FetchResult(url=url, status=None, text=None, error=f"timeout:{e}")
        except httpx.HTTPError as e:
            return FetchResult(url=url, status=None, text=None, error=f"http_error:{e}")
        cached = self._cache.get(url)
        if resp.status_code == 304 and cached:
            # The origin asserted "unchanged since your validator" — replay the cached
            # body so the classifier sees the same LIVE content it saw last time.
            return FetchResult(
                url=url,
                status=cached["status"],
                text=cached["text"],
                final_url=cached["final_url"],
                not_modified=True,
            )
        if resp.status_code == 200:
            self._remember_validators(url, resp)
        return FetchResult(
            url=url,
            status=resp.status_code,
            text=resp.text,
            final_url=str(resp.url),
        )

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
