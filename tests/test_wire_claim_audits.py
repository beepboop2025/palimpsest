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
    build_wire_claim_audits,
    canonical_json_bytes,
    validate_wire_claim_audits,
)


ROOT = Path(__file__).resolve().parents[1]
READINGS = ROOT / "readings"
CLOCK = datetime(2026, 8, 13, 17, 30, tzinfo=timezone.utc)


def _audit(document: dict, headline_fragment: str) -> dict:
    return next(
        audit
        for audit in document["audits"]
        if headline_fragment.casefold() in audit["headline"].casefold()
    )


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


def test_editorial_ranking_prefers_consequential_change_over_routine_print() -> None:
    document = build_wire_claim_audits(READINGS, decision_clock=CLOCK)
    yuan = _audit(document, "targets global yuan")
    ev_sales = _audit(document, "EV sales slide again")
    tender = _audit(document, "Exchange Fund Bills tender results")

    assert yuan["brief_eligible"] is True
    assert ev_sales["brief_eligible"] is True
    assert tender["brief_eligible"] is False
    assert yuan["interest"]["score"] > tender["interest"]["score"]
    assert ev_sales["interest"]["score"] > tender["interest"]["score"]
    assert "routine release" in " ".join(tender["interest"]["penalties"])


def test_truth_source_context_and_motive_probabilities_remain_separate() -> None:
    document = build_wire_claim_audits(READINGS, decision_clock=CLOCK)
    yuan = _audit(document, "targets global yuan")

    assert yuan["truth_assessment"]["status"] == "single_source_attributed"
    assert (
        yuan["truth_assessment"]["collector_conclusion"]
        == "no_comparable_instrument"
    )
    assert all(
        row["fit"] != "direct-test-surface"
        for row in yuan["current_condition"]
    )
    assert any(
        row["signal_id"] == "china-econ"
        and row["metric"]["label"] == "FDR007 repo fixing"
        for row in yuan["current_condition"]
    )
    assert "conditional on the source account" in yuan["synthesis"][
        "why_it_might_be_happening"
    ].casefold()


def test_capped_feed_excerpt_is_not_published_mid_word() -> None:
    document = build_wire_claim_audits(READINGS, decision_clock=CLOCK)
    yuan = _audit(document, "targets global yuan")

    summary = yuan["source_claim"]["attributed_summary"]
    assert summary.endswith("financial powerhouse”.")
    assert "plan perio" not in summary
    assert "plan perio" not in yuan["synthesis"]["what_happened"]


def test_hong_kong_release_does_not_inherit_mainland_measurements() -> None:
    document = build_wire_claim_audits(READINGS, decision_clock=CLOCK)
    survey = _audit(document, "small and medium-sized enterprises")

    mainland_ids = {"data-darkness", "china-econ", "cny-fix-gap"}
    assert {
        row["fit"]
        for row in survey["current_condition"]
        if row["signal_id"] in mainland_ids
    } == {"cross-geography-context"}
    assert survey["truth_assessment"]["collector_conclusion"] == (
        "no_comparable_instrument"
    )


def test_same_source_ddti_echo_is_named_as_non_corroboration() -> None:
    document = build_wire_claim_audits(READINGS, decision_clock=CLOCK)
    migrant = _audit(document, "Migrant Worker Jing Shuren")
    trace = next(
        row for row in migrant["current_condition"] if row["signal_id"] == "ddti"
    )

    assert trace["fit"] == "same-lineage-topic-trace"
    assert trace["metric"]["value"] > 0
    assert "not corroboration" in trace["read"].casefold()
    assert migrant["truth_assessment"]["status"] == "single_source_attributed"


def test_validator_rejects_probability_drift() -> None:
    document = build_wire_claim_audits(READINGS, decision_clock=CLOCK)
    forged = copy.deepcopy(document)
    explanations = forged["audits"][0]["competing_explanations"]
    explanations[0]["probability_percent"] += 5

    with pytest.raises(WireClaimAuditError, match="sum to 100"):
        validate_wire_claim_audits(forged)
