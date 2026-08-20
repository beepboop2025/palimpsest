"""Contract tests for evidence-bounded analysis on every wire event."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from core import event_analysis


ROOT = Path(__file__).resolve().parent.parent


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


@pytest.fixture(scope="module")
def inputs():
    wire = json.loads((ROOT / "readings/newswire-latest.json").read_text())
    feed = json.loads((ROOT / "readings/newsroom-latest.json").read_text())
    return wire, feed


@pytest.fixture(scope="module")
def analyses(inputs):
    wire, feed = inputs
    return event_analysis.build_event_analyses(wire, feed)


def test_every_wire_event_gets_exactly_one_content_addressed_assessment(
    inputs, analyses
) -> None:
    wire, _feed = inputs

    assert set(analyses) == {event["event_id"] for event in wire["events"]}
    assert len(analyses) == wire["n_events"]
    assert len({row["analysis_id"] for row in analyses.values()}) == len(analyses)
    for event in wire["events"]:
        analysis = analyses[event["event_id"]]
        event_analysis.validate_event_analysis(analysis, event=event)
        assert analysis["url"] == f"{event['url']}analysis.json"


def test_bangladesh_article_is_explicitly_outside_the_china_remit(analyses) -> None:
    analysis = analyses["event-7f9867253599802f5d470f8a"]

    assert analysis["scope_status"] == "outside-remit"
    assert analysis["disposition"] == "outside-remit"
    assert analysis["collector_context"] == []
    assert "outside the declared China evidence remit" in analysis["position"]
    assert "not be read as a Palimpsest finding" in analysis["position"]


def test_collector_context_uses_only_declared_signals_and_never_calls_it_verification(
    inputs, analyses
) -> None:
    wire, _feed = inputs
    events = {event["event_id"]: event for event in wire["events"]}

    for event_id, analysis in analyses.items():
        event = events[event_id]
        declared = sorted(
            set(event["declared_links"]["scan_signal_ids"])
            | set(event["declared_links"]["economic_signal_ids"])
        )
        observed = [row["signal_id"] for row in analysis["collector_context"]]
        if analysis["scope_status"] == "outside-remit":
            assert observed == []
        else:
            assert observed == declared
        for row in analysis["collector_context"]:
            assert row["relation"] == "topic-surface-only"
            assert "context only" in row["interpretation"] or row["status"] != "live"
            assert "verification join" in row["interpretation"] or row["status"] != "live"


def test_dispositions_follow_scope_and_collector_freshness(inputs, analyses) -> None:
    _wire, _feed = inputs
    counts = Counter(row["disposition"] for row in analyses.values())

    assert counts["outside-remit"] > 0
    assert counts["source-assessment"] > 0
    assert counts["collector-context"] > 0
    expected_abstentions = 0
    for analysis in analyses.values():
        statuses = [row["status"] for row in analysis["collector_context"]]
        expected = (
            "outside-remit"
            if analysis["scope_status"] == "outside-remit"
            else "source-assessment"
            if not statuses
            else "collector-context"
            if set(statuses) == {"live"}
            else "collector-abstention"
        )
        assert analysis["disposition"] == expected
        if expected == "collector-abstention":
            expected_abstentions += 1
    assert counts["collector-abstention"] == expected_abstentions


def test_non_live_declared_collector_forces_an_explicit_abstention(inputs) -> None:
    wire, feed = inputs
    event = next(
        row
        for row in wire["events"]
        if row["declared_links"]["scan_signal_ids"]
        or row["declared_links"]["economic_signal_ids"]
    )
    signal_id = sorted(
        set(event["declared_links"]["scan_signal_ids"])
        | set(event["declared_links"]["economic_signal_ids"])
    )[0]
    modified_feed = copy.deepcopy(feed)
    story = next(row for row in modified_feed["stories"] if row["signal_id"] == signal_id)
    story["status"] = "stale"
    story["metric"]["value"] = None

    analysis = event_analysis.build_event_analysis(event, wire=wire, feed=modified_feed)

    assert analysis["disposition"] == "collector-abstention"
    assert any(row["status"] == "stale" for row in analysis["collector_context"])


def test_analysis_is_deterministic_and_changes_when_bound_collector_evidence_changes(
    inputs,
) -> None:
    wire, feed = inputs
    first = event_analysis.build_event_analyses(wire, feed)
    second = event_analysis.build_event_analyses(copy.deepcopy(wire), copy.deepcopy(feed))
    assert first == second

    event = next(
        row
        for row in wire["events"]
        if first[row["event_id"]]["collector_context"]
    )
    signal_id = first[event["event_id"]]["collector_context"][0]["signal_id"]
    modified_feed = copy.deepcopy(feed)
    story = next(row for row in modified_feed["stories"] if row["signal_id"] == signal_id)
    story["claims"][0]["statement"] += " Updated normalized finding."
    story["claim_fingerprint"] = "sha256:" + "a" * 64

    changed = event_analysis.build_event_analysis(
        event, wire=wire, feed=modified_feed
    )
    assert changed["analysis_id"] != first[event["event_id"]]["analysis_id"]
    assert changed["collector_context"][0]["finding"].endswith(
        "Updated normalized finding."
    )


def test_runtime_validator_rejects_unknown_fields_and_editorial_state_tampering(
    inputs, analyses
) -> None:
    wire, _feed = inputs
    event = wire["events"][0]
    original = analyses[event["event_id"]]

    unknown = copy.deepcopy(original)
    unknown["truth_score"] = 0.9
    with pytest.raises(event_analysis.EventAnalysisError, match="fields differ"):
        event_analysis.validate_event_analysis(unknown, event=event)

    strengthened = copy.deepcopy(original)
    strengthened["position"] = "Collectors verified every claim in the article."
    with pytest.raises(event_analysis.EventAnalysisError, match="analysis_id"):
        event_analysis.validate_event_analysis(strengthened, event=event)

    broken_state = copy.deepcopy(original)
    broken_state["disposition"] = (
        "source-assessment"
        if original["disposition"] == "outside-remit"
        else "outside-remit"
    )
    with pytest.raises(event_analysis.EventAnalysisError, match="disposition"):
        event_analysis.validate_event_analysis(broken_state, event=event)


def test_generated_analysis_conforms_to_the_public_json_schema(analyses) -> None:
    validator = _protocol_validator(ROOT / "protocol/event-analysis-v2.schema.json")

    for analysis in analyses.values():
        validator.validate(analysis)
