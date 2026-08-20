"""Offline tests for the fused public-board term dump."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collectors.public_board_archives import (
    archive_login_walled,
    collect_archives,
    load_catalog,
    parse_freewechat_index,
    parse_markdown_titles,
    parse_wikipedia_mostviewed,
)
from core.public_board_terms import (
    BOARD_DISCLOSURE,
    JOB_NAME,
    SAMPLE_TERM_ROW,
    SCHEMA_VERSION,
    build_public_board_terms,
    title_identity,
    validate_public_board_terms,
    write_public_board_terms,
)


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "public_board_archives"
DATES = ["2026-08-19", "2026-08-20"]


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fetch(url: str, *, freewechat: str = "login") -> tuple[int, str]:
    if "justjavac/weibo-trending-hot-search" in url and "2026-08-20" in url:
        return 200, _read("justjavac-2026-08-20.json")
    if "lonnyzhang423/weibo-hot-hub" in url and "2026-08-20" in url:
        return 200, _read("weibo-hot-hub-2026-08-20.md")
    if "lonnyzhang423/weibo-hot-hub" in url:
        return 200, _read("weibo-hot-hub-empty.md")
    if "lonnyzhang423/zhihu-hot-hub" in url:
        return 200, _read("zhihu-hot-hub.md")
    if "lonnyzhang423/toutiao-hot-hub" in url:
        return 200, _read("weibo-hot-hub-empty.md")
    if "lonnyzhang423/douyin-hot-hub" in url:
        return 200, _read("douyin-hot-hub.md")
    if "hot_searches_for_apps" in url and "%E5%BE%AE%E5%8D%9A" in url and "2026-08-20" in url:
        return 200, _read("iiecho1-weibo.md")
    if "hot_searches_for_apps" in url:
        return 404, ""
    if "list=mostviewed" in url:
        return 200, _read("wiki-mostviewed.json")
    if url.rstrip("/").endswith("freewechat.com/feed"):
        return 404, ""
    if "freewechat.com" in url:
        name = "freewechat-login.html" if freewechat == "login" else "freewechat-titles.html"
        return 200, _read(name)
    if "passport" in url or "sso.toutiao.com" in url:
        return 200, "<html>sso.toutiao.com 请登录</html>"
    return 404, ""


def _collected(**fetch_kw):
    return collect_archives(
        dates=DATES,
        fetch=lambda url: _fetch(url, **fetch_kw),
        extra_readings={
            "public-hot-boards": {
                "generated_at": "2026-08-20T06:00:00Z",
                "boards": [
                    {
                        "name": "baidu_realtime",
                        "kind": "baidu",
                        "url": "https://top.baidu.com/api/board?platform=wise&tab=realtime",
                        "http_status": 200,
                        "n_items": 1,
                        "status": "ok",
                        "note": "live",
                    },
                    {
                        "name": "toutiao_hot_board",
                        "kind": "toutiao",
                        "url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
                        "http_status": 200,
                        "n_items": 0,
                        "status": "login_walled",
                        "note": "sso",
                    },
                ],
                "observations": [
                    {
                        "title": "杭州暴雨",
                        "source": "public-hot-boards:baidu",
                        "provenance": {"rank": 1},
                    }
                ],
            },
            "wikipedia-gazetteer-rc": {
                "generated_at": "2026-08-20T05:00:00Z",
                "observations": [{"title": "白纸运动"}],
            },
        },
    )


def test_archive_ingest_works_offline_from_fixtures():
    collected = _collected()
    document = build_public_board_terms(
        collected,
        generated_at="2026-08-20T06:00:00Z",
        ddti_terms=[{"term": "白纸运动"}, {"term": "杭州暴雨"}],
    )
    validate_public_board_terms(document)
    titles = {(row["board"], row["title"]) for row in document["terms"]}
    assert ("weibo", "杭州暴雨") in titles
    assert ("douyin", "杭州暴雨") in titles
    assert ("zhihu", "杭州暴雨为何夜间加重") in titles
    assert ("zh-wikipedia", "杭州") in titles
    assert ("zh-wikipedia", "白纸运动") in titles
    assert document["disclaimer"] == BOARD_DISCLOSURE
    assert document["n_boards_ok"] >= 1


def test_markdown_is_not_a_login_wall():
    markdown = _read("weibo-hot-hub-2026-08-20.md")
    assert archive_login_walled(200, markdown) is False
    assert archive_login_walled(200, _read("freewechat-login.html")) is True
    assert archive_login_walled(200, _read("freewechat-titles.html")) is False
    titles = parse_freewechat_index(200, _read("freewechat-titles.html"))
    assert titles and titles[0]["title"] == "杭州暴雨公益提醒"


def test_login_walled_board_abstains_and_is_never_a_zero():
    collected = _collected(freewechat="login")
    statuses = {row["name"]: row["status"] for row in collected["boards"]}
    assert statuses["freewechat-public"] == "login_walled"
    assert statuses["lonnyzhang423-weibo-hot-hub"] == "ok"
    freewechat = next(row for row in collected["boards"] if row["name"] == "freewechat-public")
    assert freewechat["n_items"] == 0
    document = build_public_board_terms(collected, generated_at="2026-08-20T06:00:00Z")
    assert document["status"] == "live"
    assert any(row["name"] == "freewechat-public" for row in document["abstained"])
    assert all(row["status"] != "ok" or row["n_items"] > 0 for row in document["boards"])


def test_login_walled_only_dump_abstains(tmp_path):
    def fetch(url: str):
        return 200, _read("freewechat-login.html")

    collected = collect_archives(
        dates=DATES,
        fetch=fetch,
        catalog={
            "wired": [],
            "candidates": [
                {
                    "name": "freewechat-public",
                    "board": "freewechat",
                    "role": "recovered-listing",
                    "urls": ["https://freewechat.com/"],
                    "note": "test",
                }
            ],
            "skipped": [],
        },
    )
    assert collected["n_boards_ok"] == 0
    assert collected["n_sightings"] == 0
    written = write_public_board_terms(
        collected,
        generated_at="2026-08-20T06:00:00Z",
        readings=tmp_path,
    )
    assert written is None
    assert not (tmp_path / "public-board-terms-latest.json").exists()


def test_no_user_ids_or_firehose_fields():
    document = build_public_board_terms(_collected(), generated_at="2026-08-20T06:00:00Z")
    term_blob = json.dumps(document["terms"], ensure_ascii=False)
    for forbidden in ("uid", "user_id", "sec_uid", "follower", "1991933892508967"):
        assert forbidden not in term_blob
    assert document["publication_policy"]["user_ids_included"] is False
    titles = {row["title"] for row in document["terms"]}
    assert "冯巩" not in titles
    assert "a video with an author id" not in titles
    wiki = next(row for row in document["terms"] if row["title"] == "杭州")
    assert "count" not in wiki
    assert wiki["best_rank"] == 1


def test_weibo_justjavac_and_hot_hub_do_not_duplicate_identity():
    collected = _collected()
    weibo_rain = [
        row
        for row in collected["sightings"]
        if row["board"] == "weibo" and row["title"] == "杭州暴雨" and row["date"] == "2026-08-20"
    ]
    assert len(weibo_rain) == 1
    archives = set(weibo_rain[0]["source_archives"])
    assert "justjavac-weibo" in archives
    assert "lonnyzhang423-weibo-hot-hub" in archives
    assert "iiecho1-hot-searches:weibo" in archives
    document = build_public_board_terms(collected, generated_at="2026-08-20T06:00:00Z")
    rows = [row for row in document["terms"] if row["board"] == "weibo" and row["title"] == "杭州暴雨"]
    assert len(rows) == 1
    assert rows[0]["appearances"] == 1
    assert rows[0]["best_rank"] == 2


def test_empty_markdown_is_silent_not_a_zero():
    items = parse_markdown_titles(_read("weibo-hot-hub-empty.md"), sections=["热门搜索"])
    assert items is None
    collected = _collected()
    empty_day = next(
        row for row in collected["boards"] if row["name"] == "lonnyzhang423-toutiao-hot-hub"
    )
    assert empty_day["status"] == "silent"
    assert empty_day["n_items"] == 0


def test_360doc_and_sports_are_skipped():
    catalog = load_catalog()
    skipped = {row["name"] for row in catalog["skipped"]}
    assert "360doc" in skipped
    assert "懂球帝" in skipped
    assert "虎扑" in skipped
    collected = _collected()
    document = build_public_board_terms(collected, generated_at="2026-08-20T06:00:00Z")
    names = {row["name"] for row in document["skipped"]}
    assert "360doc" in names
    iiecho_boards = {
        row["zh"] for row in next(
            src["boards"] for src in catalog["wired"] if src["name"] == "iiecho1-hot-searches"
        )
    }
    assert "360doc" not in iiecho_boards
    assert "懂球帝" not in iiecho_boards


def test_wikipedia_mostviewed_drops_talk_and_counts():
    rows = parse_wikipedia_mostviewed(json.loads(_read("wiki-mostviewed.json")))
    assert [row["title"] for row in rows] == ["杭州", "中国"]
    assert all("count" not in row for row in rows)


def test_sample_term_row_is_anodyne():
    assert SAMPLE_TERM_ROW["title"] == "杭州暴雨"
    assert SAMPLE_TERM_ROW["board"] == "baidu"
    assert SAMPLE_TERM_ROW["title_sha256"] == title_identity("杭州暴雨")
    assert "missing" not in SAMPLE_TERM_ROW["title"]
    assert JOB_NAME == "public-board-terms"
    assert SCHEMA_VERSION == "palimpsest-public-board-terms.v1"


def test_suppressed_invisible_still_requires_a_ddti_term():
    document = build_public_board_terms(
        _collected(),
        generated_at="2026-08-20T06:00:00Z",
        ddti_terms=[{"term": "白纸运动"}, {"term": "不存在的敏感词"}],
    )
    suppressed = [row for row in document["ddti_join"] if row["regime"] == "suppressed_invisible"]
    assert suppressed
    assert all(row["term"] for row in suppressed)
    contained = [row for row in document["ddti_join"] if row["regime"] == "contained_visible"]
    assert any(row["term"] == "白纸运动" for row in contained)


def test_write_publishes_latest_and_history(tmp_path):
    written = write_public_board_terms(
        _collected(),
        generated_at="2026-08-20T06:00:00Z",
        readings=tmp_path,
    )
    assert written is not None
    latest = json.loads((tmp_path / "public-board-terms-latest.json").read_text())
    assert latest["n_titles"] == written["n_titles"]
    history = (tmp_path / "public-board-terms-history.jsonl").read_text()
    assert "n_titles" in history


def test_json_schema_accepts_a_live_dump():
    jsonschema = pytest.importorskip("jsonschema")
    document = build_public_board_terms(_collected(), generated_at="2026-08-20T06:00:00Z")
    schema = json.loads(
        (ROOT / "protocol" / "public-board-terms-v1.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(document)


def test_repo_does_not_commit_a_fake_terms_latest():
    assert not (ROOT / "readings" / "public-board-terms-latest.json").exists()


def test_catalog_licenses_were_verified():
    catalog = load_catalog()
    wired = {row["name"]: row for row in catalog["wired"]}
    assert wired["justjavac-weibo"]["license"] == "MIT"
    assert wired["lonnyzhang423-weibo-hot-hub"]["license"] == "MIT"
    assert wired["lonnyzhang423-zhihu-hot-hub"]["license"] == "MIT"
    assert wired["lonnyzhang423-toutiao-hot-hub"]["license"] == "MIT"
    assert wired["lonnyzhang423-douyin-hot-hub"]["license"] == "MIT"
    assert wired["iiecho1-hot-searches"]["license"] == "unspecified-public-github-markdown"
    assert any(row["name"] == "freewechat-public" for row in catalog["candidates"])
