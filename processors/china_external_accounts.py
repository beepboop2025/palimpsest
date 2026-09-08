"""Private SAFE analysis and a closed metadata-only public projection."""
from __future__ import annotations

import re
from datetime import datetime

from collectors.china_external_accounts import MONTHS, PARSER_VERSION, SCHEMA, digest, load_registry, number, source_policy

PUBLIC_INTERPRETATION = "Collection metadata only. SAFE numeric tables, historical exports and derived findings are retained privately because publication permission has not been established. A retained historical vintage is not evidence of point-in-time availability or concealment."
PUBLIC_ERROR_TYPES = frozenset({"ValueError", "OSError", "RuntimeError", "FetchError"})


def validate_capture(capture: dict) -> None:
    source_policy(capture["url"])
    expected = digest({"url": capture["url"], "raw_sha256": capture["raw_sha256"], "parser_version": capture["parser_version"]})
    if expected != capture["capture_id"]:
        raise ValueError("SAFE capture identity mismatch")
    count, missing, keys = 0, 0, set()
    for row in capture["observations"]:
        expected_key = digest({key: row[key] for key in ("kind", "region", "metric", "period", "unit")})
        if row["observation_key"] != expected_key or expected_key in keys:
            raise ValueError("SAFE duplicate or invalid observation identity")
        keys.add(expected_key)
        value = None if row["status"] == "formula_not_evaluated" else number(row["raw_value"])
        if value != row["value"] or row["status"] not in {"observed", "unavailable", "formula_not_evaluated"}:
            raise ValueError("SAFE source token differs from value")
        if row["status"] == "observed" and value is None or row["status"] == "unavailable" and value is not None:
            raise ValueError("SAFE value status mismatch")
        count += value is not None
        missing += value is None
    if count != capture["numeric_observations"] or missing != capture["unavailable_observations"]:
        raise ValueError("SAFE observation counts mismatch")


