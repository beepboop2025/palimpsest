"""Tests for the proxy-aware fetcher — runs the full path via httpx.MockTransport,
no network. Verifies: success mapping, transport-failure → status=None+error (so
the classifier sees UNKNOWN, never a false deletion), jitter bounds, UA rotation.

    python3 -m pytest censorwatch/tests/test_fetcher.py
    python3 censorwatch/tests/test_fetcher.py
"""

from __future__ import annotations

import asyncio

import httpx

from censorwatch.config import CensorwatchSettings
from censorwatch.fetcher import Fetcher
import random


def _settings(**over) -> CensorwatchSettings:
    base = dict(
        enabled=True, proxy_url=None,
        min_delay_s=0.0, max_delay_s=0.0, request_timeout_s=5.0,
        collect_concurrency=4, recheck_concurrency=12,
        confirmations=3, archive_dir="/tmp/cw",
        velocity_window_min=60, velocity_baseline_windows=24, spike_z_threshold=3.0,
        cloud_sync_enabled=False, cloud_bucket=None, cloud_region="auto",
        cloud_endpoint_url=None, cloud_prefix="palimpsest/censorwatch",
        cloud_lookback_hours=24, cloud_include_archive=False,
        consolidate_lookback_hours=24, consolidate_max_rows=50000,
        promotion_gate_enabled=True, fusion_lookback_hours=48, fusion_alert_z=2.0,
    )
    base.update(over)
    return CensorwatchSettings(**base)


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def _success_case():
    def handler(req):
        assert "User-Agent" in req.headers
        return httpx.Response(200, text="<html>正常内容,足够长以越过空白阈值的判断逻辑。</html>")
    f = Fetcher(_settings(), transport=_mock(handler))
    try:
        r = await f.fetch("https://guba.eastmoney.com/news,600519,1.html")
        assert r.status == 200 and r.text and r.transport_ok
        assert r.final_url and "600519" in r.final_url
    finally:
        await f.aclose()


async def _timeout_case():
    def handler(req):
        raise httpx.ConnectTimeout("simulated timeout")
    f = Fetcher(_settings(), transport=_mock(handler))
    try:
        r = await f.fetch("https://guba.eastmoney.com/x")
        # Transport failure → no exception, status None, error set → UNKNOWN downstream.
        assert r.status is None and r.error and not r.transport_ok
        assert r.error.startswith("timeout")
    finally:
        await f.aclose()


async def _jitter_bounds_case():
    # Capture the sleep delay without actually sleeping; assert it's within bounds.
    delays = []
    orig_sleep = asyncio.sleep

    async def fake_sleep(d):
        delays.append(d)
        await orig_sleep(0)

    asyncio.sleep = fake_sleep  # type: ignore
    try:
        f = Fetcher(_settings(min_delay_s=2.0, max_delay_s=6.0),
                    transport=_mock(lambda req: httpx.Response(200, text="x" * 100)),
                    rng=random.Random(42))
        try:
            await f.fetch("https://example.com")
        finally:
            await f.aclose()
    finally:
        asyncio.sleep = orig_sleep  # type: ignore
    assert len(delays) == 1 and 2.0 <= delays[0] <= 6.0, delays


def _ua_rotation_case():
    # Deterministic with a seeded RNG; rotates across the configured pool.
    f = Fetcher(_settings(), rng=random.Random(1))
    uas = {f._headers()["User-Agent"] for _ in range(20)}
    assert len(uas) >= 2, "expected UA rotation across the pool"


def _proxy_wiring_case():
    # When proxy set, the client is constructed without error and is usable.
    f = Fetcher(_settings(proxy_url="socks5://127.0.0.1:9050"))
    assert f._client is not None


def _run_all():
    _ua_rotation_case(); print("  PASS ua_rotation")
    _proxy_wiring_case(); print("  PASS proxy_wiring")
    asyncio.run(_success_case()); print("  PASS success")
    asyncio.run(_timeout_case()); print("  PASS timeout")
    asyncio.run(_jitter_bounds_case()); print("  PASS jitter_bounds")
    print("\n5/5 fetcher checks passed")


# pytest entry points
def test_success(): asyncio.run(_success_case())
def test_timeout(): asyncio.run(_timeout_case())
def test_jitter_bounds(): asyncio.run(_jitter_bounds_case())
def test_ua_rotation(): _ua_rotation_case()
def test_proxy_wiring(): _proxy_wiring_case()


if __name__ == "__main__":
    _run_all()
