# Common Crawl institutional history and ML-ready context

## What this method adds

Palimpsest's Wayback collector reconstructs a small reviewed URL watchlist. The
Common Crawl lake adds a different scale: monthly, domain-level metadata for ten
reviewed Chinese public institutions. It reads an external public archive rather
than bypassing access controls or asking anyone inside China to act.

Common Crawl publishes two useful indexes. Its CDXJ index supports small URL
lookups. Its Parquet URL Index is designed for bulk analytical queries and
contains the WARC filename, byte offset, and record length needed to retrieve one
capture without downloading an entire WARC file. Palimpsest uses the Parquet path
for bulk work and reserves CDXJ for a bounded exact-URL diagnostic.

Primary references:

- [Common Crawl URL Index](https://commoncrawl.org/columnar-index)
- [Common Crawl index server](https://index.commoncrawl.org/)
- [Common Crawl getting-started example](https://commoncrawl.org/get-started)
- [Common Crawl terms](https://commoncrawl.org/terms-of-use)
- [WARC 1.1 specification](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/)

## Structured observation

Every accepted row records:

| Field | Purpose |
| --- | --- |
| `target_id` | Reviewed institution identity. |
| `crawl` | Common Crawl monthly collection identity. |
| `canonical_url`, `url_sha256` | Private URL identity and stable join key. |
| `capture_at` | UTC evidence time. |
| `ingested_at` | UTC knowledge time when Palimpsest accepted the export. |
| `fetch_status` | What the archive crawler observed. |
| `content_digest` | Common Crawl SHA-1 payload identity for change detection. |
| `mime_type`, `languages` | Bounded document metadata. |
| WARC filename, offset, length | Exact reproducibility locator. |
| `locator_sha256` | Stable identity of that locator. |
| `input_sha256` | Exact bulk export that supplied the row. |

The private SQLite warehouse keeps URLs because longitudinal change detection
requires stable page identity. Public or model-facing features do not carry the
URLs. Social profiles, personal accounts, arbitrary domains, credentials,
CAPTCHAs, and live Chinese-host fetching are outside this method.

## Features and labels

For each target and crawl, the lake selects the latest capture of each URL and
derives unique/live/error counts, Chinese-language coverage, retained and newly
observed URLs, comparable digests, digest mutations, and archive record bytes.
Adjacent crawl comparisons produce coverage, retention, archive-gap, mutation,
and error rates.

Each derived row carries `available_at`, the latest import time among the
observations used to build it. Capture time describes the source; availability
time describes what Palimpsest actually knew.

The label contract is intentionally asymmetric:

```json
{
  "censorship": "unlabeled",
  "absence_semantics": "archive-coverage-gap-not-deletion"
}
```

A digest change is positive evidence that archived bytes changed. It does not
say why. A URL missing from the next crawl can result from crawl selection,
robots policy, network failure, a site redesign, or removal. It therefore stays
an archive gap unless a separate independent method supplies deletion evidence.

The initial anomaly model is a one-sided robust median-absolute-deviation score.
Each row uses only earlier crawls for the same target. Six prior comparable
crawls are required; before then the state is `warming_up`. A score at or above
4.5 produces `archive_anomaly`, which is a review queue signal rather than a
censorship verdict.

## Training progression

The lake supports a staged model lifecycle:

1. **Derived-feature anomaly detection now.** No labels or source text are
   required. The model finds unusual publication and mutation rhythms.
2. **Human-reviewed ranking labels.** Editors can label whether an RSS context
   packet deserved investigation. The metadata-only story rows already reserve
   this label and keep it null until review.
3. **Supervised evidence classification.** Only independently corroborated and
   human-reviewed outcomes should become positive labels. Negatives must be true
   reviewed non-events, not simply missing data.
4. **Text models only after rights review.** Common Crawl access does not itself
   grant full-text training rights for every upstream page. Initial targets are
   `metadata_only`; derived features are `derived_only`.

Every evaluation must split by knowledge time and group by canonical URL or
institution. Random row splits would place revisions of the same page in both
train and test and overstate performance. An RSS event may only join a feature
whose `last_capture_at` and `available_at` are both at or before the event time,
preventing late archive imports from leaking backward. Report precision, recall,
calibration, coverage, and performance by institution and topic. A model that
improves aggregate accuracy while failing on a sensitive topic is not acceptable.

## Newsroom use

The existing evidence wire supplies live RSS/Atom metadata. The archive context
builder considers only sources already scoped to China or Hong Kong by that
wire. For each event it selects the latest target feature that was knowable
before the event timestamp and copies only the event's predeclared OSINT signal
receipts.

This produces Palimpsest's evidence-first perspective:

```text
RSS report now
  -> prior institutional archive state
  -> current declared measurement surfaces
  -> agreement, contradiction, freshness, and coverage limits
  -> human review
  -> existing newsroom or investigations publication gate
```

The context document contains no generated headline or article body. Its model
features describe review priority, not truth. Automatic publication is
prohibited, and causal language remains prohibited without a declared design.

The configured editorial policy intentionally favors distinctive, defensible
leads. Forty points come from declared archive-anomaly magnitude and breadth,
35 from evidence strength and independent evidence groups, and 15 from live
linked instruments. A primary- or measurement-backed archive anomaly receives
a 10-point under-coverage bonus when only one or two independent groups cover
it. This is a discovery proxy, not proof that no other publisher has the story;
the output meaning explicitly disclaims global exclusivity.

The score orders the private human-review queue only. Editors still determine
public importance, seek missing perspectives and right of reply, verify every
claim, and apply the existing publication gate. Strong writing and complete
coverage are article-level editorial responsibilities and are never inferred
from this metadata score.

## Honest limitations

Common Crawl is very large but not complete. Link popularity, crawl seeds,
robots rules, language, JavaScript rendering, and transient reachability shape
what appears. Monthly cadence is historical context, not minute-level deletion
velocity. Official institutional sites are useful for studying publication and
revision behavior, but they are not a representative sample of Chinese public
speech. These limitations are stored in the feature and summary artifacts, not
left only in prose.
