"""Data darkness collector — parsing the publication surfaces.

Fixtures are cut from the real pages as served on 2026-08-01 (the OMO anchor
carries its publication date in the URL slug and its sequential number in the
title; the Money & Banking attachments carry their upload date in the
attachDir name and the data month as anchor text; SAFE uploads carry their
date in the /file/ path). If a regex here has to change, the source markup
drifted — re-probe the live pages before touching the pattern, and say what
changed in the commit.

Offline and stdlib-only: no fixture makes a network call.
"""
from __future__ import annotations

import json

from collectors.data_darkness import (
    parse_calendar_row,
    parse_mb_stats,
    parse_nbs_listing,
    parse_nra,
    parse_omo,
    parse_safe_settlement,
    read_cfets_freshness,
)

OMO_PAGE = """
<a href="/zhengcehuobisi/125207/125213/125431/125475/2026073108575959224/index.html"
   onclick="void(0)" target="_blank" title="公开市场业务交易公告 [2026]第147号"
   istitle="true">公开市场业务交易公告 [2026]第147号</a>
<span class="hui12">2026-07-31</span>
<a href="/zhengcehuobisi/125207/125213/125431/125475/2026073009012172928/index.html"
   onclick="void(0)" target="_blank" title="公开市场业务交易公告 [2026]第146号"
   istitle="true">公开市场业务交易公告 [2026]第146号</a>
<span class="hui12">2026-07-30</span>
"""

MB_PAGE = """
<td><a href="/diaochatongjisi/attachDir/2026/02/2026022816011080796.xls">01</a></td>
<td><a href="/diaochatongjisi/attachDir/2026/06/2026063015594425261.xls">05</a></td>
<td><a href="/diaochatongjisi/attachDir/2026/07/2026073115592496796.xls">06</a></td>
<td><a href="/diaochatongjisi/attachDir/2026/07/2026070715593435513.htm">htm</a></td>
"""

SAFE_PAGE = """
<a href="/safe/file/file/20260717/a246a5ad2ced4f91b5ade2d3b6fb60f5.xlsx"
   title="2026年银行结售汇数据（分地区）.xlsx">2026年银行结售汇数据（分地区）</a>
<a href="/safe/file/file/20260115/57fc87bffdbf498ca9556bbc981e7c3d.xlsx"
   title="2025年银行结售汇数据（分地区）.xlsx">2025年银行结售汇数据（分地区）</a>
<a href="/safe/file/file/20260720/ffffffffffffffffffffffffffffffff.xlsx"
   title="2026年其他数据.xlsx">unrelated file, must not count</a>
"""


def test_omo_latest_announcement_and_number_come_from_the_same_anchor():
    got = parse_omo(OMO_PAGE)
    assert got["latest_publication"] == "2026-07-31"
    assert got["latest_announcement_no"] == 147
    assert got["n_listed"] == 2
    assert got["numbers_listed_min"] == 146
    assert got["numbers_listed_max"] == 147


def test_omo_number_gap_evidence_never_spans_the_january_rollover():
    """The announcement numbers restart each year; a January listing mixing
    [2025]第252号 with [2026]第2号 must scope min/max to the latest year, or
    the calendar itself would fake a 250-announcement hole."""
    page = """
    <a href="/zhengcehuobisi/125207/125213/125431/125475/2026010409000000000/index.html"
       title="公开市场业务交易公告 [2026]第2号">公开市场业务交易公告 [2026]第2号</a>
    <a href="/zhengcehuobisi/125207/125213/125431/125475/2025123109000000000/index.html"
       title="公开市场业务交易公告 [2025]第252号">公开市场业务交易公告 [2025]第252号</a>
    """
    got = parse_omo(page)
    assert got["latest_publication"] == "2026-01-04"
    assert got["announcement_year"] == 2026
    assert got["numbers_listed_min"] == 2 and got["numbers_listed_max"] == 2
    assert got["n_listed"] == 2 and got["n_listed_latest_year"] == 1


