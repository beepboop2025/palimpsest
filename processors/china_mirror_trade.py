"""Reproducible trade demand, breadth and composition comparisons."""
from __future__ import annotations

import math
import re
from collections import defaultdict

from collectors.china_mirror_trade import (DATASET_URL, FLOWS, PARTNERS, REPORTERS, RIGHTS,
                                          SOURCE_GROUP, PARSER_VERSION, clock, digest,
                                          source_policy, valid_product)

POINT_FIELDS = "period value_eur weight_kg value_eur_status weight_kg_status value_eur_flag weight_kg_flag snapshot_id source_updated_at collected_at".split()
SOURCE = {"publisher": "Eurostat", "independence_group": SOURCE_GROUP, "dataset": "DS-045409", "dataset_url": DATASET_URL,
          "reporting_basis": "EU declarations concerning trading partners; independently reported of partner customs"}
INTERPRETATION = [
    "EU imports from a partner provide external evidence on that partner's exports; EU exports to a partner provide external evidence on its goods demand. These are EU declarations, not partner-reported records.",
    "EU27 totals overlap member-state figures, and HS4 products overlap HS2 chapters. Do not add overlapping views or both flows to a product total.",
    "Values are nominal euros; weight is net mass converted from source units of 100 kg. Neither measures GDP, domestic household activity, freight routes, or CPEC project utilisation.",
    "Value divided by weight is a unit-value ratio affected by product composition and quality, not a price index. Year-on-year changes compare the same code and calendar month; classification revisions can still affect comparability.",
    "Mirror discrepancies require matched periods, valuation (including CIF/FOB), re-export and partner attribution adjustments. Disagreement or unavailable data alone does not establish concealment.",
    "Source update time is the dataset update, not the release time of every observation. Original first-capture times and immutable source hashes are retained; historical values can be revised.",
]


def change(current, previous):
    return round(100 * (current / previous - 1), 4) if current is not None and previous is not None and previous > 0 else None


