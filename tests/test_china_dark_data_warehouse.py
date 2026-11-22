"""Comprehensive test suite for Palimpsest's self-sustaining China Dark Data Warehouse.

Covers:
1. core/partitioned_ledger.py (rollover, cross-partition verification, tamper detection,
   non-destructive window-poll compaction preserving value revisions, disk reserve).
2. collectors/china_dark_data_bis.py (SDMX CSV parsing, shadow-credit spread, offshore
   SPV bond gap, URL allowlist SSRF refusal, per-series fail-soft abstention).
3. collectors/china_portwatch_ais.py (ArcGIS catalog/daily parsing, monthly rollups, URL policy).
4. collectors/china_sanctions_entities.py (US ITA CSL China/HK/MO filtering, UN XML
   China nexus + Azure redirect policy, GLEIF JSON:API parsing, URL policy).
5. collectors/china_energy_macro_dark.py (Ember wide + legacy monthly CSV, IMF DataMapper,
   WB IDS BRI debt, OpenAlex critical-tech decoupling).
6. collectors/china_gdelt_subnational.py (GDELT masterfilelist + zipped TSV parsing,
   province/category tagging, Common Crawl catalog).
7. collectors/lkq_telemetry.py (MOT official rail fallback parsing & discovery).
8. core/fleet_data_bridge.py (local Nemesis + Bulk metrics + SEC extract + remote fleet).
9. scripts/china_dark_data_orchestrator.py (end-to-end JSONL/Parquet/DuckDB materialization
   and fail-closed CFETS zero-leakage guard).
10. scripts/fleet_data_health_guard.py (ledger headroom, freshness & disk alerts).

Offline, deterministic, stdlib + pytest (+ optional duckdb).
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from collectors.china_dark_data_bis import (
    DEFAULT_BIS_CHINA_SERIES,
    _validate_bis_url,
    collect_bis_china,
    compute_offshore_spv_bond_gap,
    compute_shadow_credit_spread,
    parse_bis_sdmx_csv,
)
from collectors.china_energy_macro_dark import (
    collect_imf_datamapper_china,
    collect_openalex_china_tech,
    collect_wb_ids_bri_debt,
    parse_ember_china_csv,
)
from collectors.china_gdelt_subnational import (
    _discover_recent_gdelt_urls,
    collect_common_crawl_index_catalog,
    parse_gdelt_export_zip,
)
from collectors.china_portwatch_ais import (
    _validate_portwatch_url,
    aggregate_monthly_china_ports,
    fetch_daily_port_series,
)
from collectors.china_sanctions_entities import (
    _validate_url as validate_sanctions_url,
    parse_gleif_page,
    parse_ita_csl_china,
    parse_un_sanctions_china,
)
from collectors.lkq_telemetry import find_mot_rail_article, parse_mot_rail
from core.econ_ledger import LedgerIntegrityError
from core.econ_observation import EconomicObservation
from core.fleet_data_bridge import collect_fleet_bridge_snapshot
from core.partitioned_ledger import (
    append_vintages_self_sustaining,
    compact_window_poll_duplicates,
    load_partitioned_observations,
    partitions_dir_for,
    rollover_completed_partitions,
)
from core.safe_fetch import FetchError
from scripts.china_dark_data_orchestrator import (
    _assert_cfets_free,
    materialize_duckdb_and_parquet,
)
from scripts.fleet_data_health_guard import (
    check_ledger_headroom,
    check_warehouse_freshness,
    evaluate_fleet_health,
)


def _make_obs(
    *,
    series_id: str = "cn.bis.total_credit_private_nf_pct_gdp",
    period_start: date = date(2024, 1, 1),
    period_end: date = date(2024, 3, 31),
    value: float = 195.4,
    revision: int = 0,
    collected_offset_days: int = 0,
    url_suffix: str = "r0",
    source_id: str = "bis_china_credit",
) -> EconomicObservation:
    base = datetime(2026, 1, 10, 12, 0, tzinfo=UTC) + timedelta(days=collected_offset_days)
    raw_sha = hashlib.sha256(f"{series_id}:{period_start}:{value}:{url_suffix}".encode()).hexdigest()
    return EconomicObservation(
        series_id=series_id,
        value=value,
        unit="% of GDP",
        frequency="Q",
        period_start=period_start,
        period_end=period_end,
        released_at=base,
        collected_at=base,
        source_id=source_id,
        evidence_url=f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_TC/2.0/Q.CN.P.A.M.770.A?run={url_suffix}",
        revision=revision,
        status="observed",
        geography="CN",
        sector="private_non_financial",
        firm_size="all",
        ownership="all",
        quality=0.98,
        raw_sha256=raw_sha,
        metadata={"family": "credit_shadow_debt"},
    )


# ── 1. Partitioned Ledger & Compaction ────────────────────────────────────────


def test_partitioned_ledger_rollover_verify_and_tamper_detection(tmp_path: Path):
    ledger = tmp_path / "china-econ-observations.jsonl"
    rows = [
        _make_obs(period_start=date(2023, 1, 1), period_end=date(2023, 3, 31), value=188.0, url_suffix="2023q1"),
        _make_obs(period_start=date(2024, 1, 1), period_end=date(2024, 3, 31), value=192.5, url_suffix="2024q1"),
        _make_obs(period_start=date(2025, 1, 1), period_end=date(2025, 3, 31), value=196.1, url_suffix="2025q1"),
        _make_obs(period_start=date(2026, 1, 1), period_end=date(2026, 3, 31), value=198.8, url_suffix="2026q1"),
    ]
    append_vintages_self_sustaining(ledger, rows, min_disk_free_bytes=1024)

    sealed = rollover_completed_partitions(
        ledger,
        keep_recent_years=1,
        min_disk_free_bytes=1024,
        force=True,
    )
    assert [e.partition_key for e in sealed] == ["2023", "2024", "2025"]

    all_loaded = load_partitioned_observations(ledger, include_partitions=True)
    assert len(all_loaded) == 4
    active_only = load_partitioned_observations(ledger, include_partitions=False)
    assert len(active_only) == 1
    assert active_only[0].period_start == date(2026, 1, 1)

    seg = partitions_dir_for(ledger) / "2024.jsonl.gz"
    seg.write_bytes(seg.read_bytes() + b"corrupt")
    with pytest.raises(LedgerIntegrityError, match="SHA-256 mismatch"):
        load_partitioned_observations(ledger, include_partitions=True)


def test_window_poll_compaction_collapses_bloat_and_preserves_revisions(tmp_path: Path):
    ledger = tmp_path / "bloated.jsonl"
    archive_dir = tmp_path / "archive"
    rows = []
    for day in range(25):
        rows.append(
            _make_obs(
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                value=201.5,
                revision=0,
                collected_offset_days=day,
                url_suffix=f"poll-{day}",
            )
        )
    for day in range(25, 30):
        rows.append(
            _make_obs(
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                value=203.2,
                revision=1,
                collected_offset_days=day,
                url_suffix=f"rev-{day}",
            )
        )
    raw = "\n".join(json.dumps(r.to_dict(), sort_keys=True) for r in rows) + "\n"
    ledger.write_text(raw, encoding="utf-8")

    report = compact_window_poll_duplicates(ledger, archive_dir, min_disk_free_bytes=1024)
    assert report.original_rows == 30
    assert report.compacted_rows == 2
    assert report.dropped_same_value_polls == 28
    assert report.preserved_value_revisions == 1
    assert Path(report.archive_path).exists()

    loaded = load_partitioned_observations(ledger, include_partitions=False)
    assert [(r.revision, r.value) for r in loaded] == [(0, 201.5), (1, 203.2)]
    assert loaded[0].collected_at == datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def test_self_sustaining_append_dedups_window_polls(tmp_path: Path):
    ledger = tmp_path / "dedup.jsonl"
    first = _make_obs(value=150.0, collected_offset_days=0, url_suffix="day0")
    dup_same_val = _make_obs(value=150.0, collected_offset_days=1, url_suffix="day1")
    revised = _make_obs(value=151.4, revision=1, collected_offset_days=2, url_suffix="day2")

    w1 = append_vintages_self_sustaining(ledger, [first], min_disk_free_bytes=1024)
    w2 = append_vintages_self_sustaining(ledger, [dup_same_val], min_disk_free_bytes=1024)
    w3 = append_vintages_self_sustaining(ledger, [revised], min_disk_free_bytes=1024)

    assert len(w1) == 1
    assert len(w2) == 0
    assert len(w3) == 1


# ── 2. BIS SDMX China Collector ───────────────────────────────────────────────


def test_bis_sdmx_csv_parse_and_derived_metrics():
    spec_total = next(s for s in DEFAULT_BIS_CHINA_SERIES if s.series_id == "cn.bis.total_credit_private_nf_pct_gdp")
    spec_bank = next(s for s in DEFAULT_BIS_CHINA_SERIES if s.series_id == "cn.bis.bank_credit_private_nf_pct_gdp")
    spec_nat = next(s for s in DEFAULT_BIS_CHINA_SERIES if s.series_id == "cn.bis.intl_debt_sec_nationality_usd_mn")
    spec_res = next(s for s in DEFAULT_BIS_CHINA_SERIES if s.series_id == "cn.bis.intl_debt_sec_residence_usd_mn")

    ts = "2026-11-22T12:00:00Z"
    obs = []
    obs += parse_bis_sdmx_csv(b"TIME_PERIOD,OBS_VALUE\n2025-Q4,205.5\n2026-Q1,208.0\n", spec_total, collected_at=ts, evidence_url=spec_total.url)
    obs += parse_bis_sdmx_csv(b"TIME_PERIOD,OBS_VALUE\n2025-Q4,160.0\n2026-Q1,161.5\n", spec_bank, collected_at=ts, evidence_url=spec_bank.url)
    obs += parse_bis_sdmx_csv(b"TIME_PERIOD,OBS_VALUE\n2026-Q1,1150000\n", spec_nat, collected_at=ts, evidence_url=spec_nat.url)
    obs += parse_bis_sdmx_csv(b"TIME_PERIOD,OBS_VALUE\n2026-Q1,350000\n", spec_res, collected_at=ts, evidence_url=spec_res.url)

    shadow = compute_shadow_credit_spread(obs)
    assert len(shadow) == 2
    assert shadow[-1]["period"] == "2026-Q1"
    assert shadow[-1]["non_bank_shadow_credit_pct_gdp"] == pytest.approx(46.5)

    spv = compute_offshore_spv_bond_gap(obs)
    assert len(spv) == 1
    assert spv[0]["offshore_vehicle_gap_usd_mn"] == pytest.approx(800000.0)


def test_bis_url_policy_refuses_non_bis_hosts():
    with pytest.raises(FetchError):
        _validate_bis_url("https://evil.example/api/v2/data/dataflow/BIS/WS_TC/2.0/Q.CN")
    with pytest.raises(FetchError):
        _validate_bis_url("http://127.0.0.1/api/v2/data/dataflow/BIS/WS_TC/2.0/Q.CN")


def test_bis_collect_fail_soft_per_series():
    specs = DEFAULT_BIS_CHINA_SERIES[:2]

    def fake_fetch(url, **_kwargs):
        if "Q.CN.P.A.M.770.A" in url:
            return b"TIME_PERIOD,OBS_VALUE\n2026-Q1,202.3\n"
        raise FetchError("simulated 503")

    res = collect_bis_china(specs=specs, fetcher=fake_fetch, now="2026-11-22T12:00:00Z")
    assert res["status"] == "partial"
    assert res["summary"]["series_collected"] == 1
    assert res["summary"]["series_failed"] == 1


# ── 3. IMF PortWatch AIS ──────────────────────────────────────────────────────


def test_portwatch_fetch_daily_and_monthly_rollup():
    payload = {
        "features": [
            {
                "attributes": {
                    "portid": "port1116",
                    "portname": "Shanghai",
                    "country": "China",
                    "date": 1735689600000,
                    "year": 2025,
                    "month": 1,
                    "day": 1,
                    "portcalls": 180,
                    "portcalls_container": 95,
                    "portcalls_dry_bulk": 30,
                    "portcalls_tanker": 25,
                    "portcalls_cargo": 150,
                    "import": 450000.0,
                    "export": 620000.0,
                    "import_container": 200000.0,
                    "export_container": 400000.0,
                }
            },
            {
                "attributes": {
                    "portid": "port1116",
                    "portname": "Shanghai",
                    "country": "China",
                    "date": 1735776000000,
                    "year": 2025,
                    "month": 1,
                    "day": 2,
                    "portcalls": 200,
                    "portcalls_container": 110,
                    "portcalls_dry_bulk": 35,
                    "portcalls_tanker": 20,
                    "portcalls_cargo": 165,
                    "import": 500000.0,
                    "export": 680000.0,
                    "import_container": 220000.0,
                    "export_container": 450000.0,
                }
            },
        ]
    }
    rows, _sha = fetch_daily_port_series(
        "port1116",
        start_year=2025,
        fetcher=lambda *_a, **_k: json.dumps(payload).encode("utf-8"),
    )
    assert len(rows) == 2
    assert rows[0]["date"] == "2025-01-01"

    monthly = aggregate_monthly_china_ports(rows)
    assert len(monthly) == 1
    assert monthly[0]["period"] == "2025-01"
    assert monthly[0]["portcalls"] == pytest.approx(380.0)
    assert monthly[0]["export_tonnes"] == pytest.approx(1300000.0)

    with pytest.raises(FetchError):
        _validate_portwatch_url("https://evil.example/arcgis/rest/services/test")


# ── 4. Sanctions & GLEIF Corporate Registry ───────────────────────────────────


def test_ita_csl_un_xml_and_gleif_parsers():
    csl_csv = (
        "_id,source,entity_number,type,programs,name,title,addresses,federal_register_notice,"
        "start_date,end_date,license_requirement,license_policy,remarks,"
        "alt_names,citizenships,nationalities,source_information_url\n"
        "1,Entity List (EL) - Bureau of Industry and Security,E1,Entity,BIS,Shenzhen Chip Corp,,"
        "Shenzhen China,89 FR 123,2025-01-10,,,,,SCC,CN,CN,https://bis.gov\n"
        "2,Specially Designated Nationals (SDN) - Treasury Department,S2,Entity,CMIC,Hong Kong Shell Ltd,,"
        "Central Hong Kong,,2025-02-01,,,,,HKSL,HK,HK,https://ofac.gov\n"
        "3,Specially Designated Nationals (SDN) - Treasury Department,S3,Entity,IRAN,Tehran Logistics,,"
        "Tehran Iran,,2024-05-01,,,,,,IR,IR,https://ofac.gov\n"
    ).encode("utf-8")
    parsed_csl = parse_ita_csl_china(csl_csv)
    assert parsed_csl["total_csl_records"] == 3
    assert parsed_csl["china_nexus_count"] == 2
    assert parsed_csl["by_jurisdiction"]["CN"] == 1
    assert parsed_csl["by_jurisdiction"]["HK"] == 1

    un_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <CONSOLIDATED_LIST dateGenerated="2026-11-22">
      <ENTITIES>
        <ENTITY>
          <DATAID>9991</DATAID>
          <FIRST_NAME>Dandong Shipping Co Ltd</FIRST_NAME>
          <UN_LIST_TYPE>DPRK</UN_LIST_TYPE>
          <REFERENCE_NUMBER>KPe.001</REFERENCE_NUMBER>
          <LISTED_ON>2024-03-01</LISTED_ON>
          <ENTITY_ADDRESS><COUNTRY>China</COUNTRY><CITY>Dandong</CITY></ENTITY_ADDRESS>
        </ENTITY>
      </ENTITIES>
    </CONSOLIDATED_LIST>"""
    un_parsed = parse_un_sanctions_china(un_xml)
    assert un_parsed["china_nexus_count"] == 1
    assert un_parsed["entries"][0]["name"] == "Dandong Shipping Co Ltd"

    # UN redirect host policy: allow /publiclegacyxmlfiles/, refuse arbitrary Azure paths
    validate_sanctions_url("https://unsolprodfiles.blob.core.windows.net/publiclegacyxmlfiles/EN/consolidatedLegacyByPRN.xml")
    with pytest.raises(FetchError):
        validate_sanctions_url("https://unsolprodfiles.blob.core.windows.net/othercontainer/secret.xml")

    gleif_json = json.dumps({
        "meta": {"pagination": {"total": 107302}, "goldenCopy": {"publishDate": "2026-11-22"}},
        "data": [{
            "id": "549300TESTCHINA00001",
            "attributes": {
                "entity": {
                    "legalName": {"name": "China Test Energy Group Co., Ltd."},
                    "status": "ACTIVE",
                    "category": "GENERAL",
                    "legalAddress": {"country": "CN", "city": "Beijing", "region": "CN-BJ"},
                },
                "registration": {"status": "ISSUED", "initialRegistrationDate": "2020-01-01T00:00:00Z"},
            },
        }],
    }).encode("utf-8")
    gleif_parsed = parse_gleif_page(gleif_json, "CN")
    assert gleif_parsed["total_registered_entities"] == 107302
    assert gleif_parsed["sample_entities"][0]["lei"] == "549300TESTCHINA00001"


