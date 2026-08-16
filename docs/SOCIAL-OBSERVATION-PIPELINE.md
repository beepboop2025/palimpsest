# Social observation pipeline

Status: v1 contract, public zero-state, Instagram adapter, authenticated Telegram
handoff, Situation renderer, and offline tests implemented. Live collection remains
credential- and source-onboarding-gated.

This design adds social platforms as **attributed observations**, not as a new
kind of proof and not as pretend RSS. It preserves the current Evidence Wire v1
contract while creating the transport-neutral boundary needed for a later Wire
v2 migration.

## Outcome and non-goals

Palimpsest will be able to:

- collect new and edited posts from an explicit registry of authorized Telegram
  channels;
- collect bounded public metadata from approved Instagram professional accounts;
- retain stable identities and append-only revision receipts;
- show exactly which configured sources succeeded, failed, were rejected, or were
  not attempted;
- connect a social observation to a publisher or candidate dossier without
  increasing corroboration merely because the same newsroom posted on several
  platforms; and
- publish a useful China social desk, JSON artifact, and optional RSS/JSON Feed
  views from one underlying ledger.

This pipeline does **not** claim to index all of Telegram, Instagram, or the
internet. It does not scrape private or consumer accounts, ingest direct
messages, download media, archive full captions, infer identities, or turn
likes/reposts into evidence. It does not alter the Evidence Wire v1 evidence
strength, lead eligibility, event analysis, or human corroboration workflow.

## Requirements and assumptions

### Functional requirements

1. Every source is present in a reviewed, versioned registry before collection.
2. Every published record has an opaque stable observation ID, stable source
   identity, collection time, publication time, original permalink, bounded
   metadata, rights label, and revision ID.
3. Telegram edits create a new version of the same observation. Instagram media
   metadata changes do the same and are marked `edited` after the first observed
   state. Version IDs bind each revision to its immediate parent, so an A → B → A
   reversion remains visible while consecutive identical polls remain idempotent.
   A platform deletion is recorded only when the official API exposes enough
   information to do so; otherwise the limitation is explicit.
4. The latest projection is rebuildable from an append-only normalized ledger.
5. Coverage includes every configured source and exposes successful, failed,
   rejected, and not-attempted states. Adapter-specific failures collapse to a
   credential-free error code at the public boundary.
6. A shared publisher independence group follows the publisher across RSS,
   Telegram, and Instagram. Platform count is never independence count.
7. Public output is metadata-link-only. Raw API responses, tokens, native IDs,
   cursors, account peer IDs, media binaries, engagement data, locations,
   comments, mentions, and DMs never cross the publication boundary.

### Platform assumptions

- Telegram Bot API collection is forward-only. The dedicated bot must be added
  to each allowlisted channel and sees no pre-onboarding history. Long polling
  and webhooks are mutually exclusive; the first deployment uses outbound-only
  long polling because the Palimpsest node currently exposes no public ingress.
- Instagram uses only Meta's documented APIs. Professional-account discovery
  is a bounded surface, not a keyword firehose. Hashtag discovery is reserved for
  a later reviewed registry type and is not active in v1.
  The connector remains disabled until App Review, the required permissions,
  and an organization-controlled credential exist.
- A source appearing on two platforms may still be one editorial organization.
  Registry review, rather than a hostname or platform name, assigns the
  independence group.

## High-level design

```text
 ScamShield Telethon          Instagram Graph API            RSS / Atom
 reviewed public channels     reviewed accounts              existing v1
          |                            |                         |
          v                            v                         v
  sanitized signed export       instagram adapter          Evidence Wire v1
          |                            |                         |
          +------------+---------------+                         |
                       v                                         |
             normalized private spool                           |
       native IDs + private cursor/peer pins only                |
                       |                                         |
                       v                                         |
       strict social-observation materializer                    |
       stable IDs + revisions + coverage receipt                 |
                       |                                         |
                       v                                         |
        signed, sanitized latest + version ledger                |
                       |                                         |
                       v                                         |
        publication importer / fail-closed gate                  |
                       |                                         |
                       +-------------------+---------------------+
                                           v
                                China Situation desk
                         exact publisher-article URL joins
                         never changes corroboration counts
```

The adapters are deliberately small. They translate platform responses into a
common candidate shape; the central model owns validation, canonicalization,
stable identity, rights, coverage, and forbidden-field checks. This prevents a
future platform change from silently changing Palimpsest's evidence semantics.

