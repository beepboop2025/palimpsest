"""Reproducible analytical cuts of attributed NBS source tables."""
from __future__ import annotations

import re
import math
from datetime import datetime

from collectors.nbs_releases import FAMILIES, INDEX_URL, TERMS_URL, digest, number, source_policy

NBS_GROUP = "nbs_official_statistics"
NBS_PUBLISHER = "National Bureau of Statistics of China"
NBS_RIGHTS = {
    "status": "attributed_statistical_data",
    "terms_url": TERMS_URL,
    "attribution": "Quoted from the website of the National Bureau of Statistics (www.stats.gov.cn)",
    "license": "NBS statistical-data terms; no downstream sublicense",
}


def validate(document: dict) -> None:
    if document.get("schema") != "palimpsest.china-economic-health.v1":
        raise ValueError("unsupported economic-health schema")
    if document.get("source") != {"publisher": NBS_PUBLISHER, "independence_group": NBS_GROUP, "index_url": INDEX_URL}:
        raise ValueError("economic source identity mismatch")
    families = set()
    for release in document["releases"]:
        source_policy(release["source_url"])
        if release["publisher"] != NBS_PUBLISHER or release["independence_group"] != NBS_GROUP or release["family"] not in FAMILIES:
            raise ValueError("economic release identity mismatch")
        if release["family"] in families:
            raise ValueError("multiple current releases for one family")
        families.add(release["family"])
        if not re.fullmatch(r"[0-9a-f]{64}", release["raw_sha256"]):
            raise ValueError("economic source hash missing")
        if release["rights"] != NBS_RIGHTS:
            raise ValueError("economic source rights missing")
        identity = digest({"url": release["source_url"], "raw_sha256": release["raw_sha256"], "parser_version": release["parser_version"]})
        if release["release_id"] != identity:
            raise ValueError("economic release identity hash mismatch")
        clocks = [datetime.fromisoformat(release[key].replace("Z", "+00:00")) for key in ("released_at", "collected_at")]
        if any(clock.tzinfo is None for clock in clocks) or clocks[0] > clocks[1]:
            raise ValueError("economic release clocks are invalid")
        cells = [cell for table in release["tables"] for cell in table["cells"]]
        for cell in cells:
            if cell["value"] != number(cell["raw_value"]):
                raise ValueError("economic value differs from its source token")
            if cell["status"] != ("observed" if cell["value"] is not None else "unavailable"):
                raise ValueError("missing economic value was relabeled")
        if sum(c["value"] is not None for c in cells) != release["numeric_cells"]:
            raise ValueError("economic cell count mismatch")
    if len(families) != document["coverage"]["families_available"]:
        raise ValueError("economic coverage count mismatch")


def publication_source_group(document: dict) -> str:
    """Admit this closed NBS statistical contract, not the Seiche export ledger."""
    validate(document)
    def exact(value, fields):
        if not isinstance(value, dict) or set(value) != set(fields.split()):
            raise ValueError("unexpected NBS publication fields")
    top = "schema generated_at status source collection coverage family_status releases interpretation"
    exact(document, top + (" history_export" if "history_export" in document else ""))
    exact(document["collection"], "checked_at discovered_releases checked_releases successful_releases new_vintages failures")
    for failure in document["collection"]["failures"]:
        exact(failure, "url family error")
    exact(document["coverage"], "families_available families_expected latest_numeric_cells latest_missing_cells retained_vintages retained_numeric_cells independent_source_groups")
    for state in document["family_status"]:
        exact(state, "family label status released_at retained_vintages latest_discovered_url")
    if "history_export" in document:
        exact(document["history_export"], "path sha256 vintages numeric_cells interpretation")
    for release in document["releases"]:
        exact(release, "release_id family title source_url publisher independence_group released_at publisher_time_zone collected_at raw_sha256 raw_bytes parser_version measurement_scope rights tables narrative_metrics numeric_cells missing_cells")
        for table in release["tables"]:
            exact(table, "table_id table_sha256 context columns rows cells")
            for cell in table["cells"]:
                exact(cell, "source_row source_column row_label column_label raw_value value status")
        for metric in release["narrative_metrics"]:
            exact(metric, "firm_size metric source_locator unit value")
            if (metric["firm_size"] not in {"large", "medium", "small"}
                    or metric["metric"] != "manufacturing_pmi"
                    or metric["unit"] != "diffusion index"
                    or type(metric["value"]) not in {int, float}
                    or not math.isfinite(metric["value"])
                    or not 0 <= metric["value"] <= 100):
                raise ValueError("invalid published NBS firm-size PMI")
    return NBS_GROUP


def evidence(release: dict, table: dict, cell: dict) -> dict:
    return {"source_url": release["source_url"], "release_id": release["release_id"],
            "raw_sha256": release["raw_sha256"], "released_at": release["released_at"],
            "collected_at": release["collected_at"], "table_id": table["table_id"],
            "source_row": cell["source_row"], "source_column": cell["source_column"],
            "row_label": cell["row_label"], "column_label": cell["column_label"],
            "value": cell["value"], "raw_value": cell["raw_value"]}


