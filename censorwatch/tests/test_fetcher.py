"""Offline contract tests for CensorWatch's hardened async acquisition facade."""

from __future__ import annotations

import asyncio
import random

from censorwatch.config import CensorwatchSettings
from censorwatch.fetcher import Fetcher
from core.safe_fetch import FetchError, SafeFetchResponse


def _settings(**over) -> CensorwatchSettings:
    base = {
        "enabled": True,
        "proxy_url": None,
        "min_delay_s": 0.0,
        "max_delay_s": 0.0,
        "request_timeout_s": 5.0,
        "confirmations": 3,
        "archive_dir": "/tmp/cw",
        "velocity_window_min": 60,
        "velocity_baseline_windows": 24,
        "spike_z_threshold": 3.0,
    }
    base.update(over)
    return CensorwatchSettings(**base)


def _response(url, *, status=200, body=b"healthy", headers=None):
    return SafeFetchResponse(status, headers or {}, body, url)


async def _success_case():
    calls = []

    def acquire(url, **kwargs):
        calls.append((url, kwargs))
        assert "User-Agent" in kwargs["headers"]
        kwargs["url_policy"](url)
        return _response(url, body="正常内容,足够长。".encode())

    async with Fetcher(
        _settings(), source="eastmoney_guba", response_fetcher=acquire
    ) as fetcher:
        result = await fetcher.fetch(
            "https://guba.eastmoney.com/news,600519,1.html"
        )
    assert result.status == 200 and result.text and result.transport_ok
    assert result.final_url and "600519" in result.final_url
    assert calls[0][1]["max_bytes"] == _settings().max_page_bytes


async def _failure_case():
    def acquire(_url, **_kwargs):
        raise FetchError("simulated transport failure with secret path")

    async with Fetcher(
        _settings(), source="eastmoney_guba", response_fetcher=acquire
    ) as fetcher:
        result = await fetcher.fetch("https://guba.eastmoney.com/x")
    assert result.status is None and not result.transport_ok
    assert result.error == "fetch_refused:FetchError"
    assert "secret" not in result.error


async def _jitter_bounds_case():
    delays = []
    original_sleep = asyncio.sleep

    async def fake_sleep(delay):
        delays.append(delay)
        await original_sleep(0)

    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        fetcher = Fetcher(
            _settings(min_delay_s=2.0, max_delay_s=6.0),
            source="eastmoney_guba",
            response_fetcher=lambda url, **_kwargs: _response(url),
            rng=random.Random(42),
        )
        await fetcher.fetch("https://guba.eastmoney.com/a")
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]
    assert len(delays) == 1 and 2.0 <= delays[0] <= 6.0


def test_user_agent_rotation():
    fetcher = Fetcher(_settings(), source="eastmoney_guba", rng=random.Random(1))
    agents = {fetcher._headers()["User-Agent"] for _ in range(20)}
    assert len(agents) >= 2


def test_success():
    asyncio.run(_success_case())


def test_failure_is_sanitized_abstention():
    asyncio.run(_failure_case())


def test_jitter_bounds():
    asyncio.run(_jitter_bounds_case())


def test_proxy_and_security_budgets_reach_hardened_transport():
    calls = []

    def acquire(url, **kwargs):
        calls.append(kwargs)
        return _response(url)

    fetcher = Fetcher(
        _settings(proxy_url="http://trusted-proxy.example:8080", max_redirects=3),
        source="eastmoney_guba",
        response_fetcher=acquire,
    )
    asyncio.run(fetcher.fetch("https://guba.eastmoney.com/a"))
    assert calls[0]["proxy"] == "http://trusted-proxy.example:8080"
    assert calls[0]["max_redirects"] == 3
    assert calls[0]["timeout"] == 5.0


def test_unreviewed_initial_url_is_refused_before_transport():
    calls = []
    fetcher = Fetcher(
        _settings(),
        source="eastmoney_guba",
        response_fetcher=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    result = asyncio.run(fetcher.fetch("http://169.254.169.254/latest/meta-data/"))
    assert result.status is None and result.error == "fetch_refused:FetchError"
    assert calls == []


def test_caller_policy_is_reapplied_to_redirects():
    def acquire(_url, **kwargs):
        kwargs["url_policy"]("https://guba.eastmoney.com/start")
        kwargs["url_policy"]("http://127.0.0.1/admin")
        raise AssertionError("private redirect should have been refused")

    fetcher = Fetcher(
        _settings(), source="eastmoney_guba", response_fetcher=acquire
    )
    result = asyncio.run(fetcher.fetch("https://guba.eastmoney.com/start"))
    assert result.status is None and result.error == "fetch_refused:FetchError"


def test_asset_authority_does_not_become_page_authority():
    calls = []

    def acquire(url, **kwargs):
        calls.append((url, kwargs["max_bytes"]))
        kwargs["url_policy"](url)
        return _response(url, body=b"image")

    fetcher = Fetcher(
        _settings(max_image_bytes=1024),
        source="eastmoney_guba",
        response_fetcher=acquire,
    )
    asset_url = "https://np-newspic.dfcfw.com/a.png"
    page = asyncio.run(fetcher.fetch(asset_url))
    status, body, error = asyncio.run(fetcher.fetch_bytes(asset_url, max_bytes=512))
    assert page.status is None
    assert (status, body, error) == (200, b"image", None)
    assert calls == [(asset_url, 512)]


def test_image_cycle_budget_is_hard_even_for_an_injected_transport():
    def acquire(url, **_kwargs):
        return _response(url, body=b"1234")

    fetcher = Fetcher(
        _settings(max_image_bytes=8, max_cycle_image_bytes=6),
        source="eastmoney_guba",
        response_fetcher=acquire,
    )
    first = asyncio.run(fetcher.fetch_bytes("https://guba.eastmoney.com/a.png"))
    second = asyncio.run(fetcher.fetch_bytes("https://guba.eastmoney.com/b.png"))
    assert first == (200, b"1234", None)
    assert second[0:2] == (None, None)
    assert second[2] == "fetch_refused:ResponseTooLarge"
    assert fetcher._cycle_image_bytes == 4


def test_unknown_source_and_local_browser_fail_closed():
    try:
        Fetcher(_settings(), source="unknown")
    except FetchError:
        pass
    else:
        raise AssertionError("an unregistered source must not get a fetcher")

    fetcher = Fetcher(_settings(), source="weibo_search")
    result = asyncio.run(
        fetcher.fetch("https://s.weibo.com/weibo?q=test", render=True)
    )
    assert result.status is None and result.error == "render_gateway_unconfigured"


def test_declared_chinese_charset_is_decoded_from_bounded_bytes():
    text = "雪球正文"
    fetcher = Fetcher(
        _settings(),
        source="xueqiu",
        response_fetcher=lambda url, **_kwargs: _response(
            url,
            body=text.encode("gb18030"),
            headers={"content-type": "text/html; charset=gb18030"},
        ),
    )
    result = asyncio.run(fetcher.fetch("https://xueqiu.com/a"))
    assert result.text == text
