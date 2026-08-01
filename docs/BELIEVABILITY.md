# The believability read

**One sentence.** Every month we compute the growth story China's physical
telemetry tells (electricity, rail freight, bank loans, weighted 40/40/20),
subtract it from the growth story the headline claims, and ask one narrow
question: did the gap between the two break away from its own history?

**What a reading outside the band establishes:** the established relationship
between the headline and the state's own physical series moved this month.

**What it does not establish:** fabrication. The gap has innocent causes and
the method note below names them before any critic has to.

## Origin, with the record corrected

In March 2007, Li Keqiang, then Liaoning Party Secretary and later Premier,
told the US ambassador over dinner in Beijing that GDP figures are "man-made"
and therefore unreliable, and that he watched three things instead:
electricity consumption, rail cargo volume, and loans disbursed. "All other
figures, especially GDP statistics, are 'for reference only,' he said
smiling." The primary source is the leaked cable
[07BEIJING1760](https://wikileaks.org/plusd/cables/07BEIJING1760_a.html).
(The remark is often placed in Kunming; the cable says Beijing.)

The Economist built the index in his honour on 9 December 2010
(["Keqiang ker-ching"](https://web.archive.org/web/20120222021017/http://www.economist.com/node/17681868),
snapshot pinned because the live page blocks robots). The original article
fixed no weights; the canonical 40% loans / 40% electricity / 20% rail
convention is documented in
[CME Group research](https://thehedgefundjournal.com/china-s-li-keqiang-index/).
This instrument computes exactly that canonical composite and nothing else:
extra telemetry (coal, crude steel) is published beside it, never blended in.

## Method

1. **Components**, monthly, from the state's own releases, keyless:
   loan growth = outstanding RMB loan balance YoY from the PBC monthly
   financial statistics report; electricity = generation YoY from the NBS
   Energy Production release (the original watched consumption; generation is
   the keyless state series, and the substitution is stated in every
   reading); rail freight = cumulative year-to-date volume YoY from the
   National Railway Administration's monthly news.
2. **Headline comparator**: industrial production YoY from the NBS release
   for the same month.
3. **The statistic is drift, not level.** The published number is this
   month's gap (headline minus composite) relative to the gap's own rolling
   median, with a band of two median absolute deviations. Every reading
   carries the band. Fewer than eight months of history publishes the gap
   and claims nothing (`warming_up`).
4. **Abstention discipline.** A missing component abstains the composite
   rather than reweighting it. A release article that does not match the
   expected data month is dropped, not reused. Narrative-text parsers return
   None on any rewording, and the miss is published as a missing component.

## Why the naive version would be wrong

A services-heavy economy legitimately uses less electricity and ships less
freight per yuan of output, so the raw gap is positive in any modern year
and scoring its level would flag every month forever. That critique is not a
footnote here; it is why the statistic is drift against the gap's own
baseline. The literature the design answers to:

- SF Fed, 2013 ([Fernald, Malkin, Spiegel](https://www.frbsf.org/research-and-insights/publications/economic-letter/2013/03/reliability-chinese-output-figures/)):
  2012 official output was consistent with electricity and externally
  reported trade. No smoking gun should be assumed.
- SF Fed working paper 2019-19 ([Fernald, Hsu, Spiegel](https://www.frbsf.org/economic-research/publications/working-papers/2019/19/)):
  trading-partner imports as a manipulation-free benchmark; their eight-series
  C-CAT tracks the cycle better than GDP, and "GDP adds little information."
- NY Fed, 2017 ([Liberty Street Economics](https://libertystreeteconomics.newyorkfed.org/2017/04/is-chinese-growth-overstated/)):
  the explicit counterpoint. Their nightlights-weighted indicator did NOT
  support the exaggeration claim. This instrument quotes it so that a
  within-band reading is reported as exactly that.

And the priors that justify watching at all:

- Brookings BPEA 2019 ([Chen, Chen, Hsieh, Song](https://www.brookings.edu/articles/a-forensic-examination-of-chinas-national-accounts/)):
  official growth 2008 to 2016 overstated by roughly 1.7 points per year,
  reconstructed from VAT and the local-versus-national gap.
- Martinez, JPE 2022 ([nightlights](https://bfi.uchicago.edu/working-paper/how-much-should-we-trust-the-dictators-gdp-growth-estimates/)):
  reported GDP outruns satellite night-lights systematically more in
  autocracies, and more when the incentive to exaggerate is stronger.

## The darkness ledger this instrument sits beside

The believability read is the value-level companion to the
[data darkness signal](../readings/index.html), which watches whether the
inputs keep being published at all. The verified withholding record:

- Youth unemployment (16 to 24): suspended 15 August 2023 after a record
  21.3% ([BBC](https://www.bbc.com/news/business-66506132)); resumed
  17 January 2024 with a new methodology excluding students, printing 14.9%
  ([CNBC](https://www.cnbc.com/2024/01/17/china-misses-fourth-quarter-gdp-estimates-resumes-posting-youth-unemployment-data.html)).
  An eight-month gap followed by a six-point-lower series is itself a
  believability event.
- Stock Connect: the two-stage removal of flow data was announced 12 April
  2024 ([HKEX](https://www.hkex.com.hk/News/Market-Communications/2024/2404122news?sc_lang=en));
  real-time northbound turnover ended 13 May 2024
  ([The Standard](https://www.thestandard.com.hk/finance/article/62700/HKEX-halts-real-time-data-for-northbound-trading));
  daily flow data ended 19 August 2024
  ([Taipei Times/Bloomberg](https://www.taipeitimes.com/News/biz/archives/2024/08/19/2003822426)).
- Interbank bond pricing feeds: suspended under regulator order, March 2023
  ([FT, snapshot](http://web.archive.org/web/20231013210319/https://www.ft.com/content/77b920f5-1b33-4a59-8d91-eabf434b9687)).
- The wide-angle 2025 count: "Chinese officials have stopped publishing
  hundreds of data points once used by researchers and investors," including
  land-sales measures, FDI data, unemployment indicators, cremation data and
  a business confidence index (Feng and Douglas,
  [WSJ, May 2025](https://www.wsj.com/world/china/china-economy-data-missing-096cac9a),
  quote verified via reprints). Note the granularity: the NBS land-purchase
  detail stopped while the MOF aggregate continues, so the ledger encodes
  granularity loss, not total disappearance.

Two often-repeated items are deliberately absent pending confirmation: a
claimed 2024 CFETS bond-feed vendor cutoff (every checkable trail leads back
to the March 2023 event) and the May 2022 bondholder-data halt (headline
only, no fetchable source). The NBS consumer confidence index is NOT
discontinued (alive through May 2026 with a lag) and is not claimed here.

## Prior art

[Capital Economics' China Activity Proxy](https://www.capitaleconomics.com/china-activity-proxy)
(client-gated), [Rhodium Group](https://rhg.com/research/chinas-economy-rightsizing-2025-looking-ahead-to-2026/)
(2025 growth estimated at 2.5 to 3.0% against the official 5.2%), and the
actively maintained
[SF Fed China CAT](https://www.frbsf.org/research-and-insights/data-and-indicators/china-cyclical-activity-tracker/).
What this instrument adds is not a better growth estimate; it is a public,
reproducible, self-updating divergence watch whose every input, rule and
miss is in this repository.

## Data notes

Sources are narrative sentences and Word-export tables on stats.gov.cn,
pbc.gov.cn and nra.gov.cn; every parser is fixture-pinned to the exact
wording served on 2026-08-01 (tests/test_lkq_telemetry.py) and returns None
on rewording. Article URLs are unguessable, so every series is a two-step
fetch through its listing page. nra.gov.cn intermittently gates foreign
vantages; a failed fetch is a missing component, never a guessed one.

Known calendar structure, stated up front: NBS publishes no standalone
January release (the combined January-February articles land in March and
are scored as the February data month), so the January data month abstains
by design, every year. The PBC annual report (the bare "YYYY年金融统计数据
报告") covers December. The loans component reads the outstanding RMB loan
balance YoY; when a report vintage omits that line (the H1 2026 report
nearly did), the all-currency balance line is used and the reading names
the basis (`loans_basis`) — the disappearance of a long-standing line from
the state's own report is, of course, exactly the kind of event the sibling
data-darkness signal exists to date.
