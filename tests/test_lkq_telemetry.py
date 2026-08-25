"""LKQ telemetry parsers — narrative text is the source, so the fixtures ARE
the contract.

Every fixture sentence below is cut verbatim from the live releases as
served on 2026-08-01 (NBS Energy Production / Industrial Production for June
2026, the PBC H1 2026 financial statistics report, the NRA June rail news).
When a parser here has to change, the state reworded its releases — that is
itself a fact worth a commit message, and the collector's job in that moment
is to return None loudly, never to guess.

Offline, stdlib-only.
"""
from __future__ import annotations

import pytest

import collectors.lkq_telemetry as lkq
from core.safe_fetch import FetchError
from collectors.lkq_telemetry import (
    find_article,
    find_rail_article,
    next_listing_page,
    parse_energy,
    parse_industrial,
    parse_nra_rail,
    parse_pbc_loans,
    pbc_period,
    period_from_en_title,
    rail_period,
)

ENERGY_TEXT = (
    "In June, the raw coal production by industrial enterprises above the "
    "designated size was 380 million tons, down by 9.7% year on year. "
    "The electricity generation by industrial enterprises above the designated "
    "size was 827.6 billion kWh, a year-on-year increase of 2.0%."
)

IP_TEXT = (
    "<p>In June, the value added of industrial enterprises above the designated "
    "size increased by 5.3% year on year in real terms.</p>"
    "<tr><td><span>Crude Steel (10,000 tons)</span></td>"
    "<td><span>8367</span></td><td><span>0.4</span></td>"
    "<td><span>49995</span></td><td><span>-3.0</span></td></tr>"
)

PBC_TEXT = "六月末，人民币贷款余额268.56万亿元，同比增长7.1%。"

NRA_TEXT = (
    "上半年，全国铁路累计完成货物发送量26.22亿吨，同比增长2.5%。"
    "6月份，完成货物发送量4.36亿吨，同比基本持平。"
)

NRA_TEXT_NUMERIC = (
    "1至5月份，全国铁路累计完成货运发送量21.86亿吨，同比增长3.1%。"
    "5月份，完成货运发送量4.59亿吨，同比增长4.5%。"
)


def test_energy_parses_signed_yoy_for_both_series():
    got = parse_energy(ENERGY_TEXT)
    assert got["electricity_yoy"] == pytest.approx(2.0)
    assert got["coal_yoy"] == pytest.approx(-9.7)


def test_energy_reworded_returns_nothing_not_a_guess():
    assert parse_energy("The grid performed admirably this month.") == {}


SUMMARY_BLEED = (
    "the growth of electricity generation was steady. I. Raw Coal Production "
    "Raw coal production fell year on year. In June, the raw coal production "
    "by industrial enterprises above the designated size was 380 million tons, "
    "down by 9.7% year on year; the average daily output was 12.70 million "
    "tons. In June, the electricity generation by industrial enterprises above "
    "the designated size was 827.6 billion kWh, a year-on-year increase of "
    "2.0%; the average daily electricity generation was 27.59 billion kWh. "
    "From January to June, electricity generation was 4,750.1 billion kWh, "
    "a year-on-year increase of 3.5%."
)


def test_the_summary_preamble_cannot_bleed_the_coal_number_into_electricity():
    """Caught live 2026-08-01: the numberless summary mention of electricity
    sat 300 chars before the coal figure, and an unbounded window read
    electricity_yoy = -9.7. The value must come from the keyword's own
    sentence, and the monthly figure must win over the cumulative one."""
    got = parse_energy(SUMMARY_BLEED)
    assert got["electricity_yoy"] == pytest.approx(2.0)
    assert got["coal_yoy"] == pytest.approx(-9.7)


def test_industrial_headline_and_steel_table_row():
    got = parse_industrial(IP_TEXT)
    assert got["ip_yoy"] == pytest.approx(5.3)
    assert got["steel_yoy"] == pytest.approx(0.4), (
        "second numeric cell is the month YoY; the (10,000 tons) unit inside "
        "the label text must not be read as a value")


