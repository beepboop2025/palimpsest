"""A mandatory filing footer is not a censorship verdict.

    PYTHONPATH=. python3 -m pytest tests/test_cdn_edge_icp_footer.py -q

The regression: _BLOCK_MARKERS carried `ICP备` and `备案`, annotated as
"high-recall/low-precision — deployment-tunable". They were not low precision. ICP filing is
mandatory for mainland hosting, so the filing number sits in the footer of essentially every
page served from inside China. classify() therefore returned present=False for every genuine
Chinese site, and this collector could never report a mainland object as present at all.

That failure is worse than a dead collector, because a dead collector is visibly dead. Here
every observation came back with a plausible reason ("legal-block") attached, and downstream
a present=False is not inert: differencing one healthy POP against one of these fabricates a
GEO_FORK, and a present->absent flip fabricates a DELETION. Both flow into the DDTI index as
censorship findings. A marker that matches the entire population it is meant to discriminate
within produces confident noise, not weak signal.

The fix keeps the real interstitial — a host refusing to serve an UNFILED or DE-FILED domain
is a genuine content decision — and drops the bare filing number.
"""

from collectors.cdn_edge import classify

# A realistic mainland footer. Every one of these strings appears on ordinary, entirely
# unblocked Chinese sites; several appear on all of them.
ICP_FOOTER = (
    '<footer class="site-footer">'
    "<p>© 2026 示例科技有限公司 版权所有</p>"
    '<p><a href="https://beian.miit.gov.cn/" rel="noreferrer">京ICP备12345678号-1</a></p>'
    '<p>京公网安备 11010802012345号 · 备案号：京ICP备12345678号</p>'
    "</footer>"
)
BODY = "<html><body><article>" + ("这是一篇完全正常的文章内容。" * 40) + "</article>" + ICP_FOOTER + "</body></html>"


def test_an_ordinary_mainland_page_is_present():
    """The whole bug in one assertion: a normal Chinese site, ICP footer and all, is served."""
    present, reason, _ = classify(200, {}, BODY)
    assert present is True
    assert reason == "served"


def test_the_bare_filing_number_alone_never_blocks():
    for footer in (
        "京ICP备12345678号-1",
        "沪ICP备09876543号",
        "粤ICP备11111111号-5",
        "备案号：京ICP备12345678号",
        "京公网安备11010802012345号",
    ):
        body = ("正文内容。" * 80) + footer
        present, reason, _ = classify(200, {}, body)
        assert present is True, f"{footer!r} must not read as a block (got {reason})"


def test_a_refused_filing_is_still_a_block():
    """The signal that actually exists: a host declining to serve an unfiled domain.
    Kept, because that IS a content decision — just not one every site is making. It
    reports under its own reason rather than the generic legal-block, so a filing refusal
    and a censorship substitution stay distinguishable in the evidence."""
    for body in (
        "根据工业和信息化部相关规定，本网站未备案，暂时无法访问。",
        "该域名未备案",
        "抱歉，本站未进行备案",
        "该网站备案号已注销",
        "无备案信息，禁止访问",
    ):
        present, reason, _ = classify(200, {}, body + "填充" * 200)
        assert present is False, f"{body!r} should classify as blocked"
        assert reason == "icp-not-filed"


def test_the_genuine_legal_replacement_marker_is_untouched():
    """The marker that was always correct stays correct."""
    present, reason, _ = classify(200, {}, "根据相关法律法规，该内容无法显示")
    assert present is False and reason == "legal-block"


def test_a_filed_site_serving_a_deletion_stub_still_reads_as_deleted():
    """A real removal notice on a page that also carries the mandatory footer must still
    be caught — the footer must not have been made load-bearing in either direction."""
    body = "<html><body>内容已删除</body></html>" + ICP_FOOTER
    present, reason, _ = classify(200, {}, body)
    assert present is False and reason == "deleted"


def test_prose_about_filing_is_not_a_refusal_to_serve():
    """The trap one step past the original bug, and the first fix walked into it. `未备案`
    as a bare substring matches every hosting-provider help article and ICP FAQ that
    DISCUSSES filing. The markers are anchored to the host's own refusal wording instead."""
    article = "本文介绍未备案的网站如何完成ICP备案流程。" * 20
    present, reason, _ = classify(200, {}, article)
    assert present is True, f"an article about ICP filing is not a block (got {reason})"


def test_the_real_not_filed_pages_are_caught_under_their_own_reason():
    """Both live wordings. The WeChat page says 未完成备案, which does NOT contain 未备案 —
    it used to be caught only by accident, via the unrelated 无法访问 marker, so it reported
    the wrong reason."""
    wechat = "由于该网站未完成备案或未转入备案，造成你暂时无法访问。"
    aliyun = "该网站暂时无法进行访问" + "填充" * 100
    for body in (wechat, aliyun):
        present, reason, _ = classify(200, {}, body)
        assert present is False and reason == "icp-not-filed", f"{body[:20]}… -> {reason}"


