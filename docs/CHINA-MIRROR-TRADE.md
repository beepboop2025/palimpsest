# EU mirror evidence on China and connected economies

The collector adds externally reported monthly goods trade with China, Pakistan and Myanmar. Eurostat's `DS-045409` dataset is queried through its fixed HTTPS COMEXT endpoint. EU27 provides the broad chapter panel; Germany, France, the Netherlands, Italy, Spain and Poland provide additional views of trade with China. A strategic HS4 panel covers machinery, semiconductor equipment, integrated circuits, batteries, vehicles, pharmaceuticals, fuels and metals.

The default scope starts in January 2010, covers both directions and preserves nominal euro value and net mass. Source units of 100 kg are converted to kilograms. The API omits HS chapter 98; chapter 77 is reserved. Neither is filled with invented zeros. Empty cells remain unavailable, source status flags remain visible, and future empty calendar positions are excluded.

## Collection and refresh

Run from the repository root using its collector environment:

```sh
python -m scripts.china_mirror_trade_pull --store /private/china-mirror-trade
python -m scripts.china_mirror_trade_pull --check
```

Normal repeated runs make a small current-year total-trade request to check the dataset update clock. Verified retained batches are reused while the update clock is unchanged and the last full capture is less than seven days old. New dataset updates, the weekly boundary, or `--full-refresh` trigger all 19 historical batch requests. There are two workers by default, a maximum of three, no redirects, a 45-second request timeout, a 12 MiB decompressed response bound and a 200,000-cell cube bound. All egress uses `core.safe_fetch` with an exact host/path/query policy. Authentication is not required.

`--retry-missing` is for interrupted initial collection: it verifies existing batches and only requests missing batches. `--reparse-retained` verifies raw hashes and original receipts without network access. Both retain original capture clocks. Public CSV and JSON are written only after the public contract validates; deployment should promote the pair under its normal data lock.

The private archive stores exact response bytes by SHA-256 and immutable acquisition receipts whose identities bind the query, raw hash and parser version. Identical bytes preserve their first capture time; revised bytes create another vintage. The private archive is bounded at 512 MiB, with last-good retention on capacity failures; it never silently deletes evidence. The index selects one current vintage per query. Dataset checks are privately retained separately. Public history contains unique reporter/partner/product/flow/month rows, with both measures and source-vintage references.

## Interpretation

EU imports from China offer an external check on Chinese goods exports; EU exports to China offer a view of Chinese demand for EU goods. These observations do not measure all Chinese imports or exports, domestic GDP, household confidence, port activity, shipping routes, or CPEC utilisation. Country and EU27 views overlap, as do HS2 chapters and their HS4 components: adding those views would double-count.

Analytical findings include same-month year-on-year changes, equal-chapter breadth and sufficiently large HS4 cases where value and weight move in opposite directions. The exact baseline month must exist, and a zero baseline does not produce a growth percentage. Ratios of value to mass are labelled unit values, not price indices: product mix and quality can change them. HS classifications are revised, so the same code is not a guarantee of complete comparability over time. Missing observations or cross-source differences alone do not establish concealment.

The source `updated` timestamp applies to the dataset. It is not a release timestamp for each historical cell. Original local capture times and hashes are distinct from both that clock and the observation month; historical replay must use the actual capture vintages.

## Primary sources and publication rights

- [Eurostat dataset and download interface](https://ec.europa.eu/eurostat/databrowser/view/DS-045409/default/table?lang=en).
- [Trade methodology](https://ec.europa.eu/eurostat/cache/metadata/en/ext_go_detail_sims.htm): EU-reported values and quantities, partner attribution and CIF/FOB valuation.
- [Eurostat reuse terms](https://ec.europa.eu/eurostat/help/copyright-notice): attributed statistical reuse is permitted, including EU-declared trade with outside partners. The collector admits only EU reporters and HS2/HS4 aggregates. Third-country-declared records and Austrian CN8 records are excluded from the contract. The original terms apply; this project does not invent a downstream licence.

The prototype also evaluated PortWatch and the US Census API. [Current IMF terms](https://www.imf.org/en/about/copyright-and-terms) require permission for potential commercial IMF-data reuse. [Census examples](https://api.census.gov/data/timeseries/intltrade/exports/hs/examples.html) now require an API key, confirmed by the live missing-key response. Neither was admitted to this public collector. The retained discovery probes are private and are not public datasets.
