"""Greyball methods stay inert unless PALIMPSEST_GREYBALL_ENABLED is set.

Kept off Celery so pull scripts can import this without the fleet.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def greyball_enabled() -> bool:
    return os.getenv("PALIMPSEST_GREYBALL_ENABLED", "").strip().lower() in _TRUTHY
