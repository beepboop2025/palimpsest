"""Fail-closed tests for the public social-spread join desk."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import dragon_whispers, social_spread
from core.social_spread import (
    ALLOWED_TELEGRAM_HANDLES,
    DISCLAIMER,
    JOB_NAME,
    OFFICIAL_MISSING_WHISPER_REFUSAL,
    PUBLICATION_POLICY,
    RELATION,
    SAMPLE_ROW,
    SCHEMA_VERSION,
    build_social_spread,
    refuse_official_missing_whisper,
    validate_social_spread,
)
from scripts import social_spread_pull as pull


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _weibo(*titles: str, generated_at: str = "2026-08-20T06:00:00Z") -> dict:
    return {
        "generated_at": generated_at,
        "board_entries": len(titles),
        "pinned_headlines": [{"date": "2026-08-20", "pinned": list(titles)}],
        "gazetteer_breakthroughs": [],
        "withdrawal_watch": {"candidates": []},
    }


def _wire(*headlines: str) -> dict:
    events = []
    for index, headline in enumerate(headlines):
        event_id = f"event-{'ab' * 12}" if index == 0 else f"event-{'cd' * 12}"
        if index > 1:
            event_id = f"event-{(chr(ord('e') + index) * 2) * 12}"[: 6 + 24]
            event_id = "event-" + f"{index:02x}" * 12
        events.append(
            {
                "event_id": event_id,
                "headline": headline,
                "dek": f"Registered dek mentioning {headline}.",
                "published_at": "2026-08-20T05:00:00Z",
                "updated_at": "2026-08-20T05:00:00Z",
            }
        )
    return {"generated_at": "2026-08-20T06:00:00Z", "events": events}


def _telegram(text: str, handle: str = "DragonDenWhispers") -> dict:
    return {
        "generated_at": "2026-08-20T06:00:00Z",
        "observations": [
            {
                "title": f"[telegram:public] {handle}/12",
                "text": text,
                "channel_handle": handle,
                "first_seen": "2026-08-20T05:00:00Z",
            }
        ],
    }


def _official(title: str) -> dict:
    return {
        "generated_at": "2026-08-20T06:00:00Z",
        "pages": {
            "https://www.gov.cn/example": {
                "term": title,
                "last_confirmed_alive": "2026-08-20T04:00:00Z",
            }
        },
        "observations": [],
    }


def _inputs(**overrides):
    base = {
        "weibo-hotsearch": None,
        "weibo-hotsearch-terms": None,
        "public-board-terms": None,
        "public-hot-boards": None,
        "telegram-public-channels": None,
        "social-observations": None,
        "newswire": None,
        "news-wire-live": None,
        "official-first-seen": None,
        "public-deletion-ledgers": None,
        "wayback": None,
    }
    base.update(overrides)
    return base


def test_in_tree_sources_and_handles_were_not_invented():
    assert ALLOWED_TELEGRAM_HANDLES == {
        "DragonDenWhispers",
        "DragonDenCyber",
        "DragonDenBorderlands",
    }
    config = json.loads(
        (ROOT / "config" / "telegram_public_channels.json").read_text(encoding="utf-8")
    )
    assert [row["handle"] for row in config["channels"]] == sorted(
        ALLOWED_TELEGRAM_HANDLES
    ) or [row["handle"] for row in config["channels"]] == [
        "DragonDenWhispers",
        "DragonDenCyber",
        "DragonDenBorderlands",
    ]
    blob = json.dumps(config)
    assert "weixin" not in blob
    assert "WeChat" not in blob


def test_dragon_whispers_publication_policy_is_untouched():
    policy = dragon_whispers.empty_document("2026-08-20T06:00:00Z")["publication_policy"]
    assert policy == {
        "human_review_required": True,
        "raw_messages_included": False,
        "source_identifiers_included": False,
        "exact_iocs_included": False,
        "named_allegations_included": False,
        "counts_as_corroboration": False,
    }
    assert PUBLICATION_POLICY["dragon_whispers_reused"] is False
    src = (ROOT / "core" / "dragon_whispers.py").read_text(encoding="utf-8")
    assert '"named_allegations_included": False' in src
    assert "named_party" in src
    assert "accused" in src


def test_exact_refusal_text_for_official_missing_whisper_example():
    assert refuse_official_missing_whisper() == OFFICIAL_MISSING_WHISPER_REFUSAL
    assert refuse_official_missing_whisper() == (
        "A whisper-only report that a Chinese official is missing is not a "
        "Palimpsest claim. Palimpsest does not confirm a person is missing, "
        "detained, or dead."
    )
    assert DISCLAIMER in refuse_official_missing_whisper()


def test_whisper_only_official_missing_does_not_emit_a_person_package():
    document = build_social_spread(
        _inputs(
            **{
                "weibo-hotsearch": _weibo("杭州暴雨"),
                "telegram-public-channels": _telegram(
                    "A Chinese official is reported missing in whispers."
                ),
                "newswire": _wire("杭州暴雨"),
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    assert DISCLAIMER == document["disclaimer"]
    person_rows = [row for row in document["rows"] if row["names_a_person"]]
    assert person_rows == []
    assert any(
        refusal["reason"] == OFFICIAL_MISSING_WHISPER_REFUSAL
        for refusal in document["refusals"]
    )
    assert all("official" not in row["term"].casefold() for row in document["rows"])
    assert all("missing" not in row["term"].casefold() for row in document["rows"])


def test_whisper_only_name_without_boards_still_refuses_person_package():
    document = build_social_spread(
        _inputs(
            **{
                "weibo-hotsearch": _weibo("高考分数线"),
                "telegram-public-channels": _telegram("官员张某失联"),
                "newswire": _wire("高考分数线"),
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    assert not any(row["names_a_person"] for row in document["rows"])
    assert not any("张某" in row["term"] for row in document["rows"])
    assert any(
        refusal["reason"] == OFFICIAL_MISSING_WHISPER_REFUSAL
        for refusal in document["refusals"]
    )


def test_wire_and_hotsearch_match_emits_matched_or_circulating_row():
    document = build_social_spread(
        _inputs(
            **{
                "weibo-hotsearch": _weibo("杭州暴雨"),
                "newswire": _wire("杭州暴雨"),
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    assert document["status"] == "live"
    row = next(item for item in document["rows"] if item["term"] == "杭州暴雨")
    assert row["disposition"] in {"circulating-unverified", "matched-to-wire"}
    assert row["disposition"] == "matched-to-wire"
    assert row["matches"]["wire_event_ids"]
    assert row["relation"] == RELATION
    assert row["disclaimer"] == DISCLAIMER
    assert row["names_a_person"] is False
    assert row["automatic_publication"] is True
    assert row["spreading"]["source_ids"] == ["weibo-hotsearch"]
    assert row["spreading"]["n_surfaces"] == 1
    assert row["join_keys"]["term"] == "杭州暴雨"
    assert row["join_keys"]["board"] == "weibo"
    assert row["join_keys"]["host"] == "s.weibo.com"
    assert row["join_keys"]["first_seen"].startswith("2026-08-20")


def test_official_page_title_match_and_person_name_stays_review_gated():
    document = build_social_spread(
        _inputs(
            **{
                "weibo-hotsearch": _weibo("青年失业率"),
                "official-first-seen": _official("青年失业率"),
                "newswire": _wire("unrelated headline about trade"),
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    row = next(item for item in document["rows"] if item["term"] == "青年失业率")
    assert row["disposition"] == "matched-to-official-page"
    assert row["matches"]["official_page_last_alive"]
    assert row["automatic_publication"] is True


def test_missing_collectors_abstain():
    document = build_social_spread(
        _inputs(**{"newswire": _wire("杭州暴雨")}),
        generated_at="2026-08-20T06:00:00Z",
    )
    assert document["status"] == "abstain"
    assert document["rows"] == []
    assert document["n_rows"] == 0
    assert document["n_abstained"] >= 1
    assert document["input_status"]["weibo-hotsearch"] == "missing"
    assert document["input_status"]["public-hot-boards"] == "missing"
    assert document["news_story"] is None


def test_no_named_missing_claim_in_module_fixtures_or_sample_row():
    assert SAMPLE_ROW["term"] == "杭州暴雨"
    assert SAMPLE_ROW["disposition"] == "matched-to-wire"
    assert SAMPLE_ROW["names_a_person"] is False
    claim_fields = {
        key: value
        for key, value in SAMPLE_ROW.items()
        if key not in {"disclaimer"}
    }
    assert "missing" not in json.dumps(claim_fields, ensure_ascii=False).casefold()
    assert "失联" not in json.dumps(claim_fields, ensure_ascii=False)
    assert "失踪" not in json.dumps(claim_fields, ensure_ascii=False)
    assert SAMPLE_ROW["disclaimer"] == DISCLAIMER
    src = (ROOT / "core" / "social_spread.py").read_text(encoding="utf-8")
    assert "SAMPLE_ROW" in src
    assert DISCLAIMER in src
    test_src = Path(__file__).read_text(encoding="utf-8")
    assert "does not emit a person package" in test_src
    # Fixtures may mention the refusal example, but must not assert a finding.
    assert "Palimpsest " + "confirms" not in test_src
    assert "finding that" not in test_src.casefold() or "prohibited" in test_src


def test_person_status_on_boards_without_capture_is_refused_not_a_finding():
    document = build_social_spread(
        _inputs(
            **{
                "weibo-hotsearch": _weibo("张某失联"),
                "newswire": _wire("杭州暴雨"),
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    assert not any(row["term"] == "张某失联" for row in document["rows"])
    assert any("named-person-without-capture" in row["term_class"] for row in document["refusals"])
    assert all(DISCLAIMER in row["reason"] for row in document["refusals"])


def test_schema_job_name_and_sample_row_contract():
    assert JOB_NAME == "social-spread"
    assert SCHEMA_VERSION == "palimpsest-social-spread.v1"
    assert RELATION == "topic-surface-only"
    validate_social_spread(
        build_social_spread(
            _inputs(
                **{
                    "weibo-hotsearch": _weibo("杭州暴雨"),
                    "newswire": _wire("杭州暴雨"),
                }
            ),
            generated_at="2026-08-20T06:00:00Z",
        )
    )


def test_pull_writes_latest_and_history(tmp_path, monkeypatch):
    readings = tmp_path / "readings"
    readings.mkdir()
    (readings / "weibo-hotsearch-latest.json").write_text(
        json.dumps(_weibo("杭州暴雨"), ensure_ascii=False), encoding="utf-8"
    )
    (readings / "newswire-latest.json").write_text(
        json.dumps(_wire("杭州暴雨"), ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(pull, "OUT", readings / "social-spread-latest.json")
    monkeypatch.setattr(pull, "HIST", readings / "social-spread-history.jsonl")

    document = pull.main(
        readings_dir=readings,
        now=datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc),
    )
    assert document is not None
    assert (readings / "social-spread-latest.json").is_file()
    written = json.loads((readings / "social-spread-latest.json").read_text(encoding="utf-8"))
    assert written["job_name"] == "social-spread"
    assert written["n_rows"] >= 1
    assert DISCLAIMER == written["disclaimer"]
    history = (readings / "social-spread-history.jsonl").read_text(encoding="utf-8")
    assert "n_rows" in history


def test_pull_does_not_commit_a_repo_latest_file():
    assert not (ROOT / "readings" / "social-spread-latest.json").exists()


def test_no_wechat_and_no_new_telegram_handles_in_desk():
    src = (ROOT / "core" / "social_spread.py").read_text(encoding="utf-8")
    pull_src = (ROOT / "scripts" / "social_spread_pull.py").read_text(encoding="utf-8")
    assert "weixin" not in src
    assert "weixin" not in pull_src
    assert "WeChat" not in pull_src
    assert src.count("WeChat") == 1
    assert "No WeChat" in src
    for handle in ("DragonDenWhispers", "DragonDenCyber", "DragonDenBorderlands"):
        assert handle in src
    assert "DragonDenWhispersBot" not in src


def test_sense_gated_ordinary_shilian_accident_is_not_a_person_package():
    document = build_social_spread(
        _inputs(
            **{
                "weibo-hotsearch-terms": {
                    "generated_at": "2026-08-20T06:00:00Z",
                    "terms": [
                        {
                            "title": "重庆彭水发现失联中巴车残骸",
                            "first_seen": "2026-08-20",
                            "last_seen": "2026-08-20",
                        }
                    ],
                },
                "public-hot-boards": {
                    "generated_at": "2026-08-20T06:00:00Z",
                    "observations": [{"title": "杭州暴雨", "source": "public-hot-boards"}],
                },
                "newswire": _wire("杭州暴雨"),
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    row = next(
        item for item in document["rows"] if item["term"] == "重庆彭水发现失联中巴车残骸"
    )
    assert row["names_a_person"] is False
    assert row["automatic_publication"] is True
    assert row["disposition"] == "circulating-unverified"
    assert not any(item["names_a_person"] for item in document["rows"])


def test_weibo_terms_and_hot_boards_fold_into_one_join():
    document = build_social_spread(
        _inputs(
            **{
                "weibo-hotsearch-terms": {
                    "generated_at": "2026-08-20T06:00:00Z",
                    "terms": [{"title": "杭州暴雨", "first_seen": "2026-08-19"}],
                },
                "public-hot-boards": {
                    "generated_at": "2026-08-20T06:00:00Z",
                    "observations": [{"title": "杭州暴雨", "source": "public-hot-boards:baidu"}],
                },
                "public-board-terms": {
                    "generated_at": "2026-08-20T06:00:00Z",
                    "terms": [
                        {
                            "board": "zhihu",
                            "title": "杭州暴雨",
                            "first_seen": "2026-08-20",
                        }
                    ],
                },
                "newswire": _wire("杭州暴雨"),
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    rain = [item for item in document["rows"] if item["term"] == "杭州暴雨"]
    boards = {item["join_keys"]["board"] for item in rain}
    assert boards == {"weibo", "baidu", "zhihu"}
    weibo = next(item for item in rain if item["join_keys"]["board"] == "weibo")
    assert weibo["disposition"] == "matched-to-wire"
    assert weibo["join_keys"]["host"] == "s.weibo.com"
    assert weibo["join_keys"]["first_seen"].startswith("2026-08-19")
    assert "weibo-hotsearch-terms" in weibo["spreading"]["source_ids"]
    zhihu = next(item for item in rain if item["join_keys"]["board"] == "zhihu")
    assert zhihu["join_keys"]["host"] == "www.zhihu.com"
    assert "public-board-terms:zhihu" in zhihu["spreading"]["source_ids"]
    baidu = next(item for item in rain if item["join_keys"]["board"] == "baidu")
    assert "public-hot-boards:baidu" in baidu["spreading"]["source_ids"]


def test_weibo_zhihu_tieba_do_not_join_on_substring_or_wrong_day():
    contained = _wire("浙江杭州暴雨预警升级")
    stale = {
        "generated_at": "2026-08-20T06:00:00Z",
        "events": [
            {
                "event_id": "event-" + "ab" * 12,
                "headline": "杭州暴雨",
                "dek": "Registered dek.",
                "published_at": "2026-07-01T05:00:00Z",
                "updated_at": "2026-07-01T05:00:00Z",
            }
        ],
    }
    substring = build_social_spread(
        _inputs(
            **{
                "public-board-terms": {
                    "generated_at": "2026-08-20T06:00:00Z",
                    "terms": [
                        {
                            "board": "weibo",
                            "title": "杭州暴雨",
                            "first_seen": "2026-08-20",
                            "last_seen": "2026-08-20",
                            "best_rank": 2,
                        }
                    ],
                },
                "newswire": contained,
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    weibo = next(item for item in substring["rows"] if item["join_keys"]["board"] == "weibo")
    assert weibo["disposition"] == "circulating-unverified"
    assert weibo["matches"]["wire_event_ids"] == []

    window_miss = build_social_spread(
        _inputs(
            **{
                "public-board-terms": {
                    "generated_at": "2026-08-20T06:00:00Z",
                    "terms": [
                        {
                            "board": "zhihu",
                            "title": "杭州暴雨",
                            "first_seen": "2026-08-20",
                            "last_seen": "2026-08-20",
                            "best_rank": 4,
                        },
                        {
                            "board": "tieba",
                            "title": "杭州暴雨",
                            "first_seen": "2026-08-20",
                            "last_seen": "2026-08-20",
                            "best_rank": 5,
                        },
                    ],
                },
                "newswire": stale,
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    for board in ("zhihu", "tieba"):
        row = next(item for item in window_miss["rows"] if item["join_keys"]["board"] == board)
        assert row["disposition"] == "circulating-unverified"
        assert row["matches"]["wire_event_ids"] == []
        assert row["join_keys"]["term"] == "杭州暴雨"
        assert row["join_keys"]["rank"] >= 1


def test_exact_term_and_day_window_joins_registered_wire_only():
    document = build_social_spread(
        _inputs(
            **{
                "public-board-terms": {
                    "generated_at": "2026-08-20T06:00:00Z",
                    "terms": [
                        {
                            "board": "weibo",
                            "title": "杭州暴雨",
                            "first_seen": "2026-08-20",
                            "last_seen": "2026-08-20",
                            "best_rank": 3,
                        }
                    ],
                },
                "newswire": _wire("杭州暴雨"),
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    row = next(item for item in document["rows"] if item["term"] == "杭州暴雨")
    assert row["disposition"] == "matched-to-wire"
    assert row["join_keys"] == {
        "term": "杭州暴雨",
        "host": "s.weibo.com",
        "first_seen": "2026-08-20T00:00:00Z",
        "last_seen": "2026-08-20T00:00:00Z",
        "board": "weibo",
        "rank": 3,
    }
    assert row["names_a_person"] is False


def test_json_schema_accepts_a_live_document():
    jsonschema = pytest.importorskip("jsonschema")
    document = build_social_spread(
        _inputs(
            **{
                "weibo-hotsearch": _weibo("杭州暴雨"),
                "newswire": _wire("杭州暴雨"),
            }
        ),
        generated_at="2026-08-20T06:00:00Z",
    )
    schema = json.loads(
        (ROOT / "protocol" / "social-spread-v1.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(document)
