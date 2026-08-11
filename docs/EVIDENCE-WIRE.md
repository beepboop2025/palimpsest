# Palimpsest Evidence Wire

## Product contract

Palimpsest is an evidence observatory, not a headline copier. The Evidence Wire
turns public releases and RSS/Atom notices into structured China-intelligence
dossiers, then presents those dossiers beside—not merged into—the existing
Palimpsest measurements. It preserves the boundary between:

- what a publisher or authority reported;
- what a Palimpsest instrument measured;
- what independent sources corroborated;
- what changed after first publication; and
- what the available evidence cannot establish.

“Every item is analysed” means every accepted item receives a durable receipt
and a visible disposition. It does not mean every duplicate, heartbeat, or
single-source assertion is promoted to a lead story.

The first release covers public, aggregate evidence about China across economy,
politics and law, censorship and rights, networks, technology, security, and
platform policy. It does not collect person-level records, bypass access
controls, mirror article bodies, or claim to estimate a hidden “true GDP”.

## Architecture

```text
 allowlisted RSS/Atom ──> item receipts ──> lexical dossiers ────────┐
 official/market bytes ─> bitemporal observations ─> economic pulse ┤
 Palimpsest scans ──────> current instrument briefs ────────────────┤
                                                                    v
                                server-rendered wire + receipt tape
                                + declared topic pointers (not joins)
                                                                    │
                                                                    v
                                                  sealed static publication
```

Acquisition and publication are separate failure domains. A malformed feed may
degrade that source's receipt, but it cannot erase the last-good public edition
or block a fresh censorship scan. Rendering performs no network access.

The existing `palimpsest-news.v1` instrument briefs remain a compatibility
surface. Dynamic events live in a parallel contract and join the instruments
only in the rendered newsroom.

## Intake boundary

`config/news_sources.json` is the closed egress and editorial registry. A
source declares an exact HTTPS feed, permitted article hosts, source role,
independence and syndication groups, rights mode, desks, cadence, freshness
budget, and bounded intake limits.

The parser accepts RSS 2.x and Atom without trusting namespace prefixes. It:

1. rejects DTD/entity declarations and non-feed HTML responses;
2. caps response, item, title, URL, excerpt, and item-count sizes;
3. rejects credentials, private destinations, unsafe schemes, invalid or
   timezone-free dates, and implausible future dates;
4. removes markup, tracking parameters, controls and bidirectional overrides;
5. stores only title, canonical link, bounded plain-text excerpt, categories,
   timestamps, and cryptographic provenance; and
6. distinguishes fetch failure, parse failure, valid-empty, stale, and current.

Feed text is untrusted evidence, never executable instructions. Palimpsest does
not fetch linked article bodies in this pipeline.

## Identity and revision model

Identifiers are layered so later corroboration cannot rewrite history:

| Identifier | Stable over | Changes when |
|---|---|---|
| `item_id` | a publisher item | its trustworthy GUID/canonical identity changes |
| `item_version_id` | one sanitized version | title, excerpt, time, link, or provenance changes |
| `event_id` | the first accepted event anchor | never; later sources attach to it |
| `revision_id` | one dossier revision | claims, evidence, method, or limitations change |
| `observation_id` | one economic observation | any value, vintage, method, unit, or evidence field changes |

Item mutations append a version receipt. Dossier corrections append a revision;
they do not silently overwrite the prior revision. JSON Feed and RSS GUIDs use
the stable event identity, while modification times expose new revisions.

## Event dossier

Each dossier contains:

- stable event and version identity, desk, topics, publication interval, and
  the current mutation link;
- `reported_facts`, each explicitly attributed to a source item;
- evidence references with role, source and independence group, timestamp,
  canonical URL, and item-version identity;
- declared scan/economic surface IDs labelled `topic-surface-only`;
- an evidence braid describing source ordering without implying causation;
- an evidence-strength class, lead eligibility reason, and explicit
  limitations; and
- the previous version ID when the current dossier is an update.

Evidence strength is categorical, not a pseudo-precise truth score:

1. `measurement-corroborated` — at least two independent groups, including a
   measurement source;
2. `primary-corroborated` — at least two independent groups, including a
   primary source;
3. `multi-source` — at least two independent groups without either role above;
4. `single-measurement-source` — one group containing a measurement source;
5. `single-primary-source` — one group containing a primary source; and
6. `single-source` — one other attributed group.

Mirrors and syndicated copies share an independence group and add no confidence.
An authority's statement is primary evidence that the statement was made, not
independent proof that its substantive assertion is true.

## Analysis methods

### Evidence braid — shipped

Predeclared topic-to-instrument mappings expose where a report could be tested.
The braid orders source receipts and labels those mappings as topical pointers.
Version 1 does not assert a timed statistical match between a report and a
measurement.

### Negative-space divergence — gated

This method is not active in version 1. A later version may compare reporting
activity with measurements, but only after a point-in-time join and null model
are preregistered. Missing/stale instruments will be `untestable`, never “no
signal”.

### Independence graph — shipped

Source, syndication, upstream-data, and measurement groups prevent mirrors from
manufacturing corroboration. International mirrors of an NBS series can validate
transport and revision handling, but remain in the NBS upstream group.

### Mutation and revision watch — shipped

Content hashes expose amended or removed feed entries. Economic vintages retain
release and collection clocks so an “as known on” view cannot learn from a later
revision.

### Counterfactual shadow — gated

