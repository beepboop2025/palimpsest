"""Synthetic calibration distinguishes eight processes and withholds censorship labels."""

from __future__ import annotations

from processors.synthetic_calibration import (
    CASES,
    censorship_label_if_calibrated,
    classify_world,
    generate_world,
    run_calibration,
)


def test_calibration_distinguishes_all_eight_cases():
    result = run_calibration(seed=7)
    assert set(result["cases"]) == set(CASES)
    assert result["all_distinguished"] is True, result["predictions"]
    assert result["distinguished"] == {case: True for case in CASES}
    assert result["censorship_label_emitted"] is None
    assert result["may_emit_censorship_label"] is False
    for case in CASES:
        world = generate_world(case, seed=7 + CASES.index(case) * 17)
        predicted = classify_world(world)
        assert predicted["predicted_case"] == case, (case, predicted)
        assert predicted["censorship_label"] is None


def test_cannot_emit_censorship_label_even_when_distinguished():
    result = run_calibration(seed=0)
    assert result["all_distinguished"] is True
    assert censorship_label_if_calibrated(result) is None
    assert censorship_label_if_calibrated({"all_distinguished": False}) is None
