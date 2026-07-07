"""Tests for the fetcher's conditional-GET revalidation and per-host politeness ceiling.

    python3 -m pytest censorwatch/tests/test_fetcher_politeness.py

Runs the full path via httpx.MockTransport (no network) and an injected clock (no real
sleeping). Verifies: validators are replayed as If-None-Match / If-Modified-Since; a 304
maps back to the cached body with not_modified=True (a strong LIVE tell, never a false
deletion); a changed 200 refreshes the cache; the per-host pacer spaces same-host requests
and ignores cross-host ones; interval 0 (the default) changes nothing.
"""

from __future__ import annotations

import asyncio
import random

import httpx

from censorwatch.config import CensorwatchSettings
from censorwatch.fetcher import Fetcher


def _settings(**over) -> CensorwatchSettings:
    base = dict(
        enabled=True, proxy_url=None,
        min_delay_s=0.0, max_delay_s=0.0, request_timeout_s=5.0,
        confirmations=3, archive_dir="/tmp/cw",
        velocity_window_min=60, velocity_baseline_windows=24, spike_z_threshold=3.0,
    )
    base.update(over)
    return CensorwatchSettings(**base)


# ── conditional GET / ETag revalidation ─────────────────────────────────────────

async def _revalidation_case():
    seen_conditionals = []

    def handler(req):
        inm = req.headers.get("If-None-Match")
        seen_conditionals.append(inm)
        if inm == 'W/"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text="帖子正文 body v1",
                              headers={"ETag": 'W/"v1"', "Last-Modified": "Mon, 06 Jul 2026 10:00:00 GMT"})

    f = Fetcher(_settings(), transport=httpx.MockTransport(handler))
    try:
        first = await f.fetch("https://guba.eastmoney.com/news,600519,1.html")
        assert first.status == 200 and not first.not_modified
        assert seen_conditionals[0] is None          # no validator on first contact

        second = await f.fetch("https://guba.eastmoney.com/news,600519,1.html")
        assert seen_conditionals[1] == 'W/"v1"'      # validator replayed
        # 304 → the cached body comes back, flagged, with the cached status: the
        # classifier sees the same LIVE content it saw last time, never an empty 304.
        assert second.status == 200 and second.text == "帖子正文 body v1"
        assert second.not_modified is True
    finally:
        await f.aclose()


async def _changed_body_refreshes_cache_case():
    version = {"n": 1}

    def handler(req):
        # Always serve fresh content with a new validator (post was edited).
        n = version["n"]
        version["n"] += 1
        return httpx.Response(200, text=f"body v{n}", headers={"ETag": f'W/"v{n}"'})

    f = Fetcher(_settings(), transport=httpx.MockTransport(handler))
    try:
        await f.fetch("https://x.test/p/1")
        r2 = await f.fetch("https://x.test/p/1")
        assert r2.status == 200 and r2.text == "body v2" and not r2.not_modified
        r3 = await f.fetch("https://x.test/p/1")
        assert r3.text == "body v3"                  # cache tracked the newest validator
    finally:
        await f.aclose()


async def _no_validator_no_conditional_case():
    conditionals = []

    def handler(req):
        conditionals.append((req.headers.get("If-None-Match"),
                             req.headers.get("If-Modified-Since")))
        return httpx.Response(200, text="no validators here")   # origin sends no ETag/L-M

    f = Fetcher(_settings(), transport=httpx.MockTransport(handler))
    try:
        await f.fetch("https://y.test/p/1")
        await f.fetch("https://y.test/p/1")
        assert conditionals == [(None, None), (None, None)]     # nothing cached, nothing sent
    finally:
        await f.aclose()


# ── per-host politeness ceiling ─────────────────────────────────────────────────

async def _host_pacing_case():
    """With a 10s ceiling and a frozen clock, the 2nd same-host request must wait ~10s
    while a different host proceeds immediately."""
    delays = []
    orig_sleep = asyncio.sleep

    async def fake_sleep(d):
        delays.append(d)
        await orig_sleep(0)

    asyncio.sleep = fake_sleep  # type: ignore
    try:
        f = Fetcher(_settings(host_min_interval_s=10.0),
                    transport=httpx.MockTransport(lambda req: httpx.Response(200, text="ok")),
                    rng=random.Random(7), clock=lambda: 100.0)   # frozen clock
        try:
            await f.fetch("https://guba.eastmoney.com/list,600519.html")   # first contact: no wait
            await f.fetch("https://guba.eastmoney.com/list,300750.html")   # same host: waits interval
            await f.fetch("https://xueqiu.com/S/SH600519")                  # other host: no wait
        finally:
            await f.aclose()
    finally:
        asyncio.sleep = orig_sleep  # type: ignore
    waits = [d for d in delays if d > 0]
    assert waits == [10.0], waits


async def _pacing_off_by_default_case():
    """interval 0 (the default) must add no sleeps at all — prior behavior preserved."""
    delays = []
    orig_sleep = asyncio.sleep

    async def fake_sleep(d):
        delays.append(d)
        await orig_sleep(0)

    asyncio.sleep = fake_sleep  # type: ignore
    try:
        f = Fetcher(_settings(),
                    transport=httpx.MockTransport(lambda req: httpx.Response(200, text="ok")))
        try:
            await f.fetch("https://guba.eastmoney.com/a")
            await f.fetch("https://guba.eastmoney.com/b")
        finally:
            await f.aclose()
    finally:
        asyncio.sleep = orig_sleep  # type: ignore
    assert [d for d in delays if d > 0] == []


# pytest entry points
def test_revalidation_304_returns_cached_body(): asyncio.run(_revalidation_case())
def test_changed_body_refreshes_cache(): asyncio.run(_changed_body_refreshes_cache_case())
def test_no_validator_sends_no_conditionals(): asyncio.run(_no_validator_no_conditional_case())
def test_host_pacing_spaces_same_host_only(): asyncio.run(_host_pacing_case())
def test_pacing_off_by_default(): asyncio.run(_pacing_off_by_default_case())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  PASS {name}")
