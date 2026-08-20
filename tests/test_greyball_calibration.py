"""Eight synthetic Greyball cases. One misclassification fails. Missing is not censorship."""

from __future__ import annotations

import pytest

from core.visibility_event import visibility_label_for
from processors.greyball_missingness import (
    CASES,
    CENSORSHIP_LABEL,
    FIXTURE_PACK,
    censorship_label_if_calibrated,
    classify_world,
    load_fixture_pack,
    run_calibration,
)


def test_fixture_pack_has_eight_cases():
    pack = load_fixture_pack()
    names = [row["case"] for row in pack["cases"]]
    assert names == list(CASES)
    assert len(names) == 8


def test_eight_synthetic_cases_one_misclassification_fails():
    pack = load_fixture_pack()
    failures = []
    for world in pack["cases"]:
        truth = world["case"]
        observables = {k: v for k, v in world.items() if k != "case"}
        predicted = classify_world(observables)["predicted_case"]
        if predicted != truth:
            failures.append((truth, predicted))
    if failures:
        pytest.fail(f"one misclassification fails: {failures}")
    result = run_calibration(fixture_pack=pack)
    assert result["all_distinguished"] is True, result["predictions"]
    assert result["may_emit_censorship_label"] is False
    assert result["censorship_label_emitted"] is None
    assert CENSORSHIP_LABEL not in result["visibility_labels"].values()
    for label in result["visibility_labels"].values():
        assert label != "confirmed_removal"


def test_missing_is_not_censorship_and_absent_is_not_confirmed_removal():
    assert visibility_label_for(state="unavailable") is None
    assert visibility_label_for(state="unavailable", missingness="coverage_gap") is None
    assert visibility_label_for(state="unknown", missingness="archive_gap") == "archive_gap"
    result = run_calibration()
    assert censorship_label_if_calibrated(result) is None
    assert result["censorship_label_emitted"] is None
    assert result["may_emit_censorship_label"] is False
    assert CENSORSHIP_LABEL not in (result.get("visibility_labels") or {}).values()
    assert result.get("censorship_label") is None


def test_fixture_pack_is_committed():
    assert FIXTURE_PACK.name == "greyball_missingness_cases.json"
    assert FIXTURE_PACK.exists()