def test_omo_returns_none_when_no_announcement_anchor_matches():
    """A page that answers but shows no announcements is a parse miss, not a
    zero — the caller must treat it as unreachable, never as darkness."""
    assert parse_omo("<html><body>renovated portal</body></html>") is None


def test_mb_stats_takes_the_highest_month_and_its_upload_date():
    got = parse_mb_stats(MB_PAGE, 2026)
    assert got["latest_period"] == "2026-06"
    assert got["latest_publication"] == "2026-07-31"
    assert got["months_listed"] == 3


def test_mb_stats_ignores_attachments_not_labelled_with_a_data_month():
    page = '<a href="/diaochatongjisi/attachDir/2026/07/2026070715593435513.htm">htm</a>'
    assert parse_mb_stats(page, 2026) is None


def test_mb_stats_a_reupload_keeps_the_latest_upload_for_that_month():
    page = MB_PAGE + (
        '<a href="/diaochatongjisi/attachDir/2026/08/2026080115592496796.xls">06</a>'
    )
    got = parse_mb_stats(page, 2026)
    assert got["latest_period"] == "2026-06"
    assert got["latest_publication"] == "2026-08-01"


def test_safe_counts_only_settlement_files():
    got = parse_safe_settlement(SAFE_PAGE)
    assert got["latest_publication"] == "2026-07-17"
    assert got["n_files"] == 2


def test_cfets_freshness_scores_the_most_stale_family_and_carries_our_look(tmp_path):
    """latest_publication is the MIN over families: a single family going
    quiet (repo fixings pulled while SHIBOR lives) is the most realistic
    CFETS withholding event, and a max would hide it. our_last_look carries
    the china-econ reading's own timestamp so the driver can tell 'CFETS
    quiet' from 'our reader quiet'."""
    hist = tmp_path / "china-econ-history.jsonl"
    rows = [
        {"date": "2026-07-30", "shibor_on": 1.41, "fdr007": 1.45},
        {"date": "2026-07-31", "fdr007": 1.44},
        {"date": "2026-07-29", "usdcny_parity": 6.7894},
        "not json at all",
    ]
    hist.write_text("\n".join(
        r if isinstance(r, str) else json.dumps(r) for r in rows
    ), encoding="utf-8")
    latest = tmp_path / "china-econ-latest.json"
    latest.write_text(json.dumps({"generated_at": "2026-08-01T04:04:31Z"}),
                      encoding="utf-8")

    got = read_cfets_freshness(str(hist), str(latest))
    assert got["families"] == {
        "shibor": "2026-07-30",
        "repo_fixing": "2026-07-31",
        "central_parity": "2026-07-29",
    }
    assert got["latest_publication"] == "2026-07-29", (
        "the most stale family drives the score")
    assert got["our_last_look"] == "2026-08-01T04:04:31Z"


def test_cfets_freshness_is_none_when_the_history_is_absent_or_empty(tmp_path):
    """No local history is a statement about this checkout, not about CFETS."""
    missing_latest = str(tmp_path / "no-latest.json")
    assert read_cfets_freshness(str(tmp_path / "missing.jsonl"), missing_latest) is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert read_cfets_freshness(str(empty), missing_latest) is None


# The zxfb firehose repeats each item as three responsive anchors (dedup on
# href); publication date is in the filename, the DATA period in the title.
ZXFB_PAGE = """
<li>
<a class="fl pc_1600" href="./202607/t20260715_1964128.html" target="_blank"
   title='2026年6月份能源生产情况'>2026年6月份能源生产情况</a>
<a class="fl mhide pc1200" href="./202607/t20260715_1964128.html" target="_blank"
   title='2026年6月份能源生产情况'>2026年6月份能源生产情况</a>
<a class="fl pchide" href="./202607/t20260715_1964128.html" target="_blank"
   title='2026年6月份能源生产情况'>能源生产情况</a>
<span>2026-07-15</span></li>
<li>
<a class="fl pc_1600" href="./202606/t20260616_1963001.html" target="_blank"
   title='2026年5月份能源生产情况'>2026年5月份能源生产情况</a>
<span>2026-06-16</span></li>
<li>
<a class="fl pc_1600" href="./202607/t20260715_1964123.html" target="_blank"
   title='2026年6月份规模以上工业增加值增长5.3%'>2026年6月份规模以上工业增加值增长5.3%</a>
<span>2026-07-15</span></li>
"""