def test_pbc_outstanding_loan_yoy_and_period_forms():
    got = parse_pbc_loans(PBC_TEXT)
    assert got["value"] == pytest.approx(7.1)
    assert got["basis"] == "rmb_balance"
    down = parse_pbc_loans("人民币贷款余额200万亿元，同比下降1.2%。")
    assert down["value"] == pytest.approx(-1.2)
    assert pbc_period("2026年上半年金融统计数据报告") == "2026-06"
    assert pbc_period("2026年一季度金融统计数据报告") == "2026-03"
    assert pbc_period("2026年5月金融统计数据报告") == "2026-05"
    assert pbc_period("2025年金融统计数据报告") == "2025-12", (
        "the annual report's bare title covers December — without this the "
        "December data month abstains every January")


def test_pbc_afre_line_is_a_different_estimand_and_never_matches():
    """Caught live 2026-08-01: the H1 2026 report leads with the AFRE
    loans-to-real-economy sentence, whose 5.3% is NOT the loan-balance
    growth the Li Keqiang index uses. It must be skipped, and when the plain
    RMB line is absent (this report no longer prints it), the all-currency
    line is used with its basis named."""
    afre_only = "6月末对实体经济发放的人民币贷款余额279.16万亿元，同比增长5.3%。"
    assert parse_pbc_loans(afre_only) is None

    real_report = (afre_only
                   + "五、上半年人民币贷款增加10.72万亿元。"
                   + "6月末，本外币贷款余额286.43万亿元，同比增长5.1%。")
    got = parse_pbc_loans(real_report)
    assert got["value"] == pytest.approx(5.1)
    assert got["basis"] == "all_currency_balance"


def test_rail_cumulative_is_primary_and_prose_flat_maps_to_zero_flagged():
    got = parse_nra_rail(NRA_TEXT)
    assert got["rail_freight_yoy"] == pytest.approx(2.5)
    assert got["rail_freight_month_yoy"] == 0.0
    assert got["rail_month_flat_prose"] is True


def test_rail_accepts_both_freight_wordings():
    got = parse_nra_rail(NRA_TEXT_NUMERIC)
    assert got["rail_freight_yoy"] == pytest.approx(3.1)
    assert got["rail_freight_month_yoy"] == pytest.approx(4.5)
    assert "rail_month_flat_prose" not in got


def test_rail_period_prefers_title_markers_then_falls_back_to_body():
    assert rail_period("2026年上半年国家铁路运输情况", "") == "2026-06"
    assert rail_period("2026年国家铁路运输持续向好", NRA_TEXT) == "2026-06"
    assert rail_period("铁路新闻", "无相关内容") is None


def test_en_title_period():
    assert period_from_en_title("Energy Production in June 2026") == "2026-06"
    assert period_from_en_title("Energy Production Annual Report") is None
    assert period_from_en_title(
        "Industrial Production Operation in January and February 2026") == "2026-02", (
        "NBS publishes no standalone January release; the combined article "
        "scores as the February data month")


LISTING = """
<a target="_blank" title="Energy Production in June 2026"
   href="./202607/t20260717_1964155.html">5.Energy Production in June 2026</a>
<a target="_blank" title="Industrial Production Operation in June 2026"
   href="./202607/t20260717_1964159.html">6.Industrial Production Operation in June 2026</a>
"""

NRA_LISTING = """
<a href="./202607/t20260729_351737.shtml" target="_blank"
   title='蒙内铁路货运量累计突破5000万吨'>蒙内铁路货运量累计突破5000万吨</a>
<a href="./202607/t20260715_351669.shtml" target="_blank"
   title='2026年上半年国家铁路发送货物26.22亿吨'>2026年上半年国家铁路发送货物26.22亿吨</a>
<a href="./202606/t20260615_351400.shtml" target="_blank"
   title='2026年5月份国家铁路客货运量情况'>2026年5月份国家铁路客货运量情况</a>
<a href="./202607/t20260720_351700.shtml" target="_blank"
   title='铁路安全生产会议召开'>铁路安全生产会议召开</a>
"""