def build_analysis(document: dict) -> dict:
    validate(document)
    releases = {row["family"]: row for row in document["releases"]}
    findings, series, housing = [], [], []
    profits = releases.get("industrial_profits")
    if profits:
        for table in profits["tables"]:
            if "by industry" in table["context"].lower() or table["columns"][0].lower() == "industry":
                points = [evidence(profits, table, c) for c in table["cells"]
                          if "total profits" in c["column_label"].lower()
                          and "growth" in c["column_label"].lower()
                          and c["row_label"].lower() != "total" and c["value"] is not None]
                if points:
                    up, down = sum(p["value"] > 0 for p in points), sum(p["value"] < 0 for p in points)
                    findings.append({"id": "profit-breadth", "title": "How widely are profits growing?",
                                     "text": f"Profits grew in {up} of {len(points)} industries with a reported growth rate; {down} declined. This is an equal-count breadth measure, so a small industry counts as much as a large one.",
                                     "limit": "Industries with a nonnumeric loss-to-profit comparison are excluded from this denominator. Growth rates are year-on-year for the source's cumulative period.",
                                     "evidence": points, "family": "industrial_profits"})
                    series.append({"id": "industry-profit-growth", "label": "Industry profit growth", "unit": "year-on-year %", "points": sorted(points, key=lambda p: p["value"])})
            if any("average collection period" in c.lower() for c in table["columns"]):
                points = [evidence(profits, table, c) for c in table["cells"]
                          if c["row_label"].lower() == "total" and c["value"] is not None
                          and any(term in c["column_label"].lower() for term in ("profit rate", "turnover days", "average collection period", "asset-liability"))]
                by_name = {p["column_label"].split(" | ")[0]: p for p in points}
                if by_name:
                    summary = "; ".join(f"{name}: {p['raw_value']} ({p['column_label'].split(' | ')[-1]})" for name, p in by_name.items())
                    findings.append({"id": "working-capital", "title": "Cash collection and balance-sheet pressure",
                                     "text": summary + ".", "limit": "These are aggregates for industrial enterprises above the designated size. They do not measure individual defaults or small-firm borrowing access.",
                                     "evidence": points, "family": "industrial_profits"})
    pmi = releases.get("pmi")
    if pmi:
        for table in pmi["tables"]:
            points = [c for c in table["cells"] if c["column_label"].split(" | ")[-1].strip().lower() == "pmi" and c["value"] is not None]
            if points:
                latest = points[-1]
                observed = evidence(pmi, table, latest)
                previous = points[-2] if len(points) > 1 else None
                change = f" It changed by {latest['value'] - previous['value']:+.1f} index points from the preceding reported month." if previous else ""
                findings.append({"id": "manufacturing-pmi", "title": "Manufacturing demand and activity",
                                 "text": f"The latest manufacturing PMI is {latest['value']:g}, {'above' if latest['value'] > 50 else 'below' if latest['value'] < 50 else 'at'} the 50 diffusion threshold.{change}",
                                 "limit": "A PMI is a survey diffusion index, not an output growth rate. NBS and CFLP are the source, not an independent Palimpsest business panel.",
                                 "evidence": [observed] + ([evidence(pmi, table, previous)] if previous else []), "family": "pmi"})
                series.append({"id": "manufacturing-pmi", "label": "Manufacturing PMI history", "unit": "diffusion index", "points": [evidence(pmi, table, c) for c in points]})
    homes = releases.get("housing")
    if homes:
        for table in homes["tables"]:
            context = table["context"].lower()
            if any(term in context for term in ("floor space", "90 m", "90 square", "classification")) or any("90m" in column for column in table["columns"]):
                continue
            groups = {}
            for cell in table["cells"]:
                column = cell["column_label"].lower()
                if not re.search(r"(?:preceding|previous|last) month\s*=\s*100", column) or cell["value"] is None:
                    continue
                groups[cell["row_label"]] = evidence(homes, table, cell)
            if len(groups) >= 60:
                points = sorted(groups.values(), key=lambda p: p["row_label"])
                below = sum(p["value"] < 100 for p in points)
                above = sum(p["value"] > 100 for p in points)
                housing.append({"table_id": table["table_id"], "label": table["context"], "cities": len(points),
                                "falling": below, "rising": above, "unchanged": len(points) - below - above, "points": points})
                findings.append({"id": "housing-" + table["table_id"], "title": "Housing prices across cities",
                                 "text": f"In this housing table, prices fell from the previous month in {below} of {len(points)} cities, rose in {above}, and were unchanged in {len(points) - below - above}.",
                                 "limit": table["context"] + ". City counts are unweighted; they do not measure sales volume, local GDP, or a national house-price change.",
                                 "evidence": points, "family": "housing"})
    return {"schema": "palimpsest.china-economic-analysis.v1", "generated_at": document["generated_at"],
            "findings": findings, "series": series, "housing": housing,
            "coverage_comparison": [
                {"dimension": "Industry and sector", "available": "Industrial revenue, costs, profits, production, retail and price tables", "gap": "No consistent private-firm panel across all sectors"},
                {"dimension": "City", "available": "Housing-price tables for 70 cities, when captured", "gap": "Housing coverage is not a city-wide economic survey"},
                {"dimension": "Ownership", "available": "Published state-holding, share-holding, private and foreign-invested aggregate categories", "gap": "Overlapping categories; no respondent-level ownership comparison"},
                {"dimension": "Firm size", "available": "Published large, medium and small enterprise manufacturing PMI, when present", "gap": "No private small-business credit, revenue or hiring panel"},
                {"dimension": "Province by sector", "available": "Not yet collected as a consistent time series", "gap": "Provincial releases and licensed aggregate panels remain a priority"},
                {"dimension": "Credit and shadow finance", "available": "No independent firm-level borrowing-access measurement", "gap": "Rejections, loan terms and informal credit require a licensed aggregate source"}]}