# ── 5. Energy (Ember Wide + Legacy), IMF Macro, WB IDS & OpenAlex ─────────────


def test_ember_wide_and_legacy_imf_wb_openalex():
    ember_wide = (
        "Area,ISO 3 code,Date,Area type,Electricity source,Is aggregated source,"
        "Generation (TWh),Generation YoY change (TWh),Generation YoY change (%),"
        "Share of generation (%),Share of generation YoY change (% points),"
        "Emissions (MtCO2e),Emissions YoY change (MtCO2e),Emissions YoY change (%),"
        "Share of emissions (%),Emissions intensity (gCO2e/kWh)\n"
        "China,CHN,2026-08-01,Country,Coal,False,480.2,10.1,2.15,57.1,-2.1,435.0,8.2,1.9,92.0,906.0\n"
        "China,CHN,2026-08-01,Country,Clean,True,360.5,45.2,14.3,42.9,2.1,8.5,0.5,6.2,1.8,23.6\n"
        "France,FRA,2026-08-01,Country,Nuclear,False,32.0,1.0,3.2,70.0,0.5,0.2,0.0,0.0,5.0,6.0\n"
    ).encode("utf-8")
    wide_res = parse_ember_china_csv(ember_wide)
    assert wide_res["schema_variant"] == "wide"
    assert wide_res["row_count"] == 2
    assert wide_res["latest_china_snapshot"]["Coal (TWh)"]["value"] == pytest.approx(480.2)

    ember_legacy = (
        "Area,ISO 3 code,Date,Category,Subcategory,Variable,Unit,Value\n"
        "China,CHN,2026-08-01,Electricity generation,Fuel,Coal,TWh,480.2\n"
    ).encode("utf-8")
    leg_res = parse_ember_china_csv(ember_legacy)
    assert leg_res["schema_variant"] == "long"
    assert leg_res["row_count"] == 1

    imf_payload = json.dumps({
        "values": {"GGXWDG_NGDP": {"CHN": {"2024": 88.3, "2025": 93.1}, "PAK": {"2024": 71.2}}}
    }).encode("utf-8")
    imf = collect_imf_datamapper_china(
        indicators=(("GGXWDG_NGDP", "General government gross debt", "% of GDP"),),
        countries=("CHN", "PAK"),
        fetcher=lambda *_a, **_k: imf_payload,
    )
    assert imf["row_count"] == 3
    assert imf["latest_china"]["GGXWDG_NGDP"]["value"] == pytest.approx(93.1)

    wb_payload = json.dumps([
        {"page": 1, "pages": 1, "total": 1},
        [{
            "countryiso3code": "PAK",
            "country": {"id": "PK", "value": "Pakistan"},
            "indicator": {"id": "DT.DOD.DECT.CD", "value": "External debt stocks, total"},
            "date": "2024",
            "value": 130500000000.0,
        }],
    ]).encode("utf-8")
    wb = collect_wb_ids_bri_debt(countries=("PAK",), fetcher=lambda *_a, **_k: wb_payload)
    assert wb["row_count"] >= 1
    assert wb["latest_bri_external_debt"]["PAK"]["external_debt_usd"] == pytest.approx(130500000000.0)

    oa_payload = json.dumps({"group_by": [{"key": "2025", "count": 5000}]}).encode("utf-8")
    oa = collect_openalex_china_tech(queries=(("ai_llm", "llm"),), fetcher=lambda *_a, **_k: oa_payload)
    assert oa["row_count"] == 2
    assert oa["collab_shares"][0]["us_collab_share_pct"] == pytest.approx(100.0)