No correlation result is published by version 1. Future candidates must be
predeclared, evaluated against shuffled-time and unrelated-signal baselines,
and survive a family-level multiple-testing rule before a recurrent
relationship can be named.

## China economic pulse

The economic layer keeps official, market, survey, physical, international,
news, and data-availability evidence visually and statistically distinct. Its
desks are:

- activity and demand;
- money, credit and foreign exchange;
- markets and capital flows;
- trade, logistics and the physical economy;
- property, labour and firms; and
- data integrity, releases and revisions.

Every value retains series concept, unit, frequency, period, dimensions,
seasonal/price treatment where known, source and independence group, release and
collection clocks, revision, evidence URL/hash, quality, freshness, and limits.

A global “state of the economy” synthesis remains `warming_up` unless its
declared minimum independent groups, domain coverage, period overlap, revision
history, and pseudo-real-time validation gates all pass. The public output shows
which gates failed. It never substitutes a confidence-sounding narrative for
missing data.

## Interface: the evidence braid

The signature UI is a dossier rail connecting reports, official releases, and
measurements. Readers should be able to answer “who said this, what did we
measure, when, and what is still unknown?” without opening a methodology page.

```text
┌ Palimpsest Wire ───────────────────────────── edition / coverage / RSS ┐
│ LEAD DOSSIER                                      ECONOMIC STATE       │
│ headline + attributed finding                     WARMING UP           │
│                                                    4/12 domains live    │
│ Report source ───── Source group ───── Declared measurement surface   │
│ source receipt        independence label          topical pointer       │
│                                                                    │
│ [reported] [measured] [corroborated] [unknown]                       │
├ Economy ─ Politics ─ Censorship ─ Networks ─ Technology ─ Security ─┤
│ reverse-chronological dossier wire │ release/revision calendar       │
├ Current instruments ─────────────────────────────────────────────────┤
│ compatible v1 measurement briefs                                    │
├ Accountability tape ─────────────────────────────────────────────────┤
│ 143 accepted · 31 duplicate · 2 stale · 1 malformed · 0 invented    │
└───────────────────────────────────────────────────────────────────────┘
```

At phone width the braid becomes a vertical timeline; claims remain adjacent to
their receipts. Keyboard focus is visible, status is never conveyed by colour
alone, motion is nonessential and disabled under `prefers-reduced-motion`, and
untrusted text is escaped into server-rendered text nodes.

The visual language keeps Palimpsest's carbon/paper/registry-blue foundation.
Economic data adds amber, revisions add plum, evidence gaps use the existing
red, and current measurements use green. The accent colours are labels and
rules, not decorative gradients.

## Storage and publication

Public output is metadata-only and bounded:

```text
readings/newswire-latest.json
readings/china-economic-pulse-latest.json
news/wire/index.html
news/wire/page/<page>/index.html
news/wire/<event-id>/index.html
news/wire/<event-id>/story.json
news/wire/<event-id>/revisions/<revision-id>.json
news/archive/YYYY/MM/index.json
news/economy/index.html
news/feed.json
news/feed.xml
```

The node retains a bounded normalized latest document and revision ledger. Each
item records the exact fetched feed-document hash, but this intake does not
retain or republish feed bodies. The public revision record includes hashes and
canonical source links, not copied article bodies or private filesystem paths.

Every latest reading is schema-validated, atomically written, included in the
publication contract and data catalog, excluded from recursive OSINT roll-ups,
sealed into the readings ledger, scrubbed, and rebuilt after any publication
race.

## Hetzner operations

The 24/7 node runs acquisition queues separately from publication:

- passive public collectors and RSS intake;
- an isolated high-volume OONI lane;
- the explicitly authorised, stateless BLEEDTHROUGH DNS prober;
- optional browser velocity work only when its proxy and retention gates pass;
  and
- a localhost-only health API.

The active DNS method has an explicit kill switch and durable non-overlap lock.
The RSS timer is a bounded, unprivileged oneshot and is stopped by disabling its
systemd timer. Both have hard runtime/byte limits, freshness receipts, and
last-good preservation on total failure.

The public publisher remains a separately verified boundary. A compromised
measurement node cannot silently rewrite the canonical site.

## Reliability objectives

- Account for 100% of fetched feed responses with a source disposition.
- Never publish a lead from stale, malformed, or unreachable evidence.
- Never increase corroboration by adding a same-group mirror.
- Preserve stable event URLs and prior revision bytes.
- Keep RSS intake failure independent from scan publication.
- Surface scheduler, worker, source and evidence freshness independently.
- Preserve the last-good edition on total intake failure.

## Deliberate trade-offs

- Metadata-only feed intake loses article-body detail but avoids copyright,
  prompt-injection and unbounded-storage risks.
- Deterministic lexical clustering is more explainable than an opaque embedding
  model, but initially misses some cross-language duplicates. Alias rules can be
  reviewed before a multilingual model is introduced.
- A single Hetzner vantage can establish what that path observed, not provincial
  representativeness. BLEEDTHROUGH therefore publishes an injector-count floor
  and abstains from strong regional claims.
- Thin economic coverage produces a less exciting `warming_up` state. That is a
  feature: the source and revision archive must mature before synthesis earns a
  headline.

## Editorial knobs

The small set of choices that meaningfully changes the product lives in reviewed
configuration: source inclusion, topic keywords, topic-to-instrument mappings,
materiality thresholds, freshness windows, minimum independent groups, and lead
eligibility. Defaults are conservative and complete; changing them creates a
method revision rather than silently rewriting old dossiers.
