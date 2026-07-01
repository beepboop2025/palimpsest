"""Tests for collectors.baike_redaction — Baidu Baike vs Wikipedia redaction-diff.

    PYTHONPATH=. python3 -m pytest tests/test_baike_redaction.py -q

Fully OFFLINE. Every Baike HTML / Wikipedia JSON round is an inline fixture (the
``round-1 / round-2`` snapshots the brief calls for, kept in-file so the deliverable is
exactly the module + this test and is trivially replayable). No test ever touches real
Chinese infrastructure: the two fetchers are injected fakes. Governance gating, fail-soft
fetch handling, the lexical classifiers, and the DDTI hand-off are all exercised without a
network.
"""

import urllib.error

import pytest

from collectors.baike_redaction import (
    BaikeRedactionWatch, Entity,
    ENCYCLOPEDIA_FORK, STATE_REWRITE, MUTATION, DELETION,
    NORMAL_EDIT, STATE_REWRITE_SUSPECTED,
    extract_baike, extract_wiki, fingerprint, state_rewrite_signal,
    is_state_media, redaction_to_ddti,
)
from collectors.undertext import divergence_to_observation, JsonBaselineStore
from core.governance import KillSwitch, RateCeiling


# ── inline fixtures (round-1 / round-2 Baike HTML, Wikipedia extracts JSON) ──────────────────

def _baike(summary="", paras=(), infobox=(), refs=(), chrome=""):
    """Assemble a minimal-but-realistic Baike entry page from facets."""
    parts = ["<html><body>", chrome]
    if summary:
        parts.append(f'<div class="lemma-summary">{summary}</div>')
    for k, v in infobox:
        parts.append(f'<dt class="basicInfo-name">{k}</dt><dd class="basicInfo-value">{v}</dd>')
    for p in paras:
        parts.append(f'<div class="para">{p}</div>')
    for u in refs:
        parts.append(f'<a href="{u}">来源</a>')
    parts.append("</body></html>")
    return "".join(parts)


def _wiki(title, extract, externallinks=()):
    import json
    data = {"query": {"pages": {"42": {"title": title, "extract": extract}}}}
    if externallinks:
        data["parse"] = {"externallinks": list(externallinks)}
    return json.dumps(data, ensure_ascii=False)


WIKI_MISSING = '{"query":{"pages":{"-1":{"title":"X","missing":""}}}}'


def _seq(*responses):
    """A fetcher that returns the given responses in order (one per call)."""
    it = iter(responses)
    return lambda url: next(it)


# Round-1 / round-2 snapshots for the canonical managed-bio rewrite (秦刚 / Qin Gang).
QIN_R1 = _baike(
    summary="秦刚，中华人民共和国外交部部长。",
    infobox=[("职务", "外交部长")],
    paras=["2022年12月，秦刚出任外交部长。", "2023年6月，秦刚会见外国政要，开展外交活动。"],
    refs=["https://www.reuters.com/world/qin", "https://www.people.com.cn/qin"],
    chrome='<div class="side">阅读次数 123456 最近更新 2023-07-01</div>',
)
QIN_R2 = _baike(
    summary="秦刚，中国政治人物。",
    paras=["2022年12月，秦刚曾任相关职务。",
           "秦刚长期从事外交相关工作，为国家建设作出了贡献，受到广泛关注。"],
    refs=["https://www.xinhuanet.com/qin", "https://www.people.com.cn/qin"],
    chrome='<div class="side">阅读次数 654321 最近更新 2023-07-26</div>',
)

# 李克强 / Li Keqiang — euphemism substitution + section-deletion-with-padding.
LI_R1 = _baike(
    summary="李克强，曾任国务院总理。",
    paras=["2023年10月27日，李克强在上海因突发心脏病逝世。",
           "各地民众自发前往悼念，献花寄托哀思。"],
    refs=["https://www.bbc.com/li", "https://www.people.com.cn/li"],
)
LI_R2 = _baike(
    summary="李克强，曾任国务院总理。",
    paras=["2023年10月，李克强因身体原因逝世。",
           "李克强长期担任领导职务，为改革开放和国家发展作出重要贡献，事迹广为人知。"],
    refs=["https://www.xinhuanet.com/li", "https://www.people.com.cn/li"],
)

