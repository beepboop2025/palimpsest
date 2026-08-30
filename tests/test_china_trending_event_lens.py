"""Automatic trend lenses must stay deterministic and evidence-bounded."""

from __future__ import annotations

from datetime import datetime, timezone

from core.china_trending_event_lens import (
    SCHEMA_VERSION,
    build_trending_event_lenses,
    headline_key,
)


NOW = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)


def _term(
    title: str,
    rank: int | None,
    first: str,
    last: str = "2026-08-30",
    *,
    days: int = 1,
) -> dict:
    return {
        "title": title,
        "best_rank": rank,
        "first_seen": first,
        "last_seen": last,
        "days_present": days,
        "appearances": days,
        "pinned": False,
    }


def _board(
    terms: list[dict],
    *,
    generated_at: str = "2026-08-30T09:00:00Z",
    pins: list[dict] | None = None,
    candidates: list[dict] | None = None,
    sense_filtered: list[dict] | None = None,
    ddti: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": "palimpsest-weibo-hotsearch-terms.v1",
        "status": "live",
        "generated_at": generated_at,
        "window_days": ["2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30"],
        "terms": terms,
        "pinned_headlines": pins or [],
        "withdrawal_watch": {
            "baseline_persist_rate": 0.24,
            "candidates": candidates or [],
            "sense_filtered": sense_filtered or [],
        },
        "ddti_join": ddti or [],
    }


def _events(board: dict, **kwargs) -> list[dict]:
    result = build_trending_event_lenses(board, evaluated_at=NOW, **kwargs)
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["status"] == "live"
    return result["events"]


def test_casualty_revisions_form_one_current_event_cluster():
    terms = [
        _term(
            "吉隆泥石流已致16人遇难546人失联",
            1,
            "2026-08-30",
        ),
        _term(
            "吉隆泥石流已致17人遇难600人失联",
            4,
            "2026-08-30",
        ),
        _term(
            "吉隆泥石流已致12人遇难400人失联",
            8,
            "2026-08-29",
            "2026-08-29",
        ),
    ]

    first = _events(_board(terms))[0]
    second = _events(_board(list(reversed(terms))))[0]

    assert headline_key(terms[0]["title"]) == headline_key(terms[1]["title"])
    assert first["trend_id"] == second["trend_id"]
    assert first["attention"]["latest_day_headlines"] == 2
    assert first["attention"]["distinct_headlines"] == 3
    assert first["attention"]["title_days"] == 3
    assert first["assessment"]["code"] == "visible_permitted_attention"
    assert "does not prove uncensored discussion" in first["assessment"]["reading"]


def test_similar_casualty_templates_do_not_merge_unrelated_places():
    events = _events(
        _board(
            [
                _term("吉隆泥石流已致16人遇难", 1, "2026-08-30"),
                _term("四川地震已致16人遇难", 2, "2026-08-30"),
                _term("吉隆口岸小邬警官确认平安", 3, "2026-08-30"),
            ]
        )
    )

    assert len(events) == 3
    assert {event["canonical_headline"] for event in events} == {
        "吉隆泥石流已致16人遇难",
        "四川地震已致16人遇难",
        "吉隆口岸小邬警官确认平安",
    }


def test_pin_and_ddti_are_separate_visible_signals():
    title = "某地工人维权活动持续"
    event = _events(
        _board(
            [_term(title, 5, "2026-08-29", days=2)],
            pins=[{"date": "2026-08-30", "pinned": [title]}],
            ddti=[
                {
                    "term": "维权",
                    "regime": "contained_visible",
                    "samples": [{"title": title}],
                }
            ],
        ),
        ddti_source_generated_at="2026-08-30T09:00:00Z",
    )[0]

    assert event["assessment"]["code"] == "visible_state_pinned_framing"
    assert event["attention"]["state_pins"]["days"] == ["2026-08-30"]
    assert event["ddti_corroboration"]["matches"] == 1
    assert event["ddti_corroboration"]["current_matches"] == 1
    assert "supports a containment review" in event["assessment"]["reading"]
    assert "does not establish cause or scale" in event["assessment"]["reading"]


def test_unclocked_ddti_overlap_cannot_set_a_current_label():
    title = "工人维权活动持续"
    result = build_trending_event_lenses(
        _board(
            [_term(title, 5, "2026-08-30")],
            ddti=[
                {
                    "term": "维权",
                    "regime": "contained_visible",
                    "samples": [{"title": title}],
                }
            ],
        ),
        evaluated_at=NOW,
    )
    event = result["events"][0]

    assert result["ddti_context"]["state"] == "unclocked"
    assert event["ddti_corroboration"]["matches"] == 1
    assert event["ddti_corroboration"]["current_matches"] == 0
    assert event["ddti_corroboration"]["state"] == "present_unclocked"
    assert event["assessment"]["code"] == "visible_permitted_attention"
    assert "fresh DDTI" not in event["assessment"]["reading"]


