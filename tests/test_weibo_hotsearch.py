"""Weibo hot-search collector tests: parse, join, regime labels and the sense
gate (offline)."""
import json

import collectors.weibo_hotsearch as hotsearch
from core.safe_fetch import FetchError

carries_sensitive_sense = hotsearch.carries_sensitive_sense
collect_range = hotsearch.collect_range
join_ddti = hotsearch.join_ddti
parse_day = hotsearch.parse_day
pinned_series = hotsearch.pinned_series
term_presence = hotsearch.term_presence
withdrawal_candidates = hotsearch.withdrawal_candidates

DAY = json.dumps([
    {"url": "/weibo?q=%23a%23&Refer=new_time", "title": "向上向善造福人类"},
    {"url": "/weibo?q=%23b%23&t=31&band_rank=1&Refer=top", "title": "澎湖海战 撤档"},
    {"url": "/weibo?q=%23c%23&t=31&band_rank=7&Refer=top", "title": "杭州暴雨"},
    {"url": "/weibo?q=%23d%23&t=31&band_rank=3&Refer=top", "title": "澎湖海战 票房"},
])


def test_parse_day_extracts_title_rank_pinned():
    rows = parse_day(DAY)
    assert len(rows) == 4
    pinned = [r for r in rows if r["pinned"]]
    assert len(pinned) == 1 and pinned[0]["rank"] is None
    assert {r["title"]: r["rank"] for r in rows}["澎湖海战 撤档"] == 1


def test_parse_day_rejects_garbage():
    assert parse_day("<html>404</html>") is None
    assert parse_day(json.dumps({"not": "a list"})) is None
    assert parse_day(json.dumps([])) is None


def test_collect_range_fail_soft_absence():
    fetched = collect_range(["2026-01-01", "2026-01-02"],
                            fetch=lambda d: DAY if d == "2026-01-02" else None)
    assert list(fetched) == ["2026-01-02"]


def test_archive_fetch_is_exact_bounded_and_redirect_free():
    seen = {}

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        try:
            kwargs["url_policy"]("https://raw.githubusercontent.com/other/repo.json")
        except FetchError:
            pass
        else:
            raise AssertionError("changed archive object must be refused")
        return DAY.encode("utf-8")

    assert hotsearch._get_raw("2026-01-02", fetcher=fetcher) == DAY
    assert seen["max_bytes"] == hotsearch.MAX_BYTES
    assert seen["max_redirects"] == 0


def test_archive_window_refuses_bad_dates_and_excessive_fanout_before_fetch():
    def no_fetch(*_args, **_kwargs):
        raise AssertionError("invalid window must fail before fetch")

    assert hotsearch._get_raw("../../secret", fetcher=no_fetch) is None
    assert collect_range(["2026-02-30"], fetch=no_fetch) == {}
    assert collect_range(["2026-01-01"] * 33, fetch=no_fetch) == {}


def test_day_parser_bounds_hostile_cardinality_and_fields(monkeypatch):
    monkeypatch.setattr(hotsearch, "MAX_ROWS_PER_DAY", 2)
    assert parse_day(json.dumps([{"title": "x", "url": "/x"}] * 3)) is None
    assert parse_day(json.dumps([{"title": "x" * 513, "url": "/x"}])) is None


def test_term_presence_substring_and_best_rank():
    days = {"2026-01-02": parse_day(DAY)}
    p = term_presence("澎湖海战", days)
    assert p["appearances"] == 2 and p["best_rank"] == 1
    assert p["days_present"] == ["2026-01-02"]


def test_join_ddti_regime_labels():
    days = {"2026-01-02": parse_day(DAY)}
    ddti = [{"term": "澎湖海战", "threat": 0.8},   # trending while deleted
            {"term": "白纸运动", "threat": 1.2}]   # never on the board
    joined = {j["term"]: j for j in join_ddti(ddti, days)}
    assert joined["澎湖海战"]["regime"] == "contained_visible"
    assert joined["澎湖海战"]["attention_ratio"] is not None
    assert joined["白纸运动"]["regime"] == "suppressed_invisible"
    assert joined["白纸运动"]["attention_ratio"] is None   # absence, not a number


def test_join_ddti_empty_days_abstains():
    assert join_ddti([{"term": "x", "threat": 1.0}], {}) == []


def test_pinned_series():
    days = {"2026-01-02": parse_day(DAY)}
    assert pinned_series(days) == [
        {"date": "2026-01-02", "pinned": ["向上向善造福人类"]}]


