from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.wire_claim_audits import (
    DELIVERY_POLICY,
    PROBABILITY_METHOD,
    WireClaimAuditError,
    _event_geography,
    _event_text,
    _signal_fit,
    build_wire_claim_audits,
    canonical_json_bytes,
    validate_wire_claim_audits,
)


ROOT = Path(__file__).resolve().parents[1]
READINGS = ROOT / "readings"
CLOCK = datetime(2026, 8, 13, 17, 30, tzinfo=timezone.utc)


def _fixture_event(
    index: int,
    *,
    slug: str,
    headline: str,
    dek: str,
    desk: str,
    evidence_refs: list[dict],
    evidence_strength: str = "single-source",
) -> dict:
    published_at = f"2026-08-13T{12 + index:02d}:00:00Z"
    return {
        "event_id": f"event-{index:024x}",
        "version_id": f"eventv-{index:024x}",
        "url": f"https://fixture.example/wire/{slug}",
        "headline": headline,
        "dek": dek,
        "desk": desk,
        "topics": ["china", desk],
        "published_at": published_at,
        "updated_at": published_at,
        "evidence_strength": evidence_strength,
        "evidence_refs": evidence_refs,
        "reported_facts": [],
    }


@pytest.fixture
def synthetic_claim_audits(tmp_path: Path) -> dict:
    yuan_source = {
        "source_id": "fixture-china-desk",
        "source_name": "Fixture China Desk",
        "role": "media",
        "independence_group": "publisher:fixture-china-desk",
        "url": "https://fixture.example/sources/yuan-policy",
    }
    ev_sources = [
        {
            "source_id": f"fixture-ev-desk-{index}",
            "source_name": f"Fixture EV Desk {index}",
            "role": "media",
            "independence_group": f"publisher:fixture-ev-desk-{index}",
            "url": f"https://fixture.example/sources/ev-sales-{index}",
        }
        for index in (1, 2)
    ]
    routine_source = {
        "source_id": "hksar-releases",
        "source_name": "Fixture HKSAR Release",
        "role": "primary",
        "independence_group": "publisher:fixture-hksar",
        "url": "https://fixture.example/sources/tender-results",
    }
    ddti_source_url = "https://fixture.example/sources/lin-qiao-petition"
    ddti_source = {
        "source_id": "fixture-rights-desk",
        "source_name": "Fixture Rights Desk",
        "role": "media",
        "independence_group": "publisher:fixture-rights-desk",
        "url": ddti_source_url,
    }
    capped_yuan_dek = (
        "The fixture source attributes a new settlement strategy to Beijing and "
        "describes its stated ambition as making China a “financial powerhouse”."
        + " This retained continuation is deliberately incomplete and contains no "
        "sentence terminator" * 3
        + " before the plan perio"
    )
    events = [
        _fixture_event(
            1,
            slug="yuan-policy",
            headline=(
                "Fixture: Beijing announces a five-year plan that targets global "
                "yuan use after a 25 percent settlement-share surge"
            ),
            dek=capped_yuan_dek,
            desk="economy",
            evidence_refs=[yuan_source],
        ),
        _fixture_event(
            2,
            slug="ev-sales",
            headline="Fixture: China retail EV sales slide again as discounting deepens",
            dek="Two fixture sources report that China EV sales declined 12 percent.",
            desk="economy",
            evidence_refs=ev_sources,
            evidence_strength="multi-source",
        ),
        _fixture_event(
            3,
            slug="routine-tender",
            headline="Fixture: Hong Kong reports tender results for Exchange Fund Bills",
            dek="A routine fixture release records the scheduled tender results.",
            desk="economy",
            evidence_refs=[routine_source],
            evidence_strength="single-primary-source",
        ),
        _fixture_event(
            4,
            slug="same-source-ddti",
            headline=(
                "Fixture: China detains labor advocate Lin Qiao after deleted wage petition"
            ),
            dek="The fixture source reports detention after a wage-rights petition was deleted.",
            desk="rights",
            evidence_refs=[ddti_source],
        ),
    ]
    inputs = {
        "newswire-latest.json": {
            "schema_version": "palimpsest-newswire.v1",
            "generated_at": "2026-08-13T17:00:00Z",
            "events": events,
        },
        "osint-china-latest.json": {
            "schema_version": "osint-china.v1",
            "generated_at": "2026-08-13T17:00:00Z",
            "signals": [
                {
                    "id": "china-econ",
                    "title": "Fixture China money-market benchmark",
                    "live": True,
                    "status": "fresh",
                    "source_timestamp": "2026-08-13T17:00:00Z",
                    "metric": {
                        "label": "available benchmark count",
                        "value": 1,
                        "unit": "count",
                        "denominator": None,
                    },
                    "payload": {"benchmarks": {"fdr007": 1.75}},
                },
                {
                    "id": "data-darkness",
                    "title": "Fixture publication-coverage monitor",
                    "live": True,
                    "status": "fresh",
                    "source_timestamp": "2026-08-13T17:00:00Z",
                    "metric": {
                        "label": "publication darkness index",
                        "value": 9,
                        "unit": "points",
                        "denominator": None,
                    },
                },
                {
                    "id": "ddti",
                    "title": "Fixture deletion-directive trace",
                    "live": True,
                    "status": "fresh",
                    "source_timestamp": "2026-08-13T17:00:00Z",
                    "metric": {
                        "label": "ranked deletion terms",
                        "value": 1,
                        "unit": "count",
                        "denominator": None,
                    },
                    "payload": {
                        "n_terms": 1,
                        "ranked": [
                            {
                                "term": "Lin Qiao wage petition",
                                "samples": [{"url": ddti_source_url}],
                            }
                        ],
                    },
                },
            ],
        },
        "board-alarm-latest.json": {
            "generated_at": "2026-08-13T17:00:00Z",
            "signals": {},
        },
        "coverage-guard-latest.json": {
            "generated_at": "2026-08-13T17:00:00Z",
            "confounded": [],
        },
    }
    for filename, payload in inputs.items():
        (tmp_path / filename).write_bytes(canonical_json_bytes(payload))

    document = build_wire_claim_audits(tmp_path, decision_clock=CLOCK)
    audits = {
        audit["url"].removeprefix("https://fixture.example/wire/"): audit
        for audit in document["audits"]
    }
    return {"audits": audits}


