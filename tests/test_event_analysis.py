"""Contract tests for evidence-bounded analysis on every wire event."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from core import event_analysis


ROOT = Path(__file__).resolve().parent.parent
FIXED_EVENT_ID = "event-" + "a1" * 12
FIXED_EVENT_VERSION_ID = "eventv-" + "b2" * 12
FIXED_ITEM_ID = "item-" + "c3" * 12
FIXED_ITEM_VERSION_ID = "itemv-" + "d4" * 12
FIXED_SIGNAL_ID = "fixture-signal"


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


def _fixed_event(*, china_term: bool = False, linked: bool = False) -> dict:
    headline = "China weather bulletin" if china_term else "Regional weather bulletin"
    return {
        "event_id": FIXED_EVENT_ID,
        "version_id": FIXED_EVENT_VERSION_ID,
        "url": f"https://palimpsest.info/news/wire/{FIXED_EVENT_ID}/",
        "headline": headline,
        "dek": "A wire service published a routine regional weather note.",
        "desk": "information-controls",
        "topics": ["weather"],
        "published_at": "2026-08-20T01:00:00Z",
        "updated_at": "2026-08-20T01:00:00Z",
        "lead": False,
        "lead_reason": "single-source",
        "evidence_strength": "single-source",
        "reported_facts": [
            {
                "statement": f"Fixture Wire published “{headline}”.",
                "attribution": "Fixture Wire",
                "published_at": "2026-08-20T01:00:00Z",
                "evidence_item_id": FIXED_ITEM_ID,
            }
        ],
        "evidence_refs": [
            {
                "item_id": FIXED_ITEM_ID,
                "version_id": FIXED_ITEM_VERSION_ID,
                "source_id": "fixture-world-wire",
                "source_name": "Fixture Wire",
                "role": "media",
                "independence_group": "fixture-wire",
                "title": headline,
                "url": "https://www.example.com/weather",
                "published_at": "2026-08-20T01:00:00Z",
            }
        ],
        "evidence_groups": [
            {
                "group_id": "fixture-wire",
                "source_ids": ["fixture-world-wire"],
                "roles": ["media"],
            }
        ],
        "declared_links": {
            "relation": "topic-surface-only",
            "scan_signal_ids": [FIXED_SIGNAL_ID] if linked else [],
            "economic_signal_ids": [],
        },
        "limitations": ["Synthetic feed metadata only."],
        "mutation": {"kind": "new", "previous_version_id": None},
    }


def _fixed_wire(event: dict) -> dict:
    return {
        "schema_version": "palimpsest-newswire.v1",
        "items": [
            {
                "item_id": FIXED_ITEM_ID,
                "title": event["headline"],
                "excerpt": "A routine regional weather note.",
                "feed_sha256": "e5" * 32,
                "source_id": "fixture-world-wire",
            }
        ],
        "events": [event],
    }


def _fixed_feed(status: str | None = None) -> dict:
    stories = []
    if status is not None:
        live = status == "live"
        stories.append(
            {
                "signal_id": FIXED_SIGNAL_ID,
                "headline": "Synthetic collector reading",
                "status": status,
                "url": f"https://palimpsest.info/news/{FIXED_SIGNAL_ID}/",
                "modified_at": "2026-08-20T02:00:00Z",
                "claim_fingerprint": "sha256:" + "f6" * 32,
                "metric": {
                    "label": "fixture index" if live else None,
                    "value": 1.0 if live else None,
                    "unit": "index" if live else None,
                    "denominator": {
                        "label": "fixture observations" if live else None,
                        "value": 1 if live else None,
                    },
                },
                "claims": [{"statement": "The fixed collector fixture is available."}],
                "evidence": {
                    "url": (
                        "https://palimpsest.info/readings/"
                        f"{FIXED_SIGNAL_ID}-latest.json"
                    ),
                    "input": {"sha256": "07" * 32},
                    "source_timestamp": "2026-08-20T02:00:00Z",
                },
                "method": {
                    "summary": "Synthetic aggregate used as topical context only.",
                    "version": 1,
                },
            }
        )
    return {
        "schema_version": "palimpsest-news.v1",
        "generated_at": "2026-08-20T02:00:00Z",
        "stories": stories,
    }


def _fixed_analysis(
    *, china_term: bool = False, collector_status: str | None = None
) -> dict:
    event = _fixed_event(
        china_term=china_term,
        linked=collector_status is not None,
    )
    return event_analysis.build_event_analysis(
        event,
        wire=_fixed_wire(event),
        feed=_fixed_feed(collector_status),
    )


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


def test_outside_remit_articles_are_explicitly_bounded() -> None:
    analysis = _fixed_analysis()

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
            assert (
                "verification join" in row["interpretation"] or row["status"] != "live"
            )


def test_dispositions_follow_scope_and_collector_freshness(inputs, analyses) -> None:
    _wire, _feed = inputs
    counts = Counter(row["disposition"] for row in analyses.values())
    expected_counts = Counter()
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
        expected_counts[expected] += 1
    assert counts == expected_counts

    fixed_cases = (
        (False, None, "outside-remit", "outside-remit"),
        (True, None, "in-scope", "source-assessment"),
        (False, "live", "in-scope", "collector-context"),
        (False, "stale", "in-scope", "collector-abstention"),
    )
    for china_term, status, expected_scope, expected_disposition in fixed_cases:
        analysis = _fixed_analysis(
            china_term=china_term,
            collector_status=status,
        )
        assert analysis["scope_status"] == expected_scope
        assert analysis["disposition"] == expected_disposition


def test_non_live_declared_collector_forces_an_explicit_abstention() -> None:
    analysis = _fixed_analysis(collector_status="stale")

    assert analysis["disposition"] == "collector-abstention"
    assert analysis["collector_context"][0]["signal_id"] == FIXED_SIGNAL_ID
    assert analysis["collector_context"][0]["status"] == "stale"


def test_analysis_is_deterministic_and_changes_when_bound_collector_evidence_changes() -> (
    None
):
    event = _fixed_event(linked=True)
    wire = _fixed_wire(event)
    feed = _fixed_feed("live")
    first = event_analysis.build_event_analysis(event, wire=wire, feed=feed)
    second = event_analysis.build_event_analysis(
        copy.deepcopy(event),
        wire=copy.deepcopy(wire),
        feed=copy.deepcopy(feed),
    )
    assert first == second

    modified_feed = copy.deepcopy(feed)
    story = modified_feed["stories"][0]
    story["claims"][0]["statement"] += " Updated normalized finding."
    story["claim_fingerprint"] = "sha256:" + "a" * 64

    changed = event_analysis.build_event_analysis(event, wire=wire, feed=modified_feed)
    assert changed["analysis_id"] != first["analysis_id"]
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


def test_peer_miss_rows_use_the_warehouse_clock_not_wall_clock() -> None:
    from core.peer_context import peer_context_for_event

    event = {
        "event_id": "evt-peer-clock",
        "updated_at": "2026-08-20T00:00:00Z",
        "published_at": "2026-08-20T00:00:00Z",
        "canonical_url": "https://www.example.com/story",
        "url": "https://www.example.com/story",
        "title": "example",
        "items": [],
    }
    peer = {
        "generated_at": "2026-08-22T09:48:00Z",
        "greatfire": {},
        "ooni": {},
        "cdt_items": [],
    }
    first = peer_context_for_event(event, peer)
    second = peer_context_for_event(event, peer)
    assert first == second
    assert all(
        row["as_of"] in {"2026-08-22T09:48:00Z", "2026-08-20T00:00:00Z"}
        or row["as_of"] is None
        or row["status"] == "live"
        for row in first
    )
    miss_or_silent = [
        row for row in first if row["status"] in {"miss", "silent", "abstain"}
    ]
    assert miss_or_silent
    assert all(row["as_of"] is None for row in miss_or_silent)
