"""Human-owned ranking policy for archive-context review candidates.

The evidence pipeline deliberately stops before converting its components into one
editorial score. Weighting archive anomalies, source corroboration, and live instrument
coverage is a product judgment rather than a measurement fact.
"""

from __future__ import annotations

import math
from typing import Any, Mapping


_ARCHIVE_ANOMALY_THRESHOLD = 4.5
_ARCHIVE_MAGNITUDE_SATURATION = 3 * _ARCHIVE_ANOMALY_THRESHOLD
_EVIDENCE_STRENGTH_POINTS = (0.0, 10.0, 14.0, 18.0, 22.0, 25.0)


def _nonnegative_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return max(0.0, number) if math.isfinite(number) else 0.0


def editorial_priority(features: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded review-priority result, never a truth or publish score.

    The policy favors under-covered, evidence-rich leads: up to 40 points for
    point-in-time archive context and declared anomalies, 35 for evidence quality,
    15 for live linked instruments, and a 10-point discovery bonus when only one
    or two independent groups cover a primary or measurement report. The caller
    still preserves ``automatic_publication_eligible = False``.
    """

    targets = _nonnegative_number(features.get("archive_targets"))
    anomalies = min(_nonnegative_number(features.get("archive_anomalies")), targets)
    anomaly_max = _nonnegative_number(features.get("archive_anomaly_max"))
    groups = _nonnegative_number(features.get("independent_evidence_groups"))
    evidence_ordinal = min(
        int(_nonnegative_number(features.get("evidence_strength_ordinal"))),
        len(_EVIDENCE_STRENGTH_POINTS) - 1,
    )
    linked = _nonnegative_number(features.get("linked_signals"))
    live = min(_nonnegative_number(features.get("live_linked_signals")), linked)

    declared_anomaly = anomalies >= 1 and anomaly_max >= _ARCHIVE_ANOMALY_THRESHOLD
    novelty = 5.0 if targets >= 1 else 0.0
    if declared_anomaly:
        novelty += 25 * min(anomaly_max / _ARCHIVE_MAGNITUDE_SATURATION, 1)
        novelty += 10 * min(anomalies / 2, 1)
    evidence_quality = _EVIDENCE_STRENGTH_POINTS[evidence_ordinal]
    evidence_quality += 10 * min(groups / 3, 1)
    live_context = 15 * min(live / 2, 1)
    under_coverage_bonus = (
        10.0 if 1 <= groups <= 2 and evidence_ordinal >= 1 else 0.0
    )
    score = novelty + evidence_quality + live_context + under_coverage_bonus
    if groups == 0:
        score = min(score, 25.0)

    return {
        "status": "configured",
        "score": round(min(100.0, score), 1),
        "meaning": (
            "review priority only under the high-novelty/high-evidence policy; "
            "not truth, causality, global exclusivity, public importance, or "
            "publication permission"
        ),
    }
