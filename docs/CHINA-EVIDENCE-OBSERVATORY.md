# China evidence observatory

The evidence desk at `/china/evidence/` brings four independently dated research
streams together. It asks testable questions about economic demand, cash flow,
housing and the official publication record. It does not infer concealment from
missing observations, changed numbers, inaccessible pages or mirror discrepancies.

## Data and collection

| Stream | Actual scope | Collection | Publication |
| --- | --- | --- | --- |
| NBS release archive | Original statistical cells; monthly business indicators; 70-city new/resale housing | Six-hour current release refresh; retained historical capture store | Attributed source tables, full vintage CSV and comparable-series JSON |
| Eurostat mirror trade | EU27 and six EU member reporters; China, Pakistan, Myanmar; monthly HS2 and selected HS4 values and net mass since 2010 | Dataset update probe and weekly full historical refresh | Attributed EU-declared trade, full CSV and deterministic comparisons |
| Official publication watch | Reviewed statistical, fiscal, monetary, trade, energy and transport documents and indexes | Six-hour bounded public checks | Capture metadata, fingerprints, availability and original editorial summaries; bodies private |
| SAFE external accounts | National balance-of-payments and regional cross-border receipt/payment workbooks | Weekly public-workbook acquisition | Acquisition metadata only; quantities, complete CSV and derived analysis private pending permission |

Read the individual artifact clocks and availability fields. A recent collection
check does not advance a source observation or release date. A configured target
is not a successfully captured target. SAFE regional reporting areas describe
the bank processing the payment, not the location of the ultimate firm.

The NBS public database returned access denials during this upgrade. It is not a
live data dependency. The collector does not bypass those controls. PortWatch
and the Census API were evaluated but are not activated as open public sources:
the former has unresolved commercial reuse permission and the latter needs a key.

## Reproducible analysis

`processors/china_economic_history.py` verifies the historical CSV against its
snapshot digest, validates source URLs, clocks and numeric tokens, and builds
like-for-like monthly series. Explicit year labels carry only within the same
source table. Later source vintages supersede earlier chart values while all
captured vintages remain in the export. Conflicting values within one source
are excluded. The result is not a point-in-time backtest.

Housing streaks stop at missing months. Six-month persistence uses only cities
with six adjacent captured months. A short history makes the measure unavailable,
not zero. Inventory and receivable comparisons use the same calendar month of
the prior year where captured; cumulative ratios are not differenced into flows.

Same-URL numeric revision pairs require different captured source bytes and
matching table/cell locators. They record changes, not motive. The document watch
separates ordinary index rotation from stable-document revisions, access limits,
transport errors and repeated 404/410 responses after a known successful capture.
Historical methodology cases cite official announcements and are explicitly
distinct from changes first observed by this installation.

Eurostat comparisons retain reporter, partner, commodity, flow, period, units,
flags and source update/capture clocks. EU aggregates overlap member-state figures;
HS4 products overlap HS2 chapters. Value/weight ratios reflect composition and
quality as well as prices, and are never described as price indexes. A customs
mirror comparison needs period, valuation, re-export and classification alignment
before it can support a discrepancy claim.

## Public and private boundaries

Public artifacts are:

- `/readings/china-evidence-observatory-latest.json`
- `/readings/china-economic-history-analysis-latest.json`
- `/readings/china-economic-history.csv`
- `/readings/china-mirror-trade-latest.json`
- `/readings/china-mirror-trade-history.csv`
- `/readings/china-publication-watch-latest.json`
- `/readings/china-external-accounts-latest.json` (metadata only)

The existing source-policy gate recognizes the exact validated Eurostat contract
and the closed SAFE metadata contract. It grants no additional Seiche export
entitlement and never grants SAFE numeric republication. Unknown fields, changed
rights, source substitutions and numeric injection fail validation. NBS data
retain [their statistical-data terms](https://www.stats.gov.cn/english/nbs/200701/t20070104_59236.html);
Eurostat data retain [their source reuse conditions](https://ec.europa.eu/eurostat/about-us/policies/copyright).
SAFE [requires publication permission](https://www.safe.gov.cn/safe/flsm/index.html).

Private stores live under the regional research state directory on Hetzner.
Raw responses, normalized captures and manifests retain hashes and first-capture
clocks. SAFE `observations.csv`, `analysis.json` and revision comparisons stay
inside that private store and are absent from the public Git tree.

## NarcoScope integration

`connected-research.v1` carries an optional compact observatory summary. Its
`input_sha256.observatory` binds the full Palimpsest observatory document, while
each upstream dataset retains its own clock. NarcoScope validates the summary,
serves it through its existing REST and MCP connected-research interfaces, and
renders the findings and coverage in its research component.

Joins remain country, theme and time context. Pakistan national trade is not a
CPEC project account; Myanmar trade is not a narcotics flow; location and timing
do not identify an actor or establish causality. This public evidence collection
does not replicate China Beige Book's proprietary firm-level panel.

## Operation and validation

The existing `palimpsest-regional-research` service uses its separate source pin.
The publisher builds the economic desk, then this observatory, then connected
research. JSON and CSV companions are promoted together under the data lock.
New artifacts, source code, configuration and page assets are release-critical.

Run the collectors with their `--store` argument pointing outside the checkout.
Build with `python -m scripts.build_china_evidence_observatory`, then run the same
command with `--check`. Tests exercise period inheritance, gaps, revisions,
unavailable values, tampered source bytes, public/private boundaries, transport
errors and HTML/XLSX/JSON shape changes. Browser checks cover mobile, desktop,
city filtering and an accessible table companion to the chart.