# Normal growth — latest date advances, sourcing stable, no excision (must NOT fire).
NORMAL_R1 = _baike(
    summary="某科技公司，成立于2015年。",
    paras=["2015年，公司成立。", "2021年，公司完成B轮融资。"],
    refs=["https://www.techweb.com.cn/a", "https://www.people.com.cn/b"],
)
NORMAL_R2 = _baike(
    summary="某科技公司，成立于2015年。",
    paras=["2015年，公司成立。", "2021年，公司完成B轮融资。", "2023年，公司发布新产品。"],
    refs=["https://www.techweb.com.cn/a", "https://www.people.com.cn/b"],
)

# 江泽民 toad/蛤 meme — euphemism present in BOTH rounds (negative control, must NOT over-fire).
JIANG_R1 = _baike(
    summary="江泽民，中国前领导人。",
    paras=["2022年，江泽民逝世。", "网民以蛤蟆作为对其的戏称，膜蛤文化在网络流行。"],
    refs=["https://www.people.com.cn/jiang"],
)
JIANG_R2 = _baike(
    summary="江泽民，中国前领导人。",
    paras=["2022年，江泽民逝世，举行了追悼活动。", "网民以蛤蟆作为对其的戏称，膜蛤文化在网络流行。"],
    refs=["https://www.people.com.cn/jiang"],
)

# Chrome-only change — paragraphs identical, only view-count/timestamp/rail differ.
CHROME_R1 = _baike(summary="某词条。", paras=["内容一。", "内容二。"],
                   chrome='<div>阅读次数 100000 最近更新 2023-01-01 猜你关注 相关词条</div>')
CHROME_R2 = _baike(summary="某词条。", paras=["内容一。", "内容二。"],
                   chrome='<div>阅读次数 999999 最近更新 2024-06-30 猜你关注 别的推荐</div>')


# ── extraction ───────────────────────────────────────────────────────────────────────────

def test_extract_baike_present_paragraphs_infobox_refs():
    ex = extract_baike(QIN_R1)
    assert ex["present"] is True and ex["interstitial"] == ""
    assert ex["paragraphs"] and "部长" in ex["summary"]
    assert ex["infobox"].get("职务") == "外交部长"
    assert "reuters.com" in ex["ref_domains"] and "people.com.cn" in ex["ref_domains"]
    assert ex["latest_year"] == 2023
    # baidu-internal hosts and the view-count chrome are excluded from the citation domains
    assert all(not d.endswith("baidu.com") for d in ex["ref_domains"])


def test_extract_baike_distinguishes_five_absence_states():
    assert extract_baike("百科尚未收录词条")["interstitial"] == "not_created"
    assert extract_baike("该词条已被删除")["interstitial"] == "deleted"
    assert extract_baike("该词条已被锁定")["interstitial"] == "locked"
    assert extract_baike('<div class="lemmaWgt-subLemmaList">多义词</div>')["interstitial"] == "disambiguation"
    for sig in ("not_created", "deleted", "locked", "disambiguation"):
        ex = extract_baike({"not_created": "百科尚未收录词条", "deleted": "该词条已被删除",
                            "locked": "该词条已被锁定", "disambiguation": "多义词"}[sig])
        assert ex["present"] is False  # none of these is a fingerprintable entry


def test_extract_baike_dom_churn_falls_back_to_whole_body():
    """When the known selectors miss, present is decided by the normalize_body fallback."""
    html = "<html><body>" + ("这是一段没有已知类名的正文内容。" * 10) + "</body></html>"
    ex = extract_baike(html)
    assert ex["present"] is True and ex["paragraphs"] == []  # fell back, did not crash


