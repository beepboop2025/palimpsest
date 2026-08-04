"""Validate and score the China-economic source registry.

The registry is an executable scope boundary, not a shopping list.  It states
which CBB-like domains a source can illuminate, whether the source is genuinely
independent, what access/licensing class applies, and whether Palimpsest's
public-only safety rules permit collection.  Coverage reports count independent
information families so ten mirrors of one NBS series never become ten sources.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse


DOMAINS = frozenset({
    "activity", "firm_health", "investment", "credit", "inflation",
    "labor", "property", "trade", "consumer_digital", "commodities",
    "agriculture", "logistics",
})
DIMENSIONS = frozenset({
    "national", "province", "city_tier", "sector", "firm_size", "ownership",
})
ACCESS_MODES = frozenset({
    "open_keyless", "open_bulk", "public_manual", "licensed", "restricted",
})
IMPLEMENTATION_STATES = frozenset({
    "live", "adapter_ready", "planned", "licensed_adapter", "blocked", "out_of_scope",
})
SAFETY_CLASSES = frozenset({
    "public_aggregate", "public_entity_aggregate", "licensed_aggregate",
    "restricted_nonpublic", "prohibited_personal",
})
CADENCES = frozenset({"event", "daily", "weekly", "monthly", "quarterly", "annual"})

REQUIRED_FIELDS = frozenset({
    "source_id", "name", "publisher", "home_url", "access_mode",
    "implementation", "safety_class", "cadence", "domains", "dimensions",
    "independence_group", "collector", "license_note", "evidence_url",
})


class RegistryError(ValueError):
    pass


def load_registry(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        registry = json.load(fh)
    validate_registry(registry)
    return registry


def validate_registry(registry: dict) -> None:
    if not isinstance(registry, dict) or not isinstance(registry.get("sources"), list):
        raise RegistryError("registry must contain a sources list")
    ids = set()
    for index, source in enumerate(registry["sources"]):
        if not isinstance(source, dict):
            raise RegistryError(f"source {index} must be an object")
        missing = REQUIRED_FIELDS - set(source)
        if missing:
            raise RegistryError(f"source {index} missing {sorted(missing)}")
        sid = source["source_id"]
        if not isinstance(sid, str) or not sid or sid in ids:
            raise RegistryError(f"source_id must be unique and non-empty: {sid!r}")
        ids.add(sid)
        for field in ("home_url", "evidence_url"):
            parsed = urlparse(str(source[field]))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise RegistryError(f"{sid}: {field} must be an http(s) URL")
        if source["access_mode"] not in ACCESS_MODES:
            raise RegistryError(f"{sid}: invalid access_mode")
        if source["implementation"] not in IMPLEMENTATION_STATES:
            raise RegistryError(f"{sid}: invalid implementation")
        if source["safety_class"] not in SAFETY_CLASSES:
            raise RegistryError(f"{sid}: invalid safety_class")
        if source["cadence"] not in CADENCES:
            raise RegistryError(f"{sid}: invalid cadence")
        domains = set(source["domains"])
        dimensions = set(source["dimensions"])
        if not domains or not domains <= DOMAINS:
            raise RegistryError(f"{sid}: invalid or empty domains {sorted(domains)}")
        if not dimensions or not dimensions <= DIMENSIONS:
            raise RegistryError(f"{sid}: invalid or empty dimensions {sorted(dimensions)}")
        if not str(source["independence_group"]).strip():
            raise RegistryError(f"{sid}: independence_group is required")
        if source["safety_class"] in {"restricted_nonpublic", "prohibited_personal"} \
                and source["implementation"] not in {"blocked", "out_of_scope"}:
            raise RegistryError(
                f"{sid}: unsafe/restricted source cannot be an active implementation"
            )
        if source["access_mode"] == "restricted" and source["implementation"] \
                not in {"blocked", "out_of_scope"}:
            raise RegistryError(f"{sid}: restricted access must be blocked/out_of_scope")


def _independent_groups(sources: list[dict]) -> set[str]:
    return {s["independence_group"] for s in sources}


def coverage_report(registry: dict) -> dict:
    validate_registry(registry)
    sources = registry["sources"]
    live_states = {"live"}
    ready_states = live_states | {"adapter_ready"}
    buildable_states = ready_states | {"planned", "licensed_adapter"}
    live = [s for s in sources if s["implementation"] in live_states]
    ready = [s for s in sources if s["implementation"] in ready_states]
    buildable = [s for s in sources if s["implementation"] in buildable_states]

    def _coverage(values, field, universe):
        out = {}
        for item in sorted(universe):
            matched = [s for s in values if item in s[field]]
            out[item] = {
                "sources": len(matched),
                "independent_groups": len(_independent_groups(matched)),
                "source_ids": [s["source_id"] for s in matched],
            }
        return out

    domain_live = _coverage(live, "domains", DOMAINS)
    domain_ready = _coverage(ready, "domains", DOMAINS)
    domain_buildable = _coverage(buildable, "domains", DOMAINS)
    dim_live = _coverage(live, "dimensions", DIMENSIONS)
    dim_ready = _coverage(ready, "dimensions", DIMENSIONS)
    dim_buildable = _coverage(buildable, "dimensions", DIMENSIONS)
    return {
        "registry_version": registry.get("registry_version"),
        "n_sources": len(sources),
        "n_live": len(live),
        "n_adapter_ready": len(ready) - len(live),
        "n_buildable": len(buildable),
        "n_independent_live_groups": len(_independent_groups(live)),
        "domains": {
            "live": domain_live, "adapter_ready": domain_ready,
            "buildable": domain_buildable,
        },
        "dimensions": {
            "live": dim_live, "adapter_ready": dim_ready,
            "buildable": dim_buildable,
        },
        "live_domain_gaps": [
            d for d, row in domain_live.items() if row["independent_groups"] == 0
        ],
        "live_dimension_gaps": [
            d for d, row in dim_live.items() if row["independent_groups"] == 0
        ],
        "adapter_ready_domain_gaps": [
            d for d, row in domain_ready.items() if row["independent_groups"] == 0
        ],
        "blocked_or_out_of_scope": [
            {"source_id": s["source_id"], "reason": s["license_note"]}
            for s in sources if s["implementation"] in {"blocked", "out_of_scope"}
        ],
        "replicability_note": registry.get("replicability_note"),
    }


_CADENCE_POINTS = {
    "event": 5.0, "daily": 5.0, "weekly": 4.0, "monthly": 3.0,
    "quarterly": 2.0, "annual": 0.5,
}


def prioritized_backlog(registry: dict) -> list[dict]:
    """Rank safe, not-yet-live adapters by marginal coverage and timeliness."""
    report = coverage_report(registry)
    active_domain_groups = {
        d: row["independent_groups"] for d, row in report["domains"]["live"].items()
    }
    active_dimension_groups = {
        d: row["independent_groups"] for d, row in report["dimensions"]["live"].items()
    }
    candidates = []
    for source in registry["sources"]:
        if source["implementation"] not in {"planned", "adapter_ready", "licensed_adapter"}:
            continue
        if source["safety_class"] not in {
            "public_aggregate", "public_entity_aggregate", "licensed_aggregate"
        }:
            continue
        domain_gain = sum(3.0 if active_domain_groups[d] == 0 else
                          1.0 / (1.0 + active_domain_groups[d])
                          for d in source["domains"])
        dimension_gain = sum(2.0 if active_dimension_groups[d] == 0 else
                             0.5 / (1.0 + active_dimension_groups[d])
                             for d in source["dimensions"])
        access = {
            "open_keyless": 3.0, "open_bulk": 2.5, "public_manual": 1.0,
            "licensed": 0.5, "restricted": -100.0,
        }[source["access_mode"]]
        vintage = 2.0 if source.get("vintage_support") else 0.0
        score = domain_gain + dimension_gain + _CADENCE_POINTS[source["cadence"]] + access + vintage
        track = (
            "licensed"
            if source["access_mode"] == "licensed"
            or source["implementation"] == "licensed_adapter"
            else "public"
        )
        candidates.append({
            "source_id": source["source_id"],
            "name": source["name"],
            "priority_score": round(score, 3),
            "domains": source["domains"],
            "dimensions": source["dimensions"],
            "cadence": source["cadence"],
            "access_mode": source["access_mode"],
            "implementation": source["implementation"],
            "collector": source["collector"],
            "track": track,
        })
    # Public adapters form the default build queue.  Licensed candidates are a
    # separate procurement track even when their theoretical coverage score is
    # higher; contract negotiation must never block lawful public collection.
    return sorted(
        candidates,
        key=lambda r: (0 if r["track"] == "public" else 1,
                       -r["priority_score"], r["source_id"]),
    )


def independence_collisions(registry: dict) -> dict[str, list[str]]:
    """Expose mirrors/derivatives that must be correlated in downstream fusion."""
    validate_registry(registry)
    groups: dict[str, list[str]] = defaultdict(list)
    for source in registry["sources"]:
        groups[source["independence_group"]].append(source["source_id"])
    return {k: sorted(v) for k, v in sorted(groups.items()) if len(v) > 1}
