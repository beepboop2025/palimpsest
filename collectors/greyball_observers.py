"""Outside-China observer registry — the same panel, different outside networks.

Researchers outside China compare availability, ranking, fingerprints, HTTP
status, language variant, and time. An observer claiming to be inside China is
invalid. A blocked vantage abstains; it does not rotate identity or path.

Refuse tokens: ``china_in_country``, ``in_country=true``,
``path_kind=residential_proxy``. Twenty rows from AS24940 (Hetzner) count as
one independent backer, not twenty.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from core.governance import KillSwitch, RateCeiling
from core.observer_class import (
    ForbiddenTechniqueError,
    ObserverClassError,
    blocked_abstention,
    refuse_forbidden,
    validate_observer_class,
)
from core.visibility_event import stamp_visibility_event


SCHEMA_VERSION = "palimpsest-greyball-observers.v1"
METHOD_VERSION = 1
HETZNER_ASN = 24940

COMPARE_FIELDS = (
    "availability",
    "ranking",
    "content_hash",
    "http_status",
    "language",
    "timestamp",
)


def _availability(row: Mapping[str, Any]) -> str:
    return str(row.get("visibility_state") or row.get("availability") or "unknown")


def _asn(row: Mapping[str, Any]) -> int | None:
    value = row.get("asn") or row.get("observer_asn")
    if value is None or value == "":
        return None
    text = str(value).strip().upper().replace("AS", "")
    if not text.isdigit():
        return None
    return int(text)


def independent_backer_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Same ASN is one backer. Hetzner AS24940 collapses to a single key."""

    asn = _asn(row)
    if asn is not None:
        return ("asn", asn)
    return (
        "observer",
        str(row.get("geo") or row.get("observer_geo") or ""),
        str(row.get("observer_class") or ""),
        str(row.get("vantage") or ""),
    )


def independent_backers(rows: Sequence[Mapping[str, Any]]) -> int:
    return len({independent_backer_key(row) for row in rows})


def ingest_observer_row(
    row: Mapping[str, Any],
    *,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
) -> dict[str, Any]:
    """Validate one observer's reading. China-as-sensor raises."""

    kill = kill_switch or KillSwitch()
    kill.require_live()
    ceiling = rate_ceiling or RateCeiling(rate=1.0, capacity=1.0)
    ceiling.acquire()
    if row.get("china_in_country") in (True, 1, "1", "true", "yes", "on"):
        raise ObserverClassError(
            "observer_class rejects China-as-sensor: china_in_country is not a Palimpsest instrument"
        )
    if row.get("in_country") in (True, 1, "1", "true", "yes", "on"):
        raise ObserverClassError(
            "observer_class rejects China-as-sensor: in_country=true is not a Palimpsest instrument"
        )
    path_kind = str(row.get("path_kind") or "").strip().lower().replace("-", "_")
    if path_kind == "residential_proxy":
        refuse_forbidden(
            "residential_proxy_rotation",
            detail="path_kind=residential_proxy is not an outside-China observer",
        )
    if row.get("blocked") or row.get("status") == "blocked":
        abstain = blocked_abstention("blocked")
        abstain["locator"] = row.get("locator") or row.get("url")
        return abstain
    cls = validate_observer_class(
        str(row.get("observer_class") or "outside-china-researcher"),
        geo=row.get("geo") or row.get("observer_geo"),
        country=row.get("country") or row.get("observer_country"),
        vantage=row.get("vantage"),
        claimed_inside_china=row.get("inside_china") or row.get("claimed_inside_china"),
        in_country=row.get("in_country"),
        china_in_country=row.get("china_in_country"),
        path_kind=row.get("path_kind"),
    )
    stamped = stamp_visibility_event(
        dict(row),
        observer_class=cls,
        locator=str(row.get("locator") or row.get("url") or ""),
        http_status=row.get("http_status"),
        content_hash=row.get("content_hash") or row.get("content_sha256") or "",
        visibility_state=row.get("visibility_state") or _availability(row),
        timestamp=row.get("timestamp") or row.get("captured_at"),
        geo=row.get("geo"),
        country=row.get("country"),
    )
    stamped["language"] = row.get("language") or row.get("language_variant")
    stamped["ranking"] = row.get("ranking") or row.get("search_rank")
    stamped["asn"] = _asn(row)
    stamped["independent_backer"] = independent_backer_key(stamped | {"asn": stamped.get("asn") or row.get("asn")})
    return stamped


def compare_panel(
    rows: Sequence[Mapping[str, Any]],
    *,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
) -> dict[str, Any]:
    """Compare validated outside-China observations of the same locators."""

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    abstained: list[dict[str, Any]] = []
    for raw in rows:
        try:
            item = ingest_observer_row(
                raw, kill_switch=kill_switch, rate_ceiling=rate_ceiling
            )
        except (ObserverClassError, ForbiddenTechniqueError) as exc:
            rejected.append({"reason": str(exc), "locator": raw.get("locator") or raw.get("url")})
            continue
        if item.get("status") == "abstained":
            abstained.append(item)
            continue
        accepted.append(item)

    by_locator: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in accepted:
        loc = item.get("locator") or ""
        if loc:
            by_locator[loc].append(item)

    comparisons: list[dict[str, Any]] = []
    for locator, group in sorted(by_locator.items()):
        hashes = {row.get("content_hash") for row in group if row.get("content_hash")}
        states = {row.get("visibility_state") for row in group}
        ranks = {row.get("ranking") for row in group if row.get("ranking") is not None}
        langs = {row.get("language") for row in group if row.get("language")}
        statuses = {row.get("http_status") for row in group}
        anomaly = None
        if len(states) > 1 or len(hashes) > 1:
            anomaly = "visibility_anomaly"
        elif len(ranks) > 1 and all(
            row.get("visibility_state") == "visible" for row in group
        ):
            anomaly = "ranking_suppression"
        comparisons.append(
            {
                "locator": locator,
                "n_observers": len(group),
                "n_independent_backers": independent_backers(group),
                "observer_classes": sorted({row["observer_class"] for row in group}),
                "availability": sorted(states),
                "http_status": sorted(str(s) for s in statuses),
                "language": sorted(str(s) for s in langs),
                "content_hashes": len(hashes),
                "ranking": sorted(ranks, key=lambda v: (isinstance(v, int), str(v))),
                "visibility_label": anomaly,
                "compared": list(COMPARE_FIELDS),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "n_accepted": len(accepted),
        "n_rejected_china_sensor": len(rejected),
        "n_abstained": len(abstained),
        "n_independent_backers": independent_backers(accepted),
        "hetzner_asn": HETZNER_ASN,
        "rejected": rejected,
        "abstained": abstained,
        "comparisons": comparisons,
        "observations": accepted,
    }


def rotate_identity(*_args, **_kwargs) -> None:
    refuse_forbidden(
        "fake_account_network",
        detail="multi-node observation does not rotate identities to evade controls",
    )


def rotate_residential_path(*_args, **_kwargs) -> None:
    refuse_forbidden(
        "residential_proxy_rotation",
        detail="multi-node observation does not rotate paths to evade controls",
    )