def test_extract_wiki_present_and_missing():
    ex = extract_wiki(_wiki("六四事件", "1989年六四天安门事件，军队镇压示威者。"))
    assert ex["present"] is True and "天安门" in ex["plaintext"] and ex["latest_year"] == 1989
    assert extract_wiki(WIKI_MISSING)["present"] is False           # missing page
    assert extract_wiki("not json")["interstitial"] == "parse_error"  # never raises


def test_is_state_media_allowlist():
    assert is_state_media("xinhuanet.com") and is_state_media("www.people.com.cn".replace("www.", ""))
    assert is_state_media("sub.ccdi.gov.cn") and not is_state_media("reuters.com")


# ── fingerprint: paragraph-set ignores chrome (no fake MUTATION) ────────────────────────────

def test_fingerprint_paragraph_set_ignores_chrome():
    fp1 = fingerprint(extract_baike(CHROME_R1))
    fp2 = fingerprint(extract_baike(CHROME_R2))
    assert fp1 == fp2  # only the view count / timestamp / rail changed → same content fp


# ── detection (i): the FORK — derived-facet delta, never content_fp equality ────────────────

def test_fork_absent_on_baike_total_scrub():
    """四通桥事件: no Baike entry, full Wikipedia entry → the hardest fork."""
    e = Entity("四通桥事件", lemma_id="0", domain="UNREST")
    w = BaikeRedactionWatch(
        baike_fetch=_seq("抱歉，百科尚未收录词条。"),
        wiki_fetch=_seq(_wiki("四通桥事件", "2022年10月，北京四通桥出现抗议横幅，彭立发被拘留。")))
    divs = w.observe(e, observed_at=1000.0)["divergences"]
    fork = [d for d in divs if d.kind == ENCYCLOPEDIA_FORK]
    assert len(fork) == 1 and "absent_on_baike" in fork[0].detail


def test_fork_wiki_only_sensitive_large_delta():
    """六四事件: Baike present but sanitized; Wikipedia carries the sensitive terms."""
    e = Entity("六四事件", lemma_id="1", domain="UNREST")
    baike = _baike(summary="1989年北京发生政治风波。", paras=["1989年春夏之交，北京发生了一场政治风波。"])
    wiki = _wiki("六四事件", "1989年六四天安门事件中，军队对示威者进行了镇压，屠杀引发国际关注。")
    divs = BaikeRedactionWatch(baike_fetch=_seq(baike), wiki_fetch=_seq(wiki)).observe(
        e, observed_at=1000.0)["divergences"]
    fork = [d for d in divs if d.kind == ENCYCLOPEDIA_FORK][0]
    assert "wiki_only_sensitive" in fork.detail
    terms = redaction_to_ddti(fork)["terms"]
    assert "六四" in terms and "镇压" in terms and "六四事件" in terms  # title + delta → gazetteer


def test_fork_sourcing_monoculture_isolated():
    """胡鑫宇: Baike present, state-media-only refs; Wikipedia independently sourced. No
    sensitive-term delta — the monoculture path fires on its own."""
    e = Entity("胡鑫宇事件", lemma_id="2", domain="RIGHTS")
    baike = _baike(summary="胡鑫宇事件，官方已通报。", paras=["2023年，官方通报调查结论。"],
                   refs=["https://www.xinhuanet.com/h", "https://www.cctv.com/h",
                         "https://www.people.com.cn/h"])
    wiki = _wiki("胡鑫宇事件", "2023年，该事件引发广泛讨论。",
                 externallinks=["https://www.nytimes.com/h", "https://www.bbc.com/h"])
    divs = BaikeRedactionWatch(baike_fetch=_seq(baike), wiki_fetch=_seq(wiki)).observe(
        e, observed_at=1000.0)["divergences"]
    fork = [d for d in divs if d.kind == ENCYCLOPEDIA_FORK][0]
    assert "sourcing_monoculture" in fork.detail