def build_private_analysis(captures: list[dict]) -> dict:
    rows, by_capture = [], {}
    for capture in captures:
        validate_capture(capture)
        by_capture[capture["capture_id"]] = capture
        rows.extend({**row, "capture_id": capture["capture_id"]} for row in capture["observations"])
    regional = [r for r in rows if r["kind"] == "regional" and r["unit"] == "USD 100 million"]
    periods = sorted({r["period"] for r in regional})
    panels = []
    for period in periods:
        current = [r for r in regional if r["period"] == period and r["metric"] in {"receipts:total", "payments:total", "balance:total"}]
        grouped = {}
        for row in current:
            grouped.setdefault(row["region"], {})[row["metric"]] = row
        points = []
        for region, metrics in sorted(grouped.items()):
            if set(metrics) != {"receipts:total", "payments:total", "balance:total"} or any(r["value"] is None for r in metrics.values()):
                continue
            receipt, payment, balance = (metrics[k]["value"] for k in ("receipts:total", "payments:total", "balance:total"))
            if receipt < 0 or payment < 0:
                continue
            points.append({"region": region, "receipts": receipt, "payments": payment, "reported_balance": balance,
                           "derived_balance": receipt - payment, "balance_residual": receipt - payment - balance,
                           "evidence_keys": [r["observation_key"] for r in metrics.values()], "capture_id": metrics["receipts:total"]["capture_id"]})
        complete = len(points) == 36
        total = sum(p["receipts"] for p in points) if complete else None
        top = sorted(points, key=lambda p: p["receipts"], reverse=True)[:5]
        panels.append({"period": period, "reporting_areas": len(points), "complete": complete,
                       "unit": "USD 100 million", "total_receipts": total,
                       "top_five_receipt_share_percent": round(100 * sum(p["receipts"] for p in top) / total, 3) if total and total > 0 else None,
                       "negative_balance_areas": sum(p["reported_balance"] < 0 for p in points) if complete else None,
                       "points": points})
    by_period = {p["period"]: p for p in panels}
    for panel in panels:
        prior_period = f"{int(panel['period'][:4]) - 1}{panel['period'][4:]}"
        prior = by_period.get(prior_period)
        panel["same_month_prior_year"] = prior_period if prior else None
        panel["receipts_year_over_year_percent"] = None
        panel["top_five_share_change_percentage_points"] = None
        if prior and prior["complete"] and panel["complete"]:
            if prior["total_receipts"] and prior["total_receipts"] > 0:
                panel["receipts_year_over_year_percent"] = 100 * (panel["total_receipts"] / prior["total_receipts"] - 1)
            if prior["top_five_receipt_share_percent"] is not None and panel["top_five_receipt_share_percent"] is not None:
                panel["top_five_share_change_percentage_points"] = panel["top_five_receipt_share_percent"] - prior["top_five_receipt_share_percent"]
        previous_points = {p["region"]: p for p in prior["points"]} if prior else {}
        for point in panel["points"]:
            previous = previous_points.get(point["region"])
            point["receipts_year_over_year_percent"] = 100 * (point["receipts"] / previous["receipts"] - 1) if previous and previous["receipts"] > 0 else None
            point["payments_year_over_year_percent"] = 100 * (point["payments"] / previous["payments"] - 1) if previous and previous["payments"] > 0 else None
    bop = [r for r in rows if r["kind"] == "bop" and r["unit"] == "USD 100 million" and r["frequency"] == "quarterly" and r["value"] is not None]
    selected_metrics = ("1. Current account", "3.Net errors and omissions", "2.2.2 Reserve assets")
    series = [{"metric": metric, "unit": "USD 100 million", "points": sorted([r for r in bop if r["metric"].strip() == metric], key=lambda r: r["period"])} for metric in selected_metrics]
    return {"visibility": "private_permission_required", "regional_monthly": panels, "bop_series": series,
            "interpretation": [
                "Reporting areas identify the bank handling the payment, not the home of the ultimate firm or investor.",
                "The 36 areas are non-overlapping: five separately listed cities are excluded from their named provinces.",
                "Payments and receipts are gross flows; their difference is a net payment balance, not a capital-flight estimate.",
                "Year-over-year changes compare the same calendar month and currency; missing prior months and nonpositive bases produce no rate.",
                "Category totals need not equal headline totals because small and late reports can lack category detail.",
                "BOP financial-account assets use SAFE's sign convention: a negative value denotes net asset acquisition.",
                "Net errors and omissions are an accounting residual; they do not establish concealed capital flows.",
                "Historical workbook observations are the current revised vintage; first-seen capture time is not historical availability.",
                "New investment-income, financial-sector and maturity breakdowns begin in 2025; earlier blanks are not zero."]}


def compare_vintages(previous: dict, current: dict) -> dict:
    """Value changes between two captured vintages, never a concealment finding."""
    validate_capture(previous)
    validate_capture(current)
    if previous["kind"] != current["kind"] or previous["source_id"] != current["source_id"]:
        raise ValueError("SAFE vintages must belong to the same statistical series")
    before = {r["observation_key"]: r for r in previous["observations"]}
    after = {r["observation_key"]: r for r in current["observations"]}
    changes = []
    for key in sorted(before.keys() & after.keys()):
        left, right = before[key], after[key]
        if (left["value"], left["status"]) != (right["value"], right["status"]):
            changes.append({"observation_key": key, "region": right["region"], "metric": right["metric"],
                            "period": right["period"], "unit": right["unit"], "before": left["value"], "after": right["value"],
                            "before_status": left["status"], "after_status": right["status"]})
    return {"previous_capture_id": previous["capture_id"], "current_capture_id": current["capture_id"],
            "changed_observations": len(changes), "added_observations": len(after.keys() - before.keys()),
            "no_longer_present_observations": len(before.keys() - after.keys()), "changes": changes,
            "interpretation": "Observed changes between retained workbooks. Publication revisions, coverage changes and corrections need source-specific review; a changed value does not establish concealment."}


