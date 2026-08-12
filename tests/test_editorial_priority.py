from __future__ import annotations

from processors.editorial_priority import editorial_priority


def _features(**overrides):
    features = {
        "archive_targets": 0,
        "archive_anomaly_max": None,
        "archive_anomalies": 0,
        "linked_signals": 0,
        "live_linked_signals": 0,
        "independent_evidence_groups": 1,
        "evidence_strength_ordinal": 0,
    }
    features.update(overrides)
    return features


def test_distinctive_evidence_backed_lead_outranks_ubiquitous_story():
    distinctive = editorial_priority(
        _features(
            archive_targets=1,
            archive_anomaly_max=20,
            archive_anomalies=1,
            linked_signals=1,
            live_linked_signals=1,
            evidence_strength_ordinal=2,
        )
    )
    ubiquitous = editorial_priority(
        _features(
            independent_evidence_groups=3,
            evidence_strength_ordinal=5,
            linked_signals=2,
            live_linked_signals=2,
        )
    )

    assert distinctive == {
        "status": "configured",
        "score": 69.8,
        "meaning": (
            "review priority only under the high-novelty/high-evidence policy; "
            "not truth, causality, global exclusivity, public importance, or "
            "publication permission"
        ),
    }
    assert distinctive["score"] > ubiquitous["score"] == 50.0


def test_under_coverage_bonus_requires_archive_and_primary_or_measurement_evidence():
    archive_only = editorial_priority(
        _features(archive_targets=1, archive_anomaly_max=20, archive_anomalies=1)
    )
    primary_archive_lead = editorial_priority(
        _features(
            archive_anomaly_max=20,
            archive_anomalies=1,
            archive_targets=1,
            evidence_strength_ordinal=1,
        )
    )
    primary_without_archive = editorial_priority(
        _features(evidence_strength_ordinal=1)
    )

    assert archive_only["score"] == 38.3
    assert primary_archive_lead["score"] == 58.3
    assert primary_without_archive["score"] == 23.3


def test_archive_context_adds_reporting_depth_during_anomaly_warmup():
    no_archive = editorial_priority(_features())
    warming_archive = editorial_priority(
        _features(archive_targets=1, archive_anomaly_max=4.4)
    )

    assert no_archive["score"] == 3.3
    assert warming_archive["score"] == 8.3


def test_primary_undercovered_lead_beats_same_record_after_broad_coverage():
    undercovered = editorial_priority(_features(evidence_strength_ordinal=1))
    broadly_covered = editorial_priority(
        _features(independent_evidence_groups=3, evidence_strength_ordinal=1)
    )

    assert undercovered["score"] == 23.3
    assert broadly_covered["score"] == 20.0


def test_zero_evidence_is_capped_and_malformed_numbers_are_ignored():
    no_evidence = editorial_priority(
        _features(
            archive_targets=99,
            archive_anomaly_max=999,
            archive_anomalies=99,
            independent_evidence_groups=0,
            evidence_strength_ordinal=5,
            linked_signals=99,
            live_linked_signals=99,
        )
    )
    malformed = editorial_priority(
        {
            "archive_targets": True,
            "archive_anomaly_max": float("nan"),
            "archive_anomalies": "many",
            "linked_signals": None,
            "live_linked_signals": float("inf"),
            "independent_evidence_groups": False,
            "evidence_strength_ordinal": -4,
        }
    )

    assert no_evidence["score"] == 25.0
    assert malformed["status"] == "configured"
    assert malformed["score"] == 0.0