## Components

### 1. Reviewed source registry

`config/social_sources.json` is the public declaration of collection scope. A
source record contains:

- Palimpsest `source_id` and display name;
- platform and source type;
- exact allowlisted article hosts;
- publisher independence group;
- rights policy; and
- activation state and a human-readable onboarding requirement.

The initial checked-in registry contains seven verified institutional Instagram
professional accounts and one separately reviewed public Telegram publisher
binding; no private monitoring identity is inferred into it. A source becomes active
only after its account identity and permission path have been verified. For
Telegram, deployment additionally pins the numeric peer ID in private state to
detect handle reassignment; that ID is not published.

### 2. Telegram companion export

ScamShield's existing Telethon relay captures only sources present in its two
operational allowlists and an additional reviewed social binding. It:

- keeps numeric peer IDs, native message IDs and checkpoints in mode-0600 private
  state;
- rejects channels not present in both the public registry and private peer-ID
  pinset;
- records a bounded headline/excerpt, timestamps, post URL, content digest, edit
  state, and media kind without downloading media;
- commits the revision spool before publishing the signed latest projection; and
- mirrors non-Telegram public registry rows as `not-attempted` so both repositories
  authenticate the same registry digest.

This does not consume Dragon Den's destination database and does not reveal the
identities of existing private monitoring routes. Only separately reviewed public
publisher bindings are eligible for the attributed social ledger.

### 3. Instagram adapter

The implemented adapter supports `instagram_professional`: known professional
publishers through Meta's Business Discovery surface. The schema reserves
`instagram_hashtag`, but the connector does not activate it in v1.

Requests use a fixed Graph API host and pinned API version, reconstruct cursor
pagination instead of following returned paging URLs, put the token in an
authorization header, cap pages/items/bytes, and retain only a SHA-256 of each
raw response. It never collects consumer accounts, comments, DMs, follower
graphs, engagement counts, precise location, or media binaries.

Activation also requires an owner-only private JSON pinset whose stable numeric
target IDs exactly cover the public bindings. Every Graph response must return the
configured caller ID at the root, the pinned target ID inside Business Discovery,
and the reviewed username; mismatches fail the source with no identifier in public
receipts. IDs remain outside Git and public configuration. China-scoped publishers
use an explicit `source-scoped` policy. Broader publishers use deterministic,
boundary-aware multilingual China keywords and count non-matching posts as rejected
rather than automatically labeling every account post as China-related.

The pinset defaults to `/run/secrets/meta_instagram_target_ids.json`; deployments
may select another absolute owner-only file with
`META_INSTAGRAM_TARGET_PINS_FILE`. It uses schema
`palimpsest-instagram-target-pins.v1` and a sorted `bindings` array of exact
`source_id` / `instagram_user_id` pairs. The adapter rejects symlinks, permissive
file modes, duplicate keys, duplicate IDs, extra rows, and incomplete coverage.

The public coverage receipt records success/failure/not-attempted and accepted or
rejected counts under the phrase `bounded-registry-not-global`; request bounds remain
reviewable in `config/instagram_graph.json`.

### 4. Social observation model

The public relation is fixed to:

`attributed-source-report-not-corroboration`

An observation ID is derived from platform, reviewed source ID, and native
object identity. The native identifier stays private; only the opaque digest
crosses the boundary. A version ID is derived from the complete sanitized
record and its immediate `supersedes_version_id`, so edits and reversions are
visible without changing the observation's identity.

The append-only ledger stores one normalized row per version. The latest document
contains current records plus per-source coverage. Both use canonical JSON and
strict duplicate-key rejection. The validator scans recursively for forbidden
raw, credential, identity, engagement, location, and messaging fields.

### 5. Publication boundary

Telegram collection runs on the ScamShield node and publishes only the sanitized
latest document, immutable versions and an HMAC-SHA256 manifest at exact read-only
HTTPS paths. Instagram collection runs in the hourly publication workflow with
repository secrets only when explicitly enabled. The GitHub workflow:

1. downloads the Telegram latest, versions and manifest without redirects;
2. authenticates the exact bytes before parsing;
3. recomputes `bundle_id = sha256(latest_bytes + NUL + versions_bytes)[:32]`, so
   the bundle identity is bound to the two HMAC-authenticated byte streams;
4. validates the closed public schema and exact local registry digest;
5. checks clocks, the persistent acceptance receipt, monotonic revision chains,
   and refuses remote non-Telegram rows;