def test_later_numeric_revision_resolves_exact_title_exit():
    event = _events(
        _board(
            [
                _term(
                    "尼泊尔山洪已致579人遇难",
                    8,
                    "2026-08-29",
                    "2026-08-29",
                ),
                _term(
                    "尼泊尔山洪已致675人遇难",
                    2,
                    "2026-08-30",
                ),
            ],
            candidates=[
                {
                    "title": "尼泊尔山洪已致579人遇难",
                    "date": "2026-08-29",
                    "best_rank": 8,
                    "matched_terms": ["遇难"],
                }
            ],
        )
    )[0]

    assert event["withdrawal_watch"]["resolved_by_later_attention"] == 1
    assert event["withdrawal_watch"]["unresolved"] == 0
    assert "treated as revision churn" in event["assessment"]["reading"]


def test_current_exact_title_exit_stays_unconfirmed_not_a_takedown():
    title = "某敏感事件通报"
    event = _events(
        _board(
            [_term(title, 6, "2026-08-30")],
            candidates=[
                {
                    "title": title,
                    "date": "2026-08-30",
                    "best_rank": 6,
                    "matched_terms": ["敏感"],
                }
            ],
        )
    )[0]

    assert event["assessment"]["code"] == "visible_withdrawal_watch_unconfirmed"
    assert event["withdrawal_watch"]["unresolved"] == 1
    assert "not a takedown finding" in event["assessment"]["reading"]


def test_stale_or_abstaining_board_is_unavailable_not_a_quiet_reading():
    stale = _board(
        [_term("当前话题", 1, "2026-08-30")],
        generated_at="2026-08-29T00:00:00Z",
    )
    stale_result = build_trending_event_lenses(stale, evaluated_at=NOW)
    abstain = _board([])
    abstain["status"] = "abstain"
    abstain_result = build_trending_event_lenses(abstain, evaluated_at=NOW)

    assert stale_result["status"] == "unavailable"
    assert stale_result["clocks"]["freshness"] == "stale"
    assert stale_result["events"] == []
    assert abstain_result["status"] == "unavailable"
    assert "no quiet-state reading" in abstain_result["assessment"]["reading"]


def test_selection_is_ranked_deterministic_and_bounded():
    board = _board(
        [
            _term("丙国公布新的能源计划", 3, "2026-08-30"),
            _term("甲地教育改革正式发布", 1, "2026-08-30"),
            _term("乙市交通枢纽今天启用", 2, "2026-08-30"),
        ]
    )
    result = build_trending_event_lenses(board, evaluated_at=NOW, max_events=2)

    assert [event["attention"]["best_rank"] for event in result["events"]] == [1, 2]
    assert result["selection"]["current_clusters"] == 3
    assert result["selection"]["published_clusters"] == 2
    assert result["selection"]["truncated"] is True


def test_rankless_state_pin_is_not_buried_by_ranked_ordinary_trends():
    pin = "习近平出席峰会"
    result = build_trending_event_lenses(
        _board(
            [
                _term("普通高排名话题", 1, "2026-08-30"),
                _term(pin, None, "2026-08-30"),
            ],
            pins=[{"date": "2026-08-30", "pinned": [pin]}],
        ),
        evaluated_at=NOW,
        max_events=1,
    )

    assert len(result["events"]) == 1
    assert result["events"][0]["canonical_headline"] == pin
    assert result["events"][0]["assessment"]["code"] == "visible_state_pinned_framing"


def test_fresh_chinese_newswire_match_reports_independent_groups_only_as_context():
    title = "华为发布新一代芯片"
    wire = {
        "schema_version": "palimpsest-newswire.v1",
        "generated_at": "2026-08-30T09:30:00Z",
        "events": [
            {
                "event_id": "event-chip",
                "headline": "华为发布新一代芯片",
                "url": "https://palimpsest.info/news/wire/event-chip/",
                "evidence_strength": "multi-source",
                "evidence_groups": [
                    {"group_id": "publisher-a"},
                    {"group_id": "publisher-b"},
                ],
                "evidence_refs": [],
            },
            {
                "event_id": "event-english-only",
                "headline": "Huawei announces a different product",
                "url": "https://example.com/english",
                "evidence_strength": "single-source",
                "evidence_groups": [{"group_id": "publisher-c"}],
                "evidence_refs": [],
            },
        ],
    }
    result = build_trending_event_lenses(
        _board([_term(title, 1, "2026-08-30")]),
        wire,
        evaluated_at=NOW,
    )
    context = result["events"][0]["newswire_context"]

    assert result["newswire_context"]["state"] == "fresh"
    assert context["state"] == "present"
    assert context["matches"] == 1
    assert context["independent_publisher_groups"] == 2
    assert context["match_method"] == "chinese_headline_only"
    assert result["events"][0]["assessment"]["code"] == "visible_permitted_attention"


def test_missing_newswire_does_not_block_board_read_but_stays_explicit():
    result = build_trending_event_lenses(
        _board([_term("当前经济话题", 1, "2026-08-30")]),
        evaluated_at=NOW,
    )

    assert result["status"] == "live"
    assert result["newswire_context"]["state"] == "unavailable"
    assert result["events"][0]["newswire_context"]["state"] == "unavailable"
