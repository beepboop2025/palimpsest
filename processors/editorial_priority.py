"""Human-owned ranking policy for archive-context review candidates.

The evidence pipeline deliberately stops before converting its components into one
editorial score. Weighting archive anomalies, source corroboration, and live instrument
coverage is a product judgment rather than a measurement fact.
"""

from __future__ import annotations

from typing import Any, Mapping


def editorial_priority(features: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded review-priority result, never a truth or publish score.

    Policy seam: choose how Palimpsest should balance archive novelty, independent
    corroboration, and live linked instruments. Keep the output score in 0..100 and
    preserve ``automatic_publication_eligible = False`` in the caller.
    """

    return {
        "status": "unconfigured-human-policy",
        "score": None,
        "meaning": "review priority only; not truth, causality, or publication permission",
    }
