"""Reconstruct comparable economic series from hash-bound NBS release tables.

Only explicit source reference periods are used. Repeated publications of a
month are vintages, never additional independent observations.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from datetime import datetime

from collectors.nbs_releases import FAMILIES, number, source_policy
from processors.china_economic_health import validate

MONTHS = {name.lower(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
SCHEMA = "palimpsest.china-economic-history-analysis.v1"


def period(text: str) -> str | None:
    years = re.findall(r"\b(20\d{2})\b", text)
    months = re.findall(r"\b(" + "|".join(MONTHS) + r")\b", text, re.I)
    if len(set(years)) == 1 and months:
        return f"{years[0]}-{MONTHS[months[-1].lower()]:02d}"
    return None


def _point(row: dict, reference: str) -> dict:
    return {"period": reference, "value": row["value"],
            **{key: row[key] for key in ("source_url", "raw_sha256", "released_at", "collected_at",
                                        "table_context", "source_row", "source_column", "row_label", "column_label", "raw_value")}}


def _adjacent(left: str, right: str) -> bool:
    ly, lm = map(int, left.split("-")); ry, rm = map(int, right.split("-"))
    return ry * 12 + rm == ly * 12 + lm + 1


def build_history(snapshot: dict, raw: bytes) -> dict:
    validate(snapshot)
    if len(raw) > 96 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != snapshot["history_export"]["sha256"]:
        raise ValueError("historical table bytes do not match the current source snapshot")
    rows = []
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8"))):
        if len(rows) >= 500000:
            raise ValueError("historical table row limit exceeded")
        source_policy(row["source_url"])
        if row["family"] not in FAMILIES or not re.fullmatch("[0-9a-f]{64}", row["raw_sha256"]):
            raise ValueError("invalid historical source identity")
        # Undo only the exporter spreadsheet-formula prefix, never arbitrary text.
        for key in ("raw_value", "row_label", "column_label", "table_context", "release_title"):
            if row[key].startswith("'") and row[key][1:].lstrip().startswith(("=", "+", "-", "@")):
                row[key] = row[key][1:]
        value = number(row["raw_value"])
        if (row["value"] == "") != (value is None) or (value is not None and float(row["value"]) != value):
            raise ValueError("historical numeric value differs from source token")
        if row["status"] != ("observed" if value is not None else "unavailable"):
            raise ValueError("historical availability differs from source token")
        released, captured = [datetime.fromisoformat(row[k].replace("Z", "+00:00")) for k in ("released_at", "collected_at")]
        if released.tzinfo is None or captured.tzinfo is None or released > captured:
            raise ValueError("invalid historical clocks")
        row["value"] = value
        rows.append(row)
    if sum(row["value"] is not None for row in rows) != snapshot["history_export"]["numeric_cells"]:
        raise ValueError("historical numeric-cell count differs from manifest")

    candidates = defaultdict(list)
    definitions = {}
    housing = defaultdict(list)
    table_years = {}
    table_columns = defaultdict(set)
    for row in rows:
        table_columns[(row["source_url"], row["raw_sha256"], row["table"])].add(row["column_label"].split(" | ")[-1].strip().lower())
    conflicts = []
    for row in rows:
        if row["value"] is None:
            continue
        family, context = row["family"], row["table_context"].lower()
        label, column = row["row_label"].lower(), row["column_label"].lower()
        reference = period(row["release_title"])
        if reference and reference > row["released_at"][:7]:
            raise ValueError("economic reference period follows publication")
        if family == "pmi":
            reference = period(row["row_label"])
            table_key = (row["source_url"], row["raw_sha256"], row["table"])
            if reference:
                table_years[table_key] = reference[:4]
            elif label in MONTHS and table_key in table_years:
                reference = f"{table_years[table_key]}-{MONTHS[label]:02d}"
            if not reference:
                continue
            if reference > row["released_at"][:7]:
                raise ValueError("PMI source period follows publication")
            columns = table_columns[table_key]
            if "non-manufacturing" in context or "nonmanufacturing" in context:
                segment = "nonmanufacturing"
            elif "manufacturing" in context:
                segment = "manufacturing"
            elif columns & {"pmi", "production index", "finished goods inventory index", "import index", "purchase quantity index"}:
                segment = "manufacturing"
            elif columns & {"business activity index", "input price index", "sales price index"}:
                segment = "nonmanufacturing"
            elif {"inventory index", "existing order index", "new export order index", "supplier delivery time index"} <= columns:
                segment = "nonmanufacturing"
            else:
                # A generic orders/employment column alone cannot identify the survey.
                continue
            metric = row["column_label"].split(" | ")[-1].strip()
            metric = re.sub(r"\bRaw Material\b(?!s)", "Raw Materials", metric, flags=re.I)
            key = segment + ":" + re.sub(r"[^a-z0-9]+", "-", metric.lower()).strip("-")
            definitions[key] = {"label": segment.replace("nonmanufacturing", "Non-manufacturing").capitalize() + " · " + metric,
                                "unit": "diffusion index", "window": "month", "family": family}
            if 0 <= row["value"] <= 100:
                candidates[key].append(_point(row, reference))
        elif family == "industrial_profits" and reference and label == "total":
            # NBS uses both English headings for the same finished-goods formula.
            column = column.replace("turnover days for inventory of finished goods",
                                    "turnover days of finished goods inventory")
            for token, key, name, unit, window in (
                ("average collection period", "receivable-days", "Accounts receivable collection period", "days", "period-end"),
                ("turnover days of finished", "inventory-days", "Finished-goods inventory turnover", "days", "period-end"),
                ("asset-liability", "leverage", "Industrial asset-liability ratio", "%", "period-end"),
                ("profit rate of business", "profit-margin", "Industrial profit margin", "%", "year-to-date"),
            ):
                if token in column:
                    definitions[key] = {"label": name, "unit": unit, "window": window, "family": family}
                    candidates[key].append(_point(row, reference))
        elif family == "housing" and reference and "90" not in context and "classification" not in context:
            if re.search(r"(?:preceding|previous|last) month\s*=\s*100", column):
                kind = "resale" if "second-hand" in context or "resale" in context else "new" if "newly" in context else None
                if kind:
                    housing[(kind, row["row_label"])].append(_point(row, reference))

    def dedupe(points, key):
        by_month = defaultdict(list)
        for point in points:
            by_month[point["period"]].append(point)
        result = []
        for month, vintages in sorted(by_month.items()):
            ordered = sorted(vintages, key=lambda p: (p["released_at"], p["collected_at"], p["raw_sha256"]))
            latest = ordered[-1]
            # Two different values in the same latest source are ambiguous.
            same_source = [p for p in ordered if (p["source_url"], p["raw_sha256"]) == (latest["source_url"], latest["raw_sha256"])]
            if len({p["value"] for p in same_source}) > 1:
                conflicts.append({"series": key, "period": month, "reason": "conflicting values within one source"})
                continue
            result.append({**latest, "retained_source_vintages": len({(p["source_url"], p["raw_sha256"]) for p in ordered}),
                           "distinct_reported_values": len({p["value"] for p in ordered})})
        return result

    series = [{"id": key, **definitions[key], "points": dedupe(points, key)} for key, points in sorted(candidates.items())]
    city_series = []
    for (kind, city), points in sorted(housing.items()):
        values = dedupe(points, kind + ":" + city)
        streak = 0
        for i in range(len(values) - 1, -1, -1):
            if values[i]["value"] >= 100 or (i < len(values) - 1 and not _adjacent(values[i]["period"], values[i+1]["period"])):
                break
            streak += 1
        city_series.append({"kind": kind, "city": city, "consecutive_observed_declines": streak, "points": values})

    # Same URL, same table locator, different captured bytes: an observed revision.
    by_url = defaultdict(lambda: defaultdict(dict))
    clocks = {}
    for row in rows:
        identity = (row["source_url"], row["raw_sha256"])
        clocks[identity] = row["collected_at"]
        locator = (row["table"], row["table_context"], row["source_row"], row["source_column"], row["row_label"], row["column_label"])
        by_url[row["source_url"]][row["raw_sha256"]][locator] = row
    revisions = []
    for url, captures in sorted(by_url.items()):
        hashes = sorted(captures, key=lambda h: (clocks[(url,h)], h))
        for before, after in zip(hashes, hashes[1:]):
            changes = []
            for locator in sorted(captures[before].keys() & captures[after].keys()):
                old, new = captures[before][locator], captures[after][locator]
                if old["value"] != new["value"]:
                    changes.append({"row_label": new["row_label"], "column_label": new["column_label"], "before": old["value"], "after": new["value"]})
            if changes:
                revisions.append({"source_url": url, "before_raw_sha256": before, "after_raw_sha256": after,
                                  "before_captured_at": clocks[(url,before)], "after_captured_at": clocks[(url,after)],
                                  "changed_cells": len(changes), "examples": changes[:20]})

    findings = _findings(series, city_series)
    return {"schema": SCHEMA, "generated_at": snapshot["generated_at"], "input_sha256": snapshot["history_export"]["sha256"],
            "coverage": {"archive_numeric_cells": snapshot["history_export"]["numeric_cells"], "archive_vintages": snapshot["history_export"]["vintages"],
                         "comparable_series": len(series), "city_housing_series": len(city_series),
                         "deduplicated_series_months": sum(len(s["points"]) for s in series + city_series),
                         "observed_revision_pairs": len(revisions), "ambiguous_series_months": len(conflicts)},
            "series": series, "housing_history": city_series, "revisions": revisions, "conflicts": conflicts, "findings": findings,
            "limitations": ["Official NBS aggregates remain one source group. Repeated releases are not independent corroboration.",
                            "A revision records changed published values; intent cannot be inferred from a revision alone.",
                            "Latest retained source vintage is used for charts. This is not a point-in-time backtest.",
                            "Housing streaks stop at a missing month. City counts are unweighted and measure prices, not sales volumes.",
                            "Cumulative margins retain year-to-date scope; no monthly flow is inferred by subtracting cumulative ratios."]}


def _findings(series, housing):
    by_id = {s["id"]: s for s in series}
    findings = []
    production = by_id.get("manufacturing:production-index", {}).get("points", [])
    orders = {p["period"]: p for p in by_id.get("manufacturing:new-order-index", {}).get("points", [])}
    compatible = [(p, orders[p["period"]]) for p in production if p["period"] in orders]
    if compatible:
        p, o = compatible[-1]
        findings.append({"id": "output-orders", "title": "Is production running ahead of orders?", "period": p["period"],
                         "text": f"The production diffusion index is {p['value']:g}; new orders are {o['value']:g}. Their spread is {p['value']-o['value']:+.1f} index points.",
                         "interpretation": "A positive spread suggests different survey breadth for output and demand. Check inventories, export orders and realized sales before inferring unwanted production.", "evidence": [p,o]})
    for key, question in (("receivable-days", "Are customers taking longer to pay?"), ("inventory-days", "Is inventory taking longer to clear?")):
        points = by_id.get(key, {}).get("points", [])
        if not points:
            continue
        latest = points[-1]; previous_year = f"{int(latest['period'][:4])-1}{latest['period'][4:]}"
        comparison = next((p for p in points if p["period"] == previous_year), None)
        change = f" This is {latest['value']-comparison['value']:+.1f} days from the same month a year earlier." if comparison else " A same-month prior-year comparison is not yet captured."
        findings.append({"id": key, "title": question, "period": latest["period"], "text": f"The reported measure is {latest['value']:g} days." + change,
                         "interpretation": "Coverage is industrial enterprises above the designated size. Longer collection or turnover can motivate investigation; these aggregates do not identify defaults or concealment.",
                         "evidence": [latest] + ([comparison] if comparison else [])})
    for kind in ("new", "resale"):
        rows = [s for s in housing if s["kind"] == kind and s["points"]]
        if not rows:
            continue
        latest = max(s["points"][-1]["period"] for s in rows)
        current = [s for s in rows if s["points"][-1]["period"] == latest]
        eligible = [s for s in current if len(s["points"]) >= 6 and all(_adjacent(a["period"], b["period"]) for a,b in zip(s["points"][-6:],s["points"][-5:]))]
        persistent = [s for s in eligible if s["consecutive_observed_declines"] >= 6]
        description = (f"{len(persistent)} of {len(eligible)} cities with six consecutive monthly readings ending {latest} have price declines in all six months."
                       if eligible else f"{len(current)} cities have readings for {latest}; none yet has six consecutive captured months, so six-month persistence is unavailable.")
        findings.append({"id": "housing-persistence-" + kind, "title": f"How persistent are {kind}-home price declines?", "period": latest,
                         "text": description,
                         "interpretation": "This measures the persistence of reported monthly falls. Missing months break the streak; the count does not measure national property losses or transactions.",
                         "evidence": [s["points"][-1] for s in current]})
    return findings
