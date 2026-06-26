"""Tests for the Weibo search parser (pure HTML → Post transform).

NOTE: Weibo public search is login-walled / 403'd from open egress, so this
validates the documented s.weibo.com card SHAPE via a synthetic fixture, not live
data. Live fetch needs Playwright + an in-China residential proxy.

    python3 -m pytest censorwatch/tests/test_weibo_search.py
    python3 censorwatch/tests/test_weibo_search.py
"""

from __future__ import annotations

from datetime import timezone

from censorwatch.collectors.weibo_search import WeiboSearchCollector

# Synthetic s.weibo.com search results matching the real card DOM.
_HTML = """
<div id="pl_feedlist_index">
  <div class="card-wrap" mid="5012345678901234">
    <div class="card"><div class="content">
      <a class="name" href="//weibo.com/u/123" nick-name="财经观察">财经观察</a>
      <p class="txt" node-type="feed_list_content">关于<a>#经济政策#</a>的一条微博正文。</p>
      <p class="from"><a href="//weibo.com/123/Abc12xYz?refer_flag=1001" target="_blank">2026-06-20 10:05</a></p>
    </div></div>
  </div>
  <div class="card-wrap" mid="5012345678905678">
    <div class="card"><div class="content">
      <a class="name" href="//weibo.com/u/456">市场观察员</a>
      <p class="txt" node-type="feed_list_content">短线波动加大,注意风险。</p>
      <p class="from"><a href="//weibo.com/456/Bcd34?from=x">6月19日 22:30</a></p>
    </div></div>
  </div>
  <div class="card-wrap"><div class="card-top">no mid — ad/header card, skipped</div></div>
</div>
"""


def test_parse_search_html():
    rows = WeiboSearchCollector._parse_search_html(_HTML)
    assert len(rows) == 2, "card without mid must be skipped"
    r0 = rows[0]
    assert r0["post_id"] == "5012345678901234" and r0["author"] == "财经观察"
    assert "经济政策" in r0["full_text"] and "<a>" not in r0["full_text"]
    assert r0["url"] == "https://weibo.com/123/Abc12xYz"   # // → https, query stripped
    assert r0["posted_at"].tzinfo == timezone.utc          # 2026-06-20 10:05 BJ → UTC
    assert r0["posted_at"].hour == 2                        # 10:05 BJ − 8h
    assert len(r0["content_hash"]) == 64


def test_time_parsing_forms():
    P = WeiboSearchCollector._parse_time
    assert P("2026-06-20 10:05").hour == 2                  # absolute
    assert P("6月19日 22:30") is not None                    # M月D日 form
    assert P("10分钟前") is None                              # relative → None (fallback)
    assert P("") is None


def test_abs_url():
    A = WeiboSearchCollector._abs_url
    assert A("//weibo.com/1/Ab?x=1") == "https://weibo.com/1/Ab"
    assert A("/1/Ab") == "https://weibo.com/1/Ab"
    assert A(None) is None


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  PASS {name}")
    print("\nweibo_search checks passed")


if __name__ == "__main__":
    _run_all()
