# China economic health and connected regional research

Palimpsest now collects detailed official China release tables and connects them
to a shared China, CPEC/Gwadar, Balochistan, BRI and Myanmar research desk.
NarcoScope packages the same research snapshot for its regional views, REST
endpoint and `get_connected_research` MCP tool.

## Collected evidence

The initial September 8, 2026 capture includes 52 NBS release vintages across ten
statistical families. The latest release per family contains 3,248 numeric cells
and 61 explicit missing cells. The retained vintages contain 16,138 numeric
cells. These are source table cells and release vintages, not independent survey
respondents or distinct economic indicators.

| Evidence | Detail | Public artifact |
| --- | --- | --- |
| NBS release tables | Industry, ownership and activity breakdowns; 70-city housing; PMI tables and published firm-size figures; original merged headings | `readings/china-economic-health-latest.json` |
| China analysis | Profit breadth, industrial working capital, PMI comparison, housing-price breadth, cell-level citations | `readings/china-economic-analysis-latest.json` |
| Regional economic context | 24 WDI indicators for China, Pakistan and Myanmar, requested 2000–2025; 1,702 observed and 170 unavailable annual country rows | `readings/regional-economic-context-latest.json` |
| Regional reporting | 20 publisher/research feeds, metadata and links, publisher-group deduplication, individual collection receipts | `readings/regional-research-wire-latest.json` |
| Connected research | Five regional desks, 18 research questions, annual economic comparisons, source-linked reporting, explicit missing primary records | `readings/connected-research-latest.json` |

The economic desk is `/china/economy/`; its latest-table CSV is `/china/economy/data.csv`.
All retained current-parser vintages are downloadable as
`/readings/china-economic-history.csv`. Its digest and vintage/cell counts are
bound by the current economic-health snapshot. Repeated periods and revisions
remain separate rows; this export must not be treated as independent samples.
The connected desk is `/research/connected/`. Both are in the site navigation.
NBS energy releases without unambiguous tables and national-accounts releases
not captured by the current parser remain unavailable. A failed refresh preserves
the retained release and exposes the update failure.

The annual WDI expansion covers external debt, debt service, public and private
debt, reserves, current account, FDI, remittances, inflation, exchange rate,
consumption, youth unemployment and poverty. Latest available years are shown
alongside the requested final year's availability. Annual comparisons use
adjacent observed years only; gaps are never interpolated.

## Analysis and evidence boundaries

Every NBS finding points to its release, original row and column, value, release
clock, collection clock and raw-response digest. Tables preserve period and
unit headings, including cumulative versus monthly measures. Housing counts are
unweighted; industrial profit breadth does not weight firms or industry size.
The parser separates side-by-side city groups and deduplicates responsive table
copies before counting them. Parser revisions replay retained raw bytes without
changing their original acquisition clock.

The connected desk asks concrete questions about disbursement, repayment,
utilization, local services, budget execution, public accountability and project
delivery. Captured headlines remain attributed accounts, not verified findings.
Article counts describe collection coverage, not incident frequency. Several
feeds owned by one publisher count as one publisher group.

Pakistan country data do not describe a Balochistan district or a CPEC project.
Total external debt is not bilateral Chinese debt. Economic findings describe
national conditions and never infer project effects from those conditions.
Shared geography, theme or time does not establish an actor relationship,
criminal involvement or causality. The existing NarcoScope corridor artifact
passes its full schema and pinned-receipt validator before use.

## China Beige Book comparison

[China Beige Book's platform](https://www.chinabeigebook.com/analytics-platform/)
describes over 100 indicators and 60 dimensions. Its
[independent respondent network](https://www.chinabeigebook.com/cbb-advantage/)
is an essential difference from official aggregate releases.

This release supplies inspectable public industry, ownership, firm-size and city
detail. It does not reproduce an independent respondent panel, loan application
and rejection rates, firm-level borrowing terms, or consistent province × sector
× ownership × firm-size cells. It must not be described as equivalent to China
Beige Book or as having acquired its data.

The next depth requirements are longitudinal provincial and city observations,
product/partner customs quantities, city housing volumes, provincial land-sale
receipts, company cash-flow filings, district welfare and budget execution,
project-level disbursement/utilization records, and an independently recruited
business panel or an appropriately licensed one. Each requires a reviewed
acquisition contract, rights, period/unit definitions and revision history before
it can become an analytical input. No uncollected source is advertised as live.

## Source terms

NBS statistical data are attributed under its
[published statistical-data terms](https://www.stats.gov.cn/english/nbs/200701/t20070104_59236.html).
The collector retains raw evidence privately; the public export includes
statistical tables and attribution, not article prose or an invented sublicense.
The publication filter recognizes the closed NBS statistical-data contract only
after validating source identity, URLs, exact attribution, release identity and
source tokens. It continues scanning for denied lineage inside that contract;
an arbitrary NBS-labelled mapping is not sufficient. This public-information
contract does not add an entitlement to the separate Seiche export ledger.
World Bank WDI observations retain source and CC BY 4.0 attribution. Publisher
feeds contribute title/link/time metadata only; article bodies and raw feed
responses are not retained. Existing restricted China money-market feeds and
their publication controls are unchanged.

## Collection and deployment

Run from the repository root, using the collector environment:

```sh
python -m scripts.china_economic_health_pull --store /private/china-economic-health
python -m scripts.regional_research_pull --store /private/regional-research
python -m scripts.regional_economic_pull --store /private/regional-economics
python -m scripts.build_china_economic_health
python -m scripts.build_connected_research
python -m scripts.build_china_economic_health --check
python -m scripts.build_connected_research --check
```

`palimpsest-regional-research.timer` checks hourly. The runner applies a six-hour
NBS cadence, three-hour regional-feed cadence and weekly WDI cadence. Its
`regional-research` scope runs only those three jobs. The ordinary measurement
service retains its `core` scope, preventing duplicate jobs.

The service uses a separate, exact-commit checkout at
`/opt/palimpsest-research/source`, verified against the root-owned
`/etc/palimpsest/regional-research-commit` file. This leaves the production
incident checkout and its deployed-commit identity intact. Private raw and
metadata histories live under `/var/lib/palimpsest/regional-research/private/`.
Public source snapshots are atomically promoted under the existing data lock.
The Railway publisher derives both desks from its immutable input snapshot and
binds the new pages, assets and JSON to the release manifest.

Install the reviewed commit, seed private histories without overwriting newer
captures, install the service/timer, then run and verify the service once before
enabling the timer. Record the exact commit and collector receipts. A working
collector does not prove the public deployment; verify the provider and custom
origins separately. Existing candidate-reconciliation and base-rotation gates
remain mandatory.

NarcoScope's wire publisher refreshes the fixed Palimpsest snapshot URL in its
disposable checkout. It validates scope, source identities, rights, clocks,
values and hashes before committing an update. A failed fetch retains the
packaged snapshot with its original displayed clock. Builds are offline and
require the matching SHA-256 sidecar. No publisher request can supply arbitrary
upstream URLs.

## Verification

Parser tests exercise merged headings, city-label binding, source clocks,
duplicate tables, missing values, hostile URLs and changed source shapes.
Collector tests exercise idempotency, retained failures, feed deduplication and
body exclusion. Analysis tests check percentage-point comparisons and missing
years. NarcoScope tests exercise API/MCP discovery, tampered hashes, rights,
source and country mismatches, and UI failure/region switching.
