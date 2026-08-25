"""Pure policy tests for the disposable browser gateway."""

from __future__ import annotations

from censorwatch.render_gateway import _request_is_allowed, _resolver_rules


def test_browser_router_distinguishes_documents_and_subresources():
    pins = {
        "s.weibo.com": "203.0.113.1",
        "js.t.sinajs.cn": "203.0.113.2",
    }
    assert _request_is_allowed(
        "weibo_search",
        "https://s.weibo.com/weibo?q=test",
        resource_type="document",
        pins=pins,
    )
    assert _request_is_allowed(
        "weibo_search",
        "https://js.t.sinajs.cn/app.js",
        resource_type="script",
        pins=pins,
    )
    assert not _request_is_allowed(
        "weibo_search",
        "https://js.t.sinajs.cn/fake-page",
        resource_type="document",
        pins=pins,
    )


def test_browser_router_requires_a_publicly_validated_pin():
    pins = {"s.weibo.com": "203.0.113.1"}
    for hostile in (
        "http://127.0.0.1/admin",
        "https://169.254.169.254/latest/meta-data/",
        "https://s.weibo.com.evil.invalid/pixel",
        "file:///etc/passwd",
    ):
        assert not _request_is_allowed(
            "weibo_search", hostile, resource_type="script", pins=pins
        )
    assert not _request_is_allowed(
        "weibo_search",
        "https://js.t.sinajs.cn/app.js",
        resource_type="script",
        pins=pins,
    )


def test_chromium_resolver_rules_are_deterministic_and_pinned():
    assert _resolver_rules(
        {"xueqiu.com": "93.184.216.34", "stock.xueqiu.com": "2001:4860:4860::8888"}
    ) == (
        "MAP stock.xueqiu.com [2001:4860:4860::8888], "
        "MAP xueqiu.com 93.184.216.34, MAP * ~NOTFOUND"
    )
