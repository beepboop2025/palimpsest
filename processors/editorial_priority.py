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
_MAX_EVIDENCE_ORDINAL = 5.0


def _nonnegative_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return max(0.0, number) if math.isfinite(number) else 0.0


def editorial_priority(features: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded review-priority result, never a truth or publish score.

    The policy favors under-covered, evidence-rich leads: 40 points for a declared
    archive anomaly, 35 for evidence quality, 15 for live linked instruments, and
    a 10-point discovery bonus when only one or two independent groups cover an
    anomaly-backed primary or measurement report. The caller still preserves
    ``automatic_publication_eligible = False``.
    """

    targets = _nonnegative_number(features.get("archive_targets"))
    anomalies = min(_nonnegative_number(features.get("archive_anomalies")), targets)
    anomaly_max = _nonnegative_number(features.get("archive_anomaly_max"))
    groups = _nonnegative_number(features.get("independent_evidence_groups"))
    evidence = min(
        _nonnegative_number(features.get("evidence_strength_ordinal")),
        _MAX_EVIDENCE_ORDINAL,
    )
    linked = _nonnegative_number(features.get("linked_signals"))
    live = min(_nonnegative_number(features.get("live_linked_signals")), linked)

    declared_anomaly = anomalies >= 1 and anomaly_max >= _ARCHIVE_ANOMALY_THRESHOLD
    novelty = 0.0
    if declared_anomaly:
        novelty = 30 * min(anomaly_max / _ARCHIVE_MAGNITUDE_SATURATION, 1)
        novelty += 10 * min(anomalies / 2, 1)
    evidence_quality = 25 * (evidence / _MAX_EVIDENCE_ORDINAL)
    evidence_quality += 10 * min(groups / 3, 1)
    live_context = 15 * min(live / 2, 1)
    under_coverage_bonus = (
        10.0 if declared_anomaly and 1 <= groups <= 2 and evidence >= 1 else 0.0
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