def public_projection(*, captures: list[dict], generated_at: str, failures: list[dict], checked_workbooks: int, new_captures: int) -> dict:
    """Explicit field projection: no observation value, token, prose or finding escapes."""
    registry = load_registry()
    sources = []
    for capture in captures:
        validate_capture(capture)
        sources.append({key: capture[key] for key in ("capture_id", "source_id", "kind", "source_page", "url", "attachment_date", "date_basis", "collected_at", "raw_sha256", "raw_bytes", "parser_version", "numeric_observations", "unavailable_observations", "geography_basis", "sheets")})
    output = {"schema": SCHEMA, "generated_at": generated_at,
              "status": "permission_required" if captures else "unavailable",
              "source": {key: registry[key] for key in ("publisher", "independence_group")},
              "rights": registry["rights"],
              "coverage": {"workbooks_retained": len(captures), "sheets_retained": sum(len(c["sheets"]) for c in captures),
                           "numeric_observations_private": sum(c["numeric_observations"] for c in captures),
                           "unavailable_observations_private": sum(c["unavailable_observations"] for c in captures),
                           "public_numeric_observations": 0, "independent_source_groups": 1 if captures else 0},
              "collection": {"checked_at": generated_at, "checked_workbooks": checked_workbooks,
                             "new_captures": new_captures, "failures": failures},
              "sources": sources,
              "interpretation": PUBLIC_INTERPRETATION}
    validate_public(output)
    return output


