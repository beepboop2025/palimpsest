"""Offline tests for the reviewed CensorWatch network-authority registry."""

from __future__ import annotations

import pytest

from censorwatch.source_policy import (
    enforce_source_url,
    source_url_is_allowed,
    source_url_policy,
)
from core.safe_fetch import FetchError


@pytest.mark.parametrize(
    ("source", "url", "purpose"),
    [
        ("eastmoney_guba", "https://guba.eastmoney.com/news,600519,1.html", "page"),
        ("eastmoney_guba", "https://caifuhao.eastmoney.com/news/1", "page"),
        ("eastmoney_guba", "https://np-newspic.dfcfw.com/a.png", "asset"),
        ("weibo_search", "https://s.weibo.com/weibo?q=test", "page"),
        ("weibo_search", "https://wx3.sinaimg.cn/large/a.jpg", "asset"),
        ("weibo_search", "https://js.t.sinajs.cn/a.js", "render"),
        ("xueqiu", "https://xueqiu.com/123/456", "page"),
        ("xueqiu", "https://xqimg.imedao.com/a.png", "asset"),
    ],
)
def test_reviewed_exact_hosts_are_allowed(source, url, purpose):
    enforce_source_url(source, url, purpose=purpose)
    assert source_url_is_allowed(source, url, purpose=purpose)


@pytest.mark.parametrize(
    "url",
    [
        "http://guba.eastmoney.com/a",
        "https://guba.eastmoney.com:444/a",
        "https://:@guba.eastmoney.com/a",
        "https://guba.eastmoney.com@169.254.169.254/a",
        "https://guba.eastmoney.com.evil.example/a",
        "https://sub.guba.eastmoney.com/a",
        "https://guba%2eeastmoney.com/a",
        "https://guba.eastmoney.com\\@127.0.0.1/a",
        "https://guba.eastmoney.com/a#fragment",
        "file:///etc/passwd",
    ],
)
def test_parser_confusion_and_authority_expansion_are_refused(url):
    with pytest.raises(FetchError):
        enforce_source_url("eastmoney_guba", url)
    assert not source_url_is_allowed("eastmoney_guba", url)


def test_page_asset_and_render_authorities_are_distinct():
    asset = "https://wx1.sinaimg.cn/large/a.jpg"
    script = "https://js.t.sinajs.cn/a.js"
    with pytest.raises(FetchError):
        enforce_source_url("weibo_search", asset, purpose="page")
    with pytest.raises(FetchError):
        enforce_source_url("weibo_search", script, purpose="asset")
    enforce_source_url("weibo_search", script, purpose="render")


def test_unknown_source_and_purpose_fail_closed():
    with pytest.raises(FetchError):
        enforce_source_url("unregistered", "https://example.com/")
    with pytest.raises(FetchError):
        enforce_source_url("xueqiu", "https://xueqiu.com/", purpose="other")


def test_callback_rechecks_each_url_supplied_by_the_transport():
    policy = source_url_policy("xueqiu")
    policy("https://xueqiu.com/start")
    with pytest.raises(FetchError):
        policy("https://127.0.0.1/redirect")
