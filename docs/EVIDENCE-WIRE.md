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

“Every item is analysed” now has two public projections. Every accepted event
receives a durable source receipt and a content-addressed Palimpsest assessment;
the China Article Stream then restores every in-scope publisher item as its own
chronological entry and attaches that exact event assessment. The assessment
states whether the event is inside the declared remit, evaluates independent
source structure, quotes only explicitly linked normalized collector findings,
and publishes an abstention when those collectors are not current. It does not
mean every duplicate, heartbeat, or single-source assertion is promoted to a
lead story. Publisher repetition remains visible in the article stream but does
not manufacture additional source independence.

The first release covers public, aggregate evidence about China across economy,
politics and law, censorship and rights, networks, technology, security, and
platform policy. It does not collect person-level records, bypass access
controls, mirror article bodies, or claim to estimate a hidden “true GDP”.

## Architecture

```text
 allowlisted RSS/Atom ──> item receipts ──> chronological China feed ─┐
                                  └───────> lexical dossiers ─────────┤
 official/market bytes ─> bitemporal observations ─> economic pulse ┤
 Palimpsest scans ──────> current instrument briefs ────────────────┤
 public Telegram allowlist ─> raw warned Telegram mirror ───────────┐
                              └─> private ScamShield capsules ──────┤
 reviewed ScamShield aggregate ─> context-only Telegram watch ──────┤
 human-reviewed capsule ─> sanitized individual whisper ────────────┤
 official Instagram API ─> sanitized social-version ledger ─────────┤
 authenticated Telegram export ─> same social-version ledger ───────┤
                                                                    v
                                event-bound China Situation desk
                                + exact publisher-URL social joins
                                + declared topic pointers (not verification)
                                                                    │
                                                                    v
                                                  sealed static publication
```

Acquisition and publication are separate failure domains. A malformed feed may
degrade that source's receipt, but it cannot erase the last-good public edition
or block a fresh censorship scan. Rendering performs no network access.

The public article stream is available at `/news/china/`, with RSS at
`/news/china/feed.xml`, JSON Feed 1.1 at `/news/china/feed.json`, and its strict
document at `/readings/china-article-stream-latest.json`. “Every” means every
in-scope item from the declared, measured registry window—not the entire web.
Global feeds must contain an explicit China/Hong Kong term in retained title or
excerpt metadata; reviewed China-specific desks are included item by item.

## China Situation synthesis

The Situation desk at `/news/china/situation/` is the combined view requested by
readers who want reporting, circulation context and measurements in one place. Its
strict document is `/readings/china-situation-latest.json`; its RSS and JSON Feed
are `/news/china/situation/feed.xml` and `/news/china/situation/feed.json`.

The join rules are intentionally narrow:

1. Evidence Wire events supply attributed publisher reports and the only independent-
   publisher count.
2. Institutional Telegram or Instagram observations join an event only when their
   sanitized record carries an exact canonical URL already present in that event.
3. Dragon Whispers appear in a separate human-reviewed, source-free Telegram briefing;
   the builder does not guess which event they describe.
4. Observatory rows come only from the event's predeclared `topic-surface-only`
   context. They do not verify the publisher's article or establish causation.

This means a cross-platform repost from one newsroom remains one publisher lineage.
An unmatched social post remains visible in coverage as unmatched rather than being
forced into the nearest-looking story.

## Social observation intake

`config/social_sources.json` is a second closed registry. It currently binds seven
institutional Instagram professional accounts whose identities are linked from their
publishers' official sites. `collectors/instagram_graph.py` uses Meta's official Graph
API v26 Business Discovery surface, sends the token only in an authorization header,
requests metadata fields only, reconstructs bounded cursor pages, and never requests or
stores media binaries, comments, engagement, followers, locations or direct messages.

The connector is dormant unless `PALIMPSEST_INSTAGRAM_ENABLED=1`,
`META_INSTAGRAM_ACCESS_TOKEN`, and `META_INSTAGRAM_BUSINESS_ACCOUNT_ID` are configured.
Until then, every source has a `not-attempted` receipt; the absence of records is not
presented as social silence.

An optional ScamShield runtime can publish the same strict Telegram social contract.
`scripts/import_social_observations.py` fetches its latest view, immutable JSONL versions,
and HMAC manifest with redirects disabled. It verifies the exact bytes before parsing,
requires the local registry digest, and permits the remote runtime to append Telegram
versions only. Local Instagram observations and history cannot be overwritten by that
handoff. The full security and rollout design is in
`docs/SOCIAL-OBSERVATION-PIPELINE.md`.

## Telegram and ScamShield context

Telegram is a separate monitoring lane, never an evidence shortcut. It now has
two publication surfaces with different contracts, plus a warehouse capture
lane that does not auto-publish.