def validate_public(document: dict) -> None:
    registry = load_registry()
    def exact(value, fields):
        if type(value) is not dict or set(value) != set(fields.split()):
            raise ValueError("unexpected SAFE public fields")
    def text(value, maximum=500):
        if type(value) is not str or not 1 <= len(value) <= maximum or any(ord(c) < 32 for c in value):
            raise ValueError("SAFE metadata must be bounded scalar text")
        return value
    def count(value, maximum=5_000_000):
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("SAFE metadata must be a bounded integer count")
        return value
    def listing(value, maximum):
        if type(value) is not list or len(value) > maximum:
            raise ValueError("SAFE metadata list exceeds its bound")
        return value
    def stamp(value):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text(value, 20)):
            raise ValueError("SAFE metadata timestamp must be canonical UTC")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    exact(document, "schema generated_at status source rights coverage collection sources interpretation")
    if document["schema"] != SCHEMA or document["rights"] != registry["rights"]:
        raise ValueError("SAFE public rights contract mismatch")
    if document["interpretation"] != PUBLIC_INTERPRETATION:
        raise ValueError("SAFE public interpretation must be the fixed metadata-only notice")
    if document["source"] != {key: registry[key] for key in ("publisher", "independence_group")}:
        raise ValueError("SAFE public source mismatch")
    exact(document["coverage"], "workbooks_retained sheets_retained numeric_observations_private unavailable_observations_private public_numeric_observations independent_source_groups")
    if document["coverage"]["public_numeric_observations"] != 0:
        raise ValueError("SAFE public numeric data is not authorized")
    exact(document["collection"], "checked_at checked_workbooks new_captures failures")
    generated = stamp(document["generated_at"])
    if stamp(document["collection"]["checked_at"]) != generated:
        raise ValueError("SAFE collection clocks mismatch")
    if text(document["status"]) not in {"permission_required", "unavailable"}:
        raise ValueError("SAFE permission status mismatch")
    for value in document["coverage"].values():
        count(value)
    count(document["collection"]["checked_workbooks"], 24)
    count(document["collection"]["new_captures"], 24)
    if document["collection"]["new_captures"] > document["collection"]["checked_workbooks"]:
        raise ValueError("SAFE new captures exceed checked workbooks")
    expected_sources = {source["id"]: source for source in registry["sources"]}
    for failure in listing(document["collection"]["failures"], 26):
        exact(failure, "source_id url error_type")
        if text(failure["source_id"]) not in expected_sources or text(failure["error_type"]) not in PUBLIC_ERROR_TYPES:
            raise ValueError("SAFE metadata failure category mismatch")
        source_policy(text(failure["url"]))
    seen = set()
    for source in listing(document["sources"], 24):
        exact(source, "capture_id source_id kind source_page url attachment_date date_basis collected_at raw_sha256 raw_bytes parser_version numeric_observations unavailable_observations geography_basis sheets")
        for key in ("capture_id", "source_id", "kind", "source_page", "url", "attachment_date", "date_basis", "collected_at", "raw_sha256", "parser_version", "geography_basis"):
            text(source[key])
        for key in ("raw_bytes", "numeric_observations", "unavailable_observations"):
            count(source[key], 4 * 1024 * 1024 if key == "raw_bytes" else 400_000)
        if source["raw_bytes"] == 0:
            raise ValueError("SAFE raw workbook size must be positive")
        source_policy(source["url"])
        source_policy(source["source_page"])
        if source["capture_id"] in seen:
            raise ValueError("SAFE duplicate public capture identity")
        seen.add(source["capture_id"])
        known = expected_sources.get(source["source_id"])
        if not known or source["kind"] != known["kind"] or source["source_page"] != known["url"] or source["parser_version"] != PARSER_VERSION:
            raise ValueError("SAFE metadata source identity mismatch")
        collected = stamp(source["collected_at"])
        if collected > generated or source["attachment_date"] > collected.date().isoformat():
            raise ValueError("SAFE metadata evidence clocks mismatch")
        file_date = re.search(r"/file/(\d{8})/", source["url"])
        if not file_date or datetime.strptime(file_date[1], "%Y%m%d").date().isoformat() != source["attachment_date"]:
            raise ValueError("SAFE attachment date differs from its URL")
        if source["date_basis"] != "attachment URL date; not an independently verified publication timestamp":
            raise ValueError("SAFE attachment date basis was relabeled")
        expected_geography = "reporting bank location; five separately listed cities excluded from their provinces" if source["kind"] == "regional" else "national external accounts"
        if source["geography_basis"] != expected_geography:
            raise ValueError("SAFE reporting geography was relabeled")
        if not re.fullmatch(r"[0-9a-f]{64}", source["raw_sha256"]):
            raise ValueError("SAFE public raw digest mismatch")
        if source["capture_id"] != digest({"url": source["url"], "raw_sha256": source["raw_sha256"], "parser_version": source["parser_version"]}):
            raise ValueError("SAFE public capture identity mismatch")
        sheet_names = set()
        for sheet in listing(source["sheets"], 24):
            exact(sheet, "sheet unit frequency observations numeric_observations period_start period_end")
            for key in ("sheet", "unit", "frequency", "period_start", "period_end"):
                text(sheet[key], 40)
            if sheet["sheet"] in sheet_names:
                raise ValueError("SAFE duplicate public worksheet")
            sheet_names.add(sheet["sheet"])
            if sheet["unit"] not in {"USD 100 million", "CNY 100 million", "SDR 100 million"} or sheet["frequency"] not in {"monthly", "annual", "quarterly"}:
                raise ValueError("SAFE metadata unit or frequency mismatch")
            if sheet["sheet"].lower() not in MONTHS and not re.fullmatch(r"(?:annual|quarterly)\((?:RMB|USD|SDR)\)", sheet["sheet"]):
                raise ValueError("SAFE metadata sheet name mismatch")
            for key in ("observations", "numeric_observations"):
                count(sheet[key], 400_000)
            if sheet["numeric_observations"] > sheet["observations"]:
                raise ValueError("SAFE numeric count exceeds requested cells")
            for key in ("period_start", "period_end"):
                if not re.fullmatch(r"(?:19|20)\d{2}(?:Q[1-4]|-(?:0[1-9]|1[0-2]))?", sheet[key]):
                    raise ValueError("SAFE metadata period invalid")
            if sheet["period_start"] > sheet["period_end"]:
                raise ValueError("SAFE metadata period range is reversed")
        if sum(s["numeric_observations"] for s in source["sheets"]) != source["numeric_observations"] or sum(s["observations"] - s["numeric_observations"] for s in source["sheets"]) != source["unavailable_observations"]:
            raise ValueError("SAFE sheet observation counts mismatch")
    if document["coverage"]["workbooks_retained"] != len(document["sources"]):
        raise ValueError("SAFE public workbook count mismatch")
    expected_counts = {"sheets_retained": sum(len(s["sheets"]) for s in document["sources"]),
                       "numeric_observations_private": sum(s["numeric_observations"] for s in document["sources"]),
                       "unavailable_observations_private": sum(s["unavailable_observations"] for s in document["sources"]),
                       "independent_source_groups": 1 if document["sources"] else 0}
    if any(document["coverage"][key] != value for key, value in expected_counts.items()):
        raise ValueError("SAFE public coverage totals mismatch")