6. publishes versions first, latest second, and the acceptance receipt last,
   rolling all attempted files back to their prior bytes on an ordinary failure; and
7. rebuilds the static site after any rebase or push race.

The monotonic receipt is
`/readings/social-observations-import-state.json`, with the closed schema at
`/protocol/social-observations-import-state-v1.schema.json`. It contains only the
authenticated opaque bundle ID, the remote `generated_at`, and the SHA-256 digest
of each remote artifact. The same tuple is an idempotent replay. An older remote
timestamp, a different bundle at the same timestamp, or reuse of a bundle ID with
different bytes fails closed. The receipt does not exist until the first successful
authenticated remote import; Palimpsest does not manufacture a bootstrap acceptance.

The node never receives a GitHub write credential or Meta token. GitHub never
receives a Telegram session credential.

### 6. Public experience

`/news/china/situation/` is a situation desk, not a feed reader. It shows:

- registry status and collection failures;
- platform, publisher-lineage and edit receipts for exact-link observations;
- the original post permalink and matched publisher URL;
- why the item does or does not count as independent evidence; and
- the next verification action.

RSS and JSON Feed are convenience projections of the combined Situation index. The
structured Situation document, social latest artifact and append-only versions remain
the primary machine interfaces.

### 7. China situation synthesis

The public synthesis joins three already-labeled layers without flattening them:

| Layer | What it contributes | What it cannot establish alone |
| --- | --- | --- |
| Publisher wire | Attributed reporting, chronology, source lineage, corrections | Truth of every reported claim |
| Social observations | Distribution, official-account updates, edits, and additional attributed links | Popularity as truth, independence, or representative public opinion |
| Observatory measurements | Current normalized measurements with method and input receipts | Article-specific verification, motive, or causation without an explicit study |

The first join rule is an exact canonical article URL from an allowlisted
publisher account to an in-scope Evidence Wire item. That relation is called
`publisher-link-context`. It places the social post beside the dossier but does
not add a source group. A future title/time similarity join may create a review
candidate, never an automatic public relation.

Each situation record contains:

- the publisher event and its existing source-lineage assessment;
- zero or more exact-link social observations and their edit history;
- the event's already-declared Observatory surfaces, each still labeled
  `topic-surface-only`;
- conflicts and missing layers;
- a deterministic posture such as `report-only`, `report-plus-social-context`,
  `report-plus-measurement-context`, or `three-layer-context`; and
- next checks that say what evidence would change the posture.

The synthesis never generates a stronger conclusion than its inputs. Three
layers can make a dossier more navigable and more complete while all three
relations remain explicitly non-causal and non-confirmatory.

## Data contract

A sanitized observation has this conceptual shape:

```json
{
  "observation_id": "social-<opaque digest>",
  "version_id": "socialv-<opaque digest>",
  "platform": "telegram",
  "source_id": "reviewed-publisher-id",
  "source_name": "Reviewed publisher",
  "source_type": "telegram_channel",
  "independence_group": "reviewed-publisher-editorial",
  "published_at": "2026-08-16T12:00:00Z",
  "first_observed_at": "2026-08-16T12:01:00Z",
  "permalink": "https://t.me/example/123/",
  "title": "Bounded source-supplied headline",
  "excerpt": "Bounded source-supplied excerpt",
  "content_type": "text",
  "content_sha256": "<sha256>",
  "state": "published",
  "china_relevance_labels": ["china", "politics"],
  "related_urls": ["https://publisher.example/article"],
  "supersedes_version_id": null,
  "rights_policy": "metadata-bounded-excerpt-link-only",
  "relation": "attributed-source-report-not-corroboration"
}
```

The concrete runtime schema is authoritative. This example intentionally omits
native object IDs and private checkpoint fields.

## Failure, retry, and recovery behavior

- **No credential or disabled connector:** no network call; publish an explicit
  disabled/not-configured status only when rebuilding from public state. If the
  connector is enabled with credentials but its private target pinset is absent or
  invalid, fail closed before the first request.
- **Partial source failure:** retain successful sources, expose every failed
  source receipt, and keep prior current records until their declared stale
  boundary.
- **All sources fail:** do not replace the last known-good signed snapshot.
- **429/rate limit:** fail the affected source inside the global request budget,
  preserve its prior observation, and retry only on the next scheduled run.
- **401/permission loss:** stop that connector, preserve prior state, and alert
  without printing a token or credential-bearing URL.