# ── the same failure mode, three markers over ────────────────────────────────
# `notfound`, `error.html` and `access denied` were substring-scanned over the raw document,
# so they matched minified JS, meta attributes and ordinary English prose. Same bug as the
# ICP footer: a marker matching page construction rather than a decision the edge made.

def _long_clean_page(extra: str) -> str:
    return "<html><body><article>" + ("完全正常的文章内容。" * 60) + "</article>" + extra + "</body></html>"


def test_a_script_bundle_mentioning_notfound_is_not_a_block():
    body = _long_clean_page('<script>var NotFoundRoute={path:"*",component:NotFound};</script>')
    present, reason, _ = classify(200, {}, body)
    assert present is True, f"an SPA route table is not a censorship finding (got {reason})"


def test_an_attribute_pointing_at_an_error_page_is_not_a_block():
    body = _long_clean_page('<meta name="errorPage" content="/error.html">')
    present, reason, _ = classify(200, {}, body)
    assert present is True, f"a meta attribute is not a block (got {reason})"


def test_english_prose_containing_access_denied_abstains_rather_than_accusing():
    """`access denied` moved out of the block markers and into the WALL markers, so prose
    containing it no longer produces a censorship finding — it abstains.

    That is deliberate, and it is a trade with a real cost: an article discussing access
    control is excluded from the cross-POP comparison, so a genuine block at another POP
    would not fork against it. A missed detection. The alternative was what it did before —
    publishing that page as censored — and this project's asymmetry is settled: fabricating
    is fatal, missing is survivable. The list is shared with censorwatch and pinned in sync
    by tests/test_classifiers_share_one_corpus.py."""
    from collectors.cdn_edge import _ABSTAIN_REASONS

    body = _long_clean_page("<p>Access denied users will be redirected to the help centre.</p>")
    present, reason, _ = classify(200, {}, body)
    assert present is False and reason in _ABSTAIN_REASONS, (
        f"must abstain, not accuse (got present={present} reason={reason})")


def test_style_blocks_cannot_trigger_markers_either():
    body = _long_clean_page('<style>.notfound{display:none}</style>')
    assert classify(200, {}, body)[0] is True


def test_visible_text_extraction_suppresses_script_and_style():
    from collectors.cdn_edge import visible_text
    out = visible_text('<div>hello</div><script>var NotFound=1;</script><style>.a{}</style>')
    assert "hello" in out
    assert "NotFound" not in out
    assert ".a{}" not in out


def test_visible_text_fails_soft_rather_than_raising():
    """A parse failure must degrade the scan, never crash a probe cycle."""
    from collectors.cdn_edge import visible_text
    assert visible_text("") == ""
    assert visible_text("<<<not really html<<<") is not None


def test_a_real_interstitial_inside_markup_is_still_caught():
    """The guard must not have bought precision by making the scan blind to real pages —
    an interstitial is markup too, and its text is exactly what a reader sees."""
    body = '<html><body><div class="tip"><h1>根据相关法律法规，该内容无法显示</h1></div></body></html>'
    present, reason, _ = classify(200, {}, body)
    assert present is False and reason == "legal-block"


# ── the legal preamble: the last member of the over-broad family ─────────────
# `根据相关法律法规` alone is also standard PRC privacy-policy boilerplate, and unlike the ICP
# footer it cannot be separated by length — a privacy policy sails past the min-length floor.
# What separates them is the verb: an interstitial pairs the preamble with something WITHHELD,
# a privacy policy pairs it with an obligation being met. Hence a conjunction.

def test_a_privacy_policy_mentioning_the_law_is_not_a_block():
    body = ("<p>隐私政策：我们会根据相关法律法规的要求保留您的个人信息，"
            "并在法定期限内予以保存。</p>" + "<p>" + "条款内容说明。" * 80 + "</p>")
    present, reason, _ = classify(200, {}, body)
    assert present is True, f"a compliant site's privacy page is not censorship (got {reason})"


def test_the_preamble_alone_on_a_long_page_is_not_a_block():
    body = "<p>根据相关法律法规</p><p>" + "其他正常内容。" * 80 + "</p>"
    assert classify(200, {}, body)[0] is True


def test_both_real_interstitial_wordings_still_block():
    """Short form (preamble + withheld verb) and long form (the fixture's own wording).
    Tightening must not have bought precision by going blind to the real notices."""
    for body in (
        "根据相关法律法规，该内容无法显示",
        "根据相关法律法规和政策,此内容暂时无法显示。",
        "根据法律法规，本页内容未予显示",
    ):
        present, reason, _ = classify(200, {}, body)
        assert present is False, f"{body!r} must still block"
        assert reason == "legal-block", f"{body!r} -> {reason}"


def test_a_legal_notice_naming_a_deletion_reports_the_deletion():
    """First match wins, and the more specific reason is the better one: a notice that says
    the post was removed is `deleted`, not the generic `legal-block`. Both are present=False,
    so nothing downstream changes — only the evidence gets sharper."""
    present, reason, _ = classify(200, {}, "根据相关法律法规，该微博已被删除")
    assert present is False and reason == "deleted"
