"""Public Weibo hot-search BOARD dump — titles and ranks, never user firehose."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collectors.weibo_hotsearch import parse_day
from core.weibo_hotsearch_terms import (
    BOARD_DISCLOSURE,
    JOB_NAME,
    SAMPLE_TERM_ROW,
    SCHEMA_VERSION,
    build_weibo_hotsearch_terms,
    title_identity,
    validate_weibo_hotsearch_terms,
    write_weibo_hotsearch_terms,
)


DAY = json.dumps([
    {"url": "/weibo?q=%23a%23&Refer=new_time", "title": "向上向善造福人类"},
    {"url": "/weibo?q=%23b%23&t=31&band_rank=1&Refer=top", "title": "澎湖海战 撤档"},
    {"url": "/weibo?q=%23c%23&t=31&band_rank=7&Refer=top", "title": "杭州暴雨"},
    {"url": "/weibo?q=%23d%23&t=31&band_rank=3&Refer=top", "title": "澎湖海战 票房"},
])


def _window() -> dict[str, list[dict]]:
    parsed = parse_day(DAY)
    assert parsed is not None
    return {
        "2026-08-18": parsed,
        "2026-08-19": parsed,
        "2026-08-20": [
            {"title": "杭州暴雨", "rank": 2, "pinned": False},
            {"title": "重庆彭水发现失联中巴车残骸", "rank": 11, "pinned": False},
        ],
    }


def test_dump_contains_titles_and_ranks():
    document = build_weibo_hotsearch_terms(
        _window(),
        generated_at="2026-08-20T06:00:00Z",
        ddti_terms=[{"term": "澎湖海战", "threat": 0.4}, {"term": "白纸运动", "threat": 1.1}],
    )
    validate_weibo_hotsearch_terms(document)
    titles = {row["title"]: row for row in document["terms"]}
    assert "杭州暴雨" in titles
    assert titles["杭州暴雨"]["best_rank"] == 2
    assert titles["杭州暴雨"]["appearances"] == 3
    assert titles["杭州暴雨"]["days_present"] == 3
    assert titles["杭州暴雨"]["first_seen"] == "2026-08-18"
    assert titles["杭州暴雨"]["last_seen"] == "2026-08-20"
    assert titles["杭州暴雨"]["title_sha256"] == title_identity("杭州暴雨")
    assert titles["向上向善造福人类"]["pinned"] is True
    assert titles["向上向善造福人类"]["best_rank"] is None
    assert document["pinned_headlines"]
    assert document["disclaimer"] == BOARD_DISCLOSURE
    assert document["publication_policy"]["automatic_publication"] is True
    assert document["publication_policy"]["named_person_packages_auto_published"] is False


def test_dump_has_no_user_ids_or_firehose_fields():
    document = build_weibo_hotsearch_terms(
        _window(), generated_at="2026-08-20T06:00:00Z"
    )
    term_blob = json.dumps(document["terms"], ensure_ascii=False)
    for forbidden in ("uid", "user_id", "weibo_uid", "followers", "mid", "mblog"):
        assert forbidden not in term_blob
    assert document["publication_policy"]["user_ids_included"] is False
    assert "No Weibo user ids" in document["scope"]
    src = Path(__file__).resolve().parent.parent.joinpath(
        "core", "weibo_hotsearch_terms.py"
    ).read_text(encoding="utf-8")
    assert "not uncensored public opinion" in src


def test_suppressed_invisible_still_requires_a_ddti_term():
    document = build_weibo_hotsearch_terms(
        _window(),
        generated_at="2026-08-20T06:00:00Z",
        ddti_terms=[{"term": "白纸运动", "threat": 1.1}],
    )
    regimes = {row["term"]: row["regime"] for row in document["ddti_join"]}
    assert regimes["白纸运动"] == "suppressed_invisible"
    assert all(row["title"] != "白纸运动" for row in document["terms"])
    rain = next(row for row in document["terms"] if row["title"] == "杭州暴雨")
    assert "regime" not in rain
    empty = build_weibo_hotsearch_terms(
        _window(), generated_at="2026-08-20T06:00:00Z", ddti_terms=[]
    )
    assert empty["ddti_join"] == []
    assert empty["regimes"]["suppressed_invisible"] == 0


def test_sample_term_row_is_anodyne():
    assert SAMPLE_TERM_ROW["title"] == "杭州暴雨"
    assert "失联" not in SAMPLE_TERM_ROW["title"]
    assert "missing" not in json.dumps(SAMPLE_TERM_ROW).casefold()
    assert JOB_NAME == "weibo-hotsearch-terms"
    assert SCHEMA_VERSION == "palimpsest-weibo-hotsearch-terms.v1"


def test_missing_archive_abstains_and_write_skips_latest(tmp_path):
    document = build_weibo_hotsearch_terms(None, generated_at="2026-08-20T06:00:00Z")
    assert document["status"] == "abstain"
    assert document["terms"] == []
    assert write_weibo_hotsearch_terms(
        {}, generated_at="2026-08-20T06:00:00Z", readings=tmp_path
    ) is None
    assert not (tmp_path / "weibo-hotsearch-terms-latest.json").exists()


def test_write_publishes_latest_and_history(tmp_path):
    document = write_weibo_hotsearch_terms(
        _window(),
        generated_at="2026-08-20T06:00:00Z",
        readings=tmp_path,
        ddti_terms=[{"term": "白纸运动", "threat": 1.0}],
    )
    assert document is not None
    written = json.loads((tmp_path / "weibo-hotsearch-terms-latest.json").read_text())
    assert written["n_titles"] == document["n_titles"]
    history = (tmp_path / "weibo-hotsearch-terms-history.jsonl").read_text()
    assert "n_titles" in history


def test_json_schema_accepts_a_live_dump():
    jsonschema = pytest.importorskip("jsonschema")
    document = build_weibo_hotsearch_terms(
        _window(),
        generated_at="2026-08-20T06:00:00Z",
        ddti_terms=[{"term": "白纸运动", "threat": 1.0}],
    )
    schema = json.loads(
        Path(__file__).resolve().parent.parent.joinpath(
            "protocol", "weibo-hotsearch-terms-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(document)


def test_repo_commits_a_measured_terms_latest_not_a_fixture():
    path = Path(__file__).resolve().parent.parent.joinpath(
        "readings", "weibo-hotsearch-terms-latest.json"
    )
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["schema_version"] == SCHEMA_VERSION
    assert document["job_name"] == JOB_NAME
    assert document["status"] == "live"
    assert document["generated_at"].endswith("Z")
    assert document["window_days"]
    assert document["n_titles"] == len(document["terms"])
    assert document["n_titles"] > 0
    assert all(row.get("title") and row.get("title_sha256") for row in document["terms"])
