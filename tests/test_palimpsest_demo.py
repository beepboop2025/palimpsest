"""The zero-pip demo shares Palimpsest's hardened fixed-feed transport."""

from __future__ import annotations

import pytest

from core.safe_fetch import FetchError
from demo import palimpsest_demo as demo


RSS = (
    b'<?xml version="1.0"?><rss><channel><item><title>bounded</title>'
    b"</item></channel></rss>"
)


def test_demo_uses_the_exact_bounded_transport(monkeypatch):
    seen = {}

    def fetch(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return RSS

    monkeypatch.setattr(demo, "safe_fetch_bytes", fetch)
    items = demo.fetch_feed(
        demo.CDT_FEEDS[0], timeout=9, proxy="http://127.0.0.1:8080"
    )

    assert len(items) == 1
    assert seen["url"] == demo.CDT_FEEDS[0]
    assert seen["max_bytes"] == demo.MAX_FEED_BYTES
    assert seen["timeout"] == 9
    assert seen["max_redirects"] == 2
    assert seen["proxy"] == "http://127.0.0.1:8080"
    assert seen["headers"]["User-Agent"] == demo.USER_AGENT
    seen["url_policy"](demo.CDT_FEEDS[0])
    with pytest.raises(FetchError):
        seen["url_policy"]("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(FetchError):
        seen["url_policy"]("https://chinadigitaltimes.net/private/feed/")


def test_demo_transport_failure_is_fail_soft_without_error_detail(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise FetchError("secret at http://127.0.0.1/private")

    monkeypatch.setattr(demo, "safe_fetch_bytes", fail)
    assert demo.fetch_feed(demo.CDT_FEEDS[0]) == []
    output = capsys.readouterr().out
    assert "FetchError" in output
    assert "secret" not in output
    assert "127.0.0.1" not in output


def test_demo_rejects_doctype_before_xml_parse(monkeypatch):
    malicious = b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel/></rss>'
    monkeypatch.setattr(demo, "safe_fetch_bytes", lambda *_a, **_k: malicious)
    assert demo.fetch_feed(demo.CDT_FEEDS[0]) == []


def test_demo_user_agent_is_identifying_not_browser_impersonation():
    assert "Palimpsest" in demo.USER_AGENT
    assert "palimpsest.info" in demo.USER_AGENT
    assert "Mozilla" not in demo.USER_AGENT
