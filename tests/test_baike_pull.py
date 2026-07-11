"""Offline proof that the narrative-erasure pipeline detects a fork when Baike IS
reachable — validating the scoring path without any live network (Baike blocks
datacenter IPs, so live can't run in CI). Fetches are injected with realistic
fixtures: the open record carries sensitive terms and independent sourcing; the
state encyclopedia has neither.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.baike_redaction import BaikeRedactionWatch, Entity, ENCYCLOPEDIA_FORK  # noqa: E402

# A Baike entry scrubbed clean: long-enough neutral body, sourcing collapsed to state media,
# none of the sensitive vocabulary the open record carries.
BAIKE_SCRUBBED = (
    '<html><body>'
    '<div class="lemma-summary">该词条介绍相关历史与社会背景，内容依据官方公开资料整理编写。</div>'
    '<div class="para">本条目对相关事件的背景、经过与影响作了概述性介绍，'
    '所引用资料来自官方媒体的公开报道，力求客观中立地呈现基本情况与后续发展脉络。</div>'
    '<a href="https://people.com.cn/n1/2020/report.html">来源</a>'
    '<a href="https://xinhuanet.com/politics/x.htm">来源</a>'
    '</body></html>'
)

# The open record: names the event plainly (sensitive terms present) with independent sourcing.
WIKI_OPEN = json.dumps({"query": {"pages": {"1": {
    "title": "六四事件",
    "extract": ("六四事件，又称天安门事件，是1989年发生的重大历史事件。"
                "官方对示威进行了镇压，海内外广泛关注。相关维权人士其后长期受到软禁。"),
    "extlinks": [{"*": "https://www.nytimes.com/1989/story.html"},
                 {"*": "https://www.bbc.com/zhongwen/x"}],
}}}})


def _watch():
    return BaikeRedactionWatch(
        baike_fetch=lambda url: BAIKE_SCRUBBED,
        wiki_fetch=lambda url: WIKI_OPEN,
    )


def test_fork_fires_on_scrubbed_entry():
    r = _watch().observe(Entity(zh_title="六四事件", domain="UNREST"))
    assert r["baike"].get("present"), "fixture baike should parse as present"
    assert r["wiki"].get("present"), "fixture wiki should parse as present"
    forks = [d for d in r["divergences"] if getattr(d, "kind", None) == ENCYCLOPEDIA_FORK]
    assert forks, f"expected an ENCYCLOPEDIA_FORK; got divergences={[getattr(d,'kind',None) for d in r['divergences']]}"
    detail = str(getattr(forks[0], "detail", ""))
    # the fork should self-evidence via at least one concrete facet
    assert any(k in detail for k in ("wiki_only_sensitive", "sourcing_monoculture", "absent_on_baike")), detail


def test_index_math_is_share_of_comparable():
    # rewrite_index = 100 * forks / comparable; a single comparable+forked entity => 100.0
    r = _watch().observe(Entity(zh_title="六四事件", domain="UNREST"))
    comparable = 1 if (r["wiki"].get("present")
                       and r["baike"].get("interstitial", "") not in ("fetch_failed", "disambiguation")) else 0
    forks = 1 if any(getattr(d, "kind", None) == ENCYCLOPEDIA_FORK for d in r["divergences"]) else 0
    assert comparable == 1 and forks == 1
    assert round(100.0 * forks / comparable, 1) == 100.0


def test_fetch_failure_is_not_comparable():
    # A Baike timeout must abstain that entity, never count as "no fork" (a false clean bill).
    w = BaikeRedactionWatch(
        baike_fetch=lambda url: (_ for _ in ()).throw(OSError("timeout")),
        wiki_fetch=lambda url: WIKI_OPEN,
    )
    r = w.observe(Entity(zh_title="六四事件"))
    assert r["baike"].get("interstitial") == "fetch_failed"
    comparable = r["wiki"].get("present") and r["baike"].get("interstitial") not in ("fetch_failed", "disambiguation")
    assert not comparable


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"=== baike_pull: {passed}/{len(fns)} passed ===")
    sys.exit(0 if passed == len(fns) else 1)
