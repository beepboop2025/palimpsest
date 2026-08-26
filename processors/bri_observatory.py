"""Validation and coverage planning for Palimpsest's Belt and Road observatory.

The observatory is a source-and-claim spine, not a database of asserted facts.
It records what a source can establish, its rights and collection state, and
which evidence fields remain missing.  This prevents a project announcement,
loan commitment, disbursement, completed asset and operating asset from being
collapsed into one misleading project count.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class BriRegistryError(ValueError):
    """The BRI source registry or public contract failed closed."""


SOURCE_CLASSES = {
    "official_china", "official_host", "multilateral", "research",
    "civil_society", "legal", "partner",
}
AUTHORITY_ROLES = {
    "primary_record", "administrative_position", "independent_observation",
    "analytical_estimate", "legal_instrument", "partner_aggregate",
}
ACCESS_MODES = {
    "open_download", "open_keyless", "public_manual", "registration",
    "licensed", "restricted",
}
RIGHTS_STATES = {
    "public_domain", "attribution", "open_reuse",
    "official_publication_review_required", "link_only_pending_review",
    "licensed_no_redistribution",
}
IMPLEMENTATION_STATES = {
    "live", "adapter_ready", "planned", "link_only", "blocked",
    "out_of_scope", "repository_ready",
}
CADENCES = {
    "event", "daily", "weekly", "monthly", "quarterly", "annual",
    "irregular", "snapshot",
}
CLAIM_CLASSES = {
    "official_statistic", "official_position", "project_register",
    "administrative_action", "allegation", "reported_event", "licensed_event",
    "modeled_estimate", "analytical_estimate", "legal_status",
    "partner_aggregate", "humanitarian_observation",
}
COVERAGE_AXES = {
    "project_identity", "project_status", "contract", "finance_commitment",
    "disbursement", "debt", "ownership", "contractor", "procurement",
    "trade", "port_logistics", "energy", "fiscal", "employment", "land",
    "compensation", "livelihood", "environment", "human_rights", "conflict",
    "elections", "legal_designation", "drug_market", "humanitarian",
    "remote_sensing", "corrections",
}
SAFE_PUBLIC_RIGHTS = {"public_domain", "attribution", "open_reuse"}
PUBLIC_BUILD_STATES = {"live", "adapter_ready", "repository_ready"}
REQUIRED_ROOT = {
    "schema_version", "as_of", "purpose", "publication_policy",
    "project_fields", "economic_metrics", "local_impact_fields",
    "movement_taxonomy", "geographies", "workstreams", "watch_targets",
    "partner_bridges", "sources",
}
REQUIRED_SOURCE = {
    "source_id", "name", "publisher", "url", "source_class",
    "authority_role", "access_mode", "rights_status", "implementation",
    "cadence", "geographies", "coverage", "independence_group",
    "claim_classes", "notes",
}


def load_registry(path: str | Path) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BriRegistryError(f"cannot load BRI registry: {exc}") from exc
    validate_registry(document)
    return document


def _nonempty_strings(value: Any, path: str) -> list[str]:
    if type(value) is not list or not value:
        raise BriRegistryError(f"{path} must be a non-empty array")
    if any(type(item) is not str or not item.strip() for item in value):
        raise BriRegistryError(f"{path} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise BriRegistryError(f"{path} contains duplicates")
    return value


def _https(value: Any, path: str) -> str:
    if type(value) is not str:
        raise BriRegistryError(f"{path} must be a URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise BriRegistryError(f"{path} must be a public HTTPS URL")
    return value


def validate_registry(document: Any) -> dict[str, Any]:
    if type(document) is not dict or set(document) != REQUIRED_ROOT:
        actual = set(document) if type(document) is dict else set()
        raise BriRegistryError(
            f"registry fields differ: missing={sorted(REQUIRED_ROOT - actual)}, "
            f"unknown={sorted(actual - REQUIRED_ROOT)}"
        )
    if document["schema_version"] != "palimpsest.bri-source-registry.v1":
        raise BriRegistryError("unexpected BRI registry schema")
    try:
        cutoff = datetime.fromisoformat(str(document["as_of"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BriRegistryError("as_of must be an ISO-8601 timestamp") from exc
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise BriRegistryError("as_of must include a timezone")
    project_fields = set(_nonempty_strings(document["project_fields"], "project_fields"))
    economic_metrics = set(_nonempty_strings(document["economic_metrics"], "economic_metrics"))
    impact_fields = set(_nonempty_strings(document["local_impact_fields"], "local_impact_fields"))
    required_project = {
        "identity", "host_country", "corridor", "sector", "announced_at",
        "approval_status", "contract_status", "finance_status",
        "implementation_status", "operating_status", "owner", "lender",
        "contractor", "committed_amount", "disbursed_amount", "currency",
        "source_release_at", "collected_at", "revision", "rights",
    }
    if not required_project <= project_fields:
        raise BriRegistryError(
            f"project field contract is incomplete: {sorted(required_project - project_fields)}"
        )
    if not {"jobs_promised", "jobs_observed", "land", "compensation", "grievances", "fisheries", "water"} <= impact_fields:
        raise BriRegistryError("local-impact contract is incomplete")
    if not {"commitment", "disbursement", "debt_service", "trade", "port_throughput", "fiscal_exposure"} <= economic_metrics:
        raise BriRegistryError("economic metric contract is incomplete")

    policy = document["publication_policy"]
    if type(policy) is not dict:
        raise BriRegistryError("publication_policy must be an object")
    if policy.get("project_total_rule") != "never_mix_lifecycle_states":
        raise BriRegistryError("project totals must keep lifecycle states separate")
    if policy.get("conflict_location_grain") != "delayed_administrative_area_only":
        raise BriRegistryError("conflict publication must use delayed administrative-area grain")
    if policy.get("person_level_dossiers") != "prohibited":
        raise BriRegistryError("person-level dossiers must be prohibited")

    taxonomy = document["movement_taxonomy"]
    if type(taxonomy) is not dict or taxonomy.get("umbrella_term_policy") != "concept_only_never_single_actor":
        raise BriRegistryError("Balochistan umbrella term must never be modeled as one actor")
    lanes = taxonomy.get("lanes")
    if type(lanes) is not list:
        raise BriRegistryError("movement taxonomy lanes must be an array")
    lane_ids = {lane.get("lane_id") for lane in lanes if type(lane) is dict}
    required_lanes = {
        "electoral_politics", "peaceful_civic_advocacy", "armed_organizations",
        "state_actions", "legal_designations", "rights_and_humanitarian",
        "political_economy_and_local_impact",
    }
    if not required_lanes <= lane_ids:
        raise BriRegistryError(f"movement taxonomy lacks {sorted(required_lanes - lane_ids)}")
    armed = next(lane for lane in lanes if lane.get("lane_id") == "armed_organizations")
    if "peaceful_civic_advocacy" not in armed.get("prohibited_merges", []):
        raise BriRegistryError("armed organizations must not absorb peaceful civic advocacy")

    geographies = document["geographies"]
    if type(geographies) is not list:
        raise BriRegistryError("geographies must be an array")
    geography_ids = set()
    for index, geography in enumerate(geographies):
        if type(geography) is not dict or set(geography) != {"id", "label", "kind", "parent", "priority"}:
            raise BriRegistryError(f"geographies[{index}] has an invalid shape")
        gid = geography["id"]
        if type(gid) is not str or not gid or gid in geography_ids:
            raise BriRegistryError(f"invalid duplicate geography {gid!r}")
        geography_ids.add(gid)
    required_geographies = {"GLOBAL", "CHN", "PAK", "MMR", "PAK-BAL", "PAK-GWD", "MMR-RKH"}
    if not required_geographies <= geography_ids:
        raise BriRegistryError("priority BRI geographies are incomplete")

    sources = document["sources"]
    if type(sources) is not list or not sources:
        raise BriRegistryError("sources must be a non-empty array")
    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        if type(source) is not dict or set(source) != REQUIRED_SOURCE:
            actual = set(source) if type(source) is dict else set()
            raise BriRegistryError(
                f"source {index} fields differ: missing={sorted(REQUIRED_SOURCE - actual)}, "
                f"unknown={sorted(actual - REQUIRED_SOURCE)}"
            )
        sid = source["source_id"]
        if type(sid) is not str or not sid or sid in source_ids:
            raise BriRegistryError(f"source_id must be unique: {sid!r}")
        source_ids.add(sid)
        _https(source["url"], f"{sid}.url")
        if source["source_class"] not in SOURCE_CLASSES:
            raise BriRegistryError(f"{sid}: invalid source_class")
        if source["authority_role"] not in AUTHORITY_ROLES:
            raise BriRegistryError(f"{sid}: invalid authority_role")
        if source["access_mode"] not in ACCESS_MODES:
            raise BriRegistryError(f"{sid}: invalid access_mode")
        if source["rights_status"] not in RIGHTS_STATES:
            raise BriRegistryError(f"{sid}: invalid rights_status")
        if source["implementation"] not in IMPLEMENTATION_STATES:
            raise BriRegistryError(f"{sid}: invalid implementation")
        if source["cadence"] not in CADENCES:
            raise BriRegistryError(f"{sid}: invalid cadence")
        if not set(_nonempty_strings(source["geographies"], f"{sid}.geographies")) <= geography_ids:
            raise BriRegistryError(f"{sid}: unknown geography")
        coverage = set(_nonempty_strings(source["coverage"], f"{sid}.coverage"))
        if not coverage <= COVERAGE_AXES:
            raise BriRegistryError(f"{sid}: invalid coverage {sorted(coverage - COVERAGE_AXES)}")
        claims = set(_nonempty_strings(source["claim_classes"], f"{sid}.claim_classes"))
        if not claims <= CLAIM_CLASSES:
            raise BriRegistryError(f"{sid}: invalid claim classes {sorted(claims - CLAIM_CLASSES)}")
        if not str(source["independence_group"]).strip() or not str(source["notes"]).strip():
            raise BriRegistryError(f"{sid}: independence group and notes are required")
        if source["implementation"] in PUBLIC_BUILD_STATES and source["rights_status"] not in SAFE_PUBLIC_RIGHTS:
            raise BriRegistryError(f"{sid}: build-ready source lacks public reuse rights")
        if source["access_mode"] in {"licensed", "restricted"} and source["implementation"] not in {"blocked", "out_of_scope"}:
            raise BriRegistryError(f"{sid}: licensed/restricted source must stay blocked or out of scope")
        if "allegation" in claims and source["authority_role"] == "legal_instrument":
            raise BriRegistryError(f"{sid}: allegations cannot be relabeled as legal findings")
        if "administrative_action" in claims and "legal_status" in claims:
            raise BriRegistryError(f"{sid}: administrative action and legal status must remain distinct")

    for section in ("workstreams", "watch_targets"):
        rows = document[section]
        if type(rows) is not list or not rows:
            raise BriRegistryError(f"{section} must be a non-empty array")
        ids: set[str] = set()
        key = "workstream_id" if section == "workstreams" else "target_id"
        for index, row in enumerate(rows):
            if type(row) is not dict or type(row.get(key)) is not str or row[key] in ids:
                raise BriRegistryError(f"{section}[{index}] has an invalid or duplicate id")
            ids.add(row[key])
            referenced = row.get("source_ids", [])
            if type(referenced) is not list or not referenced or not set(referenced) <= source_ids:
                raise BriRegistryError(f"{section}.{row[key]} has unknown or empty source_ids")
            required = row.get("required_coverage", [])
            if type(required) is not list or not set(required) <= COVERAGE_AXES:
                raise BriRegistryError(f"{section}.{row[key]} has invalid required_coverage")

    bridges = document["partner_bridges"]
    if type(bridges) is not list or not bridges:
        raise BriRegistryError("partner_bridges must be non-empty")
    corridor = next((item for item in bridges if item.get("contract") == "narcoscope.palimpsest.corridor-aggregate.v2"), None)
    if not corridor or corridor.get("join_policy") != "geography_and_time_only":
        raise BriRegistryError("NarcoScope v2 bridge must be geography-and-time-only")
    if corridor.get("actor_inference") != "prohibited":
        raise BriRegistryError("NarcoScope bridge actor inference must be prohibited")
    return document


def coverage_report(registry: dict[str, Any]) -> dict[str, Any]:
    validate_registry(registry)
    sources = registry["sources"]
    state_counts = Counter(source["implementation"] for source in sources)
    class_counts = Counter(source["source_class"] for source in sources)
    buildable = [source for source in sources if source["implementation"] in PUBLIC_BUILD_STATES]

    def rows_for(axis: str) -> list[dict[str, Any]]:
        return [source for source in sources if axis in source["coverage"]]

    coverage: dict[str, Any] = {}
    for axis in sorted(COVERAGE_AXES):
        matches = rows_for(axis)
        ready = [source for source in matches if source in buildable]
        coverage[axis] = {
            "sources": len(matches),
            "independent_groups": len({source["independence_group"] for source in matches}),
            "build_ready_sources": len(ready),
            "build_ready_independent_groups": len({source["independence_group"] for source in ready}),
            "source_ids": [source["source_id"] for source in matches],
        }
    geography_coverage = {}
    for geography in registry["geographies"]:
        matched = [source for source in sources if geography["id"] in source["geographies"]]
        geography_coverage[geography["id"]] = {
            "sources": len(matched),
            "independent_groups": len({source["independence_group"] for source in matched}),
            "source_ids": [source["source_id"] for source in matched],
        }
    return {
        "source_count": len(sources),
        "implementation_states": dict(sorted(state_counts.items())),
        "source_classes": dict(sorted(class_counts.items())),
        "build_ready_source_count": len(buildable),
        "coverage": coverage,
        "geographies": geography_coverage,
        "build_ready_gaps": [
            axis for axis, row in coverage.items()
            if row["build_ready_independent_groups"] == 0
        ],
    }


def ground_level_priority_adjustment(source: dict[str, Any]) -> float:
    """Return the owner-tunable public-impact component of backlog priority."""
    # Owner-tunable placeholder: these seven values define the field-impact emphasis.
    weights = {
        "compensation": 2.0, "livelihood": 2.0, "human_rights": 2.0,
        "humanitarian": 1.75, "land": 1.5, "employment": 1.25,
        "corrections": 1.0,
    }
    return sum(weights.get(axis, 0.0) for axis in source["coverage"])


def prioritized_backlog(registry: dict[str, Any]) -> list[dict[str, Any]]:
    report = coverage_report(registry)
    ready_groups = {
        axis: row["build_ready_independent_groups"]
        for axis, row in report["coverage"].items()
    }
    geography_priority = {
        geography["id"]: geography["priority"] for geography in registry["geographies"]
    }
    candidates = []
    for source in registry["sources"]:
        if source["implementation"] not in {"planned", "link_only"}:
            continue
        if source["access_mode"] in {"licensed", "restricted"}:
            continue
        marginal = sum(3 if ready_groups[axis] == 0 else 1 / (1 + ready_groups[axis]) for axis in source["coverage"])
        geo = max((geography_priority[item] for item in source["geographies"]), default=0)
        rights = 3 if source["rights_status"] in SAFE_PUBLIC_RIGHTS else 0
        cadence = {"daily": 3, "weekly": 2.75, "monthly": 2.5, "quarterly": 2, "event": 2, "annual": 1, "irregular": .5, "snapshot": .25}[source["cadence"]]
        ground_level = ground_level_priority_adjustment(source)
        candidates.append({
            "source_id": source["source_id"],
            "name": source["name"],
            "priority_score": round(marginal + geo + rights + cadence + ground_level, 3),
            "ground_level_adjustment": round(ground_level, 3),
            "rights_status": source["rights_status"],
            "geographies": source["geographies"],
            "coverage": source["coverage"],
            "next_gate": (
                "rights_review" if source["rights_status"] not in SAFE_PUBLIC_RIGHTS
                else "adapter_implementation"
            ),
        })
    return sorted(candidates, key=lambda item: (-item["priority_score"], item["source_id"]))


def build_public_artifact(registry: dict[str, Any]) -> dict[str, Any]:
    validate_registry(registry)
    return {
        "$schema": "/protocol/belt-and-road-observatory-v1.schema.json",
        "schema_version": "palimpsest.belt-and-road-observatory.v1",
        "as_of": registry["as_of"],
        "scope": registry["purpose"],
        "publication_policy": registry["publication_policy"],
        "coverage_report": coverage_report(registry),
        "project_fields": registry["project_fields"],
        "economic_metrics": registry["economic_metrics"],
        "local_impact_fields": registry["local_impact_fields"],
        "movement_taxonomy": registry["movement_taxonomy"],
        "geographies": registry["geographies"],
        "workstreams": registry["workstreams"],
        "watch_targets": registry["watch_targets"],
        "partner_bridges": registry["partner_bridges"],
        "sources": registry["sources"],
        "prioritized_backlog": prioritized_backlog(registry),
        "interpretation": [
            "This artifact is a coverage and provenance contract, not a claim that every listed source has been ingested.",
            "Announcement, commitment, contract, disbursement, construction, completion and operation are different lifecycle states.",
            "The Balochistan liberation movement is an umbrella research concept, never a single actor or automatic militancy label.",
            "NarcoScope contributes country aggregates only; shared geography or time does not establish an actor or causal relationship.",
        ],
    }


def independence_collisions(registry: dict[str, Any]) -> dict[str, list[str]]:
    validate_registry(registry)
    groups: dict[str, list[str]] = defaultdict(list)
    for source in registry["sources"]:
        groups[source["independence_group"]].append(source["source_id"])
    return {
        group: sorted(source_ids)
        for group, source_ids in sorted(groups.items()) if len(source_ids) > 1
    }