def project(rows: list[dict], snapshots: list[dict], *, generated_at: str, failures: list[dict], expected_batches: int) -> dict:
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[k] for k in ("reporter", "partner", "product", "flow"))].append(row)
    series = []
    for key, history in sorted(groups.items()):
        history.sort(key=lambda row: row["period"])
        reported = [r for r in history if r["value_eur"] is not None]
        last = reported[-1] if reported else history[-1]
        prior_period = str(int(last["period"][:4]) - 1) + last["period"][4:]
        prior = next((r for r in history if r["period"] == prior_period), None)
        point = {k: last[k] for k in POINT_FIELDS}
        before = {k: prior[k] for k in POINT_FIELDS} if prior else None
        current_ratio = last["value_eur"] / last["weight_kg"] if last["value_eur"] is not None and last["weight_kg"] and last["weight_kg"] > 0 else None
        prior_ratio = prior["value_eur"] / prior["weight_kg"] if prior and prior["value_eur"] is not None and prior["weight_kg"] and prior["weight_kg"] > 0 else None
        series.append({"series_id": ":".join(key), "reporter": key[0], "partner": key[1], "partner_label": PARTNERS[key[1]],
                       "product": key[2], "product_label": last["product_label"], "flow": key[3], "latest": point,
                       "year_ago": before, "yoy_value_pct": change(last["value_eur"], prior["value_eur"] if prior else None),
                       "yoy_weight_pct": change(last["weight_kg"], prior["weight_kg"] if prior else None),
                       "unit_value_eur_per_kg": round(current_ratio, 6) if current_ratio is not None else None,
                       "yoy_unit_value_pct": change(current_ratio, prior_ratio),
                       "reported_months": len(reported), "unavailable_months": len(history) - len(reported),
                       "requested_through": history[-1]["period"]})
    findings = []
    for row in series:
        if row["reporter"] != "EU27_2020" or row["product"] != "TOTAL":
            continue
        latest = row["latest"]
        direction = "imports from" if row["flow"] == FLOWS["1"] else "exports to"
        growth = row["yoy_value_pct"]
        text = (f"EU27 {direction} {row['partner_label']} were €{latest['value_eur'] / 1e9:.2f} billion in {latest['period']}"
                + (f", {growth:+.1f}% from the same month a year earlier." if growth is not None else "; a comparable year-earlier value is unavailable.")) if latest["value_eur"] is not None else "No reported total in this capture."
        findings.append({"id": "total:" + row["series_id"], "title": f"Europe's {direction} {row['partner_label']}", "text": text,
                         "kind": "mirror_trade", "evidence_series": [row["series_id"]],
                         "limit": "Nominal monthly goods trade reported by the EU; euro exchange rates, valuation and shipping lags can affect changes."})
    # Breadth includes non-overlapping HS2 chapters with comparable values; not totals or HS4 subheadings.
    for partner in PARTNERS:
        for flow in FLOWS.values():
            candidates = [r for r in series if r["reporter"] == "EU27_2020" and r["partner"] == partner and r["flow"] == flow and len(r["product"]) == 2 and r["yoy_value_pct"] is not None]
            if not candidates:
                continue
            latest_period = max(r["latest"]["period"] for r in candidates)
            candidates = [r for r in candidates if r["latest"]["period"] == latest_period]
            rises = sum(r["yoy_value_pct"] > 0 for r in candidates)
            falls = sum(r["yoy_value_pct"] < 0 for r in candidates)
            findings.append({"id": f"breadth:{partner}:{flow}", "title": f"{PARTNERS[partner]}: how broad is the trade change?",
                             "text": f"In {latest_period}, {rises} of {len(candidates)} comparable HS2 chapters increased in euro value and {falls} declined ({flow.replace('_', ' ')}).",
                             "kind": "sector_breadth", "evidence_series": [r["series_id"] for r in candidates],
                             "limit": "Each chapter counts equally; small chapters weigh as much as machinery. Missing and zero year-earlier denominators are excluded. This is not a firm survey."})
    # Interesting divergence requires sufficiently large, comparable HS4 observations.
    divergences = [r for r in series if r["reporter"] == "EU27_2020" and r["partner"] == "CN" and len(r["product"]) == 4
                   and r["yoy_value_pct"] is not None and r["yoy_weight_pct"] is not None and r["latest"]["value_eur"] >= 1_000_000
                   and r["yoy_value_pct"] * r["yoy_weight_pct"] < 0]
    for row in sorted(divergences, key=lambda r: abs(r["yoy_value_pct"] - r["yoy_weight_pct"]), reverse=True)[:8]:
        findings.append({"id": "value-weight:" + row["series_id"], "title": f"HS {row['product']}: value and weight move in opposite directions",
                         "text": f"In {row['latest']['period']}, {row['flow'].replace('_', ' ')} changed {row['yoy_value_pct']:+.1f}% in euro value and {row['yoy_weight_pct']:+.1f}% in net mass from a year earlier. Product: {row['product_label']}.",
                         "kind": "value_weight_divergence", "evidence_series": [row["series_id"]],
                         "limit": "A lead for investigation: price, quality, product mix, exchange rates and classification changes can explain divergence. It is not evidence of falsification."})
    numeric = sum(row[k] is not None for row in rows for k in ("value_eur", "weight_kg"))
    observed_periods = [r["period"] for r in rows if r["value_eur"] is not None or r["weight_kg"] is not None]
    return {"schema": "palimpsest.china-mirror-trade.v1", "generated_at": generated_at,
            "status": "partial" if failures else "available", "source": SOURCE, "rights": RIGHTS,
            "collection": {"checked_at": generated_at, "expected_batches": expected_batches, "available_batches": len(snapshots), "failures": failures},
            "coverage": {"series": len(series), "rows": len(rows), "numeric_cells": numeric, "unavailable_cells": len(rows) * 2 - numeric,
                         "first_reported_period": min(observed_periods) if observed_periods else None,
                         "last_reported_period": max(observed_periods) if observed_periods else None,
                         "reporters": sorted({r["reporter"] for r in rows}), "partners": sorted({r["partner"] for r in rows}),
                         "products": len({r["product"] for r in rows}), "independent_source_groups": 1},
            "snapshots": snapshots, "series": series, "findings": findings, "interpretation": INTERPRETATION}