**Warehouse public-channel records** (`scripts/telegram_public_channels_pull.py`,
fleet `telegram-public-channels`) poll keyless `https://t.me/s/{handle}` HTML
for the three Dragon Den public channels already named in this repository.
Each public post becomes a fat observation (full public text, message date,
channel handle, `content_sha256`, first-seen, outbound public links, gazetteer)
and joins official-first-seen / deletion ledgers / Weibo boards / Wayback when
a URL or distinctive span already exists as a real record. A first-class
`mainland_echo` family marks posts that quote or archive a deleted mainland
item. The same beat drains `var/scamshield-inbox` through
`scripts/scamshield_feed.py` so capsules land as sanitized counts. This runner
never writes `readings/telegram-watch-latest.json` and never auto-promotes
Dragon Whispers. Login-walled or empty previews abstain. CDT and GreatFire have
no public `t.me` handle in-tree; those desks stay on RSS ledgers.

**Whispers from the Dragon Den on Telegram** is the raw companion. A dedicated
bot receives new and edited posts from an explicit allowlist of public Telegram
channels and uses Telegram's native forward operation to send each delivered
post to one catch-all destination and any configured topic destinations. A
mandatory warning is posted before the forward. There is no classifier or
editorial gate on that Telegram delivery: ScamShield analysis runs in parallel
and cannot suppress it. Native forwarding preserves Telegram's source context;
protected or otherwise unforwardable posts produce a warning tombstone rather
than being copied around the restriction.

“Every post” means every new or edited channel update Telegram delivers after
the bot is active and an administrator in the configured public source and
destination channels. It does not mean historical backfill, deleted posts,
private channels, direct messages, invite-only access, or guaranteed delivery
through a third-party platform. The forwarding service persists delivery
coordinates and receipts, not message bodies or media. Its separate bot token,
route file and operational runbook live in the ScamShield repository.

**Whispers on Palimpsest** is the smaller reviewed lane at
`/news/china/whispers/`, with RSS at `/news/china/whispers/feed.xml`, JSON Feed
at `/news/china/whispers/feed.json`, and the strict artifact at
`/readings/dragon-whispers-latest.json`. A page entry can be created only from a
verified Evidence Capsule for an explicitly public channel, after a human
reviewer approves both sanitation and China relevance. The closed
`palimpsest-dragon-whispers.v1` contract rejects raw wording, source identity,
Telegram coordinates, named allegations, exact indicators, URLs and contact
details. It retains only classifier tier/family labels, indicator counts,
script hints, reviewer-authored analysis, uncertainty, next checks and a
capsule digest receipt. Every projection says
`unverified-context-only-not-evidence` and cannot increase a dossier's
independent-source count.

Promote one eligible private capsule only in the secure review environment.
The analytical text below is illustrative; a reviewer must write it from their
own assessment rather than copying the Telegram post:

```bash
python3 scripts/review_dragon_whisper.py \
  /secure/review/public-channel-capsule.json \
  --reviewed-at 2026-08-15T08:00:00Z \
  --reviewer-role china-desk-editor \
  --review-note "Reviewed for privacy, scope, and analytical restraint." \
  --headline "Sanitized pattern-level headline" \
  --summary "What the classifier record suggests without repeating the claim." \
  --why-it-matters "Why this pattern merits independent China-desk checks." \
  --uncertainty "What remains unknown and what one observation cannot establish." \
  --next-check "Compare with independently archived public reporting." \
  --next-check "Seek a separate attributable source class." \
  --approve-sanitized-whisper \
  --confirm-china-relevance
python3 -m scripts.build_newsroom
```

The existing daily aggregate is a third, coarser context path. ScamShield
collects configured public channels and operator-authorized surfaces and
exports a private summary with observed-message, observed-source, flag, and
error counts. Raw text, channel identities, pseudonyms, exact IOCs, assessment
IDs and message-level allegations are excluded.

The private `scamshield-telegram-monitoring-summary/v1` is explicitly not
publication-eligible. `scripts/review_scamshield_summary.py` requires a human to
approve a public aggregate and explicitly select any classifier families that
are relevant to the China desk. Its output is validated as
`palimpsest-telegram-watch.v1`; it remains labelled
`aggregate-context-only-not-corroboration` and cannot increase a dossier's
independent-source count. If no reviewed artifact exists, the page says so
instead of rendering missing coverage as quiet or zero activity.

Run the promotion only in the secure review environment; keep the private
summary outside this repository. Replace the illustrative review time, note,
and classifier families with the reviewer-approved values:

```bash
python3 scripts/review_scamshield_summary.py \
  /secure/review/scamshield-monitoring-summary.json \
  --reviewed-at 2026-08-15T08:00:00Z \
  --reviewer-role china-desk-editor \
  --review-note "Reviewed aggregate coverage and selected China-relevant families." \
  --china-family CYBER_FRAUD \
  --approve-public-aggregate \
  --output readings/telegram-watch-latest.json
python3 -m scripts.build_newsroom
```

The aggregate-review command writes only the validated public aggregate. The
newsroom command rebuilds the article stream and changes its Telegram panel
from review-gate closed to human-reviewed context; it still cannot alter article
corroboration. Neither aggregate review nor individual-whisper review controls
raw delivery inside the separate Telegram companion.

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