# ── the sense gate ─────────────────────────────────────────────────────────────

# The four false positives from the reading published 2026-08-01, verbatim board
# titles. Each is an everyday use of a gazetteer term that a bare substring scan
# scored as a breakthrough. This list is the evidence the gate is bound to: a
# rule change that lets any of these back through is a regression.
PUBLISHED_FALSE_POSITIVES = [
    ("失联", "重庆彭水发现失联中巴车残骸"),           # minibus wreck
    ("失联", "重庆失联00后网格员确认遇难"),           # accident death
    ("失联", "男孩失联5天后被找到躲在邻居空房"),      # boy at the neighbour's
    ("散步", "散步是一项隐私且暧昧的行为"),           # lifestyle essay
    ("散步", "教师午休散步猝死未被认定工亡"),         # death on a lunch walk
    ("维权", "虞书欣名誉维权案胜诉"),                 # celebrity reputation suit
    ("维权", "瑞幸泰国商标维权再胜创赔偿纪录"),       # Luckin trademark case
    ("屏蔽", "找工作屏蔽原公司的重要性"),             # job-hunt privacy advice
    ("失联", "尼泊尔山洪已致579死1924失联"),          # disaster casualty update
]

# The coded sense the gazetteer actually lists the terms for. Every one of
# these must survive the gate: the fix exists to remove noise, not recall.
SENSITIVE_SENSE_TITLES = [
    ("失联", "维权律师失联多日家属发声"),
    ("散步", "业主集体散步抵制物业费上涨"),
    ("维权", "维权律师被带走"),
    ("屏蔽", "多个热搜词条疑被屏蔽"),
]


def test_published_false_positives_are_dropped_with_a_named_cue():
    for term, title in PUBLISHED_FALSE_POSITIVES:
        keep, cue = carries_sensitive_sense(term, title)
        assert not keep, f"{term} in {title!r} must be gated"
        assert cue and cue in title   # a drop always says why


def test_sensitive_sense_titles_survive_the_gate():
    for term, title in SENSITIVE_SENSE_TITLES:
        keep, _cue = carries_sensitive_sense(term, title)
        assert keep, f"{term} in {title!r} is the coded sense and must count"


def test_sensitive_cue_overrides_ordinary_cue():
    # 胜诉 is on the ordinary list, but 业主 corroborates the coded sense and
    # is checked first: a rights case is not lost to the word for its win.
    keep, cue = carries_sensitive_sense("维权", "多地业主维权案胜诉")
    assert keep and cue == "业主"


def test_unknown_context_defaults_to_keep():
    # Recall direction pinned: a listed term in a context the rules do not
    # recognise is published, not suppressed. Over-tightening is silent and
    # silence is the failure mode this collector exists to catch.
    assert carries_sensitive_sense("失联", "多名工人失联") == (True, None)


def test_unlisted_terms_pass_ungated():
    assert carries_sensitive_sense("彭帅", "彭帅") == (True, None)
    assert carries_sensitive_sense("白纸", "白纸运动纪念活动") == (True, None)


def test_term_presence_gates_and_records_the_drop():
    days = {
        "2026-07-27": [{"title": "重庆彭水发现失联中巴车残骸",
                        "rank": 16, "pinned": False}],
        "2026-07-29": [{"title": "维权律师失联多日家属发声",
                        "rank": 30, "pinned": False}],
    }
    p = term_presence("失联", days)
    assert p["appearances"] == 1
    assert p["days_present"] == ["2026-07-29"]
    assert p["best_rank"] == 30
    assert p["sense_filtered_count"] == 1
    drop = p["sense_filtered"][0]
    assert drop["title"] == "重庆彭水发现失联中巴车残骸"
    assert drop["cue"] in drop["title"]


def test_term_presence_drop_count_is_complete_beyond_the_sample_cap():
    days = {"2026-07-27": [
        {"title": f"第{i}支救援队搜救失联游客", "rank": i + 1, "pinned": False}
        for i in range(5)]}
    p = term_presence("失联", days)
    assert p["appearances"] == 0
    assert p["sense_filtered_count"] == 5     # full count, always
    assert len(p["sense_filtered"]) == 3      # evidence sample, capped


def _day(titles_ranks):
    return [{"title": t, "rank": r, "pinned": False} for t, r in titles_ranks]