- **Schema drift:** reject the affected response and expose a credential-free
  source-failure receipt.
- **Crash after spool write:** replay is idempotent by observation/version ID.
- **Crash after public data but before acceptance receipt:** the receipt is still
  old, so the same authenticated bundle is eligible to replay and completes the
  commit. The receipt is never written ahead of the latest/ledger pair.
- **Rollback or equivocation:** a bundle older than the accepted remote timestamp,
  or a different bundle at that same timestamp, is rejected without touching the
  last-good artifacts.
- **Registry addition:** a prior latest/ledger pair may move to a strict registry
  superset only when its old digest exactly matches the retained current-source
  metadata. Newly added sources receive `not-attempted` receipts; removals,
  unknown IDs, and any retained metadata drift fail closed.
- **Crash before cursor commit:** replay may duplicate input but cannot duplicate
  a normalized version.
- **Edit:** append a new version and point latest at it.
- **Deletion:** publish a tombstone only when observed through a documented API
  signal or a reviewed reconciliation. Absence alone is never interpreted as
  deletion.
- **Publication authentication failure:** preserve the committed artifact and
  monotonic receipt, and fail the workflow loudly.

## Security and privacy boundaries

- Credentials and stable platform identity pins live in owner-only private files,
  never in configuration, logs, status receipts, URLs, Git, or public JSON.
- Network clients allow only fixed official HTTPS hosts, zero redirects, bounded
  response sizes, and explicit timeouts.
- Public registries contain public institutional identities only. Private peer
  IDs, cursors, webhook secrets, and native IDs stay private.
- The process runs with a read-only application tree, a dedicated writable state
  directory, no executable state mount, no Linux capabilities, and a separate
  kill switch for each platform.
- The publication scrubber treats tokens, credential assignments, internal
  paths, raw payload keys, native IDs, and forbidden social fields as release
  blockers.

## Scale and operational limits

The intended scale is tens to low hundreds of reviewed publishers, not arbitrary
user-generated-content crawling. Each run has fixed per-source and global page,
item, byte, and time budgets. Source cursors allow incremental polling, while an
overlap window catches edits and late pagination. The append-only ledger is
retention-bounded by version count and age; pruning produces a signed compaction
receipt rather than silently rewriting history.

If the registry grows beyond a single-node polling budget, partition it by a
stable source hash across workers. Stable IDs and source-level receipts make the
result deterministic regardless of worker completion order.

## Rollout plan

### Phase 1 — safe vertical slice

- land the registry, schema, deterministic model, offline fixtures, architecture
  documentation, and zero-state public desk;
- implement the ScamShield Telegram revision spool/export and Instagram Graph
  adapter with injected network clients and secret/redirect/bound tests;
- deploy collectors disabled by default;
- activate only reviewed sources after credentials and permissions exist; and
- publish social observations without touching Wire v1 evidence counts.

### Phase 2 — source partnerships and coverage

- onboard publisher-operated Telegram channels and Instagram professional
  accounts;
- activate approved hashtag discovery with a reviewed 30-tag rolling budget;
- add coverage history, alerting, and explicit source onboarding receipts; and
- connect exact publisher/article URLs to existing dossiers as context-only
  relations.

### Phase 3 — Evidence Wire v2

Dual-publish a transport-neutral Wire v2 whose item provenance is
`transport + source_document_sha256 + source_endpoint` rather than
`feed_url + feed_sha256`. Prove in tests that an RSS item, Telegram post, and
Instagram post from the same publisher remain one independence group and cannot
inflate evidence strength. Retire v1 only after every consumer has migrated.

## Tradeoffs and decisions to revisit

- **Long polling before webhook:** preserves the node's outbound-only posture but
  gives a shorter recovery window. Revisit if Palimpsest gains a hardened public
  ingress service.
- **Metadata-only publication:** limits quotation and richer analysis, but sharply
  reduces rights, privacy, and deletion risk. Direct publisher agreements may
  justify a richer per-source policy later.
- **Separate lane before Wire v2:** avoids false RSS provenance and protects
  existing consumers, at the cost of temporarily keeping social observations
  outside event clustering.
- **Closed allowlist:** does not provide universal discovery, but makes coverage,
  authorization, and editorial independence auditable.

The architecture should be revisited when Meta changes permissions or rate
limits, Telegram exposes a supported deletion/history signal, the source registry
exceeds one-node budgets, or Wire v2 is ready for dual publication.