# ── 6. GDELT Sub-national & Common Crawl ──────────────────────────────────────


def test_gdelt_discovery_parsing_and_common_crawl():
    lastupdate_text = (
        "12345 ghi http://data.gdeltproject.org/gdeltv2/20261122151500.export.CSV.zip\n"
        "12345 def http://data.gdeltproject.org/gdeltv2/20261122151500.mentions.CSV.zip\n"
    )
    urls = _discover_recent_gdelt_urls(lastupdate_text, num_intervals=2)
    assert len(urls) == 2
    assert urls[0].endswith("20261122151500.export.CSV.zip")
    assert urls[1].endswith("20261122150000.export.CSV.zip")

    cols = [""] * 61
    cols[0] = "123456789"
    cols[1] = "20261122"
    cols[6] = "CHINA WORKERS"
    cols[7] = "CHN"
    cols[16] = "GUANGDONG AUTHORITIES"
    cols[17] = "CHN"
    cols[26] = "141"
    cols[28] = "14"
    cols[29] = "3"
    cols[30] = "-6.5"
    cols[31] = "12"
    cols[34] = "-4.2"
    cols[52] = "Guangzhou, Guangdong, China"
    cols[53] = "CH"
    cols[54] = "CH30"
    cols[56] = "23.1291"
    cols[57] = "113.2644"
    cols[60] = "https://example.org/news/guangzhou-factory-strike"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("20261122151500.export.CSV", "\t".join(cols) + "\n")
    events = parse_gdelt_export_zip(buf.getvalue(), source_url=urls[-1])
    assert len(events) == 1
    assert events[0]["category"] == "protest_dissent_strike"
    assert events[0]["action_geo_country"] == "CH"
    assert events[0]["action_geo_adm1"] == "CH30"

    cc_raw = json.dumps([
        {"id": "CC-MAIN-2026-43", "name": "October 2026", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-43-index"},
        {"id": "CC-MAIN-2026-39", "name": "September 2026", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-39-index"},
        {"id": "CC-MAIN-2026-30", "name": "July 2026", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-30-index"},
    ]).encode("utf-8")
    cc = collect_common_crawl_index_catalog(fetcher=lambda *_a, **_k: cc_raw, now="2026-11-22T12:00:00Z")
    assert cc["latest_crawl"] == "CC-MAIN-2026-43"
    assert "CC-MAIN-2026-43" in cc["recommended_index_only_queue"]


# ── 7. LKQ MOT Rail Fallback ──────────────────────────────────────────────────


def test_lkq_mot_rail_fallback_parser_and_discovery():
    mot_html = "<p>1—10月，全国铁路累计完成货运量42.55亿吨，同比增长3.4%。</p>"
    parsed = parse_mot_rail(mot_html)
    assert parsed["rail_freight_yoy"] == pytest.approx(3.4)
    assert parse_mot_rail("<p>无数据</p>") == {}

    listing = (
        '<a href="./202611/t20261118_4100001.html" title="2026年10月全国铁路货运量情况">10月</a>'
        '<a href="./202611/t20261120_4100002.html" title="民航工作会议召开">会议</a>'
    )
    hit = find_mot_rail_article(listing)
    assert hit is not None
    assert hit[0].endswith("t20261118_4100001.html")


# ── 8. Fleet Bridge, DuckDB/Parquet Materializer & CFETS Guard ────────────────


def test_fleet_bridge_orchestrator_and_cfets_isolation_guard(tmp_path: Path):
    nem_path = tmp_path / "nemesis.json"
    nem_path.write_text(json.dumps({"status": "ok", "generated_at": 1795365961.5, "leads": [{"id": 1}], "counts": {"topics": 186}}), encoding="utf-8")
    bulk_path = tmp_path / "bulk_metrics.json"
    bulk_path.write_text(json.dumps({"generated_at": "2026-11-22T16:42:46Z", "edition": "abc", "summary": {"analytical_rows": 12086397}, "lanes": [{"id": "world-bank-wdi", "queryable_records": 9015914}]}), encoding="utf-8")
    sec_path = tmp_path / "sec_china.json"
    sec_path.write_text(json.dumps({"extracted_at": "2026-11-22T16:50:00Z", "total_sec_companies": 9430, "china_hk_cayman_matched_count": 152, "companies": [{"cik": "0001", "name": "CHINA TEST"}]}), encoding="utf-8")

    def fake_fetch(url, **_kwargs):
        if "money-markets" in url:
            return json.dumps({"status": "PARTIAL", "markets": [{"market_id": "US-USD", "status": "LIVE_REFERENCE", "benchmark": {"id": "SOFR", "value": 3.91}}]}).encode("utf-8")
        if "capital_markets" in url:
            return json.dumps({"status": "derived", "as_of": "2026-11-19"}).encode("utf-8")
        if "failure-radar" in url:
            return json.dumps({"as_of": "2026-11-22", "rows": [{"id": "B1"}], "excluded_stale_count": 4}).encode("utf-8")
        if "undertow" in url:
            return json.dumps({"asof": "2026-11-22", "funding_regime": "STRAIN", "segments": {"CN": "PARTIAL"}}).encode("utf-8")
        raise FetchError(f"unexpected url: {url}")

    bridge = collect_fleet_bridge_snapshot(
        nemesis_path=nem_path,
        bulk_metrics_path=bulk_path,
        sec_china_extract_path=sec_path,
        fetcher=fake_fetch,
        now="2026-11-22T12:00:00Z",
    )
    assert bridge["status"] == "ok"
    assert bridge["surfaces"]["palimpsest_nemesis"]["status"] == "ok"
    assert bridge["surfaces"]["palimpsest_bulk_lake"]["summary"]["analytical_rows"] == 12086397
    assert bridge["surfaces"]["hel1_sec_china_issuers"]["china_hk_cayman_matched_companies"] == 152

    wh = materialize_duckdb_and_parquet(
        tmp_path / "wh",
        {
            "bis_china_observations": [{"series_id": "cn.bis.test", "value": 12.3}],
            "us_ita_csl_china_entities": [{"name": "Test Corp", "country": "CN"}],
        },
    )
    assert wh["total_rows"] == 2
    assert (tmp_path / "wh" / "jsonl" / "bis_china_observations.jsonl").exists()
    if wh["duckdb_available"]:
        assert (tmp_path / "wh" / "parquet" / "bis_china_observations.parquet").exists()
        assert Path(wh["duckdb_path"]).exists()

    _assert_cfets_free({"layers": {"bis": "clean"}}, "clean_test")
    with pytest.raises(RuntimeError, match="Rights isolation violation"):
        _assert_cfets_free({"layers": {"bad": "contains cfets_benchmarks"}}, "dirty_test")


# ── 9. Fleet Data Health Guard ────────────────────────────────────────────────


def test_fleet_data_health_guard_alerts(tmp_path: Path):
    big_ledger = tmp_path / "china-econ-observations.jsonl"
    big_ledger.write_bytes(b"x" * 900)
    chk = check_ledger_headroom(big_ledger, max_bytes=1000, warn_ratio=0.7, crit_ratio=0.85)
    assert chk["status"] == "critical"

    wh_dir = tmp_path / "warehouse"
    wh_dir.mkdir()
    (wh_dir / "warehouse-manifest.json").write_text(
        json.dumps({
            "generated_at": "2026-11-20T00:00:00Z",
            "storage": {"total_rows": 50000},
            "layer_errors": [],
        }),
        encoding="utf-8",
    )
    stale = check_warehouse_freshness(
        wh_dir / "warehouse-manifest.json",
        max_age_hours=12.0,
        now=datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
    )
    assert stale["status"] == "stale"

    report = evaluate_fleet_health(
        warehouse_dir=wh_dir,
        ledger_path=big_ledger,
        probe_remote=False,
        now=datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
    )
    assert report["overall_status"] == "critical"
    assert report["alert_count"] >= 1