### Pages analysis-history archive

The Git publication tree retains every analysis revision as an ordinary JSON
file. The Pages edition uses a deterministic representation bridge so that the
append-only history does not exceed the host's artifact limit:

- every current `news/wire/event-*/analysis.json` remains directly readable;
- the byte-identical revision named by that head's `analysis_id` also remains
  directly readable under its `analysis/revisions/` path;
- every analysis revision, including those current copies, is available by its
  exact repository path inside
  `/news/wire/analysis-revisions.tar.xz`; and
- every event-dossier revision remains a direct JSON file. It is never moved
  into this analysis-only archive.

The publication-rights transform always runs first. The Pages archive builder
then reads the canonical
`/readings/china-publication-rights-latest.json` for the exact publication
commit. If its `quarantined_paths` contains any wire `analysis.json` or
`analysis/revisions/*.json` path, rights take precedence: the builder returns
`mode=rights-suppressed`, creates neither the archive nor its receipt, and
verifies that neither output already exists. Directly safe wire files and the
same-path restricted stubs remain untouched. Suppression still proves that each
unrestricted current head has one regular, byte-identical named revision; it
cannot hide unrelated missing, drifted, or ambiguous history. A malformed,
non-canonical, or different-commit rights status fails the Pages build instead
of falling back to archiving.

The access receipt at
`/news/wire/analysis-revisions-archive.json` binds the exact publication commit,
archive byte count and SHA-256, expanded entry count and bytes, deterministic
member tree, retained-head closure, unchanged event-revision tree, and the
`history_tree_sha256` from `/news/wire-history-integrity.json`. Its `archive.url`
adds the archive digest as a cache-busting query. Consumers should fetch that
URL, verify `archive.sha256`, run `xz -t`, and then read the requested exact
member path. A non-current analysis revision returning 404 at its former direct
Pages URL means “use the archive access map,” not “the revision was deleted.”
If the archive and access receipt are both absent, consumers must check the
exact-edition publication-rights status first; a listed wire restriction means
the archive was deliberately suppressed, not lost.

The archive is an exact-Pages staging transformation only. It does not rewrite
or thin repository history, and deployment fails if a current head is missing,
ambiguous, or differs by one byte from its named revision; if an archive member
is a link, duplicate, or non-canonical path; if xz integrity fails; or if the
archive and public receipt disagree. Production CLI build and `--check` use the
same rights-aware decision as `build_for_pages(root, publication_sha)` and
`verify_for_pages(root, publication_sha)`; the lower-level `build` and `verify`
APIs retain the archive-only contract for existing callers.

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

### Per-event Palimpsest assessment — shipped

Every event page and `analysis.json` companion has exactly one of four
dispositions:

1. `outside-remit` — the global-feed item has no reviewed China source, term, or
   intake-approved collector link and must not be read as a Palimpsest finding;
2. `source-assessment` — Palimpsest can judge attribution and independent-source
   structure, but has no declared collector surface for the event;
3. `collector-context` — every declared collector surface is live, so its exact
   aggregate finding, method, timestamp, evidence URL and input hash are shown as
   context; or
4. `collector-abstention` — at least one declared surface is non-live, so
   Palimpsest withholds a collector-backed conclusion while preserving the gap.

The join is deliberately `topic-surface-only`. A current value is not called
verification, refutation, cause, coordination, or impact unless a future method
predeclares and validates a timed, claim-specific join. The article body remains
outside this metadata-only rights boundary.

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

## Investigations desk

The investigations desk is a review gate above the wire and measurement layers.
Automation may open a **Research Lead**, bind claims to exact artifact hashes,
enumerate counterevidence and publish tests that could falsify the working
hypothesis. It may not promote its own work into a completed investigation.

Each public case keeps supporting and contradicting receipts adjacent to the
claim under test, counts independence by upstream group rather than URL, and
lists unresolved aggregate-data collection targets. Person-level records,
automated allegations, inferred motives and causal claims without a declared
design are outside the public contract. A case receives `NewsArticle` metadata
only after the structured gate and human review are complete; open work remains
a `Report` with an unmistakable not-published notice.

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
readings/investigations-latest.json
news/wire/index.html
news/wire/page/<page>/index.html
news/wire/<event-id>/index.html
news/wire/<event-id>/story.json
news/wire/<event-id>/revisions/<revision-id>.json
news/wire/<event-id>/analysis.json
news/wire/<event-id>/analysis/revisions/<analysis-id>.json
news/archive/YYYY/MM/index.json
news/economy/index.html
news/investigations/index.html
news/investigations/<case-slug>/index.html
news/investigations/<case-slug>/case.json
news/investigations/<case-slug>/revisions/<version-id>.json
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
- Publish exactly one schema-valid assessment for every current wire event.
- Never describe a topic-linked collector as article-specific verification.
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