def test_no_fork_when_wiki_fetch_failed_abstains():
    """A blocked Wikipedia read must abstain, NEVER be misread as 'Wikipedia also lacks it'."""
    e = Entity("某争议事件", lemma_id="3", domain="UNREST")

    def boom(url):
        raise urllib.error.URLError("blocked")

    res = BaikeRedactionWatch(baike_fetch=_seq(_baike(summary="正常词条。", paras=["内容。" * 30])),
                              wiki_fetch=boom).observe(e, observed_at=1000.0)
    assert not any(d.kind == ENCYCLOPEDIA_FORK for d in res["divergences"])


def test_fork_never_uses_content_fp_equality():
    """Two normal, fully-aligned entries (no sensitive delta, mixed sourcing) must NOT fork
    just because their prose differs — the PLATFORM_FORK trap the design avoids."""
    e = Entity("某中性主题", lemma_id="4", domain="OTHER")
    baike = _baike(summary="某中性主题介绍。", paras=["2023年的一些中性事实。"],
                   refs=["https://www.techweb.com.cn/x"])
    wiki = _wiki("某中性主题", "2023年关于该主题的中性描述，措辞与百科不同。",
                 externallinks=["https://www.techweb.com.cn/x"])
    divs = BaikeRedactionWatch(baike_fetch=_seq(baike), wiki_fetch=_seq(wiki)).observe(
        e, observed_at=1000.0)["divergences"]
    assert not any(d.kind == ENCYCLOPEDIA_FORK for d in divs)


# ── detection (ii): the state-rewrite classifier ────────────────────────────────────────────

def test_state_rewrite_signal_qin_gang():
    label, reasons = state_rewrite_signal(extract_baike(QIN_R1), extract_baike(QIN_R2))
    assert label == STATE_REWRITE_SUSPECTED
    joined = " ".join(reasons)
    assert "bio_truncation" in joined and "role_removal" in joined and "sourcing_collapse" in joined


def test_state_rewrite_signal_li_keqiang_euphemism():
    label, reasons = state_rewrite_signal(extract_baike(LI_R1), extract_baike(LI_R2))
    assert label == STATE_REWRITE_SUSPECTED
    joined = " ".join(reasons)
    assert "euphemism_substitution" in joined and "身体原因" in joined


def test_normal_growth_is_not_a_rewrite():
    label, reasons = state_rewrite_signal(extract_baike(NORMAL_R1), extract_baike(NORMAL_R2))
    assert label == NORMAL_EDIT and len(reasons) < 2


def test_meme_euphemism_negative_control_does_not_over_fire():
    """A euphemism present in BOTH rounds (the 蛤 meme) is not a substitution → NORMAL_EDIT."""
    label, reasons = state_rewrite_signal(extract_baike(JIANG_R1), extract_baike(JIANG_R2))
    assert label == NORMAL_EDIT


def test_reworded_dated_paragraph_is_not_counted_as_deletion():
    """An ADDITIVE rewording (a dated paragraph that merely grew) must not register as a
    section deletion. The paragraph-SET diff alone sees removal+addition; difflib containment
    recognises the survivor, so the 江泽民/蛤-meme control produces NO dated_paragraph_deletion
    reason (pinning the fragility the reviewer flagged — one benign signal no longer inflates
    the count). Contrast test_state_rewrite_signal_li_keqiang_euphemism, where a SANITIZING
    rewording (content lost to a euphemism) has low containment and IS still counted."""
    label, reasons = state_rewrite_signal(extract_baike(JIANG_R1), extract_baike(JIANG_R2))
    assert label == NORMAL_EDIT
    assert not any(r.startswith("dated_paragraph_deletion") for r in reasons)


def test_latest_year_suppressed_on_dom_churn_fallback():
    """On the DOM-churn fallback the body is normalize_body-collapsed, so 4-digit years are
    erased and latest_year is None — recency is SUPPRESSED (never guessed off chrome), so
    bio_truncation/bio_gap cannot false-fire on a churned page. Pins the documented behaviour
    the module's docstring now describes accurately."""
    html = "<html><body>" + ("某主题在2023年取得重要进展，引发广泛关注。" * 10) + "</body></html>"
    ex = extract_baike(html)
    assert ex["present"] is True and ex["paragraphs"] == []  # selector miss → whole-body path
    assert ex["latest_year"] is None


