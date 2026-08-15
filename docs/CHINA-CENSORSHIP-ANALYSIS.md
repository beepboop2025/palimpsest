# China Censorship Analysis

`/news/china/analysis/` is Palimpsest's continuously refreshed analytical
reading of its current China censorship instruments. It is separate from the
publisher dispatch stream at `/news/china/`: the dispatch stream preserves
what monitored publishers reported, while this article compares only validated
Palimpsest aggregate measurements.

## Publication path

`core/china_analysis.py` consumes `readings/newsroom-latest.json` through the
validated in-memory newsroom feed. It selects ten declared instruments covering
the board alarm, coverage guard, directive attention, blackout detection,
network vantages, erasure, and app distribution. The builder performs no
network access and accepts no free-form model prose.

Every evidence projection copies the current story's:

- signal identity and publication status;
- aggregate claim and metric;
- source timestamp and input SHA-256;
- story and reading URLs; and
- first declared interpretation limit.

The runtime validator compares every projection with the source feed, resolves
every sentence citation, rejects missing or repeated receipts, and recomputes
the content-derived article revision. A changed evidence row cannot pass merely
because its revision string was also changed.

## Analytical boundary

The article reads five kinds of evidence together without pretending they have
one denominator:

1. directive attention records which provenance-bound terms appear in collected
   censorship reports;
2. the silence index applies a predeclared blackout rule to considered topics;
3. network instruments use volunteer measurements, fixed panels, and a bounded
   fusion of reporting vantages;
4. the erasure index summarizes movement across heterogeneous archive and
   distribution layers; and
5. app comparisons cover a controlled fixed panel and a much broader catalogue.

Their co-location supports a layered current reading. It does not produce a
national censorship percentage, prove motive, or turn related measurement
pipelines into independent corroboration.

## Availability and automation

The evidence-wire workflow runs twice per hour and rebuilds the article after
the aggregate newsroom feed. If a selected story is stale, missing, degraded,
or corrupt, the article copies that story's availability claim, displays its
metric as `withheld`, enters `instrument-warning` state, and lists the signal in
`publication_receipt.availability_warnings`. It never republishes a retained
non-live value as current.

The workflow tests the closed article and renderer, rebuilds after a late ledger
change or push race, stages the article JSON with the reader surfaces, and then
seals every `*-latest.json` reading in the shared readings ledger. The article
therefore cannot be published by the scheduled path without its structured
receipt and seal participating in the same candidate commit.

Public surfaces:

- `/news/china/analysis/` for readers;
- `/news/china/analysis/feed.json` for JSON Feed 1.1;
- `/news/china/analysis/feed.xml` for RSS 2.0; and
- `/readings/china-censorship-analysis-latest.json` for the complete structured
  article and publication receipt.
