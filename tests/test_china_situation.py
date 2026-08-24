"""Contract tests for the three-layer China situation synthesis."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import (
    china_situation,
    dragon_whispers,
    event_analysis,
    newswire,
    social_observations,
)


ROOT = Path(__file__).resolve().parent.parent
_SOCIAL_SOURCE_BY_WIRE_SOURCE = {
    "cecc": "cecc-instagram",
    "cgtn-china": "cgtn-telegram",
    "chrd": "chrd-instagram",
    "dw-chinese": "dw-chinese-instagram",
    "global-voices-china": "global-voices-instagram",
    "new-bloom": "new-bloom-instagram",
    "pandaily": "pandaily-instagram",
    "rthk-finance": "rthk-instagram",
    "rthk-greater-china": "rthk-instagram",
}


def _event_and_social_reference(wire, analyses, scope_status, *, social_platform=None):
    for event in wire["events"]:
        if analyses[event["event_id"]]["scope_status"] != scope_status:
            continue
        for reference in event["evidence_refs"]:
            social_source_id = _SOCIAL_SOURCE_BY_WIRE_SOURCE.get(reference["source_id"])
            if social_source_id is None:
                continue
            platform = (
                "telegram" if social_source_id == "cgtn-telegram" else "instagram"
            )
            if social_platform is None or platform == social_platform:
                return event, reference
    raise AssertionError(f"fixture has no {scope_status} event with a social source")


def _social_record(reference, *, observed_at, title="China publisher social notice"):
    source_id = _SOCIAL_SOURCE_BY_WIRE_SOURCE[reference["source_id"]]
    return {
        "source_id": source_id,
        "native_id": f"fixture-{reference['item_id']}",
        "permalink": (
            "https://t.me/fixture_public/1/"
            if source_id == "cgtn-telegram"
            else "https://www.instagram.com/p/SITUATION_TEST_1/"
        ),
        "published_at": reference["published_at"],
        "observed_at": observed_at,
        "title": title,
        "excerpt": "Bounded exact-link publisher context.",
        "content_type": "image",
        "content_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
        "state": "published",
        "china_relevance_labels": ["china"],
        "related_urls": [reference["url"]],
    }


def _minutes_after(timestamp: str, minutes: int = 1) -> str:
    parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (parsed + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _protocol_validator(schema_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    resources = []
    for path in (ROOT / "protocol").glob("*.schema.json"):
        document = json.loads(path.read_text())
        schema_id = document.get("$id")
        if schema_id:
            resources.append((schema_id, referencing.Resource.from_contents(document)))
    registry = referencing.Registry().with_resources(resources)
    schema = json.loads(schema_path.read_text())
    return jsonschema.Draft202012Validator(schema, registry=registry)


def _social_receipts(registry, successful_source):
    return [
        {
            "source_id": source.id,
            "status": "success" if source.id == successful_source else "not-attempted",
            "rejected": 0,
            "error_code": None,
        }
        for source in registry.sources
    ]


def _synthetic_cgtn_wire():
    source = replace(
        next(
            source
            for source in newswire.load_source_registry().sources
            if source.id == "cgtn-china"
        ),
        declared_scan_ids=(),
        declared_economic_ids=(),
    )
    registry = newswire.SourceRegistry(
        schema_version=newswire.REGISTRY_SCHEMA_VERSION,
        window_hours=168,
        max_items_per_source=128,
        max_events=2048,
        sources=(source,),
        sha256="0" * 64,
    )
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><item>'
        '<title>China publisher bulletin</title>'
        '<link>https://news.cgtn.com/news/2026-08-25/china-publisher-bulletin</link>'
        '<description>A bounded China report.</description>'
        '<pubDate>Tue, 25 Aug 2026 10:00:00 +0000</pubDate>'
        '</item></channel></rss>'
    ).encode()
    return newswire.collect_newswire(
        registry,
        lambda _url, **_kwargs: rss,
        now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )


@pytest.fixture(scope="module")
def inputs():
    wire = json.loads((ROOT / "readings/newswire-latest.json").read_text())
    feed = json.loads((ROOT / "readings/newsroom-latest.json").read_text())
    analyses = event_analysis.build_event_analyses(wire, feed)
    return wire, feed, analyses


@pytest.fixture(scope="module")
def situation(inputs):
    wire, _feed, analyses = inputs
    return china_situation.build_china_situation(wire, analyses)


def test_every_in_scope_event_gets_one_situation_without_strengthening(
    inputs, situation
):
    wire, _feed, analyses = inputs
    expected = {
        event_id
        for event_id, analysis in analyses.items()
        if analysis["scope_status"] == "in-scope"
    }

    assert {row["event_id"] for row in situation["situations"]} == expected
    assert situation["coverage"]["in_scope_events"] == len(expected)
    assert situation["inputs"]["social_status"] == "not-configured"
    assert situation["inputs"]["telegram_status"] == "not-configured"
    assert situation["reviewed_telegram"] == []
    assert situation["coverage"]["social_observations"] == 0
    assert situation["coverage"]["situations_with_three_layers"] == 0
    assert "events_with_osint_context" in situation["coverage"]
    assert "osint_context_rows" in situation["coverage"]
    assert all("osint_context" in row for row in situation["situations"])
    assert all("interconnection" in row for row in situation["situations"])
    assert "events_with_interconnection" in situation["coverage"]
    assert "interconnection_joined_rows" in situation["coverage"]
    order = [
        (row["published_at"], row["updated_at"], row["situation_id"])
        for row in situation["situations"]
    ]
    assert order == sorted(order, reverse=True)

    events = {event["event_id"]: event for event in wire["events"]}
    for row in situation["situations"]:
        event = events[row["event_id"]]
        assert row["reporting"]["evidence_strength"] == event["evidence_strength"]
        assert row["reporting"]["independent_groups"] == len(event["evidence_groups"])
        assert row["social_context"] == []
        assert "do not increase that count" in row["synthesis"]["summary"]
        assert "official-page coverage is" in row["synthesis"]["summary"]
        assert "archive-news-context" in row["synthesis"]["summary"]


def test_reviewed_telegram_empty_state_is_bound_without_event_guessing(inputs):
    wire, _feed, analyses = inputs
    reviewed = dragon_whispers.empty_document("2026-08-15T05:00:00Z")
    document = china_situation.build_china_situation(
        wire,
        analyses,
        reviewed_telegram=reviewed,
    )

    assert document["inputs"]["telegram_status"] == "awaiting-review"
    assert document["inputs"]["telegram_generated_at"] == "2026-08-15T05:00:00Z"
    assert document["inputs"]["telegram_sha256"]
    assert document["coverage"]["reviewed_telegram_signals"] == 0
    assert document["reviewed_telegram"] == []
    assert all("telegram_context" not in row for row in document["situations"])


def test_social_url_index_excludes_outside_remit_events(inputs):
    wire, _feed, analyses = inputs
    outside_event, reference = _event_and_social_reference(
        wire, analyses, "outside-remit"
    )
    registry = social_observations.load_source_registry()
    fixture_time = _minutes_after(reference["published_at"])
    record = _social_record(reference, observed_at=fixture_time)
    social, _ledger = social_observations.build_latest(
        [record],
        registry=registry,
        generated_at=fixture_time,
        collection_receipts=_social_receipts(registry, record["source_id"]),
    )

    document = china_situation.build_china_situation(wire, analyses, social=social)
    assert document["coverage"]["social_observations"] == 1
    assert document["coverage"]["social_observations_linked"] == 0
    assert document["coverage"]["social_observations_unmatched"] == 1
    assert outside_event["event_id"] not in {
        row["event_id"] for row in document["situations"]
    }
    assert all(not row["social_context"] for row in document["situations"])


def test_cgtn_rss_and_telegram_keep_one_publisher_lineage():
    wire = _synthetic_cgtn_wire()
    analyses = event_analysis.build_event_analyses(
        wire, {"schema_version": "palimpsest-news.v1", "stories": []}
    )
    event = wire["events"][0]
    reference = event["evidence_refs"][0]
    registry = social_observations.load_source_registry()
    social_source = next(
        source for source in registry.sources if source.id == "cgtn-telegram"
    )
    assert reference["independence_group"] == social_source.independence_group
    assert social_source.platform == "telegram"
    fixture_time = _minutes_after(reference["published_at"])
    record = _social_record(reference, observed_at=fixture_time)
    social, _ledger = social_observations.build_latest(
        [record],
        registry=registry,
        generated_at=fixture_time,
        collection_receipts=_social_receipts(registry, "cgtn-telegram"),
    )

    document = china_situation.build_china_situation(wire, analyses, social=social)
    row = next(
        item for item in document["situations"] if item["event_id"] == event["event_id"]
    )
    context = row["social_context"][0]
    assert reference["independence_group"] == "china-media-group-state-media"
    assert context["independence_group"] == "china-media-group-state-media"
    assert context["same_publisher_lineage"] is True
    assert row["reporting"]["independent_groups"] == len(event["evidence_groups"])

    forged = copy.deepcopy(document)
    forged_row = next(
        item for item in forged["situations"] if item["event_id"] == event["event_id"]
    )
    forged_row["social_context"][0]["same_publisher_lineage"] = False
    version_payload = {
        key: value
        for key, value in forged_row.items()
        if key not in {"situation_id", "version_id", "url"}
    }
    forged_row["version_id"] = china_situation._stable_id(
        "situationv", version_payload
    )
    with pytest.raises(china_situation.ChinaSituationError, match="publisher groups"):
        china_situation.validate_china_situation(forged)


def test_instagram_edit_state_and_observation_time_advance_situation(inputs):
    wire, _feed, analyses = inputs
    event, reference = _event_and_social_reference(
        wire, analyses, "in-scope", social_platform="instagram"
    )
    registry = social_observations.load_source_registry()
    first_time = _minutes_after(reference["published_at"])
    edited_time = _minutes_after(first_time)
    original = _social_record(reference, observed_at=first_time)
    receipts = _social_receipts(registry, original["source_id"])
    first_latest, first_ledger = social_observations.build_latest(
        [original],
        registry=registry,
        generated_at=first_time,
        collection_receipts=receipts,
    )
    changed = _social_record(
        reference,
        observed_at=edited_time,
        title="China publisher social notice — corrected",
    )
    edited_latest, _edited_ledger = social_observations.build_latest(
        [changed],
        registry=registry,
        generated_at=edited_time,
        prior_latest=first_latest,
        prior_ledger=first_ledger,
        collection_receipts=receipts,
    )

    document = china_situation.build_china_situation(
        wire, analyses, social=edited_latest
    )
    row = next(
        item for item in document["situations"] if item["event_id"] == event["event_id"]
    )
    assert row["social_context"][0]["state"] == "edited"
    assert row["updated_at"] == edited_time


def test_measurements_are_exact_event_analysis_context_and_remain_topic_only(
    inputs, situation
):
    _wire, _feed, analyses = inputs
    rows = {row["event_id"]: row for row in situation["situations"]}

    assert situation["coverage"]["events_with_measurement_context"] > 0
    assert situation["coverage"]["measurement_context_rows"] > 0
    for event_id, row in rows.items():
        expected = analyses[event_id]["collector_context"]
        assert [item["signal_id"] for item in row["measurement_context"]] == [
            item["signal_id"] for item in expected
        ]
        for measurement in row["measurement_context"]:
            assert measurement["relation"] == "topic-surface-only"
            assert measurement["story_url"].startswith("https://palimpsest.info/")
            assert measurement["evidence_url"].startswith("https://palimpsest.info/")


def test_situation_is_deterministic_and_changes_with_bound_measurement(inputs):
    wire, feed, analyses = inputs
    first = china_situation.build_china_situation(wire, analyses)
    second = china_situation.build_china_situation(
        copy.deepcopy(wire), copy.deepcopy(analyses)
    )
    assert first == second

    event = next(
        row for row in wire["events"] if analyses[row["event_id"]]["collector_context"]
    )
    signal_id = analyses[event["event_id"]]["collector_context"][0]["signal_id"]
    changed_feed = copy.deepcopy(feed)
    story = next(
        item for item in changed_feed["stories"] if item["signal_id"] == signal_id
    )
    story["claims"][0]["statement"] += " Revised normalized measurement."
    story["claim_fingerprint"] = "sha256:" + "b" * 64
    changed_analyses = event_analysis.build_event_analyses(wire, changed_feed)
    changed = china_situation.build_china_situation(wire, changed_analyses)

    by_event = {row["event_id"]: row for row in first["situations"]}
    changed_by_event = {row["event_id"]: row for row in changed["situations"]}
    assert (
        changed_by_event[event["event_id"]]["version_id"]
        != by_event[event["event_id"]]["version_id"]
    )
    assert changed["inputs"]["analysis_sha256"] != first["inputs"]["analysis_sha256"]


def test_presentation_urls_bind_to_the_page_that_contains_each_anchor(situation):
    bound = china_situation.bind_situation_page_urls(situation, page_size=2)

    assert bound["situations"][0]["url"].endswith(
        f"/news/china/situation/#{bound['situations'][0]['situation_id']}"
    )
    assert bound["situations"][1]["url"].endswith(
        f"/news/china/situation/#{bound['situations'][1]['situation_id']}"
    )
    assert bound["situations"][2]["url"].endswith(
        f"/news/china/situation/page/2/#{bound['situations'][2]['situation_id']}"
    )
    assert [row["version_id"] for row in bound["situations"]] == [
        row["version_id"] for row in situation["situations"]
    ]
    china_situation.validate_china_situation(bound)

    wrong_anchor = copy.deepcopy(bound)
    wrong_anchor["situations"][0]["url"] = (
        "https://palimpsest.info/news/china/situation/#situation-" + "f" * 24
    )
    with pytest.raises(
        china_situation.ChinaSituationError, match="canonical situation"
    ):
        china_situation.validate_china_situation(wrong_anchor)


def test_runtime_validator_rejects_relation_and_count_tampering(situation):
    strengthened = copy.deepcopy(situation)
    row = next(
        item for item in strengthened["situations"] if item["measurement_context"]
    )
    row["measurement_context"][0]["relation"] = "verified-by-observatory"
    with pytest.raises(china_situation.ChinaSituationError, match="relation"):
        china_situation.validate_china_situation(strengthened)

    bad_count = copy.deepcopy(situation)
    bad_count["coverage"]["publisher_reports"] += 1
    with pytest.raises(china_situation.ChinaSituationError, match="publisher report"):
        china_situation.validate_china_situation(bad_count)

    unknown = copy.deepcopy(situation)
    unknown["truth_score"] = 1
    with pytest.raises(china_situation.ChinaSituationError, match="fields differ"):
        china_situation.validate_china_situation(unknown)

    wrong_order = copy.deepcopy(situation)
    wrong_order["situations"][0], wrong_order["situations"][1] = (
        wrong_order["situations"][1],
        wrong_order["situations"][0],
    )
    with pytest.raises(
        china_situation.ChinaSituationError, match="reverse chronological"
    ):
        china_situation.validate_china_situation(wrong_order)


def test_prior_validator_accepts_only_the_superseded_updated_order(situation):
    prior = copy.deepcopy(situation)
    prior["situations"].sort(
        key=lambda row: (row["updated_at"], row["situation_id"]), reverse=True
    )
    assert prior["situations"] != situation["situations"]

    with pytest.raises(
        china_situation.ChinaSituationError, match="reverse chronological"
    ):
        china_situation.validate_china_situation(prior)
    china_situation.validate_prior_china_situation(prior)

    tampered = copy.deepcopy(prior)
    tampered["coverage"]["publisher_reports"] += 1
    with pytest.raises(china_situation.ChinaSituationError, match="publisher report"):
        china_situation.validate_prior_china_situation(tampered)


def test_generated_situation_conforms_to_public_json_schema(situation):
    validator = _protocol_validator(ROOT / "protocol/china-situation-v1.schema.json")
    validator.validate(situation)


def test_osint_observations_join_by_exact_url_or_topic_and_stay_non_corroboration(
    inputs,
):
    wire, _feed, analyses = inputs
    event = next(
        row
        for row in wire["events"]
        if analyses[row["event_id"]]["scope_status"] == "in-scope"
        and row["evidence_refs"]
    )
    url = event["evidence_refs"][0]["url"]
    topic = event["topics"][0] if event["topics"] else "china"
    observations = [
        {
            "source": "ddti",
            "title": "Public ledger row",
            "url": url,
            "terms": [topic],
            "first_seen": "2026-08-01T00:00:00Z",
            "last_seen": "2026-08-01T00:00:00Z",
            "content_sha256": "a" * 64,
            "gazetteer_hits": [{"zh": "白纸运动", "en": "White Paper"}],
            "archive": {
                "wayback_lookup": "https://web.archive.org/web/*/https://example.test/",
                "wayback_snapshot": None,
                "archive_today_lookup": None,
                "timestamp_bracket": {"last_live": None, "post_event": None},
            },
        }
    ]
    document = china_situation.build_china_situation(
        wire, analyses, osint_observations=observations
    )
    row = next(
        item for item in document["situations"] if item["event_id"] == event["event_id"]
    )
    assert row["osint_context"]
    assert row["osint_context"][0]["relation"] == (
        "topic-or-url-context-not-corroboration"
    )
    assert document["coverage"]["osint_context_rows"] >= 1
    assert document["coverage"]["events_with_osint_context"] >= 1


def _title_matchable_event(wire, analyses):
    for event in wire["events"]:
        if analyses[event["event_id"]]["scope_status"] != "in-scope":
            continue
        if not china_situation._span_is_substantial(event["headline"]):
            continue
        if china_situation._PERSON_STATUS_RE.search(event["headline"] + event["dek"]):
            continue
        for reference in event["evidence_refs"]:
            if reference["source_id"] in _SOCIAL_SOURCE_BY_WIRE_SOURCE:
                return event, reference
    raise AssertionError("fixture has no title-matchable in-scope social event")


def test_title_surface_match_is_not_corroboration_and_does_not_add_a_group(inputs):
    wire, _feed, analyses = inputs
    event, reference = _title_matchable_event(wire, analyses)
    baseline = china_situation.build_china_situation(wire, analyses)
    baseline_row = next(
        item for item in baseline["situations"] if item["event_id"] == event["event_id"]
    )
    registry = social_observations.load_source_registry()
    fixture_time = _minutes_after(reference["published_at"])
    record = _social_record(
        reference, observed_at=fixture_time, title=event["headline"]
    )
    record["related_urls"] = []
    social, _ledger = social_observations.build_latest(
        [record],
        registry=registry,
        generated_at=fixture_time,
        collection_receipts=_social_receipts(registry, record["source_id"]),
    )

    document = china_situation.build_china_situation(wire, analyses, social=social)
    row = next(
        item for item in document["situations"] if item["event_id"] == event["event_id"]
    )
    assert document["coverage"]["social_observations_linked"] == 1
    assert row["social_context"][0]["relation"] == (
        "topic-title-context-not-corroboration"
    )
    assert row["social_context"][0]["matched_article_url"].startswith("https://")
    assert row["reporting"]["independent_groups"] == baseline_row["reporting"][
        "independent_groups"
    ]
    assert row["reporting"]["evidence_strength"] == baseline_row["reporting"][
        "evidence_strength"
    ]
    assert "do not increase that count" in row["synthesis"]["summary"]


def test_exact_publisher_url_wins_over_a_title_surface_on_another_event(inputs):
    wire, _feed, analyses = inputs
    event_a, reference_a = _event_and_social_reference(wire, analyses, "in-scope")
    event_b = next(
        row
        for row in wire["events"]
        if row["event_id"] != event_a["event_id"]
        and analyses[row["event_id"]]["scope_status"] == "in-scope"
        and row["headline"] != event_a["headline"]
    )
    registry = social_observations.load_source_registry()
    fixture_time = _minutes_after(reference_a["published_at"])
    record = _social_record(
        reference_a,
        observed_at=fixture_time,
        title=event_b["headline"],
    )
    social, _ledger = social_observations.build_latest(
        [record],
        registry=registry,
        generated_at=fixture_time,
        collection_receipts=_social_receipts(registry, record["source_id"]),
    )

    document = china_situation.build_china_situation(wire, analyses, social=social)
    row = next(
        item for item in document["situations"] if item["event_id"] == event_a["event_id"]
    )
    assert row["social_context"][0]["relation"] == (
        "publisher-link-context-not-corroboration"
    )
    linked_ids = {
        item["event_id"] for item in document["situations"] if item["social_context"]
    }
    assert linked_ids == {event_a["event_id"]}
    assert document["coverage"]["social_observations_linked"] == 1


def test_generic_or_person_status_titles_do_not_become_situation_findings(inputs):
    wire, _feed, analyses = inputs
    _event, reference = _event_and_social_reference(wire, analyses, "in-scope")
    registry = social_observations.load_source_registry()
    generic_time = _minutes_after(reference["published_at"])
    missing_time = _minutes_after(generic_time, minutes=5)
    generic = _social_record(
        reference, observed_at=generic_time, title="China news"
    )
    generic["related_urls"] = []
    generic["native_id"] = "fixture-generic-title"
    missing = _social_record(
        reference,
        observed_at=missing_time,
        title="A Chinese official is reported missing",
    )
    missing["related_urls"] = []
    missing["native_id"] = "fixture-person-status"
    if generic["source_id"] == "cgtn-telegram":
        generic["permalink"] = "https://t.me/fixture_public/11/"
        missing["permalink"] = "https://t.me/fixture_public/12/"
    else:
        generic["permalink"] = "https://www.instagram.com/p/SITUATION_TEST_GENERIC/"
        missing["permalink"] = "https://www.instagram.com/p/SITUATION_TEST_MISSING/"
    social, _ledger = social_observations.build_latest(
        [generic, missing],
        registry=registry,
        generated_at=missing_time,
        collection_receipts=_social_receipts(registry, generic["source_id"]),
    )

    document = china_situation.build_china_situation(wire, analyses, social=social)
    assert document["coverage"]["social_observations"] == 2
    assert document["coverage"]["social_observations_linked"] == 0
    assert all(not row["social_context"] for row in document["situations"])


def test_situation_copies_named_key_interconnection_without_mixing_peer_sentences(
    inputs,
):
    from tests.test_event_interconnection import _warehouses, _load_fixture

    wire, feed, _analyses = inputs
    official = _load_fixture("official-first-seen-warehouse.json")
    greatfire = _load_fixture("greatfire-warehouse.json")
    analyses = event_analysis.build_event_analyses(
        wire,
        feed,
        peer_warehouses=_warehouses(
            **{"official-first-seen": official, "greatfire": greatfire}
        ),
    )
    document = china_situation.build_china_situation(wire, analyses)
    joined_rows = [
        row
        for row in document["situations"]
        if row["interconnection"]["joined_count"]
    ]
    assert document["coverage"]["interconnection_joined_rows"] == sum(
        row["interconnection"]["joined_count"] for row in document["situations"]
    )
    assert document["coverage"]["events_with_interconnection"] == len(joined_rows)
    if joined_rows:
        row = joined_rows[0]
        assert row["peer_context"] == [] or all(
            item["relation"] == "peer-context-not-palimpsest-capture"
            for item in row["peer_context"]
        )
        assert row["interconnection"]["relation"] == "topic-surface-only"
        assert row["interconnection"]["meets_quality_bar"] is (
            row["interconnection"]["independent_source_groups"] >= 2
        )
