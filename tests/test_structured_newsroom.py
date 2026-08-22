"""Contract tests for the aggregate-only structured newsroom feed."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import newsroom


ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = ROOT / "readings" / "osint-china-latest.json"
CONFIG_PATH = ROOT / "config" / "newsroom.json"
SCHEMA_PATH = ROOT / "protocol" / "news-feed-v1.schema.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def source() -> dict:
    return _load(SOURCE_PATH)


@pytest.fixture
def config() -> dict:
    return _load(CONFIG_PATH)


@pytest.fixture
def feed() -> dict:
    return newsroom.build_news_feed(SOURCE_PATH, CONFIG_PATH)


def _stories_by_id(feed: dict) -> dict[str, dict]:
    return {story["signal_id"]: story for story in feed["stories"]}


def test_all_current_signals_have_curated_templates_and_exactly_one_story(
    source: dict, config: dict, feed: dict
) -> None:
    source_ids = {signal["id"] for signal in source["signals"]}
    config_ids = {signal["id"] for signal in config["signals"]}
    story_ids = {story["signal_id"] for story in feed["stories"]}

    assert source_ids == config_ids == story_ids
    assert len(source["signals"]) == len(source_ids)
    assert feed["schema_version"] == "palimpsest-news.v1"
    assert feed["n_stories"] == len(feed["stories"]) == len(story_ids)
    assert all(signal["headline_template"].strip() for signal in config["signals"])
    assert all(signal["claim_template"].strip() for signal in config["signals"])
    assert all(signal["limitations"] for signal in config["signals"])


def test_ooni_story_names_the_arithmetic_denominator(feed: dict) -> None:
    story = _stories_by_id(feed)["ooni-gfw"]

    if story["status"] != "live":
        assert story["status"] in {"stale", "degraded", "missing"}
        assert story["metric"]["value"] is None
        return
    assert "completed measurements" in story["headline"]
    assert "completed China measurements" in story["claims"][0]["statement"]
    assert story["metric"]["denominator"]["label"] == "completed measurements"


def test_live_believability_warmup_reports_collection_without_claiming_drift(
    feed: dict,
) -> None:
    story = _stories_by_id(feed)["believability"]
    surface = story["headline"] + story["dek"] + json.dumps(story["claims"])

    # A live warmup and a degraded/abstain read are both honest. Drift is not.
    assert story["status"] in {"live", "degraded", "stale", "missing"}
    assert "enough history" not in surface
    assert "drift" not in surface.lower() or "no drift" in surface.lower() or "withheld" in surface
    if story["status"] == "live":
        assert story["claims"] == [{
            "type": "observation",
            "statement": (
                "The current believability collection is complete; divergence remains "
                "withheld while its baseline has 0 of 8 required prior months."
            ),
        }]
        assert "building its baseline" in story["headline"]
        assert story["metric"]["value"] is None
        assert story["limitations"][0] == (
            "No drift finding is claimed until 8 prior monthly gaps exist."
        )
    else:
        assert story["claims"][0]["type"] == "availability"
        assert "no current finding" in story["headline"].lower()
        assert story["metric"]["value"] is None


def test_transform_is_byte_deterministic_with_stable_ids_slugs_and_order(
    source: dict, config: dict
) -> None:
    first = newsroom.transform_osint_feed(copy.deepcopy(source), copy.deepcopy(config))
    second_source = copy.deepcopy(source)
    second_source["signals"].reverse()
    second_config = copy.deepcopy(config)
    second_config["sections"].reverse()
    second_config["signals"].reverse()
    second = newsroom.transform_osint_feed(second_source, second_config)

    assert newsroom.canonical_json_bytes(first) == newsroom.canonical_json_bytes(second)
    assert [story["id"] for story in first["stories"]] == [
        f"palimpsest-news:{story['signal_id']}" for story in first["stories"]
    ]
    assert len({story["id"] for story in first["stories"]}) == len(source["signals"])
    assert len({story["slug"] for story in first["stories"]}) == len(source["signals"])
    assert all(
        story["url"] == f"https://palimpsest.info/news/{story['slug']}/"
        for story in first["stories"]
    )
    expected_order = sorted(
        first["stories"],
        key=lambda story: (
            next(section["order"] for section in first["sections"] if section["id"] == story["section"]),
            story["order"],
            story["signal_id"],
        ),
    )
    assert first["stories"] == expected_order


def test_claim_fingerprint_excludes_feed_generated_at_only_churn(
    source: dict, config: dict
) -> None:
    first = newsroom.transform_osint_feed(source, config)
    later_source = copy.deepcopy(source)
    edition_time = datetime.fromisoformat(
        source["generated_at"].replace("Z", "+00:00")
    )
    later_source["generated_at"] = (
        edition_time.astimezone(timezone.utc) + timedelta(seconds=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    later = newsroom.transform_osint_feed(later_source, config)

    first_stories = _stories_by_id(first)
    later_stories = _stories_by_id(later)
    for signal_id in first_stories:
        assert first_stories[signal_id]["id"] == later_stories[signal_id]["id"]
        assert first_stories[signal_id]["slug"] == later_stories[signal_id]["slug"]
        assert (
            first_stories[signal_id]["claim_fingerprint"]
            == later_stories[signal_id]["claim_fingerprint"]
        )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda config: config.update({"schema_version": "palimpsest-newsroom-config.v2"}), "version"),
        (lambda config: config.update({"unexpected": True}), "fields do not match"),
        (lambda config: config["signals"][0].update({"unsafe_extra": "x"}), "fields do not match"),
        (lambda config: config["signals"][0].update({"headline_template": "{unknown}"}), "placeholder"),
        (lambda config: config["signals"][0].update({"slug": "../escape"}), "permalink-safe"),
        (lambda config: config["signals"][0].update({"headline_template": "<script>alert(1)</script>"}), "unsafe"),
    ],
)
def test_config_version_fields_templates_and_values_fail_closed(
    source: dict, config: dict, mutate, match: str
) -> None:
    changed = copy.deepcopy(config)
    mutate(changed)
    with pytest.raises(newsroom.NewsroomError, match=match):
        newsroom.transform_osint_feed(source, changed)


def test_unknown_duplicate_and_cross_contract_signal_ids_are_rejected(
    source: dict, config: dict
) -> None:
    duplicate_source = copy.deepcopy(source)
    duplicate_source["signals"][1]["id"] = duplicate_source["signals"][0]["id"]
    with pytest.raises(newsroom.NewsroomError, match="duplicate signal ids"):
        newsroom.transform_osint_feed(duplicate_source, config)

    duplicate_config = copy.deepcopy(config)
    duplicate_config["signals"][1]["id"] = duplicate_config["signals"][0]["id"]
    with pytest.raises(newsroom.NewsroomError, match="duplicate newsroom signal id"):
        newsroom.transform_osint_feed(source, duplicate_config)

    unknown_config = copy.deepcopy(config)
    unknown_config["signals"][0]["id"] = "unknown-signal"
    unknown_config["signals"][0]["slug"] = "unknown-signal"
    unknown_config["signals"][0]["related_signal_ids"] = []
    with pytest.raises(newsroom.NewsroomError, match="unknown|differ"):
        newsroom.transform_osint_feed(source, unknown_config)


def test_duplicate_json_object_keys_and_nonfinite_constants_are_rejected(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":"osint-china.v1","schema_version":"osint-china.v1"}',
        encoding="utf-8",
    )
    with pytest.raises(newsroom.NewsroomError, match="duplicate JSON key"):
        newsroom.build_news_feed(duplicate, CONFIG_PATH)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"metric":NaN}', encoding="utf-8")
    with pytest.raises(newsroom.NewsroomError, match="non-finite JSON constant"):
        newsroom.build_news_feed(nonfinite, CONFIG_PATH)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_are_rejected_even_inside_opaque_source_payloads(
    source: dict, config: dict, value: float
) -> None:
    changed = copy.deepcopy(source)
    changed["signals"][0]["payload"]["unsafe_number"] = value
    with pytest.raises(newsroom.NewsroomError, match="finite"):
        newsroom.transform_osint_feed(changed, config)


def test_live_and_nonlive_story_semantics_never_promote_an_abstention(feed: dict) -> None:
    for story in feed["stories"]:
        assert len(story["claims"]) == 1
        if story["status"] == "live":
            assert story["claims"][0]["type"] in {
                "finding",
                "observation",
                "method",
                "integrity",
            }
        else:
            assert story["claims"] == [
                {
                    "type": "availability",
                    "statement": (
                        f"No current finding is published for "
                        f"{next(item['name'] for item in _load(CONFIG_PATH)['signals'] if item['id'] == story['signal_id'])} "
                        f"because the source status is {story['status']}."
                    ),
                }
            ]
            assert story["metric"] == {
                "label": None,
                "value": None,
                "unit": None,
                "denominator": {"label": None, "value": None},
            }
            assert story["limitations"][0].startswith("Current finding withheld:")

def test_a_newly_degraded_source_with_a_retained_metric_becomes_availability_only(
    source: dict, config: dict
) -> None:
    changed = copy.deepcopy(source)
    signal = next(
        (
            item
            for item in changed["signals"]
            if item["status"] == "live"
            and isinstance((item.get("metric") or {}).get("value"), (int, float))
        ),
        None,
    )
    if signal is None:
        pytest.skip("no live numeric OSINT signal is available to degrade")
    retained_metric = signal["metric"]["value"]
    previous_status = signal["status"]
    signal["status"] = "degraded"
    signal["live"] = False
    signal["health"]["ok"] = False
    signal["health"]["reason"] = "test degradation"
    if previous_status == "live":
        changed["n_signals_live"] -= 1
        changed["health"]["counts"]["live"] -= 1
    else:
        changed["health"]["counts"][previous_status] -= 1
    changed["health"]["counts"]["degraded"] += 1

    story = _stories_by_id(newsroom.transform_osint_feed(changed, config))[signal["id"]]
    assert story["status"] == "degraded"
    assert story["claims"][0]["type"] == "availability"
    assert story["metric"]["value"] is None
    assert f"{retained_metric:g}" not in story["headline"]
    assert f"{retained_metric:g}" not in story["dek"]


def test_every_claim_keeps_aggregate_metric_evidence_method_and_limitations_beside_it(
    source: dict, feed: dict
) -> None:
    source_by_id = {signal["id"]: signal for signal in source["signals"]}
    for story in feed["stories"]:
        raw = source_by_id[story["signal_id"]]
        assert set(story["metric"]) == {"label", "value", "unit", "denominator"}
        assert set(story["metric"]["denominator"]) == {"label", "value"}
        assert story["claims"]
        assert story["method"]["summary"] == raw["method"]
        assert story["limitations"]
        assert story["evidence"]["url"] == raw["raw_url"]
        assert story["evidence"]["input"]["filename"] == raw["input"]["filename"]
        assert story["evidence"]["input"]["sha256"] == raw["input"]["sha256"]
        assert story["evidence"]["source_timestamp"] == raw["source_timestamp"]
        assert story["published_at"] == (raw["source_timestamp"] or feed["generated_at"])
        assert story["modified_at"] == story["published_at"]


def test_feed_keeps_the_normalized_board_headline_and_strict_coverage_summary(
    source: dict, feed: dict
) -> None:
    assert feed["headline"] == source["headline"]
    board_story = _stories_by_id(feed)["board-alarm"]
    single = re.match(
        r"^Upstream board reports: single layer elevated: ([a-z][a-z0-9_-]{0,63})\.",
        source["headline"],
    )
    if single:
        layer = single.group(1).replace("_", " ").replace("-", " ").capitalize()
        assert board_story["headline"] == (
            f"{layer} layer elevated in the latest board synthesis"
        )
    else:
        assert board_story["headline"] in {
            "Multiple layers elevated together in the latest board synthesis",
            "Signal-level elevations detected in the latest board synthesis",
            "No signal clears the board's historical-elevation threshold",
            "No current board-level analytic headline is available",
        }
    assert source["headline"] in board_story["claims"][0]["statement"]
    assert all(len(story["headline"]) <= 160 for story in feed["stories"])
    assert feed["coverage"] == {
        "total": source["n_signals_total"],
        "reporting": source["n_signals_reporting"],
        "live": source["n_signals_live"],
        "status": source["health"]["status"],
        "counts": {
            "live": source["health"]["counts"]["live"],
            "degraded": source["health"]["counts"]["degraded"],
            "stale": source["health"]["counts"]["stale"],
            "missing": source["health"]["counts"]["missing"],
            "corrupt": source["health"]["counts"]["corrupt"],
        },
    }


def test_output_is_aggregate_only_and_does_not_copy_signal_payload_rows(feed: dict) -> None:
    serialized = newsroom.canonical_json_bytes(feed).decode("utf-8")
    assert '"payload"' not in serialized
    assert '"excerpt"' not in serialized
    assert '"domains"' not in serialized
    assert '"apps"' not in serialized

    forbidden_key_markers = {
        "person",
        "user",
        "email",
        "phone",
        "respondent",
        "device",
        "individual",
    }

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                compact = key.lower().replace("_", "").replace("-", "")
                assert not any(marker in compact for marker in forbidden_key_markers)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(feed)


def test_protocol_schema_is_strict_recursively_and_matches_the_emitted_shape(feed: dict) -> None:
    schema = _load(SCHEMA_PATH)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"]["const"] == "palimpsest-news.v1"
    assert schema["additionalProperties"] is False

    def assert_strict_objects(value: object, path: str = "$") -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False, path
            for key, child in value.items():
                assert_strict_objects(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                assert_strict_objects(child, f"{path}[{index}]")

    assert_strict_objects(schema)
    assert set(feed) == set(schema["required"]) == set(schema["properties"])
    story_required = set(schema["$defs"]["story"]["required"])
    assert all(set(story) == story_required for story in feed["stories"])
    assert set(schema["$defs"]["story"]["properties"]["status"]["enum"]) == {
        "live",
        "degraded",
        "stale",
        "missing",
        "corrupt",
    }
    assert "availability" not in schema["$defs"]["liveClaim"]["properties"]["type"]["enum"]


def test_default_paths_publish_from_osint_to_the_expected_newsroom_destination() -> None:
    assert newsroom.DEFAULT_SOURCE_PATH == SOURCE_PATH
    assert newsroom.DEFAULT_CONFIG_PATH == CONFIG_PATH
    assert newsroom.DEFAULT_OUTPUT_PATH == ROOT / "readings" / "newsroom-latest.json"
    assert newsroom.SCHEMA_PATH == SCHEMA_PATH
