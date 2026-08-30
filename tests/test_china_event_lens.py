"""Event-level censorship readings must preserve visibility and uncertainty."""

from __future__ import annotations

from datetime import datetime, timezone

from core.china_event_lens import build_declared_event_lenses


NOW = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)


def _term(title, rank, first, last=None, appearances=1):
    return {
        "title": title,
        "best_rank": rank,
        "first_seen": first,
        "last_seen": last or first,
        "appearances": appearances,
        "pinned": False,
    }


def _document(*, terms, candidates=None, sense_filtered=None, generated_at="2026-08-30T09:00:00Z"):
    return {
        "status": "live",
        "generated_at": generated_at,
        "window_days": [
            "2026-08-26",
            "2026-08-27",
            "2026-08-28",
            "2026-08-29",
            "2026-08-30",
        ],
        "terms": terms,
        "pinned_headlines": [
            {
                "date": "2026-08-28",
                "pinned": ["向吉隆泥石流灾害遇难人员默哀"],
            },
            {
                "date": "2026-08-29",
                "pinned": ["只要有一线希望就尽最大努力救援"],
            },
        ],
        "withdrawal_watch": {
            "baseline_persist_rate": 0.24,
            "candidates": candidates or [],
            "sense_filtered": sense_filtered or [],
        },
        "ddti_join": [],
    }


def _event(document):
    result = build_declared_event_lenses(document, evaluated_at=NOW)
    assert result["schema_version"] == "palimpsest.china-event-lenses.v1"
    assert len(result["events"]) == 1
    return result["events"][0]


def test_nepal_flood_is_visible_managed_attention_not_a_blackout():
    document = _document(
        terms=[
            _term("尼泊尔山洪", 1, "2026-08-29"),
            _term("尼泊尔山洪已致579死1924失联", 8, "2026-08-29"),
            _term("尼泊尔山洪已致675死2498失联", 2, "2026-08-30"),
            _term("尼泊尔泥石流2426人失联", 16, "2026-08-29", "2026-08-30", 2),
            _term("西藏吉隆泥石流", 10, "2026-08-29"),
            _term("吉隆泥石流已致16人遇难546人失联", 1, "2026-08-30"),
            _term("习近平出席峰会", 1, "2026-08-30"),
        ],
        candidates=[
            {
                "title": "尼泊尔山洪已致579死1924失联",
                "date": "2026-08-29",
                "best_rank": 8,
                "matched_terms": ["失联"],
            }
        ],
    )

    event = _event(document)

    assert event["finding_state"] == "bounded_observation"
    assert event["assessment"]["code"] == "visible_managed_attention"
    assert event["assessment"]["headline"] == "No topic-level blackout detected"
    assert event["attention"]["cross_border"]["distinct_headlines"] == 4
    assert event["attention"]["cross_border"]["title_days"] == 5
    assert event["attention"]["cross_border"]["best_rank"] == 1
    assert event["attention"]["cross_border"]["visible_on_latest_day"] is True
    assert event["attention"]["state_pins"]["days"] == ["2026-08-28"]
    assert event["withdrawal_watch"]["resolved_by_later_attention"] == 1
    assert event["withdrawal_watch"]["unresolved"] == 0
    assert "revision churn" in event["assessment"]["reading"]
    assert event["ddti_corroboration"]["state"] == "absent"


def test_disaster_sense_rejection_is_not_promoted_to_takedown_evidence():
    document = _document(
        terms=[
            _term("尼泊尔山洪484名游客失联", 7, "2026-08-27"),
            _term("尼泊尔山洪已致675死2498失联", 2, "2026-08-30"),
            _term("吉隆泥石流已致16人遇难546人失联", 1, "2026-08-30"),
        ],
        sense_filtered=[
            {
                "title": "尼泊尔山洪484名游客失联",
                "date": "2026-08-27",
                "best_rank": 7,
                "sense_filtered_terms": [{"term": "失联", "cue": "游客"}],
            }
        ],
    )

    event = _event(document)

    assert event["assessment"]["code"] == "visible_managed_attention"
    assert event["withdrawal_watch"]["ordinary_sense_rejections"] == 1
    assert event["withdrawal_watch"]["unresolved"] == 0
    assert "ordinary disaster context" in event["assessment"]["reading"]


def test_unresolved_exit_stays_a_warning_not_a_confirmed_censorship_claim():
    document = _document(
        terms=[
            _term("尼泊尔山洪已致579死1924失联", 8, "2026-08-29"),
            _term("吉隆泥石流救援", 2, "2026-08-30"),
        ],
        candidates=[
            {
                "title": "尼泊尔山洪已致579死1924失联",
                "date": "2026-08-29",
                "best_rank": 8,
                "matched_terms": ["失联"],
            }
        ],
    )

    event = _event(document)

    assert event["finding_state"] == "instrument_warning"
    assert event["assessment"]["code"] == "withdrawal_watch_unconfirmed"
    assert event["withdrawal_watch"]["unresolved"] == 1
    assert "not proof of a takedown" in event["assessment"]["reading"]


def test_stale_input_withholds_a_current_inference():
    document = _document(
        terms=[_term("尼泊尔山洪", 1, "2026-08-30")],
        generated_at="2026-08-29T00:00:00Z",
    )

    event = _event(document)

    assert event["finding_state"] == "unavailable"
    assert event["assessment"]["code"] == "unavailable"
    assert event["clocks"]["freshness"] == "stale"
    assert "withholds" in event["assessment"]["reading"]


def test_no_matching_title_is_bounded_absence_not_blackout():
    event = _event(
        _document(terms=[_term("上海电影节开幕", 3, "2026-08-30")])
    )

    assert event["finding_state"] == "bounded_absence"
    assert event["assessment"]["code"] == "not_observed"
    assert "cannot distinguish censorship" in event["assessment"]["reading"]


def test_abstaining_source_is_unavailable_not_calm():
    document = _document(terms=[])
    document["status"] = "abstain"

    event = _event(document)

    assert event["finding_state"] == "unavailable"
    assert event["attention"]["cross_border"]["distinct_headlines"] == 0
    assert event["assessment"]["headline"] == "No current censorship inference"