def test_withdrawal_candidates_flags_one_day_top_exit():
    days = {
        "2026-01-01": _day([("坚持的话题", 3), ("闪退话题", 2)]),
        "2026-01-02": _day([("坚持的话题", 5)]),
        "2026-01-03": _day([("坚持的话题", 8), ("末日首秀", 1)]),
    }
    from collectors.weibo_hotsearch import withdrawal_candidates
    got = withdrawal_candidates(days, top_rank=10, sensitive_terms={"闪退"})
    assert got["one_day_exits"] == 1                       # 闪退话题 only
    assert [c["title"] for c in got["candidates"]] == ["闪退话题"]
    assert got["candidates"][0]["matched_terms"] == ["闪退"]
    assert got["baseline_persist_rate"] == 0.5


def test_withdrawal_candidates_nonsensitive_exit_counted_not_named():
    days = {
        "2026-01-01": _day([("坚持的话题", 3), ("球赛话题", 2)]),
        "2026-01-02": _day([("坚持的话题", 5)]),
        "2026-01-03": _day([("坚持的话题", 8)]),
    }
    from collectors.weibo_hotsearch import withdrawal_candidates
    got = withdrawal_candidates(days, top_rank=10, sensitive_terms={"敏感"})
    assert got["one_day_exits"] == 1 and got["candidates"] == []


def test_withdrawal_candidates_short_window_warms_up():
    got = withdrawal_candidates({"2026-01-01": _day([("a", 1)])})
    assert got["candidates"] == [] and got["baseline_persist_rate"] is None


def test_withdrawal_candidates_sense_gates_ordinary_hits_and_records_them():
    # A trademark case reaching the top ranks for one day is normal board
    # churn, not a withdrawal of 维权: it must not be NAMED a candidate, and
    # the gate's work must ship as evidence rather than vanish.
    days = {
        "2026-01-01": _day([("坚持的话题", 3),
                            ("瑞幸泰国商标维权再胜创赔偿纪录", 2)]),
        "2026-01-02": _day([("坚持的话题", 5)]),
        "2026-01-03": _day([("坚持的话题", 8)]),
    }
    got = withdrawal_candidates(days, top_rank=10, sensitive_terms={"维权"})
    assert got["one_day_exits"] == 1
    assert got["candidates"] == []
    assert len(got["sense_filtered"]) == 1
    gated = got["sense_filtered"][0]
    assert gated["title"] == "瑞幸泰国商标维权再胜创赔偿纪录"
    assert gated["sense_filtered_terms"] == [{"term": "维权", "cue": "商标"}]


def test_withdrawal_candidates_keeps_the_coded_sense_exit():
    days = {
        "2026-01-01": _day([("坚持的话题", 3), ("维权律师被带走", 2)]),
        "2026-01-02": _day([("坚持的话题", 5)]),
        "2026-01-03": _day([("坚持的话题", 8)]),
    }
    got = withdrawal_candidates(days, top_rank=10, sensitive_terms={"维权"})
    assert [c["title"] for c in got["candidates"]] == ["维权律师被带走"]
    assert got["candidates"][0]["matched_terms"] == ["维权"]
    assert got["sense_filtered"] == []


def test_nepal_flood_casualty_update_is_not_a_withdrawal_candidate():
    days = {
        "2026-08-29": _day([
            ("持续话题", 3),
            ("尼泊尔山洪已致579死1924失联", 8),
        ]),
        "2026-08-30": _day([("持续话题", 5)]),
        "2026-08-31": _day([("持续话题", 8)]),
    }

    got = withdrawal_candidates(days, top_rank=10, sensitive_terms={"失联"})

    assert got["candidates"] == []
    assert got["sense_filtered"][0]["title"] == "尼泊尔山洪已致579死1924失联"
    assert got["sense_filtered"][0]["sense_filtered_terms"] == [
        {"term": "失联", "cue": "山洪"}
    ]


def test_withdrawal_baseline_states_its_pooling_bias():
    # No genre field exists in the archive (title and url only), so the
    # baseline cannot be stratified deterministically; the pooled rate must
    # therefore carry its own caveat and the direction of the bias.
    days = {
        "2026-01-01": _day([("坚持的话题", 3), ("闪退话题", 2)]),
        "2026-01-02": _day([("坚持的话题", 5)]),
        "2026-01-03": _day([("坚持的话题", 8)]),
    }
    got = withdrawal_candidates(days, top_rank=10)
    assert "genre" in got["baseline_note"]
    assert "missing withdrawals" in got["baseline_note"]