def test_claim_audits_cover_every_wire_event_and_replay_exactly() -> None:
    first = build_wire_claim_audits(READINGS, decision_clock=CLOCK)
    second = build_wire_claim_audits(READINGS, decision_clock=CLOCK)

    validate_wire_claim_audits(first)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["delivery_policy"] == DELIVERY_POLICY
    assert first["probability_method"] == PROBABILITY_METHOD
    newswire = json.loads((READINGS / "newswire-latest.json").read_bytes())
    assert first["n_events"] == first["n_audits"] == len(newswire["events"])
    assert sum(first["counts"].values()) == first["n_audits"]

    for audit in first["audits"]:
        probabilities = [
            row["probability_percent"]
            for row in audit["competing_explanations"]
        ]
        assert sum(probabilities) == 100
        assert all(value % 5 == 0 for value in probabilities)


def test_editorial_ranking_scores_consequential_change_over_routine_print(
    synthetic_claim_audits: dict,
) -> None:
    audits = synthetic_claim_audits["audits"]
    yuan = audits["yuan-policy"]
    ev_sales = audits["ev-sales"]
    routine = audits["routine-tender"]

    assert routine["brief_eligible"] is False
    assert "routine release without a material change signal" in routine["interest"][
        "penalties"
    ]
    assert yuan["interest"]["band"] == "exceptional"
    assert ev_sales["interest"]["band"] in {"strong", "exceptional"}
    assert yuan["interest"]["score"] > routine["interest"]["score"]
    assert ev_sales["interest"]["score"] > routine["interest"]["score"]


def test_truth_source_context_and_motive_probabilities_remain_separate(
    synthetic_claim_audits: dict,
) -> None:
    yuan = synthetic_claim_audits["audits"]["yuan-policy"]

    assert yuan["truth_assessment"]["status"] == "single_source_attributed"
    assert (
        yuan["truth_assessment"]["collector_conclusion"]
        == "no_comparable_instrument"
    )
    assert all(
        row["fit"] != "direct-test-surface"
        for row in yuan["current_condition"]
    )
    fdr_rows = [
        row
        for row in yuan["current_condition"]
        if row["signal_id"] == "china-econ"
        and row["metric"]["label"] == "FDR007 repo fixing"
    ]
    assert len(fdr_rows) == 1
    probabilities = [
        row["probability_percent"] for row in yuan["competing_explanations"]
    ]
    assert len(probabilities) > 1
    assert sum(probabilities) == 100
    assert "conditional on the source account" in yuan["synthesis"][
        "why_it_might_be_happening"
    ].casefold()


def test_capped_feed_excerpt_is_not_published_mid_word(
    synthetic_claim_audits: dict,
) -> None:
    yuan = synthetic_claim_audits["audits"]["yuan-policy"]

    summary = yuan["source_claim"]["attributed_summary"]
    assert summary.endswith("financial powerhouse”.")
    assert "plan perio" not in summary
    assert "plan perio" not in yuan["synthesis"]["what_happened"]


def test_hong_kong_release_does_not_inherit_mainland_measurements() -> None:
    event = {
        "headline": "Hong Kong small-business survey",
        "dek": "A current Hong Kong economic release.",
        "topics": ["economy"],
        "evidence_refs": [{"source_id": "hksar-releases"}],
    }
    text = _event_text(event)
    geography = _event_geography(event, text)

    assert geography == "hong-kong"
    assert {
        _signal_fit(signal_id, text, {"economic-conditions"}, geography)
        for signal_id in {"data-darkness", "china-econ", "cny-fix-gap"}
    } == {"cross-geography-context"}


def test_same_source_ddti_echo_is_named_as_non_corroboration(
    synthetic_claim_audits: dict,
) -> None:
    migrant = synthetic_claim_audits["audits"]["same-source-ddti"]
    trace = next(
        row for row in migrant["current_condition"] if row["signal_id"] == "ddti"
    )

    assert trace["fit"] == "same-lineage-topic-trace"
    assert trace["metric"]["value"] > 0
    assert "not corroboration" in trace["read"].casefold()
    assert migrant["truth_assessment"]["status"] == "single_source_attributed"
    assert (
        migrant["truth_assessment"]["collector_conclusion"]
        == "no_comparable_instrument"
    )


def test_validator_rejects_probability_drift() -> None:
    document = build_wire_claim_audits(READINGS, decision_clock=CLOCK)
    forged = copy.deepcopy(document)
    explanations = forged["audits"][0]["competing_explanations"]
    explanations[0]["probability_percent"] += 5

    with pytest.raises(WireClaimAuditError, match="sum to 100"):
        validate_wire_claim_audits(forged)