def publication_source_group(document: dict) -> str:
    """Closed public contract: no generic source allowlist or private raw payload."""
    def exact(value, fields):
        if not isinstance(value, dict) or set(value) != set(fields.split()):
            raise ValueError("unexpected mirror-trade fields")
    def finite(value):
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError("invalid numeric mirror-trade field")
    def hashed(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("invalid evidence hash")
    def string(value, bound=2000):
        if not isinstance(value, str) or len(value) > bound:
            raise ValueError("invalid mirror-trade text")
    exact(document, "schema generated_at status source rights collection coverage snapshots series findings interpretation history_export")
    if (document["schema"] != "palimpsest.china-mirror-trade.v1" or document["source"] != SOURCE
            or document["rights"] != RIGHTS or document["interpretation"] != INTERPRETATION or document["status"] not in {"available", "partial"}):
        raise ValueError("mirror-trade identity or rights mismatch")
    generated = clock(document["generated_at"])
    exact(document["collection"], "checked_at expected_batches available_batches failures")
    if document["collection"]["checked_at"] != document["generated_at"]:
        raise ValueError("collection clocks mismatch")
    for failure in document["collection"]["failures"]:
        exact(failure, "source_url error retained")
        source_policy(failure["source_url"])
        if type(failure["retained"]) is not bool or not isinstance(failure["error"], str) or len(failure["error"]) > 100:
            raise ValueError("invalid failure summary")
    exact(document["coverage"], "series rows numeric_cells unavailable_cells first_reported_period last_reported_period reporters partners products independent_source_groups")
    coverage = document["coverage"]
    for field in ("series", "rows", "numeric_cells", "unavailable_cells", "products", "independent_source_groups"):
        if type(coverage[field]) is not int or coverage[field] < 0:
            raise ValueError("invalid mirror coverage count")
    if coverage["independent_source_groups"] != 1 or coverage["rows"] * 2 != coverage["numeric_cells"] + coverage["unavailable_cells"] or coverage["series"] != len(document["series"]):
        raise ValueError("mirror-trade coverage mismatch")
    if not set(coverage["reporters"]) <= REPORTERS or not set(coverage["partners"]) <= set(PARTNERS):
        raise ValueError("non-EU reporting coverage")
    exact(document["history_export"], "path sha256 rows numeric_cells")
    history = document["history_export"]
    if history["path"] != "/readings/china-mirror-trade-history.csv" or history["rows"] != coverage["rows"] or history["numeric_cells"] != coverage["numeric_cells"]:
        raise ValueError("mirror history contract mismatch")
    hashed(history["sha256"])
    snapshots = {}
    for snapshot in document["snapshots"]:
        exact(snapshot, "snapshot_id source_url raw_sha256 raw_bytes parser_version collected_at source_updated_at rows")
        source_policy(snapshot["source_url"])
        hashed(snapshot["raw_sha256"])
        identity = digest({"source_url": snapshot["source_url"], "raw_sha256": snapshot["raw_sha256"], "parser_version": PARSER_VERSION})
        if snapshot["parser_version"] != PARSER_VERSION or snapshot["snapshot_id"] != identity or snapshot["snapshot_id"] in snapshots:
            raise ValueError("mirror snapshot identity mismatch")
        if type(snapshot["raw_bytes"]) is not int or not 0 < snapshot["raw_bytes"] <= 12 * 1024 * 1024 or type(snapshot["rows"]) is not int or snapshot["rows"] <= 0:
            raise ValueError("invalid source capture bounds")
        if not clock(snapshot["source_updated_at"]) <= clock(snapshot["collected_at"]) <= generated:
            raise ValueError("mirror snapshot clocks mismatch")
        snapshots[snapshot["snapshot_id"]] = snapshot
    if len(snapshots) != document["collection"]["available_batches"] or sum(s["rows"] for s in snapshots.values()) != coverage["rows"]:
        raise ValueError("snapshot coverage mismatch")
    ids = set()
    for series in document["series"]:
        exact(series, "series_id reporter partner partner_label product product_label flow latest year_ago yoy_value_pct yoy_weight_pct unit_value_eur_per_kg yoy_unit_value_pct reported_months unavailable_months requested_through")
        identity = ":".join(series[k] for k in ("reporter", "partner", "product", "flow"))
        if (series["series_id"] != identity or identity in ids or series["reporter"] not in REPORTERS or series["partner"] not in PARTNERS
                or series["partner_label"] != PARTNERS[series["partner"]] or not valid_product(series["product"]) or series["flow"] not in FLOWS.values()):
            raise ValueError("mirror series identity mismatch")
        ids.add(identity)
        string(series["product_label"])
        for point in (series["latest"], series["year_ago"]):
            if point is None:
                continue
            exact(point, " ".join(POINT_FIELDS))
            snapshot = snapshots.get(point["snapshot_id"])
            if not snapshot or any(point[k] != snapshot[k] for k in ("source_updated_at", "collected_at")):
                raise ValueError("missing mirror point provenance")
            query = source_policy(snapshot["source_url"])
            if series["reporter"] not in query["reporter"] or series["partner"] not in query["partner"] or series["product"] not in query["product"]:
                raise ValueError("point is outside captured dimensions")
            if not re.fullmatch(r"20[0-9]{2}-(?:0[1-9]|1[0-2])", point["period"]) or point["period"] > generated.strftime("%Y-%m"):
                raise ValueError("invalid point period")
            for metric in ("value_eur", "weight_kg"):
                finite(point[metric])
                if point[metric] is not None and point[metric] < 0 or point[metric + "_status"] != ("reported" if point[metric] is not None else "unavailable"):
                    raise ValueError("missing value mislabeled")
                string(point[metric + "_flag"], 40)
        current, prior = series["latest"], series["year_ago"]
        if prior and prior["period"] != str(int(current["period"][:4]) - 1) + current["period"][4:]:
            raise ValueError("non-comparable year-ago period")
        for metric, field in (("value_eur", "yoy_value_pct"), ("weight_kg", "yoy_weight_pct")):
            if series[field] != change(current[metric], prior[metric] if prior else None):
                raise ValueError("incorrect trade change")
        for field in ("yoy_value_pct", "yoy_weight_pct", "unit_value_eur_per_kg", "yoy_unit_value_pct"):
            finite(series[field])
        current_ratio = current["value_eur"] / current["weight_kg"] if current["value_eur"] is not None and current["weight_kg"] and current["weight_kg"] > 0 else None
        prior_ratio = prior["value_eur"] / prior["weight_kg"] if prior and prior["value_eur"] is not None and prior["weight_kg"] and prior["weight_kg"] > 0 else None
        if series["unit_value_eur_per_kg"] != (round(current_ratio, 6) if current_ratio is not None else None) or series["yoy_unit_value_pct"] != change(current_ratio, prior_ratio):
            raise ValueError("incorrect unit-value ratio")
    for finding in document["findings"]:
        exact(finding, "id title text kind evidence_series limit")
        if not finding["evidence_series"] or not set(finding["evidence_series"]) <= ids or finding["kind"] not in {"mirror_trade", "sector_breadth", "value_weight_divergence"}:
            raise ValueError("unsupported trade finding")
        for field in ("id", "title", "text", "limit"):
            string(finding[field])
    return SOURCE_GROUP
