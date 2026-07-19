"""Weibo hot-search collector — parse, join, and regime-label tests (offline)."""
import json

from collectors.weibo_hotsearch import (
    collect_range, join_ddti, parse_day, pinned_series, term_presence)

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
