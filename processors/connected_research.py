"""Connect economic evidence and regional reporting around explicit questions.

Only country, theme and publication time organize these records. This module
does not construct relationships between people, movements or illicit markets.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import math

TREND_QUESTIONS = {
    "DT.TDS.DECT.EX.ZS": ("Debt service against export earnings", "A higher ratio means more annual export and primary-income receipts are matched by external debt service. It does not identify the creditor or a project's repayment burden."),
    "FI.RES.TOTL.MO": ("Reserve coverage of imports", "Reserve cover provides a national external-liquidity comparison. Gross reserves do not show every encumbrance or contingent liability."),
    "BN.CAB.XOKA.GD.ZS": ("External financing balance", "The current-account balance measures the national flow relative to GDP. A smaller deficit can reflect weaker imports as well as stronger export earnings."),
    "FP.CPI.TOTL.ZG": ("Household price pressure", "Lower inflation means prices rise more slowly; it does not mean the previous price increases have reversed. This national annual series does not measure local household costs."),
}


def economic_findings(series: list[dict]) -> list[dict]:
    """Compare adjacent observed annual periods, never bridge a missing year."""
    findings = []
    for row in series:
        if row["indicator_id"] not in TREND_QUESTIONS:
            continue
        available = sorted((point for point in row["history"] if point["evidence_state"] == "observed"), key=lambda point: point["period_end"])
        if not available:
            continue
        current = available[-1]
        if type(current["value"]) not in (int, float) or not math.isfinite(current["value"]):
            raise ValueError("regional analysis requires a finite observed value")
        prior = available[-2] if len(available) > 1 and int(current["period_end"][:4]) - int(available[-2]["period_end"][:4]) == 1 else None
        title, interpretation = TREND_QUESTIONS[row["indicator_id"]]
        text = f'{row["country_code"]}: {row["name"]} was {current["value"]:,.2f} {row["unit"]} in {current["period_end"][:4]}.'
        evidence = [current]
        change = None
        if prior is not None:
            if type(prior["value"]) not in (int, float) or not math.isfinite(prior["value"]):
                raise ValueError("regional comparison requires a finite observed value")
            change = current["value"] - prior["value"]
            change_unit = "percentage points" if "%" in row["unit"] or "percent" in row["unit"] else row["unit"]
            direction = "higher" if change > 0 else "lower" if change < 0 else "unchanged"
            text += f' That compares with {prior["value"]:,.2f} in {prior["period_end"][:4]} ({abs(change):,.2f} {change_unit} {direction}).'
            evidence.append(prior)
        else:
            text += " No adjacent observed year is available for a year-to-year comparison."
        findings.append({"id": f'{row["country_code"]}-{row["indicator_id"]}', "title": title,
                         "country_code": row["country_code"], "indicator_id": row["indicator_id"], "text": text,
                         "interpretation": interpretation, "scope": "annual_country_context", "change": change,
                         "latest_requested_state": row["last_requested_period"]["evidence_state"],
                         "evidence": [{**point, "source_url": row["source_url"], "unit": row["unit"]} for point in evidence]})
    return findings

REGIONS = {
    "china": {"title": "China: demand, firms and external pressure", "country": "CHN", "palimpsest_path": "/china/economy/", "narcoscope_tab": "bri",
              "thesis": "Read output alongside sales, profits, inventories and cash collection. A stronger production number alone does not establish stronger household demand or healthier firms.",
              "questions": [
                  ("Household demand", ["retail", "consumer", "consumption", "income", "household"], "Do sales and household income support the increase in production?", ["Monthly household income and spending by province", "Private small-firm revenue and hiring panels"]),
                  ("Firm cash flow", ["profit", "receivable", "bankrupt", "credit", "debt"], "Are reported profits converting into cash, and which industries are carrying the aggregate?", ["Receivables and inventory by industry over comparable periods", "Borrowing applications, rejections and loan terms"]),
                  ("Property and local finance", ["housing", "property", "land", "local government", "lgfv"], "Is price weakness broadening across cities, and what does it imply for land-sale-dependent budgets?", ["City transaction volumes and inventories", "Provincial land-sale receipts and local financing vehicle accounts"]),
                  ("Trade and industrial capacity", ["export", "tariff", "trade", "capacity", "steel"], "Does external demand absorb production, and which sectors face weaker prices or trade barriers?", ["Product-partner customs quantities and unit values", "Capacity utilization and comparable producer prices"])]},
    "cpec": {"title": "CPEC and Gwadar: financing to operation", "country": "PAK", "palimpsest_path": "/belt-and-road/gwadar/", "narcoscope_tab": "pakistan-gwadar",
             "thesis": "Judge projects through financing, delivered infrastructure, actual use and local benefits. Announced investment and completed construction answer different questions from cash flow, cargo demand or household welfare.",
             "questions": [
                 ("Finance and repayment", ["debt", "loan", "finance", "investment", "payment", "imf"], "Which commitments were disbursed, on what terms, and who bears repayment and currency risk?", ["Loan contracts, disbursement and debt-service schedules", "Sovereign guarantees and power-sector payment arrears"]),
                 ("Port and corridor use", ["port", "cargo", "shipping", "trade", "rail", "road", "airport"], "How much traffic uses completed assets, and is utilization sufficient for the operating model?", ["Monthly Gwadar cargo, vessel calls and route-level trade", "Tariffs, operating costs and concession revenue sharing"]),
                 ("Local services and livelihoods", ["water", "power", "electricity", "fish", "job", "land", "protest"], "Are water, electricity, employment and fishing access improving for local residents?", ["District service-delivery outcomes and local hiring shares", "Land compensation and fishing-access records"]),
                 ("Project delivery", ["project", "construction", "complete", "delay", "approve"], "Does each reported milestone change the project's operating status or only its announced plan?", ["Dated engineering completion and operating certificates", "Procurement variations and independently checked timelines"])]},
    "balochistan": {"title": "Balochistan: livelihoods, resources and political life", "country": "PAK", "palimpsest_path": "/belt-and-road/balochistan/", "narcoscope_tab": "balochistan",
                   "thesis": "Examine local outcomes and public accountability alongside resource development and political reporting. Parties, civic campaigns, communities, armed organizations and legal designations need distinct, attributed records.",
                   "questions": [
                       ("Resources and provincial finance", ["mining", "mineral", "reko diq", "saindak", "royalt", "budget", "revenue"], "What revenue reaches the province and districts, and how does it compare with public spending and liabilities?", ["Provincial budget execution and district allocations", "Mining contracts, royalty receipts and environmental obligations"]),
                       ("Services and livelihoods", ["water", "flood", "school", "education", "health", "fish", "job", "power"], "Which districts face persistent service and livelihood deficits?", ["PBS district census and household indicators", "Service availability, local prices and employment data"]),
                       ("Civic and political record", ["protest", "election", "assembly", "party", "dialogue", "activist"], "Which demands and institutional responses are documented, by whom, and with what counterevidence?", ["Original statements, assembly proceedings and court records", "Independent corroboration and affected-community reporting"]),
                       ("Rights and security reporting", ["missing", "disappear", "detain", "custody", "killed", "attack", "rights"], "What is an attributed allegation, an official account, or a documented legal finding?", ["Case-level public findings and independent documentation", "Reliable coverage denominators; reporting volume is not incidence"])]},
    "bri": {"title": "BRI: projects, creditor exposure and host-country outcomes", "country": None, "palimpsest_path": "/belt-and-road/", "narcoscope_tab": "bri",
            "thesis": "Track lending, investment, construction revenue, operating use and host-country fiscal costs separately. Country borrowing and trade trends provide context; project effects require direct project evidence.",
            "questions": [
                ("Lending and investment", ["loan", "lending", "debt", "investment", "finance"], "How are financing flows, creditor terms and borrower repayment capacity changing?", ["Project-level commitments versus disbursements", "Creditor-specific debt stocks and restructuring terms"]),
                ("Energy and industrial projects", ["energy", "power", "solar", "coal", "mineral", "manufactur"], "What is operational, what is still under construction, and where are environmental and fiscal costs recorded?", ["Plant-level operating status and generation", "Contractual liabilities and environmental assessments"]),
                ("Trade and logistics", ["port", "rail", "trade", "shipping", "logistics"], "Are corridor assets being used and do they reduce actual trading costs?", ["Port and border utilization over time", "Comparable transit costs and customs volumes"])]},
    "myanmar": {"title": "Myanmar: corridor economics and separate illicit-economy evidence", "country": "MMR", "palimpsest_path": "/belt-and-road/myanmar/", "narcoscope_tab": "myanmar",
                "thesis": "Read CMEC and Kyaukpyu project records with economic and humanitarian conditions. NarcoScope's official cultivation, seizure and precursor aggregates remain separately attributed measurements.",
                "questions": [
                    ("Corridor and port development", ["cmec", "kyaukpyu", "port", "rail", "project", "investment"], "Which project claims have independently documented financing, land and operating evidence?", ["Contracts and dated project status", "Local livelihood and environmental outcomes"]),
                    ("Economic and humanitarian conditions", ["trade", "border", "electricity", "displac", "food", "price"], "How do disruptions affect trade, household costs and access to services?", ["Comparable border trade and local price series", "Humanitarian access and service data"]),
                    ("Separate official illicit-economy record", ["opium", "drug", "precursor", "seizure", "scam"], "What do official aggregates measure, and which geographic or temporal gaps prevent stronger conclusions?", ["Comparable survey definitions and reporting coverage", "Direct evidence before any claim linking infrastructure and illicit activity"])]},
}


def build_connected(wire: dict, economy: dict, china: dict, partner: dict, input_hashes: dict) -> dict:
    if wire.get("schema") != "palimpsest.regional-research-wire.v1" or economy.get("schema") != "palimpsest.regional-economic-context.v1":
        raise ValueError("connected research input schema mismatch")
    if economy["context_policy"]["aggregate_level"] != "country" or economy["source"]["redistribution_status"] != "allowed_with_attribution":
        raise ValueError("regional economic scope or rights mismatch")
    if partner.get("schemaVersion") != "narcoscope.palimpsest.corridor-aggregate.v2" or partner["disclosure"]["politicalOrArmedActorInference"] != "prohibited":
        raise ValueError("NarcoScope scope contract mismatch")
    now = datetime.fromisoformat(wire["generated_at"].replace("Z", "+00:00"))
    cutoff = now - timedelta(days=30)
    regions = []
    for identity, spec in REGIONS.items():
        items = [row for row in wire["items"] if identity in row["regions"]]
        recent = [row for row in items if datetime.fromisoformat(row["published_at"].replace("Z", "+00:00")) >= cutoff]
        questions = []
        for title, terms, question, missing in spec["questions"]:
            matching = [row for row in items if any(term in row["title"].casefold() for term in terms)]
            questions.append({"title": title, "question": question, "missing_evidence": missing,
                              "matching_captured_items": len(matching),
                              "independence_groups": sorted({row["independence_group"] for row in matching}),
                              "recent_reporting": matching[:5],
                              "assessment": f'{len(matching)} captured reports from {len({row["independence_group"] for row in matching})} publisher groups address this question. These are attributed accounts; the primary records below are needed to resolve it.' if matching else "No matching report in this captured feed window; the topic remains unmeasured."})
        country_series = [row for row in economy["series"] if spec["country"] is None or row["country_code"] == spec["country"]]
        regions.append({"region": identity, **{k: v for k, v in spec.items() if k != "questions"},
                        "captured_items": len(items), "last_30_days_items": len(recent),
                        "last_30_days_independence_groups": len({row["independence_group"] for row in recent}),
                        "latest_publication": items[0]["published_at"] if items else None,
                        "questions": questions, "recent_reporting": recent[:10], "economic_findings": economic_findings(country_series),
                        "national_indicators": [{"country_code": row["country_code"], "indicator_id": row["indicator_id"], "name": row["name"], "unit": row["unit"], "latest_available": row["latest_available"], "last_requested_period": row["last_requested_period"], "source_url": row["source_url"]} for row in country_series]})
    return {"schema": "palimpsest.connected-research.v1", "generated_at": wire["generated_at"],
            "input_sha256": input_hashes,
            "source_clocks": {"regional_collection": wire["generated_at"], "economic_retrieval": economy["generated_at"], "china_analysis": china["generated_at"], "narcoscope_data_as_of": partner["dataAsOf"]},
            "regions": regions, "china_findings": china["findings"], "source_status": wire["sources"],
            "narcoscope": {"artifact_id": partner["artifactId"], "data_as_of": partner["dataAsOf"],
                           "source_url": "https://narcoscope.com/data/narcoscope-palimpsest-corridors-v2.json",
                           "datasets": [{"id": row["datasetId"], "topic": row["topic"], "measurement": row["measurement"], "temporal_coverage": row["temporalCoverage"], "limitations": row["limitations"]} for row in partner["datasets"].values()]},
            "use_policy": {"integration": "shared_research_questions_and_source_navigation", "joins": "country_theme_time_context_only",
                           "actor_inference": "prohibited", "causal_inference": "not_established", "missing_values": "unavailable_not_zero",
                           "rights": "NBS statistical data with attribution; World Bank CC BY 4.0; publisher reporting metadata and links only"},
            "limitations": ["Captured coverage is not exhaustive. Multiple articles or feed endpoints from the same publisher are not independent corroboration.", "National economic series are not Balochistan district or CPEC project measurements.", "No source here replicates China Beige Book's independent respondent panel."]}