# ── collector: two-round behaviour ──────────────────────────────────────────────────────────

def test_observe_two_rounds_emits_state_rewrite_with_bounded_velocity():
    e = Entity("秦刚", lemma_id="123", domain="LEADERSHIP")
    w = BaikeRedactionWatch(baike_fetch=_seq(QIN_R1, QIN_R2),
                            wiki_fetch=_seq(WIKI_MISSING, WIKI_MISSING))  # no control → fork inert
    assert w.observe(e, observed_at=1000.0)["divergences"] == []        # round 1: baseline only
    res = w.observe(e, observed_at=1000.0 + 86400)                      # round 2: the rewrite
    rw = [d for d in res["divergences"] if d.kind == STATE_REWRITE]
    assert len(rw) == 1
    assert res["state_rewrite_label"] == STATE_REWRITE_SUSPECTED
    # velocity is poll-bounded and shown suppressed, never a precise censor-action latency
    obs = redaction_to_ddti(rw[0])
    assert obs.get("latency_bounded_by_poll") is True and "suppressed" in obs["velocity_note"]


def test_chrome_only_change_emits_no_mutation():
    e = Entity("某词条", lemma_id="5")
    w = BaikeRedactionWatch(baike_fetch=_seq(CHROME_R1, CHROME_R2),
                            wiki_fetch=_seq(WIKI_MISSING, WIKI_MISSING))
    w.observe(e, observed_at=1000.0)
    res = w.observe(e, observed_at=2000.0)
    assert res["divergences"] == []  # paragraph-set fp unchanged ⇒ no fake MUTATION


def test_normal_edit_mutation_is_kept_but_not_relabelled():
    e = Entity("某科技公司", lemma_id="6")
    w = BaikeRedactionWatch(baike_fetch=_seq(NORMAL_R1, NORMAL_R2),
                            wiki_fetch=_seq(WIKI_MISSING, WIKI_MISSING))
    w.observe(e, observed_at=1000.0)
    res = w.observe(e, observed_at=2000.0)
    muts = [d for d in res["divergences"] if d.kind in (MUTATION, STATE_REWRITE)]
    assert len(muts) == 1 and muts[0].kind == MUTATION          # a real edit, not relabelled
    assert res["state_rewrite_label"] == NORMAL_EDIT


def test_fetch_failure_is_not_a_deletion_and_preserves_baseline():
    """Round 1 present, round 2 fetch fails, round 3 present-unchanged. The failure must NOT
    masquerade as a DELETION, and must not corrupt the baseline."""
    e = Entity("某词条", lemma_id="7")
    page = _baike(summary="稳定词条。", paras=["稳定内容一。", "稳定内容二。"])

    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 2:
            raise urllib.error.URLError("timeout")
        return page

    w = BaikeRedactionWatch(baike_fetch=flaky, wiki_fetch=_seq(WIKI_MISSING, WIKI_MISSING, WIKI_MISSING))
    assert w.observe(e, observed_at=1000.0)["status"] == "ok"
    r2 = w.observe(e, observed_at=2000.0)
    assert r2["status"] == "baike_fetch_failed"
    assert not any(d.kind == DELETION for d in r2["divergences"])   # NOT a scrub
    r3 = w.observe(e, observed_at=3000.0)
    assert r3["divergences"] == []  # baseline intact: unchanged page ⇒ no divergence


def test_real_deletion_is_detected_distinctly():
    e = Entity("某词条", lemma_id="8")
    page = _baike(summary="词条。", paras=["内容一。", "内容二。"])
    w = BaikeRedactionWatch(baike_fetch=_seq(page, "该词条已被删除"),
                            wiki_fetch=_seq(WIKI_MISSING, WIKI_MISSING))
    w.observe(e, observed_at=1000.0)
    res = w.observe(e, observed_at=1000.0 + 900)
    dels = [d for d in res["divergences"] if d.kind == DELETION]
    assert len(dels) == 1 and "deleted" in dels[0].detail
    assert dels[0].severity() == "critical"  # <3600s ⇒ censor graded it urgent


