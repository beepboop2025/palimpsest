"""Tests for the robust RSS + Atom feed parser in collectors.ddti_probe.

    PYTHONPATH=. python3 -m pytest tests/test_ddti_feed_parse.py -q

Two guarantees:
  * RSS/CDT output is preserved byte-for-byte (the live DDTI signal must not shift).
  * Atom feeds — previously silently dropped — now parse, including attribute-based <link href>
    and <category term>, closing a whole class of reachable sources (GreatFire/FreeWeibo/mirrors).
"""

import asyncio
from datetime import timezone

import pytest

pytest.importorskip("pandas", reason="collectors.ddti_probe needs the collector stack; "
                    "the sealed-signal suite stays stdlib-only")
from collectors.ddti_probe import (  # noqa: E402
    DDTIProbeCollector,
    PALIMPSEST_UA,
    parse_feed_items,
)
from core.exceptions import RateLimitError, SourceDownError  # noqa: E402

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>CDT</title>
  <item>
    <title>Minitrue: delete the term</title>
    <link>https://chinadigitaltimes.net/2026/07/example/</link>
    <description>A leaked directive about a sensitive term.</description>
    <pubDate>Mon, 06 Jul 2026 10:00:00 +0000</pubDate>
    <category>Censorship</category>
    <category>Directives</category>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>FreeWeibo-like</title>
  <entry>
    <title>Deleted post about an event</title>
    <link rel="self" href="https://example.org/self"/>
    <link rel="alternate" href="https://example.org/posts/42"/>
    <summary>A short summary.</summary>
    <content type="html">The fuller body of the deleted post.</content>
    <published>2026-07-06T10:00:00Z</published>
    <category term="rights"/>
    <category term="protest"/>
  </entry>
</feed>"""

# A WordPress-style feed carrying content:encoded — description must still win (RSS preservation).
RSS_WITH_ENCODED = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><item>
  <title>T</title>
  <link>https://x/1</link>
  <description>short desc</description>
  <content:encoded>much longer encoded body</content:encoded>
  <pubDate>Mon, 06 Jul 2026 10:00:00 +0000</pubDate>
</item></channel></rss>"""


def test_rss_output_is_preserved():
    items = parse_feed_items("cdt", RSS)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "Minitrue: delete the term"
    assert it["url"] == "https://chinadigitaltimes.net/2026/07/example/"
    assert it["text"] == "A leaked directive about a sensitive term."
    assert it["published_at"] == "Mon, 06 Jul 2026 10:00:00 +0000"
    assert it["tags"] == ["Censorship", "Directives"]


def test_atom_now_parses_with_attribute_link_and_terms():
    items = parse_feed_items("freeweibo", ATOM)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "Deleted post about an event"
    # alternate link chosen; the rel="self" link is skipped
    assert it["url"] == "https://example.org/posts/42"
    assert it["tags"] == ["rights", "protest"]         # <category term=...>
    assert it["text"] in ("A short summary.", "The fuller body of the deleted post.")
    assert it["published_at"] == "2026-07-06T10:00:00Z"


def test_rss_description_still_wins_over_encoded():
    it = parse_feed_items("cdt", RSS_WITH_ENCODED)[0]
    assert it["text"] == "short desc"   # unchanged live behavior; encoded is only a fallback


def test_non_xml_yields_empty_not_raise():
    assert parse_feed_items("x", "<html>not a feed</html>") == []
    assert parse_feed_items("x", "") == []


def test_atom_summary_only_entry():
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            '<title>t</title><link href="https://y/2"/><summary>only summary</summary>'
            '</entry></feed>')
    it = parse_feed_items("m", atom)[0]
    assert it["url"] == "https://y/2" and it["text"] == "only summary"


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _HTTP:
    def __init__(self, response):
        self.response = response
        self.headers = []

    async def get(self, _url, *, headers=None):
        self.headers.append(headers or {})
        return self.response

    async def aclose(self):
        pass


def _collector_with_http(response):
    collector = DDTIProbeCollector({
        "deletion_feeds": [{"name": "cdt", "url": "https://example.test/feed"}],
    })
    asyncio.run(collector._http.aclose())
    collector._http = _HTTP(response)
    return collector


def test_all_unreachable_feeds_are_a_failure_not_a_quiet_success():
    collector = _collector_with_http(_Response(503))

    with pytest.raises(SourceDownError):
        asyncio.run(collector.collect())

    assert collector.reachability == {"cdt": 503}


def test_rate_limit_is_reported_distinctly_for_retry_policy():
    collector = _collector_with_http(_Response(429))

    with pytest.raises(RateLimitError):
        asyncio.run(collector.collect())


def test_live_feed_read_identifies_palimpsest_instead_of_impersonating_a_browser():
    collector = _collector_with_http(_Response(200, RSS))

    rows = asyncio.run(collector.collect())

    assert len(rows) == 1
    assert collector._http.headers == [{"User-Agent": PALIMPSEST_UA}]
    assert "palimpsest.info" in PALIMPSEST_UA


def test_parser_preserves_the_source_publication_time_and_topic_metadata():
    collector = _collector_with_http(_Response(200, RSS))
    raw = asyncio.run(collector.collect())

    frame = asyncio.run(collector.parse(raw))
    row = frame.iloc[0]

    assert row["published_at"].tzinfo == timezone.utc
    assert row["published_at"].isoformat() == "2026-07-06T10:00:00+00:00"
    assert row["metadata"]["tags"] == ["Censorship", "Directives"]


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
