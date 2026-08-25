"""Bounded hostile-content acquisition for CensorWatch.

Every ordinary page and asset read passes through :mod:`core.safe_fetch`: exact source
authority, public-IP validation and pinning, redirect replay, TLS verification, and body /
decompression limits are therefore one contract. Browser-required sources abstain unless
the credential-free render gateway is configured; this worker never launches a local browser.

Transport refusal becomes ``FetchResult(status=None)``. That maps to UNKNOWN downstream,
never to a deletion, preserving the detector's evidence semantics.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlparse

from censorwatch.config import CensorwatchSettings, get_settings
from censorwatch.interfaces import FetchResult
from censorwatch.source_policy import source_network_policy, source_url_policy
from core.safe_fetch import (
    FetchError,
    ResponseTooLarge,
    SafeFetchResponse,
    safe_fetch_response,
)

logger = logging.getLogger(__name__)

_CACHE_MAX_ENTRIES = 512
_CHARSET = re.compile(
    r"(?:^|;)\s*charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE
)
_ALLOWED_CHARSETS = frozenset({"utf-8", "utf8", "gb18030", "gbk", "big5"})

ResponseFetcher = Callable[..., SafeFetchResponse]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return None


def _decode_text(response: SafeFetchResponse) -> str:
    content_type = _header(response.headers, "Content-Type") or ""
    match = _CHARSET.search(content_type)
    charset = match.group(1).casefold() if match else "utf-8"
    if charset not in _ALLOWED_CHARSETS:
        charset = "utf-8"
    try:
        return response.body.decode(charset, "replace")
    except LookupError:  # defensive; the allowlist above should make this unreachable
        return response.body.decode("utf-8", "replace")


class Fetcher:
    """Async facade over the pinned, bounded CensorWatch acquisition transport."""

    def __init__(
        self,
        settings: CensorwatchSettings | None = None,
        *,
        source: str,
        response_fetcher: ResponseFetcher = safe_fetch_response,
        rng: random.Random | None = None,
        clock=time.monotonic,
    ):
        self.s = settings or get_settings()
        self.source = source
        self._response_fetcher = response_fetcher
        self._rng = rng or random.Random()
        self._clock = clock
        self._host_last: dict[str, float] = {}
        self._cache: dict[str, dict] = {}
        self._cache_bytes = 0
        self._cycle_image_bytes = 0
        # Resolve policy at construction so an unregistered source cannot create
        # an ambiently privileged client and fail only after its task starts.
        policy = source_network_policy(source)
        self._page_policy = source_url_policy(source, purpose="page")
        self._page_policy("https://" + next(iter(policy.page_hosts)) + "/")

    async def aclose(self):
        """Compatibility hook; the hardened transport owns no persistent connection."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def _jitter(self):
        lo, hi = self.s.min_delay_s, self.s.max_delay_s
        delay = self._rng.uniform(lo, hi) if hi > lo else lo
        if delay > 0:
            await asyncio.sleep(delay)

    async def _host_pace(self, url: str):
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

    def _conditional_headers(self, url: str) -> dict[str, str]:
        cached = self._cache.get(url)
        if not cached:
            return {}
        headers = {}
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
        return headers

    def _forget(self, url: str) -> None:
        old = self._cache.pop(url, None)
        if old:
            self._cache_bytes = max(0, self._cache_bytes - int(old["bytes"]))

    def _remember_validators(
        self, url: str, response: SafeFetchResponse, text: str
    ) -> None:
        etag = _header(response.headers, "ETag")
        last_modified = _header(response.headers, "Last-Modified")
        if not (etag or last_modified):
            self._forget(url)
            return
        size = len(response.body)
        self._forget(url)
        if size > self.s.max_cache_bytes:
            return
        while self._cache and (
            len(self._cache) >= _CACHE_MAX_ENTRIES
            or self._cache_bytes + size > self.s.max_cache_bytes
        ):
            self._forget(next(iter(self._cache)))
        self._cache[url] = {
            "etag": etag,
            "last_modified": last_modified,
            "status": response.status,
            "text": text,
            "final_url": response.url,
            "bytes": size,
        }
        self._cache_bytes += size

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self._rng.choice(self.s.user_agents),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if referer:
            try:
                self._page_policy(referer)
            except FetchError:
                logger.warning("[censorwatch:%s] refused unreviewed Referer", self.source)
            else:
                headers["Referer"] = referer
        return headers

    async def _acquire(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        max_bytes: int,
        purpose: str,
    ) -> SafeFetchResponse:
        policy = source_url_policy(self.source, purpose=purpose)
        policy(url)
        response = await asyncio.to_thread(
            self._response_fetcher,
            url,
            max_bytes=max_bytes,
            timeout=self.s.request_timeout_s,
            max_redirects=self.s.max_redirects,
            headers=headers,
            proxy=self.s.proxy_url,
            url_policy=policy,
        )
        if len(response.body) > max_bytes:
            raise ResponseTooLarge("acquisition seam exceeded its byte budget")
        return response

    @staticmethod
    def _error_result(url: str, exc: BaseException) -> FetchResult:
        # Do not echo attacker-controlled paths, proxy credentials, or upstream
        # response text into durable Celery results and logs.
        return FetchResult(
            url=url,
            status=None,
            text=None,
            error=f"fetch_refused:{type(exc).__name__}",
        )

    async def fetch(
        self,
        url: str,
        *,
        referer: str | None = None,
        render: bool = False,
        polite: bool = True,
    ) -> FetchResult:
        """Fetch one reviewed page; all ambiguity becomes an UNKNOWN-compatible result."""
        if render:
            return await self._render(url, referer=referer)
        try:
            self._page_policy(url)
        except FetchError as exc:
            return self._error_result(url, exc)
        await self._host_pace(url)
        if polite:
            await self._jitter()
        headers = self._headers(referer)
        headers.update(self._conditional_headers(url))
        try:
            response = await self._acquire(
                url,
                headers=headers,
                max_bytes=self.s.max_page_bytes,
                purpose="page",
            )
        except (FetchError, OSError, TimeoutError) as exc:
            return self._error_result(url, exc)

        cached = self._cache.get(url)
        if response.status == 304 and cached:
            return FetchResult(
                url=url,
                status=cached["status"],
                text=cached["text"],
                final_url=cached["final_url"],
                not_modified=True,
            )
        text = _decode_text(response)
        if response.status == 200:
            self._remember_validators(url, response, text)
        return FetchResult(
            url=url,
            status=response.status,
            text=text,
            final_url=response.url,
        )

    async def fetch_bytes(
        self,
        url: str,
        *,
        referer: str | None = None,
        polite: bool = False,
        max_bytes: int | None = None,
    ) -> tuple[int | None, bytes | None, str | None]:
        """Fetch one reviewed asset through the same redirect and size boundary."""
        if polite:
            await self._jitter()
        remaining_cycle = self.s.max_cycle_image_bytes - self._cycle_image_bytes
        requested = self.s.max_image_bytes if max_bytes is None else max_bytes
        cap = min(self.s.max_image_bytes, requested, remaining_cycle)
        if cap <= 0:
            return None, None, "fetch_refused:image_budget_exhausted"
        try:
            response = await self._acquire(
                url,
                headers=self._headers(referer),
                max_bytes=cap,
                purpose="asset",
            )
        except (FetchError, OSError, TimeoutError) as exc:
            return None, None, f"fetch_refused:{type(exc).__name__}"
        self._cycle_image_bytes += len(response.body)
        return response.status, response.body, None

    async def _render(self, url: str, referer: str | None = None) -> FetchResult:
        """Render only through the fixed, bounded, credential-free gateway."""
        try:
            self._page_policy(url)
            if referer:
                self._page_policy(referer)
        except FetchError as exc:
            return self._error_result(url, exc)
        if not self.s.render_gateway_url:
            return FetchResult(
                url=url,
                status=None,
                text=None,
                error="render_gateway_unconfigured",
            )
        from censorwatch.render_client import render_via_gateway

        return await render_via_gateway(
            self.s.render_gateway_url,
            source=self.source,
            url=url,
            referer=referer,
            timeout=self.s.request_timeout_s,
            max_bytes=self.s.max_page_bytes,
        )