CALENDAR_PAGE = """
<h1>2026年国家统计局主要统计信息发布日程</h1>
<table><tr>
<td><span>8</span></td><td><span>能源生产情况月度报告</span></td>
<td><span>19/一</span></td><td><span>……</span></td><td><span>16/一</span></td>
<td><span>16/四</span></td><td><span>18/一</span></td><td><span>16/二</span></td>
<td><span>15/三</span></td><td><span>17/一</span></td><td><span>15/二</span></td>
<td><span>19/一</span></td><td><span>16/一</span></td><td><span>15/二</span></td>
</tr></table>
"""

NRA_PAGE = """
<script>document.write('2026-07-14')</script>
<a href="./202607/t20260714_351658.shtml" target="_blank"
   title='2026年6月份全国铁路主要指标完成情况'>2026年6月份全国铁路主要指标完成情况</a>
<a href="./202606/t20260615_351434.shtml" target="_blank"
   title='2026年5月份全国铁路主要指标完成情况'>2026年5月份全国铁路主要指标完成情况</a>
"""


def test_nbs_listing_dedups_responsive_anchors_and_reads_period_from_title():
    got = parse_nbs_listing(ZXFB_PAGE, "能源生产情况")
    assert got["latest_publication"] == "2026-07-15"
    assert got["latest_period"] == "2026-06"
    assert got["n_listed"] == 2, "three anchors per item must dedup to one href"


def test_nbs_listing_stems_do_not_cross_match():
    got = parse_nbs_listing(ZXFB_PAGE, "规模以上工业增加值")
    assert got["latest_publication"] == "2026-07-15"
    assert got["n_listed"] == 1


def test_nbs_listing_none_when_the_stem_is_absent():
    assert parse_nbs_listing(ZXFB_PAGE, "固定资产投资") is None


def test_calendar_row_maps_months_to_promised_days():
    row = parse_calendar_row(CALENDAR_PAGE, "能源生产情况月度报告", 2026)
    assert row[1] == 19 and row[3] == 16 and row[12] == 15
    assert 2 not in row, "the '……' month carries no promise"


def test_calendar_from_the_wrong_year_is_not_trusted():
    """The /bnxxfb/ URL is year-stable and its content is replaced each
    January; last year's promises must never score this year's silence."""
    assert parse_calendar_row(CALENDAR_PAGE, "能源生产情况月度报告", 2027) is None


def test_calendar_row_none_when_the_label_is_absent():
    assert parse_calendar_row(CALENDAR_PAGE, "居民消费价格月度报告", 2026) is None


def test_a_misaligned_or_truncated_row_is_a_miss_not_a_guess():
    """A partial cell walk means drifted markup; wrong promised days would
    score silence against the wrong dates, so fewer than 10 cells is None."""
    truncated = CALENDAR_PAGE.split("<td><span>16/四</span></td>")[0]
    assert parse_calendar_row(truncated, "能源生产情况月度报告", 2026) is None


def test_nra_latest_item_and_period():
    got = parse_nra(NRA_PAGE)
    assert got["latest_publication"] == "2026-07-14"
    assert got["latest_period"] == "2026-06"


def test_nra_none_on_a_validator_shell():
    """The anti-bot gate serves a page with no listing anchors; that must
    read as unreachable (None), never as a missing release."""
    assert parse_nra("<html><body>/GE/CC/VALIDATOR</body></html>") is None