def test_disambiguation_landing_is_flagged_not_treated_as_entity():
    e = Entity("张伟")  # common name, no lemma_id pinned
    w = BaikeRedactionWatch(baike_fetch=_seq('<div class="lemmaWgt-subLemmaList">多义词</div>'),
                            wiki_fetch=_seq(_wiki("张伟", "张伟可以指多个人物。")))
    res = w.observe(e, observed_at=1000.0)
    assert res["status"] == "disambiguation_flagged"
    assert res["divergences"] == []  # never fingerprinted as the subject, never a fork


def test_inert_without_backend_is_not_a_false_zero():
    """Both surfaces unreachable ⇒ no divergences AND an explicit fetch_failed status."""
    e = Entity("某词条", lemma_id="9")

    def boom(url):
        raise urllib.error.URLError("offline")

    res = BaikeRedactionWatch(baike_fetch=boom, wiki_fetch=boom).observe(e, observed_at=1000.0)
    assert res["status"] == "baike_fetch_failed" and res["divergences"] == []
    assert res["baike"]["interstitial"] == "fetch_failed"


# ── inert-by-default: bare construction performs NO network I/O ─────────────────────────────

def test_default_fetch_is_inert_no_real_network(monkeypatch):
    """A bare BaikeRedactionWatch() (no injected fetch, no proxy, no PALIMPSEST_LIVE, no
    governance) must NOT reach the real network. We trip-wire the underlying urllib fetch: it
    must never be called, and the round must degrade to fetch_failed (inert, never a false
    zero). This is the inert claim as a TEST, not an inspection."""
    import collectors.baike_redaction as br

    called = {"n": 0}

    def tripwire(url, proxy=None, timeout=20.0):
        called["n"] += 1
        raise AssertionError("real urllib fetch must not be reached when inert")

    monkeypatch.setattr(br, "_default_fetch", tripwire)
    monkeypatch.delenv("PALIMPSEST_LIVE", raising=False)
    monkeypatch.delenv("PALIMPSEST_PROXY", raising=False)

    w = br.BaikeRedactionWatch()  # every default, nothing injected
    res = w.observe(Entity("某词条", lemma_id="1"), observed_at=1000.0)

    assert called["n"] == 0                              # the real fetch was never reached
    assert res["status"] == "baike_fetch_failed"         # fail-soft
    assert res["divergences"] == []                      # never a false zero
    assert res["baike"]["interstitial"] == "fetch_failed"


def test_default_fetch_wiring_used_when_live_enabled(monkeypatch):
    """The non-injected default DOES wire through to the urllib seam once live mode is
    explicitly enabled (PALIMPSEST_LIVE=1), hitting the correct Baike + Wikipedia URLs. Pins
    the default fetch path that the inert test deliberately short-circuits."""
    import collectors.baike_redaction as br

    seen = {"urls": []}

    def fake(url, proxy=None, timeout=20.0):
        seen["urls"].append(url)
        return _baike(summary="正常词条。", paras=["内容。" * 30])

    monkeypatch.setattr(br, "_default_fetch", fake)
    monkeypatch.setenv("PALIMPSEST_LIVE", "1")
    monkeypatch.delenv("PALIMPSEST_PROXY", raising=False)

    w = br.BaikeRedactionWatch()
    res = w.observe(Entity("某词条", lemma_id="1"), observed_at=1000.0)

    assert any("baike.baidu.com" in u for u in seen["urls"])   # default Baike wiring exercised
    assert any("zh.wikipedia.org" in u for u in seen["urls"])  # default Wikipedia wiring exercised
    assert res["status"] == "ok"  # Baike parsed as present; the fetch seam was actually used


# ── governance gating ────────────────────────────────────────────────────────────────────────

