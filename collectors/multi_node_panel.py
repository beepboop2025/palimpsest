"""Multi-node public observation — the same panel, different outside networks.

Researchers outside China compare availability, ranking, fingerprints, HTTP
status, language variant, and time. An observer claiming to be inside China is
invalid. A blocked vantage abstains; it does not rotate identity or path.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from core.observer_class import (
    ObserverClassError,
    blocked_abstention,
    refuse_forbidden,
    validate_observer_class,
)
from core.visibility_event import stamp_visibility_event


SCHEMA_VERSION = "palimpsest-multi-node-panel.v1"
METHOD_VERSION = 1

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


def ingest_observer_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one observer's reading. China-as-sensor raises."""

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
    return stamped


def compare_panel(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare validated outside-China observations of the same locators."""

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    abstained: list[dict[str, Any]] = []
    for raw in rows:
        try:
            item = ingest_observer_row(raw)
        except ObserverClassError as exc:
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