def test_find_article_matches_the_stem():
    href, title = find_article(LISTING, "Energy Production in")
    assert href.endswith("t20260717_1964155.html")
    assert title == "Energy Production in June 2026"
    assert find_article(LISTING, "Fixed Asset Investment") is None


def test_find_rail_article_takes_newest_freight_item_not_meetings():
    href, title = find_rail_article(NRA_LISTING)
    assert href.endswith("t20260715_351669.shtml"), (
        "the overseas project and newer meeting item must both lose to the "
        "newest national freight/volume item")


def test_pbc_next_page_accepts_its_javascript_tagname_target():
    listing = """
    <a style="cursor:pointer"
       onclick="queryArticleByCondition(this,'/goutongjiaoliu/113456/113469/11040-2.html')"
       tagname="/goutongjiaoliu/113456/113469/11040-2.html"
       class="pagingNormal">下一页</a>
    """
    assert next_listing_page(
        "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
        listing,
    ) == "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/11040-2.html"


def test_pbc_report_discovery_follows_one_bounded_listing_page(monkeypatch):
    page_1 = """
    <a title="2026年7月金融统计数据报告"
       href="/goutongjiaoliu/newer-report/index.html">新报告</a>
    <a tagname="/goutongjiaoliu/113456/113469/11040-2.html">下一页</a>
    """
    page_2 = """
    <a title="2026年上半年金融统计数据报告"
       href="/goutongjiaoliu/report/index.html">报告</a>
    """
    article = "六月末，人民币贷款余额268.56万亿元，同比增长7.1%。"
    fetched = []

    def fake_get(url, *, expected_host=None):
        fetched.append(url)
        assert expected_host == "www.pbc.gov.cn"
        if url.endswith("11040-2.html"):
            return page_2
        if url.endswith("/report/index.html"):
            return article
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(lkq, "_get", fake_get)
    monkeypatch.setattr(lkq, "SPACING_S", 0)
    got = lkq._fetch_pbc_report(page_1, "2026-06")

    assert got == (article, "2026年上半年金融统计数据报告")
    assert len(fetched) == 2


def test_get_uses_bounded_same_host_hardened_transport():
    seen = {}

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"]("https://www.stats.gov.cn/english/next.html")
        return b"<html>ok</html>"

    got = lkq._get(
        "https://www.stats.gov.cn/english/PressRelease/",
        retries=0,
        fetcher=fetcher,
    )

    assert got == "<html>ok</html>"
    assert seen["max_bytes"] == lkq.MAX_BYTES
    assert seen["max_redirects"] == 3
    assert seen["timeout"] == 30.0
    assert seen["headers"]["User-Agent"] == lkq.USER_AGENT


def test_get_redirect_policy_refuses_cross_host_and_private_targets():
    refused = []

    def fetcher(_url, **kwargs):
        policy = kwargs["url_policy"]
        for candidate in (
            "https://www.pbc.gov.cn/report",
            "http://127.0.0.1/admin",
        ):
            with pytest.raises(FetchError):
                policy(candidate)
            refused.append(candidate)
        raise FetchError("stop after policy assertions")

    assert lkq._get(
        "https://www.stats.gov.cn/english/PressRelease/",
        retries=0,
        fetcher=fetcher,
    ) is None
    assert len(refused) == 2


def test_source_policy_normalizes_malformed_input_to_fetch_error():
    with pytest.raises(FetchError):
        lkq._source_host(b"https://www.stats.gov.cn/")


def test_article_discovery_refuses_hostile_absolute_url_before_fetch(monkeypatch):
    listing = (
        '<a title="Energy Production in June 2026" '
        'href="http://169.254.169.254/latest/meta-data/">release</a>'
    )

    def no_fetch(*_args, **_kwargs):
        raise AssertionError("cross-authority discovery must fail before egress")

    monkeypatch.setattr(lkq, "_get", no_fetch)
    assert lkq._fetch_article(
        lkq.NBS_EN_URL,
        listing,
        "Energy Production in",
    ) is None


def test_next_listing_page_refuses_cross_authority_target():
    listing = '<a href="https://evil.example/next">\u4e0b\u4e00\u9875</a>'
    assert lkq.next_listing_page(lkq.PBC_LIST_URL, listing) is None
