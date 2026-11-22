# Palimpsest China Dark Data Warehouse & Fleet Data Architecture

## Overview

Palimpsest's **China Dark Data Warehouse** turns Palimpsest from a single-file JSONL collector suite into a **self-sustaining, partitioned, multi-layer analytical data plane** covering physical activity, shadow credit, offshore corporate vehicles, sanctions/export controls, maritime trade, energy/emissions, Belt & Road sovereign debt, critical-tech decoupling, sub-national events, web disappearance, and live cross-product fleet liquidity signals (Seiche, LiquiLens, Undertow, Financial Collector SEC archive, Nemesis Watch, and Palimpsest Bulk DuckDB).

## Why Existing Data Pipelines Stalled (Root Causes Addressed)

1. **64 MiB Single-File Ledger Ceiling + Rolling-Window Poll Bloat**:
   `core/partitioned_ledger.py` replaces single-file growth failure with:
   - Same-value rolling-window deduplication (`window_poll_retain_same_value`), retaining 100% of new periods and genuine value revisions while preventing 50× duplicate poll inflation.
   - Automatic immutable yearly gzip partition rollover (`<ledger>.partitions/<YYYY>.jsonl.gz`) with SHA-256 verification in `manifest.json`.
   - Non-destructive compaction (`compact_window_poll_duplicates`) that archives a verified byte-exact `.jsonl.gz` snapshot + JSON receipt before rewriting the active ledger.
   - Hard disk-free reserve guard (`DEFAULT_MIN_DISK_FREE_BYTES = 10 GiB`).
2. **Collateral Rights Quarantine**:
   `config/china_econ_source_policy.json` remains untouched (preserving its pinned SHA-256 contract for CFETS segregation). All new rights-cleared open layers register separately in `config/china_dark_data_sources.json` and `config/china_dark_data_policy.json`, and `scripts/china_dark_data_orchestrator.py` enforces a fail-closed `_assert_cfets_free()` gate before writing `china-open-dark-data-latest.json`.
3. **Disconnected Cross-Product Warehouses**:
   `core/fleet_data_bridge.py` unifies local signed Nemesis OSINT snapshots, `fsn1` Bulk DuckDB tables, `hel1` SEC EDGAR China/HK/Cayman 20-F/6-K foreign private issuers, and live Seiche, LiquiLens, and Undertow public endpoints while keeping observation, publication, and collection clocks strictly distinct.

## Nine-Layer China Dark Data Stack

| Layer | Module | Signal / Blind Spot Covered | Storage Format |
|---|---|---|---|
| L1 | `collectors/china_dark_data_bis.py` | BIS 8 SDMX dataflows: Total vs Bank credit (shadow credit/LGFV spread), Credit-to-GDP gap, DSR, Property prices, Broad REER/NEER, Policy rate, Locational Banking claims/liabilities (CN & HK), International Bonds nationality minus residence (Cayman/BVI/HK SPV gap) | DuckDB + ZSTD Parquet + Bitemporal Observations |
| L2 | `collectors/china_portwatch_ais.py` | IMF PortWatch satellite-AIS daily vessel calls & import/export tonnage across Chinese ports + 7 China-critical maritime chokepoints (Taiwan Strait, Malacca, Singapore, Hormuz, Suez, Bab el-Mandeb, Panama) | DuckDB + ZSTD Parquet |
| L3 | `collectors/china_sanctions_entities.py` | US ITA Consolidated Screening List (BIS Entity List, MEU, OFAC SDN, NS-CMIC, DDTC, UVL), UN Consolidated Sanctions China nexus, GLEIF 107,000+ CN LEI entities & Level-2 parent ownership | DuckDB + ZSTD Parquet |
| L4 | `collectors/china_energy_macro_dark.py` | Ember monthly China electricity/coal/solar/wind/hydro/nuclear/CO2, IMF DataMapper China & 24 BRI sovereign fiscal/debt ratios, World Bank IDS BRI bilateral debt, OpenAlex China critical-tech share & US-China decoupling | DuckDB + ZSTD Parquet |
| L5 | `collectors/china_gdelt_subnational.py` | GDELT 2.0 15-minute sub-national China protests, labor unrest, regulatory actions, strait military activity, and Goldstein tension scores | DuckDB + ZSTD Parquet |
| L6 | `collectors/china_gdelt_subnational.py` + `common_crawl_lake.py` | Common Crawl 2024–2026 crawl index catalog for longitudinal `.gov.cn` and 31 provincial statistical bureau disappearance & mutation tracking | SQLite + Manifest |
| L7 | `collectors/lkq_telemetry.py`, `data_darkness.py`, `nbs_housing.py`, `mot_transport.py`, `spb_parcels.py`, `nea_electricity.py` | Official physical-economy series & official release silence / wording degradation | Partitioned JSONL Ledger |
| L8 | `collectors/ooni_bulk.py`, `bleedthrough.py`, `cdn_edge.py`, `wayback_reconstruct.py`, Nemesis | Censorship, GFW DNS bleedthrough, CDN anomalies, OSINT discourse & DDTI | Readings + Archive |
| L9 | `core/fleet_data_bridge.py` | Cross-product bridge across Seiche, LiquiLens, Undertow, `hel1` SEC EDGAR China/HK issuers, `fsn1` Nemesis & Bulk DuckDB (12.08M+ rows) | Unified Warehouse Snapshot |

## Egress, Rights & Operational Safety

- Every new HTTP collector routes strictly through `core.safe_fetch.safe_fetch_bytes` with explicit per-host `url_policy` allowlists (enforced by `tests/test_egress_policy.py`).
- Zero CAPTCHA bypasses, zero paywall scraping, zero CFETS leakage into public artifacts.
- Production scheduling units live in `ops/systemd/palimpsest-china-dark-data.{service,timer}` and `ops/bulk-data-plane/china-dark-data-lanes.json`, monitored by `scripts/fleet_data_health_guard.py`.
