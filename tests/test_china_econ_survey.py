"""Tests for the aggregate-only China business-survey estimator."""
from __future__ import annotations

import math
import unittest

from processors.china_econ_survey import (
    StratumCounts,
    SurveyInputError,
    estimate_diffusion_index,
)


class ChinaEconomicSurveyTests(unittest.TestCase):
    def assertClose(self, actual: float, expected: float, *, tol: float = 1e-12) -> None:
        self.assertTrue(
            math.isclose(actual, expected, rel_tol=tol, abs_tol=tol),
            f"{actual!r} != {expected!r}",
        )

    def test_equal_weight_diffusion_se_ci_and_kish_n(self) -> None:
        result = estimate_diffusion_index(
            [StratumCounts(up=30, same=40, down=30, population_target_share=1.0)]
        )

        self.assertEqual(result["status"], "ok")
        self.assertClose(result["diffusion_index"], 50.0)
        self.assertEqual(
            result["weighted_proportions"],
            {"up": 0.3, "same": 0.4, "down": 0.3},
        )
        self.assertClose(result["kish_effective_sample_size"], 100.0)
        expected_se = 50.0 * math.sqrt(0.6 / 100.0)
        self.assertClose(result["standard_error"], expected_se)
        self.assertClose(
            result["confidence_interval_95"]["low"],
            50.0 - 1.959963984540054 * expected_se,
        )
        self.assertClose(
            result["confidence_interval_95"]["high"],
            50.0 + 1.959963984540054 * expected_se,
        )
        self.assertClose(result["weighting_design_effect"], 1.0)

    def test_poststratification_hits_target_mix_when_uncapped(self) -> None:
        rows = [
            StratumCounts(80, 10, 10, 0.25),
            StratumCounts(10, 10, 80, 0.75),
        ]
        result = estimate_diffusion_index(rows, max_poststrat_weight=10.0)

        self.assertEqual(result["status"], "ok")
        self.assertClose(result["weighted_proportions"]["up"], 0.275)
        self.assertClose(result["weighted_proportions"]["same"], 0.1)
        self.assertClose(result["weighted_proportions"]["down"], 0.625)
        self.assertClose(result["diffusion_index"], 32.5)
        # Conceptual respondent weights are 0.5 and 1.5.
        self.assertClose(result["kish_effective_sample_size"], 160.0)
        self.assertEqual(result["weights"]["strata_capped"], 0)
        self.assertClose(
            result["weights"]["target_total_variation_gap_after_cap"], 0.0
        )

    def test_weight_cap_is_enforced_and_reports_calibration_gap(self) -> None:
        rows = [
            StratumCounts(80, 10, 10, 0.10),
            StratumCounts(10, 10, 80, 0.90),
        ]
        uncapped = estimate_diffusion_index(rows, max_poststrat_weight=10.0)
        capped = estimate_diffusion_index(rows, max_poststrat_weight=1.0)

        self.assertClose(uncapped["diffusion_index"], 22.0)
        # A normalized cap of one permits no unequal weights: bounded
        # calibration redistributes the trimmed mass until both factors are 1.
        self.assertClose(capped["diffusion_index"], 50.0)
        self.assertClose(capped["weights"]["maximum_raw"], 1.8)
        self.assertClose(capped["weights"]["maximum_used"], 1.0)
        self.assertLessEqual(
            capped["weights"]["maximum_used"],
            capped["weights"]["maximum_allowed"],
        )
        self.assertClose(capped["weights"]["sample_weighted_mean_used"], 1.0)
        self.assertEqual(capped["weights"]["strata_capped"], 1)
        self.assertClose(capped["weights"]["population_target_share_capped"], 0.9)
        self.assertClose(
            capped["weights"]["target_total_variation_gap_after_cap"], 0.4
        )
        self.assertClose(capped["kish_effective_sample_size"], 200.0)

    def test_exact_aggregate_mappings_are_accepted(self) -> None:
        result = estimate_diffusion_index(
            [
                {
                    "up": 25,
                    "same": 50,
                    "down": 25,
                    "population_target_share": 1.0,
                }
            ]
        )
        self.assertEqual(result["status"], "ok")
        self.assertClose(result["diffusion_index"], 50.0)

    def test_extra_identifier_field_is_rejected_not_silently_dropped(self) -> None:
        with self.assertRaisesRegex(SurveyInputError, "disallowed respondent_id"):
            estimate_diffusion_index(
                [
                    {
                        "up": 20,
                        "same": 20,
                        "down": 20,
                        "population_target_share": 1.0,
                        "respondent_id": "firm-123",
                    }
                ]
            )

    def test_respondent_rows_and_single_mapping_are_rejected(self) -> None:
        with self.assertRaisesRegex(SurveyInputError, "exactly up, same, down"):
            estimate_diffusion_index(
                [{"respondent_id": "firm-123", "response": "up"}]
            )
        with self.assertRaisesRegex(SurveyInputError, "iterable of aggregate rows"):
            estimate_diffusion_index(  # type: ignore[arg-type]
                {
                    "up": 20,
                    "same": 20,
                    "down": 20,
                    "population_target_share": 1.0,
                }
            )
        with self.assertRaisesRegex(SurveyInputError, "exact aggregate mapping"):
            estimate_diffusion_index([("up", "same", "down")])  # type: ignore[list-item]

    def test_small_cell_suppresses_whole_stratum_without_exposing_its_counts(self) -> None:
        result = estimate_diffusion_index(
            [
                StratumCounts(2, 20, 20, 0.20),
                StratumCounts(20, 20, 20, 0.80),
            ],
            min_cell_count=5,
        )

        self.assertEqual(result["status"], "ok")
        coverage = result["coverage"]
        self.assertEqual(coverage["strata_received"], 2)
        self.assertEqual(coverage["strata_used"], 1)
        self.assertEqual(coverage["strata_suppressed"], 1)
        self.assertEqual(coverage["eligible_sample_size"], 60)
        self.assertClose(coverage["population_target_share_suppressed"], 0.2)
        self.assertClose(coverage["population_coverage"], 0.8)
        # Diagnostics contain only aggregate coverage, never a per-stratum row.
        self.assertNotIn("strata", coverage)

    def test_privacy_floor_cannot_be_disabled_and_zero_cell_is_suppressed(self) -> None:
        row = StratumCounts(10, 0, 10, 1.0)
        suppressed = estimate_diffusion_index([row])
        self.assertEqual(suppressed["status"], "abstain")
        self.assertIn("privacy-eligible", suppressed["reason"])
        for attempted_minimum in (0, 1, 4):
            with self.subTest(attempted_minimum=attempted_minimum):
                with self.assertRaisesRegex(SurveyInputError, "at least 5"):
                    estimate_diffusion_index(
                        [row], min_cell_count=attempted_minimum
                    )

    def test_low_coverage_abstains_without_publishing_an_estimate(self) -> None:
        result = estimate_diffusion_index(
            [
                StratumCounts(2, 20, 20, 0.30),
                StratumCounts(20, 20, 20, 0.70),
            ],
            min_cell_count=5,
            min_population_coverage=0.8,
        )

        self.assertEqual(result["status"], "abstain")
        self.assertIn("coverage", result["reason"])
        self.assertNotIn("diffusion_index", result)
        self.assertClose(result["coverage"]["population_coverage"], 0.7)

    def test_unprovided_target_population_counts_as_uncovered(self) -> None:
        result = estimate_diffusion_index(
            [StratumCounts(20, 20, 20, 0.75)],
            min_population_coverage=0.8,
        )
        self.assertEqual(result["status"], "abstain")
        self.assertClose(
            result["coverage"]["population_target_share_unprovided"], 0.25
        )
        self.assertClose(result["coverage"]["population_coverage"], 0.75)

    def test_empty_valid_input_and_all_suppressed_input_abstain(self) -> None:
        empty = estimate_diffusion_index([])
        suppressed = estimate_diffusion_index([StratumCounts(4, 5, 5, 1.0)])

        self.assertEqual(empty["status"], "abstain")
        self.assertEqual(empty["reason"], "no aggregate strata supplied")
        self.assertEqual(suppressed["status"], "abstain")
        self.assertIn("privacy-eligible", suppressed["reason"])
        self.assertNotIn("standard_error", suppressed)

    def test_se_uses_within_stratum_not_between_stratum_heterogeneity(self) -> None:
        result = estimate_diffusion_index(
            [
                StratumCounts(90, 5, 5, 0.5),
                StratumCounts(5, 5, 90, 0.5),
            ]
        )
        self.assertEqual(result["status"], "ok")
        self.assertClose(result["diffusion_index"], 50.0)
        within_variance = 0.95 - 0.85**2
        expected_mean_variance = 2.0 * 0.5**2 * within_variance / 100.0
        self.assertClose(
            result["standard_error"], 50.0 * math.sqrt(expected_mean_variance)
        )
        # Pooling the two very different strata would wrongly report 3.446.
        self.assertLess(result["standard_error"], 2.0)

    def test_zero_target_rows_do_not_inflate_analyzed_n_or_design_effect(self) -> None:
        result = estimate_diffusion_index(
            [
                StratumCounts(10, 10, 10, 1.0),
                StratumCounts(1000, 1000, 1000, 0.0),
            ]
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage"]["strata_zero_target_excluded"], 1)
        self.assertEqual(result["coverage"]["eligible_sample_size"], 30)
        self.assertClose(result["kish_effective_sample_size"], 30.0)
        self.assertClose(result["weighting_design_effect"], 1.0)

    def test_invalid_counts_shares_and_configuration_raise_explicit_errors(self) -> None:
        invalid_rows = [
            lambda: StratumCounts(-1, 5, 5, 1.0),
            lambda: StratumCounts(1.0, 5, 5, 1.0),  # type: ignore[arg-type]
            lambda: StratumCounts(True, 5, 5, 1.0),
            lambda: StratumCounts(5, 5, 5, float("nan")),
            lambda: StratumCounts(5, 5, 5, 1.01),
        ]
        for make_row in invalid_rows:
            with self.subTest(make_row=make_row), self.assertRaises(SurveyInputError):
                make_row()

        rows = [StratumCounts(5, 5, 5, 1.0)]
        invalid_options = [
            {"min_cell_count": -1},
            {"min_cell_count": 4},
            {"min_cell_count": 1.0},
            {"max_poststrat_weight": 0.99},
            {"max_poststrat_weight": float("inf")},
            {"max_poststrat_weight": 10**10000},
            {"min_population_coverage": -0.1},
            {"min_population_coverage": float("nan")},
            {"min_population_coverage": 10**10000},
        ]
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(SurveyInputError):
                estimate_diffusion_index(rows, **options)

        with self.assertRaisesRegex(SurveyInputError, "sample size is too large"):
            estimate_diffusion_index(
                [StratumCounts(10**10000, 5, 5, 1.0)]
            )

    def test_target_shares_cannot_sum_above_one(self) -> None:
        with self.assertRaisesRegex(SurveyInputError, "sum to no more than 1"):
            estimate_diffusion_index(
                [
                    StratumCounts(10, 10, 10, 0.6),
                    StratumCounts(10, 10, 10, 0.5),
                ]
            )

    def test_result_is_deterministic_and_independent_of_row_order(self) -> None:
        rows = [
            StratumCounts(50, 30, 20, 0.4),
            StratumCounts(20, 30, 50, 0.6),
        ]
        first = estimate_diffusion_index(rows)
        second = estimate_diffusion_index(reversed(rows))
        third = estimate_diffusion_index(iter(rows))
        self.assertEqual(first, second)
        self.assertEqual(first, third)


if __name__ == "__main__":
    unittest.main()
