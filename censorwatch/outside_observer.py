"""Outside-China opt-in observers and donation ingest for CensorWatch.

CensorWatch stays feature-flagged and is **not** an in-country China sensor.
This module is the only Greyball extension of the package: it accepts the same
donation / observer payloads the rest of Palimpsest accepts, and only when
``CENSORWATCH_ENABLED`` is set. It does not add an in-country egress path,
does not rotate residential proxies, and does not solve CAPTCHAs.
"""

from __future__ import annotations

from typing import Any, Mapping

from censorwatch.config import get_settings, is_enabled
from collectors.donation_ingest import DonationRejected, ingest_donation
from collectors.multi_node_panel import ingest_observer_row
from core.observer_class import ObserverClassError, refuse_forbidden


SCHEMA_VERSION = "palimpsest-censorwatch-outside-observer.v1"


def _disabled() -> dict[str, Any]:
    return {
        "status": "disabled",
        "note": "CENSORWATCH_ENABLED not set; outside-observer ingest is inert",
        "in_country_egress": False,
    }


def ingest_outside_donation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Donation ingest behind the CensorWatch flag. China-as-sensor still rejects."""

    if not is_enabled():
        return _disabled()
    settings = get_settings()
    if not settings.enabled:
        return _disabled()
    try:
        accepted = ingest_donation(payload)
    except (DonationRejected, ObserverClassError) as exc:
        return {"status": "rejected", "reason": str(exc), "in_country_egress": False}
    accepted["censorwatch"] = {
        "schema_version": SCHEMA_VERSION,
        "in_country_egress": False,
        "china_sensor": False,
    }
    return accepted


def ingest_outside_observer(row: Mapping[str, Any]) -> dict[str, Any]:
    if not is_enabled():
        return _disabled()
    try:
        return ingest_observer_row(row)
    except ObserverClassError as exc:
        return {"status": "rejected", "reason": str(exc), "in_country_egress": False}


def in_country_egress(*_args, **_kwargs) -> None:
    """There is no in-country egress helper. The name exists so tests can prove it."""

    refuse_forbidden(
        "covert_in_china_collection",
        detail="CensorWatch is not an in-country China sensor",
    )