def test_collector_refuses_when_killswitched(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_BAIKE_UNSET")
    ks.engage("test")
    w = BaikeRedactionWatch(baike_fetch=lambda u: "should not be called",
                            wiki_fetch=lambda u: "should not be called", kill_switch=ks)
    with pytest.raises(RuntimeError):
        w.observe(Entity("秦刚", lemma_id="1"))


def test_collector_consults_rate_ceiling():
    hits = {"n": 0}

    class CountingRate:
        def acquire(self, tokens=1.0):
            hits["n"] += 1

    w = BaikeRedactionWatch(baike_fetch=_seq(_baike(summary="x", paras=["内容。" * 30])),
                            wiki_fetch=_seq(WIKI_MISSING), rate_ceiling=CountingRate())
    w.observe(Entity("某词条", lemma_id="1"), observed_at=1000.0)
    assert hits["n"] == 2  # one acquire per outbound read (baike + wiki), polite by construction


# ── persistence: baselines survive across collector instances ───────────────────────────────

def test_baseline_store_persists_mutation_across_instances(tmp_path):
    store = JsonBaselineStore(str(tmp_path / "baselines"))
    e = Entity("某科技公司", lemma_id="6")
    BaikeRedactionWatch(baike_fetch=_seq(NORMAL_R1), wiki_fetch=_seq(WIKI_MISSING),
                        store=store).observe(e, observed_at=1000.0)
    # a fresh collector backed by the same store still remembers the baseline fingerprint
    res = BaikeRedactionWatch(baike_fetch=_seq(NORMAL_R2), wiki_fetch=_seq(WIKI_MISSING),
                              store=store).observe(e, observed_at=2000.0)
    muts = [d for d in res["divergences"] if d.kind in (MUTATION, STATE_REWRITE)]
    assert len(muts) == 1 and muts[0].kind == MUTATION
    # the prior TEXT is not in the fingerprint store, so the rewrite label is unavailable — the
    # divergence still fires honestly as a MUTATION (fingerprint = the fact)
    assert "state_rewrite_label" not in res


# ── DDTI integration: redaction findings flow into the existing index ───────────────────────

def test_divergence_to_observation_handles_new_kinds_unchanged():
    """The reused, UNCHANGED adapter maps the new kinds straight onto deletion_signal."""
    e = Entity("四通桥事件", lemma_id="0", domain="UNREST")
    fork = [d for d in BaikeRedactionWatch(
        baike_fetch=_seq("百科尚未收录词条"),
        wiki_fetch=_seq(_wiki("四通桥事件", "2022年抗议事件。"))).observe(
        e, observed_at=1000.0)["divergences"] if d.kind == ENCYCLOPEDIA_FORK][0]
    base = divergence_to_observation(fork)            # the unchanged undertext adapter
    assert base["deletion_signal"] == ENCYCLOPEDIA_FORK and base["terms"] == ["四通桥事件"]


def test_redaction_flows_into_ddti_selectivity_index():
    """A redaction divergence scores as censor attention in the same index that consumes
    CDT-sourced deletions — the encyclopedia front-end feeds the passive DDTI loop."""
    from datetime import datetime, timezone
    from processors.ddti_index import compute_selectivity_novelty

    e = Entity("六四事件", lemma_id="1", domain="UNREST")
    baike = _baike(summary="1989年政治风波。", paras=["1989年北京发生政治风波。"])
    wiki = _wiki("六四事件", "1989年六四天安门事件，军队镇压示威者。")
    fork = [d for d in BaikeRedactionWatch(baike_fetch=_seq(baike), wiki_fetch=_seq(wiki)).observe(
        e, observed_at=1000.0)["divergences"] if d.kind == ENCYCLOPEDIA_FORK][0]

    obs = redaction_to_ddti(fork)
    assert obs["deletion_signal"] == ENCYCLOPEDIA_FORK
    assert obs["source"].startswith("baike-redaction:")
    assert "六四" in obs["terms"] and "六四事件" in obs["terms"]

    now = datetime.now(timezone.utc)
    obs["detected_at"] = now  # treat as a current observation for the index
    index = compute_selectivity_novelty([obs], now)
    assert index["n_terms"] >= 1
    assert any(r["term"] == "六四" for r in index["ranked"])


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
